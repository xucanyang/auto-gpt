"""Coordinated ChatGPT Web logout and OAuth credential revocation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from .oauth_revoke import (
    TOKEN_TYPE_ACCESS,
    TOKEN_TYPE_REFRESH,
    OAuthTokenRevocationResult,
    create_openai_oauth_session,
    revoke_openai_oauth_token,
)
from .task_logging import sanitize_error_message
from .web_logout import logout_chatgpt_web_session


WEB_SECRET_KEYS = (
    "cookies",
    "cookie_header",
    "cookieHeader",
    "cookie",
    "cookie_jar",
    "session_token",
    "sessionToken",
    "nextauth_session_token",
    "web_session_expires_at",
    "web_session_expiry_source",
    "web_session_observed_at",
)
ACCESS_TOKEN_KEYS = (
    "access_token",
    "accessToken",
    "webAccessToken",
    "access_token_captured_at",
    "access_token_expires_at",
    "access_token_expiry_source",
)
REFRESH_TOKEN_KEYS = ("refresh_token", "refreshToken")
ID_TOKEN_KEYS = ("id_token", "idToken")


@dataclass(frozen=True)
class FullCredentialLogoutResult:
    success: bool
    status: str
    components: dict[str, dict[str, Any]]
    remove_extra_keys: tuple[str, ...]
    clear_account_token: bool
    auth_material_changed: bool
    message: str
    logs: tuple[str, ...]
    completed_at: str

    def audit_payload(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "at": self.completed_at,
            "status": self.status,
            "components": self.components,
        }


def _absent_component() -> dict[str, Any]:
    return {
        "present": False,
        "success": True,
        "status": "absent",
        "http_status": 0,
        "error_code": "",
        "error_message": "",
        "verification_http_status": 0,
    }


def _unique_tokens(primary: str, additional: Iterable[str] | None) -> tuple[str, ...]:
    values: list[str] = []
    seen: set[str] = set()
    for candidate in (primary, *(additional or ())):
        token = str(candidate or "").strip()
        if not token or token in seen:
            continue
        seen.add(token)
        values.append(token)
    return tuple(values)


def _oauth_component(results: list[OAuthTokenRevocationResult]) -> tuple[dict[str, Any], bool]:
    if not results:
        return _absent_component(), False

    items: list[dict[str, Any]] = []
    for result in results:
        payload = result.public_dict()
        payload.pop("token_type", None)
        items.append(payload)
    removable = all(result.removable for result in results)
    success = all(result.success for result in results)
    if removable:
        status = "already_invalid" if all(result.status == "already_invalid" for result in results) else "revoked"
    else:
        status = "failed"
    failure = next((result for result in results if not result.success), None)
    last = results[-1]
    return (
        {
            "present": True,
            "success": success,
            "status": status,
            "count": len(results),
            "completed_count": sum(1 for result in results if result.removable),
            "http_status": int((failure or last).http_status or 0),
            "error_code": str(failure.error_code if failure else ""),
            "error_message": str(failure.error_message if failure else ""),
            "verification_http_status": int((failure or last).verification_http_status or 0),
            "items": items,
        },
        removable,
    )


def _component_log(label: str, component: dict[str, Any]) -> str:
    status = str(component.get("status") or "failed")
    labels = {
        "absent": "本地未保存，已跳过",
        "signed_out": "已退出并清除本地材料",
        "revoked": "已撤销并确认失效",
        "already_invalid": "已经失效，本地材料已清除",
        "failed": "处理失败，本地材料已保留",
    }
    line = f"{label}：{labels.get(status, status)}"
    error = str(component.get("error_message") or "").strip()
    if error:
        line = f"{line}；{error}"
    return line


def _safe_web_error(value: Any, *secrets: str) -> str:
    message = sanitize_error_message(value)
    for secret in secrets:
        normalized = str(secret or "").strip()
        if normalized:
            message = message.replace(normalized, "[REDACTED]")
    return message[:500]


def logout_and_revoke_chatgpt_credentials(
    *,
    cookies: str = "",
    session_token: str = "",
    access_token: str = "",
    refresh_token: str = "",
    id_token: str = "",
    access_tokens: Iterable[str] | None = None,
    refresh_tokens: Iterable[str] | None = None,
    client_id: str = "",
    proxy_url: str | None = None,
    user_agent: str = "",
    accept_language: str = "",
) -> FullCredentialLogoutResult:
    components: dict[str, dict[str, Any]] = {}
    remove_keys: list[str] = []

    has_web_material = bool(str(cookies or "").strip() or str(session_token or "").strip())
    if has_web_material:
        web = logout_chatgpt_web_session(
            cookies=cookies,
            session_token=session_token,
            proxy_url=proxy_url,
            user_agent=user_agent,
            accept_language=accept_language,
        )
        components["web_session"] = {
            "present": True,
            "success": bool(web.success),
            "status": "signed_out" if web.success else "failed",
            "http_status": int(web.status_code or 0),
            "error_code": "" if web.success else "web_logout_failed",
            "error_message": _safe_web_error(web.error_message, cookies, session_token),
            "verification_http_status": 0,
        }
        if web.success:
            remove_keys.extend(WEB_SECRET_KEYS)
    else:
        components["web_session"] = _absent_component()

    saved_refresh_tokens = _unique_tokens(refresh_token, refresh_tokens)
    saved_access_tokens = _unique_tokens(access_token, access_tokens)
    oauth_session = None
    oauth_session_error = ""
    if saved_refresh_tokens or saved_access_tokens:
        try:
            oauth_session = create_openai_oauth_session(proxy_url)
        except Exception as exc:
            oauth_session_error = _safe_web_error(exc, *saved_refresh_tokens, *saved_access_tokens)

    if oauth_session_error:
        refresh_results = [
            OAuthTokenRevocationResult(
                TOKEN_TYPE_REFRESH,
                False,
                "failed",
                error_code="revoke_session_error",
                error_message=oauth_session_error,
            )
            for _ in saved_refresh_tokens
        ]
        access_results = [
            OAuthTokenRevocationResult(
                TOKEN_TYPE_ACCESS,
                False,
                "failed",
                error_code="revoke_session_error",
                error_message=oauth_session_error,
            )
            for _ in saved_access_tokens
        ]
    else:
        try:
            refresh_results = [
                revoke_openai_oauth_token(
                    token=token,
                    token_type=TOKEN_TYPE_REFRESH,
                    client_id=client_id,
                    proxy_url=proxy_url,
                    session=oauth_session,
                )
                for token in saved_refresh_tokens
            ]
            access_results = [
                revoke_openai_oauth_token(
                    token=token,
                    token_type=TOKEN_TYPE_ACCESS,
                    proxy_url=proxy_url,
                    session=oauth_session,
                )
                for token in saved_access_tokens
            ]
        finally:
            if oauth_session is not None:
                try:
                    oauth_session.close()
                except Exception:
                    pass

    components["refresh_token"], refresh_removable = _oauth_component(refresh_results)
    if refresh_removable:
        remove_keys.extend(REFRESH_TOKEN_KEYS)

    components["access_token"], access_removable = _oauth_component(access_results)
    if access_removable:
        remove_keys.extend(ACCESS_TOKEN_KEYS)

    oauth_complete = components["refresh_token"]["success"] and components["access_token"]["success"]
    has_id_token = bool(str(id_token or "").strip())
    if oauth_complete and has_id_token:
        remove_keys.extend(ID_TOKEN_KEYS)

    success = all(bool(component.get("success")) for component in components.values())
    changed = bool(
        (has_web_material and components["web_session"]["status"] == "signed_out")
        or (bool(saved_refresh_tokens) and refresh_removable)
        or (bool(saved_access_tokens) and access_removable)
        or (has_id_token and oauth_complete)
    )
    if success:
        status = "completed"
        if any(bool(component.get("present")) for component in components.values()) or has_id_token:
            message = "ChatGPT 网页会话、AccessToken 与 RefreshToken 已退出、撤销或确认失效"
        else:
            message = "账号未保存 Web 会话、AccessToken 或 RefreshToken，无需退出"
    elif any(str(component.get("status") or "") in {"signed_out", "revoked", "already_invalid"} for component in components.values()):
        status = "partial"
        message = "ChatGPT 凭证仅部分退出；失败项的本地材料已保留，可重试"
    else:
        status = "failed"
        message = "ChatGPT 凭证退出失败；本地认证材料未删除"

    logs = (
        _component_log("网页会话", components["web_session"]),
        _component_log("RefreshToken", components["refresh_token"]),
        _component_log("AccessToken", components["access_token"]),
    )
    return FullCredentialLogoutResult(
        success=success,
        status=status,
        components=components,
        remove_extra_keys=tuple(dict.fromkeys(remove_keys)),
        clear_account_token=bool(str(access_token or "").strip()) and access_removable,
        auth_material_changed=changed,
        message=message,
        logs=logs,
        completed_at=datetime.now(timezone.utc).isoformat(),
    )
