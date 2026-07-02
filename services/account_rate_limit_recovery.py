"""Automatic recovery for temporarily rate-limited accounts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import threading
from typing import Any, Iterable

from sqlmodel import Session, select

from core import db as core_db
from core.db import AccountModel


RATE_LIMITED_STATUS = "rate_limited"
DEFAULT_RECOVERED_STATUS = "registered"
RATE_LIMIT_RECOVERY_SECONDS = 3600
LOOP_INTERVAL_SECONDS = 60

RATE_LIMIT_STARTED_AT_KEY = "rate_limit_started_at"
RATE_LIMIT_RECOVER_AT_KEY = "rate_limit_recover_at"
RATE_LIMIT_PREVIOUS_STATUS_KEY = "rate_limit_previous_status"
RATE_LIMIT_LAST_STARTED_AT_KEY = "rate_limit_last_started_at"
RATE_LIMIT_LAST_RECOVER_AT_KEY = "rate_limit_last_recover_at"
RATE_LIMIT_RECOVERED_AT_KEY = "rate_limit_recovered_at"
RATE_LIMIT_LAST_PREVIOUS_STATUS_KEY = "rate_limit_last_previous_status"

_state_lock = threading.Lock()
_stop_event = threading.Event()
_worker_thread: threading.Thread | None = None
_running = False


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _safe_str(value: Any) -> str:
    return str(value or "").strip()


def _lower_status(value: Any) -> str:
    return _safe_str(value).lower()


def _iso(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = _safe_str(value)
        if not text:
            return None
        try:
            numeric = float(text)
        except ValueError:
            numeric = None
        if numeric is not None:
            if numeric <= 0:
                return None
            seconds = numeric / 1000 if numeric > 1_000_000_000_000 else numeric
            return datetime.fromtimestamp(seconds, timezone.utc)
        iso_text = text[:-1] + "+00:00" if text.endswith("Z") else text
        try:
            parsed = datetime.fromisoformat(iso_text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _extra(account: AccountModel) -> dict[str, Any]:
    try:
        extra = account.get_extra()
    except Exception:
        return {}
    return extra if isinstance(extra, dict) else {}


def _recover_status(value: Any, fallback: str = DEFAULT_RECOVERED_STATUS) -> str:
    status = _lower_status(value)
    if not status or status == RATE_LIMITED_STATUS:
        return fallback
    return status


def mark_account_rate_limited(
    account: AccountModel,
    *,
    now: datetime | None = None,
    previous_status: Any = None,
) -> bool:
    """Put an account into the temporary rate-limited state for one hour."""

    current_status = _lower_status(getattr(account, "status", ""))
    if current_status == RATE_LIMITED_STATUS:
        extra = _extra(account)
        previous = _recover_status(extra.get(RATE_LIMIT_PREVIOUS_STATUS_KEY), DEFAULT_RECOVERED_STATUS)
    else:
        previous = _recover_status(previous_status if previous_status is not None else current_status)

    now = now or _utcnow()
    recover_at = now + timedelta(seconds=RATE_LIMIT_RECOVERY_SECONDS)
    extra = _extra(account)
    extra[RATE_LIMIT_STARTED_AT_KEY] = _iso(now)
    extra[RATE_LIMIT_RECOVER_AT_KEY] = _iso(recover_at)
    extra[RATE_LIMIT_PREVIOUS_STATUS_KEY] = previous
    extra.pop(RATE_LIMIT_RECOVERED_AT_KEY, None)

    account.status = RATE_LIMITED_STATUS
    account.set_extra(extra)
    account.updated_at = now
    return True


def clear_account_rate_limit(account: AccountModel, *, now: datetime | None = None) -> bool:
    """Clear active rate-limit metadata when an operator changes status manually."""

    extra = _extra(account)
    active_keys = {
        RATE_LIMIT_STARTED_AT_KEY,
        RATE_LIMIT_RECOVER_AT_KEY,
        RATE_LIMIT_PREVIOUS_STATUS_KEY,
    }
    if not any(key in extra for key in active_keys):
        return False

    now = now or _utcnow()
    started_at = _parse_datetime(extra.get(RATE_LIMIT_STARTED_AT_KEY))
    recover_at = _parse_datetime(extra.get(RATE_LIMIT_RECOVER_AT_KEY))
    previous = _recover_status(extra.get(RATE_LIMIT_PREVIOUS_STATUS_KEY), DEFAULT_RECOVERED_STATUS)
    if started_at:
        extra[RATE_LIMIT_LAST_STARTED_AT_KEY] = _iso(started_at)
    if recover_at:
        extra[RATE_LIMIT_LAST_RECOVER_AT_KEY] = _iso(recover_at)
    extra[RATE_LIMIT_LAST_PREVIOUS_STATUS_KEY] = previous
    for key in active_keys:
        extra.pop(key, None)
    extra[RATE_LIMIT_RECOVERED_AT_KEY] = _iso(now)

    account.set_extra(extra)
    account.updated_at = now
    return True


def reconcile_account_rate_limit(account: AccountModel, *, now: datetime | None = None) -> bool:
    """Ensure active rate-limit metadata exists, or recover the account if due."""

    if _lower_status(getattr(account, "status", "")) != RATE_LIMITED_STATUS:
        return False

    now = now or _utcnow()
    extra = _extra(account)
    previous = _recover_status(extra.get(RATE_LIMIT_PREVIOUS_STATUS_KEY), DEFAULT_RECOVERED_STATUS)
    started_at = (
        _parse_datetime(extra.get(RATE_LIMIT_STARTED_AT_KEY))
        or _parse_datetime(getattr(account, "updated_at", None))
        or now
    )
    recover_at = _parse_datetime(extra.get(RATE_LIMIT_RECOVER_AT_KEY)) or (
        started_at + timedelta(seconds=RATE_LIMIT_RECOVERY_SECONDS)
    )

    if recover_at <= now:
        account.status = previous
        extra[RATE_LIMIT_LAST_STARTED_AT_KEY] = _iso(started_at)
        extra[RATE_LIMIT_LAST_RECOVER_AT_KEY] = _iso(recover_at)
        extra[RATE_LIMIT_LAST_PREVIOUS_STATUS_KEY] = previous
        extra[RATE_LIMIT_RECOVERED_AT_KEY] = _iso(now)
        extra.pop(RATE_LIMIT_STARTED_AT_KEY, None)
        extra.pop(RATE_LIMIT_RECOVER_AT_KEY, None)
        extra.pop(RATE_LIMIT_PREVIOUS_STATUS_KEY, None)
        account.set_extra(extra)
        account.updated_at = now
        return True

    changed = False
    normalized_started_at = _iso(started_at)
    normalized_recover_at = _iso(recover_at)
    if extra.get(RATE_LIMIT_STARTED_AT_KEY) != normalized_started_at:
        extra[RATE_LIMIT_STARTED_AT_KEY] = normalized_started_at
        changed = True
    if extra.get(RATE_LIMIT_RECOVER_AT_KEY) != normalized_recover_at:
        extra[RATE_LIMIT_RECOVER_AT_KEY] = normalized_recover_at
        changed = True
    if extra.get(RATE_LIMIT_PREVIOUS_STATUS_KEY) != previous:
        extra[RATE_LIMIT_PREVIOUS_STATUS_KEY] = previous
        changed = True
    if changed:
        account.set_extra(extra)
        account.updated_at = now
    return changed


def reconcile_rate_limited_accounts(
    session: Session,
    *,
    platform: str | None = None,
    account_ids: Iterable[int] | None = None,
    accounts: Iterable[AccountModel] | None = None,
    now: datetime | None = None,
    commit: bool = True,
) -> int:
    """Recover due rate-limited accounts and backfill missing recovery metadata."""

    if accounts is None:
        query = select(AccountModel).where(AccountModel.status == RATE_LIMITED_STATUS)
        platform_value = _safe_str(platform)
        if platform_value:
            query = query.where(AccountModel.platform == platform_value)
        ids = [int(value) for value in (account_ids or []) if int(value or 0) > 0]
        if ids:
            query = query.where(AccountModel.id.in_(ids))
        items = session.exec(query).all()
    else:
        items = list(accounts)

    changed = 0
    now = now or _utcnow()
    for account in items:
        if reconcile_account_rate_limit(account, now=now):
            session.add(account)
            changed += 1
    if changed and commit:
        session.commit()
    return changed


def account_rate_limit_payload(
    account: AccountModel,
    *,
    now: datetime | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    extra = extra if isinstance(extra, dict) else _extra(account)
    recover_at = _parse_datetime(extra.get(RATE_LIMIT_RECOVER_AT_KEY))
    started_at = _parse_datetime(extra.get(RATE_LIMIT_STARTED_AT_KEY))
    now = now or _utcnow()
    seconds_remaining = 0
    if recover_at:
        seconds_remaining = max(0, int((recover_at - now).total_seconds()))
    return {
        "started_at": _iso(started_at),
        "recover_at": _iso(recover_at),
        "previous_status": _recover_status(extra.get(RATE_LIMIT_PREVIOUS_STATUS_KEY), ""),
        "seconds_remaining": seconds_remaining,
    }


def _loop() -> None:
    global _running

    while not _stop_event.is_set():
        try:
            with Session(core_db.engine) as session:
                changed = reconcile_rate_limited_accounts(session)
            if changed:
                print(f"[AccountRateLimitRecovery] 已恢复/修正 {changed} 个限流账号")
        except Exception as exc:
            print(f"[AccountRateLimitRecovery] 调度错误: {exc}")
        _stop_event.wait(LOOP_INTERVAL_SECONDS)

    with _state_lock:
        _running = False


def start() -> None:
    global _worker_thread, _running

    with _state_lock:
        if _running:
            return
        _running = True
    _stop_event.clear()
    _worker_thread = threading.Thread(target=_loop, daemon=True, name="account-rate-limit-recovery")
    _worker_thread.start()
    print("[AccountRateLimitRecovery] 已启动")


def stop() -> None:
    global _worker_thread

    _stop_event.set()
    thread = _worker_thread
    if thread and thread.is_alive():
        thread.join(timeout=5)
    _worker_thread = None
