from __future__ import annotations

import threading
import time
from unittest import mock

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import api.tasks as tasks_api
from api.tasks import RegisterTaskRequest, _create_task_record, _run_register
from core import db as core_db
from core.base_mailbox import BaseMailbox, MailboxAccount
from core.base_platform import Account, BasePlatform
from core.db import AccountModel
from services.chatgpt_core.registration_eligibility import (
    RegistrationEligibilityCoordinator,
)


class _Mailbox(BaseMailbox):
    def get_email(self) -> MailboxAccount:
        return MailboxAccount(email="registered-zero@example.com")

    def get_current_ids(self, account: MailboxAccount) -> set:
        return set()

    def wait_for_code(self, account: MailboxAccount, **kwargs) -> str:
        return "123456"


class _Platform(BasePlatform):
    name = "chatgpt"
    display_name = "ChatGPT"

    def __init__(self, config=None, mailbox=None):
        super().__init__(config)
        self.mailbox = mailbox

    def register(self, email: str | None = None, password: str | None = None) -> Account:
        return Account(
            platform="chatgpt",
            email="registered-zero@example.com",
            password=password or "pw",
            token="at-registered-zero",
            extra={"access_token": "at-registered-zero"},
        )

    def check_valid(self, account: Account) -> bool:
        return True


def _run_with_probe_result(
    probe_result: dict,
    *,
    freeze_eligibility_runtime: bool = True,
    eligibility_settings_error: Exception | None = None,
) -> tuple[dict, AccountModel]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    request = RegisterTaskRequest(
        platform="chatgpt",
        count=1,
        concurrency=1,
        executor_type="protocol",
        proxy_mode="direct",
        extra={"mail_provider": "fake", "register_max_attempts": 1},
    )
    if freeze_eligibility_runtime:
        request._registration_eligibility_runtime = {
            "proxy_mode": "direct",
            "promotion_proxy_country_code": "VN",
            "max_attempts": 1,
        }
    task_id = f"task-registration-zero-amount-{probe_result.get('state') or 'unknown'}"

    with (
        mock.patch.object(core_db, "engine", engine),
        mock.patch.object(tasks_api, "engine", engine),
    ):
        SQLModel.metadata.create_all(engine)
        _create_task_record(task_id, request, "manual", None)
        eligibility_settings_patch = mock.patch.object(
            tasks_api,
            "_registration_zero_amount_eligibility_settings",
            side_effect=eligibility_settings_error,
        ) if eligibility_settings_error is not None else mock.patch.object(
            tasks_api,
            "_registration_zero_amount_eligibility_settings",
            wraps=tasks_api._registration_zero_amount_eligibility_settings,
        )
        with (
            mock.patch("core.config_store.config_store.get_all", return_value={}),
            mock.patch("services.chatgpt_core.ChatGPTPlatform", _Platform),
            mock.patch("core.base_mailbox.create_mailbox", return_value=_Mailbox()),
            mock.patch("core.proxy_utils.resolve_task_proxy_candidates", return_value=[("", None, "direct")]),
            mock.patch("api.tasks._auto_upload_integrations"),
            mock.patch("api.tasks._save_task_log"),
            mock.patch("api.tasks.schedule_chatgpt_local_status_refresh_for_account_id"),
            mock.patch("services.chatgpt_core.local_status_refresh.schedule_chatgpt_local_status_refresh_for_account_id"),
            mock.patch.object(tasks_api, "probe_zero_amount_eligibility", return_value=probe_result),
            eligibility_settings_patch,
        ):
            _run_register(task_id, request)

        snapshot = tasks_api._task_store.snapshot(task_id)
        with Session(engine) as session:
            account = session.exec(select(AccountModel)).one()
            session.expunge(account)
        return snapshot, account


def test_registration_success_automatically_persists_zero_amount_result():
    snapshot, account = _run_with_probe_result(
        {
            "state": "eligible",
            "reason_code": "zero_php",
            "message": "最终应付金额为 0 PHP",
            "checked_at": "now",
            "evidence": {
                "amount_minor": 0,
                "currency": "PHP",
                "verified_stage": "taxes_refresh",
                "profile": {
                    "proxy_chain": {
                        "checkout": "US",
                        "promotion": "VN",
                        "taxes": "US",
                    }
                },
            },
        }
    )

    marker = account.get_extra()["chatgpt_zero_amount_eligibility"]
    summary = snapshot["meta"]["registration_zero_amount_eligibility"]
    assert snapshot["status"] == "done"
    assert snapshot["success"] == 1
    assert snapshot["errors"] == []
    assert marker["confirmed_state"] == "eligible"
    assert summary["finished"] is True
    assert summary["submitted"] == 1
    assert summary["counts"]["eligible"] == 1
    assert summary["counts"]["completed"] == 1
    assert summary["results"][0]["amount_minor"] == 0


def test_registration_probe_failure_does_not_reclassify_registration_success():
    snapshot, account = _run_with_probe_result(
        {
            "state": "probe_failed",
            "reason_code": "technical_error",
            "message": "temporary proxy failure",
            "checked_at": "now",
            "evidence": {
                "attempt_count": 1,
                "profile": {
                    "proxy_chain": {
                        "checkout": "US",
                        "promotion": "VN",
                        "taxes": "US",
                    }
                },
            },
        }
    )

    marker = account.get_extra()["chatgpt_zero_amount_eligibility"]
    summary = snapshot["meta"]["registration_zero_amount_eligibility"]
    assert snapshot["status"] == "done"
    assert snapshot["success"] == 1
    assert snapshot["errors"] == []
    assert "confirmed_state" not in marker
    assert marker["last_attempt"]["state"] == "probe_failed"
    assert summary["counts"]["probe_failed"] == 1
    assert summary["counts"]["completed"] == 1


def test_registration_eligibility_configuration_failure_does_not_fail_registration():
    snapshot, account = _run_with_probe_result(
        {"state": "eligible"},
        freeze_eligibility_runtime=False,
        eligibility_settings_error=RuntimeError("broken eligibility proxy config"),
    )

    marker = account.get_extra()["chatgpt_zero_amount_eligibility"]
    summary = snapshot["meta"]["registration_zero_amount_eligibility"]
    assert snapshot["status"] == "done"
    assert snapshot["success"] == 1
    assert snapshot["errors"] == []
    assert marker["last_attempt"]["state"] == "probe_failed"
    assert marker["last_attempt"]["reason_code"] == "configuration_error"
    assert "broken eligibility proxy config" in marker["last_attempt"]["message"]
    assert summary["finished"] is True
    assert summary["counts"]["probe_failed"] == 1
    assert summary["counts"]["completed"] == 1


def test_registration_coordinators_share_process_wide_two_probe_limit():
    lock = threading.Lock()
    two_running = threading.Event()
    release = threading.Event()
    active = 0
    peak = 0

    def run_account(account_id, _kind, _settings, *, task_id=""):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if active >= 2:
                two_running.set()
        assert release.wait(timeout=3), f"probe release timed out for {task_id}:{account_id}"
        with lock:
            active -= 1
        return {
            "account_id": account_id,
            "state": "eligible",
            "reason_code": "zero_php",
            "message": "ok",
            "checked_at": "now",
            "evidence": {"amount_minor": 0, "currency": "PHP"},
        }

    snapshots: dict[str, dict] = {}

    def coordinator(task_id: str) -> RegistrationEligibilityCoordinator:
        return RegistrationEligibilityCoordinator(
            task_id=task_id,
            settings={"proxy_mode": "direct", "max_attempts": 1},
            run_account=run_account,
            update_meta=lambda value: snapshots.__setitem__(task_id, value),
            log=lambda _message, _level: None,
            concurrency=99,
        )

    first = coordinator("registration-a")
    second = coordinator("registration-b")
    for account_id in (1, 2):
        assert first.submit(account_id, f"a{account_id}@example.com")
    for account_id in (3, 4):
        assert second.submit(account_id, f"b{account_id}@example.com")

    try:
        assert two_running.wait(timeout=3)
        time.sleep(0.1)
        with lock:
            assert active == 2
            assert peak == 2
    finally:
        release.set()

    first_summary = first.finish()
    second_summary = second.finish()
    assert first_summary["effective_concurrency"] == 2
    assert second_summary["effective_concurrency"] == 2
    assert first_summary["global_concurrency_limit"] == 2
    assert second_summary["global_concurrency_limit"] == 2
    assert first_summary["counts"]["completed"] == 2
    assert second_summary["counts"]["completed"] == 2
    assert snapshots["registration-a"]["finished"] is True
    assert snapshots["registration-b"]["finished"] is True


def test_registration_coordinator_callback_failures_do_not_break_results():
    coordinator = RegistrationEligibilityCoordinator(
        task_id="registration-callback-failure",
        settings={"proxy_mode": "direct", "max_attempts": 1},
        run_account=lambda account_id, _kind, _settings, **_kwargs: {
            "account_id": account_id,
            "state": "ineligible",
            "reason_code": "nonzero_php",
            "message": "amount is nonzero",
            "checked_at": "now",
            "evidence": {"amount_minor": 9900, "currency": "PHP"},
        },
        update_meta=mock.Mock(side_effect=RuntimeError("meta unavailable")),
        log=mock.Mock(side_effect=RuntimeError("log unavailable")),
        concurrency=1,
    )

    assert coordinator.submit(5, "callback@example.com")
    summary = coordinator.finish()
    assert summary["finished"] is True
    assert summary["counts"]["ineligible"] == 1
    assert summary["counts"]["completed"] == 1


def test_registration_coordinator_executor_startup_failure_is_recorded():
    run_account = mock.Mock(
        return_value={
            "account_id": 6,
            "state": "probe_failed",
            "reason_code": "configuration_error",
            "message": "thread pool unavailable",
            "checked_at": "now",
            "evidence": {},
        }
    )
    snapshots: list[dict] = []
    with mock.patch(
        "services.chatgpt_core.registration_eligibility.ThreadPoolExecutor",
        side_effect=RuntimeError("thread pool unavailable"),
    ):
        coordinator = RegistrationEligibilityCoordinator(
            task_id="registration-executor-failure",
            settings={"proxy_mode": "direct", "max_attempts": "invalid"},
            run_account=run_account,
            update_meta=lambda value: snapshots.append(value),
            log=lambda _message, _level: None,
            concurrency=2,
        )

    assert coordinator.submit(6, "executor@example.com")
    summary = coordinator.finish()
    assert summary["finished"] is True
    assert summary["max_attempts"] == 2
    assert summary["counts"]["probe_failed"] == 1
    assert summary["counts"]["completed"] == 1
    assert snapshots[-1]["finished"] is True
    settings = run_account.call_args.args[2]
    assert "thread pool unavailable" in settings["_configuration_error"]
