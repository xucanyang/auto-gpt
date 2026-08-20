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


def test_task_summary_maps_payment_result_and_post_payment_states(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    states = [
        followup.PAYMENT_PENDING,
        followup.RELOGIN_PENDING,
        followup.LOCAL_REFRESH_PENDING,
        "paypal_authorized",
        "subscription_confirmed",
        "relogin_failed",
        "local_unconfirmed",
        "payment_failed",
        "payment_unknown",
        "account_identity_changed",
        "future_unmapped_state",
    ]
    with Session(engine) as session:
        for index, state in enumerate(states, start=1):
            session.add(
                RegistrationPaypalPaymentFollowupModel(
                    task_id="task-summary",
                    account_id=index,
                    account_email=f"summary-{index}@example.com",
                    account_created_at=f"created-{index}",
                    batch_id=f"batch-{index}",
                    item_id=f"item-{index}",
                    state=state,
                )
            )
        session.add(
            RegistrationPaypalPaymentFollowupModel(
                task_id="other-task",
                account_id=99,
                account_email="other@example.com",
                account_created_at="other-created",
                batch_id="other-batch",
                item_id="other-item",
                state="payment_failed",
            )
        )
        session.commit()

    monkeypatch.setattr(core_db, "engine", engine)

    summary = followup.registration_paypal_followup_summary("task-summary")

    assert summary == {
        "available": True,
        "total": 11,
        "active": 3,
        "processing": 1,
        "succeeded": 6,
        "failed": 1,
        "unknown": 3,
        "finished": False,
        "counts_by_state": {
            "account_identity_changed": 1,
            "future_unmapped_state": 1,
            "local_refresh_pending": 1,
            "local_unconfirmed": 1,
            "payment_failed": 1,
            "payment_pending": 1,
            "payment_unknown": 1,
            "paypal_authorized": 1,
            "relogin_failed": 1,
            "relogin_pending": 1,
            "subscription_confirmed": 1,
        },
    }


def test_relogin_failure_is_finished_payment_success(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            RegistrationPaypalPaymentFollowupModel(
                task_id="task-relogin-failed",
                account_id=1,
                account_email="relogin-failed@example.com",
                account_created_at="created",
                batch_id="batch",
                item_id="item",
                state="relogin_failed",
                paypal_authorized=True,
            )
        )
        session.commit()

    monkeypatch.setattr(core_db, "engine", engine)

    summary = followup.registration_paypal_followup_summary(
        "task-relogin-failed"
    )

    assert summary["active"] == 0
    assert summary["succeeded"] == 1
    assert summary["failed"] == 0
    assert summary["unknown"] == 0
    assert summary["finished"] is True


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


def test_authorized_payment_is_persisted_before_separate_web_login(monkeypatch):
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
    assert current is not None
    assert current.state == followup.RELOGIN_PENDING
    assert current.remote_stage == "协议授权及商户回跳完成，权益未核验"
    assert current.payment_result == "PayPal 已授权，商户回跳成功"
    assert current.remote_job_id == "job-authorized"
    assert current.paypal_authorized is True
    assert current.relogin_attempt_count == 0
    assert account is not None and account.status == "invalid"
    assert account.get_extra()["chatgpt_paypal_auto_payment"]["status"] == followup.RELOGIN_PENDING
    assert account.get_extra()["chatgpt_registration_pipeline"]["payment"]["state"] == "succeeded"
    assert account.get_extra()["chatgpt_registration_pipeline"]["payment"]["followup_state"] == followup.RELOGIN_PENDING
    assert followup.registration_paypal_followup_summary("task-followup")["succeeded"] == 1
    login.assert_not_called()

    followup._process_row(current)

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
    assert current.paypal_authorized is True
    assert current.relogin_attempt_count == 1
    assert account is not None and account.status == "pending_payment"
    assert account.get_extra()["chatgpt_paypal_auto_payment"]["status"] == followup.LOCAL_REFRESH_PENDING
    assert account.get_extra()["chatgpt_registration_pipeline"]["payment"]["state"] == "succeeded"
    assert account.get_extra()["chatgpt_registration_pipeline"]["payment"]["followup_state"] == followup.LOCAL_REFRESH_PENDING
    assert login.call_count == 1
    assert "payment_authorized" in stages
    assert "relogin_succeeded" in stages


def test_authorization_evidence_wins_over_remote_failed_status(monkeypatch):
    engine, identity = _engine_with_account()
    monkeypatch.setattr(core_db, "engine", engine)
    row = _create_followup(identity)
    assert row is not None

    client = mock.Mock()
    client.get_item_result.return_value = {
        "status": "failed",
        "stage": "PayPal 已授权，后处理失败",
        "payment_result": "PayPal 已授权，后处理失败",
        "job_id": "job-authorized-post-failure",
        "paypal_authorized": True,
        "settlement_status": "vault_failed",
        "merchant_redirect_succeeded": False,
        "entitlement_verified": False,
        "error_code": "MERCHANT_REDIRECT_FAILED",
        "error": "merchant redirect failed after authorization",
    }
    monkeypatch.setattr(
        followup.PaypalAgreementAutoClient,
        "from_env",
        classmethod(lambda cls: client),
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
    assert current is not None and current.state == followup.RELOGIN_PENDING
    assert current.remote_status == "failed"
    assert current.paypal_authorized is True
    assert current.payment_result_code == "MERCHANT_REDIRECT_FAILED"
    assert account is not None
    assert account.get_extra()["chatgpt_registration_pipeline"]["payment"]["state"] == "succeeded"
    assert "payment_authorized" in stages
    assert "payment_failed" not in stages


def test_reconcile_due_followups_filters_worker_lane_states(monkeypatch):
    engine, identity = _engine_with_account()
    monkeypatch.setattr(core_db, "engine", engine)
    payment_row = _create_followup(identity)
    assert payment_row is not None
    with Session(engine) as session:
        relogin_row = RegistrationPaypalPaymentFollowupModel(
            task_id="task-followup",
            account_id=identity["account_id"],
            account_email=identity["account_email"],
            account_created_at=identity["account_created_at"],
            batch_id="batch-relogin",
            item_id="item-relogin",
            state=followup.RELOGIN_PENDING,
            next_poll_at=0,
        )
        local_row = RegistrationPaypalPaymentFollowupModel(
            task_id="task-followup",
            account_id=identity["account_id"],
            account_email=identity["account_email"],
            account_created_at=identity["account_created_at"],
            batch_id="batch-local",
            item_id="item-local",
            state=followup.LOCAL_REFRESH_PENDING,
            next_poll_at=0,
        )
        session.add(relogin_row)
        session.add(local_row)
        session.commit()

    processed: list[str] = []
    monkeypatch.setattr(
        followup,
        "_process_row",
        lambda current: processed.append(current.state),
    )

    assert followup.reconcile_due_followups(states={followup.PAYMENT_PENDING}) == 1
    assert processed == [followup.PAYMENT_PENDING]

    processed.clear()
    assert followup.reconcile_due_followups(
        states={followup.RELOGIN_PENDING, followup.LOCAL_REFRESH_PENDING}
    ) == 2
    assert sorted(processed) == sorted(
        [followup.RELOGIN_PENDING, followup.LOCAL_REFRESH_PENDING]
    )


def test_worker_loops_keep_remote_polling_and_browser_login_in_separate_lanes(
    monkeypatch,
):
    class OneIterationEvent:
        def __init__(self) -> None:
            self.stopped = False

        def is_set(self) -> bool:
            return self.stopped

        def wait(self, _timeout: float) -> None:
            self.stopped = True

    backfill = mock.Mock()
    reconcile = mock.Mock(return_value=0)
    monkeypatch.setattr(followup, "backfill_followups_from_markers", backfill)
    monkeypatch.setattr(followup, "reconcile_due_followups", reconcile)

    monkeypatch.setattr(followup, "_STOP_EVENT", OneIterationEvent())
    followup._payment_poll_worker_loop()
    backfill.assert_called_once_with()
    reconcile.assert_called_once_with(
        limit=100,
        states={followup.PAYMENT_PENDING},
    )

    reconcile.reset_mock()
    monkeypatch.setattr(followup, "_STOP_EVENT", OneIterationEvent())
    followup._post_payment_worker_loop()
    assert reconcile.call_args_list == [
        mock.call(limit=100, states={followup.LOCAL_REFRESH_PENDING}),
        mock.call(limit=1, states={followup.RELOGIN_PENDING}),
    ]


def test_live_followup_log_includes_masked_account_identity(monkeypatch):
    engine, identity = _engine_with_account()
    monkeypatch.setattr(core_db, "engine", engine)
    emit = mock.Mock()
    monkeypatch.setattr(followup, "_emit_live_event", emit)

    assert followup.append_registration_paypal_event(
        task_id="task-live-log",
        **identity,
        stage="payment_authorized",
        message="支付结果已回读：PayPal 已授权/商户回跳成功",
    ) is True

    emitted = emit.call_args.args[1]
    assert emitted.startswith("[PayPal 跟进][账号=")
    assert "followup@example.com" not in emitted
    assert emitted.endswith("支付结果已回读：PayPal 已授权/商户回跳成功")


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
        account = session.get(AccountModel, identity["account_id"])
    assert current is not None and current.state == "payment_failed"
    assert current.remote_stage == "执行失败"
    assert current.payment_result == "PayPal signup failed"
    assert current.remote_job_id == "job-failed"
    assert account is not None
    assert account.get_extra()["chatgpt_registration_pipeline"]["payment"]["state"] == "failed"
    assert account.get_extra()["chatgpt_registration_pipeline"]["payment"]["payment_result_code"] == "OAS_ERROR"
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
