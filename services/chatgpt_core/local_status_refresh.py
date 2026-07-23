"""Local ChatGPT account status refresh helpers.

Used after external payment/card-code systems report success so the local account
record is refreshed from ChatGPT instead of only writing an external paid marker.
"""
from __future__ import annotations

from datetime import datetime, timezone
import logging
import threading
import time
from types import SimpleNamespace
from typing import Any

from sqlmodel import Session

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
_SUBSCRIPTION_RETRY_DELAY_SECONDS = 3.0


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

    extra = account.get_extra()
    if not isinstance(extra, dict):
        extra = {}
    probe_account = SimpleNamespace(
        id=account.id,
        email=account.email,
        password=account.password,
        user_id=account.user_id,
        token=account.token,
        status=account.status,
        access_token=str(extra.get("access_token") or account.token or "").strip(),
        refresh_token=str(extra.get("refresh_token") or "").strip(),
        id_token=str(extra.get("id_token") or "").strip(),
        session_token=str(extra.get("session_token") or "").strip(),
        client_id=str(extra.get("client_id") or "app_EMoamEEZ73f0CkXaXp7hrann").strip(),
        cookies=str(extra.get("cookies") or "").strip(),
        workspace_id=str(extra.get("workspace_id") or "").strip(),
        extra=extra,
    )
    probe = _probe_local_status_with_subscription_retry(
        probe_account,
        proxy=proxy,
        use_default_proxy=use_default_proxy,
    )
    try:
        session.commit()
    except Exception:
        session.rollback()
    try:
        session.refresh(account)
    except Exception:
        pass
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

    with _LOCAL_STATUS_REFRESH_LOCK:
        if account_id_value in _LOCAL_STATUS_REFRESH_IN_FLIGHT:
            return False
        _LOCAL_STATUS_REFRESH_IN_FLIGHT.add(account_id_value)

    def _worker() -> None:
        try:
            delay = max(0.0, float(delay_seconds or 0.0))
            if delay > 0:
                time.sleep(delay)
            from core.db import AccountModel, engine

            with Session(engine) as session:
                account = session.get(AccountModel, account_id_value)
                if account is None:
                    return
                if str(getattr(account, "platform", "") or "").strip().lower() != "chatgpt":
                    return
                if not account_has_local_status_auth_material(account):
                    return
                sync_chatgpt_account_local_status(
                    session,
                    account,
                    proxy=proxy,
                    use_default_proxy=use_default_proxy,
                )
        except Exception as exc:
            logger.warning(
                "ChatGPT local status auto-refresh failed account_id=%s reason=%s error=%s",
                account_id_value,
                reason,
                exc,
                exc_info=True,
            )
        finally:
            with _LOCAL_STATUS_REFRESH_LOCK:
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
