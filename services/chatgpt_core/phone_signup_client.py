from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import json
import re
import uuid
from typing import Any, Optional
from urllib.parse import urlsplit

from curl_cffi import requests as curl_requests

from core.proxy_utils import build_requests_proxy_config
from services.chatgpt_core.sentinel_browser import get_sentinel_token_via_browser, run_sync_playwright_safely
from services.chatgpt_core.sentinel_batch import (
    DEFAULT_FRAME_URL,
    DEFAULT_SDK_URL,
    FlowSpec,
    PlaywrightSentinelProvider,
    SentinelBatchConfig,
)
from services.chatgpt_core.sentinel_token import build_sentinel_token
from services.chatgpt_core.task_logging import mask_phone_for_log, redact_log_text
from services.chatgpt_core.utils import (
    apply_browser_fingerprint,
    build_browser_headers,
    coerce_browser_fingerprint,
    generate_datadog_trace,
)

CHATGPT_BASE = "https://chatgpt.com"
AUTH_BASE = "https://auth.openai.com"


class PhoneRegistrationRouteError(RuntimeError):
    def __init__(self, message: str, *, final_url: str = "") -> None:
        super().__init__(message)
        self.final_url = final_url


@dataclass
class StepResult:
    ok: bool
    status: int = 0
    data: Optional[dict[str, Any]] = None
    text: str = ""
    url: str = ""


@dataclass
class AuthRouteResult:
    final_url: str
    status: int = 0
    route: str = ""
    redirects: list[str] = field(default_factory=list)
    set_cookie_names: list[str] = field(default_factory=list)
    cf_ray: str = ""
    request_id: str = ""


def normalize_phone(value: str) -> str:
    raw = str(value or "").strip()
    digits = re.sub(r"\D", "", raw)
    if not digits:
        raise ValueError("手机号为空或没有数字")
    return f"+{digits}"


def mask_phone(phone: str) -> str:
    return mask_phone_for_log(phone)


def short_url(url: str) -> str:
    if not url:
        return ""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def classify_auth_route(url: str) -> str:
    path = urlsplit(str(url or "")).path
    if "/create-account/password" in path:
        return "create_account_password"
    if "/log-in/password" in path:
        return "login_password"
    if "/api/accounts/authorize" in path:
        return "authorize_endpoint"
    return path.strip("/") or "unknown"


def redact_text(text: str, phone: str = "") -> str:
    value = str(text or "")
    if phone:
        value = value.replace(phone, mask_phone(phone))
        value = value.replace(phone.lstrip("+"), mask_phone(phone))
    return redact_log_text(value)


class PhoneSignupClient:
    """ChatGPT phone-number signup state machine.

    This is the productionized version of the local HAR/test script: it owns the
    OpenAI auth/signup requests only. Phone acquisition and SMS polling stay in
    ``phone_service`` so phone signup and phone binding share the same input/API
    format.
    """

    def __init__(
        self,
        *,
        proxy: str = "",
        browser_mode: str = "protocol",
        log_fn=None,
        stop_checker=None,
    ) -> None:
        self.proxy = str(proxy or "").strip()
        self.browser_mode = str(browser_mode or "protocol").strip() or "protocol"
        self.log_fn = log_fn or (lambda _msg: None)
        self.stop_checker = stop_checker
        self.fingerprint = coerce_browser_fingerprint()
        self.device_id = self.fingerprint.device_id
        self.session = curl_requests.Session(impersonate=self.fingerprint.impersonate)
        if self.proxy:
            self.session.proxies = build_requests_proxy_config(self.proxy)
        apply_browser_fingerprint(self.session, self.fingerprint)

    def _check_stop(self) -> None:
        if callable(self.stop_checker):
            self.stop_checker()

    def log(self, msg: str, level: str = "debug") -> None:
        try:
            self.log_fn(msg, level)
        except TypeError:
            self.log_fn(msg)

    def headers(
        self,
        url: str,
        *,
        accept: str = "application/json",
        referer: str = "",
        origin: str = "",
        content_type: str = "",
        navigation: bool = False,
        fetch_site: str = "",
        extra: Optional[dict[str, str]] = None,
    ) -> dict[str, str]:
        return build_browser_headers(
            url=url,
            user_agent=self.fingerprint.user_agent,
            sec_ch_ua=self.fingerprint.sec_ch_ua,
            chrome_full_version=self.fingerprint.chrome_full_version,
            sec_ch_platform_version=self.fingerprint.platform_version,
            accept=accept,
            accept_language=self.fingerprint.accept_language,
            referer=referer or None,
            origin=origin or None,
            content_type=content_type or None,
            navigation=navigation,
            fetch_site=fetch_site or None,
            headed=self.browser_mode == "headed",
            extra_headers=extra,
        )

    @staticmethod
    def json_or_text(resp) -> StepResult:
        try:
            text = resp.text or ""
        except Exception:
            text = ""
        try:
            data = resp.json()
        except Exception:
            data = None
        return StepResult(
            ok=200 <= int(resp.status_code or 0) < 300,
            status=int(resp.status_code or 0),
            data=data if isinstance(data, dict) else None,
            text=text,
            url=str(getattr(resp, "url", "") or ""),
        )

    @staticmethod
    def response_header_values(resp, name: str) -> list[str]:
        headers = getattr(resp, "headers", None)
        if headers is None:
            return []
        try:
            values = headers.get_list(name)
            return [str(v) for v in values if v is not None]
        except Exception:
            pass
        try:
            value = headers.get(name)
        except Exception:
            value = ""
        return [str(value)] if value else []

    @classmethod
    def response_header(cls, resp, name: str) -> str:
        values = cls.response_header_values(resp, name)
        return str(values[-1] if values else "")

    @classmethod
    def response_set_cookie_names(cls, resp) -> list[str]:
        names: list[str] = []
        seen: set[str] = set()
        for value in cls.response_header_values(resp, "set-cookie"):
            name = str(value or "").split("=", 1)[0].strip()
            if name and name not in seen:
                seen.add(name)
                names.append(name)
        return names

    @staticmethod
    def redirect_chain(resp) -> list[str]:
        chain: list[str] = []
        for item in list(getattr(resp, "history", []) or []):
            url = str(getattr(item, "url", "") or "")
            if url:
                chain.append(short_url(url))
        final = str(getattr(resp, "url", "") or "")
        if final:
            short = short_url(final)
            if not chain or chain[-1] != short:
                chain.append(short)
        return chain

    def get_sentinel_pair(self, flow: str, *, page_url: str, need_so: bool = False) -> tuple[str, str]:
        self._check_stop()
        self.log(f"  Sentinel: 获取 {flow}{' + so' if need_so else ''} ...")
        sentinel_token = ""
        so_token = ""

        if need_so:
            try:
                spec = FlowSpec(
                    internal_name=flow,
                    alias=flow.replace("_", "-"),
                    page_url=page_url,
                    needs_session_observer_token=True,
                )
                cfg = SentinelBatchConfig(
                    frame_url=DEFAULT_FRAME_URL,
                    sdk_url=DEFAULT_SDK_URL,
                    user_agent=self.fingerprint.user_agent,
                    output_path=Path("/tmp/chatgpt-phone-signup-sentinel.json"),
                    proxy=self.proxy or None,
                    flows=(spec,),
                    headless=self.browser_mode != "headed",
                )
                def _fetch_sentinel_pair_via_browser() -> tuple[str, str]:
                    with PlaywrightSentinelProvider(cfg, device_id=self.device_id) as provider:
                        return provider.get_flow_token(spec), provider.get_session_observer_token(spec)

                sentinel_token, so_token = run_sync_playwright_safely(
                    _fetch_sentinel_pair_via_browser,
                    logger=lambda msg: self.log(f"  Sentinel: {msg}", "debug"),
                    label="Sentinel Browser SO",
                )
                if sentinel_token:
                    self.log("  Sentinel: 浏览器 SDK token 已生成")
                if so_token:
                    self.log("  Sentinel: sessionObserverToken 已生成")
                    return sentinel_token, so_token
            except Exception as exc:
                self.log(f"  Sentinel: 浏览器 SO 获取失败，降级: {exc}")

        try:
            sentinel_token = get_sentinel_token_via_browser(
                flow=flow,
                proxy=self.proxy or None,
                page_url=page_url,
                headless=self.browser_mode != "headed",
                device_id=self.device_id,
                user_agent=self.fingerprint.user_agent,
                sec_ch_ua=self.fingerprint.sec_ch_ua,
                chrome_full_version=self.fingerprint.chrome_full_version,
                accept_language=self.fingerprint.accept_language,
                platform_version=self.fingerprint.platform_version,
                viewport_width=self.fingerprint.viewport_width,
                viewport_height=self.fingerprint.viewport_height,
                log_fn=lambda msg: self.log(f"  Sentinel: {msg}", "debug"),
            ) or sentinel_token
        except Exception as exc:
            self.log(f"  Sentinel: 浏览器 token 获取失败，降级: {exc}")

        if not sentinel_token:
            sentinel_token = build_sentinel_token(
                self.session,
                self.device_id,
                flow=flow,
                user_agent=self.fingerprint.user_agent,
                sec_ch_ua=self.fingerprint.sec_ch_ua,
                impersonate=self.fingerprint.impersonate,
            ) or ""

        if not sentinel_token:
            raise RuntimeError(f"无法生成 Sentinel token: {flow}")

        if need_so and not so_token:
            self.log("  Sentinel: 未拿到 SO token，先只带 openai-sentinel-token 继续")
        else:
            self.log("  Sentinel: token 已生成")
        return sentinel_token, so_token

    def warm_chatgpt_and_signin(self, phone: str, *, screen_hint: str = "login_or_signup", prompt: str = "login") -> AuthRouteResult:
        phone = normalize_phone(phone)
        self._check_stop()
        self.log("1/8 访问 chatgpt.com，获取 provider/csrf ...")
        home = self.session.get(
            f"{CHATGPT_BASE}/",
            headers=self.headers(
                f"{CHATGPT_BASE}/",
                accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                navigation=True,
            ),
            timeout=30,
        )
        self.log(f"  home -> {home.status_code}")

        providers = self.session.get(
            f"{CHATGPT_BASE}/api/auth/providers",
            headers=self.headers(f"{CHATGPT_BASE}/api/auth/providers", referer=f"{CHATGPT_BASE}/", fetch_site="same-origin"),
            timeout=30,
        )
        self.log(f"  providers -> {providers.status_code}")

        csrf_resp = self.session.get(
            f"{CHATGPT_BASE}/api/auth/csrf",
            headers=self.headers(f"{CHATGPT_BASE}/api/auth/csrf", referer=f"{CHATGPT_BASE}/", fetch_site="same-origin"),
            timeout=30,
        )
        try:
            csrf = (csrf_resp.json() or {}).get("csrfToken", "")
        except Exception:
            csrf = ""
        if not csrf:
            raise RuntimeError(f"未拿到 CSRF: HTTP {csrf_resp.status_code} {redact_log_text(csrf_resp.text)[:160]}")
        self.log("  csrf -> ok")

        self._check_stop()
        self.log("2/8 POST /api/auth/signin/openai ...")
        signin_url = f"{CHATGPT_BASE}/api/auth/signin/openai"
        params = {
            "prompt": prompt,
            "ext-passkey-client-capabilities": "11111",
            "ext-oai-did": self.device_id,
            "auth_session_logging_id": str(uuid.uuid4()),
            "screen_hint": screen_hint,
            "login_hint": phone,
        }
        self.log(f"  params -> screen_hint={screen_hint} prompt={prompt}")
        resp = self.session.post(
            signin_url,
            params=params,
            data={"callbackUrl": f"{CHATGPT_BASE}/", "csrfToken": csrf, "json": "true"},
            headers=self.headers(
                signin_url,
                referer=f"{CHATGPT_BASE}/",
                origin=CHATGPT_BASE,
                content_type="application/x-www-form-urlencoded",
                fetch_site="same-origin",
            ),
            timeout=30,
        )
        result = self.json_or_text(resp)
        if not result.ok or not result.data or not result.data.get("url"):
            raise RuntimeError(f"signin/openai 失败: HTTP {result.status} {redact_text(result.text, phone)[:300]}")
        authorize_url = str(result.data["url"])
        self.log(f"  authorize_url -> {short_url(authorize_url)}")

        self._check_stop()
        self.log("3/8 访问 auth.openai.com authorize，建立注册会话 ...")
        auth_resp = self.session.get(
            authorize_url,
            headers=self.headers(
                authorize_url,
                accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                referer=f"{CHATGPT_BASE}/",
                navigation=True,
                fetch_site="cross-site",
            ),
            allow_redirects=True,
            timeout=45,
        )
        final_url = str(getattr(auth_resp, "url", "") or "")
        route = classify_auth_route(final_url)
        status = int(getattr(auth_resp, "status_code", 0) or 0)
        redirects = self.redirect_chain(auth_resp)
        set_cookie_names: list[str] = []
        seen_cookies: set[str] = set()
        for item in list(getattr(auth_resp, "history", []) or []) + [auth_resp]:
            for name in self.response_set_cookie_names(item):
                if name not in seen_cookies:
                    seen_cookies.add(name)
                    set_cookie_names.append(name)
        cf_ray = self.response_header(auth_resp, "cf-ray").strip()
        request_id = self.response_header(auth_resp, "x-request-id").strip()
        self.log(f"  authorize -> {status} {short_url(final_url)} route={route}")
        if redirects:
            self.log(f"  authorize redirects -> {' -> '.join(redirects)}")
        return AuthRouteResult(
            final_url=final_url,
            status=status,
            route=route,
            redirects=redirects,
            set_cookie_names=set_cookie_names,
            cf_ray=cf_ray,
            request_id=request_id,
        )

    def ensure_registration_route(self, auth_route: AuthRouteResult | str, *, phone: str, allow_unknown_route: bool = False) -> None:
        if isinstance(auth_route, AuthRouteResult):
            final_url = auth_route.final_url
            status = auth_route.status
            redirects = auth_route.redirects
        else:
            final_url = str(auth_route or "")
            status = 0
            redirects = []
        final_path = urlsplit(final_url).path
        if "/create-account/password" in final_path:
            return
        diag = f"status={status or '-'} final={short_url(final_url)}"
        if redirects:
            diag += f" redirects={' -> '.join(redirects)}"
        if "/log-in/password" in final_path:
            raise PhoneRegistrationRouteError(
                "authorize 没有进入注册态，而是落到登录密码页；按已注册手机号处理，不继续调用 user/register。"
                f" 诊断：{diag}",
                final_url=final_url,
            )
        if allow_unknown_route:
            self.log(f"  警告：authorize 最终页不是标准注册页，继续尝试: {diag}")
            return
        raise PhoneRegistrationRouteError(f"authorize 没进入注册密码页；未触发短信。诊断：{diag}", final_url=final_url)

    def register_phone_password(self, phone: str, password: str) -> dict[str, Any]:
        phone = normalize_phone(phone)
        self._check_stop()
        self.log("4/8 提交手机号 + 密码：/api/accounts/user/register ...")
        sentinel, _ = self.get_sentinel_pair("username_password_create", page_url=f"{AUTH_BASE}/create-account/password")
        url = f"{AUTH_BASE}/api/accounts/user/register"
        resp = self.session.post(
            url,
            headers=self.headers(
                url,
                referer=f"{AUTH_BASE}/create-account/password",
                origin=AUTH_BASE,
                content_type="application/json",
                fetch_site="same-origin",
                extra={"openai-sentinel-token": sentinel, "oai-device-id": self.device_id, **generate_datadog_trace()},
            ),
            json={"username": phone, "password": password},
            timeout=45,
        )
        result = self.json_or_text(resp)
        if not result.ok or not result.data:
            raise RuntimeError(f"user/register 失败: HTTP {result.status} {redact_text(result.text, phone)[:500]}")
        page_type = ((result.data.get("page") or {}).get("type") or "").strip()
        continue_url = str(result.data.get("continue_url") or "")
        self.log(f"  user/register -> page={page_type or '-'} continue={short_url(continue_url)}")
        return result.data

    def verify_login_password_for_existing_phone(self, password: str) -> dict[str, Any]:
        self._check_stop()
        self.log("4/8 已注册手机号登录：/api/accounts/password/verify ...")
        sentinel = build_sentinel_token(
            self.session,
            self.device_id,
            flow="password_verify",
            user_agent=self.fingerprint.user_agent,
            sec_ch_ua=self.fingerprint.sec_ch_ua,
            impersonate=self.fingerprint.impersonate,
        ) or ""
        self.log(f"  Sentinel password_verify -> {'ok' if sentinel else 'failed'}")
        if not sentinel:
            raise RuntimeError("无法生成 password_verify Sentinel token")

        url = f"{AUTH_BASE}/api/accounts/password/verify"
        resp = self.session.post(
            url,
            headers=self.headers(
                url,
                referer=f"{AUTH_BASE}/log-in/password",
                origin=AUTH_BASE,
                content_type="application/json",
                fetch_site="same-origin",
                extra={
                    "oai-device-id": self.device_id,
                    "openai-sentinel-token": sentinel,
                    **generate_datadog_trace(),
                },
            ),
            json={"password": str(password or "")},
            timeout=45,
            allow_redirects=False,
        )
        result = self.json_or_text(resp)
        page_type = ((result.data or {}).get("page") or {}).get("type", "") if result.data else ""
        continue_url = str((result.data or {}).get("continue_url") or "") if result.data else ""
        self.log(f"  password/verify -> {result.status} page={page_type or '-'} continue={short_url(continue_url)}")
        if not result.ok or not result.data:
            raise RuntimeError(f"password/verify 失败: HTTP {result.status} {redact_text(result.text)[:500]}")
        return result.data

    def open_contact_verification_page(self, continue_url: str, *, referer: str = "") -> dict[str, Any]:
        if not continue_url:
            return {}
        if urlsplit(continue_url).path != "/contact-verification":
            return {}
        self._check_stop()
        self.log("5/8 打开 contact-verification 收码页 ...")
        resp = self.session.get(
            continue_url,
            headers=self.headers(
                continue_url,
                accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                referer=referer or f"{AUTH_BASE}/log-in/password",
                navigation=True,
                fetch_site="same-origin",
            ),
            allow_redirects=True,
            timeout=30,
        )
        final_url = str(getattr(resp, "url", "") or "")
        self.log(f"  contact-verification -> {resp.status_code} {short_url(final_url)} route={classify_auth_route(final_url)}")
        redirects = self.redirect_chain(resp)
        if redirects:
            self.log(f"  contact-verification redirects -> {' -> '.join(redirects)}")
        return {"final_url": final_url}

    def maybe_send_phone_otp(self, continue_url: str, *, explicit_send: bool = True) -> dict[str, Any]:
        if not continue_url:
            self.log("  未返回 phone-otp/send continue_url，跳过发码确认")
            return {}
        if not explicit_send:
            self.log("  按参数跳过 GET phone-otp/send；不按 HAR 跟随发码确认")
            return {}
        path = urlsplit(continue_url).path
        if path != "/api/accounts/phone-otp/send":
            self.log(f"  continue_url 不是 phone-otp/send，跳过: {short_url(continue_url)}")
            return {}
        self._check_stop()
        self.log("5/8 按 HAR 跟随 phone-otp/send，触发/确认短信发送 ...")
        resp = self.session.get(
            continue_url,
            headers=self.headers(
                continue_url,
                accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                referer=f"{AUTH_BASE}/create-account/password",
                navigation=True,
                fetch_site="same-origin",
            ),
            allow_redirects=True,
            timeout=30,
        )
        result = self.json_or_text(resp)
        final_url = str(getattr(resp, "url", "") or "")
        self.log(f"  phone-otp/send -> {result.status} {short_url(final_url)} route={classify_auth_route(final_url)}")
        if result.ok:
            data = dict(result.data or {})
            data.setdefault("final_url", final_url)
            return data
        self.log(f"  phone-otp/send -> HTTP {result.status}，不终止，继续等验证码")
        self.log(f"  响应: {redact_text(result.text)[:240]}")
        return result.data or {"final_url": final_url}

    def resend_phone_otp(self) -> bool:
        self._check_stop()
        self.log("  Resend: POST /api/accounts/phone-otp/resend ...")
        url = f"{AUTH_BASE}/api/accounts/phone-otp/resend"
        resp = self.session.post(
            url,
            headers=self.headers(
                url,
                referer=f"{AUTH_BASE}/contact-verification",
                origin=AUTH_BASE,
                content_type="application/json",
                fetch_site="same-origin",
                extra=generate_datadog_trace(),
            ),
            json={},
            timeout=30,
            allow_redirects=False,
        )
        result = self.json_or_text(resp)
        page_type = ((result.data or {}).get("page") or {}).get("type", "") if result.data else ""
        continue_url = str((result.data or {}).get("continue_url") or "") if result.data else ""
        self.log(f"  resend -> {result.status} page={page_type or '-'} continue={short_url(continue_url)}")
        if result.ok:
            return True
        self.log(f"  resend 响应: {redact_text(result.text)[:300]}")
        return False

    def validate_phone_otp(self, code: str) -> dict[str, Any]:
        self._check_stop()
        self.log("6/8 提交短信验证码：/api/accounts/phone-otp/validate ...")
        url = f"{AUTH_BASE}/api/accounts/phone-otp/validate"
        resp = self.session.post(
            url,
            headers=self.headers(
                url,
                referer=f"{AUTH_BASE}/contact-verification",
                origin=AUTH_BASE,
                content_type="application/json",
                fetch_site="same-origin",
                extra=generate_datadog_trace(),
            ),
            json={"code": str(code).strip()},
            timeout=45,
        )
        result = self.json_or_text(resp)
        if not result.ok or not result.data:
            raise RuntimeError(f"phone-otp/validate 失败: HTTP {result.status} {redact_text(result.text)[:500]}")
        page_type = ((result.data.get("page") or {}).get("type") or "").strip()
        continue_url = str(result.data.get("continue_url") or "")
        channel = str(((result.data.get("oai-client-auth-session") or {}).get("phone_verification_channel")) or "")
        self.log(f"  validate -> page={page_type or '-'} channel={channel or '-'} continue={short_url(continue_url)}")
        return result.data

    def create_account(self, *, full_name: str, birthdate: str) -> dict[str, Any]:
        self._check_stop()
        self.log("7/8 提交姓名生日：/api/accounts/create_account ...")
        sentinel, so_token = self.get_sentinel_pair("oauth_create_account", page_url=f"{AUTH_BASE}/about-you", need_so=True)
        url = f"{AUTH_BASE}/api/accounts/create_account"
        extra = {"openai-sentinel-token": sentinel, "oai-device-id": self.device_id, **generate_datadog_trace()}
        if so_token:
            extra["openai-sentinel-so-token"] = so_token
        resp = self.session.post(
            url,
            headers=self.headers(
                url,
                referer=f"{AUTH_BASE}/about-you",
                origin=AUTH_BASE,
                content_type="application/json",
                fetch_site="same-origin",
                extra=extra,
            ),
            json={"name": full_name, "birthdate": birthdate},
            timeout=45,
        )
        result = self.json_or_text(resp)
        if not result.ok or not result.data:
            raise RuntimeError(f"create_account 失败: HTTP {result.status} {redact_text(result.text)[:500]}")
        page_type = ((result.data.get("page") or {}).get("type") or "").strip()
        continue_url = str(result.data.get("continue_url") or "")
        self.log(f"  create_account -> page={page_type or '-'} continue={short_url(continue_url)}")
        return result.data

    def get_chatgpt_cookie_header(self) -> str:
        pairs = []
        seen = set()
        for cookie in self.session.cookies.jar:
            domain = cookie.domain or ""
            name = cookie.name or ""
            value = cookie.value or ""
            if not name or not value or "chatgpt.com" not in domain:
                continue
            key = (name, value)
            if key in seen:
                continue
            seen.add(key)
            pairs.append(f"{name}={value}")
        return "; ".join(pairs)

    def fetch_chatgpt_session(self) -> tuple[bool, dict[str, Any] | str]:
        url = f"{CHATGPT_BASE}/api/auth/session"
        self._check_stop()
        resp = self.session.get(
            url,
            headers=self.headers(url, accept="application/json", referer=f"{CHATGPT_BASE}/", fetch_site="same-origin"),
            timeout=30,
        )
        if resp.status_code != 200:
            return False, f"/api/auth/session -> HTTP {resp.status_code}"
        try:
            data = resp.json()
        except Exception as exc:
            return False, f"/api/auth/session 返回非 JSON: {exc}"
        access_token = str(data.get("accessToken") or "").strip()
        if not access_token:
            return False, "/api/auth/session 未返回 accessToken"
        return True, data

    def follow_chatgpt_callback_and_capture(self, callback_url: str, phone: str) -> dict[str, Any]:
        phone = normalize_phone(phone)
        if not callback_url:
            raise RuntimeError("create_account 响应没有 callback continue_url")

        self._check_stop()
        self.log("8/8 跟随 chatgpt callback，并读取 session / /backend-api/me ...")
        resp = self.session.get(
            callback_url,
            headers=self.headers(
                callback_url,
                accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                referer=f"{AUTH_BASE}/about-you",
                navigation=True,
                fetch_site="cross-site",
            ),
            allow_redirects=True,
            timeout=45,
        )
        self.log(f"  callback -> {resp.status_code} {short_url(str(getattr(resp, 'url', '') or ''))}")

        session_ok, session_data_or_error = self.fetch_chatgpt_session()
        if not session_ok:
            raise RuntimeError(str(session_data_or_error))
        session_data = dict(session_data_or_error or {})

        me_url = f"{CHATGPT_BASE}/backend-api/me"
        me_resp = self.session.get(
            me_url,
            headers=self.headers(
                me_url,
                accept="application/json",
                referer=f"{CHATGPT_BASE}/",
                fetch_site="same-origin",
                extra={
                    "oai-client-build-number": "7430979",
                    "oai-client-version": "prod-phone-signup",
                    "oai-device-id": self.device_id,
                    "oai-session-id": str(uuid.uuid4()),
                    "x-openai-target-path": "/backend-api/me",
                    "x-openai-target-route": "/backend-api/me",
                },
            ),
            timeout=30,
        )
        me = self.json_or_text(me_resp)
        if not me.ok or not me.data:
            raise RuntimeError(f"/backend-api/me 验证失败: HTTP {me.status} {redact_text(me.text, phone)[:500]}")

        phone_number = str(me.data.get("phone_number") or "")
        email = me.data.get("email")
        country = str(me.data.get("country") or "")
        user_id = str(me.data.get("id") or "")
        orgs = (((me.data.get("orgs") or {}).get("data")) or [])
        self.log(
            "  /me -> "
            f"user_id={'有' if user_id else '无'} "
            f"phone={'有' if phone_number else '无'} "
            f"email={'null' if email is None else '有'} "
            f"country={country or '-'} "
            f"orgs={len(orgs)}"
        )
        if not phone_number:
            self.log("  /me 未返回 phone_number；短信 OTP 与 create_account 已完成，按输入手机号继续保存")

        account = session_data.get("account") if isinstance(session_data.get("account"), dict) else {}
        user = session_data.get("user") if isinstance(session_data.get("user"), dict) else {}
        return {
            "access_token": str(session_data.get("accessToken") or "").strip(),
            "session_token": str(session_data.get("sessionToken") or "").strip(),
            "cookies": self.get_chatgpt_cookie_header(),
            "account_id": str(account.get("id") or "").strip(),
            "user_id": str(user.get("id") or user_id or "").strip(),
            "phone_number": phone_number or phone,
            "me_phone_number_missing": not bool(phone_number),
            "email": email,
            "country": country,
            "org_count": len(orgs),
            "raw_session": session_data,
            "me": me.data,
        }
