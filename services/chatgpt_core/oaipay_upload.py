"""
OAIPay 上传功能
"""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import Any, Tuple

from curl_cffi import requests as cffi_requests

from services.chatgpt_core.cpa_upload import generate_token_json
from services.chatgpt_core.status_probe import probe_local_chatgpt_status

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
    extra = getattr(account, "extra", {}) or {}
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


def build_oaipay_export_account_payload(account) -> dict[str, Any]:
    """构造 OAIPay 文件导出格式，不带上传接口专用字段。"""
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
    if not isinstance(expires_at, int) or expires_at <= 0:
        expires_at = int(time.time()) + 863999

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
        "expires_in": 863999,
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


def build_oaipay_lookup_payload(account) -> dict[str, Any]:
    token_data = generate_token_json(account)
    access_token = str(token_data.get("access_token") or "").strip()
    refresh_token = str(token_data.get("refresh_token") or "").strip()
    id_token = str(token_data.get("id_token") or "").strip()
    email = str(token_data.get("email") or getattr(account, "email", "") or "").strip()

    access_payload = _decode_jwt_payload(access_token)
    access_auth = _extract_auth(access_payload)
    expires_at = access_payload.get("exp")
    if not isinstance(expires_at, int) or expires_at <= 0:
        expires_at = int(time.time()) + 863999

    organization_id = _extract_organization_id(_decode_jwt_payload(id_token))

    credentials = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_in": 863999,
        "expires_at": expires_at,
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


def build_oaipay_account_payload(account, group_ids: list[int] | None = None) -> dict[str, Any]:
    payload = build_oaipay_lookup_payload(account)
    credentials = payload.get("credentials") if isinstance(payload.get("credentials"), dict) else {}

    # 上传前做一次本地套餐探测，尽量把 plan_type / subscription_expires_at 一起带到 OAIPay。
    try:
        probe = probe_local_chatgpt_status(account)
        subscription = probe.get("subscription") if isinstance(probe.get("subscription"), dict) else {}
        plan_type = str(subscription.get("plan") or "").strip().lower()
        if plan_type and plan_type != "unknown":
            credentials["plan_type"] = plan_type
        subscription_active_until = str(subscription.get("subscription_active_until") or "").strip()
        if subscription_active_until:
            credentials["subscription_expires_at"] = subscription_active_until
    except Exception as exc:
        logger.warning("OAIPay 上传前套餐探测失败: %s", exc)

    payload["credentials"] = credentials
    payload["group_ids"] = _parse_group_ids(group_ids)
    return payload


def _extract_oaipay_response_data(detail: Any) -> dict[str, Any]:
    if not isinstance(detail, dict):
        return {}
    data = detail.get("data")
    if isinstance(data, dict):
        return data
    return detail


def upload_to_oaipay_detailed(
    account,
    api_url: str | None = None,
    api_key: str | None = None,
    group_ids: list[int] | None = None,
) -> dict[str, Any]:
    """上传单个账号到 OAIPay 管理后台，返回结构化结果。"""
    api_url = str(api_url or _get_config_value("oaipay_api_url")).strip()
    api_key = str(api_key or _get_config_value("oaipay_api_key")).strip()
    
    group = ""
    if group_ids and len(group_ids) > 0:
        group = str(group_ids[0])
    else:
        group = _get_config_value("oaipay_group")

    if not api_url:
        return {"ok": False, "message": "OAIPay API URL 未配置"}
    if not api_key:
        return {"ok": False, "message": "OAIPay API Key 未配置"}

    email = getattr(account, "email", "")
    password = getattr(account, "password", "")
    from services.chatgpt_core.cpa_upload import generate_token_json
    token_data = generate_token_json(account)
    token = token_data.get("access_token", "")

    payload = {
        "group": group,
        "accounts": [
            {
                "email": email,
                "password": password,
                "extra_info": token
            }
        ]
    }

    url = api_url.rstrip('/')
    if not url.endswith("/api/auto-gpt/upload") and not url.endswith("/api/cdk/accounts/upload"):
        url = f"{url}/api/auto-gpt/upload"

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "x-api-key": api_key,
        "Authorization": api_key,
    }

    try:
        response = cffi_requests.post(
            url,
            headers=headers,
            json=payload,
            proxies=None,
            verify=False,
            timeout=30,
            impersonate="chrome110",
        )

        detail: Any = {}
        try:
            detail = response.json()
        except Exception:
            detail = {}

        if response.status_code in (200, 201) and detail.get("success"):
            return {
                "ok": True,
                "message": f"上传成功，导入 {detail.get('imported', 0)} 个账号",
                "remote_account_id": None,
                "remote_status": "uploaded",
                "response": detail,
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
        logger.error("OAIPay 上传异常: %s", exc)
        return {"ok": False, "message": f"上传异常: {exc}"}


def upload_to_oaipay(
    account,
    api_url: str | None = None,
    api_key: str | None = None,
    group_ids: list[int] | None = None,
) -> Tuple[bool, str]:
    """上传单个账号到 OAIPay 管理后台。"""
    result = upload_to_oaipay_detailed(account, api_url=api_url, api_key=api_key, group_ids=group_ids)
    return bool(result.get("ok")), str(result.get("message") or "")
