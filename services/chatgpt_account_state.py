"""ChatGPT 账号状态判定辅助逻辑。"""

from __future__ import annotations

from typing import Any


INVALID_ACCOUNT_STATUS = "invalid"
RECOVERED_ACCOUNT_STATUS = "registered"
PENDING_PAYMENT_STATUS = "pending_payment"
PAYMENT_FAILED_STATUS = "payment_failed"
SUBSCRIBED_ACCOUNT_STATUS = "subscribed"

PAID_PLAN_TYPES = {"plus", "pro", "team", "business", "enterprise"}
PAYMENT_ACTIVE_PHASES = {"created", "starting", "waiting_otp", "waiting_link_pin", "waiting_payment_pin", "verifying"}
PAYMENT_ACTIVE_STATUSES = {"active", "started", "running"}
PAYMENT_FAILED_PHASES = {"failed", "cancelled"}
PAYMENT_FAILED_STATUSES = {"failed", "cancelled", "stopped"}
PAYMENT_SUCCEEDED_PHASES = {"succeeded"}
PAYMENT_SUCCEEDED_STATUSES = {"done", "succeeded", "success"}
AUTH_INVALID_STATES = {
    "refresh_token_invalidated",
    "access_token_invalidated",
    "unauthorized",
    "account_deactivated",
    "banned_like",
}


def _get_extra(account: Any) -> dict[str, Any]:
    if hasattr(account, "get_extra"):
        try:
            extra = account.get_extra()
            if isinstance(extra, dict):
                return extra
        except Exception:
            pass
    extra = getattr(account, "extra", {})
    return extra if isinstance(extra, dict) else {}


def _lower_text(value: Any) -> str:
    return str(value or "").strip().lower()


def _truthy_text(value: Any) -> bool:
    return bool(str(value or "").strip())


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def is_paid_subscription_plan(plan: Any) -> bool:
    return _lower_text(plan) in PAID_PLAN_TYPES


def _payment_snapshot(account: Any) -> dict[str, Any]:
    extra = _get_extra(account)
    snapshot = extra.get("chatgpt_gopay")
    return snapshot if isinstance(snapshot, dict) else {}


def is_payment_succeeded_snapshot(snapshot: dict[str, Any] | None) -> bool:
    if not isinstance(snapshot, dict):
        return False
    phase = _lower_text(snapshot.get("phase"))
    status = _lower_text(snapshot.get("status"))
    result = snapshot.get("result") if isinstance(snapshot.get("result"), dict) else {}
    result_state = _lower_text(result.get("state"))
    return phase in PAYMENT_SUCCEEDED_PHASES or status in PAYMENT_SUCCEEDED_STATUSES or result_state in PAYMENT_SUCCEEDED_STATUSES


def is_payment_failed_snapshot(snapshot: dict[str, Any] | None) -> bool:
    if not isinstance(snapshot, dict):
        return False
    phase = _lower_text(snapshot.get("phase"))
    status = _lower_text(snapshot.get("status"))
    return phase in PAYMENT_FAILED_PHASES or status in PAYMENT_FAILED_STATUSES


def is_payment_active_snapshot(snapshot: dict[str, Any] | None) -> bool:
    if not isinstance(snapshot, dict):
        return False
    phase = _lower_text(snapshot.get("phase"))
    status = _lower_text(snapshot.get("status"))
    return phase in PAYMENT_ACTIVE_PHASES or status in PAYMENT_ACTIVE_STATUSES


def has_payment_success_marker(account: Any) -> bool:
    return is_payment_succeeded_snapshot(_payment_snapshot(account))


def has_payment_pending_marker(account: Any) -> bool:
    if is_payment_active_snapshot(_payment_snapshot(account)):
        return True
    extra = _get_extra(account)
    last_payment = extra.get("chatgpt_last_payment_link")
    if isinstance(last_payment, dict) and _truthy_text(last_payment.get("url")):
        return True
    return _truthy_text(getattr(account, "cashier_url", "")) or _truthy_text(extra.get("cashier_url"))


def mark_payment_pending(account: Any, *, reason: str = "") -> str:
    current_status = str(getattr(account, "status", "") or "").strip()
    if current_status in {INVALID_ACCOUNT_STATUS, SUBSCRIBED_ACCOUNT_STATUS}:
        return current_status
    setattr(account, "status", PENDING_PAYMENT_STATUS)
    return PENDING_PAYMENT_STATUS


def mark_payment_failed(account: Any, *, reason: str = "") -> str:
    current_status = str(getattr(account, "status", "") or "").strip()
    if current_status in {INVALID_ACCOUNT_STATUS, SUBSCRIBED_ACCOUNT_STATUS}:
        return current_status
    setattr(account, "status", PAYMENT_FAILED_STATUS)
    return PAYMENT_FAILED_STATUS


def mark_payment_succeeded(account: Any, *, reason: str = "") -> str:
    setattr(account, "status", SUBSCRIBED_ACCOUNT_STATUS)
    return SUBSCRIBED_ACCOUNT_STATUS


def apply_payment_snapshot_status(account: Any, snapshot: dict[str, Any] | None) -> str:
    if is_payment_succeeded_snapshot(snapshot):
        return mark_payment_succeeded(account, reason="payment_snapshot_succeeded")
    if is_payment_failed_snapshot(snapshot):
        return mark_payment_failed(account, reason="payment_snapshot_failed")
    if is_payment_active_snapshot(snapshot):
        return mark_payment_pending(account, reason="payment_snapshot_active")
    return str(getattr(account, "status", "") or "").strip()


def apply_auth_capture_status(account: Any, captured_status: Any) -> str:
    """Merge a registration/auth-capture status without erasing payment intent."""
    current_status = str(getattr(account, "status", "") or "").strip()
    next_status = _lower_text(captured_status)
    if not next_status:
        return current_status

    if next_status == INVALID_ACCOUNT_STATUS:
        setattr(account, "status", INVALID_ACCOUNT_STATUS)
        return INVALID_ACCOUNT_STATUS
    if current_status == SUBSCRIBED_ACCOUNT_STATUS or has_payment_success_marker(account):
        setattr(account, "status", SUBSCRIBED_ACCOUNT_STATUS)
        return SUBSCRIBED_ACCOUNT_STATUS
    if next_status == SUBSCRIBED_ACCOUNT_STATUS:
        return mark_payment_succeeded(account, reason="auth_capture_subscribed")
    if current_status == PAYMENT_FAILED_STATUS:
        return PAYMENT_FAILED_STATUS
    if next_status == PAYMENT_FAILED_STATUS:
        setattr(account, "status", PAYMENT_FAILED_STATUS)
        return PAYMENT_FAILED_STATUS
    if next_status == PENDING_PAYMENT_STATUS:
        setattr(account, "status", PENDING_PAYMENT_STATUS)
        return PENDING_PAYMENT_STATUS
    if next_status == RECOVERED_ACCOUNT_STATUS:
        if current_status == PENDING_PAYMENT_STATUS and has_payment_pending_marker(account):
            return PENDING_PAYMENT_STATUS
        setattr(account, "status", RECOVERED_ACCOUNT_STATUS)
        return RECOVERED_ACCOUNT_STATUS

    setattr(account, "status", next_status)
    return next_status


def is_account_deactivated_message(error_code: Any = "", message: Any = "") -> bool:
    code = _lower_text(error_code)
    text = _lower_text(message)
    if code in {"account_deactivated", "account_deleted", "deactivated_workspace"}:
        return True
    markers = (
        "deleted or deactivated",
        "account has been deleted or deactivated",
        "you do not have an account because it has been deleted or deactivated",
        "deactivated_workspace",
    )
    return any(marker in text for marker in markers)


def classify_local_probe_state(probe: dict[str, Any] | None) -> str:
    if not isinstance(probe, dict):
        return ""

    auth = probe.get("auth") if isinstance(probe.get("auth"), dict) else {}
    codex = probe.get("codex") if isinstance(probe.get("codex"), dict) else {}

    auth_state = _lower_text(auth.get("state"))
    auth_status = int(auth.get("http_status") or 0)
    auth_error_code = auth.get("error_code")
    auth_message = auth.get("message")

    if auth_status == 401 or auth_state in {
        "refresh_token_invalidated",
        "access_token_invalidated",
        "unauthorized",
    }:
        return "auth_401"
    if is_account_deactivated_message(auth_error_code, auth_message):
        return "auth_deactivated"
    if auth_status == 403 and auth_state in {"account_deactivated", "banned_like"}:
        return "auth_403"

    codex_state = _lower_text(codex.get("state"))
    codex_status = int(codex.get("http_status") or 0)
    codex_error_code = codex.get("error_code")
    codex_message = codex.get("message")

    if codex_status == 401 or codex_state in {"refresh_token_invalidated", "access_token_invalidated", "unauthorized"}:
        return "codex_401"
    if is_account_deactivated_message(codex_error_code, codex_message):
        return "codex_deactivated"
    if codex_status == 403 and codex_state == "account_deactivated":
        return "codex_403"
    return ""


def classify_remote_sync_state(sync: dict[str, Any] | None) -> str:
    if not isinstance(sync, dict):
        return ""

    remote_state = _lower_text(sync.get("remote_state"))
    status_code = int(sync.get("last_probe_status_code") or 0)
    error_code = sync.get("last_probe_error_code")
    message = sync.get("last_probe_message") or sync.get("status_message") or sync.get("message")

    if status_code == 401 or remote_state in {"access_token_invalidated", "unauthorized"}:
        return "remote_401"
    if is_account_deactivated_message(error_code, message):
        return "remote_deactivated"
    if status_code == 403 and remote_state in {"account_deactivated", "banned_like"}:
        return "remote_403"

    return ""


def classify_chatgpt_capabilities(
    account: Any,
    *,
    local_probe: dict[str, Any] | None = None,
    remote_sync: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the derived ChatGPT/Codex capability state for display and upload gates."""
    extra = _get_extra(account)
    probe = local_probe if isinstance(local_probe, dict) else extra.get("chatgpt_local")
    if not isinstance(probe, dict):
        probe = {}
    auth = probe.get("auth") if isinstance(probe.get("auth"), dict) else {}
    subscription = probe.get("subscription") if isinstance(probe.get("subscription"), dict) else {}
    codex = probe.get("codex") if isinstance(probe.get("codex"), dict) else {}

    access_token = _first_text(extra.get("access_token"), getattr(account, "access_token", ""), getattr(account, "token", ""))
    refresh_token = _first_text(extra.get("refresh_token"), getattr(account, "refresh_token", ""))
    account_id = _first_text(
        getattr(account, "user_id", ""),
        extra.get("account_id"),
        subscription.get("chatgpt_account_id"),
        codex.get("chatgpt_account_id"),
    )
    workspace_id = _first_text(
        extra.get("workspace_id"),
        extra.get("organization_id"),
        getattr(account, "workspace_id", ""),
    )

    auth_state = _lower_text(auth.get("state"))
    auth_reason = classify_local_probe_state(probe) if probe else ""
    remote_reason = classify_remote_sync_state(remote_sync) if remote_sync else ""

    if auth_reason or remote_reason or auth_state in AUTH_INVALID_STATES:
        auth_level = "invalid"
    elif auth_state == "refresh_token_valid" or refresh_token:
        auth_level = "refresh_token"
    elif auth_state == "access_token_valid" or access_token:
        auth_level = "access_token_only"
    else:
        auth_level = "unknown"

    subscription_plan = _lower_text(subscription.get("plan")) or "unknown"
    subscription_checked = (
        auth_state in {"refresh_token_valid", "access_token_valid"}
        and isinstance(subscription, dict)
        and bool(subscription)
    )
    codex_probe_state = _lower_text(codex.get("state"))
    if auth_level == "invalid":
        codex_state = "invalid"
    elif not refresh_token:
        codex_state = "missing_refresh_token"
    elif not workspace_id:
        codex_state = "missing_workspace"
    elif codex_probe_state and codex_probe_state not in {"not_checked", "skipped_auth_invalid"}:
        codex_state = codex_probe_state
    else:
        codex_state = "unknown"

    if auth_level == "invalid":
        upload_gate = "blocked_auth_invalid"
    elif not refresh_token:
        upload_gate = "blocked_missing_rt"
    elif not account_id or not workspace_id:
        upload_gate = "blocked_missing_workspace"
    else:
        upload_gate = "ready"

    return {
        "auth_level": auth_level,
        "has_access_token": bool(access_token),
        "has_refresh_token": bool(refresh_token),
        "has_account_id": bool(account_id),
        "has_workspace": bool(workspace_id),
        "account_id": account_id,
        "workspace_id": workspace_id,
        "subscription_plan": subscription_plan,
        "has_paid_subscription": is_paid_subscription_plan(subscription_plan),
        "subscription_checked": subscription_checked,
        "has_payment_success_marker": has_payment_success_marker(account),
        "has_payment_pending_marker": has_payment_pending_marker(account),
        "codex_state": codex_state,
        "upload_gate": upload_gate,
    }


def chatgpt_upload_gate_message(capabilities: dict[str, Any]) -> str:
    gate = str((capabilities or {}).get("upload_gate") or "").strip()
    if gate == "ready":
        return ""
    if gate == "blocked_missing_rt":
        return "跳过上传：待支付/仅 AT 账号缺少 refresh_token"
    if gate == "blocked_missing_workspace":
        return "跳过上传：缺少 workspace/account_id，无法作为 Codex 账号使用"
    if gate == "blocked_auth_invalid":
        return "跳过上传：本地认证已失效"
    return "跳过上传：账号材料不完整"


def is_chatgpt_upload_ready(account: Any, *, local_probe: dict[str, Any] | None = None) -> tuple[bool, str, dict[str, Any]]:
    capabilities = classify_chatgpt_capabilities(account, local_probe=local_probe)
    ok = str(capabilities.get("upload_gate") or "") == "ready"
    return ok, "" if ok else chatgpt_upload_gate_message(capabilities), capabilities


def _status_from_capabilities(account: Any, capabilities: dict[str, Any]) -> str:
    current_status = str(getattr(account, "status", "") or "").strip()
    upload_gate = str(capabilities.get("upload_gate") or "").strip()
    auth_level = str(capabilities.get("auth_level") or "").strip()
    plan = _lower_text(capabilities.get("subscription_plan"))
    has_paid_subscription = bool(capabilities.get("has_paid_subscription")) or is_paid_subscription_plan(plan)
    subscription_checked = bool(capabilities.get("subscription_checked"))
    has_success_marker = bool(capabilities.get("has_payment_success_marker"))
    has_pending_marker = bool(capabilities.get("has_payment_pending_marker"))

    if auth_level == "invalid" or upload_gate == "blocked_auth_invalid":
        return INVALID_ACCOUNT_STATUS
    if has_paid_subscription or has_success_marker:
        return SUBSCRIBED_ACCOUNT_STATUS
    if current_status == SUBSCRIBED_ACCOUNT_STATUS:
        if subscription_checked:
            return PENDING_PAYMENT_STATUS if has_pending_marker else RECOVERED_ACCOUNT_STATUS
        return SUBSCRIBED_ACCOUNT_STATUS
    if current_status == PAYMENT_FAILED_STATUS and upload_gate != "ready":
        return PAYMENT_FAILED_STATUS
    if upload_gate == "blocked_missing_rt" and auth_level == "access_token_only":
        return PENDING_PAYMENT_STATUS
    if has_pending_marker:
        return PENDING_PAYMENT_STATUS
    if current_status in {INVALID_ACCOUNT_STATUS, PENDING_PAYMENT_STATUS, PAYMENT_FAILED_STATUS}:
        if upload_gate == "ready" and has_paid_subscription:
            return SUBSCRIBED_ACCOUNT_STATUS
        if upload_gate == "ready":
            return RECOVERED_ACCOUNT_STATUS
    return current_status


def apply_chatgpt_status_policy(
    account: Any,
    *,
    local_probe: dict[str, Any] | None = None,
    remote_sync: dict[str, Any] | None = None,
) -> str:
    reason = classify_local_probe_state(local_probe) or classify_remote_sync_state(remote_sync)
    if reason:
        setattr(account, "status", INVALID_ACCOUNT_STATUS)
    else:
        capabilities = classify_chatgpt_capabilities(account, local_probe=local_probe, remote_sync=remote_sync)
        next_status = _status_from_capabilities(account, capabilities)
        if next_status:
            setattr(account, "status", next_status)
    return reason
