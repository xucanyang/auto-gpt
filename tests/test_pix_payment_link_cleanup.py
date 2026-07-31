from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import json
import sqlite3

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from core.db import AccountListStateModel, AccountModel, PaymentLinkGenerationModel
from services.chatgpt_core import pix_payment_link_cleanup as pix_cleanup
from services.chatgpt_core.pix_payment_link_cleanup import (
    PIX_CLEANUP_MODE_CANCELLED,
    PIX_CLEANUP_MODE_PAID,
    PIX_EXPIRED_CLEANED_STATUS,
    clean_pix_payment_links,
    clean_expired_pix_payment_links,
    clean_ideal_payment_links,
    ideal_effective_expires_at,
    parse_stripe_pix_instruction_html,
    pix_schedule_expires_at,
    preview_pix_payment_link_cleanup,
    preview_payment_link_cleanup,
    preview_expired_pix_payment_links,
    clean_upi_payment_links,
    parse_stripe_payment_instruction_html,
    preview_upi_payment_link_cleanup,
)
from services.chatgpt_core.payment_link_cache import (
    IDEAL_EXPIRED_CLEANED_STATUS,
    PAYMENT_LINK_DELETED_STATUS,
    PIX_CANCELLED_CLEANED_STATUS,
    PIX_PAID_CLEANED_STATUS,
    payment_link_requires_regeneration,
)


NOW = datetime(2026, 7, 16, 4, 0, tzinfo=timezone.utc)


def _stripe_html(intent_state: str, *, server_timestamp: int | None = None, **extra) -> bytes:
    payload = {
        "type": "qr_instructions",
        "payment_method_type": "pix",
        "intent_state": intent_state,
        "server_timestamp": int(server_timestamp or NOW.timestamp()),
        **extra,
    }
    encoded = base64.b64encode(json.dumps(payload).encode()).decode()
    return f'<html><head><meta id="payload" data-message="{encoded}"></head></html>'.encode()


def _pix_link(url: str, *, generated_at: str = "", expires_at: int | None = None) -> dict:
    payload = {
        "url": url,
        "long_url": url,
        "link_type": "pix",
        "payment_method_type": "pix",
        "payment_link_format": "long_link",
        "plan": "plus",
    }
    if generated_at:
        payload["generated_at"] = generated_at
        payload["created_at"] = generated_at
    if expires_at is not None:
        payload["link_expires_at"] = expires_at
    return payload


def _upi_link(url: str, *, generated_at: str = "", expires_at: int | None = None) -> dict:
    payload = {
        "url": url,
        "long_url": url,
        "link_type": "upi",
        "payment_method_type": "upi",
        "payment_link_format": "long_link",
        "plan": "plus",
    }
    if generated_at:
        payload["generated_at"] = generated_at
        payload["created_at"] = generated_at
    if expires_at is not None:
        payload["link_expires_at"] = expires_at
        payload["link_expiry_source"] = "upi_qr_code"
    return payload


def _typed_link(
    payment_type: str,
    url: str,
    *,
    generated_at: str = "",
    expires_at: int | None = None,
    link_status: str = "",
) -> dict:
    payload = {
        "url": url,
        "long_url": url,
        "link_type": payment_type,
        "payment_method_type": payment_type,
        "payment_link_format": "long_link",
        "plan": "plus",
    }
    if generated_at:
        payload["generated_at"] = generated_at
        payload["created_at"] = generated_at
    if expires_at is not None:
        payload["link_expires_at"] = expires_at
        payload["link_expiry_source"] = "provider"
    if link_status:
        payload["link_status"] = link_status
    return payload


def _account(
    account_id: int,
    link: dict,
    *,
    cashier_url: str | None = None,
    status: str = "registered",
    payment_marker: dict | None = None,
) -> AccountModel:
    account = AccountModel(
        id=account_id,
        platform="chatgpt",
        email=f"pix-cleanup-{account_id}@example.test",
        password="pw",
        status=status,
        cashier_url=link.get("url", "") if cashier_url is None else cashier_url,
    )
    extra = {"keep": {"account_id": account_id}, "chatgpt_last_payment_link": link}
    if payment_marker is not None:
        extra["baxigpt_cdk"] = payment_marker
    account.set_extra(extra)
    return account


def _engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


def test_beijing_11_schedule_treats_exact_rollover_as_the_new_cycle():
    before = pix_schedule_expires_at("2026-07-16T02:59:59+00:00")
    exact = pix_schedule_expires_at("2026-07-16T03:00:00+00:00")

    assert before == datetime(2026, 7, 16, 3, 0, tzinfo=timezone.utc)
    assert exact == datetime(2026, 7, 17, 3, 0, tzinfo=timezone.utc)


def test_upi_instruction_parser_keeps_qr_expiry_and_never_exposes_secrets():
    secret = "seti_secret_should_not_escape"
    payload = {
        "type": "upi",
        "payment_method_type": "upi",
        "intent_state": "requires_action",
        "server_timestamp": int(NOW.timestamp()),
        "expires_at": int((NOW + timedelta(minutes=5)).timestamp()),
        "client_secret": secret,
        "publishable_key": "pk_live_should_not_escape",
    }
    encoded = base64.b64encode(json.dumps(payload).encode()).decode()
    result = parse_stripe_payment_instruction_html(
        f'<meta id="payload" data-message="{encoded}">'.encode(),
        expected_payment_type="upi",
    )

    assert result.success is True
    assert result.payment_type == "upi"
    assert result.expires_at == NOW + timedelta(minutes=5)
    assert result.expiry_source == "upi_qr_code"
    assert secret not in repr(result)


def test_upi_cleanup_uses_qr_expiry_and_leaves_valid_link(monkeypatch):
    engine = _engine()
    expired_epoch = int((NOW - timedelta(seconds=1)).timestamp())
    valid_epoch = int((NOW + timedelta(seconds=1)).timestamp())
    with Session(engine) as session:
        session.add_all(
            [
                _account(601, _upi_link("https://payments.stripe.com/upi/instructions/expired", expires_at=expired_epoch)),
                _account(602, _upi_link("https://payments.stripe.com/upi/instructions/valid", expires_at=valid_epoch)),
            ]
        )
        session.commit()

    monkeypatch.setattr(
        pix_cleanup,
        "_fetch_stripe_pix_instruction",
        lambda _url: pix_cleanup.StripePaymentDirectState(
            True,
            True,
            "requires_action",
            NOW,
            payment_type="upi",
        ),
    )
    with Session(engine) as session:
        preview = preview_upi_payment_link_cleanup(session, now=NOW)
    assert preview["current_upi_links"] == 2
    assert preview["upi_expired_links"] == 1
    assert preview["eligible_links"] == 1
    assert preview["upi_qr_validity_seconds"] == 300
    assert preview["provider_expiry_links"] == 2

    with Session(engine) as session:
        result = clean_upi_payment_links(session, now=NOW)
    assert result["cleaned_links"] == 1
    with Session(engine) as session:
        cleaned = session.get(AccountModel, 601)
        retained = session.get(AccountModel, 602)
        assert cleaned is not None and retained is not None
        assert cleaned.get_extra()["chatgpt_last_payment_link"]["link_status"] == "upi_expired_cleaned"
        assert retained.get_extra()["chatgpt_last_payment_link"]["url"].endswith("/valid")


def test_upi_cleanup_rejects_explicit_checkout_session_expiry(monkeypatch):
    engine = _engine()
    payload = _upi_link(
        "https://payments.stripe.com/upi/instructions/checkout-expiry",
        expires_at=int((NOW - timedelta(hours=1)).timestamp()),
    )
    payload["link_expiry_source"] = "checkout_session"
    untagged_checkout = _upi_link(
        "https://payments.stripe.com/upi/instructions/untagged-checkout-expiry",
        generated_at=NOW.isoformat(),
        expires_at=int((NOW + timedelta(hours=24)).timestamp()),
    )
    untagged_checkout.pop("link_expiry_source", None)
    with Session(engine) as session:
        session.add_all([_account(603, payload), _account(604, untagged_checkout)])
        session.commit()

    monkeypatch.setattr(
        pix_cleanup,
        "_fetch_stripe_pix_instruction",
        lambda _url: pix_cleanup.StripePaymentDirectState(True, False),
    )
    with Session(engine) as session:
        preview = preview_upi_payment_link_cleanup(session, now=NOW)

    assert preview["current_upi_links"] == 2
    assert preview["upi_expired_links"] == 0
    assert preview["missing_expiry_links"] == 2
    assert preview["eligible_links"] == 0


def test_mixed_scan_classifies_every_payment_type_and_retains_unknown_states():
    engine = _engine()
    future_epoch = int((NOW + timedelta(hours=1)).timestamp())
    with Session(engine) as session:
        session.add_all(
            [
                _account(701, _typed_link("hosted", "https://pay.openai.com/hosted", link_status="payment_expired")),
                _account(702, _typed_link("paypal", "https://www.paypal.com/checkout", expires_at=future_epoch)),
                _account(703, _typed_link("ideal", "https://pay.ideal.nl/transactions/expired", generated_at="2026-07-16T03:44:00+00:00")),
                _account(704, _typed_link("upi", "https://payments.example.test/upi")),
                _account(705, _typed_link("pix", "https://payments.example.test/pix", generated_at="2026-07-16T03:30:00+00:00")),
                _account(706, _typed_link("twint", "https://pay.twint.ch/order", link_status="paid")),
                _account(707, _typed_link("kakao_pay", "https://pay.kakaopay.com/order", link_status="payment_cancelled")),
                _account(708, _typed_link("gopay", "https://example.test/gopay")),
                _account(709, _typed_link("team", "https://chatgpt.com/checkout/team")),
                _account(710, _typed_link("other", "https://example.test/unknown")),
            ]
        )
        session.commit()

    with Session(engine) as session:
        scan = preview_payment_link_cleanup(session, now=NOW)

    assert scan["total_links"] == 10
    assert scan["payment_type_counts"] == {
        "hosted": 1,
        "paypal": 1,
        "ideal": 1,
        "upi": 1,
        "pix": 1,
        "twint": 1,
        "kakao_pay": 1,
        "team": 1,
        "other": 2,
    }
    assert scan["valid_links"] == 2
    assert scan["expired_links"] == 2
    assert scan["paid_links"] == 1
    assert scan["cancelled_links"] == 1
    assert scan["unknown_links"] == 4
    assert scan["ideal_expired_links"] == 1
    assert scan["ideal_derived_expiry_links"] == 1
    assert scan["ideal_validity_seconds"] == 15 * 60
    assert scan["direct_scan_supported_links"] == 2
    assert scan["direct_scan_success_links"] == 0
    assert scan["direct_scan_fallback_links"] == 2
    assert sum(
        scan[key]
        for key in ("valid_links", "expired_links", "paid_links", "cancelled_links", "unknown_links")
    ) == scan["total_links"]


def test_ideal_expiry_is_extraction_plus_15_minutes_and_cleanup_keeps_newer_link():
    expires_at, source = ideal_effective_expires_at({"generated_at": "2026-07-16T03:45:00+00:00"})
    assert expires_at == NOW
    assert source == "ideal_generated_15m"
    assert ideal_effective_expires_at({}) == (None, "missing")

    engine = _engine()
    with Session(engine) as session:
        session.add_all(
            [
                _account(711, _typed_link("ideal", "https://pay.ideal.nl/transactions/expired", generated_at="2026-07-16T03:44:59+00:00")),
                _account(712, _typed_link("ideal", "https://pay.ideal.nl/transactions/valid", generated_at="2026-07-16T03:45:01+00:00")),
                _account(713, _typed_link("ideal", "https://pay.ideal.nl/transactions/history-expired")),
            ]
        )
        session.add(
            PaymentLinkGenerationModel(
                account_id=713,
                request_id="ideal-history-expiry",
                link_type="ideal",
                status="succeeded",
                url="https://pay.ideal.nl/transactions/history-expired",
                generated_at="2026-07-16T03:40:00+00:00",
            )
        )
        session.commit()

    with Session(engine) as session:
        preview = preview_payment_link_cleanup(session, payment_type="ideal", now=NOW)
    assert preview["ideal_links"] == 3
    assert preview["ideal_expired_links"] == 2
    assert preview["ideal_valid_links"] == 1
    assert preview["ideal_unknown_links"] == 0

    with Session(engine) as session:
        result = clean_ideal_payment_links(session, now=NOW)
    assert result["cleaned_links"] == 2

    with Session(engine) as session:
        expired = session.get(AccountModel, 711)
        valid = session.get(AccountModel, 712)
        assert expired is not None and valid is not None
        marker = expired.get_extra()["chatgpt_last_payment_link"]
        assert marker["link_status"] == IDEAL_EXPIRED_CLEANED_STATUS
        assert marker["link_expiry_source"] == "ideal_generated_15m"
        assert marker["link_expires_at"] == int(datetime(2026, 7, 16, 3, 59, 59, tzinfo=timezone.utc).timestamp())
        assert "url" not in marker
        assert valid.get_extra()["chatgpt_last_payment_link"]["url"].endswith("/valid")
        state = session.get(AccountListStateModel, 711)
        assert state is not None
        assert state.payment_link_platform == "none"


def test_team_expiry_is_extraction_plus_24_hours_with_history_fallback():
    expires_at, source = pix_cleanup.team_effective_expires_at(
        {
            "generated_at": "2026-07-15T04:00:00+00:00",
            "link_expires_at": int((NOW + timedelta(days=30)).timestamp()),
        }
    )
    assert expires_at == NOW
    assert source == "team_generated_24h"
    assert pix_cleanup.team_effective_expires_at({}) == (None, "missing")

    engine = _engine()
    with Session(engine) as session:
        session.add_all(
            [
                _account(
                    714,
                    _typed_link(
                        "team",
                        "https://chatgpt.com/checkout/team-valid",
                        generated_at="2026-07-15T04:00:01+00:00",
                    ),
                ),
                _account(
                    715,
                    _typed_link(
                        "team",
                        "https://chatgpt.com/checkout/team-exact-expiry",
                        generated_at="2026-07-15T04:00:00+00:00",
                    ),
                ),
                _account(716, _typed_link("team", "https://chatgpt.com/checkout/team-history-expiry")),
                _account(717, _typed_link("team", "https://chatgpt.com/checkout/team-missing-time")),
            ]
        )
        session.add(
            PaymentLinkGenerationModel(
                account_id=716,
                request_id="team-history-expiry",
                link_type="team",
                status="succeeded",
                url="https://chatgpt.com/checkout/team-history-expiry",
                generated_at="2026-07-15T03:59:59+00:00",
            )
        )
        session.commit()

    with Session(engine) as session:
        preview = preview_payment_link_cleanup(session, payment_type="team", now=NOW)

    assert preview["team_links"] == 4
    assert preview["team_valid_links"] == 1
    assert preview["team_expired_links"] == 2
    assert preview["team_unknown_links"] == 1
    assert preview["team_derived_expiry_links"] == 3
    assert preview["team_validity_seconds"] == 24 * 60 * 60
    assert preview["derived_expiry_links"] == 3


def test_all_payment_types_support_all_five_manual_delete_modes():
    cleanup_modes = ("valid", "paid", "expired", "cancelled", "unknown")
    status_by_mode = {
        "paid": "paid",
        "expired": "payment_expired",
        "cancelled": "payment_cancelled",
    }
    legacy_terminal_modes = {
        (payment_type, mode)
        for payment_type in ("pix", "upi", "ideal")
        for mode in ("paid", "expired", "cancelled")
    }

    for type_index, payment_type in enumerate(pix_cleanup.PAYMENT_LINK_TYPE_ORDER, start=1):
        engine = _engine()
        account_ids: dict[str, int] = {}
        with Session(engine) as session:
            for mode_index, mode in enumerate(cleanup_modes, start=1):
                account_id = type_index * 100 + mode_index
                account_ids[mode] = account_id
                url = f"https://example.test/{payment_type}/{mode}"
                if mode == "valid" and payment_type in {"ideal", "team"}:
                    link = _typed_link(payment_type, url, generated_at=NOW.isoformat())
                elif mode == "valid":
                    link = _typed_link(
                        payment_type,
                        url,
                        expires_at=int((NOW + timedelta(hours=1)).timestamp()),
                    )
                else:
                    link = _typed_link(
                        payment_type,
                        url,
                        link_status=status_by_mode.get(mode, ""),
                    )
                session.add(_account(account_id, link))
            session.commit()

        for mode in cleanup_modes:
            with Session(engine) as session:
                preview = preview_payment_link_cleanup(
                    session,
                    payment_type=payment_type,
                    cleanup_mode=mode,
                    now=NOW,
                )
            assert preview["eligible_links"] == 1, (payment_type, mode, preview)

        for mode in cleanup_modes:
            with Session(engine) as session:
                result = pix_cleanup.clean_payment_links(
                    session,
                    payment_type=payment_type,
                    cleanup_mode=mode,
                    now=NOW,
                )
            assert result["cleaned_links"] == 1, (payment_type, mode, result)
            with Session(engine) as session:
                account = session.get(AccountModel, account_ids[mode])
                assert account is not None
                marker = account.get_extra()["chatgpt_last_payment_link"]
                assert "url" not in marker
                assert "long_url" not in marker
                assert marker["payment_link_cleanup_type"] == payment_type
                assert marker["payment_link_cleanup_mode"] == mode
                assert account.cashier_url == ""
                assert payment_link_requires_regeneration(marker) is True
                if (payment_type, mode) not in legacy_terminal_modes:
                    assert marker["link_status"] == PAYMENT_LINK_DELETED_STATUS


def test_preview_and_cleanup_are_scoped_atomic_and_idempotent():
    engine = _engine()
    future_epoch = int(datetime(2026, 7, 17, 3, 0, tzinfo=timezone.utc).timestamp())
    expired_epoch = int(datetime(2026, 7, 16, 3, 0, tzinfo=timezone.utc).timestamp())
    with Session(engine) as session:
        session.add_all(
            [
                _account(
                    1,
                    _pix_link("https://payments.example.test/old-derived", generated_at="2026-07-15T10:57:06+00:00"),
                    status="pending_payment",
                ),
                _account(
                    2,
                    _pix_link(
                        "https://payments.example.test/new-provider",
                        generated_at="2026-07-16T03:09:00+00:00",
                        expires_at=future_epoch,
                    ),
                ),
                _account(
                    3,
                    _pix_link(
                        "https://payments.example.test/old-generation-future-provider",
                        generated_at="2026-07-15T01:00:00+00:00",
                        expires_at=future_epoch,
                    ),
                ),
                _account(
                    4,
                    _pix_link("https://payments.example.test/before-cutoff", generated_at="2026-07-16T02:59:59+00:00"),
                    cashier_url="https://payments.example.test/unrelated-current-value",
                ),
                _account(
                    5,
                    _pix_link("https://payments.example.test/exact-cutoff", generated_at="2026-07-16T03:00:00+00:00"),
                ),
                _account(6, _pix_link("https://payments.example.test/missing-time")),
                _account(
                    7,
                    {
                        "url": "https://payments.example.test/paypal",
                        "link_type": "paypal",
                        "generated_at": "2026-07-01T00:00:00+00:00",
                    },
                ),
                _account(
                    8,
                    _pix_link(
                        "https://payments.example.test/provider-expired",
                        generated_at="2026-07-16T03:30:00+00:00",
                        expires_at=expired_epoch,
                    ),
                ),
            ]
        )
        generation = PaymentLinkGenerationModel(
            account_id=1,
            request_id="cleanup-history-one",
            link_type="pix",
            status="succeeded",
            url="https://payments.example.test/old-derived",
            generated_at="2026-07-15T10:57:06+00:00",
        )
        session.add(generation)
        session.commit()

    with Session(engine) as session:
        preview = preview_expired_pix_payment_links(session, now=NOW)

    assert preview == {
        "instance_id": "auto-gpt",
        "timezone": "Asia/Shanghai",
        "now": "2026-07-16T04:00:00+00:00",
        "cutoff_at": "2026-07-16T03:00:00+00:00",
        "cutoff_at_beijing": "2026-07-16T11:00:00+08:00",
        "cutoff_display": "2026-07-16 11:00",
        "current_pix_links": 7,
        "cleanup_mode": "expired",
        "cleanup_label": "过期",
        "expired_links": 3,
        "paid_links": 0,
        "cancelled_links": 0,
        "valid_links": 4,
        "eligible_links": 3,
        "retained_links": 4,
        "active_links": 3,
        "provider_expiry_links": 3,
        "derived_expiry_links": 3,
        "missing_expiry_links": 1,
        "valid_missing_expiry_links": 1,
        "direct_scan_source": "stripe_direct",
        "direct_scan_attempted_links": 0,
        "direct_scan_success_links": 0,
        "direct_scan_fallback_links": 7,
        "direct_scan_state_counts": {},
    }

    with Session(engine) as session:
        report = clean_expired_pix_payment_links(session, now=NOW)

    assert report["cleaned_links"] == 3
    assert report["concurrent_skipped_links"] == 0
    assert report["list_state_refreshed"] == 3
    assert report["backup_created"] is False

    with Session(engine) as session:
        cleaned = session.get(AccountModel, 1)
        assert cleaned is not None
        cleaned_extra = cleaned.get_extra()
        marker = cleaned_extra["chatgpt_last_payment_link"]
        assert cleaned.status == "pending_payment"
        assert cleaned.cashier_url == ""
        assert cleaned_extra["keep"] == {"account_id": 1}
        assert marker["link_status"] == PIX_EXPIRED_CLEANED_STATUS
        assert marker["link_expiry_source"] == "beijing_11"
        assert marker["pix_cleanup_through_at"] == "2026-07-16T03:00:00+00:00"
        assert "url" not in marker
        assert "long_url" not in marker

        unrelated_cashier = session.get(AccountModel, 4)
        assert unrelated_cashier is not None
        assert unrelated_cashier.cashier_url == "https://payments.example.test/unrelated-current-value"

        exact_cutoff = session.get(AccountModel, 5)
        assert exact_cutoff is not None
        assert exact_cutoff.get_extra()["chatgpt_last_payment_link"]["url"].endswith("/exact-cutoff")

        paypal = session.get(AccountModel, 7)
        assert paypal is not None
        assert paypal.get_extra()["chatgpt_last_payment_link"]["url"].endswith("/paypal")

        for account_id in (1, 4, 8):
            state = session.get(AccountListStateModel, account_id)
            assert state is not None
            assert state.payment_link_platform == "none"

        history = session.exec(select(PaymentLinkGenerationModel)).all()
        assert len(history) == 1
        assert history[0].url == "https://payments.example.test/old-derived"

    with Session(engine) as session:
        repeated = clean_expired_pix_payment_links(session, now=NOW)
    assert repeated["expired_links"] == 0
    assert repeated["cleaned_links"] == 0
    assert repeated["current_pix_links"] == 4
    assert repeated["backup_created"] is False


def test_paid_cleanup_requires_current_link_payment_evidence_and_leaves_a_terminal_tombstone():
    engine = _engine()
    with Session(engine) as session:
        direct_paid = _pix_link("https://payments.example.test/direct-paid", generated_at="2026-07-16T03:10:00+00:00")
        direct_paid["link_status"] = "already_paid"
        submitted_paid = _pix_link("https://payments.example.test/submitted-paid", generated_at="2026-07-16T03:20:00+00:00")
        submitted_paid["link_status"] = "pix_submitted"
        stale_new_link = _pix_link("https://payments.example.test/new-after-paid", generated_at="2026-07-16T04:30:00+00:00")
        stale_new_link["link_status"] = "pix_submitted"
        auto_extract_link = _pix_link("https://payments.example.test/auto-extract", generated_at="2026-07-16T03:20:00+00:00")
        auto_extract_link["link_status"] = "pix_submitted"
        session.add_all(
            [
                _account(21, direct_paid),
                _account(
                    22,
                    submitted_paid,
                    payment_marker={
                        "status": "paid",
                        "payment_channel": "pix",
                        "pix_submit_mode": "user_link",
                        "submitted_at": "2026-07-16T03:21:00+00:00",
                        "last_checked_at": "2026-07-16T04:00:00+00:00",
                    },
                ),
                _account(
                    23,
                    stale_new_link,
                    payment_marker={
                        "status": "paid",
                        "payment_channel": "pix",
                        "pix_submit_mode": "user_link",
                        "last_checked_at": "2026-07-16T04:00:00+00:00",
                    },
                ),
                _account(
                    24,
                    auto_extract_link,
                    payment_marker={
                        "status": "paid",
                        "payment_channel": "pix",
                        "pix_submit_mode": "auto_extract",
                        "last_checked_at": "2026-07-16T04:00:00+00:00",
                    },
                ),
            ]
        )
        session.commit()

    with Session(engine) as session:
        preview = preview_pix_payment_link_cleanup(session, cleanup_mode=PIX_CLEANUP_MODE_PAID, now=NOW)
    assert preview["paid_links"] == 2
    assert preview["eligible_links"] == 2
    assert preview["retained_links"] == 2

    with Session(engine) as session:
        report = clean_pix_payment_links(session, cleanup_mode=PIX_CLEANUP_MODE_PAID, now=NOW)
    assert report["cleaned_links"] == 2

    with Session(engine) as session:
        for account_id in (21, 22):
            marker = session.get(AccountModel, account_id).get_extra()["chatgpt_last_payment_link"]
            assert marker["link_status"] == PIX_PAID_CLEANED_STATUS
            assert marker["pix_cleanup_mode"] == "paid"
            assert marker["pix_cleanup_through_at"] == NOW.isoformat()
            assert "url" not in marker
        assert session.get(AccountModel, 23).get_extra()["chatgpt_last_payment_link"]["url"].endswith("new-after-paid")
        assert session.get(AccountModel, 24).get_extra()["chatgpt_last_payment_link"]["url"].endswith("auto-extract")


def test_cancelled_cleanup_accepts_explicit_payment_cancel_evidence_but_not_generic_failures():
    engine = _engine()
    with Session(engine) as session:
        direct_cancelled = _pix_link("https://payments.example.test/direct-cancelled", generated_at="2026-07-16T03:10:00+00:00")
        direct_cancelled["link_status"] = "payment_canceled"
        session.add_all(
            [
                _account(31, direct_cancelled),
                _account(
                    32,
                    _pix_link("https://payments.example.test/cancelled-marker", generated_at="2026-07-16T03:20:00+00:00"),
                    payment_marker={
                        "status": "failed",
                        "upstream_status": "failed",
                        "payment_channel": "pix",
                        "pix_submit_mode": "user_link",
                        "submitted_at": "2026-07-16T03:30:00+00:00",
                        "last_checked_at": "2026-07-16T04:00:00+00:00",
                        "last_error_message": '上游 HTTP 409: {"detail":"PIX 支付已取消，请重新生成支付链接"}',
                    },
                ),
                _account(
                    33,
                    _pix_link("https://payments.example.test/generic-failure", generated_at="2026-07-16T03:20:00+00:00"),
                    payment_marker={
                        "status": "failed",
                        "payment_channel": "pix",
                        "pix_submit_mode": "user_link",
                        "last_checked_at": "2026-07-16T04:00:00+00:00",
                        "last_error_message": "PIX 上游处理失败",
                    },
                ),
            ]
        )
        session.commit()

    with Session(engine) as session:
        preview = preview_pix_payment_link_cleanup(session, cleanup_mode=PIX_CLEANUP_MODE_CANCELLED, now=NOW)
    assert preview["cancelled_links"] == 2
    assert preview["eligible_links"] == 2

    with Session(engine) as session:
        report = clean_pix_payment_links(session, cleanup_mode=PIX_CLEANUP_MODE_CANCELLED, now=NOW)
    assert report["cleaned_links"] == 2

    with Session(engine) as session:
        for account_id in (31, 32):
            marker = session.get(AccountModel, account_id).get_extra()["chatgpt_last_payment_link"]
            assert marker["link_status"] == PIX_CANCELLED_CLEANED_STATUS
            assert marker["pix_cleanup_mode"] == "cancelled"
            assert "url" not in marker
        assert session.get(AccountModel, 33).get_extra()["chatgpt_last_payment_link"]["url"].endswith("generic-failure")


def test_scan_buckets_are_mutually_exclusive_and_cleanup_matches_each_bucket():
    engine = _engine()
    with Session(engine) as session:
        paid_expired = _pix_link(
            "https://payments.example.test/paid-expired",
            generated_at="2026-07-15T01:00:00+00:00",
        )
        paid_expired["link_status"] = "paid"
        cancelled_expired = _pix_link(
            "https://payments.example.test/cancelled-expired",
            generated_at="2026-07-15T01:00:00+00:00",
        )
        cancelled_expired["link_status"] = "payment_cancelled"
        session.add_all(
            [
                _account(41, paid_expired),
                _account(42, cancelled_expired),
                _account(43, _pix_link("https://payments.example.test/expired", generated_at="2026-07-15T01:00:00+00:00")),
                _account(44, _pix_link("https://payments.example.test/valid", generated_at="2026-07-16T03:30:00+00:00")),
                _account(45, _pix_link("https://payments.example.test/missing-time")),
            ]
        )
        session.commit()

    with Session(engine) as session:
        scan = preview_pix_payment_link_cleanup(session, now=NOW)

    assert scan["current_pix_links"] == 5
    assert scan["valid_links"] == 1
    assert scan["paid_links"] == 1
    assert scan["expired_links"] == 1
    assert scan["cancelled_links"] == 1
    assert scan["valid_missing_expiry_links"] == 0
    assert scan["unknown_links"] == 1
    assert scan["unknown_missing_expiry_links"] == 1
    assert sum(scan[key] for key in ("valid_links", "paid_links", "expired_links", "cancelled_links", "unknown_links")) == 5

    with Session(engine) as session:
        expired = clean_expired_pix_payment_links(session, now=NOW)
    assert expired["cleaned_links"] == 1
    assert expired["eligible_links"] == 1

    with Session(engine) as session:
        assert session.get(AccountModel, 41).get_extra()["chatgpt_last_payment_link"]["url"].endswith("paid-expired")
        assert session.get(AccountModel, 42).get_extra()["chatgpt_last_payment_link"]["url"].endswith("cancelled-expired")
        assert session.get(AccountModel, 43).get_extra()["chatgpt_last_payment_link"]["link_status"] == PIX_EXPIRED_CLEANED_STATUS


def test_file_database_cleanup_creates_a_verified_backup(tmp_path, monkeypatch):
    database = tmp_path / "account_manager.db"
    backup_runtime = tmp_path / "runtime"
    backup_runtime.mkdir()
    monkeypatch.setenv("APP_RUNTIME_DIR", str(backup_runtime))
    engine = create_engine(f"sqlite:///{database}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            _account(
                1,
                _pix_link("https://payments.example.test/file-backup", generated_at="2026-07-15T10:00:00+00:00"),
            )
        )
        session.commit()

    with Session(engine) as session:
        report = clean_expired_pix_payment_links(session, now=NOW)

    assert report["cleaned_links"] == 1
    assert report["backup_created"] is True
    backups = list((backup_runtime / "pix-link-cleanup-backups").glob("*.backup"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        original_extra = connection.execute("SELECT extra_json FROM accounts WHERE id = 1").fetchone()[0]
    assert "https://payments.example.test/file-backup" in original_extra


def test_stripe_embedded_payload_parser_returns_only_safe_state_fields():
    secret = "pi_secret_must_not_escape"
    result = parse_stripe_pix_instruction_html(
        _stripe_html(
            "succeeded",
            client_secret=secret,
            publishable_key="pk_live_must_not_escape",
        )
    )

    assert result.success is True
    assert result.intent_state == "succeeded"
    assert result.server_timestamp == NOW
    assert secret not in repr(result)
    assert "publishable_key" not in repr(result)


def test_direct_stripe_states_override_stale_local_records_and_fallback_on_failure(monkeypatch):
    engine = _engine()
    future_epoch = int(datetime(2026, 7, 17, 3, 0, tzinfo=timezone.utc).timestamp())
    expired_epoch = int(datetime(2026, 7, 16, 3, 0, tzinfo=timezone.utc).timestamp())
    urls = {
        name: f"https://payments.stripe.com/qr/instructions/{name}"
        for name in ("paid", "cancelled", "valid", "expired", "fallback")
    }
    with Session(engine) as session:
        stale_failed = _pix_link(urls["paid"], expires_at=expired_epoch)
        stale_failed["link_status"] = "failed"
        fallback_paid = _pix_link(urls["fallback"], expires_at=future_epoch)
        fallback_paid["link_status"] = "paid"
        session.add_all(
            [
                _account(101, stale_failed),
                _account(102, _pix_link(urls["cancelled"], expires_at=future_epoch)),
                _account(103, _pix_link(urls["valid"], expires_at=future_epoch)),
                _account(104, _pix_link(urls["expired"], expires_at=expired_epoch)),
                _account(105, fallback_paid),
            ]
        )
        session.commit()

    states = {
        urls["paid"]: pix_cleanup.StripePixDirectState(True, True, "succeeded", NOW),
        urls["cancelled"]: pix_cleanup.StripePixDirectState(True, True, "canceled", NOW),
        urls["valid"]: pix_cleanup.StripePixDirectState(True, True, "requires_action", NOW),
        urls["expired"]: pix_cleanup.StripePixDirectState(True, True, "requires_action", NOW),
        urls["fallback"]: pix_cleanup.StripePixDirectState(True, False),
    }
    monkeypatch.setattr(pix_cleanup, "_fetch_stripe_pix_instruction", lambda url: states[url])

    with Session(engine) as session:
        scan = preview_pix_payment_link_cleanup(session, now=NOW)

    assert scan["current_pix_links"] == 5
    assert scan["paid_links"] == 2
    assert scan["cancelled_links"] == 1
    assert scan["valid_links"] == 1
    assert scan["unknown_links"] == 0
    assert scan["expired_links"] == 1
    assert scan["direct_scan_attempted_links"] == 5
    assert scan["direct_scan_success_links"] == 4
    assert scan["direct_scan_fallback_links"] == 1
    assert scan["direct_scan_state_counts"] == {
        "canceled": 1,
        "requires_action": 2,
        "succeeded": 1,
    }


def test_non_stripe_url_never_triggers_a_network_request(monkeypatch):
    engine = _engine()
    with Session(engine) as session:
        session.add(
            _account(
                201,
                _pix_link(
                    "https://payments.example.test/qr/instructions/not-stripe",
                    generated_at="2026-07-16T03:30:00+00:00",
                ),
            )
        )
        session.commit()

    def must_not_fetch(_url):
        raise AssertionError("non-Stripe URL reached the network probe")

    monkeypatch.setattr(pix_cleanup, "_fetch_stripe_pix_instruction", must_not_fetch)
    with Session(engine) as session:
        scan = preview_pix_payment_link_cleanup(session, now=NOW)

    assert scan["direct_scan_attempted_links"] == 0
    assert scan["direct_scan_success_links"] == 0
    assert scan["direct_scan_fallback_links"] == 1
    assert scan["valid_links"] == 1


def test_invalid_stripe_response_and_cross_host_redirect_do_not_expose_secrets():
    secret = "pi_secret_response_must_not_escape"

    class FakeResponse:
        def __init__(self, status_code, headers, body=b""):
            self.status_code = status_code
            self.headers = headers
            self._body = body

        def iter_content(self, chunk_size):
            assert chunk_size > 0
            yield self._body

        def close(self):
            return None

    calls = []

    def redirect_get(url, **_kwargs):
        calls.append(url)
        return FakeResponse(302, {"Location": f"https://example.test/{secret}"})

    result = pix_cleanup._fetch_stripe_pix_instruction(
        "https://payments.stripe.com/qr/instructions/redirect",
        http_get=redirect_get,
    )
    assert result.success is False
    assert len(calls) == 1
    assert secret not in repr(result)

    invalid = parse_stripe_pix_instruction_html(
        f'<meta id="payload" data-message="not-base64-{secret}">'.encode()
    )
    assert invalid.success is False
    assert secret not in repr(invalid)


def test_cleanup_does_not_probe_after_begin_immediate(monkeypatch):
    engine = _engine()
    url = "https://payments.stripe.com/qr/instructions/transaction-boundary"
    with Session(engine) as session:
        session.add(_account(301, _pix_link(url, generated_at="2026-07-15T01:00:00+00:00")))
        session.commit()

    with Session(engine) as session:
        transaction_locked = False
        original_exec = session.exec

        def recording_exec(statement, *args, **kwargs):
            nonlocal transaction_locked
            if str(statement).strip().upper().startswith("BEGIN IMMEDIATE"):
                transaction_locked = True
            return original_exec(statement, *args, **kwargs)

        def direct_state(_url):
            assert transaction_locked is False
            return pix_cleanup.StripePixDirectState(True, True, "requires_action", NOW)

        monkeypatch.setattr(session, "exec", recording_exec)
        monkeypatch.setattr(pix_cleanup, "_fetch_stripe_pix_instruction", direct_state)
        report = clean_expired_pix_payment_links(session, now=NOW)

    assert transaction_locked is True
    assert report["cleaned_links"] == 1


def test_cleanup_skips_an_account_whose_current_url_changed_after_direct_scan(monkeypatch):
    engine = _engine()
    original_url = "https://payments.stripe.com/qr/instructions/original-paid"
    replacement_url = "https://payments.example.test/replacement-paid"
    original_link = _pix_link(original_url, generated_at="2026-07-16T03:30:00+00:00")
    with Session(engine) as session:
        session.add(_account(401, original_link))
        session.commit()

    monkeypatch.setattr(
        pix_cleanup,
        "_fetch_stripe_pix_instruction",
        lambda _url: pix_cleanup.StripePixDirectState(True, True, "succeeded", NOW),
    )

    def replace_before_lock(session, *, now):
        del now
        account = session.get(AccountModel, 401)
        replacement = _pix_link(replacement_url, generated_at="2026-07-16T03:31:00+00:00")
        replacement["link_status"] = "paid"
        extra = account.get_extra()
        extra["chatgpt_last_payment_link"] = replacement
        account.set_extra(extra)
        account.cashier_url = replacement_url
        session.add(account)
        session.commit()
        return ""

    monkeypatch.setattr(pix_cleanup, "_create_verified_backup", replace_before_lock)
    with Session(engine) as session:
        report = clean_pix_payment_links(session, cleanup_mode=PIX_CLEANUP_MODE_PAID, now=NOW)

    assert report["cleaned_links"] == 0
    assert report["concurrent_skipped_links"] == 1
    with Session(engine) as session:
        current = session.get(AccountModel, 401).get_extra()["chatgpt_last_payment_link"]
    assert current["url"] == replacement_url
