"""K12 workspace join 与 ChatGPT 全空间 AccessToken 捕获。

设计边界：
- K12 是注册后的增强能力，不复制注册链路、不重新登录。
- primary/free token 保持注册结果；额外空间通过 workspace_artifacts 保存为账号变体。
- 只保存 AT/session/cookies；Refresh Token variants 先保留配置占位，不在该模块抓取。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional
from urllib.parse import quote

from curl_cffi import requests as cffi_requests

from .utils import decode_jwt_payload

CHATGPT_BASE_URL = "https://chatgpt.com"
ACCOUNTS_CHECK_URL = f"{CHATGPT_BASE_URL}/backend-api/accounts/check/v4-2023-04-27"


@dataclass(frozen=True)
class K12JoinResult:
    workspace_id: str
    ok: bool
    status_code: int = 0
    message: str = ""
    response_snippet: str = ""
    error: str = ""
    error_code: str = ""
    already_joined: bool = False

    def to_summary(self) -> dict[str, Any]:
        return asdict(self)

    def to_dict(self) -> dict[str, Any]:
        return self.to_summary()


@dataclass(frozen=True)
class ChatGPTSpace:
    workspace_id: str
    account_id: str
    name: str = ""
    structure: str = ""
    plan_type: str = ""
    scope: str = ""
    is_default: bool = False
    raw: dict[str, Any] | None = None

    def to_summary(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "account_id": self.account_id,
            "name": self.name,
            "structure": self.structure,
            "plan_type": self.plan_type,
            "scope": self.scope,
            "is_default": bool(self.is_default),
        }


class K12WorkspaceCaptureError(RuntimeError):
    """K12 workspace 捕获失败；错误文本必须脱敏。"""


def _log(log_fn: Optional[Callable[..., None]], message: str, level: str = "info") -> None:
    if not callable(log_fn):
        return
    try:
        log_fn(message, level)
    except TypeError:
        log_fn(message)


def _check_stopped(stop_checker: Optional[Callable[[], Any]] = None) -> None:
    if callable(stop_checker):
        stop_checker()


def _sleep_with_stop(seconds: int | float, stop_checker: Optional[Callable[[], Any]] = None) -> None:
    remaining = max(float(seconds or 0), 0.0)
    while remaining > 0:
        _check_stopped(stop_checker)
        interval = min(remaining, 1.0)
        time.sleep(interval)
        remaining -= interval
    _check_stopped(stop_checker)


def _parse_bool(value: Any, *, default: bool = False) -> bool:
    if value is None or value == "":
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on", "enable", "enabled", "y", "开启", "启用"}:
        return True
    if text in {"0", "false", "no", "off", "disable", "disabled", "n", "关闭", "禁用"}:
        return False
    return bool(default)


def _parse_int(value: Any, *, default: int, minimum: int = 0, maximum: int | None = None) -> int:
    try:
        parsed = int(float(str(value or "").strip()))
    except Exception:
        parsed = int(default)
    parsed = max(parsed, int(minimum))
    if maximum is not None:
        parsed = min(parsed, int(maximum))
    return parsed


def _parse_poll_seconds(value: Any, *, default: Iterable[int] = (3, 8, 15)) -> list[int]:
    if isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        raw_items = re.split(r"[\s,;，；]+", str(value or "").strip()) if str(value or "").strip() else list(default)
    seconds: list[int] = []
    for raw in raw_items:
        try:
            second = int(float(str(raw or "").strip()))
        except Exception:
            continue
        if second < 0:
            continue
        if second not in seconds:
            seconds.append(min(second, 120))
    return seconds or list(default)


def _config_get(config: dict[str, Any], key: str, fallback_key: str = "", default: Any = None) -> Any:
    nested = config.get("chatgpt_k12") if isinstance(config.get("chatgpt_k12"), dict) else {}
    if key in config:
        return config.get(key)
    if fallback_key and fallback_key in config:
        return config.get(fallback_key)
    nested_key = key.removeprefix("chatgpt_k12_")
    if nested_key in nested:
        return nested.get(nested_key)
    if fallback_key:
        nested_fallback_key = fallback_key.removeprefix("chatgpt_k12_")
        if nested_fallback_key in nested:
            return nested.get(nested_fallback_key)
    return default


def parse_k12_workspace_ids(value: Any) -> list[str]:
    """解析逗号/空白/换行/分号分隔的 workspace id，保持顺序去重。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        raw_items: list[Any] = []
        for item in value:
            if isinstance(item, str):
                raw_items.extend(re.split(r"[\s,;，；]+", item.strip()))
            else:
                raw_items.append(item)
    else:
        raw_items = re.split(r"[\s,;，；]+", str(value or "").strip())

    workspace_ids: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        text = str(item or "").strip().strip('"\'')
        if not text:
            continue
        if "/" in text:
            text = text.rstrip("/").rsplit("/", 1)[-1]
        if "?" in text:
            text = text.split("?", 1)[0]
        if not text or text in seen:
            continue
        seen.add(text)
        workspace_ids.append(text)
    return workspace_ids


parse_workspace_ids = parse_k12_workspace_ids


def k12_capture_enabled(config: dict[str, Any] | None) -> bool:
    config = dict(config or {})
    enabled_raw = _config_get(config, "chatgpt_k12_enabled", default=None)
    if enabled_raw not in (None, ""):
        return _parse_bool(enabled_raw, default=False)
    target_ids = parse_k12_workspace_ids(_config_get(config, "chatgpt_k12_workspace_ids"))
    # save_all_spaces 只有显式配置为 true 时可单独触发；避免老任务在无 K12 配置时突然增加外部请求。
    save_all_raw = _config_get(config, "chatgpt_k12_save_all_spaces", default=None)
    save_all_spaces = _parse_bool(save_all_raw, default=False)
    return bool(target_ids or save_all_spaces)


def _safe_snippet(value: Any, limit: int = 300) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = re.sub(r"Bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer ***", text, flags=re.I)
    text = re.sub(
        r"(?i)(access[_-]?token|refresh[_-]?token|id[_-]?token|session[_-]?token|authorization|cookie_header|cookies?|set-cookie)([\"']?\s*[:=]\s*[\"']?)[^\"',;}\]\s]+",
        r"\1\2***",
        text,
    )
    text = re.sub(
        r"(?i)(__Secure-[^=;\s]+|authjs[^=;\s]*|next-auth[^=;\s]*|oai-client-auth-session|oai-did|cf_clearance)=([^;\s]+)",
        r"\1=***",
        text,
    )
    return text[:limit]


def safe_k12_error(value: Any, limit: int = 300) -> str:
    return _safe_snippet(value, limit)


def _local_timezone_offset_minutes() -> int:
    try:
        if time.daylight and time.localtime().tm_isdst > 0:
            offset = -time.altzone
        else:
            offset = -time.timezone
        return int(offset / 60)
    except Exception:
        return 0


def _response_text(response: Any) -> str:
    text = getattr(response, "text", "")
    if text:
        return str(text)
    content = getattr(response, "content", b"")
    if isinstance(content, bytes):
        return content.decode("utf-8", errors="ignore")
    return str(content or "")


def _safe_json(response: Any) -> Any:
    try:
        return response.json()
    except Exception:
        text = _response_text(response)
        if not text:
            return {}
        try:
            return json.loads(text)
        except Exception:
            return {}


def _headers_from_client(
    chatgpt_client: Any = None,
    url: str = "",
    *,
    access_token: str = "",
    cookies: str = "",
    method: str = "GET",
) -> dict[str, str]:
    header_builder = getattr(chatgpt_client, "_headers", None)
    if callable(header_builder):
        try:
            headers = dict(
                header_builder(
                    url,
                    accept="application/json",
                    referer=f"{CHATGPT_BASE_URL}/",
                    fetch_site="same-origin",
                )
            )
        except Exception:
            headers = {}
    else:
        headers = {
            "Accept": "application/json",
            "Origin": CHATGPT_BASE_URL,
            "Referer": f"{CHATGPT_BASE_URL}/",
            "User-Agent": "Mozilla/5.0",
        }
        if method.upper() != "GET":
            headers["Content-Type"] = "application/json"
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    if cookies:
        headers["Cookie"] = cookies
    return headers


def _session_from(chatgpt_client: Any = None, http: Any = None) -> Any:
    if http is not None:
        return http
    session = getattr(chatgpt_client, "session", None)
    if session is not None:
        return session
    return cffi_requests


def request_k12_workspace_join(
    workspace_id: str = "",
    *,
    chatgpt_client: Any = None,
    http: Any = None,
    access_token: str,
    cookies: str = "",
    timeout: int = 60,
    timeout_seconds: int | None = None,
    stop_checker: Optional[Callable[[], Any]] = None,
) -> K12JoinResult:
    workspace_id = str(workspace_id or "").strip()
    if not workspace_id:
        return K12JoinResult(workspace_id="", ok=False, message="workspace_id 为空", error="workspace_id 为空")
    if not str(access_token or "").strip():
        return K12JoinResult(workspace_id=workspace_id, ok=False, message="缺少 access_token", error="缺少 access_token")

    url = f"{CHATGPT_BASE_URL}/backend-api/accounts/{quote(workspace_id, safe='')}/invites/request"
    headers = _headers_from_client(chatgpt_client, url, access_token=str(access_token or "").strip(), cookies=cookies, method="POST")
    session = _session_from(chatgpt_client, http)
    _check_stopped(stop_checker)
    try:
        response = session.post(url, headers=headers, data="", timeout=int(timeout_seconds or timeout or 60))
    except Exception as exc:
        message = _safe_snippet(str(exc) or exc.__class__.__name__)
        return K12JoinResult(workspace_id=workspace_id, ok=False, message=message, error=message)
    _check_stopped(stop_checker)

    status_code = int(getattr(response, "status_code", 0) or 0)
    payload = _safe_json(response)
    text = _response_text(response).strip()
    error_payload = payload.get("error") if isinstance(payload, dict) and isinstance(payload.get("error"), dict) else {}
    error_code = str(error_payload.get("code") or (payload.get("code") if isinstance(payload, dict) else "") or "").strip()
    error_message = str(error_payload.get("message") or (payload.get("message") if isinstance(payload, dict) else "") or text or "").strip()
    lowered = f"{error_code} {error_message} {text}".lower()
    already_joined = status_code in {200, 204, 400, 409} and any(marker in lowered for marker in ("already", "member", "joined"))
    ok = 200 <= status_code < 300 or already_joined
    message = "already_joined" if already_joined else ("joined" if ok else f"HTTP {status_code}")
    safe_body = _safe_snippet(error_message or text)
    if not ok and safe_body:
        message = f"{message}: {safe_body}"
    return K12JoinResult(
        workspace_id=workspace_id,
        ok=ok,
        status_code=status_code,
        message=message,
        response_snippet=safe_body,
        error="" if ok else safe_body,
        error_code=error_code,
        already_joined=already_joined,
    )


def _account_item_scope(item: dict[str, Any], *, is_default: bool) -> str:
    account_info = item.get("account") if isinstance(item.get("account"), dict) else {}
    workspace_info = item.get("workspace") if isinstance(item.get("workspace"), dict) else {}
    plan = account_info.get("plan") if isinstance(account_info.get("plan"), dict) else {}
    structure = str(account_info.get("structure") or workspace_info.get("structure") or item.get("structure") or "").strip().lower()
    markers = " ".join(
        str(value or "").strip().lower()
        for value in (
            account_info.get("plan_type"),
            workspace_info.get("plan_type"),
            plan.get("type"),
            plan.get("plan_type"),
            item.get("plan_type"),
            account_info.get("name"),
            workspace_info.get("name"),
            item.get("name"),
        )
        if str(value or "").strip()
    )
    if any(marker in markers for marker in ("k12", "edu", "education", "school", "student")):
        return "k12"
    if is_default or structure in {"", "personal", "individual", "free"}:
        return "free"
    return "workspace"


def normalize_accounts_check_response(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    accounts = data.get("accounts") if isinstance(data.get("accounts"), dict) else {}
    ordering = data.get("account_ordering") if isinstance(data.get("account_ordering"), list) else []
    ordered_keys: list[str] = []
    for raw_key in list(ordering) + list(accounts.keys()):
        key = str(raw_key or "").strip()
        if key and key not in ordered_keys:
            ordered_keys.append(key)

    workspaces: list[dict[str, Any]] = []
    seen: set[str] = set()
    for key in ordered_keys:
        item = accounts.get(key)
        if not isinstance(item, dict):
            continue
        account_info = item.get("account") if isinstance(item.get("account"), dict) else {}
        workspace_info = item.get("workspace") if isinstance(item.get("workspace"), dict) else {}
        is_default = bool(key == "default" or item.get("is_default") or account_info.get("is_default") or item.get("default"))
        account_id = str(
            account_info.get("id")
            or account_info.get("account_id")
            or item.get("account_id")
            or ("" if is_default else key)
            or ""
        ).strip()
        workspace_id = str(
            account_info.get("workspace_id")
            or workspace_info.get("id")
            or item.get("workspace_id")
            or account_id
            or ("" if is_default else key)
            or ""
        ).strip()
        if not account_id:
            account_id = workspace_id
        if not workspace_id:
            workspace_id = account_id
        if not workspace_id and not account_id:
            continue
        dedupe_key = workspace_id or account_id
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        structure = str(account_info.get("structure") or workspace_info.get("structure") or item.get("structure") or ("personal" if is_default else "")).strip().lower()
        plan_type = str(account_info.get("plan_type") or workspace_info.get("plan_type") or item.get("plan_type") or "").strip().lower()
        label = str(
            workspace_info.get("name")
            or workspace_info.get("display_name")
            or account_info.get("name")
            or item.get("name")
            or ("Personal" if is_default else workspace_id or account_id)
        ).strip()
        scope = _account_item_scope(item, is_default=is_default)
        workspaces.append(
            {
                "key": key,
                "account_id": account_id,
                "workspace_id": workspace_id,
                "label": label,
                "name": label,
                "structure": structure,
                "space": structure or ("personal" if is_default else "workspace"),
                "plan_type": plan_type,
                "scope": scope,
                "is_default": is_default,
                "raw": {
                    "key": key,
                    "account": {
                        candidate_key: account_info.get(candidate_key)
                        for candidate_key in ("id", "account_id", "workspace_id", "name", "structure", "plan_type")
                        if account_info.get(candidate_key) not in (None, "")
                    },
                },
            }
        )
    return workspaces


def normalize_account_spaces(data: dict[str, Any]) -> list[ChatGPTSpace]:
    spaces: list[ChatGPTSpace] = []
    for item in normalize_accounts_check_response(data):
        spaces.append(
            ChatGPTSpace(
                workspace_id=str(item.get("workspace_id") or ""),
                account_id=str(item.get("account_id") or ""),
                name=str(item.get("label") or item.get("name") or ""),
                structure=str(item.get("structure") or item.get("space") or ""),
                plan_type=str(item.get("plan_type") or ""),
                scope=str(item.get("scope") or ""),
                is_default=bool(item.get("is_default")),
                raw=dict(item.get("raw") or {}),
            )
        )
    return spaces


def fetch_accounts_check_workspaces(
    *,
    http: Any = None,
    chatgpt_client: Any = None,
    access_token: str,
    cookies: str = "",
    proxy: str = "",
    timeout_seconds: int = 30,
    timezone_offset_min: int | None = None,
    stop_checker: Optional[Callable[[], Any]] = None,
) -> list[dict[str, Any]]:
    token = str(access_token or "").strip()
    if not token:
        raise K12WorkspaceCaptureError("缺少 access_token")
    offset = _local_timezone_offset_minutes() if timezone_offset_min is None else int(timezone_offset_min)
    url = f"{ACCOUNTS_CHECK_URL}?timezone_offset_min={offset}"
    headers = _headers_from_client(chatgpt_client, url, access_token=token, cookies=cookies, method="GET")
    session = _session_from(chatgpt_client, http)
    _check_stopped(stop_checker)
    try:
        if session is cffi_requests:
            response = session.get(
                url,
                headers=headers,
                proxies={"http": proxy, "https": proxy} if proxy else None,
                timeout=timeout_seconds,
                impersonate="chrome110",
            )
        else:
            response = session.get(url, headers=headers, timeout=timeout_seconds)
    except Exception as exc:
        raise K12WorkspaceCaptureError(_safe_snippet(exc, 180)) from exc
    _check_stopped(stop_checker)

    status_code = int(getattr(response, "status_code", 0) or 0)
    if status_code and status_code >= 400:
        raise K12WorkspaceCaptureError(f"accounts/check HTTP {status_code}: {_safe_snippet(_response_text(response), 180)}")
    data = _safe_json(response)
    if not isinstance(data, dict):
        raise K12WorkspaceCaptureError("accounts/check 返回非对象")
    return normalize_accounts_check_response(data)


def fetch_account_spaces(
    *,
    chatgpt_client: Any,
    access_token: str,
    cookies: str = "",
    proxy: str = "",
    timeout: int = 30,
    stop_checker: Optional[Callable[[], Any]] = None,
) -> tuple[list[ChatGPTSpace], dict[str, Any], str]:
    try:
        workspaces = fetch_accounts_check_workspaces(
            chatgpt_client=chatgpt_client,
            access_token=access_token,
            cookies=cookies,
            proxy=proxy,
            timeout_seconds=timeout,
            stop_checker=stop_checker,
        )
    except K12WorkspaceCaptureError as exc:
        return [], {}, str(exc)
    spaces = [
        ChatGPTSpace(
            workspace_id=str(item.get("workspace_id") or ""),
            account_id=str(item.get("account_id") or ""),
            name=str(item.get("label") or item.get("name") or ""),
            structure=str(item.get("structure") or item.get("space") or ""),
            plan_type=str(item.get("plan_type") or ""),
            scope=str(item.get("scope") or ""),
            is_default=bool(item.get("is_default")),
            raw=dict(item.get("raw") or {}),
        )
        for item in workspaces
    ]
    return spaces, {"count": len(spaces)}, ""


def _normalize_session_payload(
    *,
    session_data: dict[str, Any],
    fallback_session_token: str = "",
    fallback_cookies: str = "",
    fallback_workspace_id: str = "",
) -> dict[str, Any]:
    access_token = str(session_data.get("accessToken") or session_data.get("access_token") or "").strip()
    user = session_data.get("user") if isinstance(session_data.get("user"), dict) else {}
    account = session_data.get("account") if isinstance(session_data.get("account"), dict) else {}
    jwt_payload = decode_jwt_payload(access_token) if access_token else {}
    auth_payload = jwt_payload.get("https://api.openai.com/auth") if isinstance(jwt_payload, dict) else {}
    auth_payload = auth_payload if isinstance(auth_payload, dict) else {}
    account_id = str(account.get("id") or account.get("account_id") or auth_payload.get("chatgpt_account_id") or fallback_workspace_id or "").strip()
    user_id = str(user.get("id") or auth_payload.get("chatgpt_user_id") or auth_payload.get("user_id") or "").strip()
    return {
        "access_token": access_token,
        "session_token": str(session_data.get("sessionToken") or session_data.get("session_token") or fallback_session_token or "").strip(),
        "cookies": str(session_data.get("cookies") or fallback_cookies or "").strip(),
        "account_id": account_id,
        "user_id": user_id,
        "workspace_id": str(fallback_workspace_id or account_id or "").strip(),
        "expires": session_data.get("expires"),
        "user": user,
        "account": account,
        "auth_provider": session_data.get("authProvider") or session_data.get("auth_provider") or "",
    }


def exchange_workspace_session(
    *,
    chatgpt_client: Any,
    workspace_id: str,
    session_token: str = "",
    cookies: str = "",
    stop_checker: Optional[Callable[[], Any]] = None,
) -> tuple[bool, dict[str, Any], str]:
    workspace_id = str(workspace_id or "").strip()
    fetch_session = getattr(chatgpt_client, "fetch_chatgpt_session", None)
    if not callable(fetch_session):
        return False, {}, "ChatGPTClient 不支持 fetch_chatgpt_session"
    _check_stopped(stop_checker)
    try:
        ok, session_or_error = fetch_session(workspace_id=workspace_id, workspace_reason="setCurrentAccount")
    except TypeError:
        try:
            ok, session_or_error = fetch_session(workspace_id)
        except Exception as exc:
            return False, {}, _safe_snippet(exc, 300)
    except Exception as exc:
        return False, {}, _safe_snippet(exc, 300)
    _check_stopped(stop_checker)

    if not ok:
        return False, {}, _safe_snippet(session_or_error, 300) or "workspace session exchange failed"
    if not isinstance(session_or_error, dict):
        return False, {}, "workspace session 返回非对象"
    normalized = _normalize_session_payload(
        session_data=session_or_error,
        fallback_session_token=session_token,
        fallback_cookies=cookies,
        fallback_workspace_id=workspace_id,
    )
    if not normalized.get("cookies"):
        get_cookie_header = getattr(chatgpt_client, "get_chatgpt_cookie_header", None)
        if callable(get_cookie_header):
            try:
                normalized["cookies"] = str(get_cookie_header() or "")
            except Exception:
                pass
    if not normalized.get("session_token"):
        get_session_token = getattr(chatgpt_client, "get_next_auth_session_token", None)
        if callable(get_session_token):
            try:
                normalized["session_token"] = str(get_session_token() or "")
            except Exception:
                pass
    if not normalized.get("access_token"):
        return False, normalized, "workspace session 未返回 access_token"
    return True, normalized, ""


def _space_scope(space: ChatGPTSpace, *, target_workspace_ids: set[str]) -> str:
    if space.workspace_id in target_workspace_ids or space.account_id in target_workspace_ids or space.scope == "k12":
        return "k12"
    if space.is_default or space.scope == "free" or space.structure in {"personal", "individual", "free"}:
        return "free"
    return "workspace"


def _space_label(scope: str) -> str:
    if scope == "k12":
        return "k12"
    if scope == "workspace":
        return "workspace"
    return "free"


def _build_workspace_artifact(
    *,
    space: ChatGPTSpace,
    session_tokens: dict[str, Any],
    target_workspace_ids: set[str],
    join_results_by_workspace_id: dict[str, K12JoinResult],
    fallback_session_token: str,
    fallback_cookies: str,
) -> dict[str, Any]:
    scope = _space_scope(space, target_workspace_ids=target_workspace_ids)
    label = _space_label(scope)
    workspace_id = str(session_tokens.get("workspace_id") or space.workspace_id or space.account_id or "").strip()
    account_id = str(session_tokens.get("account_id") or space.account_id or workspace_id or "").strip()
    variant_id = workspace_id or account_id or space.workspace_id or "unknown"
    join_result = join_results_by_workspace_id.get(space.workspace_id) or join_results_by_workspace_id.get(space.account_id)
    artifact = {
        "scope": scope,
        "label": label,
        "account_id": account_id,
        "workspace_id": workspace_id or account_id,
        "access_token": str(session_tokens.get("access_token") or "").strip(),
        "refresh_token": "",
        "id_token": str(session_tokens.get("id_token") or "").strip(),
        "session_token": str(session_tokens.get("session_token") or fallback_session_token or "").strip(),
        "cookies": str(session_tokens.get("cookies") or fallback_cookies or "").strip(),
        "source": "k12_workspace_join" if scope == "k12" else "all_spaces_capture",
        "variant_key": f"{scope}:{variant_id}",
        "auth_level": "access_token_only",
        "partial_auth": True,
        "display_name": str(space.name or "").strip() or label,
        "space": {
            "workspace_id": workspace_id or account_id,
            "account_id": account_id,
            "name": space.name,
            "structure": space.structure,
            "plan_type": space.plan_type,
            "is_default": bool(space.is_default),
            "source": "accounts_check",
        },
        "all_spaces_capture": True,
    }
    if join_result is not None:
        artifact["k12_join"] = join_result.to_summary()
    return artifact


def _target_ids_found(spaces: list[ChatGPTSpace], target_ids: set[str]) -> bool:
    if not target_ids:
        return True
    found = {space.workspace_id for space in spaces} | {space.account_id for space in spaces}
    return target_ids.issubset(found)


def _dedupe_artifacts(artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        variant_key = str(artifact.get("variant_key") or "").strip()
        workspace_id = str(artifact.get("workspace_id") or "").strip()
        account_id = str(artifact.get("account_id") or "").strip()
        key = variant_key or f"{artifact.get('scope') or ''}:{workspace_id or account_id}"
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(artifact)
    return deduped


dedupe_workspace_artifacts = _dedupe_artifacts


def build_k12_workspace_artifacts(
    workspaces: list[dict[str, Any]],
    *,
    fetch_workspace_session: Callable[[str], tuple[bool, dict[str, Any] | str]],
    fallback_cookies: str = "",
    fallback_session_token: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    artifacts: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    seen_workspace_ids: set[str] = set()
    for item in workspaces:
        if not isinstance(item, dict):
            continue
        workspace_id = str(item.get("workspace_id") or item.get("account_id") or "").strip()
        if not workspace_id or workspace_id in seen_workspace_ids:
            continue
        seen_workspace_ids.add(workspace_id)
        ok, payload_or_error = fetch_workspace_session(workspace_id)
        if not ok or not isinstance(payload_or_error, dict):
            errors.append({"workspace_id": workspace_id, "error": _safe_snippet(payload_or_error, 180)})
            continue
        account = payload_or_error.get("account") if isinstance(payload_or_error.get("account"), dict) else {}
        tokens = {
            "access_token": str(payload_or_error.get("accessToken") or payload_or_error.get("access_token") or "").strip(),
            "session_token": str(payload_or_error.get("sessionToken") or payload_or_error.get("session_token") or fallback_session_token or "").strip(),
            "cookies": fallback_cookies,
            "account_id": str(account.get("id") or item.get("account_id") or workspace_id).strip(),
            "workspace_id": workspace_id,
        }
        if not tokens["access_token"]:
            errors.append({"workspace_id": workspace_id, "error": "workspace session 未返回 accessToken"})
            continue
        space = ChatGPTSpace(
            workspace_id=workspace_id,
            account_id=str(item.get("account_id") or tokens["account_id"] or workspace_id).strip(),
            name=str(item.get("label") or item.get("name") or "").strip(),
            structure=str(item.get("structure") or item.get("space") or "").strip(),
            plan_type=str(item.get("plan_type") or "").strip(),
            scope=str(item.get("scope") or "").strip(),
            is_default=bool(item.get("is_default")),
            raw=dict(item.get("raw") or {}),
        )
        target_ids = {workspace_id} if space.scope == "k12" else set()
        artifact = _build_workspace_artifact(
            space=space,
            session_tokens=tokens,
            target_workspace_ids=target_ids,
            join_results_by_workspace_id={},
            fallback_session_token=fallback_session_token,
            fallback_cookies=fallback_cookies,
        )
        artifact["scope"] = space.scope or artifact["scope"]
        artifact["label"] = "k12" if artifact["scope"] == "k12" else (str(item.get("label") or artifact.get("label") or "workspace").strip())
        artifact["variant_key"] = f"{artifact['scope']}:{workspace_id}"
        artifacts.append(artifact)
    return _dedupe_artifacts(artifacts), errors


def capture_k12_and_all_spaces(
    *,
    chatgpt_client: Any,
    base_session: dict[str, Any] | None,
    access_token: str,
    session_token: str = "",
    cookies: str = "",
    target_workspace_ids: Any = None,
    proxy: str = "",
    config: dict[str, Any] | None = None,
    log_fn: Optional[Callable[..., None]] = None,
    stop_checker: Optional[Callable[[], Any]] = None,
) -> dict[str, Any]:
    """Join K12 targets, fetch all account spaces, and emit AT-only artifacts."""
    config = dict(config or {})
    stop_checker = stop_checker or config.get("_task_stop_checker")
    target_ids = parse_k12_workspace_ids(target_workspace_ids if target_workspace_ids is not None else _config_get(config, "chatgpt_k12_workspace_ids"))
    enabled_raw = _config_get(config, "chatgpt_k12_enabled", default=None)
    enabled_explicit_false = enabled_raw not in (None, "") and not _parse_bool(enabled_raw, default=False)
    enabled = _parse_bool(enabled_raw, default=bool(target_ids))
    save_all_spaces = _parse_bool(_config_get(config, "chatgpt_k12_save_all_spaces"), default=True)
    strict_join = _parse_bool(_config_get(config, "chatgpt_k12_strict_join"), default=False)
    timeout = _parse_int(_config_get(config, "chatgpt_k12_join_timeout_seconds", "chatgpt_k12_timeout_seconds"), default=60, minimum=5, maximum=180)
    retry_count = _parse_int(_config_get(config, "chatgpt_k12_join_retry_count", "chatgpt_k12_retry"), default=2, minimum=0, maximum=5)
    poll_seconds = _parse_poll_seconds(_config_get(config, "chatgpt_k12_post_join_poll_seconds", "chatgpt_k12_poll_interval_seconds"), default=(3, 8, 15))
    capture_refresh_tokens = _parse_bool(_config_get(config, "chatgpt_k12_capture_refresh_tokens"), default=False)

    if capture_refresh_tokens:
        _log(log_fn, "[K12] capture_refresh_tokens 当前仅为占位，本次仍按 AT-only 保存", "warning")

    if enabled_explicit_false:
        return {
            "artifacts": [],
            "spaces": [],
            "join_results": [],
            "summary": {
                "enabled": False,
                "targets": len(target_ids),
                "joined": 0,
                "failed": 0,
                "saved_spaces": 0,
                "save_all_spaces": False,
            },
        }

    if not enabled and not save_all_spaces:
        return {
            "artifacts": [],
            "spaces": [],
            "join_results": [],
            "summary": {"enabled": False, "targets": len(target_ids), "joined": 0, "failed": 0, "saved_spaces": 0},
        }

    access_token = str(access_token or (base_session or {}).get("access_token") or "").strip()
    session_token = str(session_token or (base_session or {}).get("session_token") or "").strip()
    cookies = str(cookies or (base_session or {}).get("cookies") or (base_session or {}).get("cookie_header") or "").strip()
    if not access_token:
        return {
            "artifacts": [],
            "spaces": [],
            "join_results": [],
            "summary": {
                "enabled": bool(enabled),
                "targets": len(target_ids),
                "joined": 0,
                "failed": len(target_ids),
                "saved_spaces": 0,
                "error": "missing_access_token",
                "strict_join_failed": bool(strict_join),
            },
        }

    _log(log_fn, f"[K12] 开始 workspace 捕获 targets={len(target_ids)} save_all_spaces={save_all_spaces}")
    join_results: list[K12JoinResult] = []
    if enabled and target_ids:
        for workspace_id in target_ids:
            _check_stopped(stop_checker)
            last_result = K12JoinResult(workspace_id=workspace_id, ok=False, message="未执行")
            for attempt in range(retry_count + 1):
                if attempt:
                    _sleep_with_stop(min(2 * attempt, 8), stop_checker)
                last_result = request_k12_workspace_join(
                    chatgpt_client=chatgpt_client,
                    workspace_id=workspace_id,
                    access_token=access_token,
                    cookies=cookies,
                    timeout=timeout,
                    stop_checker=stop_checker,
                )
                if last_result.ok:
                    break
            join_results.append(last_result)
            if last_result.ok:
                _log(log_fn, f"[K12] join 成功 workspace_id={workspace_id} status={last_result.status_code}")
            else:
                _log(log_fn, f"[K12] join 失败 workspace_id={workspace_id} status={last_result.status_code} {last_result.message}", "warning")

    failed_join_ids = [item.workspace_id for item in join_results if not item.ok]
    if failed_join_ids and strict_join:
        return {
            "artifacts": [],
            "spaces": [],
            "join_results": [item.to_summary() for item in join_results],
            "summary": {
                "enabled": bool(enabled),
                "targets": len(target_ids),
                "joined": len(target_ids) - len(failed_join_ids),
                "failed": len(failed_join_ids),
                "saved_spaces": 0,
                "strict_join_failed": True,
            },
        }

    spaces: list[ChatGPTSpace] = []
    accounts_check_summary: dict[str, Any] = {}
    accounts_check_error = ""
    for index, delay_seconds in enumerate([0] + (poll_seconds if enabled and target_ids else [])):
        if delay_seconds > 0:
            _sleep_with_stop(delay_seconds, stop_checker)
        spaces, accounts_check_summary, accounts_check_error = fetch_account_spaces(
            chatgpt_client=chatgpt_client,
            access_token=access_token,
            cookies=cookies,
            proxy=proxy,
            timeout=30,
            stop_checker=stop_checker,
        )
        if spaces and (index == len(poll_seconds) or _target_ids_found(spaces, set(target_ids))):
            break

    target_set = set(target_ids)
    selected_spaces = list(spaces) if save_all_spaces else [
        space for space in spaces if space.workspace_id in target_set or space.account_id in target_set
    ]
    found_ids = {space.workspace_id for space in spaces} | {space.account_id for space in spaces}
    for workspace_id in target_ids:
        if workspace_id in found_ids:
            continue
        selected_spaces.append(
            ChatGPTSpace(
                workspace_id=workspace_id,
                account_id=workspace_id,
                name="K12 Workspace",
                structure="workspace",
                plan_type="k12",
                scope="k12",
                is_default=False,
                raw={"source": "k12_join_fallback"},
            )
        )

    join_by_workspace_id = {item.workspace_id: item for item in join_results}
    artifacts: list[dict[str, Any]] = []
    exchange_failures: list[dict[str, Any]] = []
    for space in selected_spaces:
        _check_stopped(stop_checker)
        ok, session_tokens, error = exchange_workspace_session(
            chatgpt_client=chatgpt_client,
            workspace_id=space.workspace_id or space.account_id,
            session_token=session_token,
            cookies=cookies,
            stop_checker=stop_checker,
        )
        if not ok:
            exchange_failures.append({"workspace_id": space.workspace_id, "account_id": space.account_id, "message": _safe_snippet(error, 180)})
            _log(log_fn, f"[K12] workspace token 交换失败 workspace_id={space.workspace_id or '-'}: {_safe_snippet(error, 180)}", "warning")
            continue
        artifact = _build_workspace_artifact(
            space=space,
            session_tokens=session_tokens,
            target_workspace_ids=target_set,
            join_results_by_workspace_id=join_by_workspace_id,
            fallback_session_token=session_token,
            fallback_cookies=cookies,
        )
        if artifact.get("access_token"):
            artifacts.append(artifact)

    artifacts = _dedupe_artifacts(artifacts)
    spaces_summary = [space.to_summary() for space in spaces]
    joined_count = len([item for item in join_results if item.ok])
    saved_target_ids = {str(item.get("workspace_id") or item.get("account_id") or "") for item in artifacts if str(item.get("scope") or "") == "k12"}
    visible_target_ids = ({space.workspace_id for space in spaces} | {space.account_id for space in spaces}) & target_set
    target_exchange_failed_ids = {
        target_id
        for target_id in target_set
        for item in exchange_failures
        if target_id in {str(item.get("workspace_id") or ""), str(item.get("account_id") or "")}
    }
    strict_failed = bool(
        strict_join
        and target_set
        and (
            failed_join_ids
            or target_exchange_failed_ids
            or not target_set.issubset(visible_target_ids | saved_target_ids)
        )
    )
    summary = {
        "enabled": bool(enabled),
        "targets": len(target_ids),
        "joined": joined_count,
        "failed": len(target_ids) - joined_count,
        "join_failed_ids": failed_join_ids,
        "exchange_failed_target_ids": sorted(target_exchange_failed_ids),
        "saved_spaces": len(artifacts),
        "save_all_spaces": save_all_spaces,
        "accounts_check_error": accounts_check_error,
        "exchange_failed": len(exchange_failures),
        "strict_join_failed": strict_failed,
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }
    _log(log_fn, f"[K12] workspace 捕获完成 joined={summary['joined']}/{summary['targets']} saved_spaces={summary['saved_spaces']}")
    return {
        "artifacts": artifacts,
        "spaces": spaces_summary,
        "join_results": [item.to_summary() for item in join_results],
        "summary": summary,
        "accounts_check": accounts_check_summary,
        "exchange_failures": exchange_failures,
    }


def capture_k12_workspace_artifacts(*args, **kwargs) -> dict[str, Any]:
    return capture_k12_and_all_spaces(*args, **kwargs)
