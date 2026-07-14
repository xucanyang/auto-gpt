"""Phone-pool API relay client and URL resolution.

The phone pool keeps the supplier URL as source-of-truth.  This module talks to
one central Relay registry and derives the public forwarded URL at the moment a
record is serialized or a phone task is frozen.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
import threading
import time
from typing import Any, Iterable
from urllib.parse import urlsplit

import requests

from services.phone_api_relay import RelayValidationError, forwarded_api_url, normalize_public_origin


class PhoneApiForwardError(RuntimeError):
    """The configured Relay cannot safely serve a phone API request."""

    def __init__(self, message: str, *, code: str = "api_forward_error") -> None:
        super().__init__(message)
        self.code = str(code or "api_forward_error")


@dataclass(frozen=True, slots=True)
class PhoneApiResolution:
    source_api_url: str
    request_api_url: str
    source_api_host: str
    forwarded_api_host: str
    forwarded: bool
    status: str
    error: str = ""


_LOCK = threading.RLock()
_CONFIG_CACHE: dict[str, Any] | None = None
_CONFIG_CACHE_AT = 0.0
_CONFIG_CACHE_TTL = 15.0
_SYNC_STATE: dict[str, Any] = {
    "status": "idle",
    "last_attempt_at": "",
    "last_success_at": "",
    "last_error": "",
    "inventory_count": 0,
    "route_count": 0,
    "owner_count": 0,
    "trigger": "",
}


def _utcnow_text() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _relay_base_url() -> str:
    return str(os.getenv("PHONE_API_RELAY_INTERNAL_URL") or "").strip().rstrip("/")


def _relay_token() -> str:
    return str(os.getenv("PHONE_API_RELAY_ADMIN_TOKEN") or "").strip()


def relay_instance_id() -> str:
    return str(os.getenv("APP_INSTANCE_ID") or "auto-gpt").strip() or "auto-gpt"


def relay_is_configured() -> bool:
    return bool(_relay_base_url() and _relay_token())


def _safe_detail(response: requests.Response) -> str:
    try:
        payload = response.json()
    except Exception:
        payload = {}
    detail: Any = payload.get("detail") if isinstance(payload, dict) else ""
    if isinstance(detail, dict):
        code = str(detail.get("code") or "").strip()
        route_id = str(detail.get("route_id") or "").strip()
        return " ".join(item for item in (code, route_id) if item)
    text = str(detail or "").strip()
    return text[:240]


def _relay_request(method: str, path: str, *, payload: dict[str, Any] | None = None, timeout: float = 15.0) -> dict[str, Any]:
    base_url = _relay_base_url()
    token = _relay_token()
    if not base_url or not token:
        raise PhoneApiForwardError("手机号 API Relay 未配置", code="relay_not_configured")
    session = requests.Session()
    session.trust_env = False
    try:
        response = session.request(
            method.upper(),
            f"{base_url}{path}",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=max(float(timeout or 15.0), 1.0),
        )
    except requests.RequestException as exc:
        raise PhoneApiForwardError("手机号 API Relay 暂时不可达", code="relay_unavailable") from exc
    finally:
        session.close()
    if response.status_code >= 400:
        detail = _safe_detail(response)
        if response.status_code == 409:
            raise PhoneApiForwardError(
                f"手机号 API 转发路径冲突{f': {detail}' if detail else ''}",
                code="route_conflict",
            )
        raise PhoneApiForwardError(
            f"手机号 API Relay 请求失败（HTTP {response.status_code}）{f': {detail}' if detail else ''}",
            code="relay_request_failed",
        )
    try:
        data = response.json()
    except Exception as exc:
        raise PhoneApiForwardError("手机号 API Relay 返回了无效响应", code="relay_invalid_response") from exc
    if not isinstance(data, dict):
        raise PhoneApiForwardError("手机号 API Relay 返回了无效响应", code="relay_invalid_response")
    return data


def invalidate_forwarding_config_cache() -> None:
    global _CONFIG_CACHE, _CONFIG_CACHE_AT
    with _LOCK:
        _CONFIG_CACHE = None
        _CONFIG_CACHE_AT = 0.0


def get_forwarding_config(*, force: bool = False, strict: bool = True) -> dict[str, Any]:
    global _CONFIG_CACHE, _CONFIG_CACHE_AT
    if not relay_is_configured():
        return {
            "enabled": False,
            "active_origin": "",
            "previous_origins": [],
            "relay_configured": False,
            "forward_status": "not_configured",
        }
    now = time.monotonic()
    with _LOCK:
        cached = dict(_CONFIG_CACHE or {})
        cached_at = _CONFIG_CACHE_AT
    if cached and not force and now - cached_at < _CONFIG_CACHE_TTL:
        return cached
    try:
        data = _relay_request("GET", "/admin/v1/config", timeout=8.0)
        config = {
            "enabled": bool(data.get("enabled")),
            "active_origin": str(data.get("active_origin") or "").strip(),
            "previous_origins": [str(item) for item in data.get("previous_origins", []) if str(item or "").strip()],
            "updated_at": str(data.get("updated_at") or ""),
            "relay_configured": True,
            "forward_status": "active" if bool(data.get("enabled")) else "disabled",
        }
        with _LOCK:
            _CONFIG_CACHE = dict(config)
            _CONFIG_CACHE_AT = now
        return config
    except PhoneApiForwardError as exc:
        if cached:
            cached["forward_status"] = "degraded"
            cached["relay_error"] = str(exc)
            return cached
        if strict:
            raise
        return {
            "enabled": False,
            "active_origin": "",
            "previous_origins": [],
            "relay_configured": True,
            "forward_status": "unavailable",
            "relay_error": str(exc),
        }


def set_forwarding_config(*, enabled: bool, active_origin: Any, previous_origins: Iterable[Any] | None) -> dict[str, Any]:
    try:
        active = normalize_public_origin(active_origin)
        previous = [normalize_public_origin(item) for item in (previous_origins or []) if str(item or "").strip()]
    except RelayValidationError as exc:
        raise PhoneApiForwardError(str(exc), code="invalid_origin") from exc
    data = _relay_request(
        "PUT",
        "/admin/v1/config",
        payload={"enabled": bool(enabled), "active_origin": active, "previous_origins": previous},
        timeout=12.0,
    )
    invalidate_forwarding_config_cache()
    return get_forwarding_config(force=True, strict=True)


def _host(value: str) -> str:
    try:
        return str(urlsplit(str(value or "")).hostname or "").lower()
    except Exception:
        return ""


def _resolve_phone_api_url_with_config(
    source_api_url: Any,
    config: dict[str, Any],
    *,
    strict: bool = True,
) -> PhoneApiResolution:
    """Resolve one source URL from an already fetched config snapshot.

    The public phone-pool list can contain hundreds of rows.  Keeping the
    config lookup outside this function avoids one Admin request per row.  A
    snapshot is only a presentation optimisation; strict business callers
    still use :func:`resolve_phone_api_url` and therefore retain its failure
    semantics.
    """
    source = str(source_api_url or "").strip()
    if not source:
        return PhoneApiResolution("", "", "", "", False, "missing")
    if not bool(config.get("enabled")):
        return PhoneApiResolution(
            source_api_url=source,
            request_api_url=source,
            source_api_host=_host(source),
            forwarded_api_host="",
            forwarded=False,
            status=str(config.get("forward_status") or "direct"),
            error=str(config.get("relay_error") or ""),
        )
    with _LOCK:
        inventory_state = dict(_SYNC_STATE)
    inventory_status = str(inventory_state.get("status") or "").strip().lower()
    inventory_error = str(inventory_state.get("last_error") or "").strip()
    if inventory_status in {"conflict", "error"} and str(inventory_state.get("last_attempt_at") or "").strip():
        error_code = "route_conflict" if inventory_status == "conflict" else "inventory_unavailable"
        message = inventory_error or (
            "手机号 API 转发路径冲突"
            if inventory_status == "conflict"
            else "手机号 API Relay 路由库存尚未同步"
        )
        if strict:
            raise PhoneApiForwardError(message, code=error_code)
        return PhoneApiResolution(
            source_api_url=source,
            request_api_url="",
            source_api_host=_host(source),
            forwarded_api_host="",
            forwarded=False,
            status=inventory_status,
            error=message,
        )
    active_origin = str(config.get("active_origin") or "").strip()
    if not active_origin:
        if strict:
            raise PhoneApiForwardError("手机号 API Relay 已启用但缺少主域名", code="relay_origin_missing")
        return PhoneApiResolution(source, source, _host(source), "", False, "error", "Relay 主域名为空")
    try:
        forwarded = forwarded_api_url(source, active_origin)
    except RelayValidationError as exc:
        if strict:
            raise PhoneApiForwardError("手机号 API URL 无法生成转发地址", code="invalid_source_url") from exc
        return PhoneApiResolution(source, source, _host(source), "", False, "error", str(exc))
    status = str(config.get("forward_status") or "forwarded")
    return PhoneApiResolution(
        source_api_url=source,
        request_api_url=forwarded,
        source_api_host=_host(source),
        forwarded_api_host=_host(forwarded),
        forwarded=True,
        status="forwarded" if status == "active" else status,
        error=str(config.get("relay_error") or ""),
    )


def resolve_phone_api_url(source_api_url: Any, *, strict: bool = True) -> PhoneApiResolution:
    """Resolve one source URL, fetching the current Relay config as needed."""
    config = get_forwarding_config(strict=strict)
    return _resolve_phone_api_url_with_config(source_api_url, config, strict=strict)


def serialize_forwarding_fields(
    source_api_url: Any,
    *,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return public forwarding fields without leaking a supplier URL.

    ``config`` is an optional snapshot supplied by the list serializer.  When
    omitted, retain the historical single-record behaviour.
    """
    snapshot = config if config is not None else get_forwarding_config(strict=False)
    resolution = _resolve_phone_api_url_with_config(source_api_url, snapshot, strict=False)
    return {
        "source_api_url": resolution.source_api_url,
        "source_api_host": resolution.source_api_host,
        "forwarded_api_url": resolution.request_api_url if resolution.forwarded else "",
        "forwarded_api_host": resolution.forwarded_api_host,
        "api_forwarded": resolution.forwarded,
        "forward_status": resolution.status,
        "forward_error": resolution.error,
    }


def sync_phone_pool_inventory(records: Iterable[Any], *, trigger: str = "manual", raise_on_error: bool = False) -> dict[str, Any]:
    now = _utcnow_text()
    with _LOCK:
        _SYNC_STATE.update({"status": "syncing", "last_attempt_at": now, "last_error": "", "trigger": str(trigger or "manual")})
    items: list[dict[str, Any]] = []
    for record in records or []:
        pool_id = getattr(record, "id", None)
        api_url = str(getattr(record, "api_url", "") or "").strip()
        if not pool_id or not api_url:
            continue
        items.append({"pool_id": str(pool_id), "source_api_url": api_url})
    try:
        data = _relay_request(
            "PUT",
            f"/admin/v1/inventory/{relay_instance_id()}",
            payload={"items": items},
            timeout=30.0,
        )
        result = {
            "status": "synced",
            "last_attempt_at": now,
            "last_success_at": _utcnow_text(),
            "last_error": "",
            "inventory_count": int(data.get("inventory_count") or 0),
            "route_count": int(data.get("route_count") or 0),
            "owner_count": int(data.get("owner_count") or 0),
            "trigger": str(trigger or "manual"),
        }
        with _LOCK:
            _SYNC_STATE.update(result)
        return dict(result)
    except PhoneApiForwardError as exc:
        result = {
            "status": "conflict" if exc.code == "route_conflict" else "error",
            "last_attempt_at": now,
            "last_success_at": str(_SYNC_STATE.get("last_success_at") or ""),
            "last_error": str(exc),
            "inventory_count": int(_SYNC_STATE.get("inventory_count") or 0),
            "route_count": int(_SYNC_STATE.get("route_count") or 0),
            "owner_count": int(_SYNC_STATE.get("owner_count") or 0),
            "trigger": str(trigger or "manual"),
        }
        with _LOCK:
            _SYNC_STATE.update(result)
        if raise_on_error:
            raise
        return dict(result)


def get_inventory_status(*, force_remote: bool = False) -> dict[str, Any]:
    if force_remote and relay_is_configured():
        try:
            data = _relay_request("GET", f"/admin/v1/inventory/{relay_instance_id()}", timeout=8.0)
            with _LOCK:
                _SYNC_STATE.update({
                    "inventory_count": int(data.get("inventory_count") or 0),
                    "route_count": int(data.get("route_count") or 0),
                    "owner_count": int(data.get("owner_count") or 0),
                })
        except PhoneApiForwardError as exc:
            with _LOCK:
                _SYNC_STATE.update({"status": "error", "last_error": str(exc)})
    with _LOCK:
        return dict(_SYNC_STATE)
