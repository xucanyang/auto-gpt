"""Local ChatGPT account status refresh helpers.

Used after external payment/card-code systems report success so the local account
record is refreshed from ChatGPT instead of only writing an external paid marker.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import logging
import threading
import time
from types import SimpleNamespace
from typing import Any

from sqlmodel import Session

from core import db as core_db
from core.db import AccountModel
from services.chatgpt_account_state import (
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
_LOCAL_STATUS_CONCURRENCY_HARD_LIMIT = 10
_LOCAL_STATUS_CONCURRENCY_UPDATE_LOCK = threading.RLock()
_LOCAL_STATUS_CAPACITY_CONDITION = threading.Condition()
_LOCAL_STATUS_CAPACITY_ACTIVE = 0
_LOCAL_STATUS_CAPACITY_LIMIT = 1
_LOCAL_STATUS_CAPACITY_WAITERS: list[object] = []
_LOCAL_STATUS_IDENTITY_REGISTRY_LOCK = threading.Lock()
_LOCAL_STATUS_IDENTITY_GATES: dict[str, dict[str, Any]] = {}


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
    latest_extra["chatgpt_local"] = probe
    capabilities = classify_chatgpt_capabilities(account, local_probe=probe)
    latest_extra["chatgpt_capabilities"] = capabilities
    account.set_extra(latest_extra)
    reason = apply_chatgpt_status_policy(account, local_probe=probe)
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
        "probe": probe,
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


def schedule_chatgpt_local_status_refresh_for_account_id(
    account_id: Any,
    *,
    proxy: str | None = None,
    use_default_proxy: bool = True,
    reason: str = "account_saved",
    delay_seconds: float = 0.0,
) -> bool:
    """Start a daemon local-status refresh for a committed ChatGPT account id."""
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
        "proxy": proxy,
        "use_default_proxy": bool(use_default_proxy),
        "reason": str(reason or "account_saved"),
        "delay_seconds": normalized_delay_seconds,
    }
    with _LOCAL_STATUS_REFRESH_LOCK:
        if account_id_value in _LOCAL_STATUS_REFRESH_IN_FLIGHT:
            _LOCAL_STATUS_REFRESH_PENDING[account_id_value] = request
            return True
        _LOCAL_STATUS_REFRESH_IN_FLIGHT.add(account_id_value)

    def _worker() -> None:
        current_request: dict[str, Any] | None = request
        while current_request is not None:
            try:
                delay = float(current_request.get("delay_seconds") or 0.0)
                if delay > 0:
                    time.sleep(delay)
                with Session(core_db.engine) as session:
                    account = session.get(AccountModel, account_id_value)
                    prepared_account = (
                        build_chatgpt_local_status_probe_account(account)
                        if account is not None
                        and str(getattr(account, "platform", "") or "").strip().lower() == "chatgpt"
                        and account_has_local_status_auth_material(account)
                        else None
                    )
                if prepared_account is not None:
                    sync_chatgpt_account_local_status_by_id(
                        account_id_value,
                        proxy=current_request.get("proxy"),
                        use_default_proxy=bool(current_request.get("use_default_proxy", True)),
                        prepared_account=prepared_account,
                    )
            except Exception as exc:
                logger.warning(
                    "ChatGPT local status auto-refresh failed account_id=%s reason=%s error=%s",
                    account_id_value,
                    current_request.get("reason") or "account_saved",
                    exc,
                    exc_info=True,
                )
            with _LOCAL_STATUS_REFRESH_LOCK:
                current_request = _LOCAL_STATUS_REFRESH_PENDING.pop(account_id_value, None)
                if current_request is None:
                    _LOCAL_STATUS_REFRESH_IN_FLIGHT.discard(account_id_value)

    try:
        thread = threading.Thread(
            target=_worker,
            name=f"chatgpt-local-status-refresh-{account_id_value}",
            daemon=True,
        )
        thread.start()
        return True
    except Exception as exc:
        with _LOCAL_STATUS_REFRESH_LOCK:
            _LOCAL_STATUS_REFRESH_IN_FLIGHT.discard(account_id_value)
            _LOCAL_STATUS_REFRESH_PENDING.pop(account_id_value, None)
        logger.warning(
            "ChatGPT local status auto-refresh schedule failed account_id=%s reason=%s error=%s",
            account_id_value,
            reason,
            exc,
            exc_info=True,
        )
        return False


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
