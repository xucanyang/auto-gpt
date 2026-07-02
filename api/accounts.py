from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from fastapi.responses import StreamingResponse
from sqlmodel import Session, select, func
from pydantic import BaseModel
from core.db import AccountListStateModel, AccountModel, PendingBusinessInviteModel, get_session
from services.account_filters import (
    account_auth_type,
    account_base_query,
    account_revival_info,
    account_subscription_type,
    apply_account_list_state_filters,
    apply_account_list_state_sort,
    delete_account_list_state_for_account_ids,
    refresh_stale_account_list_state,
    should_use_account_list_state,
    upsert_account_list_state_for_account_ids,
)
from services.account_rate_limit_recovery import (
    RATE_LIMITED_STATUS,
    account_rate_limit_payload,
    clear_account_rate_limit,
    mark_account_rate_limited,
    reconcile_rate_limited_accounts,
)
from services.chatgpt_account_state import classify_chatgpt_capabilities
from services.chatgpt_core.bound_phone import chatgpt_bound_phone_payload, chatgpt_phone_challenge_payload
from services.chatgpt_core.local_status_refresh import schedule_chatgpt_local_status_refresh_for_account_id
from services.team_lite import team_lite_service
from typing import Any, Optional
from datetime import datetime, timezone
import io, csv, json, logging, threading, time

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/accounts", tags=["accounts"])

_LIST_RATE_LIMIT_RECONCILE_SECONDS = 30
_list_rate_limit_reconcile_lock = threading.Lock()
_list_rate_limit_reconcile_at: dict[str, float] = {}


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _safe_str(value: Any) -> str:
    return str(value or "").strip()


def _parse_optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if not text:
        return None
    if text in {"1", "true", "yes", "on", "used"}:
        return True
    if text in {"0", "false", "no", "off", "unused"}:
        return False
    return None


def _safe_extra(account: AccountModel) -> dict[str, Any]:
    try:
        extra = account.get_extra()
    except Exception:
        return {}
    return extra if isinstance(extra, dict) else {}


def _split_filter_values(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, (list, tuple, set)):
        result: set[str] = set()
        for item in value:
            result.update(_split_filter_values(item))
        return result
    return {item.strip().lower() for item in str(value).split(",") if item.strip()}


def _account_count_query(*, platform: Optional[str] = None, status: Any = None, email: Optional[str] = None):
    query = select(func.count(AccountModel.id))
    platform_value = _safe_str(platform)
    if platform_value:
        query = query.where(AccountModel.platform == platform_value)

    status_values = _split_filter_values(status)
    if len(status_values) == 1:
        query = query.where(AccountModel.status == next(iter(status_values)))
    elif len(status_values) > 1:
        query = query.where(AccountModel.status.in_(sorted(status_values)))

    email_value = _safe_str(email)
    if email_value:
        query = query.where(AccountModel.email.contains(email_value))
    return query


def _maybe_reconcile_rate_limited_accounts(session: Session, *, platform: Optional[str] = None) -> None:
    """Keep legacy list-side recovery but avoid doing it for every list GET."""

    key = _safe_str(platform) or "*"
    now = time.monotonic()
    with _list_rate_limit_reconcile_lock:
        last = _list_rate_limit_reconcile_at.get(key, 0)
        if now - last < _LIST_RATE_LIMIT_RECONCILE_SECONDS:
            return
        _list_rate_limit_reconcile_at[key] = now
    reconcile_rate_limited_accounts(session, platform=platform)


def _is_team_invite_source_visible(*, workspace_scope: str, invite_status: str, team_id: int) -> bool:
    """Return whether an account should expose Team invite/removal metadata.

    Free workspace rows may still have historical PendingBusinessInvite rows because
    the same registration flow used the pending table as a staging area.  Those
    rows must not make the UI show "移除队伍" for a free-only account.
    """
    scope = _safe_str(workspace_scope).lower()
    status = _safe_str(invite_status).lower()
    if scope in {"business", "pending_activation"}:
        return True
    if team_id > 0 and status and status not in {"completed", "abandoned", "failed", "failed_terminal"}:
        return True
    return False


def _is_team_invite_source_removable(*, workspace_scope: str, invite_status: str, team_id: int, removed_from_team_at: str) -> bool:
    if team_id <= 0 or _safe_str(removed_from_team_at):
        return False
    return _is_team_invite_source_visible(
        workspace_scope=workspace_scope,
        invite_status=invite_status,
        team_id=team_id,
    )


def _serialize_account(account: AccountModel, *, team_invite_source: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    data = account.model_dump(mode="json") if hasattr(account, "model_dump") else account.dict()
    extra = _safe_extra(account)
    rate_limit = account_rate_limit_payload(account, extra=extra)
    data["rate_limit"] = rate_limit
    data["rate_limit_started_at"] = rate_limit["started_at"]
    data["rate_limit_recover_at"] = rate_limit["recover_at"]
    data["rate_limit_previous_status"] = rate_limit["previous_status"]
    data["revival"] = account_revival_info(account, extra)
    bound_phone = chatgpt_bound_phone_payload(extra)
    phone_challenge = chatgpt_phone_challenge_payload(extra)
    data["bound_phone"] = bound_phone
    data["bound_phone_number"] = _safe_str(bound_phone.get("phone") or bound_phone.get("phone_number"))
    data["bound_phone_masked"] = _safe_str(bound_phone.get("masked") or bound_phone.get("masked_phone"))
    data["phone_challenge"] = phone_challenge
    if team_invite_source:
        data["team_invite_source"] = team_invite_source
    return data


def _serialize_account_list_item(
    account: AccountModel,
    *,
    team_invite_source: Optional[dict[str, Any]] = None,
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Backward-compatible name for the compact list serializer."""

    return _serialize_account_compact_item(
        account,
        extra=extra,
        team_invite_source=team_invite_source,
    )


def _build_team_invite_sources(
    accounts: list[AccountModel],
    session: Session,
    *,
    include_team_brief: bool = True,
) -> dict[int, dict[str, Any]]:
    chatgpt_accounts = [account for account in accounts if account.platform == "chatgpt" and int(account.id or 0) > 0]
    if not chatgpt_accounts:
        return {}

    account_ids = [int(account.id or 0) for account in chatgpt_accounts]
    pending_rows = session.exec(
        select(PendingBusinessInviteModel).where(PendingBusinessInviteModel.account_id.in_(account_ids))
    ).all()
    pending_by_account = {
        int(row.account_id or 0): row
        for row in pending_rows
        if int(row.account_id or 0) > 0
    }

    sources: dict[int, dict[str, Any]] = {}
    team_ids: list[int] = []
    seen_team_ids: set[int] = set()

    for account in chatgpt_accounts:
        account_id = int(account.id or 0)
        extra = account.get_extra()
        pending_payload = dict(extra.get("chatgpt_pending_business_invite") or {})
        pending_row = pending_by_account.get(account_id)

        team_id = _safe_int(getattr(pending_row, "team_id", 0) if pending_row else pending_payload.get("team_id"))
        invite_status = _safe_str(getattr(pending_row, "status", "") if pending_row else pending_payload.get("status"))
        workspace_scope = _safe_str(extra.get("chatgpt_workspace_scope"))
        team_name = _safe_str(getattr(pending_row, "team_name", "") if pending_row else pending_payload.get("team_name"))
        invited_at = _safe_str(getattr(pending_row, "invited_at", "") if pending_row else pending_payload.get("invite_sent_at") or pending_payload.get("invited_at"))
        joined_at = _safe_str(getattr(pending_row, "joined_at", "") if pending_row else pending_payload.get("joined_at"))
        removed_from_team_at = _safe_str(extra.get("chatgpt_team_invite_removed_at"))

        if not _is_team_invite_source_visible(
            workspace_scope=workspace_scope,
            invite_status=invite_status,
            team_id=team_id,
        ):
            continue

        source = {
            "team_id": team_id,
            "team_name": team_name,
            "invite_status": invite_status,
            "workspace_scope": workspace_scope,
            "invited_at": invited_at,
            "joined_at": joined_at,
            "removed_from_team_at": removed_from_team_at,
            "removable": _is_team_invite_source_removable(
                workspace_scope=workspace_scope,
                invite_status=invite_status,
                team_id=team_id,
                removed_from_team_at=removed_from_team_at,
            ),
        }
        sources[account_id] = source
        if team_id > 0 and team_id not in seen_team_ids:
            seen_team_ids.add(team_id)
            team_ids.append(team_id)

    if not sources or not include_team_brief:
        return {}

    team_briefs = team_lite_service.get_team_db_briefs(team_ids)
    for source in sources.values():
        team_id = _safe_int(source.get("team_id"))
        if team_id <= 0:
            continue
        team_brief = team_briefs.get(team_id) or {}
        primary_account = dict(team_brief.get("primary_account") or {})
        source.update(
            {
                "team_email": _safe_str(team_brief.get("email")),
                "team_account_id": _safe_str(team_brief.get("account_id")),
                "team_status": _safe_str(team_brief.get("status")),
                "primary_account_id": _safe_str(primary_account.get("account_id")),
                "primary_account_name": _safe_str(primary_account.get("account_name")),
            }
        )
        if not source.get("team_name"):
            source["team_name"] = _safe_str(team_brief.get("team_name"))

    return sources


def _build_team_invite_source_summaries(
    accounts: list[AccountModel],
    session: Session,
    *,
    extras_by_id: Optional[dict[int, dict[str, Any]]] = None,
) -> dict[int, dict[str, Any]]:
    chatgpt_accounts = [account for account in accounts if account.platform == "chatgpt" and int(account.id or 0) > 0]
    if not chatgpt_accounts:
        return {}

    account_ids = [int(account.id or 0) for account in chatgpt_accounts]
    pending_rows = session.exec(
        select(PendingBusinessInviteModel).where(PendingBusinessInviteModel.account_id.in_(account_ids))
    ).all()
    pending_by_account = {
        int(row.account_id or 0): row
        for row in pending_rows
        if int(row.account_id or 0) > 0
    }

    sources: dict[int, dict[str, Any]] = {}
    for account in chatgpt_accounts:
        account_id = int(account.id or 0)
        extra = (
            extras_by_id.get(account_id)
            if isinstance(extras_by_id, dict) and isinstance(extras_by_id.get(account_id), dict)
            else _safe_extra(account)
        )
        pending_payload = dict(extra.get("chatgpt_pending_business_invite") or {})
        pending_row = pending_by_account.get(account_id)

        team_id = _safe_int(getattr(pending_row, "team_id", 0) if pending_row else pending_payload.get("team_id"))
        invite_status = _safe_str(getattr(pending_row, "status", "") if pending_row else pending_payload.get("status"))
        workspace_scope = _safe_str(extra.get("chatgpt_workspace_scope"))
        team_name = _safe_str(getattr(pending_row, "team_name", "") if pending_row else pending_payload.get("team_name"))
        invited_at = _safe_str(getattr(pending_row, "invited_at", "") if pending_row else pending_payload.get("invite_sent_at") or pending_payload.get("invited_at"))
        joined_at = _safe_str(getattr(pending_row, "joined_at", "") if pending_row else pending_payload.get("joined_at"))
        removed_from_team_at = _safe_str(extra.get("chatgpt_team_invite_removed_at"))

        if not _is_team_invite_source_visible(
            workspace_scope=workspace_scope,
            invite_status=invite_status,
            team_id=team_id,
        ):
            continue

        sources[account_id] = {
            "team_id": team_id,
            "team_name": team_name,
            "invite_status": invite_status,
            "workspace_scope": workspace_scope,
            "invited_at": invited_at,
            "joined_at": joined_at,
            "removed_from_team_at": removed_from_team_at,
            "removable": _is_team_invite_source_removable(
                workspace_scope=workspace_scope,
                invite_status=invite_status,
                team_id=team_id,
                removed_from_team_at=removed_from_team_at,
            ),
        }
    return sources


class AccountCreate(BaseModel):
    platform: str
    email: str
    password: str
    status: str = "registered"
    token: str = ""
    cashier_url: str = ""


class AccountUpdate(BaseModel):
    status: Optional[str] = None
    token: Optional[str] = None
    cashier_url: Optional[str] = None


class AccountMarkUsedRequest(BaseModel):
    used: bool = True


class ImportRequest(BaseModel):
    platform: str
    lines: list[str]


class BatchDeleteRequest(BaseModel):
    ids: list[int]


class BatchDeleteByFilterRequest(BaseModel):
    platform: Optional[str] = None
    status: Optional[str] = None
    email: Optional[str] = None


class AccountSnapshotRequest(BaseModel):
    ids: list[int]


def _safe_get_extra(account: AccountModel) -> dict[str, Any]:
    try:
        extra = account.get_extra()
    except Exception:
        return {}
    return extra if isinstance(extra, dict) else {}


def _iso_datetime(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return _safe_str(value)


def _has_secret_value(value: Any) -> bool:
    return bool(_safe_str(value))


def _first_secret(account: AccountModel, extra: dict[str, Any], *names: str) -> str:
    for name in names:
        if name == "access_token":
            value = (
                extra.get("access_token")
                or extra.get("accessToken")
                or extra.get("webAccessToken")
                or getattr(account, "token", "")
            )
        elif name == "refresh_token":
            value = extra.get("refresh_token") or extra.get("refreshToken")
        elif name == "password":
            value = getattr(account, "password", "")
        elif name == "token":
            value = getattr(account, "token", "")
        else:
            value = extra.get(name) or getattr(account, name, "")
        text = _safe_str(value)
        if text:
            return text
    return ""


def _pick_fields(source: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {key: source.get(key) for key in fields if key in source}


def _build_sync_summary(sync: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(sync, dict) or not sync:
        return {}
    last_upload = sync.get("last_upload") if isinstance(sync.get("last_upload"), dict) else {}
    return _pick_fields(
        sync,
        (
            "remote_state",
            "status",
            "uploaded",
            "uploaded_at",
            "last_attempt_at",
            "checked_at",
            "last_synced_at",
            "last_refresh",
            "next_retry_after",
            "last_probe_status_code",
            "last_probe_error_code",
            "last_probe_message",
            "status_message",
            "message",
            "last_message",
            "matched_by",
            "probe_source",
            "candidate_count",
            "remote_account_id",
            "name",
            "base_url",
        ),
    ) | (
        {
            "last_upload": _pick_fields(
                last_upload,
                (
                    "status",
                    "action",
                    "message",
                    "attempted_at",
                    "finished_at",
                    "remote_account_id",
                    "remote_status",
                    "probe_before",
                    "probe_after",
                ),
            )
        }
        if last_upload
        else {}
    )


def _build_baxigpt_cdk_summary(cdk: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(cdk, dict) or not cdk:
        return {}
    return _pick_fields(
        cdk,
        (
            "status",
            "upstream_status",
            "code_masked",
            "cdk_id",
            "order_id",
            "display_id",
            "remote_email",
            "task_id",
            "bound_at",
            "submitted_at",
            "paid_at",
            "last_checked_at",
            "last_error_message",
        ),
    )


def _build_subscription_summary(
    subscription: dict[str, Any],
    capabilities: dict[str, Any],
    extra: dict[str, Any],
) -> dict[str, Any]:
    return {
        "plan": _safe_str(
            capabilities.get("subscription_plan")
            or subscription.get("plan")
            or extra.get("chatgpt_plan_type")
            or extra.get("chatgpt_subscription_plan")
        ),
        "workspace_plan_type": _safe_str(subscription.get("workspace_plan_type")),
        "active_until": _safe_str(
            subscription.get("subscription_active_until")
            or subscription.get("subscription_expires_at_iso")
            or subscription.get("subscription_expires_at")
            or extra.get("subscription_active_until")
            or extra.get("subscription_expires_at")
            or extra.get("chatgpt_subscription_active_until")
        ),
        "checked_at": _safe_str(subscription.get("checked_at")),
        "source": _safe_str(subscription.get("source")),
        "has_paid_subscription": bool(capabilities.get("has_paid_subscription")),
        "subscription_checked": bool(capabilities.get("subscription_checked")),
    }


def _build_codex_summary(codex: dict[str, Any], capabilities: dict[str, Any]) -> dict[str, Any]:
    usage = codex.get("usage") if isinstance(codex.get("usage"), dict) else {}
    return {
        "state": _safe_str(codex.get("state") or capabilities.get("codex_state")),
        "checked_at": _safe_str(codex.get("checked_at")),
        "source": _safe_str(codex.get("source")),
        "http_status": _safe_int(codex.get("http_status")),
        "error_code": _safe_str(codex.get("error_code")),
        "message": _safe_str(codex.get("message")),
        "chatgpt_account_id": _safe_str(codex.get("chatgpt_account_id") or capabilities.get("account_id")),
        "usage": _pick_fields(
            usage,
            (
                "codex_usage_updated_at",
                "codex_5h_used_percent",
                "codex_5h_reset_after_seconds",
                "codex_5h_reset_at",
                "codex_5h_window_minutes",
                "codex_7d_used_percent",
                "codex_7d_reset_after_seconds",
                "codex_7d_reset_at",
                "codex_7d_window_minutes",
                "codex_primary_used_percent",
                "codex_primary_reset_after_seconds",
                "codex_primary_window_minutes",
                "codex_secondary_used_percent",
                "codex_secondary_reset_after_seconds",
                "codex_secondary_window_minutes",
                "codex_primary_over_secondary_percent",
            ),
        ),
    }


def _build_auth_summary(
    account: AccountModel,
    extra: dict[str, Any],
    auth: dict[str, Any],
    capabilities: dict[str, Any],
) -> dict[str, Any]:
    has_access_token = _has_secret_value(_first_secret(account, extra, "access_token"))
    has_refresh_token = _has_secret_value(_first_secret(account, extra, "refresh_token"))
    has_session_token = _has_secret_value(_first_secret(account, extra, "session_token"))
    return {
        "level": _safe_str(capabilities.get("auth_level") or extra.get("auth_level")),
        "state": _safe_str(auth.get("state")),
        "checked_at": _safe_str(auth.get("checked_at")),
        "source": _safe_str(auth.get("source") or extra.get("chatgpt_token_source")),
        "http_status": _safe_int(auth.get("http_status")),
        "error_code": _safe_str(auth.get("error_code")),
        "message": _safe_str(auth.get("message")),
        "upload_gate": _safe_str(capabilities.get("upload_gate")),
        "has_access_token": has_access_token,
        "has_refresh_token": has_refresh_token,
        "has_session_token": has_session_token,
        "password_present": _has_secret_value(account.password),
    }


def _build_account_validity_summary(
    account: AccountModel,
    auth_summary: dict[str, Any],
    capabilities: dict[str, Any],
) -> dict[str, Any]:
    auth_level = _safe_str(auth_summary.get("level") or capabilities.get("auth_level")).lower()
    upload_gate = _safe_str(capabilities.get("upload_gate")).lower()
    auth_state = _safe_str(auth_summary.get("state")).lower()
    invalid_states = {
        "refresh_token_invalidated",
        "access_token_invalidated",
        "unauthorized",
        "account_deactivated",
        "banned_like",
        "invalid",
    }
    valid = not (
        _safe_str(account.status).lower() == "invalid"
        or auth_level == "invalid"
        or upload_gate == "blocked_auth_invalid"
        or auth_state in invalid_states
    )
    reason = ""
    if not valid:
        reason = "auth_invalid" if auth_level == "invalid" or auth_state in invalid_states else "status_invalid"
    return {"state": "valid" if valid else "invalid", "valid": valid, "reason": reason}


def _build_phone_summary(
    phone_binding: dict[str, Any],
    bound_phone: dict[str, Any],
    phone_challenge: dict[str, Any],
) -> dict[str, Any]:
    return {
        "binding": _pick_fields(
            phone_binding,
            (
                "phone",
                "api_url",
                "status",
                "status_label",
                "api_expired_date",
                "code_time",
                "code_extracted",
                "bound_at",
                "task_id",
                "source",
            ),
        ),
        "bound": bound_phone,
        "challenge": phone_challenge,
    }


def _serialize_account_compact_item(
    account: AccountModel,
    *,
    extra: dict[str, Any] | None = None,
    team_invite_source: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    extra = extra if isinstance(extra, dict) else _safe_get_extra(account)
    sync_statuses = extra.get("sync_statuses") if isinstance(extra.get("sync_statuses"), dict) else {}
    chatgpt_local = extra.get("chatgpt_local") if isinstance(extra.get("chatgpt_local"), dict) else {}
    auth = chatgpt_local.get("auth") if isinstance(chatgpt_local.get("auth"), dict) else {}
    chatgpt_subscription = chatgpt_local.get("subscription") if isinstance(chatgpt_local.get("subscription"), dict) else {}
    codex = chatgpt_local.get("codex") if isinstance(chatgpt_local.get("codex"), dict) else {}
    chatgpt_capabilities = extra.get("chatgpt_capabilities") if isinstance(extra.get("chatgpt_capabilities"), dict) else {}
    if account.platform == "chatgpt" and not chatgpt_capabilities:
        # Older rows may have tokens/workspace IDs but no derived capability snapshot yet.
        chatgpt_capabilities = classify_chatgpt_capabilities(account, local_probe=chatgpt_local)

    phone_binding = extra.get("chatgpt_phone_binding") if isinstance(extra.get("chatgpt_phone_binding"), dict) else {}
    bound_phone = chatgpt_bound_phone_payload(extra)
    phone_challenge = chatgpt_phone_challenge_payload(extra)
    rate_limit = account_rate_limit_payload(account, extra=extra)
    revival = account_revival_info(account, extra)
    sub2api_sync = _build_sync_summary(sync_statuses.get("sub2api") if isinstance(sync_statuses.get("sub2api"), dict) else {})
    oaipay_sync = _build_sync_summary(sync_statuses.get("oaipay") if isinstance(sync_statuses.get("oaipay"), dict) else {})
    cliproxy_sync = _build_sync_summary(sync_statuses.get("cliproxyapi") if isinstance(sync_statuses.get("cliproxyapi"), dict) else {})
    auth_summary = _build_auth_summary(account, extra, auth, chatgpt_capabilities)
    subscription_summary = _build_subscription_summary(chatgpt_subscription, chatgpt_capabilities, extra)
    codex_summary = _build_codex_summary(codex, chatgpt_capabilities)
    validity_summary = _build_account_validity_summary(account, auth_summary, chatgpt_capabilities)
    baxigpt_cdk = _build_baxigpt_cdk_summary(extra.get("baxigpt_cdk") if isinstance(extra.get("baxigpt_cdk"), dict) else {})

    payload = {
        "id": account.id,
        "platform": account.platform,
        "email": account.email,
        "token": account.token,
        "access_token": account.token,
        "refresh_token": _safe_str(extra.get("refresh_token")),
        "status": account.status,
        "created_at": _iso_datetime(account.created_at),
        "updated_at": _iso_datetime(account.updated_at),
        "user_id": account.user_id,
        "region": account.region,
        "cashier_url": account.cashier_url,
        "manually_used": bool(extra.get("manually_used")),
        "workspace": {
            "scope": _safe_str(extra.get("chatgpt_workspace_scope")),
            "label": _safe_str(extra.get("chatgpt_workspace_label")),
            "display_name": _safe_str(extra.get("chatgpt_workspace_display_name")),
            "id": _safe_str(extra.get("workspace_id") or extra.get("organization_id") or chatgpt_capabilities.get("workspace_id")),
            "account_id": _safe_str(chatgpt_capabilities.get("account_id") or account.user_id),
        },
        "workspace_scope": _safe_str(extra.get("chatgpt_workspace_scope")),
        "workspace_label": _safe_str(extra.get("chatgpt_workspace_label")),
        "workspace_display_name": _safe_str(extra.get("chatgpt_workspace_display_name")),
        "auth": auth_summary,
        "subscription": subscription_summary,
        "account_validity": _safe_str(validity_summary.get("state")),
        "account_validity_summary": validity_summary,
        "auth_type": account_auth_type(account, extra),
        "subscription_type": account_subscription_type(account, extra),
        "codex": codex_summary,
        "sub2api": sub2api_sync,
        "oaipay": oaipay_sync,
        "cliproxy": cliproxy_sync,
        "phone": _build_phone_summary(phone_binding, bound_phone, phone_challenge),
        "rate_limit": rate_limit,
        "rate_limit_started_at": rate_limit["started_at"],
        "rate_limit_recover_at": rate_limit["recover_at"],
        "rate_limit_previous_status": rate_limit["previous_status"],
        "revival": revival,
        "has_access_token": bool(auth_summary["has_access_token"]),
        "has_refresh_token": bool(auth_summary["has_refresh_token"]),
        "has_session_token": bool(auth_summary["has_session_token"]),
        "has_password": bool(auth_summary["password_present"]),
        "password_present": bool(auth_summary["password_present"]),
        "auth_level": _safe_str(auth_summary.get("level")),
        "subscription_plan": _safe_str(subscription_summary.get("plan")),
        "subscription_active_until": _safe_str(subscription_summary.get("active_until")),
        "codex_state": _safe_str(codex_summary.get("state")),
        "cliproxy_remote_state": _safe_str(cliproxy_sync.get("remote_state")),
        "sub2api_remote_state": _safe_str(sub2api_sync.get("remote_state")),
        "oaipay_remote_state": _safe_str(oaipay_sync.get("remote_state")),
        "team_invite_status": _safe_str(team_invite_source.get("invite_status") if team_invite_source else ""),
        # Backward-compatible summary aliases: keep object names that the list UI
        # already reads, but make them compact rather than returning full nested
        # probes / sync records / token-bearing extra.
        "chatgptLocal": {
            "auth": auth_summary,
            "subscription": {
                "plan": subscription_summary["plan"],
                "workspace_plan_type": subscription_summary["workspace_plan_type"],
                "subscription_active_until": subscription_summary["active_until"],
                "checked_at": subscription_summary["checked_at"],
                "source": subscription_summary["source"],
            },
            "codex": codex_summary,
        },
        "chatgptCapabilities": {
            key: chatgpt_capabilities.get(key)
            for key in (
                "auth_level",
                "has_access_token",
                "has_refresh_token",
                "has_account_id",
                "has_workspace",
                "account_id",
                "workspace_id",
                "subscription_plan",
                "has_paid_subscription",
                "subscription_checked",
                "codex_state",
                "upload_gate",
            )
            if key in chatgpt_capabilities
        },
        "bound_phone": bound_phone,
        "bound_phone_number": _safe_str(bound_phone.get("phone") or bound_phone.get("phone_number")),
        "bound_phone_masked": _safe_str(bound_phone.get("masked") or bound_phone.get("masked_phone")),
        "phone_challenge": phone_challenge,
        "phone_binding": _build_phone_summary(phone_binding, bound_phone, phone_challenge)["binding"],
        "baxigpt_cdk": baxigpt_cdk,
        "sub2apiSync": sub2api_sync,
        "oaipaySync": oaipay_sync,
        "cliproxySync": cliproxy_sync,
        "extra": {
            "manually_used": bool(extra.get("manually_used")),
            "refresh_token": _safe_str(extra.get("refresh_token")),
            "access_token": _safe_str(extra.get("access_token") or account.token),
            "chatgpt_workspace_label": _safe_str(extra.get("chatgpt_workspace_label")),
            "chatgpt_workspace_scope": _safe_str(extra.get("chatgpt_workspace_scope")),
            "chatgpt_workspace_display_name": _safe_str(extra.get("chatgpt_workspace_display_name")),
            "chatgpt_phone_binding": _build_phone_summary(phone_binding, bound_phone, phone_challenge)["binding"],
            "chatgpt_bound_phone": bound_phone,
            "chatgpt_phone_challenge": phone_challenge,
            "baxigpt_cdk": baxigpt_cdk,
        },
    }
    if team_invite_source:
        payload["team_invite_source"] = team_invite_source
    return payload


_SECRET_FIELD_ALIASES = {
    "access_token": "access_token",
    "at": "access_token",
    "token": "access_token",
    "refresh_token": "refresh_token",
    "rt": "refresh_token",
    "password": "password",
    "session_token": "session_token",
}


def _parse_secret_fields(value: Any) -> list[str]:
    requested: list[str] = []
    seen: set[str] = set()
    for raw in str(value or "").split(","):
        normalized = _SECRET_FIELD_ALIASES.get(raw.strip().lower())
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        requested.append(normalized)
    return requested


@router.get("")
def list_accounts(
    platform: Optional[str] = None,
    status: Optional[str] = None,
    email: Optional[str] = None,
    manually_used: Optional[str] = None,
    auth_type: Optional[str] = None,
    subscription_type: Optional[str] = None,
    account_validity: Optional[str] = None,
    sub2api_state: Optional[str] = None,
    oaipay_state: Optional[str] = None,
    revival_state: Optional[str] = None,
    sort_by: Optional[str] = None,
    sort_order: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
    detail: bool = False,
    include_team_brief: bool = False,
    session: Session = Depends(get_session),
):
    _maybe_reconcile_rate_limited_accounts(session, platform=platform)
    page_value = max(1, int(page or 1))
    page_size_value = max(1, min(int(page_size or 20), 200))
    manually_used_filter = _parse_optional_bool(manually_used)
    use_list_state = should_use_account_list_state(
        manually_used=manually_used_filter,
        auth_type=auth_type,
        subscription_type=subscription_type,
        account_validity_filter=account_validity,
        sub2api_state=sub2api_state,
        oaipay_state=oaipay_state,
        revival_state=revival_state,
        sort_by=sort_by,
        sort_order=sort_order,
    )

    if use_list_state:
        refresh_stale_account_list_state(session, platform=platform)
        q = account_base_query(platform=platform, status=status, email=email).join(
            AccountListStateModel,
            AccountListStateModel.account_id == AccountModel.id,
        )
        q = apply_account_list_state_filters(
            q,
            manually_used=manually_used_filter,
            auth_type=auth_type,
            subscription_type=subscription_type,
            account_validity_filter=account_validity,
            sub2api_state=sub2api_state,
        oaipay_state=oaipay_state,
            revival_state=revival_state,
        )
        count_q = select(func.count()).select_from(q.subquery())
        total = int(session.exec(count_q).one())
        q = apply_account_list_state_sort(q, sort_by=sort_by, sort_order=sort_order)
        items = session.exec(q.offset((page_value - 1) * page_size_value).limit(page_size_value)).all()
    else:
        q = account_base_query(platform=platform, status=status, email=email).order_by(AccountModel.id.desc())
        total = int(session.exec(_account_count_query(platform=platform, status=status, email=email)).one())
        items = session.exec(q.offset((page_value - 1) * page_size_value).limit(page_size_value)).all()
    extras_by_id = {
        int(item.id or 0): _safe_extra(item)
        for item in items
        if int(item.id or 0) > 0
    }
    team_invite_sources = (
        _build_team_invite_sources(items, session, include_team_brief=True)
        if detail or include_team_brief
        else _build_team_invite_source_summaries(items, session, extras_by_id=extras_by_id)
    )
    return {
        "total": total,
        "page": page_value,
        "items": [
            (
                _serialize_account(item, team_invite_source=team_invite_sources.get(int(item.id or 0)))
                if detail
                else _serialize_account_compact_item(
                    item,
                    extra=extras_by_id.get(int(item.id or 0)),
                    team_invite_source=team_invite_sources.get(int(item.id or 0)),
                )
            )
            for item in items
        ],
    }


@router.post("")
def create_account(body: AccountCreate, session: Session = Depends(get_session)):
    acc = AccountModel(
        platform=body.platform,
        email=body.email,
        password=body.password,
        status=body.status,
        token=body.token,
        cashier_url=body.cashier_url,
    )
    if _safe_str(body.status).lower() == RATE_LIMITED_STATUS:
        mark_account_rate_limited(acc)
    session.add(acc)
    session.flush()
    upsert_account_list_state_for_account_ids(session, [acc.id], commit=False)
    session.commit()
    session.refresh(acc)
    if acc.platform == "chatgpt":
        schedule_chatgpt_local_status_refresh_for_account_id(acc.id, reason="account_create")
    return acc


@router.get("/stats")
def get_stats(session: Session = Depends(get_session)):
    """统计各平台账号数量和状态分布"""
    reconcile_rate_limited_accounts(session)
    accounts = session.exec(select(AccountModel)).all()
    platforms: dict = {}
    statuses: dict = {}
    for acc in accounts:
        platforms[acc.platform] = platforms.get(acc.platform, 0) + 1
        statuses[acc.status] = statuses.get(acc.status, 0) + 1
    return {"total": len(accounts), "by_platform": platforms, "by_status": statuses}


@router.get("/overview")
def get_accounts_overview(
    platform: Optional[str] = None,
    status: Optional[str] = None,
    email: Optional[str] = None,
    session: Session = Depends(get_session),
):
    reconcile_rate_limited_accounts(session, platform=platform)
    q = select(AccountModel)
    if platform:
        q = q.where(AccountModel.platform == platform)
    if status:
        q = q.where(AccountModel.status == status)
    if email:
        q = q.where(AccountModel.email.contains(email))

    accounts = session.exec(q).all()
    by_status: dict[str, int] = {}
    by_platform: dict[str, int] = {}
    manually_used = 0
    workspace_scope_counts: dict[str, int] = {}

    for acc in accounts:
        by_status[acc.status] = by_status.get(acc.status, 0) + 1
        by_platform[acc.platform] = by_platform.get(acc.platform, 0) + 1
        extra = acc.get_extra()
        if bool(extra.get("manually_used")):
            manually_used += 1
        scope = _safe_str(extra.get("chatgpt_workspace_scope"))
        if scope:
            workspace_scope_counts[scope] = workspace_scope_counts.get(scope, 0) + 1

    return {
        "total": len(accounts),
        "by_status": by_status,
        "by_platform": by_platform,
        "manually_used": manually_used,
        "workspace_scope_counts": workspace_scope_counts,
    }


@router.get("/export")
def export_accounts(
    platform: Optional[str] = None,
    status: Optional[str] = None,
    session: Session = Depends(get_session),
):
    reconcile_rate_limited_accounts(session, platform=platform)
    q = select(AccountModel)
    if platform:
        q = q.where(AccountModel.platform == platform)
    if status:
        q = q.where(AccountModel.status == status)
    accounts = session.exec(q).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["platform", "email", "password", "user_id", "region",
                     "status", "cashier_url", "created_at"])
    for acc in accounts:
        writer.writerow([acc.platform, acc.email, acc.password, acc.user_id,
                         acc.region, acc.status, acc.cashier_url,
                         acc.created_at.strftime("%Y-%m-%d %H:%M:%S")])
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=accounts.csv"}
    )


@router.post("/import")
def import_accounts(
    body: ImportRequest,
    session: Session = Depends(get_session),
):
    """批量导入，每行格式: email password [extra]"""
    created = 0
    created_accounts: list[AccountModel] = []
    created_chatgpt_accounts: list[AccountModel] = []
    for line in body.lines:
        parts = line.strip().split()
        if len(parts) < 2:
            continue
        email, password = parts[0], parts[1]
        extra = parts[2] if len(parts) > 2 else ""
        if extra:
            try:
                json.loads(extra)
            except (json.JSONDecodeError, ValueError):
                extra = "{}"
        else:
            extra = "{}"
        acc = AccountModel(platform=body.platform, email=email,
                           password=password, extra_json=extra)
        session.add(acc)
        created_accounts.append(acc)
        if str(body.platform or "").strip().lower() == "chatgpt":
            created_chatgpt_accounts.append(acc)
        created += 1
    if created_accounts:
        session.flush()
        upsert_account_list_state_for_account_ids(
            session,
            [acc.id for acc in created_accounts],
            commit=False,
        )
    session.commit()
    if created_chatgpt_accounts:
        for acc in created_chatgpt_accounts:
            schedule_chatgpt_local_status_refresh_for_account_id(acc.id, reason="account_import")
    return {"created": created}


@router.post("/batch-delete")
def batch_delete_accounts(
    body: BatchDeleteRequest,
    session: Session = Depends(get_session)
):
    """批量删除账号"""
    if not body.ids:
        raise HTTPException(400, "账号 ID 列表不能为空")
    
    if len(body.ids) > 1000:
        raise HTTPException(400, "单次最多删除 1000 个账号")
    
    deleted_count = 0
    deleted_ids: list[int] = []
    not_found_ids = []
    
    try:
        for account_id in body.ids:
            acc = session.get(AccountModel, account_id)
            if acc:
                deleted_ids.append(int(account_id))
                session.delete(acc)
                deleted_count += 1
            else:
                not_found_ids.append(account_id)

        delete_account_list_state_for_account_ids(session, deleted_ids, commit=False)
        session.commit()
        logger.info(f"批量删除成功: {deleted_count} 个账号")
        
        return {
            "deleted": deleted_count,
            "not_found": not_found_ids,
            "total_requested": len(body.ids)
        }
    except Exception as e:
        session.rollback()
        logger.exception("批量删除失败")
        raise HTTPException(500, f"批量删除失败: {str(e)}")


@router.post("/snapshot")
def snapshot_accounts(body: AccountSnapshotRequest, session: Session = Depends(get_session)):
    ids: list[int] = []
    seen: set[int] = set()
    for value in body.ids or []:
        account_id = _safe_int(value)
        if account_id <= 0 or account_id in seen:
            continue
        seen.add(account_id)
        ids.append(account_id)
    if len(ids) > 500:
        raise HTTPException(400, "单次最多读取 500 个账号快照")
    if not ids:
        return {"items": [], "total": 0}

    rows = session.exec(
        select(AccountModel)
        .where(AccountModel.id.in_(ids))
        .where(AccountModel.platform == "chatgpt")
    ).all()
    row_map = {int(row.id or 0): row for row in rows if int(row.id or 0) > 0}
    items: list[dict[str, Any]] = []
    for account_id in ids:
        account = row_map.get(account_id)
        if account is None:
            continue
        extra = account.get_extra()
        baxigpt_cdk = extra.get("baxigpt_cdk") if isinstance(extra.get("baxigpt_cdk"), dict) else {}
        items.append(
            {
                "id": int(account.id or 0),
                "status": str(account.status or ""),
                "updated_at": account.updated_at.isoformat() if account.updated_at else None,
                "baxigpt_cdk": baxigpt_cdk,
                "extra": {"baxigpt_cdk": baxigpt_cdk},
            }
        )
    return {"items": items, "total": len(items)}


@router.post("/check-all")
def check_all_accounts(platform: Optional[str] = None,
                       background_tasks: BackgroundTasks = None):
    from core.scheduler import scheduler
    background_tasks.add_task(scheduler.check_accounts_valid, platform)
    return {"message": "批量检测任务已启动"}


@router.post("/batch-delete-by-filter")
def batch_delete_accounts_by_filter(
    body: BatchDeleteByFilterRequest,
    session: Session = Depends(get_session),
):
    """按筛选条件批量删除账号。至少需要一个筛选条件。"""
    if not any([body.platform, body.status, body.email]):
        raise HTTPException(400, "至少需要一个筛选条件")

    reconcile_rate_limited_accounts(session, platform=body.platform)
    q = select(AccountModel)
    if body.platform:
        q = q.where(AccountModel.platform == body.platform)
    if body.status:
        q = q.where(AccountModel.status == body.status)
    if body.email:
        q = q.where(AccountModel.email.contains(body.email))

    accounts = session.exec(q).all()
    deleted_count = 0
    deleted_ids: list[int] = []

    try:
        for acc in accounts:
            if acc.id is None:
                continue
            deleted_ids.append(acc.id)
            session.delete(acc)
            deleted_count += 1

        delete_account_list_state_for_account_ids(session, deleted_ids, commit=False)
        session.commit()
        logger.info("按筛选条件批量删除成功: %s 个账号", deleted_count)
        filters = body.model_dump() if hasattr(body, "model_dump") else body.dict()
        return {
            "deleted": deleted_count,
            "deleted_ids": deleted_ids,
            "filters": filters,
        }
    except Exception as e:
        session.rollback()
        logger.exception("按筛选条件批量删除失败")
        raise HTTPException(500, f"按筛选条件批量删除失败: {str(e)}")


@router.get("/{account_id}")
def get_account(account_id: int, session: Session = Depends(get_session)):
    acc = session.get(AccountModel, account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    reconcile_rate_limited_accounts(session, accounts=[acc])
    session.refresh(acc)
    team_invite_source = _build_team_invite_sources([acc], session, include_team_brief=True).get(int(acc.id or 0))
    return _serialize_account(acc, team_invite_source=team_invite_source)


@router.get("/{account_id}/secrets")
def get_account_secrets(
    account_id: int,
    fields: str = "",
    session: Session = Depends(get_session),
):
    requested = _parse_secret_fields(fields)
    if not requested:
        raise HTTPException(400, "fields 至少需要一个有效字段")
    acc = session.get(AccountModel, account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")

    extra = _safe_get_extra(acc)
    values: dict[str, str] = {}
    for field in requested:
        if field == "access_token":
            values[field] = _first_secret(acc, extra, "access_token")
        elif field == "refresh_token":
            values[field] = _first_secret(acc, extra, "refresh_token")
        elif field == "session_token":
            values[field] = _first_secret(acc, extra, "session_token")
        elif field == "password":
            values[field] = _first_secret(acc, extra, "password")

    return {
        "account_id": int(acc.id or 0),
        "fields": requested,
        "secrets": values,
        "present": {field: bool(values.get(field)) for field in requested},
    }


@router.get("/{account_id}/team-source")
def get_account_team_source(account_id: int, session: Session = Depends(get_session)):
    acc = session.get(AccountModel, account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    team_invite_source = _build_team_invite_sources([acc], session, include_team_brief=True).get(int(acc.id or 0))
    return {
        "account_id": int(acc.id or 0),
        "team_invite_source": team_invite_source,
    }


@router.post("/{account_id}/chatgpt-team-remove")
def remove_chatgpt_team_member(account_id: int, session: Session = Depends(get_session)):
    acc = session.get(AccountModel, account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    if acc.platform != "chatgpt":
        raise HTTPException(400, "只有 ChatGPT 账号支持移除队伍")

    team_invite_source = _build_team_invite_sources([acc], session).get(int(acc.id or 0))
    if not team_invite_source:
        raise HTTPException(400, "当前账号没有 Team Invite 来源信息")

    team_id = _safe_int(team_invite_source.get("team_id"))
    if team_id <= 0:
        raise HTTPException(400, "当前账号未关联可操作的 Team")

    email = _safe_str(acc.email).lower()
    if not email:
        raise HTTPException(400, "当前账号缺少邮箱")

    try:
        member_result = team_lite_service.check_member(team_id, email, force=True)
    except Exception as exc:
        raise HTTPException(400, f"检查 Team 成员失败: {exc}") from exc

    member = dict(member_result.get("member") or {})
    member_status = _safe_str(member_result.get("status") or member.get("status")).lower()
    matched = bool(member_result.get("matched"))

    try:
        if matched and member_status == "joined":
            role = _safe_str(member.get("role")).lower()
            if role == "account-owner":
                raise HTTPException(400, "这是 Team 母号，不能直接从自己的 Team 中移除")
            user_id = _safe_str(member.get("user_id"))
            if not user_id:
                raise HTTPException(400, "命中了已加入成员，但缺少 user_id，无法删除")
            result = team_lite_service.delete_member(team_id, user_id)
            action = "delete_member"
            message_text = "已从 Team 中删除成员"
        elif matched and member_status == "invited":
            result = team_lite_service.revoke_invite(team_id, email)
            action = "revoke_invite"
            message_text = "已撤销 Team 邀请"
        elif _safe_str(team_invite_source.get("invite_status")) and _safe_str(team_invite_source.get("invite_status")) != "completed":
            result = team_lite_service.revoke_invite(team_id, email)
            action = "revoke_invite"
            message_text = "已按 pending invite 撤销 Team 邀请"
        else:
            # 如果 Team 已经没有该账号，视为“已移除”，记录本地移除时间以便前端更新按钮态
            action = "noop"
            result = None
            message_text = "Team 中未找到该账号，可能已经被移除"
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(400, f"移除队伍失败: {exc}") from exc

    extra = acc.get_extra()
    extra["chatgpt_team_invite_removed_at"] = datetime.now(timezone.utc).isoformat()
    acc.set_extra(extra)
    acc.updated_at = datetime.now(timezone.utc)
    session.add(acc)
    upsert_account_list_state_for_account_ids(session, [acc.id], commit=False)
    session.commit()
    session.refresh(acc)

    if action == "noop" and not team_invite_source.get("removed_from_team_at"):
        # 为了让前端“移除队伍”按钮立即消失，未匹配到成员时也直接写本地移除时间
        team_invite_source["removed_from_team_at"] = extra["chatgpt_team_invite_removed_at"]
        team_invite_source["removable"] = False

    refreshed_source = _build_team_invite_sources([acc], session).get(int(acc.id or 0)) or team_invite_source
    if not action == "noop":
        # 明确刷新成功执行动作后再同步一次，避免因列表查询延迟导致前端刷新后又出现按钮
        refreshed_source = _build_team_invite_sources([acc], session).get(int(acc.id or 0)) or team_invite_source
    return {
        "ok": True,
        "action": action,
        "message": message_text,
        "result": result,
        "team_invite_source": refreshed_source,
    }


@router.patch("/{account_id}")
def update_account(account_id: int, body: AccountUpdate,
                   session: Session = Depends(get_session)):
    acc = session.get(AccountModel, account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    if body.status is not None:
        next_status = _safe_str(body.status).lower()
        if next_status == RATE_LIMITED_STATUS:
            mark_account_rate_limited(acc, previous_status=acc.status)
        else:
            acc.status = body.status
            clear_account_rate_limit(acc)
    if body.token is not None:
        acc.token = body.token
    if body.cashier_url is not None:
        acc.cashier_url = body.cashier_url
    acc.updated_at = datetime.now(timezone.utc)
    session.add(acc)
    upsert_account_list_state_for_account_ids(session, [acc.id], commit=False)
    session.commit()
    session.refresh(acc)
    if acc.platform == "chatgpt" and body.token is not None:
        schedule_chatgpt_local_status_refresh_for_account_id(acc.id, reason="account_update_token")
    return acc


@router.post("/{account_id}/mark-used")
def mark_account_used(account_id: int, body: AccountMarkUsedRequest,
                      session: Session = Depends(get_session)):
    acc = session.get(AccountModel, account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    try:
        extra = json.loads(acc.extra_json or "{}")
    except Exception:
        extra = {}
    if not isinstance(extra, dict):
        extra = {}
    extra["manually_used"] = bool(body.used)
    acc.extra_json = json.dumps(extra, ensure_ascii=False)
    acc.updated_at = datetime.now(timezone.utc)
    session.add(acc)
    upsert_account_list_state_for_account_ids(session, [acc.id], commit=False)
    session.commit()
    session.refresh(acc)
    return _serialize_account(acc)


@router.delete("/{account_id}")
def delete_account(account_id: int, session: Session = Depends(get_session)):
    acc = session.get(AccountModel, account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    session.delete(acc)
    delete_account_list_state_for_account_ids(session, [account_id], commit=False)
    session.commit()
    return {"ok": True}


@router.post("/{account_id}/check")
def check_account(account_id: int, background_tasks: BackgroundTasks,
                  session: Session = Depends(get_session)):
    acc = session.get(AccountModel, account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    background_tasks.add_task(_do_check, account_id)
    return {"message": "检测任务已启动"}


def _do_check(account_id: int):
    from core.db import engine
    from sqlmodel import Session
    from services.chatgpt_core import ChatGPTPlatform
    with Session(engine) as s:
        acc = s.get(AccountModel, account_id)
    if acc:
        from core.base_platform import Account, RegisterConfig
        try:
            if acc.platform != "chatgpt":
                return
            plugin = ChatGPTPlatform(config=RegisterConfig())
            obj = Account(platform=acc.platform, email=acc.email,
                         password=acc.password, user_id=acc.user_id,
                         region=acc.region, token=acc.token,
                         extra=json.loads(acc.extra_json or "{}"))
            valid = plugin.check_valid(obj)
            with Session(engine) as s:
                a = s.get(AccountModel, account_id)
                if a:
                    if a.platform != "chatgpt":
                        a.status = a.status if valid else "invalid"
                    a.updated_at = datetime.now(timezone.utc)
                    s.add(a)
                    upsert_account_list_state_for_account_ids(s, [a.id], commit=False)
                    s.commit()
        except Exception:
            logger.exception("检测账号 %s 时出错", account_id)
