#!/usr/bin/env python3
"""按 HAR 还原的 ChatGPT 手机号注册测试脚本。

用途：
  1. 提交手机号 + 随机密码；
  2. 触发/验证短信 OTP；
  3. 提交姓名生日创建账号；
  4. 跳回 chatgpt.com，并用 /backend-api/me 做成功验证。

默认不打印 token/cookie/手机号全量，避免日志泄露。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from curl_cffi import requests as curl_requests
except Exception as exc:  # pragma: no cover
    raise SystemExit(f"缺少 curl_cffi：{exc}") from exc

from core.proxy_utils import build_requests_proxy_config  # noqa: E402
from services.chatgpt_core.sentinel_browser import (  # noqa: E402
    get_sentinel_token_via_browser,
)
from services.chatgpt_core.sentinel_batch import (  # noqa: E402
    DEFAULT_FRAME_URL,
    DEFAULT_SDK_URL,
    FlowSpec,
    PlaywrightSentinelProvider,
    SentinelBatchConfig,
)
from services.chatgpt_core.sentinel_token import build_sentinel_token  # noqa: E402
from services.chatgpt_core.utils import (  # noqa: E402
    apply_browser_fingerprint,
    build_browser_headers,
    coerce_browser_fingerprint,
    generate_datadog_trace,
    generate_random_birthday,
    generate_random_name,
    generate_random_password,
)


CHATGPT_BASE = "https://chatgpt.com"
AUTH_BASE = "https://auth.openai.com"


class RegistrationRouteError(RuntimeError):
    """authorize 没有进入注册页，不能继续打 user/register。"""

    def __init__(self, message: str, *, final_url: str = "") -> None:
        super().__init__(message)
        self.final_url = final_url


def normalize_phone(value: str) -> str:
    raw = str(value or "").strip()
    digits = re.sub(r"\D", "", raw)
    if not digits:
        raise ValueError("手机号为空或没有数字")
    return f"+{digits}"


def mask_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", str(phone or ""))
    if len(digits) <= 6:
        return "+***"
    return f"+{digits[:3]}***{digits[-4:]}"


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
    value = re.sub(r"ac_[A-Za-z0-9._-]+", "ac_<redacted>", value)
    value = re.sub(r"state=[^&\\s]+", "state=<redacted>", value)
    value = re.sub(r"code=[^&\\s]+", "code=<redacted>", value)
    value = re.sub(r"(csrfToken|password|token|session|cookie)\"?\\s*[:=]\\s*\"?[^\",}\\s]+", r"\1=<redacted>", value, flags=re.I)
    return value


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


class PhoneSignupTester:
    def __init__(
        self,
        *,
        proxy: str = "",
        browser_mode: str = "protocol",
        verbose: bool = True,
    ) -> None:
        self.proxy = str(proxy or "").strip()
        self.browser_mode = browser_mode or "protocol"
        self.verbose = verbose
        self.fingerprint = coerce_browser_fingerprint()
        self.device_id = self.fingerprint.device_id
        self.session = curl_requests.Session(impersonate=self.fingerprint.impersonate)
        if self.proxy:
            self.session.proxies = build_requests_proxy_config(self.proxy)
        apply_browser_fingerprint(self.session, self.fingerprint)

    def log(self, msg: str) -> None:
        if self.verbose:
            print(msg, flush=True)

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

    def json_or_text(self, resp) -> StepResult:
        text = ""
        try:
            text = resp.text or ""
        except Exception:
            text = ""
        data = None
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
    def cookie_names_for_domain(session, domain_hint: str) -> list[str]:
        names: list[str] = []
        seen: set[str] = set()
        try:
            for cookie in session.cookies.jar:
                domain = str(getattr(cookie, "domain", "") or "")
                name = str(getattr(cookie, "name", "") or "")
                if name and domain_hint in domain and name not in seen:
                    seen.add(name)
                    names.append(name)
        except Exception:
            return []
        return names

    @staticmethod
    def summarize_cookie_names(names: list[str]) -> str:
        important = [
            "login_session",
            "hydra_redirect",
            "oai-client-auth-session",
            "auth-session-minimized",
            "auth-session-minimized-client-checksum",
            "rg_context",
            "iss_context",
            "auth_provider",
            "oai-login-csrf_dev_3772291445",
            "cf_clearance",
            "__cf_bm",
        ]
        found = [name for name in important if name in names]
        extras = [name for name in names if name not in found]
        combined = found + extras[:6]
        return ",".join(combined) if combined else "-"

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
        """优先用浏览器 SDK 生成 Sentinel token；失败再退到纯 HTTP PoW。"""
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
                with PlaywrightSentinelProvider(cfg, device_id=self.device_id) as provider:
                    sentinel_token = provider.get_flow_token(spec)
                    so_token = provider.get_session_observer_token(spec)
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
                log_fn=lambda msg: self.log(f"  Sentinel: {msg}"),
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

    def warm_chatgpt_and_signin(
        self,
        phone: str,
        *,
        screen_hint: str = "login_or_signup",
        prompt: str = "login",
    ) -> AuthRouteResult:
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
            headers=self.headers(
                f"{CHATGPT_BASE}/api/auth/providers",
                referer=f"{CHATGPT_BASE}/",
                fetch_site="same-origin",
            ),
            timeout=30,
        )
        self.log(f"  providers -> {providers.status_code}")

        csrf_resp = self.session.get(
            f"{CHATGPT_BASE}/api/auth/csrf",
            headers=self.headers(
                f"{CHATGPT_BASE}/api/auth/csrf",
                referer=f"{CHATGPT_BASE}/",
                fetch_site="same-origin",
            ),
            timeout=30,
        )
        try:
            csrf = (csrf_resp.json() or {}).get("csrfToken", "")
        except Exception:
            csrf = ""
        if not csrf:
            raise RuntimeError(f"未拿到 CSRF: HTTP {csrf_resp.status_code} {csrf_resp.text[:160]}")
        self.log("  csrf -> ok")

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
            data={
                "callbackUrl": f"{CHATGPT_BASE}/",
                "csrfToken": csrf,
                "json": "true",
            },
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
        if set_cookie_names:
            self.log(f"  authorize set-cookie -> {self.summarize_cookie_names(set_cookie_names)}")
        auth_cookie_names = self.cookie_names_for_domain(self.session, "auth.openai.com")
        if auth_cookie_names:
            self.log(f"  auth cookie jar -> {self.summarize_cookie_names(auth_cookie_names)}")
        if cf_ray:
            self.log(f"  authorize cf-ray -> {cf_ray}")
        if request_id:
            self.log(f"  authorize request-id -> {request_id}")
        return AuthRouteResult(
            final_url=final_url,
            status=status,
            route=route,
            redirects=redirects,
            set_cookie_names=set_cookie_names,
            cf_ray=cf_ray,
            request_id=request_id,
        )

    def ensure_registration_route(
        self,
        auth_route: AuthRouteResult | str,
        *,
        phone: str,
        allow_unknown_route: bool = False,
    ) -> None:
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
            raise RegistrationRouteError(
                "authorize 没有进入注册态，而是落到登录密码页；不能继续调用 user/register，"
                "否则通常会 409 invalid_state。"
                f" 诊断：{diag}",
                final_url=final_url,
            )
        if allow_unknown_route:
            self.log(f"  警告：authorize 最终页不是标准注册页，继续尝试: {diag}")
            return
        raise RegistrationRouteError(
            f"authorize 没进入注册密码页；未触发短信。诊断：{diag}",
            final_url=final_url,
        )

    def register_phone_password(self, phone: str, password: str) -> dict[str, Any]:
        self.log("4/8 提交手机号 + 密码：/api/accounts/user/register ...")
        sentinel, _ = self.get_sentinel_pair(
            "username_password_create",
            page_url=f"{AUTH_BASE}/create-account/password",
        )
        url = f"{AUTH_BASE}/api/accounts/user/register"
        headers = self.headers(
            url,
            referer=f"{AUTH_BASE}/create-account/password",
            origin=AUTH_BASE,
            content_type="application/json",
            fetch_site="same-origin",
            extra={
                "openai-sentinel-token": sentinel,
                "oai-device-id": self.device_id,
                **generate_datadog_trace(),
            },
        )
        resp = self.session.post(
            url,
            headers=headers,
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

    def verify_login_password_for_registration_resume(self, password: str) -> dict[str, Any]:
        """手机号已创建但未完成 OTP/about-you 时，authorize 会落到登录密码页。

        这时不能再打 user/register；应按登录密码页提交 password/verify，
        由服务端把同一个 auth session 推进到 contact_verification。
        """
        self.log("4/8 登录密码页续跑：/api/accounts/password/verify ...")
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
            json={"password": password},
            timeout=45,
            allow_redirects=False,
        )
        result = self.json_or_text(resp)
        page_type = ((result.data or {}).get("page") or {}).get("type", "") if result.data else ""
        continue_url = str((result.data or {}).get("continue_url") or "")
        self.log(f"  password/verify -> {result.status} page={page_type or '-'} continue={short_url(continue_url)}")
        if not result.ok or not result.data:
            raise RuntimeError(f"password/verify 失败: HTTP {result.status} {redact_text(result.text)[:500]}")
        return result.data

    def open_contact_verification_page(self, continue_url: str, *, referer: str) -> dict[str, Any]:
        if not continue_url:
            return {}
        if urlsplit(continue_url).path != "/contact-verification":
            return {}
        self.log("5/8 打开 contact-verification 收码页 ...")
        resp = self.session.get(
            continue_url,
            headers=self.headers(
                continue_url,
                accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                referer=referer,
                navigation=True,
                fetch_site="same-origin",
            ),
            allow_redirects=True,
            timeout=30,
        )
        final_url = str(getattr(resp, "url", "") or "")
        redirects = self.redirect_chain(resp)
        self.log(f"  contact-verification -> {resp.status_code} {short_url(final_url)} route={classify_auth_route(final_url)}")
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
        redirects = self.redirect_chain(resp)
        self.log(f"  phone-otp/send -> {result.status} {short_url(final_url)} route={classify_auth_route(final_url)}")
        if redirects:
            self.log(f"  phone-otp/send redirects -> {' -> '.join(redirects)}")
        if result.ok:
            data = dict(result.data or {})
            data.setdefault("final_url", final_url)
            page_type = ((data.get("page") or {}).get("type") or "")
            if page_type:
                self.log(f"  phone-otp/send page -> {page_type}")
            return data

        # 有些环境 register 成功后已经自动发短信，send 失败不立刻终止。
        self.log(f"  phone-otp/send -> HTTP {result.status}，不终止，继续等验证码")
        self.log(f"  响应: {redact_text(result.text)[:240]}")
        return result.data or {"final_url": final_url}

    def resend_phone_otp(self) -> bool:
        """在 contact-verification 阶段按 Auth 前端接口真实重发短信。"""
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
            raise RuntimeError(f"phone-otp/validate 失败: HTTP {result.status} {result.text[:500]}")
        page_type = ((result.data.get("page") or {}).get("type") or "").strip()
        continue_url = str(result.data.get("continue_url") or "")
        channel = str(((result.data.get("oai-client-auth-session") or {}).get("phone_verification_channel")) or "")
        self.log(f"  validate -> page={page_type or '-'} channel={channel or '-'} continue={short_url(continue_url)}")
        return result.data

    def create_account(self, *, full_name: str, birthdate: str) -> dict[str, Any]:
        self.log("7/8 提交姓名生日：/api/accounts/create_account ...")
        sentinel, so_token = self.get_sentinel_pair(
            "oauth_create_account",
            page_url=f"{AUTH_BASE}/about-you",
            need_so=True,
        )
        url = f"{AUTH_BASE}/api/accounts/create_account"
        extra = {
            "openai-sentinel-token": sentinel,
            "oai-device-id": self.device_id,
            **generate_datadog_trace(),
        }
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
            raise RuntimeError(f"create_account 失败: HTTP {result.status} {result.text[:500]}")
        page_type = ((result.data.get("page") or {}).get("type") or "").strip()
        continue_url = str(result.data.get("continue_url") or "")
        self.log(f"  create_account -> page={page_type or '-'} continue={short_url(continue_url)}")
        return result.data

    def follow_chatgpt_callback_and_verify(self, callback_url: str, phone: str) -> dict[str, Any]:
        if not callback_url:
            raise RuntimeError("create_account 响应没有 callback continue_url")

        self.log("8/8 跟随 chatgpt callback，并验证 /backend-api/me ...")
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
                    "oai-client-version": "prod-har-phone-signup-test",
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
            raise RuntimeError("/backend-api/me 未返回 phone_number，注册结果不符合预期")

        accounts_url = f"{CHATGPT_BASE}/backend-api/accounts/check/v4-2023-04-27"
        accounts_resp = self.session.get(
            accounts_url,
            params={"timezone_offset_min": "-480"},
            headers=self.headers(
                accounts_url,
                accept="application/json",
                referer=f"{CHATGPT_BASE}/",
                fetch_site="same-origin",
                extra={
                    "oai-client-build-number": "7430979",
                    "oai-client-version": "prod-har-phone-signup-test",
                    "oai-device-id": self.device_id,
                    "oai-session-id": str(uuid.uuid4()),
                    "x-openai-target-path": "/backend-api/accounts/check/v4-2023-04-27",
                    "x-openai-target-route": "/backend-api/accounts/check/{version}",
                },
            ),
            timeout=30,
        )
        accounts = self.json_or_text(accounts_resp)
        plan = "-"
        sub = "-"
        if accounts.ok and accounts.data:
            first = next(iter((accounts.data.get("accounts") or {}).values()), {}) or {}
            plan = str(((first.get("account") or {}).get("plan_type")) or "-")
            sub = str(((first.get("entitlement") or {}).get("subscription_plan")) or "-")
        self.log(f"  accounts/check -> HTTP {accounts.status} plan={plan} subscription={sub}")

        return {
            "user_id_present": bool(user_id),
            "phone_present": bool(phone_number),
            "email_is_null": email is None,
            "country": country,
            "org_count": len(orgs),
            "plan_type": plan,
            "subscription_plan": sub,
        }

    def run(
        self,
        *,
        phone: str,
        password: str,
        full_name: str,
        birthdate: str,
        otp: str = "",
        explicit_send: bool = True,
        stop_after_send: bool = False,
        stop_after_authorize: bool = False,
        screen_hint: str = "login_or_signup",
        prompt: str = "login",
        try_signup_hint: bool = True,
        allow_unknown_route: bool = False,
        login_password_resume: bool = True,
    ) -> dict[str, Any]:
        phone = normalize_phone(phone)
        self.log(f"开始手机号注册测试：phone={mask_phone(phone)}")
        self.log(f"指纹：device_id={self.device_id[:8]}... ua=Chrome/{self.fingerprint.chrome_full_version}")

        auth_route = self.warm_chatgpt_and_signin(phone, screen_hint=screen_hint, prompt=prompt)
        if stop_after_authorize:
            self.log("已按参数停在 authorize 后，不提交手机号密码、不触发短信。")
            return {
                "stopped": "after_authorize",
                "route": auth_route.route,
                "status": auth_route.status,
                "final_url": short_url(auth_route.final_url),
                "redirects": auth_route.redirects,
                "set_cookie_names": auth_route.set_cookie_names,
            }

        route_path = urlsplit(auth_route.final_url).path
        if "/create-account/password" not in route_path and try_signup_hint and screen_hint != "signup" and "/log-in/password" in route_path:
            self.log("  检测到登录路径，改用 screen_hint=signup 再试一次 authorize ...")
            retry_route = self.warm_chatgpt_and_signin(phone, screen_hint="signup", prompt=prompt)
            retry_path = urlsplit(retry_route.final_url).path
            if "/create-account/password" in retry_path or not login_password_resume:
                auth_route = retry_route
                route_path = retry_path
            else:
                self.log("  signup hint 仍是登录密码页；按待验证注册账号续跑 password/verify")

        page_type = ""
        send_final_url = ""
        resumed_from_login_password = "/log-in/password" in route_path and login_password_resume

        if resumed_from_login_password:
            verify_data = self.verify_login_password_for_registration_resume(password)
            page_type = ((verify_data.get("page") or {}).get("type") or "").strip()
            continue_url = str(verify_data.get("continue_url") or "")
            opened = self.open_contact_verification_page(continue_url, referer=f"{AUTH_BASE}/log-in/password")
            send_final_url = str(opened.get("final_url") or continue_url)
            sms_sent_inferred = page_type == "contact_verification" or "/contact-verification" in urlsplit(send_final_url).path
        else:
            self.ensure_registration_route(auth_route, phone=phone, allow_unknown_route=allow_unknown_route)
            register_data = self.register_phone_password(phone, password)
            page_type = ((register_data.get("page") or {}).get("type") or "").strip()
            continue_url = str(register_data.get("continue_url") or "")
            send_data = self.maybe_send_phone_otp(continue_url, explicit_send=explicit_send)
            send_final_url = str((send_data or {}).get("final_url") or "")
            sms_sent_inferred = page_type == "phone_otp_send" or "/contact-verification" in urlsplit(send_final_url).path

        if stop_after_send:
            self.log("已按参数停在验证码发送/收码阶段后，不提交验证码。")
            return {
                "stopped": "after_send",
                "phone": mask_phone(phone),
                "sms_sent_inferred": bool(sms_sent_inferred),
                "page_type": page_type or "-",
                "send_final_url": short_url(send_final_url),
                "resumed_from_login_password": bool(resumed_from_login_password),
            }

        while not otp:
            print("", flush=True)
            value = input("请输入收到的 6 位短信验证码；输入 resend/r 重发，quit/q 退出：").strip()
            lowered = value.lower()
            if lowered in {"r", "resend", "retry", "重发", "重新发送"}:
                self.resend_phone_otp()
                continue
            if lowered in {"q", "quit", "exit", "退出"}:
                raise KeyboardInterrupt
            otp = value
        if not re.fullmatch(r"\d{4,8}", otp):
            raise RuntimeError(f"验证码格式不像数字验证码: {otp!r}")

        self.validate_phone_otp(otp)
        create_data = self.create_account(full_name=full_name, birthdate=birthdate)
        callback_url = str(create_data.get("continue_url") or ((create_data.get("page") or {}).get("payload") or {}).get("url") or "")
        summary = self.follow_chatgpt_callback_and_verify(callback_url, phone)
        self.log("完成：手机号账号注册验证通过。")
        return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="按 HAR 还原的 ChatGPT 手机号注册测试脚本",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--phone", help="手机号，建议 E.164，如 +5732xxxxxxx")
    parser.add_argument("--password", help="注册密码；不填则随机生成")
    parser.add_argument("--name", help="about-you 姓名；不填则随机英文名")
    parser.add_argument("--birthdate", help="生日 YYYY-MM-DD；不填则随机成年人生日")
    parser.add_argument("--otp", help="短信验证码；不填则运行中交互输入")
    parser.add_argument("--proxy", default="", help="代理 URL，如 http://user:pass@host:port")
    parser.add_argument(
        "--browser-mode",
        choices=("protocol", "headless", "headed"),
        default="protocol",
        help="Sentinel/节奏模式；headed 会尽量有头跑浏览器 token",
    )
    parser.add_argument(
        "--no-explicit-send",
        action="store_true",
        help="不按 HAR 跟随 GET /api/accounts/phone-otp/send，仅停留在 user/register 返回后",
    )
    parser.add_argument(
        "--stop-after-send",
        action="store_true",
        help="发码后停止，不提交验证码、不创建账号",
    )
    parser.add_argument(
        "--stop-after-authorize",
        action="store_true",
        help="只走到 auth authorize 路由判断，不提交手机号密码、不触发短信",
    )
    parser.add_argument(
        "--screen-hint",
        default="login_or_signup",
        help="传给 /api/auth/signin/openai 的 screen_hint",
    )
    parser.add_argument(
        "--prompt",
        default="login",
        help="传给 /api/auth/signin/openai 的 prompt",
    )
    parser.add_argument(
        "--no-try-signup-hint",
        action="store_true",
        help="authorize 落到登录页时，不自动再试 screen_hint=signup",
    )
    parser.add_argument(
        "--allow-unknown-route",
        action="store_true",
        help="authorize 最终页不是 create-account/password 时也继续尝试 register",
    )
    parser.add_argument(
        "--no-login-password-resume",
        action="store_true",
        help="authorize 落到 /log-in/password 时不按待验证注册账号续跑 password/verify",
    )
    parser.add_argument("--quiet", action="store_true", help="少打印日志")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    phone = args.phone or input("请输入手机号：").strip()
    password = args.password or generate_random_password(16)
    if args.name:
        full_name = args.name.strip()
    else:
        first, last = generate_random_name()
        full_name = f"{first} {last}"
    birthdate = args.birthdate or generate_random_birthday()

    tester = PhoneSignupTester(
        proxy=args.proxy,
        browser_mode=args.browser_mode,
        verbose=not args.quiet,
    )
    try:
        summary = tester.run(
            phone=phone,
            password=password,
            full_name=full_name,
            birthdate=birthdate,
            otp=args.otp or "",
            explicit_send=not args.no_explicit_send,
            stop_after_send=bool(args.stop_after_send),
            stop_after_authorize=bool(args.stop_after_authorize),
            screen_hint=args.screen_hint,
            prompt=args.prompt,
            try_signup_hint=not args.no_try_signup_hint,
            allow_unknown_route=bool(args.allow_unknown_route),
            login_password_resume=not bool(args.no_login_password_resume),
        )
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130
    except Exception as exc:
        print(f"\n失败：{redact_text(str(exc), normalize_phone(phone) if phone else '')}")
        return 1

    print("\n结果摘要：")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("\n注意：注册密码未写入日志；如果需要保存账号，请从运行参数或外部记录里保存。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
