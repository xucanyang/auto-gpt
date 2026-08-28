"""ChatGPT OAuth and Web Session token refresh transports.

OAuth ``refresh_token`` remains the preferred transport when it is available.
Accounts created in AT-only mode, however, still retain a valid ChatGPT Web
Session.  ``refresh_by_web_session`` uses that saved session over curl-cffi and
never treats ``/api/auth/session`` alone as proof of success: the returned AT
must also pass ``/backend-api/me`` and belong to the saved account.
"""

from __future__ import annotations

import logging
import json
import re
import time
from typing import Optional, Dict, Any, Tuple
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urlsplit

from curl_cffi import requests as cffi_requests

from .sentinel_constants import (
    PINNED_CHROMIUM_USER_AGENT,
    PINNED_CURL_IMPERSONATE,
)

# from ..config.settings import get_settings  # removed: external dep
# from ..database.session import get_db  # removed: external dep
# from ..database import crud  # removed: external dep
# from ..database.models import Account  # removed: external dep

logger = logging.getLogger(__name__)


CHATGPT_HOME_URL = "https://chatgpt.com/"
BACKEND_ME_URL = "https://chatgpt.com/backend-api/me"
_SESSION_COOKIE_NAMES = (
    "__Secure-next-auth.session-token",
    "next-auth.session-token",
    "__Secure-authjs.session-token",
    "authjs.session-token",
)
_ALLOWED_COOKIE_DOMAINS = (
    "chatgpt.com",
    "openai.com",
    "oaistatic.com",
)
_COOKIE_CHUNK_RE = re.compile(
    r"^(?P<base>__Secure-next-auth\.session-token|next-auth\.session-token|"
    r"__Secure-authjs\.session-token|authjs\.session-token)\.(?P<index>\d+)$",
    re.IGNORECASE,
)


def _safe_error_text(value: Any, *, limit: int = 500) -> str:
    """Bound and redact upstream text before it reaches logs or API errors."""

    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    text = re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1[redacted]", text)
    text = re.sub(
        r"(?i)(access_token|refresh_token|session_token|accessToken|sessionToken)"
        r"\s*[=:]\s*[^\s,;&]+",
        r"\1=[redacted]",
        text,
    )
    text = re.sub(
        r"([a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@",
        r"\1***:***@",
        text,
        flags=re.IGNORECASE,
    )
    return text[:limit]


def _account_extra(account: Any) -> dict[str, Any]:
    try:
        value = account.get_extra() if callable(getattr(account, "get_extra", None)) else getattr(account, "extra", {})
    except Exception:
        value = {}
    return dict(value) if isinstance(value, dict) else {}


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _allowed_cookie_domain(value: Any) -> bool:
    host = str(value or "").strip().lower().lstrip(".")
    return bool(host) and any(host == root or host.endswith(f".{root}") for root in _ALLOWED_COOKIE_DOMAINS)


def _cookie_item_host_path(item: dict[str, Any]) -> tuple[str, str] | None:
    domain = str(item.get("domain") or "").strip()
    path = str(item.get("path") or "").strip() or "/"
    if domain:
        host = domain.lstrip(".").lower()
        return (host, path) if _allowed_cookie_domain(host) else None
    raw_url = str(item.get("url") or "").strip()
    if not raw_url:
        return None
    try:
        parsed = urlsplit(raw_url)
    except Exception:
        return None
    host = str(parsed.hostname or "").lower()
    if parsed.scheme != "https" or not _allowed_cookie_domain(host):
        return None
    return host, parsed.path or "/"


def _session_token_from_cookie_items(items: list[dict[str, Any]]) -> str:
    values = {str(item.get("name") or ""): str(item.get("value") or "") for item in items}
    for name in _SESSION_COOKIE_NAMES:
        value = values.get(name, "")
        if value:
            return value
        chunks: list[tuple[int, str]] = []
        for cookie_name, cookie_value in values.items():
            match = _COOKIE_CHUNK_RE.match(cookie_name)
            if match and match.group("base").lower() == name.lower():
                try:
                    index = int(match.group("index"))
                except (TypeError, ValueError):
                    continue
                chunks.append((index, cookie_value))
        if chunks:
            return "".join(value for _, value in sorted(chunks))
    return ""


def _cookie_header_from_items(items: list[dict[str, Any]]) -> str:
    """Serialize a stable, de-duplicated legacy header from captured cookies."""

    # Prefer ChatGPT-domain values when the same name exists on several OAI
    # hosts.  This mirrors what a browser sends to chatgpt.com and avoids
    # leaking an unrelated host's duplicate cookie into the persisted header.
    ranked: list[tuple[int, int, str, str]] = []
    for index, item in enumerate(items):
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        scope = str(item.get("domain") or "").lstrip(".").lower()
        rank = 0 if scope == "chatgpt.com" or scope.endswith(".chatgpt.com") else 1
        ranked.append((rank, index, name, str(item.get("value") or "")))
    ranked.sort(key=lambda value: (value[0], value[1]))
    seen: set[str] = set()
    pairs: list[str] = []
    for _rank, _index, name, value in ranked:
        if name in seen:
            continue
        seen.add(name)
        pairs.append(f"{name}={value}")
    return "; ".join(pairs)


def _cookie_items_from_session(session: Any) -> list[dict[str, Any]]:
    jar = getattr(getattr(session, "cookies", None), "jar", None)
    cookies: list[Any] = []
    if jar is not None:
        try:
            cookies = list(jar)
        except Exception:
            cookies = []
    if not cookies:
        try:
            cookies = list(getattr(session, "cookies", None) or [])
        except Exception:
            cookies = []

    items: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for cookie in cookies:
        name = str(getattr(cookie, "name", "") or "").strip()
        if not name:
            continue
        domain = str(getattr(cookie, "domain", "") or "").strip()
        if domain and not _allowed_cookie_domain(domain):
            continue
        key = (name, domain, str(getattr(cookie, "path", "") or "/"))
        if key in seen:
            continue
        seen.add(key)
        item: dict[str, Any] = {
            "name": name,
            "value": str(getattr(cookie, "value", "") or ""),
        }
        if domain:
            item["domain"] = domain
        else:
            # curl-cffi may expose a host-only cookie without a domain after a
            # response.  Keep it explicitly scoped for the next ChatGPT call.
            item["domain"] = "chatgpt.com"
        path = str(getattr(cookie, "path", "") or "").strip()
        if path:
            item["path"] = path
        if bool(getattr(cookie, "secure", False)):
            item["secure"] = True
        rest = getattr(cookie, "_rest", {}) or {}
        if isinstance(rest, dict):
            if rest.get("HttpOnly") or rest.get("httponly"):
                item["httpOnly"] = True
            same_site = rest.get("SameSite") or rest.get("samesite")
            if same_site:
                item["sameSite"] = str(same_site)
        expires = getattr(cookie, "expires", None)
        if expires not in (None, "", -1):
            try:
                item["expires"] = float(expires)
            except (TypeError, ValueError):
                pass
        items.append(item)
    return items


def _parse_datetime(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        number = float(value)
        if number > 100_000_000_000:
            number /= 1000.0
        try:
            parsed = datetime.fromtimestamp(number, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    else:
        text = str(value).strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _response_error(response: Any) -> tuple[str, str]:
    code = ""
    message = ""
    try:
        payload = response.json()
    except Exception:
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            code = _first_text(error.get("code"), error.get("type"))
            message = _first_text(error.get("message"), error.get("detail"))
        elif error not in (None, ""):
            message = str(error)
        code = _first_text(code, payload.get("error_code"), payload.get("code"), payload.get("type"))
        message = _first_text(message, payload.get("message"), payload.get("detail"))
    if not message:
        message = _safe_error_text(getattr(response, "text", ""))
    return code[:160], message[:500]


def _token_timing(access_token: str) -> tuple[Optional[datetime], str, str]:
    try:
        from .auth_lifecycle import token_timing

        timing = token_timing(access_token)
    except Exception:
        timing = {}
    expires_at = _parse_datetime(timing.get("expires_at"))
    issued_at = _parse_datetime(timing.get("issued_at"))
    return expires_at, str(timing.get("expiry_source") or ""), str(timing.get("expiry_confidence") or "")


def _account_id_from_value(value: Any) -> str:
    """Extract a stable account identifier from plain or JSON cookie values."""

    text = str(value or "").strip()
    if not text:
        return ""
    parsed = None
    for candidate in (text, unquote(text)):
        try:
            parsed = json.loads(candidate)
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            break
    if isinstance(parsed, dict):
        nested = _first_text(parsed.get("id"), parsed.get("account_id"), parsed.get("accountId"))
        if nested:
            return nested
    for candidate in (text, unquote(text)):
        uuid_match = re.search(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            candidate,
            flags=re.IGNORECASE,
        )
        if uuid_match:
            return uuid_match.group(0)
    return text


@dataclass
class TokenRefreshResult:
    """Token 刷新结果"""
    success: bool
    access_token: str = ""
    refresh_token: str = ""
    expires_at: Optional[datetime] = None
    error_message: str = ""
    http_status: int = 0
    error_code: str = ""
    expires_in: int = 0
    expiry_source: str = ""
    # The following fields are intentionally non-secret metadata or captured
    # Web Session material.  Callers decide which secret fields to persist.
    source: str = ""
    rotated: bool = False
    validation_http_status: int = 0
    validation_error_code: str = ""
    validation_message: str = ""
    account_id: str = ""
    session_token: str = ""
    cookie_header: str = ""
    structured_cookies: Optional[list[dict[str, Any]]] = None
    web_session_expires_at: Optional[datetime] = None
    web_session_expiry_source: str = ""


class TokenRefreshManager:
    """
    Token 刷新管理器。

    现行业务口径：纯 RT。
    Session Token helper 保留但不参与主业务判定。
    """

    # OpenAI OAuth 端点
    SESSION_URL = "https://chatgpt.com/api/auth/session"
    TOKEN_URL = "https://auth.openai.com/oauth/token"

    def __init__(
        self,
        proxy_url: Optional[str] = None,
        *,
        browser_fingerprint: Optional[Dict[str, Any]] = None,
    ):
        """
        初始化 Token 刷新管理器

        Args:
            proxy_url: 代理 URL
        """
        self.proxy_url = proxy_url
        # Status refreshes must not silently manufacture a new browser
        # identity.  Only an already persisted account fingerprint is used.
        self.browser_fingerprint = (
            dict(browser_fingerprint)
            if isinstance(browser_fingerprint, dict) and browser_fingerprint
            else {}
        )
        from .constants import OAUTH_CLIENT_ID, OAUTH_REDIRECT_URI
        self._oauth_client_id = OAUTH_CLIENT_ID
        self._oauth_redirect_uri = OAUTH_REDIRECT_URI

    def _browser_fingerprint_object(self):
        """Build a curl-cffi-compatible identity without filling missing data."""
        payload = self.browser_fingerprint
        if not payload:
            return None

        device_id = str(payload.get("device_id") or "").strip()
        user_agent = str(payload.get("user_agent") or "").strip()
        impersonate = str(payload.get("impersonate") or "").strip()
        if not (device_id and user_agent and impersonate):
            return None

        try:
            chrome_major = int(payload.get("chrome_major") or 0)
        except (TypeError, ValueError):
            chrome_major = 0
        chrome_full_version = str(payload.get("chrome_full_version") or "").strip()
        if not chrome_major and chrome_full_version:
            try:
                chrome_major = int(chrome_full_version.split(".", 1)[0])
            except (TypeError, ValueError):
                chrome_major = 0

        try:
            from .utils import coerce_browser_fingerprint

            return coerce_browser_fingerprint(payload)
        except Exception:
            return None

    def _create_session(self) -> cffi_requests.Session:
        """创建 HTTP 会话"""
        fingerprint = self._browser_fingerprint_object()
        impersonate = (
            fingerprint.impersonate if fingerprint else PINNED_CURL_IMPERSONATE
        )
        try:
            session = cffi_requests.Session(impersonate=impersonate, proxy=self.proxy_url)
        except Exception:
            # A malformed legacy value must not make a token refresh unusable.
            session = cffi_requests.Session(
                impersonate=PINNED_CURL_IMPERSONATE,
                proxy=self.proxy_url,
            )
            fingerprint = None
        if fingerprint is not None:
            try:
                from .utils import apply_browser_fingerprint

                apply_browser_fingerprint(session, fingerprint)
            except Exception:
                pass
        return session

    def _ensure_account_fingerprint(self, account: Any) -> None:
        """Load the persisted account identity when a caller did not provide it."""

        if self.browser_fingerprint:
            return
        try:
            from .account_fingerprint import resolve_account_browser_fingerprint

            resolved = resolve_account_browser_fingerprint(_account_extra(account))
            if isinstance(resolved, dict) and resolved:
                self.browser_fingerprint = dict(resolved)
        except Exception:
            # Legacy rows may not have a fingerprint.  The curl impersonation
            # fallback remains usable in that case.
            return

    def _web_headers(self, url: str, *, navigation: bool = False, referer: str = CHATGPT_HOME_URL) -> dict[str, str]:
        """Build browser-shaped headers from the account's saved fingerprint."""

        fingerprint = self._browser_fingerprint_object()
        user_agent = str(
            getattr(fingerprint, "user_agent", "")
            or PINNED_CHROMIUM_USER_AGENT
        )
        try:
            from .utils import build_browser_headers

            return build_browser_headers(
                url=url,
                user_agent=user_agent,
                sec_ch_ua=getattr(fingerprint, "sec_ch_ua", "") if fingerprint else None,
                chrome_full_version=getattr(fingerprint, "chrome_full_version", "") if fingerprint else None,
                sec_ch_platform_version=getattr(fingerprint, "platform_version", "") if fingerprint else None,
                accept=(
                    "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
                    if navigation
                    else "application/json, text/plain, */*"
                ),
                accept_language=str(getattr(fingerprint, "accept_language", "") or "en-US,en;q=0.9"),
                referer=referer,
                navigation=navigation,
                fetch_mode="navigate" if navigation else "cors",
                fetch_dest="document" if navigation else "empty",
                fetch_site="same-origin",
                browser_family=getattr(fingerprint, "browser_family", "") if fingerprint else None,
            )
        except Exception:
            return {
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
                if navigation
                else "application/json, text/plain, */*",
                "Accept-Language": str(getattr(fingerprint, "accept_language", "") or "en-US,en;q=0.9"),
                "Referer": referer,
            }

    def _inject_web_session_cookies(self, session: Any, account: Any) -> tuple[list[dict[str, Any]], bool, str]:
        """Inject saved cookies with their original host/path scope."""

        extra = _account_extra(account)
        try:
            from .browser_cookies import browser_cookie_items, cookie_header_from_items

            items, is_structured = browser_cookie_items(account, extra)
        except Exception:
            items, is_structured = [], False

        session_token = _first_text(
            extra.get("session_token"),
            extra.get("sessionToken"),
            extra.get("nextauth_session_token"),
            getattr(account, "session_token", ""),
        )
        if not items and session_token:
            # A few early imports persisted only session_token.  Keep this
            # compatibility path host-scoped to chatgpt.com.
            items = [
                {
                    "name": "__Secure-next-auth.session-token",
                    "value": session_token,
                    "domain": "chatgpt.com",
                    "path": "/",
                    "secure": True,
                }
            ]

        injected: list[dict[str, Any]] = []
        for raw in items:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name") or "").strip()
            if not name:
                continue
            scope = _cookie_item_host_path(raw)
            if scope is None:
                continue
            host, path = scope
            try:
                session.cookies.set(
                    name,
                    str(raw.get("value") or ""),
                    domain=str(raw.get("domain") or host),
                    path=str(raw.get("path") or path),
                )
                injected.append(dict(raw))
            except Exception:
                # curl-cffi versions differ slightly in CookieJar argument
                # handling.  A URL-scoped fallback is safe for these hosts.
                try:
                    session.cookies.set(
                        name,
                        str(raw.get("value") or ""),
                        domain=host,
                        path=path,
                    )
                    injected.append(dict(raw))
                except Exception:
                    continue

        raw_header = _first_text(
            extra.get("cookie_header"),
            extra.get("cookies"),
            getattr(account, "cookies", ""),
        )
        if not injected and raw_header and not (is_structured and items):
            # Last-resort compatibility for malformed legacy headers.  This is
            # still sent only to the fixed ChatGPT origin below.
            try:
                session.headers["Cookie"] = raw_header
            except Exception:
                pass
        elif injected:
            # Do not pin a stale Cookie header when the jar is populated: the
            # server may rotate Set-Cookie values during the refresh sequence.
            try:
                session.headers.pop("Cookie", None)
            except Exception:
                pass
        try:
            serialized = cookie_header_from_items(injected) if injected else raw_header
        except Exception:
            serialized = raw_header
        return injected, bool(is_structured), str(serialized or "").strip()

    def refresh_by_web_session(self, account: Any) -> TokenRefreshResult:
        """Refresh an AT with the account's saved ChatGPT Web Session.

        The Web Session endpoint can return HTTP 200 with the *same* expired or
        revoked AT.  Consequently this method only reports success after a
        second request to ``/backend-api/me`` succeeds.  All captured cookie
        material is returned to the caller for an atomic account writeback.
        """

        result = TokenRefreshResult(success=False, source="web_session")
        extra = _account_extra(account)
        self._ensure_account_fingerprint(account)

        try:
            from .browser_cookies import browser_cookie_items

            saved_items, saved_structured = browser_cookie_items(account, extra)
        except Exception:
            saved_items, saved_structured = [], False
        saved_session_token = _first_text(
            extra.get("session_token"),
            extra.get("sessionToken"),
            extra.get("nextauth_session_token"),
            getattr(account, "session_token", ""),
        )
        if not saved_items and not saved_session_token:
            result.error_code = "missing_web_session"
            result.error_message = "账号缺少可用的 ChatGPT Web Session Cookie"
            return result

        old_access_token = _first_text(
            extra.get("access_token"),
            extra.get("accessToken"),
            extra.get("webAccessToken"),
            getattr(account, "access_token", ""),
            getattr(account, "token", ""),
        )
        expected_account_id = _first_text(
            extra.get("account_id"),
            extra.get("chatgpt_account_id"),
            extra.get("workspace_id"),
            getattr(account, "user_id", ""),
        )

        try:
            session = self._create_session()
            injected_items, _is_structured, fallback_header = self._inject_web_session_cookies(
                session,
                account,
            )
            if not injected_items and not fallback_header:
                result.error_code = "missing_web_session"
                result.error_message = "账号缺少可用的 ChatGPT Web Session Cookie"
                return result

            # A navigation request first lets ChatGPT refresh routing and
            # Cloudflare cookies before the JSON session endpoint is called.
            home_response = session.get(
                CHATGPT_HOME_URL,
                headers=self._web_headers(CHATGPT_HOME_URL, navigation=True),
                timeout=30,
            )
            home_status = int(getattr(home_response, "status_code", 0) or 0)
            if home_status < 200 or home_status >= 400:
                upstream_code, detail = _response_error(home_response)
                result.http_status = home_status
                result.error_code = upstream_code or "web_session_home_failed"
                result.error_message = f"ChatGPT 首页请求失败 HTTP {home_status or 'unknown'}"
                if detail:
                    result.error_message += f": {detail[:240]}"
                return result

            session_payload: dict[str, Any] = {}
            session_response = None
            for attempt in range(2):
                session_response = session.get(
                    self.SESSION_URL,
                    headers=self._web_headers(self.SESSION_URL, referer=CHATGPT_HOME_URL),
                    timeout=30,
                )
                status = int(getattr(session_response, "status_code", 0) or 0)
                if status == 200:
                    try:
                        payload = session_response.json()
                    except Exception as exc:
                        result.http_status = status
                        result.error_code = "web_session_invalid_json"
                        result.error_message = f"Web Session 响应 JSON 无效: {_safe_error_text(exc)}"
                        return result
                    if isinstance(payload, dict):
                        session_payload = payload
                    break
                if attempt == 0 and (status in {0, 408, 425, 429} or status >= 500):
                    time.sleep(0.4)
                    continue
                break

            session_status = int(getattr(session_response, "status_code", 0) or 0) if session_response is not None else 0
            result.http_status = session_status
            if session_status != 200:
                upstream_code, detail = _response_error(session_response)
                result.error_code = upstream_code or "web_session_request_failed"
                result.error_message = f"Web Session 请求失败 HTTP {session_status or 'unknown'}"
                if detail:
                    result.error_message += f": {detail[:240]}"
                return result

            access_token = _first_text(
                session_payload.get("accessToken"),
                session_payload.get("access_token"),
            )
            if not access_token:
                result.error_code = "web_session_access_token_missing"
                result.error_message = "Web Session 响应未返回 accessToken"
                return result

            # Resolve the account identity from the explicit payload first,
            # then from the AT claims and finally the _account cookie.
            account_payload = session_payload.get("account") if isinstance(session_payload.get("account"), dict) else {}
            user_payload = session_payload.get("user") if isinstance(session_payload.get("user"), dict) else {}
            try:
                from .auth_lifecycle import decode_jwt_payload

                claims = decode_jwt_payload(access_token)
            except Exception:
                claims = {}
            auth_claims = claims.get("https://api.openai.com/auth") if isinstance(claims, dict) else {}
            auth_claims = auth_claims if isinstance(auth_claims, dict) else {}
            payload_user_id = _first_text(user_payload.get("account_id"), user_payload.get("id"))
            captured_account_id = _first_text(
                account_payload.get("id"),
                account_payload.get("account_id"),
                session_payload.get("account_id"),
                session_payload.get("accountId"),
                auth_claims.get("chatgpt_account_id"),
                claims.get("chatgpt_account_id") if isinstance(claims, dict) else "",
                user_payload.get("account_id"),
                payload_user_id if not expected_account_id or payload_user_id == expected_account_id else "",
            )
            captured_account_id = _account_id_from_value(captured_account_id)
            cookie_items_after = _cookie_items_from_session(session)
            if not captured_account_id:
                captured_account_id = _account_id_from_value(
                    next(
                        (
                            item.get("value")
                            for item in cookie_items_after
                            if str(item.get("name") or "") == "_account"
                        ),
                        "",
                    )
                )
            result.account_id = captured_account_id
            if not captured_account_id:
                result.error_code = "account_identity_missing"
                result.error_message = "Web Session 响应未返回可验证的账号 ID"
                return result
            if expected_account_id and captured_account_id != expected_account_id:
                result.error_code = "account_identity_mismatch"
                result.error_message = "Web Session 返回账号与本地账号不一致"
                return result

            validation_response = session.get(
                BACKEND_ME_URL,
                headers=self._web_headers(BACKEND_ME_URL, referer=CHATGPT_HOME_URL)
                | {
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                },
                timeout=30,
            )
            validation_status = int(getattr(validation_response, "status_code", 0) or 0)
            result.validation_http_status = validation_status
            if validation_status != 200:
                validation_code, validation_detail = _response_error(validation_response)
                if validation_status == 401 and not validation_code:
                    old_expiry, _source, _confidence = _token_timing(old_access_token)
                    validation_code = "token_expired" if old_expiry and old_expiry <= datetime.now(timezone.utc) else "token_invalidated"
                result.validation_error_code = validation_code or "web_session_access_denied"
                result.validation_message = validation_detail or f"/backend-api/me HTTP {validation_status or 'unknown'}"
                result.error_code = result.validation_error_code
                result.error_message = f"Web Session 返回 AT 未通过业务校验 HTTP {validation_status or 'unknown'}"
                return result

            # If /me includes an account/workspace identity, enforce it too;
            # this catches a stale or cross-account Session payload before any
            # local credentials are replaced.
            try:
                validation_payload = validation_response.json()
            except Exception:
                validation_payload = {}
            if isinstance(validation_payload, dict):
                validation_account = validation_payload.get("account")
                validation_account = validation_account if isinstance(validation_account, dict) else {}
                validation_id = _account_id_from_value(
                    _first_text(
                        validation_account.get("id"),
                        validation_account.get("account_id"),
                        validation_payload.get("account_id"),
                        validation_payload.get("accountId"),
                        validation_payload.get("workspace_id"),
                    )
                )
                if validation_id and captured_account_id and validation_id != captured_account_id:
                    result.error_code = "account_identity_mismatch"
                    result.validation_error_code = "account_identity_mismatch"
                    result.validation_message = "/backend-api/me 返回账号与 Web Session 不一致"
                    result.error_message = "Web Session 业务校验账号身份不一致"
                    return result

            if not cookie_items_after:
                cookie_items_after = [dict(item) for item in injected_items]
            captured_cookie_header = _cookie_header_from_items(cookie_items_after) or fallback_header
            captured_session_token = _first_text(
                session_payload.get("sessionToken"),
                session_payload.get("session_token"),
                _session_token_from_cookie_items(cookie_items_after),
                saved_session_token,
            )
            if captured_session_token and not any(
                str(item.get("name") or "") in _SESSION_COOKIE_NAMES
                or _COOKIE_CHUNK_RE.match(str(item.get("name") or ""))
                for item in cookie_items_after
            ):
                cookie_items_after.append(
                    {
                        "name": "__Secure-next-auth.session-token",
                        "value": captured_session_token,
                        "domain": ".chatgpt.com",
                        "path": "/",
                        "secure": True,
                    }
                )
                captured_cookie_header = _cookie_header_from_items(cookie_items_after) or captured_cookie_header
            if not captured_cookie_header or not captured_session_token:
                result.error_code = "web_session_material_incomplete"
                result.error_message = "Web Session 校验成功但 Cookie/Session 材料不完整"
                return result

            expires_at, expiry_source, _confidence = _token_timing(access_token)
            web_session_expires_at = _parse_datetime(
                session_payload.get("expires")
                or session_payload.get("sessionExpiresAt")
                or session_payload.get("web_session_expires_at")
            )
            result.success = True
            result.access_token = access_token
            result.session_token = captured_session_token
            result.cookie_header = captured_cookie_header
            result.structured_cookies = cookie_items_after or None
            result.account_id = captured_account_id
            result.expires_at = expires_at
            result.expiry_source = expiry_source or ("jwt_exp" if expires_at else "")
            result.expires_in = max(0, int((expires_at - datetime.now(timezone.utc)).total_seconds())) if expires_at else 0
            result.web_session_expires_at = web_session_expires_at
            result.web_session_expiry_source = "web_session_expires" if web_session_expires_at else ""
            result.rotated = bool(old_access_token and old_access_token != access_token)
            result.error_message = ""
            return result
        except Exception as exc:
            result.error_code = "web_session_transport_error"
            result.error_message = f"Web Session 协议刷新异常: {_safe_error_text(exc)}"
            logger.warning("ChatGPT Web Session refresh failed code=%s error=%s", result.error_code, result.error_message)
            return result

    # Backwards-compatible spelling for callers that refer to cookies rather
    # than the underlying NextAuth Web Session.
    refresh_by_cookie = refresh_by_web_session

    def refresh_by_session_token(self, session_token: str) -> TokenRefreshResult:
        """
        使用 Session Token 刷新（仅历史兼容 helper）。

        注意：纯 RT 主链路不会调用此方法。
        """
        result = TokenRefreshResult(success=False)

        try:
            session = self._create_session()

            # 设置会话 Cookie
            session.cookies.set(
                "__Secure-next-auth.session-token",
                session_token,
                domain=".chatgpt.com",
                path="/"
            )

            # 请求会话端点
            response = session.get(
                self.SESSION_URL,
                headers={
                    "accept": "application/json",
                    "user-agent": PINNED_CHROMIUM_USER_AGENT,
                },
                timeout=30
            )

            if response.status_code != 200:
                result.error_message = f"Session token 刷新失败: HTTP {response.status_code}"
                logger.warning(result.error_message)
                return result

            data = response.json()

            # 提取 access_token
            access_token = data.get("accessToken")
            if not access_token:
                result.error_message = "Session token 刷新失败: 未找到 accessToken"
                logger.warning(result.error_message)
                return result

            # 提取过期时间
            expires_at = None
            expires_str = data.get("expires")
            if expires_str:
                try:
                    expires_at = datetime.fromisoformat(expires_str.replace("Z", "+00:00"))
                except:
                    pass

            result.success = True
            result.access_token = access_token
            result.expires_at = expires_at

            logger.info(f"Session token 刷新成功，过期时间: {expires_at}")
            return result

        except Exception as e:
            result.error_message = f"Session token 刷新异常: {str(e)}"
            logger.error(result.error_message)
            return result

    def refresh_by_oauth_token(
        self,
        refresh_token: str,
        client_id: Optional[str] = None
    ) -> TokenRefreshResult:
        """
        使用 OAuth Refresh Token 刷新

        Args:
            refresh_token: OAuth 刷新令牌
            client_id: OAuth Client ID

        Returns:
            TokenRefreshResult: 刷新结果
        """
        result = TokenRefreshResult(success=False, source="oauth")

        try:
            session = self._create_session()

            # 使用配置的 client_id 或默认值
            client_id = client_id or self._oauth_client_id

            # 构建请求体
            token_data = {
                "client_id": client_id,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "redirect_uri": self._oauth_redirect_uri
            }

            response = session.post(
                self.TOKEN_URL,
                headers={
                    "content-type": "application/x-www-form-urlencoded",
                    "accept": "application/json"
                },
                data=token_data,
                timeout=30
            )

            if response.status_code != 200:
                result.http_status = int(response.status_code or 0)
                try:
                    error_data = response.json()
                except Exception:
                    error_data = {}
                if isinstance(error_data, dict):
                    raw_error = error_data.get("error")
                    error_obj = raw_error if isinstance(raw_error, dict) else {}
                    for candidate in (
                        error_obj.get("code"),
                        raw_error if isinstance(raw_error, str) else "",
                        error_data.get("error_code"),
                        error_data.get("code"),
                    ):
                        if str(candidate or "").strip():
                            result.error_code = str(candidate).strip()
                            break
                result.error_message = f"OAuth token 刷新失败: HTTP {response.status_code}"
                logger.warning(f"{result.error_message}, 响应: {response.text[:200]}")
                return result

            data = response.json()
            result.http_status = int(response.status_code or 0)

            # 提取令牌
            access_token = data.get("access_token")
            new_refresh_token = data.get("refresh_token", refresh_token)
            expires_in = data.get("expires_in", 0)
            try:
                expires_in = max(0, int(float(expires_in)))
            except (TypeError, ValueError):
                expires_in = 0

            if not access_token:
                raw_error = data.get("error")
                error_obj = raw_error if isinstance(raw_error, dict) else {}
                for candidate in (
                    error_obj.get("code"),
                    raw_error if isinstance(raw_error, str) else "",
                    data.get("error_code"),
                    data.get("code"),
                ):
                    if str(candidate or "").strip():
                        result.error_code = str(candidate).strip()
                        break
                result.error_message = "OAuth token 刷新失败: 未找到 access_token"
                logger.warning(result.error_message)
                return result

            # 计算过期时间
            expires_at = (
                datetime.now(timezone.utc) + timedelta(seconds=expires_in)
                if expires_in > 0
                else None
            )

            result.success = True
            result.access_token = access_token
            result.refresh_token = new_refresh_token
            result.expires_at = expires_at
            result.expires_in = expires_in
            result.expiry_source = "oauth_expires_in" if expires_in > 0 else ""

            logger.info(f"OAuth token 刷新成功，过期时间: {expires_at}")
            return result

        except Exception as e:
            result.error_message = f"OAuth token 刷新异常: {str(e)}"
            logger.error(result.error_message)
            return result

    def refresh_account(self, account: Any, *, mode: str = "auto") -> TokenRefreshResult:
        """Refresh one account using OAuth RT or its saved Web Session.

        ``auto`` preserves the historical behavior for RT accounts and adds a
        Cookie fallback only when no RT is present.  ``web_session``/``protocol``
        explicitly force the Cookie transport, while ``oauth`` forces RT.
        """

        self._ensure_account_fingerprint(account)
        extra = _account_extra(account)
        refresh_token = _first_text(
            getattr(account, "refresh_token", ""),
            extra.get("refresh_token"),
            extra.get("refreshToken"),
        )
        normalized_mode = str(mode or "auto").strip().lower().replace("-", "_")
        if normalized_mode in {"cookie", "cookies", "web", "web_session", "browser_session", "protocol"}:
            return self.refresh_by_web_session(account)
        if normalized_mode in {"oauth", "refresh", "refresh_token", "rt"}:
            if not refresh_token:
                return TokenRefreshResult(
                    success=False,
                    source="oauth",
                    error_code="missing_refresh_token",
                    error_message="账号没有可用的 refresh_token",
                )
            client_id = _first_text(
                getattr(account, "client_id", ""),
                extra.get("client_id"),
                extra.get("clientId"),
            )
            result = self.refresh_by_oauth_token(
                refresh_token=refresh_token,
                client_id=client_id or None,
            )
            result.source = "oauth"
            return result

        if refresh_token:
            logger.info("尝试使用 OAuth Refresh Token 刷新 ChatGPT 账号")
            client_id = _first_text(
                getattr(account, "client_id", ""),
                extra.get("client_id"),
                extra.get("clientId"),
            )
            result = self.refresh_by_oauth_token(
                refresh_token=refresh_token,
                client_id=client_id or None,
            )
            result.source = "oauth"
            return result

        has_web_material = bool(
            _first_text(
                extra.get("session_token"),
                extra.get("sessionToken"),
                extra.get("nextauth_session_token"),
                extra.get("cookies"),
                extra.get("cookie_header"),
                getattr(account, "session_token", ""),
                getattr(account, "cookies", ""),
            )
            or extra.get("chatgpt_browser_cookies")
        )
        if has_web_material:
            return self.refresh_by_web_session(account)
        return TokenRefreshResult(
            success=False,
            source="",
            error_code="missing_refresh_material",
            error_message="账号没有可用的刷新方式（缺少 refresh_token 和 Web Session Cookie）",
        )

    def validate_token(self, access_token: str) -> Tuple[bool, Optional[str]]:
        """
        验证 Access Token 是否有效

        Args:
            access_token: 访问令牌

        Returns:
            Tuple[bool, Optional[str]]: (是否有效, 错误信息)
        """
        try:
            session = self._create_session()

            # 调用 OpenAI API 验证 token
            response = session.get(
                "https://chatgpt.com/backend-api/me",
                headers={
                    "authorization": f"Bearer {access_token}",
                    "accept": "application/json"
                },
                timeout=30
            )

            if response.status_code == 200:
                return True, None
            elif response.status_code == 401:
                return False, "Token 无效或已过期"
            elif response.status_code == 403:
                return False, "账号可能被封禁"
            else:
                return False, f"验证失败: HTTP {response.status_code}"

        except Exception as e:
            return False, f"验证异常: {str(e)}"


def build_token_refresh_extra_patch(result: TokenRefreshResult) -> dict[str, Any]:
    """Build the secret/material portion of a successful refresh writeback."""

    patch: dict[str, Any] = {}
    if str(result.access_token or "").strip():
        patch["access_token"] = str(result.access_token).strip()
    if str(result.refresh_token or "").strip():
        patch["refresh_token"] = str(result.refresh_token).strip()
    if str(result.source or "").strip().lower() == "web_session":
        if str(result.session_token or "").strip():
            patch["session_token"] = str(result.session_token).strip()
        if str(result.cookie_header or "").strip():
            patch["cookies"] = str(result.cookie_header).strip()
            patch["cookie_header"] = str(result.cookie_header).strip()
        if result.structured_cookies:
            patch["chatgpt_browser_cookies"] = result.structured_cookies
        if str(result.account_id or "").strip():
            patch["account_id"] = str(result.account_id).strip()
            patch.setdefault("workspace_id", str(result.account_id).strip())
        patch["chatgpt_token_source"] = "web_session_refresh"
        if result.web_session_expires_at:
            patch["web_session_expires_at"] = result.web_session_expires_at.isoformat().replace("+00:00", "Z")
            patch["web_session_expiry_source"] = result.web_session_expiry_source or "web_session_expires"
        patch["chatgpt_web_session_refresh"] = {
            "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "source": "web_session",
            "rotated": bool(result.rotated),
            "validation_http_status": int(result.validation_http_status or 0),
            "account_id": str(result.account_id or ""),
            "access_token_expires_at": result.expires_at.isoformat().replace("+00:00", "Z")
            if result.expires_at
            else "",
        }
    elif str(result.source or "").strip().lower() == "oauth":
        patch["chatgpt_token_source"] = "oauth_refresh"
    return patch


def refresh_account_token(account_id: int, proxy_url: Optional[str] = None) -> TokenRefreshResult:
    """
    刷新指定账号的 Token 并更新数据库

    Args:
        account_id: 账号 ID
        proxy_url: 代理 URL

    Returns:
        TokenRefreshResult: 刷新结果
    """
    with get_db() as db:
        account = crud.get_account_by_id(db, account_id)
        if not account:
            return TokenRefreshResult(success=False, error_message="账号不存在")

        manager = TokenRefreshManager(proxy_url=proxy_url)
        result = manager.refresh_account(account)

        if result.success:
            # 更新数据库
            update_data = {
                "access_token": result.access_token,
                "last_refresh": datetime.utcnow()
            }

            if result.refresh_token:
                update_data["refresh_token"] = result.refresh_token

            if result.expires_at:
                update_data["expires_at"] = result.expires_at

            crud.update_account(db, account_id, **update_data)

        return result


def validate_account_token(account_id: int, proxy_url: Optional[str] = None) -> Tuple[bool, Optional[str]]:
    """
    验证指定账号的 Token 是否有效

    Args:
        account_id: 账号 ID
        proxy_url: 代理 URL

    Returns:
        Tuple[bool, Optional[str]]: (是否有效, 错误信息)
    """
    with get_db() as db:
        account = crud.get_account_by_id(db, account_id)
        if not account:
            return False, "账号不存在"

        if not account.access_token:
            return False, "账号没有 access_token"

        manager = TokenRefreshManager(proxy_url=proxy_url)
        return manager.validate_token(account.access_token)
