from __future__ import annotations

import threading
import time
from contextlib import ExitStack
from unittest import mock

import pytest
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import api.tasks as tasks_api
from api.tasks import RegisterTaskRequest
from core import db as core_db
from core.db import AccountModel
from services.chatgpt_core import registration_paypal_payment as payment_module
from services.chatgpt_core.long_link_payment_client import LongLinkPaymentClient
from services.chatgpt_core.paypal_agreement_auto_client import (
    PaypalAgreementAutoClient,
    PaypalAgreementAutoError,
    normalize_paypal_approval_url,
)


PAYPAL_URL = "https://www.paypal.com/agreements/approve?ba_token=BA-ABCDEFGH1234"
PROFILE_HASH = "p" * 64


class _Response:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self.payload = payload

    def json(self):
        return self.payload


class _Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


def _payment_profile():
    return {
        "configured": True,
        "ready": True,
        "country": "GB",
        "proxy_country": "GB",
        "buyer_mode": "identity_elevation",
        "browser_profile": "chrome146",
        "matching_phone_count": 8,
        "queue": {"batch_count": 0, "pending_items": 0, "status_counts": {}},
    }


def _settings():
    return {
        "profile_hash": PROFILE_HASH,
        "link_type": "paypal",
        "link_profile": {
            "link_type": "paypal",
            "profile_hash": PROFILE_HASH,
            "country": "US",
            "currency": "USD",
        },
        "payment_profile": _payment_profile(),
    }


def test_internal_client_profile_and_enqueue_redact_approval_url():
    token = "t" * 64
    session = _Session(
        [
            _Response(200, {"ok": True, **_payment_profile()}),
            _Response(
                201,
                {
                    "ok": True,
                    "created": True,
                    "idempotent": False,
                    "state": "accepted",
                    "batch": {"id": "batch123456", "status": "draft"},
                    "item": {"id": "item123456", "status": "pending"},
                },
            ),
        ]
    )
    client = PaypalAgreementAutoClient(
        base_url="http://paypal.internal:18098",
        api_token=token,
        session=session,
    )

    profile = client.get_profile()
    result = client.enqueue(PAYPAL_URL)

    assert profile["ready"] is True
    assert result == {
        "batch_id": "batch123456",
        "item_id": "item123456",
        "created": True,
        "idempotent": False,
        "state": "accepted",
        "batch_status": "draft",
        "remote_status": "pending",
    }
    assert session.calls[0][2]["headers"]["X-Internal-Auto-Channel"] == "1"
    assert session.calls[1][2]["headers"]["Authorization"] == f"Bearer {token}"
    assert session.calls[1][2]["json"] == {"paypal_url": PAYPAL_URL}
    assert PAYPAL_URL not in repr(result)


def test_internal_client_rejects_non_paypal_or_malformed_approval_urls():
    invalid = [
        "https://example.com/agreements/approve?ba_token=BA-ABCDEFGH1234",
        "https://www.paypal.com/checkout?ba_token=BA-ABCDEFGH1234",
        "https://www.paypal.com/agreements/approve?ba_token=BA-short",
        "https://www.paypal.com/agreements/approve?ba_token=BA-ABCDEFGH1234&x=BA-ABCDEFGH1234",
    ]
    for value in invalid:
        with pytest.raises(PaypalAgreementAutoError):
            normalize_paypal_approval_url(value)


def test_internal_client_errors_do_not_echo_token_or_url():
    token = "t" * 64
    session = _Session(
        [
            _Response(
                503,
                {"detail": f"bad {token} {PAYPAL_URL}"},
            )
        ]
    )
    client = PaypalAgreementAutoClient(
        base_url="http://paypal.internal:18098",
        api_token=token,
        session=session,
    )
    with pytest.raises(PaypalAgreementAutoError) as raised:
        client.get_profile()
    assert token not in str(raised.value)
    assert PAYPAL_URL not in str(raised.value)
    assert "REDACTED" in str(raised.value)


def test_registration_paypal_profile_is_frozen_and_requires_paypal_ready(monkeypatch):
    link_client = mock.Mock()
    link_client.get_profile.return_value = {
        "link_type": "paypal",
        "profile_hash": PROFILE_HASH,
        "country": "US",
        "currency": "USD",
        "effective_concurrency": 2,
    }
    payment_client = mock.Mock()
    payment_client.get_profile.return_value = _payment_profile()
    monkeypatch.setattr(
        LongLinkPaymentClient,
        "from_env",
        classmethod(lambda cls: link_client),
    )
    monkeypatch.setattr(
        payment_module.PaypalAgreementAutoClient,
        "from_env",
        classmethod(lambda cls: payment_client),
    )

    frozen = tasks_api._registration_paypal_payment_settings()
    assert frozen["profile_hash"] == PROFILE_HASH
    assert frozen["link_profile"]["country"] == "US"
    assert frozen["payment_profile"]["country"] == "GB"

    link_client.get_profile.return_value["link_type"] = "pix"
    with pytest.raises(HTTPException) as raised:
        tasks_api._registration_paypal_payment_settings()
    assert raised.value.status_code == 400

    link_client.get_profile.return_value["link_type"] = "paypal"
    payment_client.get_profile.return_value = {
        **_payment_profile(),
        "ready": False,
        "blocking_reason": "没有可用手机号",
    }
    with pytest.raises(HTTPException) as raised:
        tasks_api._registration_paypal_payment_settings()
    assert raised.value.status_code == 503


def test_registration_paypal_disabled_keeps_legacy_enqueue_contract(monkeypatch):
    request = RegisterTaskRequest(platform="chatgpt")
    created = {}
    monkeypatch.setattr(tasks_api, "_prepare_register_request", lambda req: req)
    monkeypatch.setattr(tasks_api, "_build_effective_register_extra", lambda req: {})
    monkeypatch.setattr(tasks_api, "_create_task_record", lambda *args: created.setdefault("meta", args[3]))
    monkeypatch.setattr(tasks_api, "_save_task_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(tasks_api, "_registration_paypal_payment_settings", mock.Mock())
    background = BackgroundTasks()

    with mock.patch.object(tasks_api._task_store, "create"), mock.patch.object(
        tasks_api._task_store, "update_meta"
    ), mock.patch.object(tasks_api._task_store, "snapshot", return_value={"meta": {}}):
        tasks_api.enqueue_register_task(request, background_tasks=background)

    tasks_api._registration_paypal_payment_settings.assert_not_called()
    assert created["meta"]["registration_paypal_payment_request"] == {
        "enabled": False,
        "link_profile": {},
        "payment_profile": {},
    }


def test_registration_paypal_enabled_freezes_safe_profiles_in_task_meta(monkeypatch):
    request = RegisterTaskRequest(
        platform="chatgpt",
        registration_paypal_payment_enabled=True,
    )
    frozen = _settings()
    created = {}
    monkeypatch.setattr(tasks_api, "_prepare_register_request", lambda req: req)
    monkeypatch.setattr(tasks_api, "_build_effective_register_extra", lambda req: {})
    monkeypatch.setattr(
        tasks_api,
        "_registration_paypal_payment_settings",
        lambda: frozen,
    )
    monkeypatch.setattr(tasks_api, "_create_task_record", lambda *args: created.setdefault("meta", args[3]))
    monkeypatch.setattr(tasks_api, "_save_task_log", lambda *args, **kwargs: None)
    background = BackgroundTasks()

    with mock.patch.object(tasks_api._task_store, "create"), mock.patch.object(
        tasks_api._task_store, "update_meta"
    ), mock.patch.object(tasks_api._task_store, "snapshot", return_value={"meta": {}}):
        tasks_api.enqueue_register_task(request, background_tasks=background)

    assert request.registration_paypal_payment_enabled is True
    assert request._registration_paypal_payment_runtime["profile_hash"] == PROFILE_HASH
    assert created["meta"]["registration_paypal_payment_request"]["enabled"] is True
    assert created["meta"]["registration_paypal_payment_request"]["link_profile"] == frozen[
        "link_profile"
    ]
    assert "paypal_url" not in repr(created["meta"])


def _make_account_engine(*, access_token: str = "at-test"):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    account = AccountModel(
        platform="chatgpt",
        email="paypal-registration@example.com",
        password="pw",
        token=access_token,
    )
    account.set_extra({"access_token": access_token} if access_token else {})
    with Session(engine) as session:
        session.add(account)
        session.commit()
        session.refresh(account)
        account_id = int(account.id)
    return engine, account_id


def _run_one(engine, account_id, action_result, enqueue_result=None, enqueue_error=None):
    fake_platform = mock.Mock()
    fake_platform.execute_action.return_value = action_result
    fake_client = mock.Mock()
    if enqueue_error is not None:
        fake_client.enqueue.side_effect = enqueue_error
    else:
        fake_client.enqueue.return_value = enqueue_result or {
            "batch_id": "batch123456",
            "item_id": "item123456",
            "created": True,
            "idempotent": False,
            "remote_status": "pending",
            "batch_status": "draft",
        }
    patches = [
        mock.patch.object(core_db, "engine", engine),
        mock.patch("services.chatgpt_core.ChatGPTPlatform", return_value=fake_platform),
        mock.patch("core.config_store.config_store.get_all", return_value={}),
        mock.patch("api.actions._apply_action_result"),
        mock.patch.object(
            payment_module.PaypalAgreementAutoClient,
            "from_env",
            classmethod(lambda cls: fake_client),
        ),
        mock.patch.object(
            __import__("services.account_filters", fromlist=["upsert_account_list_state_for_account_ids"]),
            "upsert_account_list_state_for_account_ids",
            return_value=None,
        ),
    ]
    with ExitStack() as stack:
        for patcher in patches:
            stack.enter_context(patcher)
        result = payment_module.run_registration_paypal_payment_for_account(
            account_id,
            _settings(),
            task_id="task-paypal-1",
        )
    return result, fake_platform, fake_client


def test_registration_paypal_extract_failure_does_not_fail_registration():
    engine, account_id = _make_account_engine()
    result, platform, client = _run_one(
        engine,
        account_id,
        {"ok": False, "error": "temporary link service failure"},
    )
    assert result["state"] == "extract_failed"
    assert platform.execute_action.call_count == 1
    client.enqueue.assert_not_called()
    with Session(engine) as session:
        marker = session.get(AccountModel, account_id).get_extra()[payment_module.ACCOUNT_MARKER_KEY]
    assert marker["status"] == "extract_failed"


def test_registration_paypal_enqueue_failure_keeps_saved_result_and_marker():
    engine, account_id = _make_account_engine()
    result, _platform, client = _run_one(
        engine,
        account_id,
        {"ok": True, "data": {"url": PAYPAL_URL, "link_type": "paypal"}},
        enqueue_error=RuntimeError("payment queue offline"),
    )
    assert result["state"] == "submit_failed"
    client.enqueue.assert_called_once_with(PAYPAL_URL)
    with Session(engine) as session:
        marker = session.get(AccountModel, account_id).get_extra()[payment_module.ACCOUNT_MARKER_KEY]
    assert marker["status"] == "submit_failed"


def test_registration_paypal_success_is_locally_idempotent():
    engine, account_id = _make_account_engine()
    action_result = {"ok": True, "data": {"url": PAYPAL_URL, "link_type": "paypal"}}
    first, platform, client = _run_one(engine, account_id, action_result)
    second, _platform_again, _client_again = _run_one(engine, account_id, action_result)
    assert first["state"] == "submitted"
    assert second["state"] == "submitted"
    assert second["idempotent"] is True
    assert platform.execute_action.call_count == 1
    assert client.enqueue.call_count == 1


def test_registration_paypal_coordinator_waits_and_counts_multiple_accounts():
    lock = threading.Lock()
    release = threading.Event()
    started = threading.Event()
    active = 0
    peak = 0

    def run_account(account_id, _settings, *, task_id=""):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if active == 2:
                started.set()
        assert release.wait(timeout=3)
        with lock:
            active -= 1
        return {
            "account_id": account_id,
            "email": f"{account_id}@example.com",
            "state": "submitted",
            "reason_code": "payment_enqueued",
            "message": "queued",
            "batch_id": f"batch{account_id}",
            "item_id": f"item{account_id}",
            "remote_status": "pending",
            "completed_at": "now",
        }

    snapshots = []
    coordinator = payment_module.RegistrationPaypalPaymentCoordinator(
        task_id="task-coordinator",
        settings=_settings(),
        run_account=run_account,
        update_meta=snapshots.append,
        log=lambda *_args: None,
        concurrency=2,
    )
    for account_id in (1, 2, 3):
        assert coordinator.submit(account_id, f"{account_id}@example.com")
    assert started.wait(timeout=3)
    with lock:
        assert peak == 2
    release.set()
    summary = coordinator.finish()
    assert summary["finished"] is True
    assert summary["counts"]["submitted"] == 3
    assert summary["counts"]["completed"] == 3
    assert sorted(item["account_id"] for item in summary["submitted_results"]) == [1, 2, 3]
    assert summary["submitted_results_total"] == 3
    assert summary["submitted_results_truncated"] is False
    assert snapshots[-1]["finished"] is True


def test_registration_paypal_success_snapshot_is_separate_and_bounded(monkeypatch):
    monkeypatch.setattr(payment_module, "RESULT_RETAIN_LIMIT", 1)

    def run_account(account_id, _settings, *, task_id=""):
        state = "extract_failed" if account_id == 3 else "submitted"
        return {
            "account_id": account_id,
            "email": f"{account_id}@example.com",
            "state": state,
            "reason_code": "payment_enqueued" if state == "submitted" else "link_failed",
            "message": state,
            "batch_id": f"batch{account_id}" if state == "submitted" else "",
            "item_id": f"item{account_id}" if state == "submitted" else "",
            "remote_status": "pending" if state == "submitted" else "",
            "completed_at": "now",
        }

    coordinator = payment_module.RegistrationPaypalPaymentCoordinator(
        task_id="task-bounded-success-results",
        settings=_settings(),
        run_account=run_account,
        update_meta=lambda _snapshot: None,
        log=lambda *_args: None,
        concurrency=2,
    )
    for account_id in (1, 2, 3):
        assert coordinator.submit(account_id, f"{account_id}@example.com")

    summary = coordinator.finish()

    assert summary["counts"]["submitted"] == 2
    assert summary["counts"]["extract_failed"] == 1
    assert len(summary["submitted_results"]) == 1
    assert summary["submitted_results"][0]["state"] == "submitted"
    assert summary["submitted_results_total"] == 2
    assert summary["submitted_results_truncated"] is True
