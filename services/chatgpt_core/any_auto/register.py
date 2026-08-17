"""
注册流程引擎
从 main.py 中提取并重构的注册流程
"""

import re
import json
import time
import uuid
import base64
import random
import logging
import secrets
import string
from typing import Optional, Dict, Any, Tuple, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit

from curl_cffi import requests as cffi_requests

from core.task_runtime import TaskInterruption
from core.timezone import beijing_log_time
from .oauth import OAuthManager, OAuthStart, generate_oauth_url, submit_callback_url
from .http_client import OpenAIHTTPClient, HTTPClientError
from ..browser_identity import browser_fingerprint_to_dict
from ..utils import build_browser_headers, coerce_browser_fingerprint, decode_jwt_payload
from ..task_logging import format_http_trace_log, mask_email_for_log
# from ..services import EmailServiceFactory, BaseEmailService, EmailServiceType  # removed: external dep
# from ..database import crud  # removed: external dep
# from ..database.session import get_db  # removed: external dep
from .constants import (
    OPENAI_API_ENDPOINTS,
    OPENAI_PAGE_TYPES,
    generate_random_user_info,
    OTP_CODE_PATTERN,
    DEFAULT_PASSWORD_LENGTH,
    PASSWORD_CHARSET,
    AccountStatus,
    TaskStatus,
    SENTINEL_SDK_URL,
    OAUTH_REDIRECT_URI,
    OAUTH_CLIENT_ID,
)
# from ..config.settings import get_settings  # removed: external dep


logger = logging.getLogger(__name__)

OTP_SENT_AT_CLOCK_SKEW_GRACE_SECONDS = 5
PROTOCOL_SESSION_POLL_ATTEMPTS = 3
_SESSION_COOKIE_NAMES = (
    "__Secure-next-auth.session-token",
    "next-auth.session-token",
    "__Secure-authjs.session-token",
    "authjs.session-token",
)

_PASSWORD_PAGE_TYPES = frozenset({"password", "create_account_password"})
_OTP_SEND_PAGE_TYPES = frozenset({"email_otp_send"})
_OTP_VERIFY_PAGE_TYPES = frozenset(
    {"email_otp_verification", "email_otp_validate"}
)
_ABOUT_YOU_PAGE_TYPES = frozenset({"about_you"})
_EXTERNAL_PAGE_TYPES = frozenset({"external_url", "callback", "oauth_callback"})
_EXISTING_ACCOUNT_PAGE_TYPES = frozenset({"login", "login_password"})
_LOGIN_ROUTE_MARKERS = frozenset({"login", "log_in", "passwordless_login", "email_login"})
_SIGNUP_ROUTE_MARKERS = frozenset(
    {"signup", "sign_up", "passwordless_signup", "email_signup", "register"}
)


def _otp_request_started_at() -> float:
    return time.time() - OTP_SENT_AT_CLOCK_SKEW_GRACE_SECONDS


class _SkipCodexOAuth(RuntimeError):
    """Internal control flow for the GPT-only registration contract."""


def _cookie_header_from_session(session: Any) -> str:
    """Serialize a curl_cffi cookie jar without losing its name/value pairs."""
    jar = getattr(session, "cookies", None)
    if jar is None:
        return ""

    pairs: list[tuple[Any, Any]] = []
    try:
        pairs = list(jar.items())
    except Exception:
        try:
            values = jar.get_dict()
            if isinstance(values, dict):
                pairs = list(values.items())
        except Exception:
            pairs = []

    if not pairs:
        # Compatibility with stdlib CookieJar implementations.
        try:
            for cookie in jar:
                name = getattr(cookie, "name", "")
                value = getattr(cookie, "value", "")
                if name:
                    pairs.append((name, value))
        except Exception:
            pairs = []

    return "; ".join(
        f"{str(name).strip()}={str(value)}"
        for name, value in pairs
        if str(name or "").strip()
    )


def _session_token_from_session(session: Any) -> str:
    """Read the active NextAuth/Auth.js cookie across upstream naming variants."""

    for name in _SESSION_COOKIE_NAMES:
        value = _cookie_value_from_session(session, name)
        if value:
            return value
    return ""


@dataclass
class RegistrationResult:
    """注册结果"""
    success: bool
    email: str = ""
    password: str = ""  # 注册密码
    account_id: str = ""
    workspace_id: str = ""
    access_token: str = ""
    refresh_token: str = ""
    id_token: str = ""
    session_token: str = ""  # 会话令牌
    error_message: str = ""
    logs: list = None
    metadata: dict = None
    source: str = "register"  # 'register' 或 'login'，区分账号来源

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "success": self.success,
            "email": self.email,
            "password": self.password,
            "account_id": self.account_id,
            "workspace_id": self.workspace_id,
            "access_token": self.access_token[:20] + "..." if self.access_token else "",
            "refresh_token": self.refresh_token[:20] + "..." if self.refresh_token else "",
            "id_token": self.id_token[:20] + "..." if self.id_token else "",
            "session_token": self.session_token[:20] + "..." if self.session_token else "",
            "error_message": self.error_message,
            "logs": self.logs or [],
            "metadata": self.metadata or {},
            "source": self.source,
        }


@dataclass
class SignupFormResult:
    """提交注册表单的结果"""
    success: bool
    page_type: str = ""  # 响应中的 page.type 字段
    is_existing_account: bool = False  # 是否为已注册账号
    response_data: Dict[str, Any] = None  # 完整的响应数据
    error_message: str = ""


@dataclass
class SentinelPayload:
    """Sentinel 请求结果。"""
    p: str
    c: str
    flow: str
    t: str = ""


@dataclass(frozen=True)
class ProtocolFlowFailure:
    """Stable failure contract for the curl-only registration transport."""

    code: str
    stage: str
    message: str
    http_status: int = 0
    upstream_code: str = ""
    retriable: bool = False

    def render(self) -> str:
        fields = [
            f"code={self.code or 'protocol_failure'}",
            f"stage={self.stage or 'unknown'}",
            f"retriable={'true' if self.retriable else 'false'}",
        ]
        if self.http_status:
            fields.append(f"http={self.http_status}")
        if self.upstream_code:
            fields.append(f"upstream={self.upstream_code}")
        detail = re.sub(r"\s+", " ", str(self.message or "")).strip()[:500]
        return f"protocol_failure {' '.join(fields)}: {detail or self.code}"

    def to_metadata(self) -> dict[str, Any]:
        return {
            "protocol_failure_code": self.code,
            "protocol_failure_stage": self.stage,
            "protocol_http_status": self.http_status,
            "protocol_upstream_code": self.upstream_code,
            "protocol_retriable": self.retriable,
        }


def _response_json(response, label: str) -> dict:
    status = int(getattr(response, "status_code", 0) or 0)
    headers = getattr(response, "headers", {}) or {}
    content_type = str(headers.get("content-type") or headers.get("Content-Type") or "").lower()
    cf_mitigated = str(headers.get("cf-mitigated") or headers.get("Cf-Mitigated") or "").lower()
    body = str(getattr(response, "text", "") or "")
    body_lower = body.lower()
    cloudflare_challenge = (
        cf_mitigated == "challenge"
        or "cf-chl-" in body_lower
        or "challenge-platform" in body_lower
        or ("cloudflare" in body_lower and "just a moment" in body_lower)
    )

    if status != 200:
        if status == 403 and cloudflare_challenge:
            raise RuntimeError(f"{label}被 Cloudflare 拦截 HTTP 403")
        excerpt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)).strip()[:200]
        suffix = f": {excerpt}" if excerpt else ""
        raise RuntimeError(f"{label}失败 HTTP {status or 'unknown'}{suffix}")
    if "json" not in content_type:
        if cloudflare_challenge:
            raise RuntimeError(f"{label}被 Cloudflare 拦截 HTTP {status}")
        raise RuntimeError(f"{label}返回非 JSON 响应 ({content_type or 'unknown content-type'})")
    try:
        data = response.json()
    except Exception as exc:
        raise RuntimeError(f"{label}返回无效 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"{label}返回 JSON 结构异常")
    return data


def _page_type_from_payload(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    page = payload.get("page")
    if not isinstance(page, dict):
        return ""
    return str(page.get("type") or "").strip().lower()


def _account_route_from_payload(payload: Any) -> tuple[bool, str]:
    """Classify an OTP response without treating every OTP page as an account hit.

    OpenAI now uses ``email_otp_verification`` for both passwordless signup and
    passwordless login.  The page payload carries the route intent; when it is
    absent, the request was made with ``screen_hint=signup``, so the conservative
    default is to continue the signup flow rather than discard a fresh mailbox.
    """

    page_type = _page_type_from_payload(payload)
    if page_type in _EXISTING_ACCOUNT_PAGE_TYPES:
        return True, f"page_type={page_type}"
    if page_type not in _OTP_VERIFY_PAGE_TYPES:
        return False, f"page_type={page_type or '-'}"

    root = payload if isinstance(payload, dict) else {}
    page = root.get("page") if isinstance(root.get("page"), dict) else {}
    page_payload = page.get("payload") if isinstance(page.get("payload"), dict) else {}

    values: list[tuple[str, str]] = []
    for source_name, source in (("page", page_payload), ("root", root)):
        for key in (
            "email_verification_mode",
            "signup_mode",
            "original_screen_hint",
            "screen_hint",
            "mode",
            "flow",
        ):
            value = source.get(key)
            if isinstance(value, (list, tuple, set)):
                values.extend(
                    (f"{source_name}.{key}", str(item).strip().lower())
                    for item in value
                    if str(item or "").strip()
                )
            elif value not in (None, ""):
                values.append((f"{source_name}.{key}", str(value).strip().lower()))

    for key, value in values:
        normalized = value.replace("-", "_").replace(" ", "_")
        if normalized in _LOGIN_ROUTE_MARKERS or "passwordless_login" in normalized:
            return True, f"{key}={value}"

    for key, value in values:
        normalized = value.replace("-", "_").replace(" ", "_")
        if normalized in _SIGNUP_ROUTE_MARKERS or "passwordless_signup" in normalized:
            return False, f"{key}={value}"

    if page_payload.get("passwordless_otp_from_password_redirect") is True:
        return False, "page.passwordless_otp_from_password_redirect=true"

    return False, "otp_route=signup_default"


def _response_error(response: Any) -> tuple[str, str]:
    """Return the upstream business code and a bounded human-readable detail."""

    text = str(getattr(response, "text", "") or "")
    code = ""
    message = ""
    try:
        payload = response.json()
    except Exception:
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            code = str(error.get("code") or error.get("type") or "").strip()
            message = str(error.get("message") or error.get("detail") or "").strip()
        elif error not in (None, ""):
            message = str(error).strip()
        code = code or str(payload.get("code") or "").strip()
        message = message or str(payload.get("message") or "").strip()
    if not message:
        message = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()
    return code[:160], message[:500]


def _cookie_value_from_session(session: Any, name: str) -> str:
    """Read a cookie without failing when curl keeps duplicate domain scopes."""

    jar = getattr(getattr(session, "cookies", None), "jar", None)
    if jar is not None:
        preferred: list[tuple[int, str]] = []
        try:
            for cookie in jar:
                if str(getattr(cookie, "name", "") or "") != name:
                    continue
                value = str(getattr(cookie, "value", "") or "")
                domain = str(getattr(cookie, "domain", "") or "").lstrip(".").lower()
                score = 2 if domain == "chatgpt.com" else 1 if domain.endswith("chatgpt.com") else 0
                preferred.append((score, value))
        except Exception:
            preferred = []
        for _, value in sorted(preferred, reverse=True):
            if value:
                return value
    try:
        return str(session.cookies.get(name) or "")
    except Exception:
        return ""


def _allowed_protocol_continue_url(value: Any) -> str:
    candidate = urljoin("https://auth.openai.com/", str(value or "").strip())
    parsed = urlsplit(candidate)
    host = str(parsed.hostname or "").lower()
    allowed_host = host in {"auth.openai.com", "chatgpt.com"} or host.endswith(
        (".openai.com", ".chatgpt.com")
    )
    if parsed.scheme != "https" or not allowed_host:
        return ""
    return candidate


# ─── Sentinel helpers (ported from browser_register.py) ──────────

def _generate_datadog_trace_headers() -> dict:
    trace_hex = secrets.token_hex(8).rjust(16, "0")
    parent_hex = secrets.token_hex(8).rjust(16, "0")
    trace_id = str(int(trace_hex, 16))
    parent_id = str(int(parent_hex, 16))
    return {
        "traceparent": f"00-0000000000000000{trace_hex}-{parent_hex}-01",
        "tracestate": "dd=s:1;o:rum",
        "x-datadog-origin": "rum",
        "x-datadog-parent-id": parent_id,
        "x-datadog-sampling-priority": "1",
        "x-datadog-trace-id": trace_id,
    }


class _SentinelTokenGenerator:
    """Dynamic sentinel token generator – mirrors browser_register._SentinelTokenGenerator."""

    def __init__(self, device_id: str, user_agent: str, browser_fingerprint: Any = None):
        self.device_id = device_id or str(uuid.uuid4())
        self.user_agent = user_agent
        self.sid = str(uuid.uuid4())
        self.browser_fingerprint = (
            coerce_browser_fingerprint(browser_fingerprint)
            if browser_fingerprint is not None
            else None
        )

    @staticmethod
    def _fnv1a32(text: str) -> str:
        h = 2166136261
        for ch in text:
            h ^= ord(ch)
            h = (h * 16777619) & 0xFFFFFFFF
        h ^= (h >> 16)
        h = (h * 2246822507) & 0xFFFFFFFF
        h ^= (h >> 13)
        h = (h * 3266489909) & 0xFFFFFFFF
        h ^= (h >> 16)
        return f"{h & 0xFFFFFFFF:08x}"

    @staticmethod
    def _b64(data) -> str:
        return base64.b64encode(json.dumps(data, separators=(",", ":")).encode("utf-8")).decode("ascii")

    def _config(self) -> list:
        from zoneinfo import ZoneInfo

        perf_now = 1000 + random.random() * 49000
        fingerprint = self.browser_fingerprint
        screen = (
            f"{fingerprint.screen_width}x{fingerprint.screen_height}"
            if fingerprint is not None
            else "1920x1080"
        )
        locale = fingerprint.locale if fingerprint is not None else "en-US"
        languages = (
            ",".join(fingerprint.languages)
            if fingerprint is not None and fingerprint.languages
            else "en-US,en"
        )
        hardware_concurrency = (
            int(fingerprint.hardware_concurrency)
            if fingerprint is not None
            else random.choice([4, 8, 12, 16])
        )
        heap_limit = (
            4294705152
            if fingerprint is None or fingerprint.browser_family == "chrome"
            else None
        )
        try:
            zone = ZoneInfo(str(getattr(fingerprint, "timezone", "") or "UTC"))
        except Exception:
            zone = timezone.utc
        now = datetime.now(zone)
        date_string = now.strftime("%a %b %d %Y %H:%M:%S GMT%z") + (
            f" ({now.tzname() or 'Coordinated Universal Time'})"
        )
        return [
            screen,
            date_string,
            heap_limit,
            random.random(),
            self.user_agent,
            SENTINEL_SDK_URL,
            None,
            None,
            locale,
            languages,
            random.random(),
            "webkitTemporaryStorage\u2212undefined",
            "location",
            "Object",
            perf_now,
            self.sid,
            "",
            hardware_concurrency,
            int(time.time() * 1000 - perf_now),
        ]

    def generate_requirements_token(self) -> str:
        cfg = self._config()
        cfg[3] = 1
        cfg[9] = round(5 + random.random() * 45)
        return "gAAAAAC" + self._b64(cfg)

    def generate_token(self, seed: str, difficulty: str) -> str:
        max_attempts = 500000
        cfg = self._config()
        start_ms = int(time.time() * 1000)
        diff = str(difficulty or "0")
        for nonce in range(max_attempts):
            cfg[3] = nonce
            cfg[9] = round(int(time.time() * 1000) - start_ms)
            encoded = self._b64(cfg)
            digest = self._fnv1a32((seed or "") + encoded)
            if digest[: len(diff)] <= diff:
                return "gAAAAAB" + encoded + "~S"
        return "gAAAAAB" + self._b64(None)


class RegistrationEngine:
    """
    注册引擎
    负责协调邮箱服务、OAuth 流程和 OpenAI API 调用
    """

    def __init__(
        self,
        email_service: Any,
        proxy_url: Optional[str] = None,
        callback_logger: Optional[Callable[[str], None]] = None,
        task_uuid: Optional[str] = None,
        capture_codex_oauth: bool = False,
        browser_fingerprint: Any = None,
        profile_name: str = "",
        profile_birthdate: str = "",
        stop_check: Optional[Callable[[], None]] = None,
        otp_wait_timeout: int = 120,
        otp_resend_wait_timeout: int = 90,
    ):
        """
        初始化注册引擎

        Args:
            email_service: 邮箱服务实例
            proxy_url: 代理 URL
            callback_logger: 日志回调函数
            task_uuid: 任务 UUID（用于数据库记录）
        """
        self.email_service = email_service
        self.proxy_url = proxy_url
        self.callback_logger = callback_logger or (lambda msg: logger.info(msg))
        self.task_uuid = task_uuid
        # GPT signup and ChatGPT Web Session capture are the shared transport
        # contract. RT/Codex capture belongs to the mode-owned second stage.
        self.capture_codex_oauth = bool(capture_codex_oauth)
        self.profile_name = re.sub(r"\s+", " ", str(profile_name or "")).strip()
        self.profile_birthdate = str(profile_birthdate or "").strip()
        self.stop_check = stop_check
        try:
            self.otp_wait_timeout = max(30, min(int(otp_wait_timeout or 120), 3600))
        except (TypeError, ValueError):
            self.otp_wait_timeout = 120
        try:
            self.otp_resend_wait_timeout = max(
                0, min(int(otp_resend_wait_timeout or 0), 3600)
            )
        except (TypeError, ValueError):
            self.otp_resend_wait_timeout = 90
        self.browser_fingerprint = (
            coerce_browser_fingerprint(browser_fingerprint)
            if browser_fingerprint is not None
            else None
        )

        # 创建 HTTP 客户端
        self.http_client = OpenAIHTTPClient(
            proxy_url=proxy_url,
            browser_fingerprint=self.browser_fingerprint,
        )

        # 创建 OAuth 管理器
        from .constants import OAUTH_CLIENT_ID, OAUTH_AUTH_URL, OAUTH_TOKEN_URL, OAUTH_REDIRECT_URI, OAUTH_SCOPE
        self.oauth_manager = OAuthManager(
            client_id=OAUTH_CLIENT_ID,
            auth_url=OAUTH_AUTH_URL,
            token_url=OAUTH_TOKEN_URL,
            redirect_uri=OAUTH_REDIRECT_URI,
            scope=OAUTH_SCOPE,
            proxy_url=proxy_url  # 传递代理配置
        )

        # 状态变量
        self.email: Optional[str] = None
        self.password: Optional[str] = None  # 注册密码
        self.email_info: Optional[Dict[str, Any]] = None
        self.oauth_start: Optional[OAuthStart] = None
        self.session: Optional[cffi_requests.Session] = None
        self.session_token: Optional[str] = None  # 会话令牌
        self.logs: list = []
        self._otp_sent_at: Optional[float] = None  # OTP 发送时间戳
        self._otp_send_count = 0
        self._otp_resend_count = 0
        self._last_otp_resend_error = ""
        self._is_existing_account: bool = False  # 是否为已注册账号（用于自动登录）
        self._device_id: Optional[str] = None
        self._sentinel_token: Optional[str] = None
        self._signup_sentinel: Optional[SentinelPayload] = None
        self._password_sentinel: Optional[SentinelPayload] = None
        self._create_account_continue_url: Optional[str] = None
        self._otp_continue_url: Optional[str] = None
        self._otp_page_type: Optional[str] = None
        self._last_oauth_error: str = ""
        self._stage = "init"
        self._signup_page_type = ""
        self._password_page_type = ""
        self._session_poll_attempts = 0
        self._last_web_session_material: dict[str, Any] = {}
        self._signup_committed = False
        self._last_protocol_failure: Optional[ProtocolFlowFailure] = None

    def _checkpoint(self) -> None:
        stop_check = getattr(self, "stop_check", None)
        if callable(stop_check):
            stop_check()

    def _set_stage(self, stage: str) -> None:
        self._stage = str(stage or "unknown").strip() or "unknown"
        self._checkpoint()

    def _record_failure(
        self,
        code: str,
        stage: str,
        message: str,
        *,
        http_status: int = 0,
        upstream_code: str = "",
        retriable: bool = False,
    ) -> str:
        failure = ProtocolFlowFailure(
            code=str(code or "protocol_failure").strip(),
            stage=str(stage or getattr(self, "_stage", "") or "unknown").strip(),
            message=str(message or code or "protocol failure").strip(),
            http_status=max(int(http_status or 0), 0),
            upstream_code=str(upstream_code or "").strip(),
            retriable=bool(retriable) and not bool(
                getattr(self, "_signup_committed", False)
            ),
        )
        self._last_protocol_failure = failure
        return failure.render()

    def _record_http_failure(
        self,
        *,
        stage: str,
        response: Any,
        fallback_code: str,
        fallback_message: str,
    ) -> str:
        status = int(getattr(response, "status_code", 0) or 0)
        upstream_code, detail = _response_error(response)
        body = str(getattr(response, "text", "") or "").lower()
        cf_challenge = (
            str((getattr(response, "headers", {}) or {}).get("cf-mitigated") or "").lower()
            == "challenge"
            or "cf-chl-" in body
            or "challenge-platform" in body
        )
        code = str(upstream_code or fallback_code or "upstream_http_error").strip()
        if cf_challenge:
            code = "cloudflare_challenge"
        elif status == 429:
            code = "upstream_rate_limited"
        elif status >= 500:
            code = "upstream_server_error"
        retriable = bool(cf_challenge or status in {0, 408, 425, 429} or status >= 500)
        return self._record_failure(
            code,
            stage,
            detail or fallback_message,
            http_status=status,
            upstream_code=upstream_code,
            retriable=retriable,
        )

    def _failure_or(
        self,
        code: str,
        stage: str,
        message: str,
        *,
        retriable: bool = False,
    ) -> str:
        last_failure = getattr(self, "_last_protocol_failure", None)
        if last_failure is not None:
            return last_failure.render()
        return self._record_failure(
            code,
            stage,
            message,
            retriable=retriable,
        )

    def _attach_protocol_metadata(self, result: RegistrationResult) -> None:
        metadata = dict(result.metadata or {})
        metadata.update(
            {
                "protocol_stage": getattr(self, "_stage", ""),
                "protocol_signup_page_type": getattr(self, "_signup_page_type", ""),
                "protocol_password_page_type": getattr(self, "_password_page_type", ""),
                "protocol_otp_page_type": str(getattr(self, "_otp_page_type", "") or ""),
                "protocol_session_poll_attempts": int(
                    getattr(self, "_session_poll_attempts", 0) or 0
                ),
                "protocol_otp_send_count": int(
                    getattr(self, "_otp_send_count", 0) or 0
                ),
                "protocol_otp_resend_count": int(
                    getattr(self, "_otp_resend_count", 0) or 0
                ),
                "registration_signup_committed": bool(
                    getattr(self, "_signup_committed", False)
                ),
            }
        )
        last_failure = getattr(self, "_last_protocol_failure", None)
        if last_failure is not None:
            metadata.update(last_failure.to_metadata())
            if getattr(self, "_signup_committed", False):
                metadata["registration_post_signup_failure_code"] = last_failure.code
        result.metadata = metadata

    def _web_session_material(
        self,
        payload: Any = None,
    ) -> tuple[dict[str, Any], list[str]]:
        """Normalize whatever Web Session material is available at this point."""

        data = payload if isinstance(payload, dict) else {}
        access_token = str(data.get("accessToken") or data.get("access_token") or "").strip()
        session_token = str(
            data.get("sessionToken")
            or data.get("session_token")
            or _session_token_from_session(self.session)
            or ""
        ).strip()
        claims = decode_jwt_payload(access_token)
        auth_claims = (
            claims.get("https://api.openai.com/auth")
            if isinstance(claims, dict)
            else {}
        )
        auth_claims = auth_claims if isinstance(auth_claims, dict) else {}
        account = data.get("account") if isinstance(data.get("account"), dict) else {}
        user = data.get("user") if isinstance(data.get("user"), dict) else {}
        cookie_header = _cookie_header_from_session(self.session)
        cookie_lower = cookie_header.lower()
        if session_token and not any(
            f"{name.lower()}=" in cookie_lower for name in _SESSION_COOKIE_NAMES
        ):
            cookie_header = (
                f"{cookie_header}; " if cookie_header else ""
            ) + f"__Secure-next-auth.session-token={session_token}"
        account_id = str(
            account.get("id")
            or auth_claims.get("chatgpt_account_id")
            or _cookie_value_from_session(self.session, "_account")
            or ""
        ).strip()
        material = {
            "access_token": access_token,
            "session_token": session_token,
            "cookie_header": cookie_header,
            "account_id": account_id,
            "workspace_id": account_id,
            "user_id": str(
                user.get("id")
                or auth_claims.get("chatgpt_user_id")
                or auth_claims.get("user_id")
                or ""
            ).strip(),
            "user": user,
            "account": account,
            "expires": data.get("expires"),
        }
        missing = [
            name
            for name, value in (
                ("access_token", material["access_token"]),
                ("session_token", material["session_token"]),
                ("cookie_header", material["cookie_header"]),
                ("account_id", material["account_id"]),
            )
            if not str(value or "").strip()
        ]
        return material, missing

    def _pending_result(
        self,
        result: RegistrationResult,
        *,
        fallback_message: str = "开户已确认但 Web Session 材料待补抓",
    ) -> RegistrationResult:
        """Return a persistable pending account without replaying signup."""

        if not self._signup_committed:
            return result
        failure = getattr(self, "_last_protocol_failure", None)
        material = dict(getattr(self, "_last_web_session_material", {}) or {})
        if not material:
            material, _ = self._web_session_material()
            self._last_web_session_material = dict(material)
        reason = str(
            getattr(failure, "code", "") or fallback_message or "post_signup_session_capture_incomplete"
        ).strip()
        result.email = str(result.email or self.email or "")
        result.password = str(result.password or self.password or "")
        result.account_id = str(material.get("account_id") or "")
        result.workspace_id = str(material.get("workspace_id") or result.account_id or "")
        result.access_token = str(material.get("access_token") or "")
        result.refresh_token = ""
        result.id_token = result.access_token
        result.session_token = str(material.get("session_token") or "")
        result.source = "registered_auth_pending"
        result.success = True
        result.error_message = ""
        result.metadata = {
            "email_service": self.email_service.service_type.value,
            "proxy_used": self.proxy_url,
            "registration_stage_complete": True,
            "registration_session_capture": "pending",
            "registration_signup_committed": True,
            "registered_auth_pending": True,
            "session_capture_pending": True,
            "session_capture_pending_reason": reason,
            "registration_post_signup_failure_code": reason,
            "web_session_capture_mode": "pending_protocol",
            "user": material.get("user") or {},
            "account": material.get("account") or {},
            "user_id": str(material.get("user_id") or ""),
            "expires": material.get("expires"),
            "cookies": str(material.get("cookie_header") or ""),
            "cookie_header": str(material.get("cookie_header") or ""),
            "browser_fingerprint": browser_fingerprint_to_dict(
                self.browser_fingerprint
            ),
            "registration_profile": {
                "name": str(self.profile_name or ""),
                "birthdate": str(self.profile_birthdate or ""),
            },
        }
        self._log(
            "开户已确认但 Web Session 仍不完整；保存 registered_auth_pending，"
            f"禁止重复 signup: reason={reason}",
            "warning",
        )
        return result

    def _browser_headers(
        self,
        url: str,
        *,
        accept: str = "application/json",
        referer: str = "",
        origin: str = "",
        content_type: str = "",
        navigation: bool = False,
    ) -> dict[str, str]:
        fingerprint = getattr(self, "browser_fingerprint", None)
        default_headers = dict(
            getattr(getattr(self, "http_client", None), "default_headers", {}) or {}
        )
        user_agent = str(
            getattr(fingerprint, "user_agent", "")
            or default_headers.get("User-Agent")
            or "Mozilla/5.0"
        )
        return build_browser_headers(
            url=url,
            user_agent=user_agent,
            sec_ch_ua=str(getattr(fingerprint, "sec_ch_ua", "") or ""),
            chrome_full_version=str(
                getattr(fingerprint, "chrome_full_version", "") or ""
            ),
            sec_ch_platform_version=str(
                getattr(fingerprint, "platform_version", "") or ""
            ),
            accept=accept,
            accept_language=str(
                getattr(fingerprint, "accept_language", "")
                or default_headers.get("Accept-Language")
                or "en-US,en;q=0.9"
            ),
            referer=referer or None,
            origin=origin or None,
            content_type=content_type or None,
            navigation=navigation,
            browser_family=str(getattr(fingerprint, "browser_family", "") or ""),
        )

    def _log(self, message: str, level: str = "info"):
        """记录日志"""
        timestamp = beijing_log_time()
        log_message = f"[{timestamp}] {message}"

        # 添加到日志列表
        self.logs.append(log_message)

        # 调用回调函数
        if self.callback_logger:
            self.callback_logger(message)

        # 记录到数据库（如果有关联任务）
        if self.task_uuid:
            try:
                with get_db() as db:
                    crud.append_task_log(db, self.task_uuid, message)
            except Exception as e:
                logger.warning(f"记录任务日志失败: {e}")

        # 根据级别记录到日志系统
        if level == "error":
            logger.error(message)
        elif level == "warning":
            logger.warning(message)
        else:
            logger.info(message)

    def _generate_password(self, length: int = DEFAULT_PASSWORD_LENGTH) -> str:
        """生成随机密码"""
        # OpenAI 注册页对纯字母数字密码存在更高概率拒绝，补一个符号位更稳。
        specials = ",._!@#"
        if length < 10:
            length = 10
        core = ''.join(secrets.choice(PASSWORD_CHARSET) for _ in range(length - 2))
        return (
            secrets.choice("abcdefghijklmnopqrstuvwxyz")
            + secrets.choice("0123456789")
            + secrets.choice(specials)
            + core
        )[:length]

    def _load_create_account_password_page(self) -> bool:
        """预加载 create-account/password 页面，拿到页面阶段 cookie。"""
        try:
            response = self.session.get(
                "https://auth.openai.com/create-account/password",
                headers=self._browser_headers(
                    "https://auth.openai.com/create-account/password",
                    accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    referer="https://chatgpt.com/",
                    navigation=True,
                ),
                timeout=20,
            )
            self._log(f"加载密码页状态: {response.status_code}")
            return response.status_code == 200
        except Exception as e:
            self._log(f"加载密码页失败: {e}", "warning")
            return False

    def _check_ip_location(self) -> Tuple[bool, Optional[str]]:
        """检查 IP 地理位置"""
        try:
            return self.http_client.check_ip_location()
        except Exception as e:
            self._log(f"检查 IP 地理位置失败: {e}", "error")
            return False, None

    def _create_email(self) -> bool:
        """创建邮箱"""
        try:
            self._log(f"正在创建 {self.email_service.service_type.value} 邮箱...")
            self.email_info = self.email_service.create_email()

            if not self.email_info or "email" not in self.email_info:
                self._log("创建邮箱失败: 返回信息不完整", "error")
                return False

            self.email = self.email_info["email"]
            self._log(f"[邮箱] 当前邮箱={mask_email_for_log(self.email)}")
            return True

        except Exception as e:
            self._log(f"创建邮箱失败: {e}", "error")
            return False

    def _start_oauth(self) -> bool:
        """通过 chatgpt.com NextAuth 发起 OAuth 流程"""
        self._last_oauth_error = ""
        try:
            from .constants import CHATGPT_APP
            self._log("通过 chatgpt.com NextAuth 发起 OAuth...")

            # 1. 访问 chatgpt.com 获取基础 cookie
            self.session.get(
                f"{CHATGPT_APP}/",
                headers=self._browser_headers(
                    f"{CHATGPT_APP}/",
                    accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    navigation=True,
                ),
                timeout=20,
            )
            oai_did = _cookie_value_from_session(self.session, "oai-did")
            self._log(f"chatgpt.com oai-did: {oai_did[:20]}...")

            # 2. 获取 CSRF token
            csrf_resp = self.session.get(
                f"{CHATGPT_APP}/api/auth/csrf",
                headers=self._browser_headers(
                    f"{CHATGPT_APP}/api/auth/csrf",
                    referer=f"{CHATGPT_APP}/",
                ),
                timeout=20,
            )
            csrf_data = _response_json(csrf_resp, "ChatGPT CSRF 请求")
            csrf_token = csrf_data.get("csrfToken", "")
            if not csrf_token:
                # 从 cookie 中提取
                csrf_cookie = _cookie_value_from_session(
                    self.session,
                    "__Host-next-auth.csrf-token",
                )
                csrf_token = csrf_cookie.split("%7C")[0] if "%7C" in csrf_cookie else csrf_cookie.split("|")[0]
            if not csrf_token:
                raise RuntimeError("ChatGPT CSRF 请求未返回 csrfToken")
            self._log(f"CSRF token: {csrf_token[:20]}...")

            # 3. 调用 signin/openai 获取 authorize URL
            signin_url = f"{CHATGPT_APP}/api/auth/signin/openai"
            if oai_did:
                signin_url += f"?prompt=login&ext-oai-did={oai_did}"

            signin_resp = self.session.post(
                signin_url,
                headers=self._browser_headers(
                    signin_url,
                    referer=f"{CHATGPT_APP}/",
                    origin=CHATGPT_APP,
                    content_type="application/x-www-form-urlencoded",
                ),
                data=f"callbackUrl={CHATGPT_APP}%2F&csrfToken={csrf_token}&json=true",
                timeout=15,
            )
            self._log(f"signin/openai 状态: {signin_resp.status_code}")

            signin_data = _response_json(signin_resp, "ChatGPT signin/openai 请求")
            auth_url = signin_data.get("url", "")
            if not auth_url:
                self._log("signin/openai 未返回 authorize URL", "error")
                return False

            self._log(f"OAuth URL: {auth_url[:80]}...")

            # 存储为 OAuthStart (不需要 code_verifier，由 chatgpt.com 后端处理)
            self.oauth_start = OAuthStart(
                auth_url=auth_url,
                state="",  # state 由 NextAuth 管理
                code_verifier="",  # 不需要
                redirect_uri="",  # 不需要
            )
            return True

        except Exception as e:
            self._last_oauth_error = str(e)
            self._log(f"NextAuth OAuth 流程失败: {e}", "error")
            return False

    def _init_session(self) -> bool:
        """初始化会话"""
        try:
            self.session = self.http_client.session
            self._install_http_trace()
            return True
        except Exception as e:
            self._log(f"初始化会话失败: {e}", "error")
            return False

    def _install_http_trace(self) -> None:
        """Attach a redacted request/response trace to the protocol session."""

        session = self.session
        if session is None or getattr(session, "_auto_gpt_http_trace_wrapped", False):
            return
        original_request = getattr(session, "request", None)
        if not callable(original_request):
            return

        def _payload_size(value: Any) -> int:
            if value in (None, ""):
                return 0
            if isinstance(value, (bytes, bytearray)):
                return len(value)
            if isinstance(value, str):
                return len(value.encode("utf-8", errors="replace"))
            try:
                return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            except Exception:
                return 0

        def _page_for_url(url: Any) -> str:
            lowered = str(url or "").lower()
            if "email-otp" in lowered or "email-verification" in lowered:
                return "email_otp"
            if "create-account/password" in lowered or "user/register" in lowered:
                return "password"
            if "about-you" in lowered or "create_account" in lowered:
                return "about_you"
            if "/api/auth/session" in lowered:
                return "chatgpt_session"
            if "authorize" in lowered or "signin/openai" in lowered:
                return "authorize"
            return ""

        def _traced_request(*args: Any, **kwargs: Any):
            method = kwargs.get("method") or (args[0] if args else "GET")
            url = kwargs.get("url") or (args[1] if len(args) > 1 else args[0] if args else "")
            started = time.monotonic()
            request_bytes = _payload_size(kwargs.get("data")) + _payload_size(kwargs.get("json"))
            request_headers = dict(getattr(session, "headers", {}) or {})
            request_headers.update(dict(kwargs.get("headers") or {}))
            request_body = kwargs.get("json") if kwargs.get("json") is not None else kwargs.get("data")
            try:
                response = original_request(*args, **kwargs)
            except Exception as exc:
                duration_ms = round((time.monotonic() - started) * 1000)
                self._log(
                    format_http_trace_log(
                        method,
                        url,
                        status="ERROR",
                        duration_ms=duration_ms,
                        page=_page_for_url(url),
                        request_bytes=request_bytes,
                        error=str(exc),
                    ),
                    "debug",
                )
                try:
                    from ..registration_diagnostics import (
                        record_registration_protocol_http_exchange,
                    )

                    record_registration_protocol_http_exchange(
                        method=method,
                        url=url,
                        request_headers=request_headers,
                        request_body=request_body,
                        duration_ms=duration_ms,
                        error=str(exc),
                    )
                except Exception:
                    pass
                raise
            try:
                response_bytes = len(getattr(response, "content", b"") or b"")
            except Exception:
                response_bytes = 0
            duration_ms = round((time.monotonic() - started) * 1000)
            self._log(
                format_http_trace_log(
                    method,
                    url,
                    status=getattr(response, "status_code", ""),
                    duration_ms=duration_ms,
                    page=_page_for_url(getattr(response, "url", "") or url),
                    request_bytes=request_bytes,
                    response_bytes=response_bytes,
                ),
                "debug",
            )
            try:
                from ..registration_diagnostics import (
                    record_registration_protocol_http_exchange,
                )

                record_registration_protocol_http_exchange(
                    method=method,
                    url=getattr(response, "url", "") or url,
                    request_headers=request_headers,
                    request_body=request_body,
                    status=getattr(response, "status_code", 0),
                    response_headers=getattr(response, "headers", {}) or {},
                    response_body=getattr(response, "content", b"") or b"",
                    duration_ms=duration_ms,
                )
            except Exception:
                pass
            return response

        try:
            session.request = _traced_request
            session._auto_gpt_http_trace_wrapped = True
        except Exception:
            # Some curl backends expose a read-only request descriptor.  The
            # registration flow must remain usable even when tracing cannot be
            # attached to that particular session implementation.
            return

    def _get_device_id(self) -> Optional[str]:
        """获取 Device ID"""
        try:
            if not self.oauth_start:
                return None

            response = self.session.get(
                self.oauth_start.auth_url,
                headers=self._browser_headers(
                    self.oauth_start.auth_url,
                    accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    referer="https://chatgpt.com/",
                    navigation=True,
                ),
                timeout=20,
            )
            did = _cookie_value_from_session(self.session, "oai-did")
            self._log(f"Device ID: {did}")
            return did

        except Exception as e:
            self._log(f"获取 Device ID 失败: {e}", "error")
            return None

    def _check_sentinel(self, did: str, *, flow: str = "authorize_continue") -> Optional[SentinelPayload]:
        """检查 Sentinel 拦截（动态生成 token + 处理 PoW）"""
        try:
            ua = self.http_client.default_headers.get("User-Agent", "")
            generator = _SentinelTokenGenerator(did, ua, self.browser_fingerprint)
            sent_p = generator.generate_requirements_token()
            sen_req_body = json.dumps({"p": sent_p, "id": did, "flow": flow}, separators=(",", ":"))

            from .constants import SENTINEL_FRAME_URL
            response = self.http_client.post(
                OPENAI_API_ENDPOINTS["sentinel"],
                headers={
                    "origin": "https://sentinel.openai.com",
                    "referer": SENTINEL_FRAME_URL,
                    "content-type": "text/plain;charset=UTF-8",
                },
                data=sen_req_body,
            )

            if response.status_code == 200:
                data = response.json()
                sen_token = str(data.get("token") or "")
                turnstile = data.get("turnstile") or {}
                if not sen_token:
                    self._record_failure(
                        "sentinel_token_missing",
                        "sentinel",
                        f"Sentinel 未返回 challenge token: flow={flow}",
                        retriable=True,
                    )
                    return None

                # Handle proofofwork challenge if required
                initial_p = sent_p  # keep for dx decryption
                pow_meta = data.get("proofofwork") or {}
                if pow_meta.get("required") and pow_meta.get("seed"):
                    sent_p = generator.generate_token(
                        str(pow_meta.get("seed") or ""),
                        str(pow_meta.get("difficulty") or "0"),
                    )
                    self._log(f"Sentinel PoW solved: flow={flow}")

                # Solve turnstile dx with VM
                t_value = ""
                dx_b64 = str(turnstile.get("dx") or "")
                if dx_b64:
                    try:
                        from .sentinel_vm import solve_turnstile_dx
                        from .constants import SENTINEL_SDK_URL
                        t_value = solve_turnstile_dx(
                            dx_b64,
                            initial_p,
                            user_agent=ua,
                            sdk_url=SENTINEL_SDK_URL,
                            browser_fingerprint=browser_fingerprint_to_dict(
                                self.browser_fingerprint
                            ),
                        )
                        self._log(f"Sentinel VM solved: t_len={len(t_value)} flow={flow}")
                    except Exception as vm_err:
                        self._log(f"Sentinel VM failed: {vm_err}", "warning")
                    if not str(t_value or "").strip():
                        self._record_failure(
                            "sentinel_turnstile_unsolved",
                            "sentinel",
                            f"Sentinel turnstile dx 未解出有效 t: flow={flow}",
                            retriable=True,
                        )
                        return None

                payload = SentinelPayload(
                    p=sent_p,
                    c=sen_token,
                    flow=flow,
                    t=t_value,
                )
                self._log(f"Sentinel token 获取成功: flow={flow}")
                return payload
            else:
                self._log(f"Sentinel 检查失败: flow={flow} status={response.status_code}", "warning")
                self._record_http_failure(
                    stage="sentinel",
                    response=response,
                    fallback_code="sentinel_request_failed",
                    fallback_message=f"Sentinel 请求失败: flow={flow}",
                )
                return None

        except TaskInterruption:
            raise
        except Exception as e:
            self._log(f"Sentinel 检查异常: flow={flow} {e}", "warning")
            self._record_failure(
                "sentinel_request_exception",
                "sentinel",
                f"Sentinel 请求异常: flow={flow} {e}",
                retriable=True,
            )
            return None

    def _submit_signup_form(self, did: str, sen_payload: Optional[SentinelPayload]) -> SignupFormResult:
        """
        提交注册表单（通过 authorize/continue 建立 session）

        Returns:
            SignupFormResult: 提交结果，包含账号状态判断
        """
        try:
            self._device_id = did
            self._signup_sentinel = sen_payload
            self._sentinel_token = sen_payload.c if sen_payload else None
            signup_body = json.dumps(
                {
                    "username": {"value": str(self.email or ""), "kind": "email"},
                    "screen_hint": "signup",
                },
                separators=(",", ":"),
            )

            headers = self._browser_headers(
                OPENAI_API_ENDPOINTS["signup"],
                referer="https://auth.openai.com/create-account",
                origin="https://auth.openai.com",
                content_type="application/json",
            )

            if sen_payload:
                sentinel = json.dumps({
                    "p": sen_payload.p,
                    "t": sen_payload.t,
                    "c": sen_payload.c,
                    "id": did,
                    "flow": sen_payload.flow,
                }, separators=(",", ":"))
                headers["openai-sentinel-token"] = sentinel

            request_started_at = _otp_request_started_at()
            response = self.session.post(
                OPENAI_API_ENDPOINTS["signup"],
                headers=headers,
                data=signup_body,
                timeout=20,
            )

            self._log(f"提交注册表单状态: {response.status_code}")

            if response.status_code != 200:
                detail = self._record_http_failure(
                    stage="authorize_continue",
                    response=response,
                    fallback_code="authorize_continue_failed",
                    fallback_message="提交注册邮箱失败",
                )
                return SignupFormResult(
                    success=False,
                    error_message=detail,
                )

            try:
                response_data = response.json()
                if not isinstance(response_data, dict):
                    raise ValueError("response root is not an object")
                page_type = _page_type_from_payload(response_data)
                if not page_type:
                    raise ValueError("response.page.type is empty")
                self._signup_page_type = page_type
                self._log(f"响应页面类型: {page_type}")

                is_existing, route_signal = _account_route_from_payload(response_data)
                if page_type in _OTP_VERIFY_PAGE_TYPES:
                    # An OTP page may be an immediate passwordless signup route;
                    # retain the request timestamp for stale-code filtering in
                    # both signup and login variants.
                    self._otp_sent_at = request_started_at
                if is_existing:
                    self._log(
                        f"检测到已注册账号，将自动切换到登录流程｜signal={route_signal}"
                    )
                    self._is_existing_account = True
                elif page_type in _OTP_VERIFY_PAGE_TYPES:
                    self._log(f"OTP 路由按注册流程继续｜signal={route_signal}")

                return SignupFormResult(
                    success=True,
                    page_type=page_type,
                    is_existing_account=is_existing,
                    response_data=response_data
                )

            except Exception as parse_error:
                detail = self._record_failure(
                    "authorize_continue_invalid_response",
                    "authorize_continue",
                    f"提交注册邮箱返回无效状态: {parse_error}",
                    http_status=200,
                    retriable=True,
                )
                self._log(detail, "warning")
                return SignupFormResult(success=False, error_message=detail)

        except TaskInterruption:
            raise
        except Exception as e:
            self._log(f"提交注册表单失败: {e}", "error")
            detail = self._record_failure(
                "authorize_continue_exception",
                "authorize_continue",
                str(e),
                retriable=True,
            )
            return SignupFormResult(success=False, error_message=detail)

    @staticmethod
    def _is_password_policy_rejection(code: str, message: str) -> bool:
        normalized_code = str(code or "").strip().lower()
        normalized_message = str(message or "").strip().lower()
        if normalized_code in {
            "invalid_password",
            "password_policy_violation",
            "password_too_short",
            "password_too_weak",
        }:
            return True
        return "password" in normalized_message and any(
            marker in normalized_message
            for marker in ("at least", "characters", "requirement", "too weak", "too short")
        )

    def _register_password(self) -> Tuple[bool, Optional[str]]:
        """Submit one password, retrying once only for an explicit policy rejection."""

        try:
            preferred = str(
                getattr(self, "_preferred_password", None) or self.password or ""
            ).strip()
            candidates = [preferred or self._generate_password()]
            index = 0
            while index < len(candidates):
                password = candidates[index]
                index += 1
                self.password = password
                self._checkpoint()

                if not self._load_create_account_password_page():
                    self._record_failure(
                        "password_page_unavailable",
                        "password",
                        "create-account/password 页面预热失败",
                        retriable=True,
                    )
                    return False, None

                self._password_sentinel = None
                if self._device_id:
                    self._password_sentinel = self._check_sentinel(
                        self._device_id,
                        flow="username_password_create",
                    )
                if not self._password_sentinel:
                    if getattr(self, "_last_protocol_failure", None) is None:
                        self._record_failure(
                            "password_sentinel_unavailable",
                            "password",
                            "密码阶段未获得有效 Sentinel token",
                            retriable=True,
                        )
                    return False, None
                self._log(
                    f"密码阶段 Sentinel 已刷新: flow={self._password_sentinel.flow} "
                    f"turnstile={'yes' if self._password_sentinel.t else 'no'}"
                )
                self._log(
                    f"[注册] 注册密码已生成｜长度={len(password)}｜候选={index}/{len(candidates)}"
                )

                register_headers = {
                    **self._browser_headers(
                        OPENAI_API_ENDPOINTS["register"],
                        referer="https://auth.openai.com/create-account/password",
                        origin="https://auth.openai.com",
                        content_type="application/json",
                    ),
                    "oai-device-id": str(self._device_id or ""),
                    "openai-sentinel-token": json.dumps(
                        {
                            "p": self._password_sentinel.p,
                            "t": self._password_sentinel.t,
                            "c": self._password_sentinel.c,
                            "id": self._device_id,
                            "flow": self._password_sentinel.flow,
                        },
                        separators=(",", ":"),
                    ),
                    **_generate_datadog_trace_headers(),
                }

                request_started_at = _otp_request_started_at()
                response = self.session.post(
                    OPENAI_API_ENDPOINTS["register"],
                    headers=register_headers,
                    data=json.dumps(
                        {"password": password, "username": self.email},
                        separators=(",", ":"),
                    ),
                    timeout=30,
                )
                self._log(
                    f"提交密码状态[{index}/{len(candidates)}]: {response.status_code}"
                )

                if response.status_code == 200:
                    try:
                        payload = response.json()
                    except Exception as exc:
                        self._record_failure(
                            "password_invalid_response",
                            "password",
                            f"密码注册返回无效 JSON: {exc}",
                            http_status=200,
                            retriable=True,
                        )
                        return False, None
                    page_type = _page_type_from_payload(payload)
                    self._password_page_type = page_type
                    self._log(f"注册响应页面类型: {page_type or '-'}")
                    if page_type in _EXISTING_ACCOUNT_PAGE_TYPES:
                        self._is_existing_account = True
                        self._record_failure(
                            "existing_account_detected",
                            "password",
                            f"user_already_exists: page={page_type}",
                        )
                        return False, None
                    if page_type not in (
                        _OTP_SEND_PAGE_TYPES
                        | _OTP_VERIFY_PAGE_TYPES
                        | _ABOUT_YOU_PAGE_TYPES
                        | _EXTERNAL_PAGE_TYPES
                    ):
                        self._record_failure(
                            "password_unexpected_state",
                            "password",
                            f"密码注册返回未知 page.type={page_type or '-'}",
                            http_status=200,
                            retriable=True,
                        )
                        return False, None
                    if page_type in _OTP_VERIFY_PAGE_TYPES:
                        self._otp_sent_at = request_started_at
                    return True, password

                upstream_code, upstream_message = _response_error(response)
                if (
                    len(candidates) == 1
                    and self._is_password_policy_rejection(upstream_code, upstream_message)
                ):
                    replacement = self._generate_password()
                    if replacement != password:
                        candidates.append(replacement)
                        self._log(
                            "密码被上游策略拒绝，仅刷新 Sentinel 并改用一次系统强密码",
                            "warning",
                        )
                        continue
                detail = self._record_http_failure(
                    stage="password",
                    response=response,
                    fallback_code="password_submit_failed",
                    fallback_message="密码注册请求失败",
                )
                self._log(detail, "warning")
                if upstream_code in {"user_exists", "username_already_exists"} or any(
                    marker in upstream_message.lower()
                    for marker in ("already exists", "please login instead")
                ):
                    self._is_existing_account = True
                return False, None
            return False, None
        except TaskInterruption:
            raise
        except Exception as e:
            detail = self._record_failure(
                "password_submit_exception",
                "password",
                str(e),
                retriable=True,
            )
            self._log(detail, "error")
            return False, None

    def _mark_email_as_registered(self):
        """标记邮箱为已注册状态（用于防止重复尝试）"""
        try:
            with get_db() as db:
                # 检查是否已存在该邮箱的记录
                existing = crud.get_account_by_email(db, self.email)
                if not existing:
                    # 创建一个失败记录，标记该邮箱已注册过
                    crud.create_account(
                        db,
                        email=self.email,
                        password="",  # 空密码表示未成功注册
                        email_service=self.email_service.service_type.value,
                        email_service_id=self.email_info.get("service_id") if self.email_info else None,
                        status="failed",
                        extra_data={"register_failed_reason": "email_already_registered_on_openai"}
                    )
                    self._log(f"已在数据库中标记邮箱 {self.email} 为已注册状态")
        except Exception as e:
            logger.warning(f"标记邮箱状态失败: {e}")

    def _send_verification_code(
        self,
        *,
        referer: str = "",
        record_failure: bool = True,
    ) -> bool:
        """发送验证码，支持同一认证会话中的补发。"""
        try:
            request_started_at = _otp_request_started_at()
            request_referer = str(referer or "").strip() or (
                "https://auth.openai.com/email-verification"
                if str(getattr(self, "_stage", "") or "").startswith("email_otp")
                else "https://auth.openai.com/create-account/password"
            )
            response = self.session.get(
                OPENAI_API_ENDPOINTS["send_otp"],
                headers=self._browser_headers(
                    OPENAI_API_ENDPOINTS["send_otp"],
                    referer=request_referer,
                ),
                timeout=20,
            )

            self._log(f"验证码发送状态: {response.status_code}")
            if 200 <= int(response.status_code or 0) < 300:
                self._otp_sent_at = request_started_at
                self._otp_send_count = int(
                    getattr(self, "_otp_send_count", 0) or 0
                ) + 1
                self._last_otp_resend_error = ""
                return True
            detail = self._record_http_failure(
                stage="email_otp_send",
                response=response,
                fallback_code="email_otp_send_failed",
                fallback_message="邮箱验证码发送失败",
            )
            self._last_otp_resend_error = detail
            if not record_failure:
                self._last_protocol_failure = None
            return False

        except TaskInterruption:
            raise
        except Exception as e:
            self._log(f"发送验证码失败: {e}", "error")
            detail = self._record_failure(
                "email_otp_send_exception",
                "email_otp_send",
                str(e),
                retriable=True,
            )
            self._last_otp_resend_error = detail
            if not record_failure:
                self._last_protocol_failure = None
            return False

    def _get_verification_code(
        self,
        timeout: Optional[int] = None,
        *,
        resend_timeout: Optional[int] = None,
        resend: bool = False,
    ) -> Optional[str]:
        """等待验证码；首轮超时后在同一会话补发一次再等待。"""

        try:
            first_timeout = max(
                1,
                int(
                    getattr(self, "otp_wait_timeout", 120)
                    if timeout is None
                    else timeout
                ),
            )
        except (TypeError, ValueError):
            first_timeout = int(getattr(self, "otp_wait_timeout", 120) or 120)
        if not resend:
            second_timeout = 0
        else:
            try:
                second_timeout = max(
                    0,
                    int(
                        getattr(self, "otp_resend_wait_timeout", 90)
                        if resend_timeout is None
                        else resend_timeout
                    ),
                )
            except (TypeError, ValueError):
                second_timeout = int(
                    getattr(self, "otp_resend_wait_timeout", 90) or 90
                )

        self._last_otp_resend_error = ""

        def wait_once(wait_timeout: int) -> Optional[str]:
            try:
                wait_started = time.monotonic()
                self._checkpoint()
                self._log(
                    f"[验证码] 等待验证码｜邮箱={mask_email_for_log(self.email)} "
                    f"｜来源=注册邮箱｜超时={wait_timeout}s"
                )

                email_id = self.email_info.get("service_id") if self.email_info else None
                code = self.email_service.get_verification_code(
                    email=self.email,
                    email_id=email_id,
                    timeout=wait_timeout,
                    pattern=OTP_CODE_PATTERN,
                    otp_sent_at=self._otp_sent_at,
                )

                if code:
                    self._log(
                        f"[验证码] 验证码已收到｜邮箱={mask_email_for_log(self.email)} "
                        f"｜长度={len(str(code).strip())}"
                        f"｜等待={max(0, int(time.monotonic() - wait_started))}秒"
                        f"｜来源=注册邮箱"
                    )
                    return code

                self._log(
                    f"[验证码] 验证码未收到｜邮箱={mask_email_for_log(self.email)} "
                    f"｜等待={max(0, int(time.monotonic() - wait_started))}秒"
                    f"｜来源=注册邮箱",
                    "warning",
                )
                return None
            except TaskInterruption:
                raise
            except Exception as exc:
                self._log(f"获取验证码失败: {exc}", "warning")
                return None

        code = wait_once(first_timeout)
        if code:
            return code

        if second_timeout > 0:
            self._checkpoint()
            self._otp_resend_count = int(
                getattr(self, "_otp_resend_count", 0) or 0
            ) + 1
            self._log(
                f"[验证码] 首次等待超时，保持当前注册会话补发验证码"
                f"｜重发次数={self._otp_resend_count}｜再次等待={second_timeout}s"
            )
            resend_ok = self._send_verification_code(
                referer="https://auth.openai.com/email-verification",
                record_failure=False,
            )
            if resend_ok:
                self._log(
                    f"[验证码] 验证码已补发｜重发次数={self._otp_resend_count}"
                )
            else:
                self._log(
                    "[验证码] 验证码补发请求失败，继续等待当前会话中的延迟邮件"
                    f"｜原因={self._last_otp_resend_error or 'unknown'}",
                    "warning",
                )
            code = wait_once(second_timeout)
            if code:
                # A transient resend failure must not leak into a successful
                # registration result's protocol metadata.
                if (
                    getattr(self, "_last_protocol_failure", None) is not None
                    and self._last_protocol_failure.stage.startswith("email_otp")
                ):
                    self._last_protocol_failure = None
                return code

        self._log(
            f"[验证码] 补发后仍未收到验证码｜邮箱={mask_email_for_log(self.email)} "
            f"｜重发次数={self._otp_resend_count}",
            "error",
        )
        self._record_failure(
            "email_otp_not_received",
            "email_otp_wait",
            "邮箱验证码等待超时或邮箱源未返回验证码"
            + (
                f"；补发失败: {self._last_otp_resend_error}"
                if self._last_otp_resend_error
                else ""
            ),
            retriable=True,
        )
        return None

    def _validate_verification_code(self, code: str) -> bool:
        """验证验证码"""
        try:
            code_body = json.dumps({"code": str(code or "")}, separators=(",", ":"))

            response = self.session.post(
                OPENAI_API_ENDPOINTS["validate_otp"],
                headers=self._browser_headers(
                    OPENAI_API_ENDPOINTS["validate_otp"],
                    referer="https://auth.openai.com/email-verification",
                    origin="https://auth.openai.com",
                    content_type="application/json",
                ),
                data=code_body,
                timeout=20,
            )

            if response.status_code != 200:
                detail = self._record_http_failure(
                    stage="email_otp_validate",
                    response=response,
                    fallback_code="email_otp_validate_failed",
                    fallback_message="邮箱验证码校验失败",
                )
                self._log(detail, "warning")
                return False

            # 解析响应，存储 continue_url 和 page_type
            try:
                resp_data = response.json()
                if not isinstance(resp_data, dict):
                    raise ValueError("response root is not an object")
                self._otp_continue_url = str(resp_data.get("continue_url") or "")
                self._otp_page_type = _page_type_from_payload(resp_data)
                if not self._otp_page_type:
                    raise ValueError("response.page.type is empty")
            except Exception as exc:
                self._otp_continue_url = ""
                self._otp_page_type = ""
                self._record_failure(
                    "email_otp_invalid_response",
                    "email_otp_validate",
                    f"邮箱验证码校验返回无效状态: {exc}",
                    http_status=200,
                    retriable=True,
                )
                return False
            self._log(
                f"[验证码] 验证码已提交｜长度={len(str(code or '').strip())} "
                f"｜HTTP={response.status_code}｜下一页={self._otp_page_type or '-'}"
            )
            return True

        except TaskInterruption:
            raise
        except Exception as e:
            self._log(f"验证验证码失败: {e}", "error")
            self._record_failure(
                "email_otp_validate_exception",
                "email_otp_validate",
                str(e),
                retriable=True,
            )
            return False

    def _create_user_account(self) -> bool:
        """创建用户账户"""
        try:
            fallback = generate_random_user_info()
            profile_name = str(getattr(self, "profile_name", "") or "").strip()
            profile_birthdate = str(
                getattr(self, "profile_birthdate", "") or ""
            ).strip()
            user_info = {
                "name": profile_name or str(fallback.get("name") or "").strip(),
                "birthdate": profile_birthdate
                or str(fallback.get("birthdate") or "").strip(),
            }
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", user_info["birthdate"]):
                self._record_failure(
                    "profile_birthdate_invalid",
                    "about_you",
                    "注册生日不是 YYYY-MM-DD 格式",
                )
                return False
            self._log(
                f"[注册] about_you 资料已准备｜姓名长度={len(user_info['name'])}"
                f"｜生日={user_info['birthdate']}"
            )
            create_account_body = json.dumps(user_info, separators=(",", ":"))

            # 调 client_auth_session_dump 推进服务器 auth 状态机
            try:
                dump_resp = self.session.get(
                    "https://auth.openai.com/api/accounts/client_auth_session_dump",
                    headers=self._browser_headers(
                        "https://auth.openai.com/api/accounts/client_auth_session_dump",
                        referer="https://auth.openai.com/email-verification",
                    ),
                    timeout=20,
                )
                self._log(f"client_auth_session_dump 状态: {dump_resp.status_code}")
                if int(dump_resp.status_code or 0) < 200 or int(
                    dump_resp.status_code or 0
                ) >= 300:
                    self._record_http_failure(
                        stage="client_auth_session_dump",
                        response=dump_resp,
                        fallback_code="client_auth_session_dump_failed",
                        fallback_message="create_account 前状态推进失败",
                    )
                    return False
            except TaskInterruption:
                raise
            except Exception as e:
                self._log(f"client_auth_session_dump 异常: {e}", "warning")
                self._record_failure(
                    "client_auth_session_dump_exception",
                    "client_auth_session_dump",
                    str(e),
                    retriable=True,
                )
                return False

            create_headers = {
                **self._browser_headers(
                    OPENAI_API_ENDPOINTS["create_account"],
                    referer="https://auth.openai.com/about-you",
                    origin="https://auth.openai.com",
                    content_type="application/json",
                ),
                **_generate_datadog_trace_headers(),
            }
            if self._device_id:
                create_headers["oai-device-id"] = self._device_id

            # create_account 也需要 sentinel token (flow=oauth_create_account)
            ca_sentinel = None
            if self._device_id:
                ca_sentinel = self._check_sentinel(
                    self._device_id,
                    flow="oauth_create_account",
                )
            if not ca_sentinel:
                if getattr(self, "_last_protocol_failure", None) is None:
                    self._record_failure(
                        "create_account_sentinel_unavailable",
                        "about_you",
                        "create_account 未获得有效 Sentinel token",
                        retriable=True,
                    )
                return False
            create_headers["openai-sentinel-token"] = json.dumps(
                {
                    "p": ca_sentinel.p,
                    "t": ca_sentinel.t,
                    "c": ca_sentinel.c,
                    "id": self._device_id,
                    "flow": ca_sentinel.flow,
                },
                separators=(",", ":"),
            )
            self._log(f"create_account Sentinel 已获取: flow={ca_sentinel.flow}")

            response = self.session.post(
                OPENAI_API_ENDPOINTS["create_account"],
                headers=create_headers,
                data=create_account_body,
                timeout=30,
            )

            self._log(f"账户创建状态: {response.status_code}")

            status = int(getattr(response, "status_code", 0) or 0)
            if 200 <= status < 300:
                # The first create_account 2xx is the irreversible signup
                # boundary. Everything after it must be recoverable without
                # submitting signup again, even if the response is malformed.
                self._signup_committed = True

            if not (200 <= status < 300):
                detail = self._record_http_failure(
                    stage="about_you",
                    response=response,
                    fallback_code="create_account_failed",
                    fallback_message="create_account 请求失败",
                )
                self._log(detail, "warning")
                return False

            # 提取 continue_url（ChatGPT Web 流程直接返回 OAuth callback URL）
            try:
                resp_data = response.json()
                if not isinstance(resp_data, dict):
                    raise ValueError("response root is not an object")
                self._create_account_continue_url = str(
                    resp_data.get("continue_url") or ""
                )
                page_type = _page_type_from_payload(resp_data)
                if page_type and page_type not in _EXTERNAL_PAGE_TYPES:
                    raise ValueError(f"unexpected page.type={page_type}")
                if self._create_account_continue_url:
                    self._log(f"create_account continue_url: {self._create_account_continue_url[:100]}...")
                else:
                    raise ValueError("continue_url is empty")
            except Exception as exc:
                self._record_failure(
                    "create_account_invalid_response",
                    "about_you",
                    f"create_account 返回无效状态: {exc}",
                    http_status=status or 200,
                    retriable=False,
                )
                return False
            return True

        except TaskInterruption:
            raise
        except Exception as e:
            self._log(f"创建账户失败: {e}", "error")
            self._record_failure(
                "create_account_exception",
                "about_you",
                str(e),
                retriable=True,
            )
            return False

    def _acquire_codex_callback(self) -> Optional[str]:
        """
        注册完成后，通过 Codex CLI OAuth 完整登录流程获取 callback URL。
        使用新 session，走 authorize → authorize/continue → OTP → callback 流程。
        """
        try:
            from .constants import (
                CODEX_CLIENT_ID, CODEX_REDIRECT_URI, CODEX_SCOPE,
                OPENAI_AUTH, OPENAI_API_ENDPOINTS,
            )
            import urllib.parse

            self._log("开始 Codex CLI 登录流程...")

            # 1. 创建新 HTTP client + session
            login_client = OpenAIHTTPClient(
                proxy_url=self.proxy_url,
                browser_fingerprint=self.browser_fingerprint,
            )
            login_session = login_client.session

            # 2. 生成 Codex CLI OAuth URL (Hydra)
            codex_oauth = generate_oauth_url(
                redirect_uri=CODEX_REDIRECT_URI,
                scope=CODEX_SCOPE,
                client_id=CODEX_CLIENT_ID,
            )
            self._codex_oauth = codex_oauth

            # 3. 访问 authorize URL 获取 device_id + session cookies
            response = login_session.get(codex_oauth.auth_url, timeout=15)
            did = login_session.cookies.get("oai-did")
            self._log(f"Codex login device_id: {did}")
            if not did:
                self._log("Codex login 获取 device_id 失败", "error")
                return None

            # 4. 获取 Sentinel token
            sen_payload = None
            try:
                ua = login_client.default_headers.get("User-Agent", "")
                generator = _SentinelTokenGenerator(did, ua, self.browser_fingerprint)
                sent_p = generator.generate_requirements_token()
                sen_req_body = json.dumps({"p": sent_p, "id": did, "flow": "authorize_continue"}, separators=(",", ":"))

                from .constants import SENTINEL_FRAME_URL
                sen_resp = login_client.post(
                    OPENAI_API_ENDPOINTS["sentinel"],
                    headers={
                        "origin": "https://sentinel.openai.com",
                        "referer": SENTINEL_FRAME_URL,
                        "content-type": "text/plain;charset=UTF-8",
                    },
                    data=sen_req_body,
                )
                if sen_resp.status_code == 200:
                    data = sen_resp.json()
                    turnstile = data.get("turnstile") or {}
                    pow_meta = data.get("proofofwork") or {}
                    if pow_meta.get("required") and pow_meta.get("seed"):
                        sent_p = generator.generate_token(
                            str(pow_meta.get("seed") or ""),
                            str(pow_meta.get("difficulty") or "0"),
                        )
                    t_raw = turnstile.get("dx", "")
                    t_val = ""
                    if t_raw:
                        try:
                            t_val = generator.decrypt_turnstile(t_raw, sent_p)
                        except Exception:
                            pass
                    sen_payload = SentinelPayload(p=sent_p, t=t_val, c=str(data.get("token") or ""), flow="authorize_continue")
                    self._log("Codex login Sentinel 已获取")
            except Exception as e:
                self._log(f"Codex login Sentinel 失败: {e}", "warning")

            # 5. authorize/continue 提交邮箱（登录已有账号）
            signup_body = f'{{"username":{{"value":"{self.email}","kind":"email"}},"screen_hint":"login"}}'
            headers = {
                "referer": "https://auth.openai.com/log-in",
                "accept": "application/json",
                "content-type": "application/json",
            }
            if sen_payload:
                headers["openai-sentinel-token"] = json.dumps({
                    "p": sen_payload.p, "t": sen_payload.t, "c": sen_payload.c,
                    "id": did, "flow": sen_payload.flow,
                }, separators=(",", ":"))

            authorize_started_at = _otp_request_started_at()
            resp = login_session.post(OPENAI_API_ENDPOINTS["signup"], headers=headers, data=signup_body)
            self._log(f"Codex login authorize/continue: {resp.status_code}")
            if resp.status_code != 200:
                self._log(f"Codex login authorize/continue 失败: {resp.text[:200]}", "error")
                return None

            resp_data = resp.json()
            page_type = resp_data.get("page", {}).get("type", "")
            self._log(f"Codex login page_type: {page_type}")

            # 6. 如果需要 OTP，等待第二次验证码
            if page_type == "email_otp_verification":
                self._log("等待第二次验证码...")
                self._otp_sent_at = authorize_started_at
                code = self._get_verification_code()
                if not code:
                    self._log("Codex login 获取验证码失败", "error")
                    return None

                # 验证 OTP
                code_body = f'{{"code":"{code}"}}'
                otp_resp = login_session.post(
                    OPENAI_API_ENDPOINTS["validate_otp"],
                    headers={
                        "referer": "https://auth.openai.com/email-verification",
                        "accept": "application/json",
                        "content-type": "application/json",
                    },
                    data=code_body,
                )
                self._log(f"Codex login OTP 校验: {otp_resp.status_code}")
                if otp_resp.status_code != 200:
                    self._log(f"Codex login OTP 失败: {otp_resp.text[:200]}", "error")
                    return None

                otp_data = otp_resp.json()
                otp_page = otp_data.get("page", {}).get("type", "")
                self._log(f"Codex login OTP -> page_type={otp_page}")

                if otp_page == "add_phone":
                    self._log("Codex CLI 登录仍需 add_phone，无法跳过", "error")
                    return None

            # 7. 需要密码登录
            elif page_type in ("login_password", "create_account_password"):
                self._log(f"Codex login 提交密码...")
                if not self.password:
                    self._log("无密码可用", "error")
                    return None

                # 加载密码页获取 sentinel
                login_session.get(f"{OPENAI_AUTH}/log-in/password", timeout=15)
                pwd_sentinel = None
                try:
                    ua2 = login_client.default_headers.get("User-Agent", "")
                    gen2 = _SentinelTokenGenerator(did, ua2, self.browser_fingerprint)
                    sp2 = gen2.generate_requirements_token()
                    sr2 = json.dumps({"p": sp2, "id": did, "flow": "login_password"}, separators=(",", ":"))
                    from .constants import SENTINEL_FRAME_URL as SF2
                    sr2_resp = login_client.post(
                        OPENAI_API_ENDPOINTS["sentinel"],
                        headers={"origin": "https://sentinel.openai.com", "referer": SF2, "content-type": "text/plain;charset=UTF-8"},
                        data=sr2,
                    )
                    if sr2_resp.status_code == 200:
                        d2 = sr2_resp.json()
                        pm2 = d2.get("proofofwork") or {}
                        if pm2.get("required") and pm2.get("seed"):
                            sp2 = gen2.generate_token(str(pm2.get("seed") or ""), str(pm2.get("difficulty") or "0"))
                        tr2 = (d2.get("turnstile") or {}).get("dx", "")
                        tv2 = ""
                        if tr2:
                            try: tv2 = gen2.decrypt_turnstile(tr2, sp2)
                            except: pass
                        pwd_sentinel = SentinelPayload(p=sp2, t=tv2, c=str(d2.get("token") or ""), flow="login_password")
                        self._log("Codex login 密码 Sentinel 已获取")
                except Exception as e:
                    self._log(f"Codex login 密码 Sentinel 失败: {e}", "warning")

                pwd_headers = {
                    "origin": OPENAI_AUTH,
                    "referer": f"{OPENAI_AUTH}/log-in/password",
                    "accept": "application/json",
                    "content-type": "application/json",
                }
                if did:
                    pwd_headers["oai-device-id"] = did
                if pwd_sentinel:
                    pwd_headers["openai-sentinel-token"] = json.dumps({
                        "p": pwd_sentinel.p, "t": pwd_sentinel.t, "c": pwd_sentinel.c,
                        "id": did, "flow": pwd_sentinel.flow,
                    }, separators=(",", ":"))

                pwd_body = json.dumps({"password": self.password, "username": self.email})
                password_started_at = _otp_request_started_at()
                pwd_resp = login_session.post(OPENAI_API_ENDPOINTS["register"], headers=pwd_headers, data=pwd_body)
                self._log(f"Codex login 密码提交: {pwd_resp.status_code}")
                if pwd_resp.status_code != 200:
                    self._log(f"Codex login 密码失败: {pwd_resp.text[:200]}", "error")
                    return None

                pwd_data = pwd_resp.json()
                pwd_page = pwd_data.get("page", {}).get("type", "")
                self._log(f"Codex login 密码 -> page_type={pwd_page}")

                # 密码后可能需要 OTP
                if pwd_page == "email_otp_verification" or pwd_page == "email_otp_send":
                    if pwd_page == "email_otp_send":
                        otp_send_started_at = _otp_request_started_at()
                        login_session.get(OPENAI_API_ENDPOINTS["send_otp"], headers={
                            "referer": f"{OPENAI_AUTH}/email-verification",
                        }, timeout=15)
                        self._otp_sent_at = otp_send_started_at
                    else:
                        self._otp_sent_at = password_started_at
                    self._log("Codex login: 等待验证码...")
                    code = self._get_verification_code()
                    if not code:
                        self._log("Codex login 获取验证码失败", "error")
                        return None
                    code_body = f'{{"code":"{code}"}}'
                    otp_resp = login_session.post(
                        OPENAI_API_ENDPOINTS["validate_otp"],
                        headers={"referer": f"{OPENAI_AUTH}/email-verification", "accept": "application/json", "content-type": "application/json"},
                        data=code_body,
                    )
                    self._log(f"Codex login OTP: {otp_resp.status_code}")
                    if otp_resp.status_code != 200:
                        self._log(f"Codex login OTP 失败: {otp_resp.text[:200]}", "error")
                        return None
                    otp_data = otp_resp.json()
                    otp_page = otp_data.get("page", {}).get("type", "")
                    self._log(f"Codex login OTP -> page_type={otp_page}")
                    if otp_page == "add_phone":
                        self._log("Codex CLI 登录仍需 add_phone", "error")
                        return None

            # 8. 重新访问 authorize URL 获取回调
            self._log("Codex login: 重新访问 OAuth URL 获取回调...")
            response = login_session.get(codex_oauth.auth_url, allow_redirects=False, timeout=15)
            max_redirects = 10
            current_url = codex_oauth.auth_url
            for i in range(max_redirects):
                if response.status_code not in (301, 302, 303, 307, 308):
                    break
                location = response.headers.get("Location", "")
                if not location:
                    break
                next_url = urllib.parse.urljoin(current_url, location)
                self._log(f"Codex login 重定向 {i+1}: {next_url[:80]}...")
                if "code=" in next_url and "state=" in next_url:
                    self._log("找到 Codex CLI 回调 URL")
                    return next_url
                current_url = next_url
                response = login_session.get(current_url, allow_redirects=False, timeout=15)

            self._log(f"Codex login 最终: status={response.status_code}, url={current_url[:100]}", "warning")
            return None

        except Exception as e:
            self._log(f"Codex CLI 登录流程失败: {e}", "error")
            return None

    def _get_workspace_id(self) -> Optional[str]:
        """获取 Workspace ID"""
        try:
            auth_cookie = self.session.cookies.get("oai-client-auth-session")
            if not auth_cookie:
                self._log("未能获取到授权 Cookie", "error")
                return None

            # 解码 JWT
            import base64
            import json as json_module

            try:
                segments = auth_cookie.split(".")
                if len(segments) < 1:
                    self._log("授权 Cookie 格式错误", "error")
                    return None

                # 解码第一个 segment
                payload = segments[0]
                pad = "=" * ((4 - (len(payload) % 4)) % 4)
                decoded = base64.urlsafe_b64decode((payload + pad).encode("ascii"))
                auth_json = json_module.loads(decoded.decode("utf-8"))

                workspaces = auth_json.get("workspaces") or []
                if not workspaces:
                    self._log("授权 Cookie 里没有 workspace 信息", "error")
                    return None

                workspace_id = str((workspaces[0] or {}).get("id") or "").strip()
                if not workspace_id:
                    self._log("无法解析 workspace_id", "error")
                    return None

                self._log(f"Workspace ID: {workspace_id}")
                return workspace_id

            except Exception as e:
                self._log(f"解析授权 Cookie 失败: {e}", "error")
                return None

        except Exception as e:
            self._log(f"获取 Workspace ID 失败: {e}", "error")
            return None

    def _select_workspace(self, workspace_id: str) -> Optional[str]:
        """选择 Workspace"""
        try:
            select_body = f'{{"workspace_id":"{workspace_id}"}}'

            response = self.session.post(
                OPENAI_API_ENDPOINTS["select_workspace"],
                headers={
                    "referer": "https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
                    "content-type": "application/json",
                },
                data=select_body,
            )

            if response.status_code != 200:
                self._log(f"选择 workspace 失败: {response.status_code}", "error")
                self._log(f"响应: {response.text[:200]}", "warning")
                return None

            continue_url = str((response.json() or {}).get("continue_url") or "").strip()
            if not continue_url:
                self._log("workspace/select 响应里缺少 continue_url", "error")
                return None

            self._log(f"Continue URL: {continue_url[:100]}...")
            return continue_url

        except Exception as e:
            self._log(f"选择 Workspace 失败: {e}", "error")
            return None

    def _follow_redirects(self, start_url: str) -> Optional[str]:
        """跟随重定向链，寻找回调 URL"""
        try:
            current_url = start_url
            max_redirects = 6

            for i in range(max_redirects):
                self._log(f"重定向 {i+1}/{max_redirects}: {current_url[:100]}...")

                response = self.session.get(
                    current_url,
                    allow_redirects=False,
                    timeout=15
                )

                location = response.headers.get("Location") or ""

                # 如果不是重定向状态码，停止
                if response.status_code not in [301, 302, 303, 307, 308]:
                    self._log(f"非重定向状态码: {response.status_code}")
                    break

                if not location:
                    self._log("重定向响应缺少 Location 头")
                    break

                # 构建下一个 URL
                import urllib.parse
                next_url = urllib.parse.urljoin(current_url, location)

                # 检查是否包含回调参数
                if "code=" in next_url and "state=" in next_url:
                    self._log(f"找到回调 URL: {next_url[:100]}...")
                    return next_url

                current_url = next_url

            self._log("未能在重定向链中找到回调 URL", "error")
            return None

        except Exception as e:
            self._log(f"跟随重定向失败: {e}", "error")
            return None

    def _handle_oauth_callback(self, callback_url: str) -> Optional[Dict[str, Any]]:
        """处理 OAuth 回调"""
        try:
            if not self.oauth_start:
                self._log("OAuth 流程未初始化", "error")
                return None

            self._log("处理 OAuth 回调...")
            token_info = self.oauth_manager.handle_callback(
                callback_url=callback_url,
                expected_state=self.oauth_start.state,
                code_verifier=self.oauth_start.code_verifier
            )

            self._log("OAuth 授权成功")
            return token_info

        except Exception as e:
            self._log(f"处理 OAuth 回调失败: {e}", "error")
            return None

    def _follow_protocol_callback(self, callback_url: str) -> bool:
        safe_url = _allowed_protocol_continue_url(callback_url)
        if not safe_url:
            self._record_failure(
                "oauth_callback_invalid_url",
                "oauth_callback",
                "上游 continue_url 不是允许的 OpenAI/ChatGPT HTTPS 地址",
            )
            return False
        try:
            response = self.session.get(
                safe_url,
                headers=self._browser_headers(
                    safe_url,
                    accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    referer="https://auth.openai.com/about-you",
                    navigation=True,
                ),
                allow_redirects=True,
                timeout=30,
            )
            status = int(getattr(response, "status_code", 0) or 0)
            self._log(
                f"OAuth callback 状态: {status or '-'} "
                f"final_host={urlsplit(str(getattr(response, 'url', '') or safe_url)).hostname or '-'}"
            )
            if status < 200 or status >= 400:
                self._record_http_failure(
                    stage="oauth_callback",
                    response=response,
                    fallback_code="oauth_callback_failed",
                    fallback_message="OAuth callback 跳转失败",
                )
                return False
            return True
        except TaskInterruption:
            raise
        except Exception as exc:
            self._record_failure(
                "oauth_callback_exception",
                "oauth_callback",
                str(exc),
                retriable=True,
            )
            return False

    def _capture_chatgpt_web_session(self) -> Optional[dict[str, Any]]:
        from .constants import CHATGPT_APP

        last_status = 0
        last_data: dict[str, Any] = {}
        last_error = ""
        for attempt in range(1, PROTOCOL_SESSION_POLL_ATTEMPTS + 1):
            self._session_poll_attempts = attempt
            self._checkpoint()
            try:
                response = self.session.get(
                    f"{CHATGPT_APP}/api/auth/session",
                    headers=self._browser_headers(
                        f"{CHATGPT_APP}/api/auth/session",
                        referer=f"{CHATGPT_APP}/",
                    ),
                    timeout=20,
                )
                last_status = int(getattr(response, "status_code", 0) or 0)
                if last_status == 200:
                    payload = response.json()
                    last_data = payload if isinstance(payload, dict) else {}
                    material, missing = self._web_session_material(last_data)
                    self._last_web_session_material = dict(material)
                    self._log(
                        f"Web Session 轮询: attempt={attempt}/{PROTOCOL_SESSION_POLL_ATTEMPTS} "
                        f"HTTP=200 missing={','.join(missing) or '-'}"
                    )
                    if not missing:
                        return material
                    last_error = f"missing={','.join(missing)}"
                else:
                    _, last_error = _response_error(response)
                    self._log(
                        f"Web Session 轮询: attempt={attempt}/{PROTOCOL_SESSION_POLL_ATTEMPTS} "
                        f"HTTP={last_status or '-'}"
                    )
            except TaskInterruption:
                raise
            except Exception as exc:
                last_error = str(exc)
                self._log(
                    f"Web Session 轮询异常: attempt={attempt}/{PROTOCOL_SESSION_POLL_ATTEMPTS} "
                    f"{exc}",
                    "warning",
                )
            if attempt < PROTOCOL_SESSION_POLL_ATTEMPTS:
                try:
                    self.session.get(
                        f"{CHATGPT_APP}/",
                        headers=self._browser_headers(
                            f"{CHATGPT_APP}/",
                            accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                            navigation=True,
                        ),
                        timeout=20,
                    )
                except TaskInterruption:
                    raise
                except Exception:
                    pass
                time.sleep(0.4 * attempt)

        self._record_failure(
            "web_session_incomplete",
            "web_session",
            last_error or f"session API 未返回完整材料 keys={sorted(last_data.keys())}",
            http_status=last_status,
            retriable=bool(last_status in {0, 408, 425, 429} or last_status >= 500),
        )
        if not self._last_web_session_material:
            self._last_web_session_material, _ = self._web_session_material(last_data)
        return None

    def run(self) -> RegistrationResult:
        """Run the maintained curl-only ChatGPT signup and Web Session flow."""

        if self.capture_codex_oauth:
            # No production caller enables this. Preserve the vendored legacy
            # branch for direct integrations without coupling it to signup.
            return self._run_legacy_codex_flow()

        result = RegistrationResult(success=False, logs=self.logs)
        self._last_protocol_failure = None
        self._session_poll_attempts = 0
        self._signup_committed = False
        try:
            self._set_stage("ip_check")
            self._log("[阶段] protocol stage=ip_check status=started")
            ip_ok, location = self._check_ip_location()
            if not ip_ok and location in {"CN", "HK", "MO", "TW"}:
                result.error_message = self._record_failure(
                    "restricted_proxy_country",
                    "ip_check",
                    f"IP 地理位置不支持: {location}",
                )
                return result
            if not ip_ok:
                self._log(
                    f"IP 检查不可用(location={location})，继续协议注册",
                    "warning",
                )

            self._set_stage("mailbox")
            if not self._create_email():
                result.error_message = self._failure_or(
                    "mailbox_claim_failed",
                    "mailbox",
                    "创建或复用注册邮箱失败",
                    retriable=True,
                )
                return result
            result.email = str(self.email or "")

            self._set_stage("session_init")
            if not self._init_session():
                result.error_message = self._failure_or(
                    "protocol_session_init_failed",
                    "session_init",
                    "初始化 curl_cffi 会话失败",
                    retriable=True,
                )
                return result

            self._set_stage("oauth_start")
            if not self._start_oauth():
                result.error_message = self._record_failure(
                    "oauth_start_failed",
                    "oauth_start",
                    self._last_oauth_error or "ChatGPT NextAuth OAuth 启动失败",
                    retriable=True,
                )
                return result

            self._set_stage("authorize")
            did = self._get_device_id()
            if not did:
                result.error_message = self._record_failure(
                    "device_id_missing",
                    "authorize",
                    "OAuth authorize 后未获得 oai-did",
                    retriable=True,
                )
                return result
            self._device_id = did

            self._set_stage("sentinel")
            signup_sentinel = self._check_sentinel(
                did,
                flow="authorize_continue",
            )
            if not signup_sentinel:
                result.error_message = self._failure_or(
                    "authorize_sentinel_unavailable",
                    "sentinel",
                    "authorize/continue 未获得有效 Sentinel token",
                    retriable=True,
                )
                return result

            self._set_stage("authorize_continue")
            signup_result = self._submit_signup_form(did, signup_sentinel)
            if not signup_result.success:
                result.error_message = signup_result.error_message or self._failure_or(
                    "authorize_continue_failed",
                    "authorize_continue",
                    "提交注册邮箱失败",
                    retriable=True,
                )
                return result
            if signup_result.is_existing_account:
                result.error_message = self._record_failure(
                    "existing_account_detected",
                    "authorize_continue",
                    "user_already_exists: 注册邮箱被路由到已有账号 OTP 登录",
                )
                return result

            page_type = str(signup_result.page_type or "").strip().lower()
            continue_url = str(
                (signup_result.response_data or {}).get("continue_url") or ""
            )
            if page_type in _PASSWORD_PAGE_TYPES:
                self._set_stage("password")
                password_ok, password = self._register_password()
                if not password_ok:
                    result.error_message = self._failure_or(
                        "password_submit_failed",
                        "password",
                        "注册密码失败",
                        retriable=True,
                    )
                    return result
                result.password = str(password or self.password or "")
                page_type = self._password_page_type
            elif page_type not in (
                _OTP_SEND_PAGE_TYPES
                | _OTP_VERIFY_PAGE_TYPES
                | _ABOUT_YOU_PAGE_TYPES
                | _EXTERNAL_PAGE_TYPES
            ):
                result.error_message = self._record_failure(
                    "authorize_continue_unexpected_state",
                    "authorize_continue",
                    f"authorize/continue 返回未知 page.type={page_type or '-'}",
                    retriable=True,
                )
                return result

            if page_type in _OTP_SEND_PAGE_TYPES:
                self._set_stage("email_otp_send")
                if not self._send_verification_code():
                    result.error_message = self._failure_or(
                        "email_otp_send_failed",
                        "email_otp_send",
                        "发送邮箱验证码失败",
                        retriable=True,
                    )
                    return result
                page_type = "email_otp_verification"

            if page_type in _OTP_VERIFY_PAGE_TYPES:
                self._set_stage("email_otp_wait")
                code = self._get_verification_code(
                    timeout=self.otp_wait_timeout,
                    resend_timeout=self.otp_resend_wait_timeout,
                    resend=True,
                )
                if not code:
                    result.error_message = self._failure_or(
                        "email_otp_not_received",
                        "email_otp_wait",
                        "获取邮箱验证码失败",
                        retriable=True,
                    )
                    return result
                self._set_stage("email_otp_validate")
                if not self._validate_verification_code(code):
                    result.error_message = self._failure_or(
                        "email_otp_validate_failed",
                        "email_otp_validate",
                        "验证邮箱验证码失败",
                        retriable=True,
                    )
                    return result
                page_type = str(self._otp_page_type or "").strip().lower()
                continue_url = str(self._otp_continue_url or continue_url or "")

            if page_type == "add_phone":
                result.error_message = self._record_failure(
                    "add_phone_required",
                    "about_you",
                    "协议邮箱注册被上游要求手机号验证",
                )
                return result

            if page_type in _ABOUT_YOU_PAGE_TYPES:
                self._set_stage("client_auth_session_dump")
                if not self._create_user_account():
                    if self._signup_committed:
                        return self._pending_result(
                            result,
                            fallback_message="create_account 已提交但响应状态待补抓",
                        )
                    result.error_message = self._failure_or(
                        "create_account_failed",
                        "about_you",
                        "创建 OpenAI 账号失败",
                    )
                    return result
                continue_url = str(self._create_account_continue_url or "")
                page_type = "external_url"
            elif page_type not in _EXTERNAL_PAGE_TYPES:
                result.error_message = self._record_failure(
                    "email_otp_unexpected_state",
                    "email_otp_validate",
                    f"邮箱验证码校验后返回未知 page.type={page_type or '-'}",
                    retriable=True,
                )
                return result

            self._set_stage("oauth_callback")
            if not self._follow_protocol_callback(continue_url):
                if self._signup_committed:
                    return self._pending_result(
                        result,
                        fallback_message="OAuth callback 失败，开户结果待补抓",
                    )
                result.error_message = self._failure_or(
                    "oauth_callback_failed",
                    "oauth_callback",
                    "OAuth callback 跟随失败",
                    retriable=True,
                )
                return result

            self._set_stage("web_session")
            web_session = self._capture_chatgpt_web_session()
            if not web_session:
                if self._signup_committed:
                    return self._pending_result(
                        result,
                        fallback_message="ChatGPT Web Session 材料待补抓",
                    )
                result.error_message = self._failure_or(
                    "web_session_incomplete",
                    "web_session",
                    "ChatGPT Web Session 材料不完整",
                    retriable=True,
                )
                return result

            result.password = str(self.password or result.password or "")
            result.account_id = str(web_session.get("account_id") or "")
            result.workspace_id = str(
                web_session.get("workspace_id") or result.account_id
            )
            result.access_token = str(web_session.get("access_token") or "")
            result.refresh_token = ""
            result.id_token = result.access_token
            result.session_token = str(web_session.get("session_token") or "")
            result.source = "register"
            result.success = True
            result.metadata = {
                "email_service": self.email_service.service_type.value,
                "proxy_used": self.proxy_url,
                "registered_at": datetime.now(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
                "is_existing_account": False,
                "registration_session_capture": "chatgpt_api_auth_session",
                "cookies": str(web_session.get("cookie_header") or ""),
                "cookie_header": str(web_session.get("cookie_header") or ""),
                "browser_fingerprint": browser_fingerprint_to_dict(
                    self.browser_fingerprint
                ),
                "registration_profile": {
                    "name": str(self.profile_name or ""),
                    "birthdate": str(self.profile_birthdate or ""),
                },
            }
            self._stage = "completed"
            self._log(
                "[结果] 协议注册完成｜"
                f"AT=是｜Session=是｜Cookie=是｜账号={result.account_id}"
            )
            return result
        except TaskInterruption:
            raise
        except Exception as exc:
            result.error_message = self._record_failure(
                "protocol_unexpected_exception",
                self._stage,
                str(exc),
                retriable=True,
            )
            if self._signup_committed:
                return self._pending_result(
                    result,
                    fallback_message="开户后协议流程异常，认证材料待补抓",
                )
            self._log(result.error_message, "error")
            return result
        finally:
            self._attach_protocol_metadata(result)
            try:
                self.http_client.close()
            except Exception:
                pass

    def _run_legacy_codex_flow(self) -> RegistrationResult:
        """
        执行完整的注册流程

        支持已注册账号自动登录：
        - 如果检测到邮箱已注册，自动切换到登录流程
        - 已注册账号跳过：设置密码、发送验证码、创建用户账户
        - 共用步骤：获取验证码、验证验证码、Workspace 和 OAuth 回调

        Returns:
            RegistrationResult: 注册结果
        """
        result = RegistrationResult(success=False, logs=self.logs)

        try:
            self._log("=" * 60)
            self._log("开始注册流程")
            self._log("=" * 60)

            # 1. 检查 IP 地理位置
            self._log("1. 检查 IP 地理位置...")
            ip_ok, location = self._check_ip_location()
            if not ip_ok:
                # Probe network errors return (False, None). Only hard-block known
                # restricted locations; soft-continue when the probe itself fails.
                if location in {"CN", "HK", "MO", "TW"}:
                    result.error_message = f"IP 地理位置不支持: {location}"
                    self._log(f"IP 检查失败: {location}", "error")
                    return result
                self._log(
                    f"IP 检查不可用(location={location})，继续 any-auto 协议注册",
                    "warning",
                )
            else:
                self._log(f"IP 位置: {location}")

            # 2. 创建邮箱
            self._log("2. 创建邮箱...")
            if not self._create_email():
                result.error_message = "创建邮箱失败"
                return result

            result.email = self.email

            # 3. 初始化会话
            self._log("3. 初始化会话...")
            if not self._init_session():
                result.error_message = "初始化会话失败"
                return result

            # 4. 开始 OAuth 流程
            self._log("4. 开始 OAuth 授权流程...")
            if not self._start_oauth():
                result.error_message = self._last_oauth_error or "开始 OAuth 流程失败"
                return result

            # 5. 获取 Device ID
            self._log("5. 获取 Device ID...")
            did = self._get_device_id()
            if not did:
                result.error_message = "获取 Device ID 失败"
                return result

            # 6. 检查 Sentinel 拦截
            self._log("6. 检查 Sentinel 拦截...")
            sen_payload = self._check_sentinel(did)
            if sen_payload:
                self._log("Sentinel 检查通过")
            else:
                self._log("Sentinel 检查失败或未启用", "warning")

            # 7. 提交注册表单 + 解析响应判断账号状态
            self._log("7. 提交注册表单...")
            signup_result = self._submit_signup_form(did, sen_payload)
            if not signup_result.success:
                result.error_message = f"提交注册表单失败: {signup_result.error_message}"
                return result

            # 8. [已注册账号跳过] 注册密码
            if self._is_existing_account:
                self._log("8. [已注册账号] 跳过密码设置，OTP 已自动发送")
            else:
                self._log("8. 注册密码...")
                password_ok, password = self._register_password()
                if not password_ok:
                    result.error_message = "注册密码失败"
                    return result

            # 9. [已注册账号跳过] 发送验证码
            if self._is_existing_account:
                self._log("9. [已注册账号] 跳过发送验证码，使用自动发送的 OTP")
            else:
                self._log("9. 发送验证码...")
                if not self._send_verification_code():
                    result.error_message = "发送验证码失败"
                    return result

            # 10. 获取验证码
            self._log("10. 等待验证码...")
            code = self._get_verification_code()
            if not code:
                result.error_message = "获取验证码失败"
                return result

            # 11. 验证验证码
            self._log("11. 验证验证码...")
            if not self._validate_verification_code(code):
                result.error_message = "验证验证码失败"
                return result

            # 12. 根据 OTP 响应决定下一步
            if self._otp_page_type == "about_you" and not self._is_existing_account:
                # 正常注册流程: about_you → create_account
                self._log("12. 创建用户账户...")
                if not self._create_user_account():
                    result.error_message = "创建用户账户失败"
                    return result
            elif self._is_existing_account:
                self._log("12. [已注册账号] 跳过创建用户账户")
            else:
                self._log(f"12. OTP page_type={self._otp_page_type}，尝试创建账户...")
                if not self._create_user_account():
                    result.error_message = "创建用户账户失败"
                    return result

            # 13. 跟随 callback URL 到 chatgpt.com 获取 session
            callback_url = self._create_account_continue_url
            if not callback_url or "code=" not in str(callback_url):
                result.error_message = "create_account 未返回有效的 callback URL"
                return result

            self._log("13. 跟随 callback URL 到 chatgpt.com...")
            cb_resp = self.session.get(callback_url, timeout=20)
            self._log(f"callback 状态: {cb_resp.status_code}")

            # 提取 session cookie
            session_token = self.session.cookies.get("__Secure-next-auth.session-token")
            account_cookie = self.session.cookies.get("_account", "")
            if session_token:
                self._log(f"获取到 session-token: {session_token[:30]}...")
            if account_cookie:
                self._log(f"获取到 _account: {account_cookie}")

            # 14. 从 chatgpt.com/api/auth/session 获取 access_token
            from .constants import CHATGPT_APP
            self._log("14. 获取 session 信息...")
            session_resp = self.session.get(
                f"{CHATGPT_APP}/api/auth/session",
                headers={"accept": "application/json"},
                timeout=15,
            )
            self._log(f"session API 状态: {session_resp.status_code}")
            self._log(f"session API 响应: {session_resp.text[:500]}")

            session_data = session_resp.json()
            access_token = session_data.get("accessToken", "")
            user_data = session_data.get("user", {})
            session_token = session_data.get("sessionToken") or session_token or ""
            self._log(f"session keys: {list(session_data.keys())}")
            self._log(f"accessToken 长度: {len(access_token)}")

            if not access_token:
                result.error_message = "chatgpt.com session 未返回 accessToken"
                return result

            self._log("NextAuth session 获取成功")

            # 15. Codex CLI OTP 登录获取 refresh_token + id_token
            codex_token_info = None
            try:
                if not self.capture_codex_oauth:
                    raise _SkipCodexOAuth()
                self._log("15. Codex CLI OTP 登录...")
                from .constants import (
                    CODEX_CLIENT_ID, CODEX_REDIRECT_URI, CODEX_SCOPE,
                    OPENAI_AUTH, SENTINEL_FRAME_URL,
                )
                import urllib.parse

                codex_oauth = generate_oauth_url(
                    redirect_uri=CODEX_REDIRECT_URI,
                    scope=CODEX_SCOPE,
                    client_id=CODEX_CLIENT_ID,
                )

                # 用全新 session（Hydra 需要干净 session）
                login_client = OpenAIHTTPClient(
                    proxy_url=self.proxy_url,
                    browser_fingerprint=self.browser_fingerprint,
                )
                login_session = login_client.session

                # 访问 Codex OAuth URL，跟随重定向到 /log-in
                login_session.get(codex_oauth.auth_url, timeout=15)
                did2 = login_session.cookies.get("oai-did", "")
                self._log(f"Codex login did: {did2[:20]}...")

                # 获取 sentinel（用 login_client）
                sen2 = None
                try:
                    ua2 = login_client.default_headers.get("User-Agent", "")
                    gen2 = _SentinelTokenGenerator(did2, ua2, self.browser_fingerprint)
                    sp2 = gen2.generate_requirements_token()
                    sr2 = json.dumps({"p": sp2, "id": did2, "flow": "authorize_continue"}, separators=(",", ":"))
                    sr2_resp = login_client.post(
                        OPENAI_API_ENDPOINTS["sentinel"],
                        headers={"origin": "https://sentinel.openai.com", "referer": SENTINEL_FRAME_URL, "content-type": "text/plain;charset=UTF-8"},
                        data=sr2,
                    )
                    if sr2_resp.status_code == 200:
                        d2 = sr2_resp.json()
                        pm2 = d2.get("proofofwork") or {}
                        if pm2.get("required") and pm2.get("seed"):
                            sp2 = gen2.generate_token(str(pm2.get("seed") or ""), str(pm2.get("difficulty") or "0"))
                        tr2 = (d2.get("turnstile") or {}).get("dx", "")
                        tv2 = ""
                        if tr2:
                            try: tv2 = gen2.decrypt_turnstile(tr2, sp2)
                            except: pass
                        sen2 = SentinelPayload(p=sp2, t=tv2, c=str(d2.get("token") or ""), flow="authorize_continue")
                        self._log("Codex sentinel 获取成功")
                except Exception as e:
                    self._log(f"Codex sentinel 失败: {e}", "warning")

                # authorize/continue 提交邮箱（不带 screen_hint，让 codex_cli_simplified_flow 决定）
                signup_headers = {
                    "referer": f"{OPENAI_AUTH}/log-in",
                    "accept": "application/json",
                    "content-type": "application/json",
                }
                if sen2 and did2:
                    signup_headers["openai-sentinel-token"] = json.dumps({
                        "p": sen2.p, "t": sen2.t, "c": sen2.c,
                        "id": did2, "flow": sen2.flow,
                    }, separators=(",", ":"))

                signup_body = json.dumps({"username": {"value": self.email, "kind": "email"}, "screen_hint": "signup"})
                authorize_started_at = _otp_request_started_at()
                signup_resp = login_session.post(
                    OPENAI_API_ENDPOINTS["signup"], headers=signup_headers, data=signup_body
                )
                self._log(f"Codex authorize/continue: {signup_resp.status_code}")
                if signup_resp.status_code != 200:
                    raise RuntimeError(f"authorize/continue 失败: {signup_resp.text[:200]}")

                page_type = signup_resp.json().get("page", {}).get("type", "")
                self._log(f"Codex page_type: {page_type}")

                # 如果返回 email_otp_send 或 email_otp_verification，走 OTP 流程
                if page_type in ("email_otp_send", "email_otp_verification"):
                    # 发送 OTP
                    if page_type == "email_otp_send":
                        otp_send_started_at = _otp_request_started_at()
                        login_session.get(OPENAI_API_ENDPOINTS["send_otp"], headers={
                            "referer": f"{OPENAI_AUTH}/email-verification",
                        }, timeout=15)
                        self._otp_sent_at = otp_send_started_at
                        self._log("Codex OTP 已发送")
                    else:
                        self._otp_sent_at = authorize_started_at

                    # 等待 OTP
                    code = self._get_verification_code()
                    if not code:
                        raise RuntimeError("Codex OTP 获取失败")
                    self._log(f"Codex OTP: {code}")

                    # 验证 OTP
                    otp_resp = login_session.post(
                        OPENAI_API_ENDPOINTS["validate_otp"],
                        headers={
                            "referer": f"{OPENAI_AUTH}/email-verification",
                            "accept": "application/json",
                            "content-type": "application/json",
                        },
                        data=json.dumps({"code": code}),
                    )
                    self._log(f"Codex OTP validate: {otp_resp.status_code}")
                    if otp_resp.status_code != 200:
                        raise RuntimeError(f"Codex OTP 验证失败: {otp_resp.text[:200]}")

                    otp_data = otp_resp.json()
                    otp_page = otp_data.get("page", {}).get("type", "")
                    self._log(f"Codex OTP -> page_type={otp_page}")

                    if otp_page == "add_phone":
                        self._log("Codex CLI 仍需 add_phone，跳过", "warning")
                        raise RuntimeError("add_phone required")

                    # OTP 成功后，重新访问 OAuth URL 获取 callback
                    self._log("Codex: 重新访问 OAuth URL...")
                    resp = login_session.get(codex_oauth.auth_url, allow_redirects=False, timeout=15)
                    codex_callback = None
                    current_url = codex_oauth.auth_url
                    for i in range(15):
                        if resp.status_code not in (301, 302, 303, 307, 308):
                            break
                        location = resp.headers.get("Location", "")
                        if not location:
                            break
                        next_url = urllib.parse.urljoin(current_url, location)
                        self._log(f"Codex 重定向 {i+1}: {next_url[:80]}...")
                        if "code=" in next_url and "state=" in next_url:
                            codex_callback = next_url
                            break
                        current_url = next_url
                        resp = login_session.get(current_url, allow_redirects=False, timeout=15)

                    if codex_callback:
                        self._log("Codex CLI callback 获取成功")
                        token_json = submit_callback_url(
                            callback_url=codex_callback,
                            expected_state=codex_oauth.state,
                            code_verifier=codex_oauth.code_verifier,
                            redirect_uri=CODEX_REDIRECT_URI,
                            client_id=CODEX_CLIENT_ID,
                            proxy_url=self.proxy_url,
                        )
                        codex_token_info = json.loads(token_json)
                        self._log(f"Codex token 成功: keys={list(codex_token_info.keys())}")
                    else:
                        self._log(f"Codex callback 未获取 (status={resp.status_code})", "warning")
                else:
                    self._log(f"Codex 非 OTP 流程 ({page_type})，跳过", "warning")
            except _SkipCodexOAuth:
                self._log("15. GPT 注册模式跳过 Codex OAuth/RT 捕获")
            except Exception as e:
                self._log(f"Codex CLI 登录失败: {e}", "warning")

            # 提取账户信息（优先 Codex token，fallback 到 NextAuth session）
            if codex_token_info and codex_token_info.get("access_token"):
                self._log("使用 Codex CLI token（完整 refresh_token + id_token）")
                result.account_id = (
                    codex_token_info.get("account_id", "")
                    or account_cookie
                    or str((user_data or {}).get("id") or "")
                )
                result.access_token = codex_token_info.get("access_token", "")
                result.refresh_token = codex_token_info.get("refresh_token", "")
                result.id_token = codex_token_info.get("id_token", "")
            else:
                self._log("使用 NextAuth session token", "warning")
                result.account_id = account_cookie or str((user_data or {}).get("id") or "")
                result.access_token = access_token
                result.refresh_token = ""
                # access_token JWT 包含 chatgpt_account_id 等同于 id_token 的 claims
                result.id_token = access_token

            result.password = self.password or ""
            result.source = "login" if self._is_existing_account else "register"

            if session_token:
                self.session_token = session_token
                result.session_token = session_token
                self._log(f"获取到 Session Token")

            # 17. 完成
            self._log("=" * 60)
            if self._is_existing_account:
                self._log("登录成功! (已注册账号)")
            else:
                self._log("注册成功!")
            self._log(f"邮箱: {result.email}")
            self._log(f"Account ID: {result.account_id}")
            self._log(f"Workspace ID: {result.workspace_id}")
            self._log("=" * 60)

            result.success = True
            cookie_header = _cookie_header_from_session(self.session)
            result.metadata = {
                "email_service": self.email_service.service_type.value,
                "proxy_used": self.proxy_url,
                "registered_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "is_existing_account": self._is_existing_account,
                "registration_session_capture": "chatgpt_api_auth_session",
                "cookies": cookie_header,
                "cookie_header": cookie_header,
                "browser_fingerprint": browser_fingerprint_to_dict(
                    self.browser_fingerprint
                ),
            }

            return result

        except Exception as e:
            self._log(f"注册过程中发生未预期错误: {e}", "error")
            result.error_message = str(e)
            return result

    def save_to_database(self, result: RegistrationResult) -> bool:
        """
        保存注册结果到数据库

        Args:
            result: 注册结果

        Returns:
            是否保存成功
        """
        if not result.success:
            return False

        return True  # 由 account_manager 统一处理存库
