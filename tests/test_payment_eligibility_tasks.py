from __future__ import annotations

from unittest import mock

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import api.tasks as tasks_api
from api.tasks import (
    BatchPaymentEligibilityTaskRequest,
    PaymentEligibilityTaskRequest,
    _persist_payment_eligibility_result,
    _run_payment_eligibility_task,
    enqueue_batch_payment_eligibility_task,
    enqueue_payment_eligibility_task,
)
from core import db as core_db
from core.db import AccountModel, AccountListStateModel
from services.chatgpt_core.payment_eligibility import GCASH_KIND, ZERO_AMOUNT_KIND


class _BackgroundTasks:
    def __init__(self):
        self.calls = []

    def add_task(self, *args, **kwargs):
        self.calls.append((args, kwargs))


def _add_account(engine, *, email: str, token: str = "at", status: str = "registered", extra=None) -> int:
    account = AccountModel(platform="chatgpt", email=email, password="pw", token=token, status=status)
    account.set_extra({"access_token": token, **(extra or {})})
    with Session(engine) as session:
        session.add(account)
        session.commit()
        session.refresh(account)
        return int(account.id or 0)


def test_single_and_batch_sources_are_independent_and_prescreened():
    engine = create_engine("sqlite://")
    with mock.patch.object(core_db, "engine", engine), mock.patch.object(tasks_api, "engine", engine):
        SQLModel.metadata.create_all(engine)
        free_id = _add_account(engine, email="free@example.com")
        paid_id = _add_account(engine, email="paid@example.com", status="subscribed")
        single_bg = _BackgroundTasks()
        batch_bg = _BackgroundTasks()
        with mock.patch("api.tasks._save_task_log"):
            single_id = enqueue_payment_eligibility_task(
                PaymentEligibilityTaskRequest(account_id=free_id, proxy_mode="direct"),
                ZERO_AMOUNT_KIND,
                background_tasks=single_bg,
            )
            batch = enqueue_batch_payment_eligibility_task(
                BatchPaymentEligibilityTaskRequest(
                    account_ids=[free_id, paid_id],
                    params={"proxy_mode": "direct", "concurrency": 2},
                ),
                GCASH_KIND,
                background_tasks=batch_bg,
            )
        assert tasks_api._task_store.snapshot(single_id)["source"] == "zero_amount_eligibility"
        assert batch["task_id"]
        assert tasks_api._task_store.snapshot(batch["task_id"])["source"] == "batch_gcash_payment_method"
        assert batch["eligible"] == 1
        assert batch["skipped"] == 1
        assert "已订阅" in batch["skipped_items"][0]["reason"]
        assert single_bg.calls[0][0][3] == ZERO_AMOUNT_KIND
        assert batch_bg.calls[0][0][3] == GCASH_KIND


def test_prescreened_single_account_is_counted_as_skipped():
    engine = create_engine("sqlite://")
    with mock.patch.object(core_db, "engine", engine), mock.patch.object(tasks_api, "engine", engine):
        SQLModel.metadata.create_all(engine)
        account_id = _add_account(engine, email="subscribed@example.com", status="subscribed")
        background = _BackgroundTasks()
        with mock.patch.object(tasks_api, "_save_task_log"):
            task_id = enqueue_payment_eligibility_task(
                PaymentEligibilityTaskRequest(account_id=account_id, proxy_mode="direct"),
                GCASH_KIND,
                background_tasks=background,
            )
            runner_args = background.calls[0][0]
            runner_args[0](*runner_args[1:])
        snapshot = tasks_api._task_store.snapshot(task_id)
        assert snapshot["success"] == 0
        assert snapshot["skipped"] == 1
        assert snapshot["meta"]["eligibility_summary"]["skipped"] == 1


def test_probe_failed_is_error_not_classified_success():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with mock.patch.object(core_db, "engine", engine), mock.patch.object(tasks_api, "engine", engine):
        SQLModel.metadata.create_all(engine)
        account_id = _add_account(engine, email="failed@example.com")
        task_id = "task_payment_eligibility_failed"
        tasks_api._task_store.create(
            task_id,
            platform="chatgpt",
            total=1,
            source="zero_amount_eligibility",
            meta={"email": "failed@example.com", "skipped_items": [], "missing_ids": []},
            supports_after_current=True,
        )
        with mock.patch.object(
            tasks_api,
            "probe_zero_amount_eligibility",
            return_value={
                "state": "probe_failed",
                "reason_code": "temporary_error",
                "message": "temporary failure",
                "checked_at": "now",
            },
        ), mock.patch.object(tasks_api, "_save_task_log"):
            _run_payment_eligibility_task(
                task_id,
                [account_id],
                ZERO_AMOUNT_KIND,
                {"proxy_mode": "direct", "concurrency": 1, "max_attempts": 1},
            )
        snapshot = tasks_api._task_store.snapshot(task_id)
        assert snapshot["status"] == "done"
        assert snapshot["success"] == 0
        assert snapshot["errors"]
        assert snapshot["meta"]["eligibility_summary"]["probe_failed"] == 1


def test_technical_failure_preserves_previous_confirmed_state():
    engine = create_engine("sqlite://")
    with mock.patch.object(core_db, "engine", engine), mock.patch.object(tasks_api, "engine", engine):
        SQLModel.metadata.create_all(engine)
        account_id = _add_account(
            engine,
            email="preserve@example.com",
            extra={
                "chatgpt_zero_amount_eligibility": {
                    "confirmed_state": "eligible",
                    "confirmed_at": "old",
                    "evidence": {"amount_minor": 0},
                }
            },
        )
        _persist_payment_eligibility_result(
            account_id,
            ZERO_AMOUNT_KIND,
            {
                "state": "probe_failed",
                "reason_code": "technical_error",
                "message": "temporary",
                "checked_at": "new",
                "evidence": {"attempt_count": 2},
            },
            task_id="task-preserve",
        )
        with Session(engine) as session:
            account = session.get(AccountModel, account_id)
            assert account is not None
            marker = account.get_extra()["chatgpt_zero_amount_eligibility"]
            assert marker["confirmed_state"] == "eligible"
            assert marker["confirmed_at"] == "old"
            assert marker["last_attempt"]["state"] == "probe_failed"
            state = session.get(AccountListStateModel, account_id)
            assert state is not None
            assert state.zero_amount_eligibility_state == "eligible"


def test_confirmed_results_persist_kind_specific_proxy_chains():
    engine = create_engine("sqlite://")
    with mock.patch.object(core_db, "engine", engine), mock.patch.object(tasks_api, "engine", engine):
        SQLModel.metadata.create_all(engine)
        zero_id = _add_account(engine, email="zero-profile@example.com")
        gcash_id = _add_account(engine, email="gcash-profile@example.com")
        _persist_payment_eligibility_result(
            zero_id,
            ZERO_AMOUNT_KIND,
            {"state": "eligible", "checked_at": "now", "evidence": {"amount_minor": 0}},
        )
        _persist_payment_eligibility_result(
            gcash_id,
            GCASH_KIND,
            {"state": "available", "checked_at": "now", "evidence": {"custom_payment_method_count": 1}},
        )

        with Session(engine) as session:
            zero = session.get(AccountModel, zero_id)
            gcash = session.get(AccountModel, gcash_id)
            assert zero is not None
            assert gcash is not None
            assert zero.get_extra()["chatgpt_zero_amount_eligibility"]["profile"]["proxy_chain"] == {
                "checkout": "US",
                "promotion": "US",
                "taxes": "US",
            }
            assert gcash.get_extra()["chatgpt_gcash_payment_method"]["profile"]["proxy_chain"] == {
                "checkout": "US",
                "promotion": "VN",
                "taxes": "US",
            }


def test_dynamic_global_proxy_meta_is_not_reported_as_direct():
    meta = tasks_api._custom_email_proxy_meta(
        {
            "proxy_mode": "dynamic",
            "proxy": "",
            "proxy_country_code": "JP",
            "proxy_failover": True,
        }
    )

    assert meta["template"] == "global"
    assert meta["template_redacted"] == "global"
