import os
import tempfile
from datetime import datetime, timezone, timedelta

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from sqlmodel import Session, SQLModel, select

from core.db import (
    AccountModel,
    DeliveryCardModel,
    DeliveryRedeemApiLogModel,
    DeliverySkuModel,
    engine,
    init_db,
)
from core.config_store import config_store
from services.delivery_cards import service


def setup_function():
    SQLModel.metadata.drop_all(engine)
    SQLModel.metadata.create_all(engine)
    init_db()
    config_store.set_many({
        "delivery_cards_api_enabled": "true",
        "delivery_cards_api_failure_mode": "debug",
    })
    token = service.rotate_api_token()["token"]
    globals()["TOKEN"] = token


def _account(email: str, *, plan: str, expires_days: int = 20, manually_used: bool = False) -> AccountModel:
    expires = (datetime.now(timezone.utc) + timedelta(days=expires_days)).isoformat()
    extra = {
        "manually_used": manually_used,
        "chatgpt_capabilities": {"subscription_plan": plan, "auth_level": "refresh_token"},
        "chatgpt_local": {"subscription": {"plan": plan, "subscription_active_until": expires}},
        "refresh_token": "rt",
    }
    return AccountModel(
        platform="chatgpt",
        email=email,
        password="pw",
        status="subscribed" if plan == "plus" else "registered",
        extra_json=service.safe_json_dumps(extra),
    )


def _create_batch(sku="plus", count=1):
    with Session(engine) as session:
        result = service.create_batch(session, name="test", sku_code=sku, count=count, strict_stock_check=False)
        return result


def _redeem(code: str, request_id="order1"):
    return service.redeem_card(
        code=code,
        consumer="tester",
        request_id=request_id,
        idempotency_key=request_id,
        authorization=f"Bearer {TOKEN}",
        client_ip="127.0.0.1",
        user_agent="pytest",
    )


def test_first_redeem_marks_account_used_and_logs():
    with Session(engine) as session:
        session.add(_account("plus@example.com", plan="plus", expires_days=5))
        session.commit()
    code = _create_batch("plus", 1)["codes"][0]["code"]

    response = _redeem(code)
    assert response["ok"] is True
    assert response["first_redeem"] is True
    assert response["redeem_index"] == 1
    assert response["account"]["email"] == "plus@example.com"

    with Session(engine) as session:
        account = session.get(AccountModel, 1)
        assert account.get_extra()["manually_used"] is True
        card = session.exec(select(DeliveryCardModel)).first()
        assert card.status == service.STATUS_REDEEMED
        assert card.assigned_account_id == account.id
        logs = session.exec(select(DeliveryRedeemApiLogModel)).all()
        assert len(logs) == 1
        assert logs[0].result == service.RESULT_SUCCESS
        assert logs[0].duplicate_check_status == "passed"


def test_pool_empty_does_not_consume_card():
    code = _create_batch("plus", 1)["codes"][0]["code"]
    response = _redeem(code)
    assert response["ok"] is False
    assert response["error_code"] == service.ERROR_POOL_EMPTY
    with Session(engine) as session:
        card = session.exec(select(DeliveryCardModel)).first()
        assert card.status == service.STATUS_UNUSED
        assert card.assigned_account_id == 0
        assert card.redeem_count == 0
        log = session.exec(select(DeliveryRedeemApiLogModel)).first()
        assert log.error_code == service.ERROR_POOL_EMPTY


def test_success_idempotency_does_not_increment_count():
    with Session(engine) as session:
        session.add(_account("plus@example.com", plan="plus"))
        session.commit()
    code = _create_batch("plus", 1)["codes"][0]["code"]
    first = _redeem(code, request_id="same-order")
    second = _redeem(code, request_id="same-order")
    assert first["ok"] is True
    assert second["ok"] is True
    assert second["idempotent_replay"] is True
    assert second["redeem_index"] == 1
    with Session(engine) as session:
        card = session.exec(select(DeliveryCardModel)).first()
        assert card.redeem_count == 1


def test_refetch_with_different_request_increments_count():
    with Session(engine) as session:
        session.add(_account("plus@example.com", plan="plus"))
        session.commit()
    code = _create_batch("plus", 1)["codes"][0]["code"]
    assert _redeem(code, request_id="order1")["redeem_index"] == 1
    assert _redeem(code, request_id="order2")["redeem_index"] == 2
    with Session(engine) as session:
        card = session.exec(select(DeliveryCardModel)).first()
        assert card.redeem_count == 2


def test_free_excludes_unknown():
    with Session(engine) as session:
        session.add(_account("unknown@example.com", plan="unknown"))
        session.add(_account("free@example.com", plan="free"))
        session.commit()
    code = _create_batch("free", 1)["codes"][0]["code"]
    response = _redeem(code)
    assert response["ok"] is True
    assert response["account"]["email"] == "free@example.com"


def test_duplicate_api_log_blocks_assignment():
    with Session(engine) as session:
        account = _account("plus@example.com", plan="plus")
        session.add(account)
        session.commit()
        session.refresh(account)
        session.add(DeliveryRedeemApiLogModel(
            trace_id="old",
            card_id=999,
            sku_code="plus",
            assigned_account_id=account.id,
            assigned_account_email=account.email,
            action="first_redeem",
            result="success",
        ))
        session.commit()
    code = _create_batch("plus", 1)["codes"][0]["code"]
    response = _redeem(code)
    assert response["ok"] is False
    assert response["error_code"] == service.ERROR_DUPLICATE_ACCOUNT_DETECTED
    with Session(engine) as session:
        card = session.exec(select(DeliveryCardModel)).first()
        assert card.status == service.STATUS_UNUSED


def test_enable_redeemed_disabled_card_restores_redeemed():
    with Session(engine) as session:
        session.add(_account("plus@example.com", plan="plus"))
        session.commit()
    code = _create_batch("plus", 1)["codes"][0]["code"]
    assert _redeem(code)["ok"] is True
    with Session(engine) as session:
        card = session.exec(select(DeliveryCardModel)).first()
        service.set_card_status(session, card.id, service.STATUS_DISABLED, reason="test")
    with Session(engine) as session:
        card = session.exec(select(DeliveryCardModel)).first()
        restored = service.set_card_status(session, card.id, service.STATUS_UNUSED, reason="enable")
        assert restored["status"] == service.STATUS_REDEEMED
        assert restored["assigned_account_id"] == 1


def test_lookup_by_full_code_and_consistency_repair():
    with Session(engine) as session:
        session.add(_account("plus@example.com", plan="plus"))
        session.commit()
    code = _create_batch("plus", 1)["codes"][0]["code"]
    assert _redeem(code)["ok"] is True
    with Session(engine) as session:
        found = service.lookup_card_by_code(session, code)
        assert found["lookup"]["matched"] is True
        card = session.exec(select(DeliveryCardModel)).first()
        account = session.get(AccountModel, card.assigned_account_id)
        extra = account.get_extra()
        extra["manually_used"] = False
        extra.pop("delivery_card_assignment", None)
        account.extra_json = service.safe_json_dumps(extra)
        card.status = service.STATUS_UNUSED
        session.add(card)
        session.add(account)
        session.commit()
    with Session(engine) as session:
        report = service.scan_consistency(session)
        assert report["issue_count"] >= 1
        assert report["repairable_count"] >= 1
        repaired = service.repair_consistency(session)
        assert repaired["repaired_count"] >= 1
        card = session.exec(select(DeliveryCardModel)).first()
        account = session.get(AccountModel, card.assigned_account_id)
        assert card.status == service.STATUS_REDEEMED
        assert account.get_extra()["manually_used"] is True


def test_rate_limit_records_each_call():
    config_store.set("delivery_cards_api_rate_limit_per_minute", "2")
    with Session(engine) as session:
        session.add(_account("plus@example.com", plan="plus"))
        session.commit()
    code = _create_batch("plus", 1)["codes"][0]["code"]
    assert _redeem(code, request_id="order1")["ok"] is True
    assert _redeem(code, request_id="order2")["ok"] is True
    third = _redeem(code, request_id="order3")
    assert third["ok"] is False
    assert third["error_code"] == service.ERROR_RATE_LIMITED
    with Session(engine) as session:
        logs = session.exec(select(DeliveryRedeemApiLogModel)).all()
        assert len(logs) == 3
        assert logs[-1].error_code == service.ERROR_RATE_LIMITED


def test_redeem_response_includes_phone_binding_and_sms_api():
    with Session(engine) as session:
        acc = _account("plus-phone@example.com", plan="plus")
        extra = acc.get_extra()
        extra["chatgpt_phone_binding"] = {
            "phone": "+12345678900",
            "api_url": "https://sms.example.test/code?id=1",
            "status": "bound",
            "bound_at": "2026-06-09 10:00:00",
        }
        acc.extra_json = service.safe_json_dumps(extra)
        session.add(acc)
        session.commit()
    code = _create_batch("plus", 1)["codes"][0]["code"]
    response = _redeem(code)
    assert response["ok"] is True
    assert response["account"]["phone"] == "+12345678900"
    assert response["account"]["sms_api"] == "https://sms.example.test/code?id=1"
    assert response["account"]["phone_binding"]["bound"] is True


def test_redeem_response_reports_missing_phone_binding():
    with Session(engine) as session:
        session.add(_account("plus-no-phone@example.com", plan="plus"))
        session.commit()
    code = _create_batch("plus", 1)["codes"][0]["code"]
    response = _redeem(code)
    assert response["ok"] is True
    assert response["account"]["phone"] == ""
    assert response["account"]["sms_api"] == ""
    assert response["account"]["phone_binding"]["bound"] is False
    assert response["account"]["phone_binding"]["message"] == "该账号没有手机号绑定记录"
