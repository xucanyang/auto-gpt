"""
Sub2API 上传功能
"""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import Any, Tuple

from curl_cffi import requests as cffi_requests

from services.chatgpt_core.cpa_upload import generate_token_json
from services.chatgpt_core.auth_lifecycle import epoch_from_value, lifecycle_from_extra

logger = logging.getLogger(__name__)

DEFAULT_GROUP_IDS = [2]
DEFAULT_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"


def _get_config_value(key: str) -> str:
    try:
        from core.config_store import config_store

        return str(config_store.get(key, "") or "").strip()
    except Exception:
        return ""


def _parse_group_ids(raw: Any, fallback: list[int] | None = None) -> list[int]:
    candidates: list[Any]
    if isinstance(raw, str):
        candidates = [part.strip() for part in raw.split(",")]
    elif isinstance(raw, (list, tuple, set)):
        candidates = list(raw)
    elif raw is None:
        candidates = []
    else:
        candidates = [raw]

    values: list[int] = []
    for item in candidates:
        text = str(item or "").strip()
        if not text:
            continue
        try:
            values.append(int(text))
        except ValueError:
            continue

    return values or list(fallback or DEFAULT_GROUP_IDS)


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    try:
        parts = str(token or "").split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1]
        padding = 4 - len(payload) % 4
        if padding != 4:
            payload += "=" * padding
        decoded = base64.urlsafe_b64decode(payload)
        data = json.loads(decoded)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _extract_auth(payload: dict[str, Any]) -> dict[str, Any]:
    auth_info = payload.get("https://api.openai.com/auth")
    return auth_info if isinstance(auth_info, dict) else {}


def _extract_organization_id(id_token_payload: dict[str, Any]) -> str:
    auth_info = _extract_auth(id_token_payload)
    organization_id = str(auth_info.get("organization_id") or "").strip()
    if organization_id:
        return organization_id

    organizations = auth_info.get("organizations") or []
    if isinstance(organizations, list):
        for item in organizations:
            if isinstance(item, dict):
                organization_id = str(item.get("id") or "").strip()
                if organization_id:
                    return organization_id
    return ""


def _get_account_extra(account) -> dict[str, Any]:
    if hasattr(account, "get_extra"):
        try:
            extra = account.get_extra()
            if isinstance(extra, dict):
                return extra
        except Exception:
            pass
    extra = getattr(account, "extra", {}) or {}
    if callable(extra):
        extra = extra()
    return extra if isinstance(extra, dict) else {}


def _to_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value or "").strip()
    if not text:
        return default
    try:
        return int(float(text))
    except ValueError:
        return default


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _subscription_from_extra(extra: dict[str, Any]) -> dict[str, Any]:
    local = extra.get("chatgpt_local")
    if not isinstance(local, dict):
        return {}
    subscription = local.get("subscription")
    return subscription if isinstance(subscription, dict) else {}


def _codex_from_extra(extra: dict[str, Any]) -> dict[str, Any]:
    local = extra.get("chatgpt_local")
    if not isinstance(local, dict):
        return {}
    codex = local.get("codex")
    return codex if isinstance(codex, dict) else {}


def _export_extra_value(extra: dict[str, Any], codex: dict[str, Any], key: str, default: Any = 0) -> Any:
    if key in extra:
        return extra.get(key)
    usage = codex.get("usage") if isinstance(codex.get("usage"), dict) else {}
    if key in usage:
        return usage.get(key)
    return default


def build_sub2api_export_account_payload(account) -> dict[str, Any]:
    """构造 Sub2API 文件导出格式，不带上传接口专用字段。"""
    token_data = generate_token_json(account)
    access_token = str(token_data.get("access_token") or "").strip()
    refresh_token = str(token_data.get("refresh_token") or "").strip()
    id_token = str(token_data.get("id_token") or "").strip()
    email = str(token_data.get("email") or getattr(account, "email", "") or "").strip()
    extra = _get_account_extra(account)

    access_payload = _decode_jwt_payload(access_token)
    access_auth = _extract_auth(access_payload)
    id_payload = _decode_jwt_payload(id_token)
    id_auth = _extract_auth(id_payload)
    subscription = _subscription_from_extra(extra)
    codex = _codex_from_extra(extra)

    expires_at = access_payload.get("exp")
    expiry_source = "jwt_exp" if isinstance(expires_at, int) and expires_at > 0 else ""
    if not isinstance(expires_at, int) or expires_at <= 0:
        lifecycle = lifecycle_from_extra(account, extra)
        lifecycle_access = lifecycle.get("access_token") if isinstance(lifecycle.get("access_token"), dict) else {}
        lifecycle_epoch = epoch_from_value(lifecycle_access.get("expires_at"))
        expires_at = int(lifecycle_epoch) if lifecycle_epoch else 0
        expiry_source = str(lifecycle_access.get("expiry_source") or "") if expires_at else "unknown"
    expires_in = max(0, int(expires_at - time.time())) if expires_at else 0

    plan_type = _first_text(
        subscription.get("plan") if str(subscription.get("plan") or "").strip().lower() != "unknown" else "",
        extra.get("plan_type"),
        extra.get("chatgpt_plan_type"),
        id_auth.get("chatgpt_plan_type"),
        access_auth.get("chatgpt_plan_type"),
        "free",
    ).lower()
    subscription_expires_at = _first_text(
        subscription.get("subscription_active_until"),
        extra.get("subscription_expires_at"),
        extra.get("chatgpt_subscription_active_until"),
        id_auth.get("chatgpt_subscription_active_until"),
        access_auth.get("chatgpt_subscription_active_until"),
    )

    credentials = {
        "access_token": access_token,
        "chatgpt_account_id": _first_text(
            access_auth.get("chatgpt_account_id"),
            id_auth.get("chatgpt_account_id"),
            token_data.get("account_id"),
            getattr(account, "user_id", ""),
        ),
        "chatgpt_user_id": _first_text(
            access_auth.get("chatgpt_user_id"),
            id_auth.get("chatgpt_user_id"),
            access_auth.get("user_id"),
            id_auth.get("user_id"),
        ),
        "client_id": str(getattr(account, "client_id", "") or DEFAULT_CLIENT_ID).strip() or DEFAULT_CLIENT_ID,
        "expires_at": expires_at,
        "expires_in": expires_in,
        "expiry_source": expiry_source,
        "id_token": id_token,
        "organization_id": _first_text(
            extra.get("organization_id"),
            _extract_organization_id(id_payload),
            _extract_organization_id(access_payload),
        ),
        "plan_type": plan_type,
        "refresh_token": refresh_token,
        "subscription_expires_at": subscription_expires_at,
    }

    export_extra = {
        "codex_5h_reset_after_seconds": _to_int(_export_extra_value(extra, codex, "codex_5h_reset_after_seconds")),
        "codex_5h_reset_at": str(_export_extra_value(extra, codex, "codex_5h_reset_at", "") or ""),
        "codex_5h_used_percent": _to_int(_export_extra_value(extra, codex, "codex_5h_used_percent")),
        "codex_5h_window_minutes": _to_int(_export_extra_value(extra, codex, "codex_5h_window_minutes", 300), 300),
        "codex_7d_reset_after_seconds": _to_int(_export_extra_value(extra, codex, "codex_7d_reset_after_seconds")),
        "codex_7d_reset_at": str(_export_extra_value(extra, codex, "codex_7d_reset_at", "") or ""),
        "codex_7d_used_percent": _to_int(_export_extra_value(extra, codex, "codex_7d_used_percent")),
        "codex_7d_window_minutes": _to_int(_export_extra_value(extra, codex, "codex_7d_window_minutes", 10080), 10080),
        "codex_primary_over_secondary_percent": _to_int(_export_extra_value(extra, codex, "codex_primary_over_secondary_percent")),
        "codex_primary_reset_after_seconds": _to_int(_export_extra_value(extra, codex, "codex_primary_reset_after_seconds")),
        "codex_primary_used_percent": _to_int(_export_extra_value(extra, codex, "codex_primary_used_percent")),
        "codex_primary_window_minutes": _to_int(_export_extra_value(extra, codex, "codex_primary_window_minutes", 300), 300),
        "codex_secondary_reset_after_seconds": _to_int(_export_extra_value(extra, codex, "codex_secondary_reset_after_seconds")),
        "codex_secondary_used_percent": _to_int(_export_extra_value(extra, codex, "codex_secondary_used_percent")),
        "codex_secondary_window_minutes": _to_int(_export_extra_value(extra, codex, "codex_secondary_window_minutes", 10080), 10080),
        "codex_usage_updated_at": _first_text(
            _export_extra_value(extra, codex, "codex_usage_updated_at", ""),
            codex.get("checked_at"),
        ),
        "email": email,
        "privacy_mode": _first_text(extra.get("privacy_mode"), extra.get("chatgpt_privacy_mode"), "training_off"),
    }

    return {
        "name": email,
        "platform": "openai",
        "type": "oauth",
        "credentials": credentials,
        "extra": export_extra,
        "concurrency": 10,
        "priority": 1,
        "rate_multiplier": 1,
        "auto_pause_on_expired": True,
    }


def build_sub2api_lookup_payload(account) -> dict[str, Any]:
    token_data = generate_token_json(account)
    access_token = str(token_data.get("access_token") or "").strip()
    refresh_token = str(token_data.get("refresh_token") or "").strip()
    id_token = str(token_data.get("id_token") or "").strip()
    email = str(token_data.get("email") or getattr(account, "email", "") or "").strip()

    access_payload = _decode_jwt_payload(access_token)
    access_auth = _extract_auth(access_payload)
    expires_at = access_payload.get("exp")
    expiry_source = "jwt_exp" if isinstance(expires_at, int) and expires_at > 0 else ""
    if not isinstance(expires_at, int) or expires_at <= 0:
        extra = _get_account_extra(account)
        lifecycle = lifecycle_from_extra(account, extra)
        lifecycle_access = lifecycle.get("access_token") if isinstance(lifecycle.get("access_token"), dict) else {}
        lifecycle_epoch = epoch_from_value(lifecycle_access.get("expires_at"))
        expires_at = int(lifecycle_epoch) if lifecycle_epoch else 0
        expiry_source = str(lifecycle_access.get("expiry_source") or "") if expires_at else "unknown"
    expires_in = max(0, int(expires_at - time.time())) if expires_at else 0

    organization_id = _extract_organization_id(_decode_jwt_payload(id_token))

    credentials = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_in": expires_in,
        "expires_at": expires_at,
        "expiry_source": expiry_source,
        "chatgpt_account_id": str(
            access_auth.get("chatgpt_account_id") or token_data.get("account_id") or ""
        ).strip(),
        "chatgpt_user_id": str(access_auth.get("chatgpt_user_id") or "").strip(),
        "organization_id": organization_id,
        "client_id": str(getattr(account, "client_id", "") or DEFAULT_CLIENT_ID).strip() or DEFAULT_CLIENT_ID,
        "id_token": id_token,
    }

    return {
        "name": email,
        "notes": "",
        "platform": "openai",
        "type": "oauth",
        "credentials": credentials,
        "extra": {"email": email},
        "concurrency": 10,
        "priority": 1,
        "auto_pause_on_expired": True,
    }


def build_sub2api_account_payload(account, group_ids: list[int] | None = None) -> dict[str, Any]:
    payload = build_sub2api_lookup_payload(account)
    credentials = payload.get("credentials") if isinstance(payload.get("credentials"), dict) else {}
    extra = _get_account_extra(account)
    subscription = _subscription_from_extra(extra)
    plan_type = _first_text(
        subscription.get("plan") if str(subscription.get("plan") or "").strip().lower() != "unknown" else "",
        extra.get("plan_type"),
        extra.get("chatgpt_plan_type"),
    ).lower()
    subscription_expires_at = _first_text(
        subscription.get("subscription_active_until"),
        extra.get("subscription_expires_at"),
        extra.get("chatgpt_subscription_active_until"),
    )
    if plan_type:
        credentials["plan_type"] = plan_type
    if subscription_expires_at:
        credentials["subscription_expires_at"] = subscription_expires_at
    payload["credentials"] = credentials
    payload["group_ids"] = _parse_group_ids(group_ids)
    return payload


def _extract_sub2api_response_data(detail: Any) -> dict[str, Any]:
    if not isinstance(detail, dict):
        return {}
    data = detail.get("data")
    if isinstance(data, dict):
        return data
    return detail


def upload_to_sub2api_detailed(
    account,
    api_url: str | None = None,
    api_key: str | None = None,
    group_ids: list[int] | None = None,
) -> dict[str, Any]:
    """上传单个账号到 Sub2API 管理后台，返回结构化结果。"""
    upload_account = account
    token_data = generate_token_json(upload_account)
    if (
        not str(token_data.get("access_token") or "").strip()
        or not str(token_data.get("refresh_token") or "").strip()
    ):
        from services.chatgpt_sync import build_chatgpt_sync_account

        upload_account = build_chatgpt_sync_account(account)
        upload_account.access_token = token_data.get("access_token") or upload_account.access_token
        upload_account.refresh_token = token_data.get("refresh_token") or upload_account.refresh_token
        upload_account.id_token = token_data.get("id_token") or upload_account.id_token
        token_data = generate_token_json(upload_account)
    if not str(token_data.get("access_token") or "").strip():
        return {
            "ok": False,
            "skipped": True,
            "upload_gate": "blocked_missing_at",
            "message": "跳过上传：缺少 access_token，认证材料尚未就绪",
        }
    if not str(token_data.get("refresh_token") or "").strip():
        return {
            "ok": False,
            "skipped": True,
            "upload_gate": "blocked_missing_rt",
            "message": "跳过上传：缺少 refresh_token",
        }

    api_url = str(api_url or _get_config_value("sub2api_api_url")).strip()
    api_key = str(api_key or _get_config_value("sub2api_api_key")).strip()
    resolved_group_ids = _parse_group_ids(
        _get_config_value("sub2api_group_ids") if group_ids is None else group_ids
    )

    if not api_url:
        return {"ok": False, "message": "Sub2API API URL 未配置"}
    if not api_key:
        return {"ok": False, "message": "Sub2API API Key 未配置"}

    payload = build_sub2api_account_payload(upload_account, group_ids=resolved_group_ids)
    url = f"{api_url.rstrip('/')}/api/v1/admin/accounts"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Referer": f"{api_url.rstrip('/')}/admin/accounts",
        "x-api-key": api_key,
    }

    try:
        response = cffi_requests.post(
            url,
            headers=headers,
            json=payload,
            proxies=None,
            timeout=30,
            impersonate="chrome146",
        )

        detail: Any = {}
        try:
            detail = response.json()
        except Exception:
            detail = {}

        if response.status_code in (200, 201):
            data = _extract_sub2api_response_data(detail)
            remote_id = data.get("id") or data.get("account_id")
            return {
                "ok": True,
                "message": "上传成功",
                "remote_account_id": remote_id,
                "remote_status": str(data.get("status") or ""),
                "response": detail if isinstance(detail, dict) else {},
            }

        error_msg = f"上传失败: HTTP {response.status_code}"
        if isinstance(detail, dict):
            error_msg = str(
                detail.get("message")
                or detail.get("msg")
                or detail.get("error")
                or error_msg
            )
        else:
            error_msg = f"{error_msg} - {response.text[:200]}"
        return {"ok": False, "message": error_msg, "response": detail if isinstance(detail, dict) else {}}
    except Exception as exc:
        logger.error("Sub2API 上传异常: %s", exc)
        return {"ok": False, "message": f"上传异常: {exc}"}


def upload_to_sub2api(
    account,
    api_url: str | None = None,
    api_key: str | None = None,
    group_ids: list[int] | None = None,
) -> Tuple[bool, str]:
    """上传单个账号到 Sub2API 管理后台。"""
    result = upload_to_sub2api_detailed(account, api_url=api_url, api_key=api_key, group_ids=group_ids)
    return bool(result.get("ok")), str(result.get("message") or "")
