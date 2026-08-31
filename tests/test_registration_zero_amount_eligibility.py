from __future__ import annotations

import threading
import time
from unittest import mock

from fastapi import BackgroundTasks, HTTPException
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
from services.chatgpt_core.payment_eligibility import (
    CHECKOUT_LINK_TYPE_KIND,
    PAYMENT_ELIGIBILITY_BUNDLE_KIND,
    PAYMENT_METHODS_KIND,
    ZERO_AMOUNT_KIND,
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
    eligibility_enabled: bool = True,
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
        registration_zero_amount_eligibility_enabled=eligibility_enabled,
        extra={"mail_provider": "fake", "register_max_attempts": 1},
    )
    if freeze_eligibility_runtime:
        request._registration_eligibility_runtime = {
            "proxy_mode": "direct",
            "checkout_country_code": "VN",
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
            "reason_code": "zero_checkout_amount",
            "message": "最终应付金额为 0.00 VND",
            "checked_at": "now",
            "evidence": {
                "amount_minor": 0,
                "minor_unit_exponent": 2,
                "amount_display": "0.00 VND",
                "currency": "VND",
                "verified_stage": "taxes_refresh",
                "profile": {
                    "proxy_chain": {
                        "checkout": "VN",
                        "promotion": "VN",
                        "taxes": "VN",
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
    assert summary["stop_requested"] is False
    assert summary["submitted"] == 1
    assert summary["counts"]["eligible"] == 1
    assert summary["counts"]["completed"] == 1
    assert summary["results"][0]["amount_minor"] == 0
    assert summary["results"][0]["amount_display"] == "0.00 VND"
    assert summary["profile"]["billing_country"] == "VN"
    assert summary["profile"]["currency"] == "VND"
    assert summary["profile"]["proxy_chain"] == {
        "checkout": "VN",
        "promotion": "VN",
        "taxes": "VN",
    }


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
                        "checkout": "VN",
                        "promotion": "VN",
                        "taxes": "VN",
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


def test_registration_skips_zero_amount_probe_when_disabled():
    snapshot, account = _run_with_probe_result(
        {"state": "eligible"},
        eligibility_enabled=False,
    )

    assert snapshot["status"] == "done"
    assert snapshot["success"] == 1
    assert snapshot["errors"] == []
    assert "registration_zero_amount_eligibility" not in snapshot["meta"]
    assert "chatgpt_zero_amount_eligibility" not in account.get_extra()


def test_registration_coordinators_share_process_wide_two_probe_limit():
    lock = threading.Lock()
    two_running = threading.Event()
    release = threading.Event()
    active = 0
    peak = 0

    def run_account(
        account_id,
        _kind,
        _settings,
        *,
        task_id="",
        stop_checker=None,
    ):
        nonlocal active, peak
        assert callable(stop_checker)
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
            "reason_code": "zero_checkout_amount",
            "message": "ok",
            "checked_at": "now",
            "evidence": {
                "amount_minor": 0,
                "amount_display": "0.00 VND",
                "currency": "VND",
            },
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


def test_registration_coordinator_stop_cancels_queue_and_interrupts_active_probe():
    started = threading.Event()
    calls: list[int] = []
    log_lines: list[tuple[str, str]] = []

    def run_account(
        account_id,
        _kind,
        _settings,
        *,
        task_id="",
        stop_checker=None,
    ):
        assert task_id == "registration-stop"
        assert callable(stop_checker)
        calls.append(account_id)
        started.set()
        while True:
            stop_checker()
            time.sleep(0.01)

    coordinator = RegistrationEligibilityCoordinator(
        task_id="registration-stop",
        settings={"proxy_mode": "direct", "max_attempts": 1},
        run_account=run_account,
        update_meta=lambda _value: None,
        log=lambda message, level: log_lines.append((message, level)),
        concurrency=1,
    )

    assert coordinator.submit(11, "active@example.com")
    assert started.wait(timeout=1)
    assert coordinator.submit(12, "queued@example.com")

    started_at = time.monotonic()
    summary = coordinator.finish(cancel_pending=True, stop_grace_seconds=1)
    elapsed = time.monotonic() - started_at

    assert elapsed < 0.75
    assert calls == [11]
    assert summary["finished"] is True
    assert summary["stop_requested"] is True
    assert summary["cleanup_pending"] == 0
    assert summary["counts"]["queued"] == 0
    assert summary["counts"]["running"] == 0
    assert summary["counts"]["skipped"] == 2
    assert summary["counts"]["completed"] == 2
    assert any("已取消排队=1｜后台清理中=0" in message for message, _ in log_lines)


def test_registration_coordinator_stop_has_bounded_wait_for_noncooperative_probe():
    started = threading.Event()
    release = threading.Event()

    def run_account(
        account_id,
        _kind,
        _settings,
        *,
        task_id="",
        stop_checker=None,
    ):
        assert account_id == 21
        assert task_id == "registration-stubborn-stop"
        assert callable(stop_checker)
        started.set()
        assert release.wait(timeout=3)
        return {
            "account_id": account_id,
            "state": "eligible",
            "reason_code": "zero_checkout_amount",
            "message": "late result",
            "checked_at": "now",
            "evidence": {},
        }

    coordinator = RegistrationEligibilityCoordinator(
        task_id="registration-stubborn-stop",
        settings={"proxy_mode": "direct", "max_attempts": 1},
        run_account=run_account,
        update_meta=lambda _value: None,
        log=lambda _message, _level: None,
        concurrency=1,
    )

    assert coordinator.submit(21, "stubborn@example.com")
    assert started.wait(timeout=1)
    try:
        started_at = time.monotonic()
        summary = coordinator.finish(
            cancel_pending=True,
            stop_grace_seconds=0.05,
        )
        elapsed = time.monotonic() - started_at

        assert elapsed < 0.5
        assert summary["finished"] is True
        assert summary["cleanup_pending"] == 1
        assert summary["counts"]["running"] == 0
        assert summary["counts"]["skipped"] == 1
        assert summary["counts"]["completed"] == 1
    finally:
        release.set()

    deadline = time.monotonic() + 1
    while coordinator._snapshot()["cleanup_pending"] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert coordinator._snapshot()["cleanup_pending"] == 0


def test_registration_coordinator_callback_failures_do_not_break_results():
    coordinator = RegistrationEligibilityCoordinator(
        task_id="registration-callback-failure",
        settings={"proxy_mode": "direct", "max_attempts": 1},
        run_account=lambda account_id, _kind, _settings, **_kwargs: {
            "account_id": account_id,
            "state": "ineligible",
            "reason_code": "nonzero_checkout_amount",
            "message": "amount is nonzero",
            "checked_at": "now",
            "evidence": {
                "amount_minor": 9900,
                "amount_display": "99.00 VND",
                "currency": "VND",
            },
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


def test_registration_coordinator_logs_sanitized_failure_response_instead_of_reason_code():
    log_lines: list[tuple[str, str]] = []
    coordinator = RegistrationEligibilityCoordinator(
        task_id="registration-failure-response-log",
        settings={"proxy_mode": "direct", "max_attempts": 2},
        run_account=lambda account_id, _kind, _settings, **_kwargs: {
            "account_id": account_id,
            "state": "probe_failed",
            "reason_code": "technical_error",
            "message": (
                "checkout 创建 HTTP 400:\n"
                "Our systems have detected unusual activity. Please try again later."
            ),
            "checked_at": "now",
            "evidence": {"attempt_count": 2},
        },
        update_meta=lambda _value: None,
        log=lambda message, level: log_lines.append((message, level)),
        concurrency=1,
    )

    assert coordinator.submit(7, "failure@example.com")
    summary = coordinator.finish()

    completed = [
        (message, level)
        for message, level in log_lines
        if "[0 元试用资格] 完成" in message
    ]
    assert completed == [
        (
            "[0 元试用资格] 完成｜账号=fai***e@example.com｜结果=检测失败｜"
            "报错响应=checkout 创建 HTTP 400: Our systems have detected unusual activity. "
            "Please try again later.",
            "warning",
        )
    ]
    assert "technical_error" not in completed[0][0]
    assert summary["results"][0]["reason_code"] == "technical_error"


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


def test_registration_zero_amount_country_is_frozen_from_request():
    request = RegisterTaskRequest(
        platform="chatgpt",
        registration_zero_amount_eligibility_enabled=True,
        registration_zero_amount_checkout_country="jp",
    )
    background_tasks = BackgroundTasks()
    with (
        mock.patch.object(tasks_api, "_prepare_register_request", return_value=request),
        mock.patch.object(tasks_api, "_create_task_record") as create_task_record,
        mock.patch.object(tasks_api, "_save_task_log"),
        mock.patch.object(tasks_api, "_build_effective_register_extra", return_value={}),
        mock.patch("core.config_store.config_store.get_all", return_value={}),
    ):
        tasks_api.enqueue_register_task(request, background_tasks=background_tasks)

    assert request.registration_zero_amount_checkout_country == "JP"
    assert request.registration_zero_amount_eligibility_enabled is True
    settings = request._registration_eligibility_runtime
    assert settings["checkout_country_code"] == "JP"
    assert settings["promotion_proxy_country_code"] == "JP"
    initial_meta = create_task_record.call_args.args[3]
    assert initial_meta["registration_zero_amount_eligibility_request"] == {
        "enabled": True,
        "checkout_country_code": "JP",
    }


def test_registration_zero_amount_country_rejects_unsupported_value():
    request = RegisterTaskRequest(
        platform="chatgpt",
        registration_zero_amount_eligibility_enabled=True,
        registration_zero_amount_checkout_country="ZZ",
    )
    with mock.patch.object(tasks_api, "_prepare_register_request", return_value=request):
        try:
            tasks_api.enqueue_register_task(request, background_tasks=BackgroundTasks())
        except HTTPException as exc:
            assert exc.status_code == 400
            assert "不受支持" in str(exc.detail)
        else:
            raise AssertionError("unsupported registration eligibility country was accepted")


def test_registration_zero_amount_probe_is_disabled_by_default():
    request = RegisterTaskRequest(
        platform="chatgpt",
        registration_zero_amount_checkout_country="jp",
    )
    background_tasks = BackgroundTasks()
    with (
        mock.patch.object(tasks_api, "_prepare_register_request", return_value=request),
        mock.patch.object(tasks_api, "_create_task_record") as create_task_record,
        mock.patch.object(tasks_api, "_save_task_log"),
        mock.patch.object(tasks_api, "_build_effective_register_extra", return_value={}),
        mock.patch.object(
            tasks_api,
            "_safe_registration_zero_amount_eligibility_settings",
        ) as build_settings,
    ):
        tasks_api.enqueue_register_task(request, background_tasks=background_tasks)

    assert request.registration_zero_amount_eligibility_enabled is False
    assert request.registration_zero_amount_checkout_country == "JP"
    assert request._registration_eligibility_runtime == {}
    build_settings.assert_not_called()
    initial_meta = create_task_record.call_args.args[3]
    assert initial_meta["registration_zero_amount_eligibility_request"] == {
        "enabled": False,
        "checkout_country_code": "JP",
    }


def test_registration_payment_details_freeze_only_requested_bundle_children():
    request = RegisterTaskRequest(
        platform="chatgpt",
        registration_payment_details_enabled=True,
        registration_zero_amount_checkout_country="jp",
    )
    with (
        mock.patch.object(tasks_api, "_prepare_register_request", return_value=request),
        mock.patch.object(tasks_api, "_create_task_record") as create_task_record,
        mock.patch.object(tasks_api, "_save_task_log"),
        mock.patch.object(tasks_api, "_build_effective_register_extra", return_value={}),
        mock.patch.object(
            tasks_api,
            "_safe_registration_zero_amount_eligibility_settings",
            return_value={"checkout_country_code": "JP"},
        ),
    ):
        tasks_api.enqueue_register_task(request, background_tasks=BackgroundTasks())

    assert request.registration_zero_amount_eligibility_enabled is False
    assert request.registration_payment_details_enabled is True
    assert request._registration_eligibility_runtime["bundle_child_kinds"] == [
        CHECKOUT_LINK_TYPE_KIND,
        PAYMENT_METHODS_KIND,
    ]
    initial_meta = create_task_record.call_args.args[3]
    assert initial_meta["registration_payment_details_request"] == {
        "enabled": True,
        "checkout_country_code": "JP",
        "kinds": [CHECKOUT_LINK_TYPE_KIND, PAYMENT_METHODS_KIND],
    }


def test_registration_zero_amount_and_payment_details_share_all_bundle_children():
    request = RegisterTaskRequest(
        platform="chatgpt",
        registration_zero_amount_eligibility_enabled=True,
        registration_payment_details_enabled=True,
    )
    with (
        mock.patch.object(tasks_api, "_prepare_register_request", return_value=request),
        mock.patch.object(tasks_api, "_create_task_record"),
        mock.patch.object(tasks_api, "_save_task_log"),
        mock.patch.object(tasks_api, "_build_effective_register_extra", return_value={}),
        mock.patch.object(
            tasks_api,
            "_safe_registration_zero_amount_eligibility_settings",
            return_value={"checkout_country_code": "VN"},
        ),
    ):
        tasks_api.enqueue_register_task(request, background_tasks=BackgroundTasks())

    assert request._registration_eligibility_runtime["bundle_child_kinds"] == [
        ZERO_AMOUNT_KIND,
        CHECKOUT_LINK_TYPE_KIND,
        PAYMENT_METHODS_KIND,
    ]


def test_registration_payment_details_coordinator_tracks_independent_child_states():
    snapshots = []
    run_account = mock.Mock(
        return_value={
            "account_id": 91,
            "email": "details@example.com",
            "state": "completed",
            "reason_code": "bundle_completed",
            "message": "done",
            "checked_at": "now",
            "results": [
                {
                    "kind": CHECKOUT_LINK_TYPE_KIND,
                    "state": "oaics",
                    "reason_code": "checkout_link_type_detected",
                    "checked_at": "now",
                    "evidence": {},
                },
                {
                    "kind": PAYMENT_METHODS_KIND,
                    "state": "available",
                    "reason_code": "payment_methods_available",
                    "checked_at": "now",
                    "evidence": {},
                },
            ],
        }
    )
    coordinator = RegistrationEligibilityCoordinator(
        task_id="registration-payment-details",
        settings={
            "proxy_mode": "direct",
            "bundle_child_kinds": [
                CHECKOUT_LINK_TYPE_KIND,
                PAYMENT_METHODS_KIND,
            ],
        },
        run_account=run_account,
        update_meta=lambda value: snapshots.append(value),
        log=lambda _message, _level: None,
        kind=PAYMENT_ELIGIBILITY_BUNDLE_KIND,
        concurrency=1,
    )

    assert coordinator.submit(91, "details@example.com")
    summary = coordinator.finish()

    assert run_account.call_args.args[1] == PAYMENT_ELIGIBILITY_BUNDLE_KIND
    assert summary["counts"]["completed"] == 1
    assert summary["counts"]["bundle_completed"] == 1
    assert summary["child_counts"][CHECKOUT_LINK_TYPE_KIND]["oaics"] == 1
    assert summary["child_counts"][PAYMENT_METHODS_KIND]["available"] == 1
    assert snapshots[-1]["finished"] is True


def test_registration_bundle_child_filter_rejects_unrequested_zero_amount_result():
    selected = tasks_api._payment_eligibility_bundle_child_kinds(
        {
            "bundle_child_kinds": [
                CHECKOUT_LINK_TYPE_KIND,
                PAYMENT_METHODS_KIND,
                "unknown",
            ]
        }
    )

    assert selected == (CHECKOUT_LINK_TYPE_KIND, PAYMENT_METHODS_KIND)
