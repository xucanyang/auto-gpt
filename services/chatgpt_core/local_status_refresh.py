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
from services.chatgpt_account_state import apply_chatgpt_status_policy, classify_chatgpt_capabilities
from services.chatgpt_core.status_probe import probe_local_chatgpt_status

logger = logging.getLogger(__name__)

_LOCAL_STATUS_REFRESH_LOCK = threading.Lock()
_LOCAL_STATUS_REFRESH_IN_FLIGHT: set[int] = set()


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


def sync_chatgpt_account_local_status(session: Session, account: AccountModel, *, proxy: str = "") -> dict[str, Any]:
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
    probe = probe_local_chatgpt_status(probe_account, proxy=proxy)
    extra["chatgpt_local"] = probe
    capabilities = classify_chatgpt_capabilities(account, local_probe=probe)
    extra["chatgpt_capabilities"] = capabilities
    account.set_extra(extra)
    reason = apply_chatgpt_status_policy(account, local_probe=probe)
    account.updated_at = datetime.now(timezone.utc)
    session.add(account)
    from services.account_filters import upsert_account_list_state_for_account_ids

    upsert_account_list_state_for_account_ids(session, [account.id], commit=False)
    session.commit()
    session.refresh(account)
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
                sync_chatgpt_account_local_status(session, account, proxy=str(proxy or ""))
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
