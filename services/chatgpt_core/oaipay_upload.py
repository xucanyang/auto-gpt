"""
OAIPay 上传功能
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from typing import Any, Tuple
from urllib.parse import urlsplit

from curl_cffi import requests as cffi_requests

from services.chatgpt_core.cpa_upload import generate_token_json

logger = logging.getLogger(__name__)

DEFAULT_GROUP_IDS = [2]
DEFAULT_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
DEFAULT_OAIPAY_INTERNAL_API_URL = "http://gpt-cccy-me:8789"


def _get_config_value(key: str) -> str:
    try:
        from core.config_store import config_store

        return str(config_store.get(key, "") or "").strip()
    except Exception:
        return ""


def _url_origin_key(value: Any) -> tuple[str, str, int] | None:
    """Return a normalized origin key without retaining URL path/query data."""
    try:
        parsed = urlsplit(str(value or "").strip())
        scheme = str(parsed.scheme or "").lower()
        host = str(parsed.hostname or "").lower().rstrip(".")
        if scheme not in {"http", "https"} or not host:
            return None
        port = int(parsed.port or (443 if scheme == "https" else 80))
    except (TypeError, ValueError):
        return None
    return scheme, host, port


def _resolve_bound_phone_delivery(extra: dict[str, Any]) -> dict[str, str]:
    """Resolve persisted phone metadata to the current delivery URL.

    New records retain both URLs. For historical records, the phone-pool row
    is the authoritative supplier URL when available. Relay failures are
    propagated so OAIPay never receives a silent direct-source fallback.
    """
    bound_phone = extra.get("chatgpt_bound_phone") if isinstance(extra.get("chatgpt_bound_phone"), dict) else {}
    binding = extra.get("chatgpt_phone_binding") if isinstance(extra.get("chatgpt_phone_binding"), dict) else {}
    phone = str(
        bound_phone.get("phone")
        or bound_phone.get("phone_number")
        or binding.get("phone")
        or binding.get("phone_number")
        or extra.get("chatgpt_bound_phone_number")
        or extra.get("phone")
        or ""
    ).strip()
    saved_api_url = str(bound_phone.get("api_url") or binding.get("api_url") or "").strip()
    source_api_url = str(
        bound_phone.get("source_api_url")
        or binding.get("source_api_url")
        or ""
    ).strip()
    api_token = str(bound_phone.get("api_token") or binding.get("api_token") or "").strip()
    if not source_api_url and phone:
        try:
            from services.chatgpt_core.phone_pool_repository import PhonePoolRepository

            pool_record = PhonePoolRepository().get(phone)
            source_api_url = str(getattr(pool_record, "api_url", "") or "").strip()
        except Exception:
            source_api_url = ""
    request_api_url = saved_api_url
    if source_api_url:
        from services.chatgpt_core.phone_api_forwarding import resolve_phone_api_url

        request_api_url = str(resolve_phone_api_url(source_api_url, strict=True).request_api_url or "").strip()
    elif saved_api_url:
        # Some transitional records only retained the effective Relay URL. Do
        # not relabel that URL as a supplier source or feed it back into future
        # inventory sync. When it belongs to the configured Relay, resolve it
        # only as an already-forwarded compatibility value (which also moves a
        # previous origin to the current active origin). A disabled Relay cannot
        # safely recover the missing supplier URL, so fail closed.
        from services.chatgpt_core.phone_api_forwarding import (
            PhoneApiForwardError,
            get_forwarding_config,
            resolve_phone_api_url,
        )

        forwarding = get_forwarding_config(strict=True)
        relay_origins = [
            str(forwarding.get("active_origin") or ""),
            *[str(item or "") for item in forwarding.get("previous_origins") or []],
        ]
        saved_origin = _url_origin_key(saved_api_url)
        is_saved_relay_url = bool(
            saved_origin
            and any(saved_origin == _url_origin_key(origin) for origin in relay_origins if origin)
        )
        if is_saved_relay_url:
            if not bool(forwarding.get("enabled")):
                raise PhoneApiForwardError(
                    "历史手机号记录仅保存了 Relay URL，转发关闭时无法恢复供应商 API",
                    code="relay_source_missing",
                )
            request_api_url = str(
                resolve_phone_api_url(saved_api_url, strict=True).request_api_url or ""
            ).strip()
        else:
            # Pre-Relay records stored the supplier URL only. Keep that legacy
            # interpretation and derive the current effective URL normally.
            source_api_url = saved_api_url
            request_api_url = str(
                resolve_phone_api_url(source_api_url, strict=True).request_api_url or ""
            ).strip()
    return {
        "phone": phone,
        "api_url": request_api_url,
        "source_api_url": source_api_url,
        "api_token": api_token,
    }


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


def _extract_oaipay_response_data(detail: Any) -> dict[str, Any]:
    if not isinstance(detail, dict):
        return {}
    data = detail.get("data")
    if isinstance(data, dict):
        return data
    return detail


def _stringify_error_detail(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        parts = [_stringify_error_detail(item) for item in value]
        return "; ".join(part for part in parts if part).strip()
    if isinstance(value, dict):
        for key in ("message", "msg", "error", "detail"):
            text = _stringify_error_detail(value.get(key))
            if text:
                return text
        try:
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))[:500]
        except Exception:
            return str(value)[:500]
    return str(value).strip()


def _extract_oaipay_error_detail(detail: Any) -> str:
    if isinstance(detail, dict):
        for key in ("message", "msg", "error", "detail"):
            text = _stringify_error_detail(detail.get(key))
            if text:
                return text
    return _stringify_error_detail(detail)


def _format_oaipay_upload_error(response: Any, detail: Any) -> str:
    status_code = getattr(response, "status_code", "")
    base = f"上传失败: HTTP {status_code}" if status_code else "上传失败"
    detail_text = _extract_oaipay_error_detail(detail)
    if not detail_text:
        detail_text = str(getattr(response, "text", "") or "")[:500].strip()
    return f"{base}: {detail_text}" if detail_text else base


_CATEGORIES_CACHE: dict[str, str] = {}
_CATEGORIES_ID_TO_NAME_CACHE: dict[str, str] = {}
_CATEGORIES_ITEMS_CACHE: list[dict[str, Any]] = []
_CATEGORIES_CACHE_TIME = 0
_CATEGORIES_CACHE_KEY = ""


def normalize_oaipay_api_url(api_url: Any) -> str:
    """Map the retired public OAIPay origin to the private service network.

    Only the known public origin is rewritten. Custom OAIPay deployments and
    malformed legacy values remain untouched so this compatibility rule cannot
    silently redirect an operator-selected endpoint.
    """

    raw = str(api_url or "").strip().rstrip("/")
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
        scheme = str(parsed.scheme or "").lower()
        host = str(parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except (TypeError, ValueError):
        return raw
    if (
        scheme not in {"http", "https"}
        or host != "gpt.cccy.me"
        or port not in {None, 80, 443}
    ):
        return raw

    internal_base = str(
        os.getenv("OAIPAY_INTERNAL_API_URL") or DEFAULT_OAIPAY_INTERNAL_API_URL
    ).strip().rstrip("/")
    if not internal_base:
        return raw
    path = str(parsed.path or "").rstrip("/")
    return f"{internal_base}{path}"


def _oaipay_api_base_url(api_url: Any) -> str:
    return normalize_oaipay_api_url(api_url).split("/api/")[0].rstrip("/")


def _oaipay_auth_header_variants(api_key: str) -> list[dict[str, str]]:
    raw_key = str(api_key or "").strip()
    if not raw_key:
        return []
    bearer = raw_key if raw_key.lower().startswith("bearer ") else f"Bearer {raw_key}"
    bare = raw_key[7:].strip() if raw_key.lower().startswith("bearer ") else raw_key

    variants = [
        {
            "Authorization": raw_key,
            "x-api-key": bare,
            "api-key": bare,
        },
        {
            "Authorization": bearer,
            "x-api-key": bare,
            "api-key": bare,
        },
        {
            "Authorization": bare,
            "x-api-key": bare,
            "api-key": bare,
        },
    ]
    seen: set[tuple[tuple[str, str], ...]] = set()
    unique: list[dict[str, str]] = []
    for headers in variants:
        key = tuple(sorted(headers.items()))
        if key in seen:
            continue
        seen.add(key)
        unique.append(headers)
    return unique


def _extract_oaipay_category_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        raw_items = payload
    elif isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            raw_items = data
        elif isinstance(data, dict) and isinstance(data.get("items"), list):
            raw_items = data.get("items") or []
        elif isinstance(payload.get("categories"), list):
            raw_items = payload.get("categories") or []
        elif isinstance(payload.get("items"), list):
            raw_items = payload.get("items") or []
        else:
            raw_items = []
    else:
        raw_items = []

    categories: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        cid = str(item.get("id") or item.get("category_id") or item.get("categoryId") or "").strip()
        name = str(item.get("name") or item.get("category_name") or item.get("categoryName") or item.get("title") or "").strip()
        if not cid or not name or cid in seen_ids:
            continue
        seen_ids.add(cid)
        try:
            normalized_id: Any = int(cid)
        except ValueError:
            normalized_id = cid
        categories.append({"id": normalized_id, "name": name})
    return categories


def fetch_oaipay_categories(
    api_url: str | None = None,
    api_key: str | None = None,
    *,
    force_refresh: bool = False,
) -> list[dict[str, Any]]:
    """Fetch OAIPay categories through the same contract used by auto upload."""

    global _CATEGORIES_CACHE, _CATEGORIES_ID_TO_NAME_CACHE, _CATEGORIES_ITEMS_CACHE, _CATEGORIES_CACHE_TIME, _CATEGORIES_CACHE_KEY
    now = time.time()
    api_url = str(api_url or _get_config_value("oaipay_api_url")).strip()
    api_key = str(api_key or _get_config_value("oaipay_api_key")).strip()
    base_url = _oaipay_api_base_url(api_url)
    cache_key = f"{base_url}|{api_key}"
    if (
        not force_refresh
        and _CATEGORIES_ITEMS_CACHE
        and _CATEGORIES_CACHE_KEY == cache_key
        and now - _CATEGORIES_CACHE_TIME <= 60
    ):
        return [dict(item) for item in _CATEGORIES_ITEMS_CACHE]

    if not base_url or not api_key:
        return []

    candidate_paths = (
        # These routes authenticate with UPLOAD_KEY. The admin endpoints
        # require a browser admin session and must not be the normal path.
        "/api/auto-gpt/categories",
        "/api/cdk/accounts/categories",
        "/api/admin/cdk/categories",
        "/api/cdk/categories",
        "/api/v1/admin/categories",
        "/api/admin/categories",
    )
    last_error = ""
    for path in candidate_paths:
        url = f"{base_url}{path}"
        for auth_headers in _oaipay_auth_header_variants(api_key):
            try:
                res = cffi_requests.get(
                    url,
                    headers={
                        "Accept": "application/json, text/plain, */*",
                        "Referer": f"{base_url}/admin/accounts",
                        **auth_headers,
                    },
                    timeout=10,
                    impersonate="chrome146",
                )
                if res.status_code in (404, 405):
                    break
                if res.status_code >= 400:
                    try:
                        detail = _extract_oaipay_error_detail(res.json())
                    except Exception:
                        detail = str(getattr(res, "text", "") or "")[:200].strip()
                    last_error = f"HTTP {res.status_code}{(': ' + detail) if detail else ''}"
                    continue
                categories = _extract_oaipay_category_items(res.json())
                if not categories:
                    last_error = "OAIPay 分类接口返回空列表或不兼容格式"
                    continue

                name_to_id: dict[str, str] = {}
                id_to_name: dict[str, str] = {}
                for category in categories:
                    cname = str(category.get("name") or "").strip()
                    cid = str(category.get("id") or "").strip()
                    if not cname or not cid:
                        continue
                    name_to_id[cname] = cid
                    id_to_name[cid] = cname
                _CATEGORIES_CACHE = name_to_id
                _CATEGORIES_ID_TO_NAME_CACHE = id_to_name
                _CATEGORIES_ITEMS_CACHE = [dict(item) for item in categories]
                _CATEGORIES_CACHE_TIME = now
                _CATEGORIES_CACHE_KEY = cache_key
                return [dict(item) for item in _CATEGORIES_ITEMS_CACHE]
            except Exception:
                continue
    if last_error and not _CATEGORIES_ITEMS_CACHE:
        logger.warning("OAIPay 分类拉取失败: %s", last_error)
    return [dict(item) for item in _CATEGORIES_ITEMS_CACHE] if _CATEGORIES_CACHE_KEY == cache_key else []


def _load_oaipay_categories(api_url: str, api_key: str) -> tuple[dict[str, str], dict[str, str]]:
    fetch_oaipay_categories(api_url, api_key)
    return _CATEGORIES_CACHE, _CATEGORIES_ID_TO_NAME_CACHE


def _resolve_category_id(api_url: str, api_key: str, name: str) -> str:
    name_to_id, _ = _load_oaipay_categories(api_url, api_key)
    return name_to_id.get(name, "")


def _resolve_category_name(api_url: str, api_key: str, category_id: Any) -> str:
    _, id_to_name = _load_oaipay_categories(api_url, api_key)
    return id_to_name.get(str(category_id or "").strip(), "")


def _normalize_category_mode(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"manual", "fixed", "force", "forced", "指定", "固定", "固定分类"}:
        return "manual"
    return "auto"


def _first_group_value(values: list[int] | list[str] | tuple[Any, ...] | None) -> str:
    if not values:
        return ""
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _category_id_value(value: Any) -> Any:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return text


def _build_auto_category_name(capabilities: dict[str, Any]) -> tuple[str, str]:
    has_rt = bool(capabilities.get("has_refresh_token"))
    has_paid = bool(capabilities.get("has_paid_subscription"))
    if has_paid:
        has_confirmed_phone_binding = bool(capabilities.get("has_confirmed_phone_binding"))
        if has_rt and has_confirmed_phone_binding:
            return "PLUS--已接美国长效", "paid_with_refresh_token"
        if has_rt:
            return "PLUS--未接码", "paid_with_refresh_token_phone_unverified"
        return "PLUS--未接码", "paid_without_refresh_token"
    if has_rt:
        return "FREE--已接码带RT", "free_with_refresh_token"
    return "", ""


def _build_category_decision(
    *,
    api_url: str,
    api_key: str,
    capabilities: dict[str, Any],
    category_mode: Any = "auto",
    group_ids: list[int] | None = None,
    fallback_group_ids: list[int] | None = None,
) -> dict[str, Any]:
    mode = _normalize_category_mode(category_mode)
    requested_group = _first_group_value(group_ids)
    fallback_group = _first_group_value(fallback_group_ids) or (requested_group if mode == "auto" else "")
    global_group = _get_config_value("oaipay_group")
    decision: dict[str, Any] = {
        "category_mode": mode,
        "category_source": "",
        "category_rule": "",
        "category_id": None,
        "category_name": "",
        "requested_category_id": _category_id_value(requested_group),
        "fallback_category_id": _category_id_value(fallback_group),
        "auto_group_name": "",
        "resolved_group": "",
    }

    def use_group(group: str, source: str, rule: str = "", name: str = "") -> dict[str, Any]:
        group_text = str(group or "").strip()
        category_name = name or _resolve_category_name(api_url, api_key, group_text) or ("" if group_text.isdigit() else group_text)
        decision.update(
            {
                "category_source": source,
                "category_rule": rule,
                "category_id": _category_id_value(group_text),
                "category_name": category_name,
                "resolved_group": group_text,
            }
        )
        return decision

    if mode == "manual":
        if requested_group:
            return use_group(requested_group, "manual")
        if fallback_group:
            return use_group(fallback_group, "fallback")
        return use_group(global_group, "global_default")

    auto_group_name, auto_rule = _build_auto_category_name(capabilities)
    decision["auto_group_name"] = auto_group_name
    if auto_group_name:
        resolved_id = _resolve_category_id(api_url, api_key, auto_group_name)
        if resolved_id:
            return use_group(resolved_id, "auto", auto_rule, auto_group_name)

    if fallback_group:
        return use_group(fallback_group, "fallback", "auto_unmatched")
    return use_group(global_group, "global_default", "auto_unmatched")


def _category_fields_from_decision(decision: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "category_mode",
        "category_source",
        "category_rule",
        "category_id",
        "category_name",
        "requested_category_id",
        "fallback_category_id",
        "auto_group_name",
        "resolved_group",
        "remote_category_id",
        "remote_group",
    )
    return {key: decision.get(key) for key in keys if decision.get(key) not in (None, "")}

def upload_to_oaipay_detailed(
    account,
    api_url: str | None = None,
    api_key: str | None = None,
    group_ids: list[int] | None = None,
    category_mode: str = "auto",
    fallback_group_ids: list[int] | None = None,
    capabilities: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """上传单个账号到 OAIPay 管理后台，返回结构化结果。"""
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

    if capabilities is None:
        from services.chatgpt_account_state import classify_chatgpt_capabilities
        caps = classify_chatgpt_capabilities(upload_account)
    else:
        caps = capabilities
    from services.chatgpt_account_state import RETIRED_SUBSCRIPTION_TYPES, effective_subscription_plan

    subscription_plan = effective_subscription_plan(caps)
    if subscription_plan in RETIRED_SUBSCRIPTION_TYPES:
        return {
            "ok": False,
            "skipped": True,
            "message": f"订阅类型 {subscription_plan} 已退役，禁止上传 OAIPay",
        }
    if (
        not str(token_data.get("refresh_token") or "").strip()
        and subscription_plan not in {"plus", "pro"}
    ):
        return {
            "ok": False,
            "skipped": True,
            "upload_gate": "blocked_missing_rt",
            "message": "跳过上传：仅 Plus/Pro 未接码账号支持无 refresh_token 上传",
        }

    api_url = normalize_oaipay_api_url(api_url or _get_config_value("oaipay_api_url"))
    api_key = str(api_key or _get_config_value("oaipay_api_key")).strip()
    if not api_url:
        return {"ok": False, "message": "OAIPay API URL 未配置"}
    if not api_key:
        return {"ok": False, "message": "OAIPay API Key 未配置"}

    category_decision = _build_category_decision(
        api_url=api_url,
        api_key=api_key,
        capabilities=caps,
        category_mode=category_mode,
        group_ids=group_ids,
        fallback_group_ids=fallback_group_ids,
    )
    group = str(category_decision.get("resolved_group") or "").strip()
    category_fields = _category_fields_from_decision(category_decision)

    email = getattr(upload_account, "email", "")
    password = getattr(upload_account, "password", "")

    extra = _get_account_extra(upload_account)
    phone_delivery = {"phone": "", "api_url": "", "source_api_url": "", "api_token": ""}
    if isinstance(extra, dict):
        try:
            phone_delivery = _resolve_bound_phone_delivery(extra)
        except Exception as exc:
            from services.chatgpt_core.phone_api_forwarding import PhoneApiForwardError

            if isinstance(exc, PhoneApiForwardError):
                return {
                    "ok": False,
                    "message": f"api_forward_error: OAIPay 手机号 API 转发准备失败: {exc}",
                    "error_code": "api_forward_error",
                    **category_fields,
                }
            raise
        if phone_delivery.get("api_url"):
            token_data["api_url"] = phone_delivery["api_url"]
            token_data["source_api_url"] = phone_delivery.get("source_api_url") or ""
            token_data["phone"] = phone_delivery.get("phone") or ""

    import json
    extra_info_json = json.dumps(token_data, ensure_ascii=False, separators=(',', ':'))

    acc_dict = {
        "email": email,
        "password": password,
        "extra_info": extra_info_json,
    }
    if isinstance(extra, dict):
        phone_val = str(phone_delivery.get("phone") or "")
        api_url_val = str(phone_delivery.get("api_url") or "")
        source_api_url_val = str(phone_delivery.get("source_api_url") or "")
        api_token_val = str(phone_delivery.get("api_token") or "")
        if not api_url_val:
            api_url_val = _get_config_value("local_phone_gateway_url")
            api_token_val = _get_config_value("local_phone_gateway_token")

        if phone_val or api_url_val:
            acc_dict["phone"] = phone_val
            acc_dict["phone_api"] = api_url_val
            if source_api_url_val:
                acc_dict["source_api_url"] = source_api_url_val
            if api_token_val:
                acc_dict["phone_token"] = api_token_val

    payload = {
        "key": api_key,
        "group": group,
        "accounts": [acc_dict],
    }

    base_url = api_url.split("/api/")[0].rstrip("/")
    raw_url = api_url.rstrip("/")
    candidate_urls = []
    if raw_url.endswith("/upload"):
        candidate_urls.append(raw_url)
    for path in (
        "/api/auto-gpt/upload",
        "/api/cdk/accounts/upload",
        "/api/admin/cdk/accounts/upload",
    ):
        full = f"{base_url}{path}"
        if full not in candidate_urls:
            candidate_urls.append(full)

    auth_val = api_key if api_key.startswith("Bearer ") else f"Bearer {api_key}"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "x-api-key": api_key,
        "Authorization": auth_val,
        "api-key": api_key,
    }

    def category_fields_with_remote(detail: Any) -> dict[str, Any]:
        fields = dict(category_fields)
        data = _extract_oaipay_response_data(detail)
        remote_category_id = data.get("category_id") or data.get("categoryId")
        remote_group = data.get("group") or data.get("category") or data.get("category_name")
        if remote_category_id not in (None, ""):
            fields["remote_category_id"] = _category_id_value(remote_category_id)
            fields["category_id"] = _category_id_value(remote_category_id)
            fields["category_name"] = fields.get("category_name") or _resolve_category_name(api_url, api_key, remote_category_id)
        if remote_group not in (None, ""):
            fields["remote_group"] = str(remote_group)
            if not fields.get("category_name") and not str(remote_group).isdigit():
                fields["category_name"] = str(remote_group)
        return {key: value for key, value in fields.items() if value not in (None, "")}

    last_error = "未知错误"
    last_detail: Any = {}
    for url in candidate_urls:
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
            last_detail = detail

            is_success = (
                response.status_code in (200, 201)
                and isinstance(detail, dict)
                and (
                    detail.get("success")
                    or detail.get("code") == 0
                    or detail.get("status") == "success"
                    or int(detail.get("imported", 0) or 0) > 0
                )
            )
            if is_success:
                imported = detail.get("imported", 1)
                upload_category_fields = category_fields_with_remote(detail)
                return {
                    "ok": True,
                    "message": f"上传成功，导入 {imported} 个账号",
                    "remote_account_id": None,
                    "remote_status": "uploaded",
                    "response": detail,
                    **upload_category_fields,
                }

            if response.status_code in (404, 405):
                continue

            if response.status_code in (401, 403):
                headers_fallback = dict(headers)
                headers_fallback["Authorization"] = api_key
                resp2 = cffi_requests.post(
                    url,
                    headers=headers_fallback,
                    json=payload,
                    proxies=None,
                    timeout=30,
                    impersonate="chrome146",
                )
                try:
                    detail2 = resp2.json()
                except Exception:
                    detail2 = {}
                is_succ2 = (
                    resp2.status_code in (200, 201)
                    and isinstance(detail2, dict)
                    and (
                        detail2.get("success")
                        or detail2.get("code") == 0
                        or detail2.get("status") == "success"
                        or int(detail2.get("imported", 0) or 0) > 0
                    )
                )
                if is_succ2:
                    imported = detail2.get("imported", 1)
                    upload_category_fields = category_fields_with_remote(detail2)
                    return {
                        "ok": True,
                        "message": f"上传成功，导入 {imported} 个账号",
                        "remote_account_id": None,
                        "remote_status": "uploaded",
                        "response": detail2,
                        **upload_category_fields,
                    }

            error_msg = _format_oaipay_upload_error(response, detail)
            return {
                "ok": False,
                "message": error_msg,
                "response": detail if isinstance(detail, dict) else {},
                **category_fields_with_remote(detail),
            }
        except Exception as exc:
            logger.error("OAIPay 上传尝试失败 (%s): %s", url, exc)
            last_error = f"上传异常: {exc}"

    return {
        "ok": False,
        "message": last_error,
        "response": last_detail if isinstance(last_detail, dict) else {},
        **category_fields_with_remote(last_detail),
    }


def upload_to_oaipay(
    account,
    api_url: str | None = None,
    api_key: str | None = None,
    group_ids: list[int] | None = None,
) -> Tuple[bool, str]:
    """上传单个账号到 OAIPay 管理后台。"""
    result = upload_to_oaipay_detailed(account, api_url=api_url, api_key=api_key, group_ids=group_ids)
    return bool(result.get("ok")), str(result.get("message") or "")
