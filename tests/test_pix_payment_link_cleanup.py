from __future__ import annotations

from datetime import datetime, timezone
import sqlite3

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from core.db import AccountListStateModel, AccountModel, PaymentLinkGenerationModel
from services.chatgpt_core.pix_payment_link_cleanup import (
    PIX_CLEANUP_MODE_CANCELLED,
    PIX_CLEANUP_MODE_PAID,
    PIX_EXPIRED_CLEANED_STATUS,
    clean_pix_payment_links,
    clean_expired_pix_payment_links,
    pix_schedule_expires_at,
    preview_pix_payment_link_cleanup,
    preview_expired_pix_payment_links,
)
from services.chatgpt_core.payment_link_cache import (
    PIX_CANCELLED_CLEANED_STATUS,
    PIX_PAID_CLEANED_STATUS,
)


NOW = datetime(2026, 7, 16, 4, 0, tzinfo=timezone.utc)


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
    assert scan["valid_links"] == 2
    assert scan["paid_links"] == 1
    assert scan["expired_links"] == 1
    assert scan["cancelled_links"] == 1
    assert scan["valid_missing_expiry_links"] == 1
    assert sum(scan[key] for key in ("valid_links", "paid_links", "expired_links", "cancelled_links")) == 5

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
