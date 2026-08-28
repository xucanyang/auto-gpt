"""OpenAI OAuth token revocation with explicit access-token verification."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Iterable

from curl_cffi import requests as cffi_requests

from .constants import OAUTH_CLIENT_ID
from .sentinel_constants import PINNED_CURL_IMPERSONATE
from .task_logging import sanitize_error_message


OPENAI_OAUTH_REVOKE_URL = "https://auth.openai.com/oauth/revoke"
CHATGPT_ME_URL = "https://chatgpt.com/backend-api/me"
TOKEN_TYPE_ACCESS = "access_token"
TOKEN_TYPE_REFRESH = "refresh_token"
_SUPPORTED_TOKEN_TYPES = {TOKEN_TYPE_ACCESS, TOKEN_TYPE_REFRESH}
_ALREADY_INVALID_REFRESH_CODES = {
    "invalid_grant",
    "invalid_refresh_token",
    "refresh_token_expired",
    "refresh_token_revoked",
}


@dataclass(frozen=True)
class OAuthTokenRevocationResult:
    token_type: str
    success: bool
    status: str
    http_status: int = 0
    error_code: str = ""
    error_message: str = ""
    verification_http_status: int = 0

    @property
    def removable(self) -> bool:
        return self.success and self.status in {"revoked", "already_invalid"}

    def public_dict(self) -> dict[str, Any]:
        return {
            "token_type": self.token_type,
            "success": self.success,
            "status": self.status,
            "http_status": self.http_status,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "verification_http_status": self.verification_http_status,
        }


def _safe_message(value: Any, *secrets: str) -> str:
    message = sanitize_error_message(value)
    for secret in secrets:
        normalized = str(secret or "").strip()
        if normalized:
            message = message.replace(normalized, "[REDACTED_TOKEN]")
    return message[:500]


def _response_error(response: Any, *secrets: str) -> tuple[str, str]:
    payload: dict[str, Any] = {}
    try:
        candidate = response.json()
        if isinstance(candidate, dict):
            payload = candidate
    except Exception:
        pass

    raw_error = payload.get("error")
    error = raw_error if isinstance(raw_error, dict) else {}
    code = str(
        error.get("code")
        or payload.get("error_code")
        or payload.get("code")
        or (raw_error if isinstance(raw_error, str) else "")
        or ""
    ).strip()[:96]
    message = _safe_message(
        error.get("message")
        or payload.get("message")
        or f"OpenAI OAuth revoke 返回 HTTP {int(getattr(response, 'status_code', 0) or 0)}",
        *secrets,
    )
    return code, message


def create_openai_oauth_session(proxy_url: str | None) -> Any:
    return cffi_requests.Session(
        impersonate=PINNED_CURL_IMPERSONATE,
        proxy=str(proxy_url or "").strip() or None,
    )


def _verify_access_token_is_invalid(
    session: Any,
    access_token: str,
    *,
    verification_delays: Iterable[float],
) -> tuple[bool, int, str, str]:
    delays = tuple(max(0.0, float(value)) for value in verification_delays) or (0.0,)
    last_status = 0
    last_code = ""
    last_message = ""

    for delay in delays:
        if delay:
            time.sleep(delay)
        try:
            response = session.get(
                CHATGPT_ME_URL,
                headers={
                    "accept": "application/json",
                    "authorization": f"Bearer {access_token}",
                },
                timeout=15,
            )
        except Exception as exc:
            last_code = "access_token_verification_transport_error"
            last_message = _safe_message(exc, access_token)
            continue

        last_status = int(getattr(response, "status_code", 0) or 0)
        if last_status == 401:
            return True, last_status, "", ""
        if last_status == 200:
            last_code = "access_token_still_valid"
            last_message = "AccessToken 撤销后仍可访问 ChatGPT，未清除本地凭证"
            continue

        response_code, response_message = _response_error(response, access_token)
        last_code = response_code or "access_token_revocation_unverified"
        last_message = response_message or f"无法确认 AccessToken 已失效: HTTP {last_status or '未知'}"

    return False, last_status, last_code or "access_token_revocation_unverified", last_message or "无法确认 AccessToken 已失效"


def revoke_openai_oauth_token(
    *,
    token: str,
    token_type: str,
    client_id: str = "",
    proxy_url: str | None = None,
    session: Any | None = None,
    verification_delays: Iterable[float] = (0.0, 0.5, 1.5),
) -> OAuthTokenRevocationResult:
    """Revoke one OpenAI OAuth token without ever returning the token value."""

    normalized_token = str(token or "").strip()
    normalized_type = str(token_type or "").strip().lower()
    if normalized_type not in _SUPPORTED_TOKEN_TYPES:
        raise ValueError(f"不支持的 OAuth token_type: {normalized_type or '空'}")
    if not normalized_token:
        return OAuthTokenRevocationResult(normalized_type, True, "absent")

    try:
        http_session = session or create_openai_oauth_session(proxy_url)
    except Exception as exc:
        return OAuthTokenRevocationResult(
            normalized_type,
            False,
            "failed",
            error_code="revoke_session_error",
            error_message=_safe_message(exc, normalized_token),
        )
    payload: dict[str, str] = {
        "token": normalized_token,
        "token_type_hint": normalized_type,
    }
    if normalized_type == TOKEN_TYPE_REFRESH:
        payload["client_id"] = str(client_id or OAUTH_CLIENT_ID).strip() or OAUTH_CLIENT_ID

    try:
        response = http_session.post(
            OPENAI_OAUTH_REVOKE_URL,
            headers={
                "accept": "application/json",
                "content-type": "application/json",
            },
            json=payload,
            timeout=15,
        )
    except Exception as exc:
        return OAuthTokenRevocationResult(
            normalized_type,
            False,
            "failed",
            error_code="revoke_transport_error",
            error_message=_safe_message(exc, normalized_token),
        )

    http_status = int(getattr(response, "status_code", 0) or 0)
    error_code, error_message = _response_error(response, normalized_token)
    accepted = 200 <= http_status < 300
    already_invalid = (
        normalized_type == TOKEN_TYPE_REFRESH
        and error_code.lower() in _ALREADY_INVALID_REFRESH_CODES
    )

    if normalized_type == TOKEN_TYPE_REFRESH:
        if accepted:
            return OAuthTokenRevocationResult(normalized_type, True, "revoked", http_status=http_status)
        if already_invalid:
            return OAuthTokenRevocationResult(
                normalized_type,
                True,
                "already_invalid",
                http_status=http_status,
                error_code=error_code,
            )
        return OAuthTokenRevocationResult(
            normalized_type,
            False,
            "failed",
            http_status=http_status,
            error_code=error_code or "revoke_rejected",
            error_message=error_message,
        )

    if not accepted and error_code.lower() != "invalid_token":
        return OAuthTokenRevocationResult(
            normalized_type,
            False,
            "failed",
            http_status=http_status,
            error_code=error_code or "revoke_rejected",
            error_message=error_message,
        )

    verified, verification_status, verification_code, verification_message = _verify_access_token_is_invalid(
        http_session,
        normalized_token,
        verification_delays=verification_delays,
    )
    if verified:
        return OAuthTokenRevocationResult(
            normalized_type,
            True,
            "revoked" if accepted else "already_invalid",
            http_status=http_status,
            error_code="" if accepted else error_code,
            verification_http_status=verification_status,
        )
    return OAuthTokenRevocationResult(
        normalized_type,
        False,
        "failed",
        http_status=http_status,
        error_code=verification_code,
        error_message=verification_message,
        verification_http_status=verification_status,
    )
