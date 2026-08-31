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
from services.chatgpt_core.registration_pipeline import (
    PIPELINE_MARKER_KEY,
    claim_registration_pipeline_continuation,
    initialize_registration_pipeline,
    update_registration_pipeline_stage,
)
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
            _Response(
                200,
                {
                    "ok": True,
                    "batch_id": "batch123456",
                    "item_id": "item123456",
                    "batch_status": "completed",
                    "status": "completed",
                    "stage": "已完成",
                    "payment_result": "PayPal 已授权，商户回跳成功",
                    "paypal_authorized": True,
                    "settlement_status": "merchant_redirect_succeeded",
                    "merchant_redirect_succeeded": True,
                    "entitlement_verified": False,
                    "error_code": "",
                    "error": "",
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
    item_result = client.get_item_result(result["batch_id"], result["item_id"])

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
    assert item_result["status"] == "completed"
    assert item_result["paypal_authorized"] is True
    assert item_result["merchant_redirect_succeeded"] is True
    assert session.calls[2][0] == "GET"
    assert session.calls[2][1].endswith(
        "/api/internal/auto-payments/batch123456/items/item123456"
    )


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

    payment_client.reset_mock()
    link_only = tasks_api._registration_paypal_payment_settings(include_payment=False)
    assert link_only["submit_payment"] is False
    assert link_only["payment_profile"] == {}
    payment_client.get_profile.assert_not_called()


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


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        (
            {
                "registration_zero_amount_eligibility_enabled": False,
                "registration_paypal_link_enabled": True,
            },
            "0 元检测",
        ),
        (
            {
                "registration_zero_amount_eligibility_enabled": True,
                "registration_paypal_link_enabled": False,
                "registration_paypal_payment_enabled": True,
            },
            "注册后提链",
        ),
    ],
)
def test_registration_pipeline_rejects_missing_prerequisite(monkeypatch, kwargs, message):
    request = RegisterTaskRequest(platform="chatgpt", **kwargs)
    monkeypatch.setattr(tasks_api, "_prepare_register_request", lambda req: req)
    with pytest.raises(HTTPException) as raised:
        tasks_api.enqueue_register_task(request, background_tasks=BackgroundTasks())
    assert raised.value.status_code == 400
    assert message in str(raised.value.detail)


@pytest.mark.parametrize(
    ("state", "submit_count", "blocked_state"),
    [
        ("eligible", 1, ""),
        ("ineligible", 0, "blocked"),
        ("probe_failed", 0, "blocked"),
        ("pending_auth", 0, "pending_auth"),
    ],
)
def test_registration_pipeline_only_submits_link_after_explicit_eligibility(
    state,
    submit_count,
    blocked_state,
):
    submit_link = mock.Mock(return_value=True)
    with (
        mock.patch(
            "services.chatgpt_core.registration_pipeline.update_registration_pipeline_stage"
        ) as update_stage,
        mock.patch(
            "services.chatgpt_core.registration_pipeline.block_registration_pipeline_downstream"
        ) as block_downstream,
    ):
        tasks_api._apply_registration_eligibility_pipeline_result(
            {
                "account_id": 77,
                "email": "gated@example.com",
                "state": state,
                "reason_code": f"reason_{state}",
                "message": state,
                "checked_at": "now",
            },
            task_id="task-gated",
            payment_link_enabled=True,
            payment_enabled=True,
            legacy_combined=False,
            submit_payment_link=submit_link,
        )

    assert submit_link.call_count == submit_count
    update_stage.assert_called_once()
    if blocked_state:
        block_downstream.assert_called_once()
        assert block_downstream.call_args.kwargs["zero_state"] == state
    else:
        block_downstream.assert_not_called()


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


def _make_auth_pending_pipeline_engine():
    engine, account_id = _make_account_engine(access_token="")
    with mock.patch.object(core_db, "engine", engine):
        with Session(engine) as session:
            account = session.get(AccountModel, account_id)
            created_at = account.created_at.replace(tzinfo=None).isoformat(sep=" ")
            email = str(account.email or "")
        assert initialize_registration_pipeline(
            account_id,
            email=email,
            created_at=created_at,
            task_id="task-original-registration",
            zero_amount_enabled=True,
            payment_link_enabled=True,
            payment_enabled=True,
            auth_pending=True,
            zero_amount_checkout_country="VN",
            payment_link_profile_hash=PROFILE_HASH,
            payment_link_type="paypal",
        )
        with Session(engine) as session:
            account = session.get(AccountModel, account_id)
            extra = account.get_extra()
            extra["access_token"] = "at-recovered"
            account.token = "at-recovered"
            account.set_extra(extra)
            session.add(account)
            session.commit()
    return engine, account_id


def test_registration_pipeline_resumes_payment_details_after_auth_recovery():
    engine, account_id = _make_account_engine(access_token="")
    with mock.patch.object(core_db, "engine", engine):
        with Session(engine) as session:
            account = session.get(AccountModel, account_id)
            email = str(account.email or "")
            created_at = account.created_at.replace(tzinfo=None).isoformat(sep=" ")
        assert initialize_registration_pipeline(
            account_id,
            email=email,
            created_at=created_at,
            task_id="task-payment-details-auth",
            zero_amount_enabled=False,
            payment_details_enabled=True,
            payment_link_enabled=False,
            payment_enabled=False,
            auth_pending=True,
        )
        with Session(engine) as session:
            account = session.get(AccountModel, account_id)
            extra = account.get_extra()
            extra["access_token"] = "at-recovered"
            account.token = "at-recovered"
            account.set_extra(extra)
            session.add(account)
            session.commit()

        context = claim_registration_pipeline_continuation(account_id)

    assert context["claimed"] is True
    assert context["requested"] == {
        "zero_amount": False,
        "payment_details": True,
        "payment_link": False,
        "payment": False,
    }
    assert context["payment_details_state"] == "pending_auth"


def test_registration_payment_details_result_updates_its_pipeline_stage():
    engine, account_id = _make_account_engine(access_token="at-details")
    with mock.patch.object(core_db, "engine", engine):
        with Session(engine) as session:
            account = session.get(AccountModel, account_id)
            email = str(account.email or "")
            created_at = account.created_at.replace(tzinfo=None).isoformat(sep=" ")
        assert initialize_registration_pipeline(
            account_id,
            email=email,
            created_at=created_at,
            task_id="task-payment-details-result",
            zero_amount_enabled=False,
            payment_details_enabled=True,
            payment_link_enabled=False,
            payment_enabled=False,
        )
        state = tasks_api._apply_registration_payment_details_pipeline_result(
            {
                "account_id": account_id,
                "email": email,
                "results": [
                    {
                        "kind": "checkout_link_type",
                        "state": "cs",
                        "checked_at": "2026-09-01T00:00:00+00:00",
                    },
                    {
                        "kind": "payment_methods",
                        "state": "available",
                        "checked_at": "2026-09-01T00:00:01+00:00",
                    },
                ],
            },
            task_id="task-payment-details-result",
        )
        with Session(engine) as session:
            marker = session.get(AccountModel, account_id).get_extra()[PIPELINE_MARKER_KEY]

    assert state == "completed"
    assert marker["payment_details"]["state"] == "completed"
    assert marker["payment_details"]["checkout_link_type"] == "cs"
    assert marker["payment_details"]["payment_methods_state"] == "available"


@pytest.mark.parametrize(
    ("zero_state", "paypal_calls", "expected_link_state"),
    [
        ("eligible", 1, "succeeded"),
        ("ineligible", 0, "blocked"),
        ("probe_failed", 0, "blocked"),
        ("pending_auth", 0, "pending_auth"),
    ],
)
def test_auth_recovery_continues_original_pipeline_with_strict_gate(
    zero_state,
    paypal_calls,
    expected_link_state,
):
    engine, account_id = _make_auth_pending_pipeline_engine()
    paypal_runner = mock.Mock()

    def run_paypal(current_account_id, settings, *, task_id=""):
        assert task_id == "task-original-registration"
        assert settings["submit_payment"] is True
        update_registration_pipeline_stage(
            current_account_id,
            "payment_link",
            "succeeded",
            task_id=task_id,
            reason_code="paypal_url_persisted",
        )
        update_registration_pipeline_stage(
            current_account_id,
            "payment",
            "submitted",
            task_id=task_id,
            reason_code="payment_enqueued",
        )
        return {
            "account_id": current_account_id,
            "state": "submitted",
            "reason_code": "payment_enqueued",
            "message": "queued",
        }

    paypal_runner.side_effect = run_paypal
    zero_runner = mock.Mock(
        return_value={
            "account_id": account_id,
            "email": "paypal-registration@example.com",
            "state": zero_state,
            "reason_code": f"reason_{zero_state}",
            "message": zero_state,
            "checked_at": "2026-08-18T00:00:00+00:00",
            "evidence": {
                "amount_display": "0.00 VND" if zero_state == "eligible" else "99.00 VND",
                "currency": "VND",
            },
        }
    )
    with (
        mock.patch.object(core_db, "engine", engine),
        mock.patch.object(tasks_api, "engine", engine),
        mock.patch.object(
            tasks_api,
            "_safe_registration_zero_amount_eligibility_settings",
            return_value={"checkout_country_code": "VN", "proxy_mode": "direct"},
        ),
        mock.patch.object(
            tasks_api,
            "_run_payment_eligibility_for_account",
            zero_runner,
        ),
        mock.patch.object(
            tasks_api,
            "_registration_paypal_payment_settings",
            return_value={**_settings(), "submit_payment": False},
        ),
        mock.patch.object(
            payment_module,
            "run_registration_paypal_payment_for_account",
            paypal_runner,
        ),
    ):
        result = tasks_api._resume_registration_pipeline_after_auth(account_id)

    assert result["resumed"] is True
    assert zero_runner.call_count == 1
    assert paypal_runner.call_count == paypal_calls
    with Session(engine) as session:
        pipeline = session.get(AccountModel, account_id).get_extra()[PIPELINE_MARKER_KEY]
    assert pipeline["registration"]["state"] == "succeeded"
    assert pipeline["zero_amount"]["state"] == zero_state
    assert pipeline["payment_link"]["state"] == expected_link_state
    assert pipeline["continuation"]["state"] in {"completed", "failed"}


def test_auth_recovery_fails_closed_when_frozen_link_profile_changed():
    engine, account_id = _make_auth_pending_pipeline_engine()
    paypal_runner = mock.Mock()
    with (
        mock.patch.object(core_db, "engine", engine),
        mock.patch.object(tasks_api, "engine", engine),
        mock.patch.object(
            tasks_api,
            "_safe_registration_zero_amount_eligibility_settings",
            return_value={"checkout_country_code": "VN", "proxy_mode": "direct"},
        ),
        mock.patch.object(
            tasks_api,
            "_run_payment_eligibility_for_account",
            return_value={
                "account_id": account_id,
                "email": "paypal-registration@example.com",
                "state": "eligible",
                "reason_code": "zero_checkout_amount",
                "message": "eligible",
                "checked_at": "now",
                "evidence": {},
            },
        ),
        mock.patch.object(
            tasks_api,
            "_registration_paypal_payment_settings",
            return_value={**_settings(), "profile_hash": "changed-profile"},
        ),
        mock.patch.object(
            payment_module,
            "run_registration_paypal_payment_for_account",
            paypal_runner,
        ),
    ):
        result = tasks_api._resume_registration_pipeline_after_auth(account_id)

    assert result["state"] == "extract_failed"
    paypal_runner.assert_not_called()
    with Session(engine) as session:
        pipeline = session.get(AccountModel, account_id).get_extra()[PIPELINE_MARKER_KEY]
    assert pipeline["payment_link"]["state"] == "failed"
    assert pipeline["payment_link"]["reason_code"] == "frozen_link_profile_changed"
    assert pipeline["payment"]["state"] == "blocked"


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
        extra = session.get(AccountModel, account_id).get_extra()
        marker = extra[payment_module.ACCOUNT_MARKER_KEY]
        pipeline = extra[PIPELINE_MARKER_KEY]
    assert marker["status"] == "submit_failed"
    assert pipeline["payment_link"]["state"] == "succeeded"
    assert pipeline["payment"]["state"] == "submit_failed"


def test_registration_paypal_link_only_never_enqueues_payment():
    engine, account_id = _make_account_engine()
    fake_platform = mock.Mock()
    fake_platform.execute_action.return_value = {
        "ok": True,
        "data": {"url": PAYPAL_URL, "link_type": "paypal"},
    }
    fake_client = mock.Mock()
    settings = {**_settings(), "submit_payment": False}
    with (
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
    ):
        result = payment_module.run_registration_paypal_payment_for_account(
            account_id,
            settings,
            task_id="task-link-only",
        )

    assert result["state"] == "link_succeeded"
    fake_client.enqueue.assert_not_called()
    with Session(engine) as session:
        extra = session.get(AccountModel, account_id).get_extra()
    assert extra[payment_module.ACCOUNT_MARKER_KEY]["status"] == "link_succeeded"
    assert extra[PIPELINE_MARKER_KEY]["payment_link"]["state"] == "succeeded"
    assert extra[PIPELINE_MARKER_KEY]["payment"]["state"] == "disabled"


def test_registration_paypal_success_is_locally_idempotent():
    engine, account_id = _make_account_engine()
    action_result = {"ok": True, "data": {"url": PAYPAL_URL, "link_type": "paypal"}}
    first, platform, client = _run_one(engine, account_id, action_result)
    with Session(engine) as session:
        account = session.get(AccountModel, account_id)
        extra = account.get_extra()
        extra[PIPELINE_MARKER_KEY]["payment_link"] = {"state": "running"}
        extra[PIPELINE_MARKER_KEY]["payment"] = {"state": "blocked"}
        account.set_extra(extra)
        session.add(account)
        session.commit()
    second, _platform_again, _client_again = _run_one(engine, account_id, action_result)
    assert first["state"] == "submitted"
    assert second["state"] == "submitted"
    assert second["idempotent"] is True
    assert platform.execute_action.call_count == 1
    assert client.enqueue.call_count == 1
    with Session(engine) as session:
        pipeline = session.get(AccountModel, account_id).get_extra()[PIPELINE_MARKER_KEY]
    assert pipeline["payment_link"]["state"] == "succeeded"
    assert pipeline["payment"]["state"] == "submitted"


def test_link_only_coordinator_unhandled_exception_is_extract_failure():
    update_stage = mock.Mock()
    with mock.patch(
        "services.chatgpt_core.registration_pipeline.update_registration_pipeline_stage",
        update_stage,
    ):
        coordinator = payment_module.RegistrationPaypalPaymentCoordinator(
            task_id="task-link-only-exception",
            settings={**_settings(), "submit_payment": False},
            run_account=mock.Mock(side_effect=RuntimeError("unexpected link crash")),
            update_meta=lambda _snapshot: None,
            log=lambda *_args: None,
            concurrency=1,
        )
        assert coordinator.submit(88, "link-only@example.com")
        summary = coordinator.finish()

    assert summary["counts"]["extract_failed"] == 1
    assert summary["counts"]["submit_failed"] == 0
    assert any(
        call.args[1:3] == ("payment_link", "failed")
        for call in update_stage.call_args_list
    )
    assert any(
        call.args[1:3] == ("payment", "disabled")
        for call in update_stage.call_args_list
    )


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


def test_registration_paypal_coordinator_emits_each_compact_result_without_failing_task():
    on_result = mock.Mock(side_effect=RuntimeError("observer unavailable"))
    coordinator = payment_module.RegistrationPaypalPaymentCoordinator(
        task_id="task-result-callback",
        settings=_settings(),
        run_account=lambda account_id, _settings, **_kwargs: {
            "account_id": account_id,
            "email": "callback@example.com",
            "state": "link_succeeded",
            "reason_code": "paypal_url_persisted",
            "message": "link ready",
            "completed_at": "now",
        },
        update_meta=lambda _snapshot: None,
        log=lambda *_args: None,
        on_result=on_result,
        concurrency=1,
    )

    assert coordinator.submit(91, "callback@example.com")
    summary = coordinator.finish()

    assert summary["counts"]["link_succeeded"] == 1
    on_result.assert_called_once()
    assert on_result.call_args.args[0]["account_id"] == 91
    assert on_result.call_args.args[0]["state"] == "link_succeeded"


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
