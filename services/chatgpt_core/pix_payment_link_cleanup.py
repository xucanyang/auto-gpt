"""Preview and atomically clean expired current PIX payment links."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as datetime_time, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlmodel import Session

from services.account_filters import upsert_account_list_state_for_account_ids
from services.chatgpt_core.payment_link_cache import (
    PIX_EXPIRED_CLEANED_STATUS,
    normalize_payment_link_expires_at,
)


PIX_PAYMENT_TIMEZONE = ZoneInfo("Asia/Shanghai")
PIX_DAILY_EXPIRY_TIME = datetime_time(hour=11)
_CURRENT_LINK_URL_FIELDS = (
    "url",
    "paypal_url",
    "provider_redirect_url",
    "approval_url",
    "checkout_url",
    "cashier_url",
)
_LINK_URL_FIELDS_TO_REMOVE = frozenset({
    *_CURRENT_LINK_URL_FIELDS,
    "long_url",
    "stripe_redirect_url",
    "stripe_hosted_url",
    "chatgpt_checkout_url",
})
_BACKUP_MIN_FREE_MARGIN_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class PixLinkCandidate:
    account_id: int
    cashier_url: str
    current_url: str
    payload: dict[str, Any]
    generated_at: datetime | None
    expires_at: datetime | None
    expiry_source: str


def _utc_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        try:
            parsed = datetime.fromtimestamp(timestamp, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    else:
        raw = str(value or "").strip()
        if not raw:
            return None
        if raw.isdigit():
            return _utc_datetime(int(raw))
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def pix_schedule_expires_at(generated_at: Any) -> datetime | None:
    """Derive the PIX deadline from the Beijing 11:00 daily rollover."""

    generated_utc = _utc_datetime(generated_at)
    if generated_utc is None:
        return None
    generated_beijing = generated_utc.astimezone(PIX_PAYMENT_TIMEZONE)
    expiry_date = generated_beijing.date()
    if generated_beijing.timetz().replace(tzinfo=None) >= PIX_DAILY_EXPIRY_TIME:
        expiry_date += timedelta(days=1)
    expires_beijing = datetime.combine(
        expiry_date,
        PIX_DAILY_EXPIRY_TIME,
        tzinfo=PIX_PAYMENT_TIMEZONE,
    )
    return expires_beijing.astimezone(timezone.utc)


def pix_effective_expires_at(payload: dict[str, Any] | None) -> tuple[datetime | None, str]:
    """Prefer Stripe's deadline, falling back to the Beijing rollover rule."""

    if not isinstance(payload, dict):
        return None, "missing"
    provider_epoch = normalize_payment_link_expires_at(payload.get("link_expires_at"))
    if provider_epoch is not None:
        return datetime.fromtimestamp(provider_epoch, timezone.utc), "provider"
    derived = pix_schedule_expires_at(payload.get("generated_at") or payload.get("created_at"))
    return (derived, "beijing_11") if derived is not None else (None, "missing")


def latest_pix_expiry_cutoff(now: datetime | None = None) -> datetime:
    now_utc = _utc_datetime(now or datetime.now(timezone.utc)) or datetime.now(timezone.utc)
    now_beijing = now_utc.astimezone(PIX_PAYMENT_TIMEZONE)
    cutoff_date = now_beijing.date()
    if now_beijing.timetz().replace(tzinfo=None) < PIX_DAILY_EXPIRY_TIME:
        cutoff_date -= timedelta(days=1)
    return datetime.combine(
        cutoff_date,
        PIX_DAILY_EXPIRY_TIME,
        tzinfo=PIX_PAYMENT_TIMEZONE,
    ).astimezone(timezone.utc)


def _current_payment_link_url(payload: dict[str, Any]) -> str:
    for key in _CURRENT_LINK_URL_FIELDS:
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return ""


def _load_current_pix_link_candidates(session: Session) -> list[PixLinkCandidate]:
    rows = session.exec(
        text(
            """
            WITH account_json AS (
                SELECT
                    id,
                    cashier_url,
                    CASE WHEN json_valid(extra_json) THEN extra_json ELSE '{}' END AS extra
                FROM accounts
                WHERE platform = 'chatgpt'
            )
            SELECT
                id,
                cashier_url,
                json_extract(extra, '$.chatgpt_last_payment_link') AS link_json
            FROM account_json
            WHERE json_type(extra, '$.chatgpt_last_payment_link') = 'object'
              AND (
                    lower(trim(coalesce(json_extract(extra, '$.chatgpt_last_payment_link.link_type'), ''))) = 'pix'
                 OR lower(trim(coalesce(json_extract(extra, '$.chatgpt_last_payment_link.payment_method_type'), ''))) = 'pix'
              )
            """
        )
    ).mappings().all()
    candidates: list[PixLinkCandidate] = []
    for row in rows:
        try:
            payload = json.loads(str(row.get("link_json") or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        current_url = _current_payment_link_url(payload)
        if not current_url:
            continue
        generated_at = _utc_datetime(payload.get("generated_at") or payload.get("created_at"))
        expires_at, expiry_source = pix_effective_expires_at(payload)
        candidates.append(
            PixLinkCandidate(
                account_id=int(row.get("id") or 0),
                cashier_url=str(row.get("cashier_url") or "").strip(),
                current_url=current_url,
                payload=payload,
                generated_at=generated_at,
                expires_at=expires_at,
                expiry_source=expiry_source,
            )
        )
    return candidates


def _base_report(candidates: list[PixLinkCandidate], *, now: datetime) -> tuple[dict[str, Any], list[PixLinkCandidate]]:
    now_utc = _utc_datetime(now) or datetime.now(timezone.utc)
    cutoff_utc = latest_pix_expiry_cutoff(now_utc)
    expired = [item for item in candidates if item.expires_at is not None and item.expires_at <= now_utc]
    missing = [item for item in candidates if item.expires_at is None]
    provider_count = sum(item.expiry_source == "provider" for item in candidates)
    derived_count = sum(item.expiry_source == "beijing_11" for item in candidates)
    cutoff_beijing = cutoff_utc.astimezone(PIX_PAYMENT_TIMEZONE)
    report = {
        "instance_id": str(os.getenv("APP_INSTANCE_ID") or "auto-gpt").strip() or "auto-gpt",
        "timezone": "Asia/Shanghai",
        "now": now_utc.isoformat(),
        "cutoff_at": cutoff_utc.isoformat(),
        "cutoff_at_beijing": cutoff_beijing.isoformat(),
        "cutoff_display": cutoff_beijing.strftime("%Y-%m-%d %H:%M"),
        "current_pix_links": len(candidates),
        "expired_links": len(expired),
        "active_links": len(candidates) - len(expired) - len(missing),
        "provider_expiry_links": provider_count,
        "derived_expiry_links": derived_count,
        "missing_expiry_links": len(missing),
    }
    return report, expired


def _assert_integrity(connection: sqlite3.Connection, *, label: str) -> None:
    result = [str(row[0] or "").strip().lower() for row in connection.execute("PRAGMA integrity_check")]
    if result != ["ok"]:
        raise RuntimeError(f"SQLite integrity_check failed for {label}: {result[:3]}")


def _assert_session_integrity(session: Session) -> None:
    rows = session.exec(text("PRAGMA integrity_check")).all()
    result: list[str] = []
    for row in rows:
        try:
            value = row[0]
        except (IndexError, KeyError, TypeError):
            value = row
        result.append(str(value or "").strip().lower())
    if result != ["ok"]:
        raise RuntimeError(f"SQLite integrity_check failed after PIX cleanup: {result[:3]}")


def _session_database_path(session: Session) -> Path | None:
    bind = session.get_bind()
    if str(getattr(bind.dialect, "name", "")).lower() != "sqlite":
        return None
    database = str(getattr(bind.url, "database", "") or "").strip()
    if not database or database == ":memory:":
        return None
    path = Path(database).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def _create_verified_backup(session: Session, *, now: datetime) -> str:
    database = _session_database_path(session)
    if database is None:
        return ""
    if not database.is_file() or database.stat().st_size <= 0:
        raise RuntimeError(f"SQLite database is missing or empty: {database}")

    runtime_dir = Path(os.getenv("APP_RUNTIME_DIR") or database.parent).expanduser().resolve()
    backup_dir = runtime_dir / "pix-link-cleanup-backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    required_bytes = database.stat().st_size + _BACKUP_MIN_FREE_MARGIN_BYTES
    if shutil.disk_usage(backup_dir).free < required_bytes:
        raise RuntimeError("Insufficient disk space for verified PIX cleanup backup")

    timestamp = now.strftime("%Y%m%dT%H%M%S%fZ")
    backup = backup_dir / f"{database.stem}.before-pix-link-cleanup.{timestamp}.{os.getpid()}.backup"
    source = sqlite3.connect(str(database), timeout=30)
    destination = sqlite3.connect(str(backup), timeout=30)
    try:
        source.execute("PRAGMA busy_timeout=30000")
        _assert_integrity(source, label=str(database))
        source.backup(destination, pages=2048, sleep=0.05)
        destination.commit()
        _assert_integrity(destination, label=str(backup))
    except Exception:
        destination.close()
        source.close()
        backup.unlink(missing_ok=True)
        raise
    destination.close()
    source.close()
    backup.chmod(0o600)
    return str(backup)


def preview_expired_pix_payment_links(
    session: Session,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    now_utc = _utc_datetime(now or datetime.now(timezone.utc)) or datetime.now(timezone.utc)
    report, _ = _base_report(_load_current_pix_link_candidates(session), now=now_utc)
    return report


def _cleaned_link_payload(candidate: PixLinkCandidate, *, cleaned_at: datetime) -> dict[str, Any]:
    payload = {
        key: value
        for key, value in candidate.payload.items()
        if key not in _LINK_URL_FIELDS_TO_REMOVE
    }
    previous_status = str(candidate.payload.get("link_status") or "").strip()
    if previous_status and previous_status != PIX_EXPIRED_CLEANED_STATUS:
        payload["previous_link_status"] = previous_status
    cutoff_at = latest_pix_expiry_cutoff(cleaned_at)
    cleanup_through_at = max(
        value
        for value in (cutoff_at, candidate.generated_at)
        if value is not None
    )
    payload.update(
        {
            "link_status": PIX_EXPIRED_CLEANED_STATUS,
            "link_status_reason": "PIX payment link expired and was cleared",
            "link_status_updated_at": cleaned_at.isoformat(),
            "expired_at": candidate.expires_at.isoformat() if candidate.expires_at is not None else "",
            "cleaned_at": cleaned_at.isoformat(),
            "pix_cleanup_through_at": cleanup_through_at.isoformat(),
            "link_expiry_source": candidate.expiry_source,
        }
    )
    if candidate.expires_at is not None:
        payload["link_expires_at"] = int(candidate.expires_at.timestamp())
    return payload


def clean_expired_pix_payment_links(
    session: Session,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Clean expired current links and their exact cashier mirror in one transaction."""

    now_utc = _utc_datetime(now or datetime.now(timezone.utc)) or datetime.now(timezone.utc)
    initial_report, initial_expired = _base_report(_load_current_pix_link_candidates(session), now=now_utc)
    session.rollback()
    if not initial_expired:
        initial_report.update(
            {
                "cleaned_links": 0,
                "concurrent_skipped_links": 0,
                "list_state_refreshed": 0,
                "backup_created": False,
            }
        )
        return initial_report

    backup_path = _create_verified_backup(session, now=now_utc)
    try:
        session.exec(text("BEGIN IMMEDIATE"))
        report, expired = _base_report(_load_current_pix_link_candidates(session), now=now_utc)
        cleaned_ids: list[int] = []
        for candidate in expired:
            marker_json = json.dumps(
                _cleaned_link_payload(candidate, cleaned_at=now_utc),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            result = session.exec(
                text(
                    """
                    UPDATE accounts
                    SET
                        extra_json = json_set(
                            extra_json,
                            '$.chatgpt_last_payment_link',
                            json(:marker_json)
                        ),
                        cashier_url = CASE WHEN cashier_url = :current_url THEN '' ELSE cashier_url END,
                        updated_at = :updated_at
                    WHERE id = :account_id
                      AND platform = 'chatgpt'
                      AND json_valid(extra_json)
                      AND coalesce(
                            nullif(trim(json_extract(extra_json, '$.chatgpt_last_payment_link.url')), ''),
                            nullif(trim(json_extract(extra_json, '$.chatgpt_last_payment_link.paypal_url')), ''),
                            nullif(trim(json_extract(extra_json, '$.chatgpt_last_payment_link.provider_redirect_url')), ''),
                            nullif(trim(json_extract(extra_json, '$.chatgpt_last_payment_link.approval_url')), ''),
                            nullif(trim(json_extract(extra_json, '$.chatgpt_last_payment_link.checkout_url')), ''),
                            nullif(trim(json_extract(extra_json, '$.chatgpt_last_payment_link.cashier_url')), ''),
                            ''
                          ) = :current_url
                    """
                ),
                params={
                    "marker_json": marker_json,
                    "current_url": candidate.current_url,
                    "updated_at": now_utc.isoformat(),
                    "account_id": candidate.account_id,
                },
            )
            if int(result.rowcount or 0) == 1:
                cleaned_ids.append(candidate.account_id)

        list_state_refreshed = 0
        if cleaned_ids:
            list_state_refreshed = upsert_account_list_state_for_account_ids(
                session,
                cleaned_ids,
                commit=False,
            )
            if list_state_refreshed != len(cleaned_ids):
                raise RuntimeError("PIX payment-link list state did not refresh completely")
        session.commit()
        _assert_session_integrity(session)
        session.rollback()
    except Exception:
        session.rollback()
        raise

    report.update(
        {
            "cleaned_links": len(cleaned_ids),
            "concurrent_skipped_links": len(expired) - len(cleaned_ids),
            "list_state_refreshed": list_state_refreshed,
            "backup_created": bool(backup_path),
        }
    )
    return report
