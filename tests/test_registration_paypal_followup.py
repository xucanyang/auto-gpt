from __future__ import annotations

import time
from unittest import mock

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from core import db as core_db
from core.db import (
    AccountModel,
    RegistrationPaypalPaymentEventModel,
    RegistrationPaypalPaymentFollowupModel,
)
from services.chatgpt_core import registration_paypal_followup as followup


def _engine_with_account(*, status: str = "registered"):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    account = AccountModel(
        platform="chatgpt",
        email="followup@example.com",
        password="pw",
        status=status,
    )
    account.set_extra(
        {
            "chatgpt_mailbox_state": {
                "provider": "fixture",
                "email": "followup@example.com",
            },
            "chatgpt_paypal_auto_payment": {
                "status": "submitted",
                "batch_id": "batch123456",
                "item_id": "item123456",
            },
        }
    )
    with Session(engine) as session:
        session.add(account)
        session.commit()
        session.refresh(account)
        identity = {
            "account_id": int(account.id or 0),
            "account_email": account.email,
            "account_created_at": followup._account_created_at_text(account.created_at),
        }
    return engine, identity


def _create_followup(identity):
    return followup.ensure_payment_followup(
        task_id="task-followup",
        **identity,
        batch_id="batch123456",
        item_id="item123456",
        remote_status="pending",
    )


def test_followup_identity_and_event_are_idempotent(monkeypatch):
    engine, identity = _engine_with_account()
    monkeypatch.setattr(core_db, "engine", engine)

    first = _create_followup(identity)
    second = _create_followup(identity)

    assert first is not None and second is not None
    assert first.id == second.id
    with Session(engine) as session:
        rows = session.exec(select(RegistrationPaypalPaymentFollowupModel)).all()
        events = session.exec(select(RegistrationPaypalPaymentEventModel)).all()
    assert len(rows) == 1
    assert [event.stage for event in events] == ["queued"]


def test_authorized_payment_runs_one_web_login_and_waits_for_local_refresh(monkeypatch):
    engine, identity = _engine_with_account(status="invalid")
    monkeypatch.setattr(core_db, "engine", engine)
    row = _create_followup(identity)
    assert row is not None

    client = mock.Mock()
    client.get_item_result.return_value = {
        "status": "completed",
        "stage": "协议授权及商户回跳完成，权益未核验",
        "payment_result": "PayPal 已授权，商户回跳成功",
        "job_id": "job-authorized",
        "paypal_authorized": True,
        "settlement_status": "merchant_redirect_succeeded",
        "merchant_redirect_succeeded": True,
        "entitlement_verified": False,
        "error_code": "",
        "error": "",
    }
    login = mock.Mock(
        return_value={
            "ok": True,
            "data": {"local_status_refresh_scheduled": True},
        }
    )
    monkeypatch.setattr(
        followup.PaypalAgreementAutoClient,
        "from_env",
        classmethod(lambda cls: client),
    )
    monkeypatch.setattr(followup, "_followup_login_candidates", lambda: [("", None, "direct")])
    monkeypatch.setattr(
        "services.chatgpt_core.web_session_login.execute_chatgpt_web_session_login",
        login,
    )

    followup._process_row(row)

    with Session(engine) as session:
        current = session.get(RegistrationPaypalPaymentFollowupModel, int(row.id or 0))
        account = session.get(AccountModel, identity["account_id"])
        stages = [
            event.stage
            for event in session.exec(
                select(RegistrationPaypalPaymentEventModel).order_by(
                    RegistrationPaypalPaymentEventModel.id.asc()
                )
            ).all()
        ]
    assert current is not None
    assert current.state == followup.LOCAL_REFRESH_PENDING
    assert current.remote_stage == "协议授权及商户回跳完成，权益未核验"
    assert current.payment_result == "PayPal 已授权，商户回跳成功"
    assert current.remote_job_id == "job-authorized"
    assert current.paypal_authorized is True
    assert current.relogin_attempt_count == 1
    assert account is not None and account.status == "pending_payment"
    assert account.get_extra()["chatgpt_paypal_auto_payment"]["status"] == followup.LOCAL_REFRESH_PENDING
    assert login.call_count == 1
    assert "payment_authorized" in stages
    assert "relogin_succeeded" in stages


def test_failed_payment_never_runs_local_login(monkeypatch):
    engine, identity = _engine_with_account()
    monkeypatch.setattr(core_db, "engine", engine)
    row = _create_followup(identity)
    assert row is not None

    client = mock.Mock()
    client.get_item_result.return_value = {
        "status": "failed",
        "stage": "执行失败",
        "payment_result": "PayPal signup failed",
        "job_id": "job-failed",
        "paypal_authorized": False,
        "settlement_status": "merchant_redirect_failed",
        "error_code": "OAS_ERROR",
        "error": "payment rejected",
    }
    login = mock.Mock()
    monkeypatch.setattr(
        followup.PaypalAgreementAutoClient,
        "from_env",
        classmethod(lambda cls: client),
    )
    monkeypatch.setattr(
        "services.chatgpt_core.web_session_login.execute_chatgpt_web_session_login",
        login,
    )

    followup._process_row(row)

    with Session(engine) as session:
        current = session.get(RegistrationPaypalPaymentFollowupModel, int(row.id or 0))
    assert current is not None and current.state == "payment_failed"
    assert current.remote_stage == "执行失败"
    assert current.payment_result == "PayPal signup failed"
    assert current.remote_job_id == "job-failed"
    login.assert_not_called()


def test_marker_backfill_limits_matching_markers_not_accounts(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    ordinary = AccountModel(
        platform="chatgpt",
        email="ordinary@example.com",
        password="pw",
        status="registered",
    )
    submitted = AccountModel(
        platform="chatgpt",
        email="submitted@example.com",
        password="pw",
        status="registered",
    )
    submitted.set_extra(
        {
            "chatgpt_paypal_auto_payment": {
                "status": "submitted",
                "task_id": "task-backfill",
                "batch_id": "batch-after-limit",
                "item_id": "item-after-limit",
                "remote_status": "pending",
            }
        }
    )
    with Session(engine) as session:
        session.add(ordinary)
        session.add(submitted)
        session.commit()
        session.refresh(submitted)
        submitted_id = int(submitted.id or 0)

    monkeypatch.setattr(core_db, "engine", engine)

    assert followup.backfill_followups_from_markers(limit=1) == 1

    with Session(engine) as session:
        rows = session.exec(select(RegistrationPaypalPaymentFollowupModel)).all()
    assert len(rows) == 1
    assert rows[0].account_id == submitted_id
    assert rows[0].batch_id == "batch-after-limit"
    assert rows[0].item_id == "item-after-limit"


def test_payment_deadline_does_not_skip_already_authorized_relogin(monkeypatch):
    engine, identity = _engine_with_account()
    monkeypatch.setattr(core_db, "engine", engine)
    row = _create_followup(identity)
    assert row is not None
    with Session(engine) as session:
        current = session.get(RegistrationPaypalPaymentFollowupModel, int(row.id or 0))
        assert current is not None
        current.state = followup.RELOGIN_PENDING
        current.paypal_authorized = True
        current.deadline_at = time.time() - 60
        session.add(current)
        session.commit()
        session.refresh(current)
        authorized_row = current

    process_relogin = mock.Mock()
    monkeypatch.setattr(followup, "_process_relogin", process_relogin)

    followup._process_row(authorized_row)

    process_relogin.assert_called_once_with(authorized_row)


def test_relogin_failure_updates_legacy_marker(monkeypatch):
    engine, identity = _engine_with_account(status="invalid")
    monkeypatch.setattr(core_db, "engine", engine)
    row = _create_followup(identity)
    assert row is not None
    with Session(engine) as session:
        current = session.get(RegistrationPaypalPaymentFollowupModel, int(row.id or 0))
        assert current is not None
        current.state = followup.RELOGIN_PENDING
        current.paypal_authorized = True
        session.add(current)
        session.commit()
        session.refresh(current)
        relogin_row = current

    monkeypatch.setattr(followup, "_followup_login_candidates", lambda: [("", None, "direct")])
    monkeypatch.setattr(
        "services.chatgpt_core.web_session_login.execute_chatgpt_web_session_login",
        mock.Mock(
            return_value={
                "ok": False,
                "error": "account deactivated",
                "data": {"error_code": "account_deactivated"},
            }
        ),
    )

    followup._process_relogin(relogin_row)

    with Session(engine) as session:
        current = session.get(RegistrationPaypalPaymentFollowupModel, int(row.id or 0))
        account = session.get(AccountModel, identity["account_id"])
    assert current is not None and current.state == "relogin_failed"
    assert account is not None
    marker = account.get_extra()["chatgpt_paypal_auto_payment"]
    assert marker["status"] == "relogin_failed"
    assert marker["last_error"] == "account deactivated"


def test_backfill_repairs_terminal_row_with_stale_active_marker(monkeypatch):
    engine, identity = _engine_with_account(status="invalid")
    monkeypatch.setattr(core_db, "engine", engine)
    row = _create_followup(identity)
    assert row is not None
    with Session(engine) as session:
        current = session.get(RegistrationPaypalPaymentFollowupModel, int(row.id or 0))
        account = session.get(AccountModel, identity["account_id"])
        assert current is not None and account is not None
        current.state = "relogin_failed"
        current.last_error = "account deactivated"
        extra = account.get_extra()
        extra["chatgpt_paypal_auto_payment"]["status"] = followup.RELOGIN_PENDING
        account.set_extra(extra)
        session.add(current)
        session.add(account)
        session.commit()

    assert followup.backfill_followups_from_markers() == 1

    with Session(engine) as session:
        account = session.get(AccountModel, identity["account_id"])
    assert account is not None
    marker = account.get_extra()["chatgpt_paypal_auto_payment"]
    assert marker["status"] == "relogin_failed"
    assert marker["last_error"] == "account deactivated"
