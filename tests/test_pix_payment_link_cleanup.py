from __future__ import annotations

from datetime import datetime, timezone
import sqlite3

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from core.db import AccountListStateModel, AccountModel, PaymentLinkGenerationModel
from services.chatgpt_core.pix_payment_link_cleanup import (
    PIX_EXPIRED_CLEANED_STATUS,
    clean_expired_pix_payment_links,
    pix_schedule_expires_at,
    preview_expired_pix_payment_links,
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


def _account(account_id: int, link: dict, *, cashier_url: str | None = None, status: str = "registered") -> AccountModel:
    account = AccountModel(
        id=account_id,
        platform="chatgpt",
        email=f"pix-cleanup-{account_id}@example.test",
        password="pw",
        status=status,
        cashier_url=link.get("url", "") if cashier_url is None else cashier_url,
    )
    account.set_extra({"keep": {"account_id": account_id}, "chatgpt_last_payment_link": link})
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
        "expired_links": 3,
        "active_links": 3,
        "provider_expiry_links": 3,
        "derived_expiry_links": 3,
        "missing_expiry_links": 1,
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
