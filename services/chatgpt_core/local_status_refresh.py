"""Local ChatGPT account status refresh helpers.

Used after external payment/card-code systems report success so the local account
record is refreshed from ChatGPT instead of only writing an external paid marker.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import copy
import hashlib
import json
import logging
import threading
import time
from types import SimpleNamespace
from typing import Any

from sqlmodel import Session, select

from core import db as core_db
from core.db import (
    AccountListStateModel,
    AccountModel,
    ChatGPTLocalStatusRefreshJobModel,
)
from services.chatgpt_account_state import (
    AUTH_INVALID_STATES,
    AUTH_VALID_STATES,
    apply_chatgpt_status_policy,
    classify_chatgpt_capabilities,
    is_paid_subscription_plan,
    normalize_subscription_plan,
)
from services.chatgpt_core.status_probe import probe_local_chatgpt_status

logger = logging.getLogger(__name__)

_LOCAL_STATUS_REFRESH_LOCK = threading.Lock()
_LOCAL_STATUS_REFRESH_IN_FLIGHT: set[int] = set()
_LOCAL_STATUS_REFRESH_PENDING: dict[int, dict[str, Any]] = {}
_SUBSCRIPTION_RETRY_DELAY_SECONDS = 3.0
_AUTH_MATERIAL_PROBE_MAX_ATTEMPTS = 2
_LOCAL_STATUS_AUTO_MAX_ATTEMPTS = 3
_LOCAL_STATUS_AUTO_RETRY_DELAYS_SECONDS = (5.0, 20.0)
_LOCAL_STATUS_SUCCESS_DEDUPE_SECONDS = 90.0
_LOCAL_STATUS_STALE_RUNNING_SECONDS = 180.0
_LOCAL_STATUS_RECOVERY_INTERVAL_SECONDS = 15.0
_LOCAL_STATUS_REFRESH_META_KEY = "chatgpt_local_refresh"
_LOCAL_STATUS_LAST_CONFIRMED_SUBSCRIPTION_KEY = "chatgpt_last_confirmed_subscription"
_LOCAL_STATUS_CONCURRENCY_HARD_LIMIT = 10
_LOCAL_STATUS_CONCURRENCY_UPDATE_LOCK = threading.RLock()
_LOCAL_STATUS_CAPACITY_CONDITION = threading.Condition()
_LOCAL_STATUS_CAPACITY_ACTIVE = 0
_LOCAL_STATUS_CAPACITY_LIMIT = 1
_LOCAL_STATUS_CAPACITY_WAITERS: list[object] = []
_LOCAL_STATUS_IDENTITY_REGISTRY_LOCK = threading.Lock()
_LOCAL_STATUS_IDENTITY_GATES: dict[str, dict[str, Any]] = {}
_LOCAL_STATUS_RECOVERY_STOP_EVENT = threading.Event()
_LOCAL_STATUS_RECOVERY_THREAD: threading.Thread | None = None
_LOCAL_STATUS_RECOVERY_STATE_LOCK = threading.Lock()
_LOCAL_STATUS_RECOVERY_RUNNING = False


def _normalize_local_status_concurrency(value: Any) -> int:
    try:
        parsed = int(float(str(value).strip()))
    except (TypeError, ValueError):
        parsed = 1
    return max(1, min(parsed, _LOCAL_STATUS_CONCURRENCY_HARD_LIMIT))


@contextmanager
def local_status_concurrency_update_guard():
    """Serialize config persistence/readback with process-limiter updates."""

    with _LOCAL_STATUS_CONCURRENCY_UPDATE_LOCK:
        yield


def configure_local_status_concurrency(
    value: Any,
) -> int:
    """Set the process-wide probe budget used by manual and automatic refreshes."""

    global _LOCAL_STATUS_CAPACITY_LIMIT
    limit = _normalize_local_status_concurrency(value)
    with local_status_concurrency_update_guard():
        with _LOCAL_STATUS_CAPACITY_CONDITION:
            _LOCAL_STATUS_CAPACITY_LIMIT = limit
            _LOCAL_STATUS_CAPACITY_CONDITION.notify_all()
    return limit


def refresh_local_status_concurrency_from_store() -> int:
    """Refresh the limiter without allowing an older read to win a race."""

    with local_status_concurrency_update_guard():
        try:
            from core.config_store import config_store

            value = config_store.get("chatgpt_local_status_probe_concurrency", "1")
        except Exception:
            with _LOCAL_STATUS_CAPACITY_CONDITION:
                return _LOCAL_STATUS_CAPACITY_LIMIT
        return configure_local_status_concurrency(value)


def refresh_local_status_concurrency_from_config() -> int:
    """Compatibility alias for callers using the earlier helper name."""

    return refresh_local_status_concurrency_from_store()


def _configured_local_status_concurrency() -> int:
    """Compatibility wrapper for the internal config refresh call sites."""

    return refresh_local_status_concurrency_from_store()


@contextmanager
def local_status_capacity_slot(
    *,
    stop_check: Any = None,
):
    """Lease one process-wide local-status slot without holding a DB connection."""

    global _LOCAL_STATUS_CAPACITY_ACTIVE
    acquired = False
    waiter = object()
    with _LOCAL_STATUS_CAPACITY_CONDITION:
        _LOCAL_STATUS_CAPACITY_WAITERS.append(waiter)
        try:
            while (
                _LOCAL_STATUS_CAPACITY_WAITERS[0] is not waiter
                or _LOCAL_STATUS_CAPACITY_ACTIVE >= _LOCAL_STATUS_CAPACITY_LIMIT
            ):
                if callable(stop_check):
                    stop_check()
                _LOCAL_STATUS_CAPACITY_CONDITION.wait(timeout=0.25)
            if callable(stop_check):
                stop_check()
            _LOCAL_STATUS_CAPACITY_WAITERS.pop(0)
            _LOCAL_STATUS_CAPACITY_ACTIVE += 1
            acquired = True
            _LOCAL_STATUS_CAPACITY_CONDITION.notify_all()
        except BaseException:
            if waiter in _LOCAL_STATUS_CAPACITY_WAITERS:
                _LOCAL_STATUS_CAPACITY_WAITERS.remove(waiter)
                _LOCAL_STATUS_CAPACITY_CONDITION.notify_all()
            raise
    try:
        yield
    finally:
        if acquired:
            with _LOCAL_STATUS_CAPACITY_CONDITION:
                _LOCAL_STATUS_CAPACITY_ACTIVE = max(0, _LOCAL_STATUS_CAPACITY_ACTIVE - 1)
                _LOCAL_STATUS_CAPACITY_CONDITION.notify_all()


def _account_extra(account: Any) -> dict[str, Any]:
    if account is None:
        return {}
    if hasattr(account, "get_extra"):
        try:
            extra = account.get_extra()
            if isinstance(extra, dict):
                return extra
        except Exception:
            pass
    extra = getattr(account, "extra", {}) or {}
    return extra if isinstance(extra, dict) else {}


def _local_status_fingerprint_signature(account: Any) -> str:
    signature = ""
    try:
        from services.chatgpt_core.account_fingerprint import (
            fingerprint_signature,
            resolve_account_browser_fingerprint,
        )

        fingerprint = resolve_account_browser_fingerprint(_account_extra(account))
        if fingerprint:
            signature = fingerprint_signature(fingerprint, include_device=True)
    except Exception:
        signature = ""
    return signature


def _local_status_identity_keys(account: Any) -> list[str]:
    keys: set[str] = set()
    try:
        account_id = int(getattr(account, "id", 0) or 0)
    except (TypeError, ValueError):
        account_id = 0
    if account_id > 0:
        keys.add(f"account:{account_id}")

    signature = _local_status_fingerprint_signature(account)
    keys.add(f"fingerprint:{signature}" if signature else "fingerprint:legacy_default")
    return sorted(keys)


@contextmanager
def local_status_identity_slot(account: Any, *, stop_check: Any = None):
    """Serialize the same account or persisted fingerprint across all refresh entry points."""

    entries: list[tuple[str, dict[str, Any]]] = []
    with _LOCAL_STATUS_IDENTITY_REGISTRY_LOCK:
        for key in _local_status_identity_keys(account):
            entry = _LOCAL_STATUS_IDENTITY_GATES.get(key)
            if entry is None:
                entry = {"lock": threading.Lock(), "references": 0}
                _LOCAL_STATUS_IDENTITY_GATES[key] = entry
            entry["references"] = int(entry.get("references") or 0) + 1
            entries.append((key, entry))

    acquired: list[threading.Lock] = []
    try:
        for _key, entry in entries:
            gate = entry["lock"]
            while not gate.acquire(timeout=0.25):
                if callable(stop_check):
                    stop_check()
            acquired.append(gate)
        if callable(stop_check):
            stop_check()
        yield
    finally:
        for gate in reversed(acquired):
            gate.release()
        with _LOCAL_STATUS_IDENTITY_REGISTRY_LOCK:
            for key, entry in entries:
                entry["references"] = max(0, int(entry.get("references") or 0) - 1)
                if entry["references"] == 0:
                    _LOCAL_STATUS_IDENTITY_GATES.pop(key, None)


def account_has_local_status_auth_material(account: Any) -> bool:
    """Return whether a ChatGPT account has auth material worth local probing."""
    if account is None:
        return False
    platform = str(getattr(account, "platform", "") or "").strip().lower()
    if platform and platform != "chatgpt":
        return False
    extra = _account_extra(account)
    refresh_token = str(
        extra.get("refresh_token")
        or extra.get("refreshToken")
        or getattr(account, "refresh_token", "")
        or ""
    ).strip()
    access_token = str(
        extra.get("access_token")
        or extra.get("accessToken")
        or extra.get("webAccessToken")
        or getattr(account, "access_token", "")
        or getattr(account, "token", "")
        or ""
    ).strip()
    return bool(refresh_token or access_token)


def _auth_material_revision(account: Any) -> tuple[str, ...]:
    extra = _account_extra(account)
    return (
        str(
            extra.get("refresh_token")
            or extra.get("refreshToken")
            or getattr(account, "refresh_token", "")
            or ""
        ).strip(),
        str(
            extra.get("access_token")
            or extra.get("accessToken")
            or getattr(account, "access_token", "")
            or ""
        ).strip(),
        str(getattr(account, "token", "") or "").strip(),
        str(
            extra.get("client_id")
            or getattr(account, "client_id", "")
            or "app_EMoamEEZ73f0CkXaXp7hrann"
        ).strip(),
        str(
            extra.get("id_token")
            or getattr(account, "id_token", "")
            or ""
        ).strip(),
        str(getattr(account, "user_id", "") or "").strip(),
        str(
            extra.get("organization_id")
            or getattr(account, "organization_id", "")
            or ""
        ).strip(),
        str(extra.get("workspace_id") or getattr(account, "workspace_id", "") or "").strip(),
        str(
            extra.get("chatgpt_workspace_scope")
            or getattr(account, "workspace_scope", "")
            or ""
        ).strip(),
        _local_status_fingerprint_signature(account),
    )


def _auth_material_revision_hash(account: Any) -> str:
    """Return a non-secret identity for the credentials used by one probe."""

    payload = json.dumps(_auth_material_revision(account), ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _utc_timestamp() -> float:
    return datetime.now(timezone.utc).timestamp()


def _iso_timestamp(value: float | int | None = None) -> str:
    try:
        timestamp = float(value if value is not None else _utc_timestamp())
    except (TypeError, ValueError):
        timestamp = _utc_timestamp()
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def _refresh_meta(extra: dict[str, Any]) -> dict[str, Any]:
    value = extra.get(_LOCAL_STATUS_REFRESH_META_KEY)
    return dict(value) if isinstance(value, dict) else {}


def _set_refresh_meta(
    extra: dict[str, Any],
    *,
    state: str | None = None,
    reason: str | None = None,
    attempt_count: int | None = None,
    max_attempts: int | None = None,
    next_attempt_at: float | None = None,
    started_at: float | None = None,
    completed_at: float | None = None,
    last_outcome: str | None = None,
    last_error: str | None = None,
    canonical_preserved: bool | None = None,
    requested_at: float | None = None,
) -> dict[str, Any]:
    """Update only non-secret refresh metadata kept beside the account snapshot."""

    meta = _refresh_meta(extra)
    if state is not None:
        meta["state"] = str(state or "").strip().lower()
    if reason is not None:
        meta["reason"] = str(reason or "").strip()[:160]
    if attempt_count is not None:
        meta["attempt_count"] = max(0, int(attempt_count))
    if max_attempts is not None:
        meta["max_attempts"] = max(1, int(max_attempts))
    if next_attempt_at is not None:
        meta["next_attempt_at"] = _iso_timestamp(next_attempt_at) if next_attempt_at > 0 else ""
    if started_at is not None:
        meta["started_at"] = _iso_timestamp(started_at) if started_at > 0 else ""
    if completed_at is not None:
        meta["completed_at"] = _iso_timestamp(completed_at) if completed_at > 0 else ""
    if requested_at is not None:
        meta["requested_at"] = _iso_timestamp(requested_at) if requested_at > 0 else ""
    if last_outcome is not None:
        meta["last_outcome"] = str(last_outcome or "").strip().lower()
    if last_error is not None:
        meta["last_error"] = str(last_error or "").strip()[:500]
    if canonical_preserved is not None:
        meta["canonical_preserved"] = bool(canonical_preserved)
    meta["updated_at"] = _iso_timestamp()
    extra[_LOCAL_STATUS_REFRESH_META_KEY] = meta
    return meta


def _probe_auth_state(probe: dict[str, Any] | None) -> str:
    result = probe if isinstance(probe, dict) else {}
    auth = result.get("auth") if isinstance(result.get("auth"), dict) else {}
    return str(auth.get("state") or "").strip().lower()


def _probe_plan(probe: dict[str, Any] | None) -> str:
    result = probe if isinstance(probe, dict) else {}
    subscription = result.get("subscription") if isinstance(result.get("subscription"), dict) else {}
    return normalize_subscription_plan(subscription.get("plan"))


def _probe_refresh_outcome(probe: dict[str, Any] | None) -> str:
    """Classify one attempt without treating an incomplete read as canonical."""

    auth_state = _probe_auth_state(probe)
    if auth_state in AUTH_INVALID_STATES:
        return "auth_invalid"
    if auth_state == "probe_failed":
        return "probe_failed"
    if _probe_plan(probe) != "unknown" and auth_state not in AUTH_INVALID_STATES:
        return "confirmed"
    if auth_state in AUTH_VALID_STATES:
        return "unknown_plan"
    return "probe_incomplete"


def _probe_error_message(probe: dict[str, Any] | None) -> str:
    result = probe if isinstance(probe, dict) else {}
    for section_name in ("auth", "subscription", "codex"):
        section = result.get(section_name) if isinstance(result.get(section_name), dict) else {}
        for key in ("message", "error_code", "reason"):
            value = str(section.get(key) or "").strip()
            if value:
                return _safe_refresh_error(value)
    return "订阅状态探测未完成"


def _confirmed_subscription_snapshot(probe: dict[str, Any] | None) -> dict[str, Any]:
    """Keep only safe, non-auth fields from a confirmed subscription probe."""

    result = probe if isinstance(probe, dict) else {}
    subscription = result.get("subscription") if isinstance(result.get("subscription"), dict) else {}
    plan = normalize_subscription_plan(subscription.get("plan"))
    if plan == "unknown":
        return {}
    return {
        "plan": plan,
        "subscription_active_until": str(subscription.get("subscription_active_until") or "").strip(),
        "subscription_expires_at_iso": str(subscription.get("subscription_expires_at_iso") or "").strip(),
        "workspace_plan_type": str(subscription.get("workspace_plan_type") or "").strip(),
        "checked_at": str(subscription.get("checked_at") or "").strip(),
        "source": str(subscription.get("source") or "").strip(),
    }


def _remember_confirmed_subscription(extra: dict[str, Any], probe: dict[str, Any] | None) -> None:
    if _probe_refresh_outcome(probe) != "confirmed":
        return
    snapshot = _confirmed_subscription_snapshot(probe)
    if snapshot:
        extra[_LOCAL_STATUS_LAST_CONFIRMED_SUBSCRIPTION_KEY] = snapshot


def prepare_chatgpt_account_for_local_status_refresh(
    account: AccountModel,
    *,
    reason: str,
) -> dict[str, Any]:
    """Reset credential-bound probe evidence while retaining safe plan history.

    Auth/session replacement must not leave an old 401 as the current result.
    The old confirmed subscription is retained separately as historical evidence
    so a transient post-login probe cannot erase it.
    """

    extra = account.get_extra()
    if not isinstance(extra, dict):
        extra = {}
    previous_probe = extra.get("chatgpt_local") if isinstance(extra.get("chatgpt_local"), dict) else None
    _remember_confirmed_subscription(extra, previous_probe)
    extra.pop("chatgpt_local", None)
    extra["chatgpt_capabilities"] = classify_chatgpt_capabilities(account, local_probe={})
    _set_refresh_meta(
        extra,
        state="pending",
        reason=reason,
        attempt_count=0,
        max_attempts=_LOCAL_STATUS_AUTO_MAX_ATTEMPTS,
        next_attempt_at=_utc_timestamp(),
        started_at=0,
        completed_at=0,
        last_outcome="",
        last_error="",
        canonical_preserved=False,
        requested_at=_utc_timestamp(),
    )
    account.set_extra(extra)
    return extra


def _local_status_created_at_identity(value: Any) -> str:
    if isinstance(value, datetime):
        # SQLite persists DateTime wall-clock fields without the timezone offset.
        return value.replace(tzinfo=None).isoformat(sep=" ")
    return str(value or "").strip()


def _local_status_account_identity(account: Any) -> tuple[int, str, str, str]:
    try:
        account_id = int(getattr(account, "id", 0) or 0)
    except (TypeError, ValueError):
        account_id = 0
    return (
        account_id,
        str(getattr(account, "platform", "") or "").strip().lower(),
        str(getattr(account, "email", "") or "").strip().lower(),
        _local_status_created_at_identity(getattr(account, "created_at", None)),
    )


def _probe_account_identity(account: Any) -> tuple[int, str, str, str]:
    prepared = getattr(account, "local_status_identity", None)
    if isinstance(prepared, (tuple, list)) and len(prepared) == 4:
        try:
            account_id = int(prepared[0] or 0)
        except (TypeError, ValueError):
            account_id = 0
        return (
            account_id,
            str(prepared[1] or "").strip().lower(),
            str(prepared[2] or "").strip().lower(),
            _local_status_created_at_identity(prepared[3]),
        )
    return _local_status_account_identity(account)


def _require_probe_account_identity(
    account: Any,
    *,
    account_id: int,
) -> tuple[int, str, str, str]:
    identity = _probe_account_identity(account)
    snapshot_identity = _local_status_account_identity(account)
    if (
        identity != snapshot_identity
        or identity[0] != account_id
        or identity[1] != "chatgpt"
        or not identity[2]
        or not identity[3]
    ):
        raise ValueError(
            f"本地状态刷新快照缺少不可变账号身份 account_id={account_id}"
        )
    return identity


def _assert_local_status_account_identity(
    account: Any,
    expected_identity: tuple[int, str, str, str],
) -> None:
    if _local_status_account_identity(account) != expected_identity:
        raise LookupError(
            f"ChatGPT 账号在探测期间已被替换 account_id={expected_identity[0]}"
        )


def build_chatgpt_local_status_probe_account(account: AccountModel) -> SimpleNamespace:
    """Detach the account fields needed by remote probes from its SQLModel Session."""

    extra = _account_extra(account)
    identity = _local_status_account_identity(account)
    return SimpleNamespace(
        id=getattr(account, "id", None),
        platform=str(getattr(account, "platform", "chatgpt") or "chatgpt"),
        email=str(getattr(account, "email", "") or ""),
        created_at=getattr(account, "created_at", None),
        local_status_identity=identity,
        password=str(getattr(account, "password", "") or ""),
        user_id=str(getattr(account, "user_id", "") or ""),
        token=str(getattr(account, "token", "") or ""),
        status=str(getattr(account, "status", "") or ""),
        access_token=str(extra.get("access_token") or getattr(account, "token", "") or "").strip(),
        refresh_token=str(extra.get("refresh_token") or "").strip(),
        id_token=str(extra.get("id_token") or "").strip(),
        session_token=str(extra.get("session_token") or "").strip(),
        client_id=str(extra.get("client_id") or "app_EMoamEEZ73f0CkXaXp7hrann").strip(),
        cookies=str(extra.get("cookies") or "").strip(),
        workspace_id=str(extra.get("workspace_id") or "").strip(),
        extra=extra,
    )


def _build_probe_account(account: AccountModel) -> SimpleNamespace:
    """Compatibility alias for older tests and internal callers."""

    return build_chatgpt_local_status_probe_account(account)


def _subscription_retry_reason(probe: dict[str, Any] | None) -> str:
    """Return why one more probe can improve an otherwise valid subscription read."""
    result = probe if isinstance(probe, dict) else {}
    auth = result.get("auth") if isinstance(result.get("auth"), dict) else {}
    if str(auth.get("state") or "").strip().lower() not in AUTH_VALID_STATES:
        return ""

    subscription = result.get("subscription") if isinstance(result.get("subscription"), dict) else {}
    plan = normalize_subscription_plan(subscription.get("plan"))
    if plan == "unknown":
        return "subscription_plan_unknown"
    if is_paid_subscription_plan(plan) and not str(subscription.get("subscription_active_until") or "").strip():
        return "subscription_expiry_missing"
    return ""


def _subscription_probe_score(probe: dict[str, Any] | None) -> int:
    """Rank probes so a transient retry failure cannot replace useful evidence."""
    result = probe if isinstance(probe, dict) else {}
    auth = result.get("auth") if isinstance(result.get("auth"), dict) else {}
    if str(auth.get("state") or "").strip().lower() not in AUTH_VALID_STATES:
        return 0

    subscription = result.get("subscription") if isinstance(result.get("subscription"), dict) else {}
    plan = normalize_subscription_plan(subscription.get("plan"))
    if plan == "unknown":
        return 1
    score = 3
    if is_paid_subscription_plan(plan) and str(subscription.get("subscription_active_until") or "").strip():
        score += 1
    return score


def _probe_local_status_with_subscription_retry(
    probe_account: Any,
    *,
    proxy: str | None,
    use_default_proxy: bool,
) -> dict[str, Any]:
    """Probe once, then retry only a valid but incomplete subscription response."""
    first_probe = probe_local_chatgpt_status(
        probe_account,
        proxy=proxy,
        use_default_proxy=use_default_proxy,
    )
    retry_reason = _subscription_retry_reason(first_probe)
    if not retry_reason:
        return first_probe

    time.sleep(_SUBSCRIPTION_RETRY_DELAY_SECONDS)
    retry_probe = probe_local_chatgpt_status(
        probe_account,
        proxy=proxy,
        use_default_proxy=use_default_proxy,
    )
    selected_probe = retry_probe if _subscription_probe_score(retry_probe) >= _subscription_probe_score(first_probe) else first_probe
    subscription = selected_probe.setdefault("subscription", {})
    if not isinstance(subscription, dict):
        subscription = {}
        selected_probe["subscription"] = subscription
    remaining_reason = _subscription_retry_reason(selected_probe)
    subscription["refresh_attempts"] = 2
    subscription["retry_reason"] = retry_reason
    subscription["retry_outcome"] = "resolved" if not remaining_reason else "still_incomplete"
    return selected_probe


def probe_chatgpt_account_local_status(
    probe_account: Any,
    *,
    proxy: str | None = None,
    use_default_proxy: bool = True,
    candidate_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the remote probe against a detached account snapshot."""

    if not str(proxy or "").strip() and use_default_proxy:
        from services.chatgpt_core.local_status_proxy import (
            run_local_status_probe_with_candidates,
        )

        return run_local_status_probe_with_candidates(
            probe_account,
            {},
            _probe_local_status_with_subscription_retry,
            default_mode="global",
            candidate_state=candidate_state,
        )

    return _probe_local_status_with_subscription_retry(
        probe_account,
        proxy=proxy,
        use_default_proxy=use_default_proxy,
    )


def _reject_proxy_transport_probe(probe: dict[str, Any]) -> None:
    """Do not let infrastructure failures replace canonical account evidence."""

    from services.chatgpt_core.local_status_proxy import (
        local_status_probe_proxy_failure,
    )

    proxy_error = local_status_probe_proxy_failure(probe)
    if proxy_error:
        raise RuntimeError(proxy_error)


def _persist_chatgpt_local_status_probe(
    session: Session,
    account: AccountModel,
    probe: dict[str, Any],
) -> dict[str, Any]:
    latest_extra = account.get_extra()
    if not isinstance(latest_extra, dict):
        latest_extra = {}
    incoming_probe = copy.deepcopy(probe) if isinstance(probe, dict) else {}
    incoming_outcome = _probe_refresh_outcome(incoming_probe)
    previous_probe = latest_extra.get("chatgpt_local") if isinstance(latest_extra.get("chatgpt_local"), dict) else None
    previous_outcome = _probe_refresh_outcome(previous_probe)
    current_revision_hash = _auth_material_revision_hash(account)
    previous_meta = _refresh_meta(latest_extra)
    previous_revision_hash = str(previous_meta.get("auth_revision_hash") or "").strip()
    previous_revision_compatible = not previous_revision_hash or previous_revision_hash == current_revision_hash
    canonical_probe = incoming_probe
    canonical_preserved = False

    # A valid account response with no subscription plan is an incomplete read,
    # not new account evidence. Keep the last confirmed snapshot for this
    # credential revision and record the failed attempt separately.
    if (
        incoming_outcome in {"unknown_plan", "probe_failed", "probe_incomplete"}
        and previous_outcome == "confirmed"
        and previous_revision_compatible
    ):
        canonical_probe = copy.deepcopy(previous_probe or {})
        canonical_preserved = True
    elif incoming_outcome == "confirmed":
        _remember_confirmed_subscription(latest_extra, incoming_probe)
    elif incoming_outcome == "auth_invalid":
        _remember_confirmed_subscription(latest_extra, previous_probe)

    latest_extra["chatgpt_local"] = canonical_probe
    meta = _refresh_meta(latest_extra)
    active_state = str(meta.get("state") or "").strip().lower()
    if incoming_outcome in {"confirmed", "auth_invalid"}:
        next_state = "succeeded"
    elif active_state in {"pending", "running", "retry_wait"}:
        next_state = active_state
    else:
        next_state = "failed"
    meta = _set_refresh_meta(
        latest_extra,
        state=next_state,
        attempt_count=meta.get("attempt_count") if meta.get("attempt_count") is not None else 1,
        max_attempts=meta.get("max_attempts") or _LOCAL_STATUS_AUTO_MAX_ATTEMPTS,
        last_outcome=incoming_outcome,
        last_error="" if incoming_outcome in {"confirmed", "auth_invalid"} else _probe_error_message(incoming_probe),
        canonical_preserved=canonical_preserved,
    )
    meta["auth_revision_hash"] = current_revision_hash
    job_update = _reconcile_refresh_job_after_probe(
        session,
        int(account.id or 0),
        incoming_outcome,
        current_revision_hash,
    )
    if job_update:
        meta = _set_refresh_meta(
            latest_extra,
            state=str(job_update.get("state") or next_state),
            attempt_count=int(job_update.get("attempt_count") or meta.get("attempt_count") or 0),
            max_attempts=int(job_update.get("max_attempts") or meta.get("max_attempts") or _LOCAL_STATUS_AUTO_MAX_ATTEMPTS),
            next_attempt_at=float(job_update.get("next_attempt_at") or 0),
        )
        meta["auth_revision_hash"] = current_revision_hash
    capabilities = classify_chatgpt_capabilities(account, local_probe=canonical_probe)
    latest_extra["chatgpt_capabilities"] = capabilities
    account.set_extra(latest_extra)
    reason = apply_chatgpt_status_policy(account, local_probe=canonical_probe)
    account.updated_at = datetime.now(timezone.utc)
    session.add(account)
    from services.account_filters import upsert_account_list_state_for_account_ids

    upsert_account_list_state_for_account_ids(session, [account.id], commit=True)
    try:
        session.refresh(account)
    except Exception:
        pass
    return {
        "status": str(account.status or ""),
        "reason": reason,
        "capabilities": capabilities,
        "probe": incoming_probe,
        "canonical_probe": canonical_probe,
        "probe_persisted": not canonical_preserved,
        "canonical_preserved": canonical_preserved,
        "refresh_outcome": incoming_outcome,
        "refresh_state": "auth_invalid" if incoming_outcome == "auth_invalid" else (
            "confirmed" if incoming_outcome == "confirmed" else "refresh_failed"
        ),
        "refresh_meta": meta,
    }


def sync_chatgpt_account_local_status_by_id(
    account_id: Any,
    *,
    proxy: str | None = None,
    use_default_proxy: bool = True,
    prepared_account: Any = None,
    probe_runner: Any = None,
    on_probe_start: Any = None,
    stop_check: Any = None,
) -> dict[str, Any]:
    """Probe without a checked-out DB connection, then persist in a short transaction."""

    try:
        account_id_value = int(account_id or 0)
    except (TypeError, ValueError):
        account_id_value = 0
    if account_id_value <= 0:
        raise ValueError("本地状态刷新账号 ID 无效")

    probe_account = prepared_account
    if probe_account is None:
        with Session(core_db.engine) as read_session:
            account = read_session.get(AccountModel, account_id_value)
            if account is None or str(account.platform or "").strip().lower() != "chatgpt":
                raise LookupError(f"未找到 ChatGPT 账号 account_id={account_id_value}")
            probe_account = build_chatgpt_local_status_probe_account(account)
    expected_identity = _require_probe_account_identity(
        probe_account,
        account_id=account_id_value,
    )

    runner = probe_runner if callable(probe_runner) else None
    default_candidate_state: dict[str, Any] = {}
    for probe_attempt in range(1, _AUTH_MATERIAL_PROBE_MAX_ATTEMPTS + 1):
        _configured_local_status_concurrency()
        next_probe_account = None
        with local_status_identity_slot(probe_account, stop_check=stop_check):
            with local_status_capacity_slot(stop_check=stop_check):
                if callable(stop_check):
                    stop_check()
                if callable(on_probe_start):
                    on_probe_start(probe_account, probe_attempt)
                probed_auth_revision = _auth_material_revision(probe_account)
                probe = (
                    runner(probe_account)
                    if runner is not None
                    else probe_chatgpt_account_local_status(
                        probe_account,
                        proxy=proxy,
                        use_default_proxy=use_default_proxy,
                        candidate_state=default_candidate_state,
                    )
                )
                _reject_proxy_transport_probe(probe)

                with Session(core_db.engine) as write_session:
                    account = write_session.get(AccountModel, account_id_value)
                    if account is None or str(account.platform or "").strip().lower() != "chatgpt":
                        raise LookupError(f"ChatGPT 账号在探测期间已删除 account_id={account_id_value}")
                    _assert_local_status_account_identity(account, expected_identity)
                    if probed_auth_revision == _auth_material_revision(account):
                        return _persist_chatgpt_local_status_probe(write_session, account, probe)
                    next_probe_account = build_chatgpt_local_status_probe_account(account)

        # Both process-wide leases must be released before a revision retry can
        # queue again under the new fingerprint/auth identity.
        probe_account = next_probe_account
        logger.info(
            "ChatGPT local status probe discarded after auth material changed account_id=%s attempt=%s/%s",
            account_id_value,
            probe_attempt,
            _AUTH_MATERIAL_PROBE_MAX_ATTEMPTS,
        )

    raise RuntimeError("账号认证材料在本地状态探测期间连续变化，已丢弃过期探测结果")


def sync_chatgpt_account_local_status(
    session: Session,
    account: AccountModel,
    *,
    proxy: str | None = None,
    use_default_proxy: bool = True,
) -> dict[str, Any]:
    """Probe ChatGPT locally and persist status/capabilities onto ``account``.

    The caller owns the SQLModel session. This function commits and refreshes the
    account because the probe result should be visible immediately to UI snapshot
    polling after a paid card/order is observed.
    """

    # Build the detached snapshot before commit: SQLAlchemy expires ORM fields
    # on commit by default, and reading them afterwards would silently check a
    # connection back out for the full network probe.
    probe_account = build_chatgpt_local_status_probe_account(account)
    expected_identity = _require_probe_account_identity(
        probe_account,
        account_id=int(getattr(account, "id", 0) or 0),
    )
    try:
        session.commit()
    except Exception:
        session.rollback()

    default_candidate_state: dict[str, Any] = {}
    for probe_attempt in range(1, _AUTH_MATERIAL_PROBE_MAX_ATTEMPTS + 1):
        _configured_local_status_concurrency()
        next_probe_account = None
        with local_status_identity_slot(probe_account):
            with local_status_capacity_slot():
                probed_auth_revision = _auth_material_revision(probe_account)
                probe = probe_chatgpt_account_local_status(
                    probe_account,
                    proxy=proxy,
                    use_default_proxy=use_default_proxy,
                    candidate_state=default_candidate_state,
                )
                _reject_proxy_transport_probe(probe)
                session.refresh(account)
                _assert_local_status_account_identity(account, expected_identity)
                if probed_auth_revision == _auth_material_revision(account):
                    return _persist_chatgpt_local_status_probe(session, account, probe)
                next_probe_account = build_chatgpt_local_status_probe_account(account)
            probe_account = next_probe_account
            try:
                session.commit()
            except Exception:
                session.rollback()
        logger.info(
            "ChatGPT local status probe discarded after auth material changed account_id=%s attempt=%s/%s",
            account.id,
            probe_attempt,
            _AUTH_MATERIAL_PROBE_MAX_ATTEMPTS,
        )
    else:
        raise RuntimeError("账号认证材料在本地状态探测期间连续变化，已丢弃过期探测结果")


def _safe_refresh_error(value: Any) -> str:
    text = str(value or "").strip()
    try:
        from services.chatgpt_core.task_logging import sanitize_error_message

        text = sanitize_error_message(text)
    except Exception:
        pass
    return text[:500]


def _merge_refresh_requests(
    previous: dict[str, Any] | None,
    current: dict[str, Any],
) -> dict[str, Any]:
    """Merge duplicate requests without discarding a verified proxy or deadline."""

    if not isinstance(previous, dict):
        return dict(current)
    merged = dict(previous)
    current_proxy = str(current.get("proxy") or "").strip()
    previous_proxy = str(previous.get("proxy") or "").strip()
    if current_proxy or not previous_proxy:
        merged["proxy"] = current.get("proxy")
        merged["use_default_proxy"] = bool(current.get("use_default_proxy", True))
    merged["reason"] = str(current.get("reason") or previous.get("reason") or "account_saved")
    merged["delay_seconds"] = min(
        float(previous.get("delay_seconds") or 0.0),
        float(current.get("delay_seconds") or 0.0),
    )
    if current.get("generation") is not None:
        merged["generation"] = max(
            int(previous.get("generation") or 0),
            int(current.get("generation") or 0),
        )
    return merged


def _enqueue_local_status_refresh_job(
    account_id: int,
    request: dict[str, Any],
) -> dict[str, Any] | None:
    """Persist one coalesced refresh request and return its generation metadata."""

    now = _utc_timestamp()
    try:
        with Session(core_db.engine) as session:
            account = session.get(AccountModel, int(account_id))
            if (
                account is None
                or str(getattr(account, "platform", "") or "").strip().lower() != "chatgpt"
                or not account_has_local_status_auth_material(account)
            ):
                return None
            account_email = str(getattr(account, "email", "") or "").strip().lower()
            account_created_at = _local_status_created_at_identity(getattr(account, "created_at", None))
            revision_hash = _auth_material_revision_hash(account)
            job = session.get(ChatGPTLocalStatusRefreshJobModel, int(account_id))
            if job is None:
                job = ChatGPTLocalStatusRefreshJobModel(
                    account_id=int(account_id),
                    account_email=account_email,
                    account_created_at=account_created_at,
                    auth_revision_hash=revision_hash,
                    generation=1,
                    state="pending",
                    attempt_count=0,
                    max_attempts=_LOCAL_STATUS_AUTO_MAX_ATTEMPTS,
                )
                session.add(job)
            same_identity = (
                str(job.account_email or "").strip().lower() == account_email
                and str(job.account_created_at or "") == account_created_at
            )
            same_revision = same_identity and str(job.auth_revision_hash or "") == revision_hash
            job_state = str(job.state or "").strip().lower()
            terminal_success_recent = (
                same_revision
                and job_state == "succeeded"
                and job.completed_at_ts > 0
                and now - float(job.completed_at_ts) < _LOCAL_STATUS_SUCCESS_DEDUPE_SECONDS
            )
            if terminal_success_recent:
                return {
                    "generation": int(job.generation or 1),
                    "start": False,
                    "deduped": True,
                }

            new_generation = int(job.generation or 0)
            reset_attempts = False
            if not same_revision or job_state in {"failed", "succeeded"}:
                new_generation = max(1, new_generation + 1)
                reset_attempts = True
            job.account_email = account_email
            job.account_created_at = account_created_at
            job.auth_revision_hash = revision_hash
            job.generation = new_generation
            job.state = "pending"
            job.reason = str(request.get("reason") or "account_saved")[:160]
            if reset_attempts:
                job.attempt_count = 0
            job.max_attempts = _LOCAL_STATUS_AUTO_MAX_ATTEMPTS
            job.requested_at_ts = now
            requested_due = now + max(0.0, float(request.get("delay_seconds") or 0.0))
            if not reset_attempts and job.next_attempt_at_ts > 0:
                job.next_attempt_at_ts = min(float(job.next_attempt_at_ts), requested_due)
            else:
                job.next_attempt_at_ts = requested_due
            job.started_at_ts = 0 if reset_attempts else float(job.started_at_ts or 0)
            job.completed_at_ts = 0
            job.updated_at_ts = now
            job.last_outcome = ""
            job.last_error = ""

            extra = account.get_extra()
            if not isinstance(extra, dict):
                extra = {}
            meta = _set_refresh_meta(
                extra,
                state="pending",
                reason=job.reason,
                attempt_count=int(job.attempt_count or 0),
                max_attempts=job.max_attempts,
                next_attempt_at=job.next_attempt_at_ts,
                started_at=0,
                completed_at=0,
                last_outcome="",
                last_error="",
                requested_at=now,
            )
            meta["auth_revision_hash"] = revision_hash
            account.set_extra(extra)
            session.add(account)
            session.add(job)
            session.commit()
            return {
                "generation": new_generation,
                "start": True,
                "deduped": False,
                "delay_seconds": max(0.0, job.next_attempt_at_ts - now),
            }
    except Exception:
        logger.warning(
            "ChatGPT local status refresh job enqueue failed account_id=%s",
            account_id,
            exc_info=True,
        )
        return None


def _mark_local_status_refresh_schedule_failure(account_id: int, reason: str) -> None:
    """Do not leave an auth-change row looking queued when durable enqueue failed."""

    try:
        with Session(core_db.engine) as session:
            account = session.get(AccountModel, int(account_id))
            if account is None:
                return
            extra = account.get_extra()
            if not isinstance(extra, dict):
                return
            meta = _refresh_meta(extra)
            if str(meta.get("state") or "").strip().lower() not in {"pending", "running", "retry_wait"}:
                return
            _set_refresh_meta(
                extra,
                state="failed",
                reason=reason,
                completed_at=_utc_timestamp(),
                last_outcome="schedule_failed",
                last_error="本地状态刷新任务未能进入持久队列",
            )
            account.set_extra(extra)
            session.add(account)
            session.commit()
    except Exception:
        logger.warning(
            "ChatGPT local status refresh schedule failure state persist failed account_id=%s",
            account_id,
            exc_info=True,
        )


def _update_refresh_job_state(
    account_id: int,
    generation: int,
    *,
    state: str,
    attempt_count: int | None = None,
    next_attempt_at: float | None = None,
    started_at: float | None = None,
    completed_at: float | None = None,
    last_outcome: str | None = None,
    last_error: str | None = None,
    update_account_meta: bool = True,
) -> bool:
    now = _utc_timestamp()
    try:
        with Session(core_db.engine) as session:
            job = session.get(ChatGPTLocalStatusRefreshJobModel, int(account_id))
            if job is None or int(job.generation or 0) != int(generation):
                return False
            job.state = str(state or "").strip().lower()
            if attempt_count is not None:
                job.attempt_count = max(0, int(attempt_count))
            if next_attempt_at is not None:
                job.next_attempt_at_ts = max(0.0, float(next_attempt_at))
            if started_at is not None:
                job.started_at_ts = max(0.0, float(started_at))
            if completed_at is not None:
                job.completed_at_ts = max(0.0, float(completed_at))
            if last_outcome is not None:
                job.last_outcome = str(last_outcome or "").strip().lower()
            if last_error is not None:
                job.last_error = _safe_refresh_error(last_error)
            job.updated_at_ts = now
            account = session.get(AccountModel, int(account_id)) if update_account_meta else None
            if account is not None:
                extra = account.get_extra()
                if not isinstance(extra, dict):
                    extra = {}
                meta = _set_refresh_meta(
                    extra,
                    state=job.state,
                    reason=job.reason,
                    attempt_count=job.attempt_count,
                    max_attempts=job.max_attempts,
                    next_attempt_at=job.next_attempt_at_ts,
                    started_at=job.started_at_ts,
                    completed_at=job.completed_at_ts,
                    last_outcome=job.last_outcome,
                    last_error=job.last_error,
                )
                meta["auth_revision_hash"] = str(job.auth_revision_hash or "")
                account.set_extra(extra)
                session.add(account)
            session.add(job)
            session.commit()
            return True
    except Exception:
        logger.warning(
            "ChatGPT local status refresh job state update failed account_id=%s state=%s",
            account_id,
            state,
            exc_info=True,
        )
        return False


def _reconcile_refresh_job_after_probe(
    session: Session,
    account_id: int,
    outcome: str,
    auth_revision_hash: str,
) -> dict[str, Any]:
    """Keep direct/manual probes and the durable queue in the same terminal state."""

    try:
        job = session.get(ChatGPTLocalStatusRefreshJobModel, int(account_id))
        if job is None or str(job.auth_revision_hash or "") != str(auth_revision_hash or ""):
            return {}
        state = str(job.state or "").strip().lower()
        if state == "running":
            return {}
        attempt_count = max(1, int(job.attempt_count or 0))
        if outcome in {"confirmed", "auth_invalid"}:
            job.state = "succeeded"
            job.completed_at_ts = _utc_timestamp()
            job.next_attempt_at_ts = 0
            job.last_outcome = outcome
            job.last_error = ""
        elif state not in {"succeeded", "failed"}:
            job.state = "retry_wait"
            job.attempt_count = attempt_count
            job.next_attempt_at_ts = _utc_timestamp() + _LOCAL_STATUS_AUTO_RETRY_DELAYS_SECONDS[0]
            job.last_outcome = outcome
            job.last_error = _probe_error_message({"subscription": {"plan": "unknown"}})
        job.attempt_count = max(int(job.attempt_count or 0), attempt_count)
        job.updated_at_ts = _utc_timestamp()
        session.add(job)
        return {
            "state": str(job.state or ""),
            "attempt_count": int(job.attempt_count or 0),
            "max_attempts": int(job.max_attempts or _LOCAL_STATUS_AUTO_MAX_ATTEMPTS),
            "next_attempt_at": float(job.next_attempt_at_ts or 0),
        }
    except Exception:
        logger.warning(
            "ChatGPT local status refresh job reconciliation failed account_id=%s",
            account_id,
            exc_info=True,
        )
        return {}


def _claim_refresh_job_attempt(account_id: int, generation: int) -> dict[str, Any] | None:
    now = _utc_timestamp()
    try:
        with Session(core_db.engine) as session:
            job = session.get(ChatGPTLocalStatusRefreshJobModel, int(account_id))
            if job is None or int(job.generation or 0) != int(generation):
                return None
            if str(job.state or "").strip().lower() in {"succeeded", "failed"}:
                return None
            if float(job.next_attempt_at_ts or 0) > now:
                return {
                    "wait_seconds": float(job.next_attempt_at_ts) - now,
                    "attempt_count": int(job.attempt_count or 0),
                }
            job.state = "running"
            job.attempt_count = int(job.attempt_count or 0) + 1
            job.started_at_ts = now
            job.updated_at_ts = now
            session.add(job)
            session.commit()
            return {
                "wait_seconds": 0.0,
                "attempt_count": int(job.attempt_count or 0),
                "max_attempts": int(job.max_attempts or _LOCAL_STATUS_AUTO_MAX_ATTEMPTS),
                "reason": str(job.reason or "account_saved"),
            }
    except Exception:
        logger.warning(
            "ChatGPT local status refresh job claim failed account_id=%s",
            account_id,
            exc_info=True,
        )
        return None


def _take_pending_refresh_request(account_id: int) -> dict[str, Any] | None:
    with _LOCAL_STATUS_REFRESH_LOCK:
        return _LOCAL_STATUS_REFRESH_PENDING.pop(int(account_id), None)


def _run_local_status_refresh_worker(initial_request: dict[str, Any]) -> None:
    account_id = int(initial_request.get("account_id") or 0)
    current_request: dict[str, Any] | None = dict(initial_request)
    try:
        while current_request is not None and account_id > 0:
            generation = int(current_request.get("generation") or 0)
            delay = max(0.0, float(current_request.get("delay_seconds") or 0.0))
            if delay > 0 and _LOCAL_STATUS_RECOVERY_STOP_EVENT.wait(delay):
                return

            claim = _claim_refresh_job_attempt(account_id, generation)
            if claim is None:
                pending = _take_pending_refresh_request(account_id)
                current_request = pending
                continue
            wait_seconds = float(claim.get("wait_seconds") or 0.0)
            if wait_seconds > 0:
                if _LOCAL_STATUS_RECOVERY_STOP_EVENT.wait(wait_seconds):
                    return
                current_request["delay_seconds"] = 0
                continue

            attempt_count = int(claim.get("attempt_count") or 0)
            max_attempts = max(1, int(claim.get("max_attempts") or _LOCAL_STATUS_AUTO_MAX_ATTEMPTS))
            outcome = "probe_failed"
            error_text = ""
            refresh_result: dict[str, Any] | None = None
            try:
                with Session(core_db.engine) as session:
                    account = session.get(AccountModel, account_id)
                    prepared_account = (
                        build_chatgpt_local_status_probe_account(account)
                        if account is not None
                        and str(getattr(account, "platform", "") or "").strip().lower() == "chatgpt"
                        and account_has_local_status_auth_material(account)
                        else None
                    )
                if prepared_account is None:
                    raise LookupError(f"未找到带认证材料的 ChatGPT 账号 account_id={account_id}")
                refresh_result = sync_chatgpt_account_local_status_by_id(
                    account_id,
                    proxy=current_request.get("proxy"),
                    use_default_proxy=bool(current_request.get("use_default_proxy", True)),
                    prepared_account=prepared_account,
                )
                outcome = str(refresh_result.get("refresh_outcome") or "").strip().lower() or "probe_failed"
                if outcome in {"confirmed", "auth_invalid"}:
                    _update_refresh_job_state(
                        account_id,
                        generation,
                        state="succeeded",
                        attempt_count=attempt_count,
                        next_attempt_at=0,
                        completed_at=_utc_timestamp(),
                        last_outcome=outcome,
                        last_error="",
                        update_account_meta=not isinstance(refresh_result.get("refresh_meta"), dict),
                    )
                else:
                    error_text = _probe_error_message(refresh_result.get("probe") or {})
                    raise RuntimeError(error_text)
            except Exception as exc:
                error_text = _safe_refresh_error(exc) or error_text or "本地状态刷新失败"
                if attempt_count < max_attempts:
                    retry_index = min(attempt_count - 1, len(_LOCAL_STATUS_AUTO_RETRY_DELAYS_SECONDS) - 1)
                    retry_delay = _LOCAL_STATUS_AUTO_RETRY_DELAYS_SECONDS[max(0, retry_index)]
                    next_at = _utc_timestamp() + retry_delay
                    _update_refresh_job_state(
                        account_id,
                        generation,
                        state="retry_wait",
                        attempt_count=attempt_count,
                        next_attempt_at=next_at,
                        last_outcome=outcome,
                        last_error=error_text,
                    )
                    current_request["delay_seconds"] = retry_delay
                    pending = _take_pending_refresh_request(account_id)
                    if pending is not None:
                        current_request = _merge_refresh_requests(current_request, pending)
                        current_request["generation"] = int(pending.get("generation") or generation)
                    continue
                _update_refresh_job_state(
                    account_id,
                    generation,
                    state="failed",
                    attempt_count=attempt_count,
                    next_attempt_at=0,
                    completed_at=_utc_timestamp(),
                    last_outcome=outcome,
                    last_error=error_text,
                )

            pending = _take_pending_refresh_request(account_id)
            if pending is not None and int(pending.get("generation") or generation) != generation:
                current_request = pending
            else:
                current_request = None
    finally:
        restart_request: dict[str, Any] | None = None
        with _LOCAL_STATUS_REFRESH_LOCK:
            restart_request = _LOCAL_STATUS_REFRESH_PENDING.pop(account_id, None)
            _LOCAL_STATUS_REFRESH_IN_FLIGHT.discard(account_id)
        if restart_request is not None and not _LOCAL_STATUS_RECOVERY_STOP_EVENT.is_set():
            _start_local_status_refresh_worker(restart_request)


def _start_local_status_refresh_worker(request: dict[str, Any]) -> bool:
    account_id = int(request.get("account_id") or 0)
    if account_id <= 0:
        return False
    with _LOCAL_STATUS_REFRESH_LOCK:
        if account_id in _LOCAL_STATUS_REFRESH_IN_FLIGHT:
            existing = _LOCAL_STATUS_REFRESH_PENDING.get(account_id)
            _LOCAL_STATUS_REFRESH_PENDING[account_id] = _merge_refresh_requests(existing, request)
            return True
        _LOCAL_STATUS_REFRESH_IN_FLIGHT.add(account_id)
    try:
        thread = threading.Thread(
            target=_run_local_status_refresh_worker,
            args=(dict(request),),
            name=f"chatgpt-local-status-refresh-{account_id}",
            daemon=True,
        )
        thread.start()
        return True
    except Exception:
        with _LOCAL_STATUS_REFRESH_LOCK:
            _LOCAL_STATUS_REFRESH_IN_FLIGHT.discard(account_id)
        logger.warning(
            "ChatGPT local status refresh worker start failed account_id=%s",
            account_id,
            exc_info=True,
        )
        return False


def _resume_due_local_status_refresh_jobs() -> int:
    now = _utc_timestamp()
    requests: list[dict[str, Any]] = []
    try:
        with Session(core_db.engine) as session:
            rows = session.exec(
                select(ChatGPTLocalStatusRefreshJobModel)
                .where(ChatGPTLocalStatusRefreshJobModel.state.in_(["pending", "retry_wait", "running"]))
                .order_by(ChatGPTLocalStatusRefreshJobModel.next_attempt_at_ts.asc())
                .limit(100)
            ).all()
            for job in rows:
                state = str(job.state or "").strip().lower()
                due = float(job.next_attempt_at_ts or 0)
                if state == "running" and now - float(job.updated_at_ts or 0) < _LOCAL_STATUS_STALE_RUNNING_SECONDS:
                    continue
                if state == "running":
                    job.state = "retry_wait"
                    job.next_attempt_at_ts = now
                    job.updated_at_ts = now
                    session.add(job)
                    state = "retry_wait"
                    due = now
                if due > now:
                    continue
                requests.append(
                    {
                        "account_id": int(job.account_id or 0),
                        "generation": int(job.generation or 1),
                        "proxy": None,
                        "use_default_proxy": True,
                        "reason": f"recovery:{job.reason or 'scheduled'}",
                        "delay_seconds": 0.0,
                    }
                )
            if session.dirty:
                session.commit()
    except Exception:
        logger.warning("ChatGPT local status refresh recovery scan failed", exc_info=True)
        return 0
    started = 0
    for request in requests:
        if _start_local_status_refresh_worker(request):
            started += 1
    return started


def _schedule_legacy_stale_subscription_refreshes(*, limit: int = 500) -> int:
    """Bring pre-queue stale rows into the durable workflow after an upgrade."""

    account_ids: list[int] = []
    try:
        with Session(core_db.engine) as session:
            queued_ids = set(
                session.exec(select(ChatGPTLocalStatusRefreshJobModel.account_id)).all()
            )
            candidate_ids = session.exec(
                select(AccountListStateModel.account_id)
                .where(AccountListStateModel.platform == "chatgpt")
                .where(AccountListStateModel.subscription_type == "unknown")
                .where(AccountListStateModel.account_validity.in_(["valid", "refresh_failed"]))
                .order_by(AccountListStateModel.account_id.asc())
                .limit(max(1, int(limit)))
            ).all()
            for raw_account_id in candidate_ids:
                account_id = int(raw_account_id or 0)
                if account_id <= 0 or account_id in queued_ids:
                    continue
                account = session.get(AccountModel, account_id)
                if account is None or not account_has_local_status_auth_material(account):
                    continue
                extra = account.get_extra()
                if not isinstance(extra, dict):
                    continue
                local_probe = extra.get("chatgpt_local") if isinstance(extra.get("chatgpt_local"), dict) else {}
                subscription = local_probe.get("subscription") if isinstance(local_probe.get("subscription"), dict) else {}
                capabilities = extra.get("chatgpt_capabilities") if isinstance(extra.get("chatgpt_capabilities"), dict) else {}
                last_confirmed = extra.get(_LOCAL_STATUS_LAST_CONFIRMED_SUBSCRIPTION_KEY)
                last_confirmed = last_confirmed if isinstance(last_confirmed, dict) else {}
                current_plan = normalize_subscription_plan(
                    subscription.get("plan") or capabilities.get("subscription_plan")
                )
                last_plan = normalize_subscription_plan(
                    capabilities.get("last_known_subscription_plan")
                    or last_confirmed.get("plan")
                    or extra.get("last_known_subscription_plan")
                )
                if current_plan == "unknown" and last_plan != "unknown":
                    account_ids.append(account_id)
    except Exception:
        logger.warning("Legacy stale subscription refresh discovery failed", exc_info=True)
        return 0

    scheduled = 0
    for index, account_id in enumerate(account_ids):
        if schedule_chatgpt_local_status_refresh_for_account_id(
            account_id,
            reason="startup_legacy_stale_subscription",
            delay_seconds=min(30.0, index * 0.25),
        ):
            scheduled += 1
    if scheduled:
        logger.info("Scheduled %s legacy stale subscription refresh jobs", scheduled)
    return scheduled


def _local_status_refresh_recovery_loop() -> None:
    global _LOCAL_STATUS_RECOVERY_RUNNING
    while not _LOCAL_STATUS_RECOVERY_STOP_EVENT.is_set():
        _resume_due_local_status_refresh_jobs()
        _LOCAL_STATUS_RECOVERY_STOP_EVENT.wait(_LOCAL_STATUS_RECOVERY_INTERVAL_SECONDS)
    with _LOCAL_STATUS_RECOVERY_STATE_LOCK:
        _LOCAL_STATUS_RECOVERY_RUNNING = False


def start_chatgpt_local_status_refresh_recovery() -> None:
    """Start the process-level scanner that resumes durable refresh jobs."""

    global _LOCAL_STATUS_RECOVERY_THREAD, _LOCAL_STATUS_RECOVERY_RUNNING
    with _LOCAL_STATUS_RECOVERY_STATE_LOCK:
        if _LOCAL_STATUS_RECOVERY_RUNNING:
            return
        _LOCAL_STATUS_RECOVERY_RUNNING = True
    _LOCAL_STATUS_RECOVERY_STOP_EVENT.clear()
    _schedule_legacy_stale_subscription_refreshes()
    _resume_due_local_status_refresh_jobs()
    _LOCAL_STATUS_RECOVERY_THREAD = threading.Thread(
        target=_local_status_refresh_recovery_loop,
        name="chatgpt-local-status-refresh-recovery",
        daemon=True,
    )
    _LOCAL_STATUS_RECOVERY_THREAD.start()


def stop_chatgpt_local_status_refresh_recovery() -> None:
    """Stop the scanner while leaving queued rows durable for the next process."""

    global _LOCAL_STATUS_RECOVERY_THREAD
    _LOCAL_STATUS_RECOVERY_STOP_EVENT.set()
    thread = _LOCAL_STATUS_RECOVERY_THREAD
    if thread and thread.is_alive():
        thread.join(timeout=5)
    _LOCAL_STATUS_RECOVERY_THREAD = None


def schedule_chatgpt_local_status_refresh_for_account_id(
    account_id: Any,
    *,
    proxy: str | None = None,
    use_default_proxy: bool = True,
    reason: str = "account_saved",
    delay_seconds: float = 0.0,
) -> bool:
    """Enqueue a restart-safe, coalesced local-status refresh."""

    try:
        account_id_value = int(account_id or 0)
    except Exception:
        account_id_value = 0
    if account_id_value <= 0:
        return False
    try:
        normalized_delay_seconds = max(0.0, float(delay_seconds or 0.0))
    except (TypeError, ValueError):
        normalized_delay_seconds = 0.0
    request = {
        "account_id": account_id_value,
        "proxy": proxy,
        "use_default_proxy": bool(use_default_proxy),
        "reason": str(reason or "account_saved"),
        "delay_seconds": normalized_delay_seconds,
    }
    job_info = _enqueue_local_status_refresh_job(account_id_value, request)
    if not job_info:
        _mark_local_status_refresh_schedule_failure(account_id_value, request["reason"])
        return False
    request["generation"] = int(job_info.get("generation") or 1)
    if not bool(job_info.get("start", True)):
        return True
    return _start_local_status_refresh_worker(request)


def summarize_status_refresh(refresh_result: dict[str, Any] | None, *, trigger: str = "") -> dict[str, Any]:
    """Build a compact UI/log friendly summary from a local status refresh."""

    result = refresh_result if isinstance(refresh_result, dict) else {}
    capabilities = result.get("capabilities") if isinstance(result.get("capabilities"), dict) else {}
    probe = result.get("probe") if isinstance(result.get("probe"), dict) else {}
    auth = probe.get("auth") if isinstance(probe.get("auth"), dict) else {}
    subscription = probe.get("subscription") if isinstance(probe.get("subscription"), dict) else {}
    codex = probe.get("codex") if isinstance(probe.get("codex"), dict) else {}
    return {
        "trigger": str(trigger or ""),
        "refreshed_at": datetime.now(timezone.utc).isoformat(),
        "status": str(result.get("status") or ""),
        "policy_reason": str(result.get("reason") or ""),
        "auth_state": str(auth.get("state") or ""),
        "subscription_plan": str(subscription.get("plan") or capabilities.get("subscription_plan") or ""),
        "subscription_active_until": str(subscription.get("subscription_active_until") or ""),
        "codex_state": str(codex.get("state") or capabilities.get("codex_state") or ""),
        "upload_gate": str(capabilities.get("upload_gate") or ""),
    }
