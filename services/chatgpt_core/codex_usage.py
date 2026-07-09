"""Codex 用量窗口查询与归一化。"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from curl_cffi import requests as cffi_requests

from services.chatgpt_account_state import is_account_deactivated_message
from services.chatgpt_core.status_probe import (
    CODEX_USER_AGENT as STATUS_CODEX_USER_AGENT,
    ProbeHTTPResult,
    STATUS_PROBE_TIMEOUT_SECONDS,
    _auth_state_for_source,
    _build_proxies,
    _extract_error_code,
    _extract_error_message,
    _failed_probe_result,
    _parse_header_error_json,
    _parse_loose_json,
    _probe_codex_usage,
    _probe_exception_message,
    _resolve_effective_probe_proxy,
    _resolve_probe_access_token,
    extract_chatgpt_account_id,
)

CODEX_RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"
CODEX_PROBE_MODEL = os.getenv("CHATGPT_CODEX_USAGE_PROBE_MODEL", "gpt-5.4")
CODEX_PROBE_VERSION = os.getenv("CHATGPT_CODEX_USAGE_PROBE_VERSION", "0.125.0")
CODEX_RESPONSES_USER_AGENT = os.getenv(
    "CHATGPT_CODEX_USAGE_USER_AGENT",
    f"codex_cli_rs/{CODEX_PROBE_VERSION} (Ubuntu 22.4.0; x86_64) xterm-256color",
)
CODEX_PROBE_TIMEOUT_SECONDS = max(STATUS_PROBE_TIMEOUT_SECONDS, 15.0)
logger = logging.getLogger(__name__)

_CODEX_AUTO_REFRESH_LOCK = threading.Lock()
_CODEX_AUTO_REFRESH_IN_FLIGHT: set[int] = set()


_RATE_LIMIT_HEADERS = {
    "primary_used_percent": "x-codex-primary-used-percent",
    "primary_reset_after_seconds": "x-codex-primary-reset-after-seconds",
    "primary_window_minutes": "x-codex-primary-window-minutes",
    "secondary_used_percent": "x-codex-secondary-used-percent",
    "secondary_reset_after_seconds": "x-codex-secondary-reset-after-seconds",
    "secondary_window_minutes": "x-codex-secondary-window-minutes",
    "primary_over_secondary_percent": "x-codex-primary-over-secondary-limit-percent",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().isoformat()


def _parse_datetime(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _isoformat(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _header_get(headers: Any, key: str) -> str:
    if not headers:
        return ""
    try:
        value = headers.get(key)
        if value is not None:
            if isinstance(value, list):
                value = value[0] if value else ""
            return str(value or "").strip()
    except Exception:
        pass

    key_l = key.lower()
    try:
        items = headers.items()
    except Exception:
        items = []
    for raw_key, raw_value in items:
        if str(raw_key or "").lower() == key_l:
            if isinstance(raw_value, list):
                raw_value = raw_value[0] if raw_value else ""
            return str(raw_value or "").strip()
    return ""


def _to_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _to_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _reset_after_from_reset_at(value: Any, base: datetime) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp <= 0:
            return None
        if timestamp > 1_000_000_000_000:
            timestamp = timestamp / 1000.0
        try:
            reset_at = datetime.fromtimestamp(timestamp, tz=timezone.utc)
            return max(0, int((reset_at - base).total_seconds()))
        except Exception:
            return None
    parsed = _parse_datetime(value)
    if parsed is None:
        return None
    return max(0, int((parsed - base).total_seconds()))


def _remaining_percent(used_percent: Any) -> Optional[float]:
    used = _to_float(used_percent)
    if used is None:
        return None
    return max(0.0, min(100.0, 100.0 - used))


def _reset_at(base: datetime, reset_after_seconds: Optional[int]) -> str:
    seconds = int(reset_after_seconds or 0)
    if seconds < 0:
        seconds = 0
    return _isoformat(base + timedelta(seconds=seconds))


def parse_codex_rate_limit_headers(headers: Any, *, updated_at: str | None = None) -> dict[str, Any] | None:
    """Extract raw Codex primary/secondary usage snapshot from response headers."""
    snapshot: dict[str, Any] = {}
    for field, header in _RATE_LIMIT_HEADERS.items():
        raw = _header_get(headers, header)
        if not raw:
            continue
        if field.endswith("_percent"):
            parsed = _to_float(raw)
        else:
            parsed = _to_int(raw)
        if parsed is not None:
            snapshot[field] = parsed

    if not snapshot:
        return None
    snapshot["updated_at"] = str(updated_at or _utcnow_iso())
    return snapshot


def parse_codex_usage_body(body: Any, *, updated_at: str | None = None) -> dict[str, Any] | None:
    """Extract Codex usage from /backend-api/wham/usage JSON body.

    ChatGPT currently returns the Codex rate windows in the response body on
    `wham/usage`, while `codex/responses` may expose the same data via
    `x-codex-*` headers.  Keep both parsers feeding the same normalized snapshot.
    """
    if not isinstance(body, dict):
        return None

    base = _parse_datetime(updated_at) or _utcnow()
    rate_limit = body.get("rate_limit") if isinstance(body.get("rate_limit"), dict) else {}
    source = rate_limit if rate_limit else body

    snapshot: dict[str, Any] = {}

    def read_window(name: str) -> None:
        window = source.get(f"{name}_window") if isinstance(source.get(f"{name}_window"), dict) else {}
        direct_prefix = f"{name}_"

        used = _to_float(
            window.get("used_percent")
            if window
            else source.get(f"{direct_prefix}used_percent")
        )
        reset_after = _to_int(
            window.get("reset_after_seconds")
            if window
            else source.get(f"{direct_prefix}reset_after_seconds")
        )
        if reset_after is None:
            reset_after = _reset_after_from_reset_at(
                window.get("reset_at") if window else source.get(f"{direct_prefix}reset_at"),
                base,
            )

        window_minutes = _to_int(
            window.get("window_minutes")
            if window
            else source.get(f"{direct_prefix}window_minutes")
        )
        if window_minutes is None:
            seconds = _to_int(
                window.get("limit_window_seconds")
                or window.get("window_seconds")
                or source.get(f"{direct_prefix}limit_window_seconds")
                or source.get(f"{direct_prefix}window_seconds")
            )
            if seconds is not None and seconds > 0:
                window_minutes = max(1, int(round(seconds / 60)))

        if used is not None:
            snapshot[f"{name}_used_percent"] = used
        if reset_after is not None:
            snapshot[f"{name}_reset_after_seconds"] = reset_after
        if window_minutes is not None:
            snapshot[f"{name}_window_minutes"] = window_minutes

    read_window("primary")
    read_window("secondary")

    overflow = _to_float(
        source.get("primary_over_secondary_percent")
        or source.get("primary_over_secondary_limit_percent")
        or source.get("primary_over_secondary_limit")
    )
    if overflow is not None:
        snapshot["primary_over_secondary_percent"] = overflow

    if not snapshot:
        return None
    snapshot["updated_at"] = str(updated_at or _utcnow_iso())
    return snapshot


def _normalize_window_mapping(snapshot: dict[str, Any]) -> dict[str, str]:
    primary_window = _to_int(snapshot.get("primary_window_minutes"))
    secondary_window = _to_int(snapshot.get("secondary_window_minutes"))

    if primary_window is not None and secondary_window is not None:
        if primary_window < secondary_window:
            return {"5h": "primary", "7d": "secondary"}
        return {"5h": "secondary", "7d": "primary"}

    if primary_window is not None:
        if primary_window <= 360:
            return {"5h": "primary", "7d": "secondary"}
        return {"5h": "secondary", "7d": "primary"}

    if secondary_window is not None:
        if secondary_window <= 360:
            return {"5h": "secondary", "7d": "primary"}
        return {"5h": "primary", "7d": "secondary"}

    # Legacy / header-missing fallback from sub2api: primary = 7d, secondary = 5h.
    return {"5h": "secondary", "7d": "primary"}


def build_codex_usage_extra_updates(snapshot: dict[str, Any] | None, checked_at: str | None = None) -> dict[str, Any]:
    """Build flat fields compatible with sub2api export extra fields."""
    if not isinstance(snapshot, dict) or not snapshot:
        return {}

    base = _parse_datetime(snapshot.get("updated_at") or checked_at) or _utcnow()
    updated_at = _isoformat(base)
    updates: dict[str, Any] = {"codex_usage_updated_at": updated_at}

    raw_mapping = {
        "codex_primary_used_percent": snapshot.get("primary_used_percent"),
        "codex_primary_reset_after_seconds": snapshot.get("primary_reset_after_seconds"),
        "codex_primary_window_minutes": snapshot.get("primary_window_minutes"),
        "codex_secondary_used_percent": snapshot.get("secondary_used_percent"),
        "codex_secondary_reset_after_seconds": snapshot.get("secondary_reset_after_seconds"),
        "codex_secondary_window_minutes": snapshot.get("secondary_window_minutes"),
        "codex_primary_over_secondary_percent": snapshot.get("primary_over_secondary_percent"),
    }
    for key, value in raw_mapping.items():
        if value is not None:
            updates[key] = value

    mapping = _normalize_window_mapping(snapshot)
    defaults = {"5h": 300, "7d": 10080}
    for window in ("5h", "7d"):
        source = mapping.get(window)
        if not source:
            continue
        used = _to_float(snapshot.get(f"{source}_used_percent"))
        reset_after = _to_int(snapshot.get(f"{source}_reset_after_seconds"))
        window_minutes = _to_int(snapshot.get(f"{source}_window_minutes"))
        if window_minutes is None:
            window_minutes = defaults[window]

        prefix = f"codex_{window}"
        if used is not None:
            updates[f"{prefix}_used_percent"] = used
            remaining = _remaining_percent(used)
            if remaining is not None:
                updates[f"{prefix}_remaining_percent"] = remaining
        if reset_after is not None:
            updates[f"{prefix}_reset_after_seconds"] = reset_after
            updates[f"{prefix}_reset_at"] = _reset_at(base, reset_after)
        if window_minutes is not None:
            updates[f"{prefix}_window_minutes"] = window_minutes

    return updates


def build_codex_usage_progress_from_extra(extra: dict[str, Any] | None) -> dict[str, Any]:
    """Return frontend-friendly 5h/7d progress from cached flat fields, strictly separating short vs long windows."""
    data = extra if isinstance(extra, dict) else {}
    updated_at = str(data.get("codex_usage_updated_at") or "").strip()

    def _resolve_window(prefixes: tuple[str, ...], max_minutes: int | None, min_minutes: int | None, default_minutes: int) -> dict[str, Any]:
        for prefix in prefixes:
            used = _to_float(data.get(f"{prefix}_used_percent"))
            if used is None:
                continue
            window_minutes = _to_int(data.get(f"{prefix}_window_minutes"))
            if window_minutes is not None:
                if max_minutes is not None and window_minutes > max_minutes:
                    continue
                if min_minutes is not None and window_minutes <= min_minutes:
                    continue
            else:
                window_minutes = default_minutes

            reset_after = _to_int(data.get(f"{prefix}_reset_after_seconds"))
            reset_at = str(data.get(f"{prefix}_reset_at") or "").strip()
            remaining = _remaining_percent(used)
            return {
                "used_percent": used,
                "remaining_percent": remaining,
                "reset_after_seconds": reset_after,
                "reset_at": reset_at,
                "window_minutes": window_minutes,
            }
        return {
            "used_percent": None,
            "remaining_percent": None,
            "reset_after_seconds": None,
            "reset_at": "",
            "window_minutes": None,
        }

    return {
        "updated_at": updated_at,
        "five_hour": _resolve_window(("codex_5h", "codex_secondary", "codex_primary"), max_minutes=360, min_minutes=None, default_minutes=300),
        "seven_day": _resolve_window(("codex_7d", "codex_primary", "codex_secondary"), max_minutes=None, min_minutes=360, default_minutes=10080),
    }


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


def account_has_codex_auth_material(account: Any) -> bool:
    """Return whether a saved ChatGPT account has AT/RT material worth probing."""
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


def _to_codex_probe_account(account: Any) -> Any:
    """Build the duck-typed account object expected by the Codex probe."""
    extra = _account_extra(account)

    class _Account:
        pass

    probe_account = _Account()
    probe_account.id = getattr(account, "id", None)
    probe_account.email = getattr(account, "email", "")
    probe_account.password = getattr(account, "password", "")
    probe_account.user_id = getattr(account, "user_id", "")
    probe_account.token = getattr(account, "token", "")
    probe_account.access_token = str(
        extra.get("access_token")
        or extra.get("accessToken")
        or extra.get("webAccessToken")
        or getattr(account, "access_token", "")
        or getattr(account, "token", "")
        or ""
    ).strip()
    probe_account.refresh_token = str(
        extra.get("refresh_token")
        or extra.get("refreshToken")
        or getattr(account, "refresh_token", "")
        or ""
    ).strip()
    probe_account.id_token = str(extra.get("id_token") or getattr(account, "id_token", "") or "").strip()
    probe_account.session_token = str(extra.get("session_token") or getattr(account, "session_token", "") or "").strip()
    probe_account.client_id = str(
        extra.get("client_id")
        or getattr(account, "client_id", "")
        or "app_EMoamEEZ73f0CkXaXp7hrann"
    ).strip()
    probe_account.cookies = str(extra.get("cookies") or getattr(account, "cookies", "") or "").strip()
    probe_account.workspace_id = str(extra.get("workspace_id") or getattr(account, "workspace_id", "") or "").strip()
    probe_account.extra = extra
    return probe_account


def _codex_probe_payload(model: str) -> dict[str, Any]:
    return {
        "model": str(model or CODEX_PROBE_MODEL).strip() or CODEX_PROBE_MODEL,
        "input": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "hi",
                    }
                ],
            }
        ],
        "instructions": "You are a coding assistant.",
        "stream": True,
        "store": False,
    }


def _perform_codex_responses_post(access_token: str, account_id: str, proxy: Optional[str], *, model: str = "") -> ProbeHTTPResult:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "text/event-stream",
        "Content-Type": "application/json",
        "OpenAI-Beta": "responses=experimental",
        "Originator": "codex_cli_rs",
        "Version": CODEX_PROBE_VERSION,
        "User-Agent": CODEX_RESPONSES_USER_AGENT or STATUS_CODEX_USER_AGENT,
    }
    if account_id:
        headers["chatgpt-account-id"] = account_id

    response = None
    body_text = ""
    try:
        response = cffi_requests.post(
            CODEX_RESPONSES_URL,
            headers=headers,
            json=_codex_probe_payload(model or CODEX_PROBE_MODEL),
            proxies=_build_proxies(proxy),
            timeout=CODEX_PROBE_TIMEOUT_SECONDS,
            impersonate="chrome110",
            stream=True,
        )
        # With stream=True headers are available immediately.  Do not drain the SSE body;
        # this probe only needs rate-limit headers and should stay cheap.
        if int(getattr(response, "status_code", 0) or 0) >= 400:
            try:
                body_text = (response.text or "")[:1000]
            except Exception:
                body_text = ""
        body_json = _parse_loose_json(body_text)
        header_error_json = _parse_header_error_json(response.headers)
        error_code = _extract_error_code(response.headers, body_json, header_error_json)
        message = _extract_error_message(body_json, header_error_json, body_text, response.status_code)
        return ProbeHTTPResult(
            status_code=response.status_code,
            headers=response.headers,
            body_text=body_text,
            body_json=body_json,
            error_code=error_code,
            message=message,
        )
    except Exception as exc:
        return _failed_probe_result(exc)
    finally:
        try:
            if response is not None:
                response.close()
        except Exception:
            pass


def _probe_codex_responses(access_token: str, account_id: str, proxy: Optional[str], *, model: str = "") -> ProbeHTTPResult:
    return _perform_codex_responses_post(access_token, account_id, proxy, model=model)


def _extract_tokens_from_account(account: Any) -> tuple[str, str, str]:
    extra = getattr(account, "extra", {}) or {}
    if not isinstance(extra, dict):
        extra = {}
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
    client_id = str(extra.get("client_id") or getattr(account, "client_id", "") or "").strip()
    return refresh_token, access_token, client_id


def probe_codex_usage_window(
    account: Any,
    proxy: Optional[str] = None,
    *,
    force: bool = True,
    model: str = "",
    use_default_proxy: bool = True,
) -> dict[str, Any]:
    """Actively query Codex 5h/7d usage windows for one ChatGPT account.

    Auth precedence intentionally mirrors local status probing:
    refresh_token -> fresh access_token; otherwise existing access_token; otherwise skip.
    """
    checked_at = _utcnow_iso()
    refresh_token, access_token, client_id = _extract_tokens_from_account(account)
    account_id = extract_chatgpt_account_id(account)

    result: dict[str, Any] = {
        "state": "not_checked",
        "checked_at": checked_at,
        "source": "unknown",
        "http_status": 0,
        "error_code": "",
        "message": "",
        "chatgpt_account_id": account_id,
        "usage": {},
        "force": bool(force),
        "network": {
            "proxy_used": False,
            "proxy_source": "direct",
        },
    }

    if not refresh_token and not access_token:
        result.update(
            {
                "state": "missing_auth",
                "source": "refresh_token",
                "message": "账号缺少 refresh_token 且没有可用 access_token",
            }
        )
        return result

    try:
        effective_proxy, proxy_source = _resolve_effective_probe_proxy(proxy, use_default_proxy=use_default_proxy)
    except Exception as exc:
        message = f"默认代理解析失败: {_probe_exception_message(exc)}"
        result.update(
            {
                "state": "probe_failed",
                "error_code": "proxy_resolve_failed",
                "message": message,
            }
        )
        result["network"].update(
            {
                "proxy_used": False,
                "proxy_source": "resolve_failed",
                "proxy_error": message,
            }
        )
        return result
    result["network"].update(
        {
            "proxy_used": bool(effective_proxy),
            "proxy_source": proxy_source or ("proxy" if effective_proxy else "direct"),
        }
    )

    token_resolution = _resolve_probe_access_token(
        refresh_token=refresh_token,
        access_token=access_token,
        client_id=client_id,
        proxy=effective_proxy,
    )
    token_source = str(token_resolution.get("source") or "refresh_token").strip() or "refresh_token"
    result["source"] = token_source

    if not token_resolution.get("ok"):
        http_status = int(token_resolution.get("http_status") or 0)
        error_code = str(token_resolution.get("error_code") or "").strip()
        message = str(token_resolution.get("message") or "").strip()
        if http_status == 401:
            state = _auth_state_for_source(token_source, invalidated=True)
        elif http_status == 403:
            state = "account_deactivated" if is_account_deactivated_message(error_code, message) else "banned_like"
        elif http_status == 0 and not refresh_token and not access_token:
            state = "missing_auth"
        else:
            state = "probe_failed"
        result.update({"state": state, "http_status": http_status, "error_code": error_code, "message": message})
        return result

    probe_access_token = str(token_resolution.get("access_token") or "").strip()
    if token_source == "refresh_token" and probe_access_token:
        token_updates = {"access_token": probe_access_token}
        next_refresh_token = str(token_resolution.get("refresh_token") or refresh_token or "").strip()
        if next_refresh_token:
            token_updates["refresh_token"] = next_refresh_token
        result["_token_updates"] = token_updates

    if not account_id:
        result.update(
            {
                "state": "missing_account_id",
                "message": "缺少 Chatgpt-Account-Id，无法查询 Codex 用量",
            }
        )
        return result

    codex_result = _probe_codex_usage(probe_access_token, account_id=account_id, proxy=effective_proxy)
    result.update(
        {
            "http_status": codex_result.status_code,
            "error_code": codex_result.error_code,
            "message": codex_result.message,
        }
    )

    snapshot = parse_codex_usage_body(codex_result.body_json, updated_at=checked_at)
    if snapshot:
        result["usage_source"] = "wham_usage_body"
    if not snapshot:
        snapshot = parse_codex_rate_limit_headers(codex_result.headers, updated_at=checked_at)
        if snapshot:
            result["usage_source"] = "wham_usage_headers"

    if not snapshot and 200 <= int(codex_result.status_code or 0) < 300:
        # Some accounts / plans return no x-codex-* headers on the Responses probe,
        # so keep the sub2api-compatible Responses probe as a secondary fallback.
        responses_result = _probe_codex_responses(
            probe_access_token,
            account_id=account_id,
            proxy=effective_proxy,
            model=model or CODEX_PROBE_MODEL,
        )
        snapshot = parse_codex_rate_limit_headers(responses_result.headers, updated_at=checked_at)
        if snapshot:
            result["usage_source"] = "responses_headers"
            result["usage_http_status"] = responses_result.status_code
            result["usage_message"] = responses_result.message

    usage = build_codex_usage_extra_updates(snapshot, checked_at) if snapshot else {}
    if usage:
        result["usage"] = usage
        result["progress"] = build_codex_usage_progress_from_extra(usage)

    if 200 <= int(codex_result.status_code or 0) < 300:
        result["state"] = "usable"
        if usage:
            result["message"] = "Codex 用量查询成功"
        else:
            result["message"] = "Codex 接口可用，但响应头未返回用量窗口"
    elif codex_result.status_code == 401:
        if codex_result.error_code == "token_invalidated":
            result["state"] = _auth_state_for_source(token_source, invalidated=True)
        else:
            result["state"] = "unauthorized"
    elif is_account_deactivated_message(codex_result.error_code, codex_result.message):
        result["state"] = "account_deactivated"
    elif codex_result.status_code in (402, 403):
        result["state"] = "payment_required"
    elif codex_result.status_code == 429:
        result["state"] = "quota_exhausted"
    else:
        result["state"] = "probe_failed"

    return result


def persist_codex_usage_probe(account: Any, codex_probe: dict[str, Any], session: Any, *, commit: bool = True) -> dict[str, Any]:
    """Persist only Codex usage/auth cache fields.

    This intentionally does not apply the main ChatGPT status policy: Codex
    quota/auth probe failures must not turn an otherwise saved account invalid.
    """
    from services.chatgpt_account_state import classify_chatgpt_capabilities

    extra = _account_extra(account)
    chatgpt_local = extra.get("chatgpt_local") if isinstance(extra.get("chatgpt_local"), dict) else {}
    codex = chatgpt_local.get("codex") if isinstance(chatgpt_local.get("codex"), dict) else {}

    token_updates = codex_probe.get("_token_updates") if isinstance(codex_probe.get("_token_updates"), dict) else {}
    if token_updates.get("access_token"):
        extra["access_token"] = str(token_updates.get("access_token") or "").strip()
        if hasattr(account, "token"):
            account.token = extra["access_token"]
    if token_updates.get("refresh_token"):
        extra["refresh_token"] = str(token_updates.get("refresh_token") or "").strip()

    next_codex = {
        **codex,
        "state": str(codex_probe.get("state") or codex.get("state") or "not_checked").strip(),
        "checked_at": str(codex_probe.get("checked_at") or "").strip(),
        "source": str(codex_probe.get("source") or "").strip(),
        "http_status": int(codex_probe.get("http_status") or 0),
        "error_code": str(codex_probe.get("error_code") or "").strip(),
        "message": str(codex_probe.get("message") or "").strip(),
        "chatgpt_account_id": str(codex_probe.get("chatgpt_account_id") or codex.get("chatgpt_account_id") or "").strip(),
    }
    if isinstance(codex_probe.get("usage"), dict) and codex_probe["usage"]:
        existing_usage = codex.get("usage") if isinstance(codex.get("usage"), dict) else {}
        next_codex["usage"] = {**existing_usage, **codex_probe["usage"]}
    if isinstance(codex_probe.get("progress"), dict):
        next_codex["progress"] = codex_probe["progress"]

    chatgpt_local["codex"] = next_codex
    extra["chatgpt_local"] = chatgpt_local
    if hasattr(account, "set_extra"):
        account.set_extra(extra)

    extra["chatgpt_capabilities"] = classify_chatgpt_capabilities(account, local_probe=chatgpt_local)
    if hasattr(account, "set_extra"):
        account.set_extra(extra)
    if hasattr(account, "updated_at"):
        account.updated_at = _utcnow()

    session.add(account)
    account_id = int(getattr(account, "id", 0) or 0)
    if account_id > 0:
        from services.account_filters import upsert_account_list_state_for_account_ids

        upsert_account_list_state_for_account_ids(session, [account_id], commit=False)
    if commit:
        session.commit()
        try:
            session.refresh(account)
        except Exception:
            pass
    return next_codex


def refresh_codex_usage_for_saved_account(
    account: Any,
    session: Any,
    *,
    proxy: Optional[str] = None,
    force: bool = True,
    model: str = "",
    reason: str = "account_saved",
    commit: bool = True,
) -> dict[str, Any]:
    """Refresh one saved account's Codex usage cache without failing the save path."""
    account_id = int(getattr(account, "id", 0) or 0)
    if str(getattr(account, "platform", "") or "").strip().lower() != "chatgpt":
        return {"ok": False, "skipped": True, "reason": "not_chatgpt", "account_id": account_id}
    if not account_has_codex_auth_material(account):
        return {"ok": False, "skipped": True, "reason": "missing_auth", "account_id": account_id}

    try:
        probe = probe_codex_usage_window(
            _to_codex_probe_account(account),
            proxy=proxy,
            force=force,
            model=model,
        )
        codex = persist_codex_usage_probe(account, probe, session, commit=commit)
        return {
            "ok": str(probe.get("state") or "") in {"usable", "quota_exhausted"},
            "skipped": False,
            "reason": reason,
            "account_id": account_id,
            "state": str(codex.get("state") or "").strip(),
            "message": str(codex.get("message") or "").strip(),
            "has_usage": bool(codex.get("usage") if isinstance(codex, dict) else {}),
        }
    except Exception as exc:
        logger.warning(
            "Codex usage auto-refresh failed account_id=%s reason=%s error=%s",
            account_id or "-",
            reason,
            exc,
            exc_info=True,
        )
        return {
            "ok": False,
            "skipped": False,
            "reason": reason,
            "account_id": account_id,
            "state": "probe_failed",
            "message": str(exc),
        }


def schedule_codex_usage_refresh_for_account_id(
    account_id: Any,
    *,
    proxy: Optional[str] = None,
    force: bool = True,
    model: str = "",
    reason: str = "account_saved",
    delay_seconds: float = 0.0,
) -> bool:
    """Start a daemon refresh for a committed account id.

    The worker opens its own SQLModel session so callers can schedule this right
    after saving without sharing request/task sessions across threads.
    """
    try:
        account_id_value = int(account_id or 0)
    except Exception:
        account_id_value = 0
    if account_id_value <= 0:
        return False

    with _CODEX_AUTO_REFRESH_LOCK:
        if account_id_value in _CODEX_AUTO_REFRESH_IN_FLIGHT:
            return False
        _CODEX_AUTO_REFRESH_IN_FLIGHT.add(account_id_value)

    def _worker() -> None:
        try:
            delay = max(0.0, float(delay_seconds or 0.0))
            if delay > 0:
                time.sleep(delay)
            from core.db import AccountModel, engine
            from sqlmodel import Session

            with Session(engine) as session:
                account = session.get(AccountModel, account_id_value)
                if account is None:
                    return
                refresh_codex_usage_for_saved_account(
                    account,
                    session,
                    proxy=proxy,
                    force=force,
                    model=model,
                    reason=reason,
                    commit=True,
                )
        except Exception as exc:
            logger.warning(
                "Codex usage auto-refresh worker crashed account_id=%s reason=%s error=%s",
                account_id_value,
                reason,
                exc,
                exc_info=True,
            )
        finally:
            with _CODEX_AUTO_REFRESH_LOCK:
                _CODEX_AUTO_REFRESH_IN_FLIGHT.discard(account_id_value)

    try:
        thread = threading.Thread(
            target=_worker,
            name=f"codex-usage-refresh-{account_id_value}",
            daemon=True,
        )
        thread.start()
        return True
    except Exception as exc:
        with _CODEX_AUTO_REFRESH_LOCK:
            _CODEX_AUTO_REFRESH_IN_FLIGHT.discard(account_id_value)
        logger.warning(
            "Codex usage auto-refresh schedule failed account_id=%s reason=%s error=%s",
            account_id_value,
            reason,
            exc,
            exc_info=True,
        )
        return False


def json_dumps_compact(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))
