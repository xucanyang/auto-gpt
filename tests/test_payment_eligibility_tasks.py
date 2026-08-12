from __future__ import annotations

from unittest import mock

from fastapi import HTTPException
import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import api.tasks as tasks_api
from api.tasks import (
    BatchPaymentEligibilityTaskRequest,
    PaymentEligibilityTaskRequest,
    _persist_payment_eligibility_result,
    _run_payment_eligibility_for_account,
    _run_payment_eligibility_task,
    enqueue_batch_payment_eligibility_task,
    enqueue_payment_eligibility_task,
)
from core import db as core_db
from core.db import AccountModel, AccountListStateModel
from core.task_runtime import SkipCurrentAttemptRequested
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
                PaymentEligibilityTaskRequest(
                    account_id=free_id,
                    proxy_mode="direct",
                    promotion_proxy_country_code="jp",
                ),
                ZERO_AMOUNT_KIND,
                background_tasks=single_bg,
            )
            batch = enqueue_batch_payment_eligibility_task(
                BatchPaymentEligibilityTaskRequest(
                    account_ids=[free_id, paid_id],
                    params={
                        "proxy_mode": "direct",
                        "concurrency": 2,
                        "promotion_proxy_country_code": "JP",
                    },
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
        assert single_bg.calls[0][0][4]["promotion_proxy_country_code"] == "JP"
        assert batch_bg.calls[0][0][4]["promotion_proxy_country_code"] == "VN"
        assert tasks_api._task_store.snapshot(single_id)["meta"]["proxy_chain"] == {
            "checkout": "US",
            "promotion": "JP",
            "taxes": "US",
        }
        assert tasks_api._task_store.snapshot(batch["task_id"])["meta"]["proxy_chain"] == {
            "checkout": "US",
            "promotion": "VN",
            "taxes": "US",
        }


def test_payment_eligibility_country_validation_rejects_invalid_single_and_batch_values():
    engine = create_engine("sqlite://")
    with mock.patch.object(core_db, "engine", engine), mock.patch.object(tasks_api, "engine", engine):
        SQLModel.metadata.create_all(engine)
        account_id = _add_account(engine, email="country-validation@example.com")
        with pytest.raises(HTTPException) as single_error:
            enqueue_payment_eligibility_task(
                PaymentEligibilityTaskRequest(
                    account_id=account_id,
                    proxy_mode="direct",
                    promotion_proxy_country_code="JPN",
                ),
                ZERO_AMOUNT_KIND,
                background_tasks=_BackgroundTasks(),
            )
        assert single_error.value.status_code == 400

        with pytest.raises(HTTPException) as batch_error:
            enqueue_batch_payment_eligibility_task(
                BatchPaymentEligibilityTaskRequest(
                    account_ids=[account_id],
                    params={
                        "proxy_mode": "direct",
                        "promotion_proxy_country_code": "1P",
                    },
                ),
                ZERO_AMOUNT_KIND,
                background_tasks=_BackgroundTasks(),
            )
        assert batch_error.value.status_code == 400


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


def test_shared_account_runner_persists_confirmed_result_under_identity_gate():
    engine = create_engine("sqlite://")
    with mock.patch.object(core_db, "engine", engine), mock.patch.object(tasks_api, "engine", engine):
        SQLModel.metadata.create_all(engine)
        account_id = _add_account(engine, email="shared-runner@example.com")
        with mock.patch.object(
            tasks_api,
            "probe_zero_amount_eligibility",
            return_value={
                "state": "eligible",
                "reason_code": "zero_php",
                "message": "最终应付金额为 0 PHP",
                "checked_at": "now",
                "evidence": {
                    "amount_minor": 0,
                    "currency": "PHP",
                    "profile": {
                        "proxy_chain": {
                            "checkout": "US",
                            "promotion": "VN",
                            "taxes": "US",
                        }
                    },
                },
            },
        ):
            result = _run_payment_eligibility_for_account(
                account_id,
                ZERO_AMOUNT_KIND,
                {"proxy_mode": "direct", "max_attempts": 1},
                task_id="task-shared-runner",
            )

        assert result["status"] == "classified"
        assert result["state"] == "eligible"
        with Session(engine) as session:
            account = session.get(AccountModel, account_id)
            assert account is not None
            marker = account.get_extra()["chatgpt_zero_amount_eligibility"]
            assert marker["confirmed_state"] == "eligible"
            assert marker["last_attempt"]["reason_code"] == "zero_php"


def test_shared_account_runner_records_pending_auth_without_calling_probe():
    engine = create_engine("sqlite://")
    with mock.patch.object(core_db, "engine", engine), mock.patch.object(tasks_api, "engine", engine):
        SQLModel.metadata.create_all(engine)
        account_id = _add_account(engine, email="pending-auth@example.com", token="")
        with mock.patch.object(tasks_api, "probe_zero_amount_eligibility") as probe:
            result = _run_payment_eligibility_for_account(
                account_id,
                ZERO_AMOUNT_KIND,
                {"proxy_mode": "direct", "max_attempts": 1},
                task_id="task-pending-auth",
            )

        probe.assert_not_called()
        assert result["status"] == "skipped"
        assert result["state"] == "pending_auth"
        with Session(engine) as session:
            account = session.get(AccountModel, account_id)
            assert account is not None
            marker = account.get_extra()["chatgpt_zero_amount_eligibility"]
            assert "confirmed_state" not in marker
            assert marker["last_attempt"]["state"] == "pending_auth"


def test_shared_account_runner_converges_unexpected_exception_to_probe_failed():
    engine = create_engine("sqlite://")
    with mock.patch.object(core_db, "engine", engine), mock.patch.object(tasks_api, "engine", engine):
        SQLModel.metadata.create_all(engine)
        account_id = _add_account(engine, email="runner-exception@example.com")
        with mock.patch.object(
            tasks_api,
            "probe_zero_amount_eligibility",
            side_effect=RuntimeError("unexpected upstream failure"),
        ):
            result = _run_payment_eligibility_for_account(
                account_id,
                ZERO_AMOUNT_KIND,
                {"proxy_mode": "direct", "max_attempts": 1},
                task_id="task-runner-exception",
            )

        assert result["status"] == "failed"
        assert result["state"] == "probe_failed"
        assert result["reason_code"] == "task_exception"
        with Session(engine) as session:
            account = session.get(AccountModel, account_id)
            assert account is not None
            marker = account.get_extra()["chatgpt_zero_amount_eligibility"]
            assert marker["last_attempt"]["state"] == "probe_failed"
            assert marker["last_attempt"]["reason_code"] == "task_exception"


def test_shared_account_runner_closes_running_attempt_when_probe_is_interrupted():
    engine = create_engine("sqlite://")
    with mock.patch.object(core_db, "engine", engine), mock.patch.object(tasks_api, "engine", engine):
        SQLModel.metadata.create_all(engine)
        account_id = _add_account(
            engine,
            email="interrupted@example.com",
            extra={
                "chatgpt_zero_amount_eligibility": {
                    "confirmed_state": "eligible",
                    "confirmed_at": "old",
                }
            },
        )
        with mock.patch.object(
            tasks_api,
            "probe_zero_amount_eligibility",
            side_effect=SkipCurrentAttemptRequested("operator skipped probe"),
        ):
            with pytest.raises(SkipCurrentAttemptRequested):
                _run_payment_eligibility_for_account(
                    account_id,
                    ZERO_AMOUNT_KIND,
                    {"proxy_mode": "direct", "max_attempts": 1},
                    task_id="task-interrupted-runner",
                )

        with Session(engine) as session:
            account = session.get(AccountModel, account_id)
            assert account is not None
            marker = account.get_extra()["chatgpt_zero_amount_eligibility"]
            assert marker["confirmed_state"] == "eligible"
            assert marker["confirmed_at"] == "old"
            assert marker["last_attempt"]["state"] == "skipped"
            assert marker["last_attempt"]["reason_code"] == "probe_interrupted"
            assert marker["last_attempt"]["message"] == "operator skipped probe"


def test_confirmed_results_persist_payment_eligibility_proxy_chain():
    engine = create_engine("sqlite://")
    with mock.patch.object(core_db, "engine", engine), mock.patch.object(tasks_api, "engine", engine):
        SQLModel.metadata.create_all(engine)
        zero_id = _add_account(engine, email="zero-profile@example.com")
        gcash_id = _add_account(engine, email="gcash-profile@example.com")
        _persist_payment_eligibility_result(
            zero_id,
            ZERO_AMOUNT_KIND,
            {
                "state": "eligible",
                "checked_at": "now",
                "evidence": {
                    "amount_minor": 0,
                    "profile": {
                        "proxy_chain": {
                            "checkout": "US",
                            "promotion": "JP",
                            "taxes": "US",
                        }
                    },
                },
            },
        )
        _persist_payment_eligibility_result(
            gcash_id,
            GCASH_KIND,
            {
                "state": "available",
                "checked_at": "now",
                "evidence": {
                    "custom_payment_method_count": 1,
                    "profile": {
                        "proxy_chain": {
                            "checkout": "US",
                            "promotion": "JP",
                            "taxes": "US",
                        }
                    },
                },
            },
        )

        with Session(engine) as session:
            zero = session.get(AccountModel, zero_id)
            gcash = session.get(AccountModel, gcash_id)
            assert zero is not None
            assert gcash is not None
            assert zero.get_extra()["chatgpt_zero_amount_eligibility"]["profile"]["proxy_chain"] == {
                "checkout": "US",
                "promotion": "JP",
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
