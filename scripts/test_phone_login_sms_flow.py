#!/usr/bin/env python3
"""按 HAR 还原的 ChatGPT 手机号登录短信验证发码脚本。

用途：
  - 已有手机号账号：手机号 -> 密码 -> contact_verification 发码；
  - 可选提交收到的 OTP，验证到下一状态；
  - 不用于全新手机号注册（create-account/user-register）链路。

默认不打印完整手机号、密码、token/cookie。
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("DATABASE_URL", f"sqlite:///{ROOT / 'data/account_manager.db'}")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from curl_cffi import requests as curl_requests
except Exception as exc:  # pragma: no cover
    raise SystemExit(f"缺少 curl_cffi：{exc}") from exc

from core.proxy_utils import build_requests_proxy_config, normalize_proxy_url  # noqa: E402
from services.chatgpt_core.sentinel_token import build_sentinel_token  # noqa: E402
from services.chatgpt_core.utils import (  # noqa: E402
    apply_browser_fingerprint,
    build_browser_headers,
    coerce_browser_fingerprint,
    generate_datadog_trace,
)

CHATGPT_BASE = "https://chatgpt.com"
AUTH_BASE = "https://auth.openai.com"


@dataclass
class FlowResult:
    ok: bool
    stage: str
    classification: str = ""
    sms_sent_inferred: bool = False
    otp_validated: bool = False
    status: int = 0
    page_type: str = ""
    final_url: str = ""
    proxy: str = ""
    error: str = ""


def normalize_phone(value: str) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if not digits:
        raise ValueError("手机号为空或没有数字")
    return f"+{digits}"


def mask_phone(value: str) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) <= 6:
        return "+***"
    return f"+{digits[:3]}***{digits[-4:]}"


def short_url(url: str) -> str:
    if not url:
        return ""
    parts = urlsplit(str(url or ""))
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def redact_text(text: str, *, phone: str = "", password: str = "") -> str:
    value = str(text or "")
    if phone:
        value = value.replace(phone, mask_phone(phone))
        value = value.replace(phone.lstrip("+"), mask_phone(phone))
    if password:
        value = value.replace(password, "<password>")
    value = re.sub(r"ac_[A-Za-z0-9._-]+", "ac_<redacted>", value)
    value = re.sub(r"state=[^&\s]+", "state=<redacted>", value)
    value = re.sub(r"code=[^&\s]+", "code=<redacted>", value)
    value = re.sub(r"(token|session|cookie|csrfToken)\"?\s*[:=]\s*\"?[^\",}\s]+", r"\1=<redacted>", value, flags=re.I)
    return value


def extract_har_credentials(path: str | Path) -> tuple[str, str]:
    """从浏览器 HAR 中提取 login_hint 手机号和 password/verify 密码。"""
    har_path = Path(path).expanduser()
    data = json.loads(har_path.read_text(encoding="utf-8"))
    phone = ""
    password = ""
    for entry in (data.get("log") or {}).get("entries") or []:
        req = entry.get("request") or {}
        url = str(req.get("url") or "")
        if "/api/auth/signin/openai" in url:
            qs = parse_qs(urlsplit(url).query)
            phone = str((qs.get("login_hint") or [""])[0] or phone).strip()
        if "/api/accounts/password/verify" in url:
            text = str(((req.get("postData") or {}).get("text")) or "").strip()
            try:
                password = str((json.loads(text) or {}).get("password") or password)
            except Exception:
                pass
    if not phone or not password:
        raise ValueError("HAR 里没有同时解析到 login_hint 手机号和 password/verify 密码")
    return normalize_phone(phone), password


def iter_pool_proxies(*, limit: int, min_score: float = 0, country_code: str = "") -> list[str]:
    try:
        from core.proxy_pool import proxy_pool

        rows = proxy_pool.get_candidate_records(
            target="chatgpt",
            limit=max(int(limit or 0), 0),
            min_score=float(min_score or 0),
            country_code=str(country_code or "").strip().upper(),
        )
    except Exception:
        rows = []
    proxies: list[str] = []
    seen: set[str] = set()
    for item in rows:
        url = str((item or {}).get("url") or "").strip()
        # 数据库里的 host.docker.internal 是给容器内运行用的；宿主机脚本用 127.0.0.1。
        url = url.replace("host.docker.internal", "127.0.0.1")
        if url and url not in seen:
            seen.add(url)
            proxies.append(url)
    return proxies


class PhoneLoginSmsTester:
    def __init__(self, *, proxy: str = "", verbose: bool = True) -> None:
        self.proxy = str(proxy or "").strip()
        self.verbose = bool(verbose)
        self.fingerprint = coerce_browser_fingerprint()
        self.device_id = self.fingerprint.device_id
        self.session = curl_requests.Session(impersonate=self.fingerprint.impersonate)
        if self.proxy:
            self.session.proxies = build_requests_proxy_config(self.proxy)
        apply_browser_fingerprint(self.session, self.fingerprint)

    def log(self, message: str) -> None:
        if self.verbose:
            print(message, flush=True)

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
        extra: dict[str, str] | None = None,
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
            headed=False,
            extra_headers=extra,
        )

    @staticmethod
    def _json(resp) -> dict[str, Any]:
        try:
            data = resp.json()
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _get_csrf(self) -> str:
        self.log("1/5 预热 chatgpt.com 并获取 CSRF ...")
        try:
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
        except Exception as exc:
            self.log(f"  home 异常，继续尝试 CSRF: {str(exc)[:120]}")

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
        csrf = str((self._json(csrf_resp)).get("csrfToken") or "")
        self.log(f"  csrf -> {csrf_resp.status_code}{' ok' if csrf else ''}")
        return csrf

    def _signin_and_authorize(self, phone: str, csrf: str) -> tuple[bool, str, int]:
        self.log("2/5 提交手机号入口：/api/auth/signin/openai ...")
        signin_url = f"{CHATGPT_BASE}/api/auth/signin/openai"
        params = {
            "prompt": "login",
            "ext-passkey-client-capabilities": "11111",
            "ext-oai-did": self.device_id,
            "auth_session_logging_id": str(uuid.uuid4()),
            "screen_hint": "login_or_signup",
            "login_hint": phone,
        }
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
        data = self._json(resp)
        authorize_url = str(data.get("url") or "")
        self.log(f"  signin/openai -> {resp.status_code}")
        if not authorize_url:
            return False, "", int(resp.status_code or 0)

        self.log("3/5 访问 authorize，确认进入密码页 ...")
        auth = self.session.get(
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
        final_url = str(getattr(auth, "url", "") or "")
        self.log(f"  authorize -> {auth.status_code} {short_url(final_url)}")
        return "/log-in/password" in urlsplit(final_url).path, final_url, int(auth.status_code or 0)

    def _password_verify(self, phone: str, password: str) -> tuple[dict[str, Any], int, str]:
        self.log("4/5 提交密码：/api/accounts/password/verify ...")
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
            return {}, 0, "无法生成 password_verify Sentinel token"

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
        data = self._json(resp)
        page_type = str(((data.get("page") or {}).get("type")) or "")
        continue_url = str(data.get("continue_url") or "")
        self.log(f"  password/verify -> {resp.status_code} page={page_type or '-'} continue={short_url(continue_url)}")
        if int(resp.status_code or 0) >= 400:
            return data, int(resp.status_code or 0), redact_text(resp.text or "", phone=phone, password=password)[:500]
        return data, int(resp.status_code or 0), ""

    def _validate_otp(self, code: str) -> tuple[bool, dict[str, Any], int]:
        self.log("5/5 提交短信验证码：/api/accounts/phone-otp/validate ...")
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
            allow_redirects=False,
        )
        data = self._json(resp)
        page_type = str(((data.get("page") or {}).get("type")) or "")
        self.log(f"  phone-otp/validate -> {resp.status_code} page={page_type or '-'}")
        return 200 <= int(resp.status_code or 0) < 300, data, int(resp.status_code or 0)

    def run(self, *, phone: str, password: str, otp: str = "") -> FlowResult:
        phone = normalize_phone(phone)
        self.log(f"开始手机号登录发码测试：phone={mask_phone(phone)} proxy={self.proxy or 'direct'}")
        self.log(f"指纹：device_id={self.device_id[:8]}... ua=Chrome/{self.fingerprint.chrome_full_version}")

        csrf = self._get_csrf()
        if not csrf:
            return FlowResult(False, "csrf", "session_blocked", proxy=self.proxy)

        ok, final_url, auth_status = self._signin_and_authorize(phone, csrf)
        if not ok:
            classification = "not_login_password"
            path = urlsplit(final_url).path
            if "/create-account/password" in path:
                classification = "signup_path_ready"
            elif auth_status in {403, 429} or "/error" in path:
                classification = "session_blocked"
            return FlowResult(False, "authorize", classification, status=auth_status, final_url=short_url(final_url), proxy=self.proxy)

        data, status, error = self._password_verify(phone, password)
        if error:
            classification = "password_verify_failed"
            lowered = error.lower()
            if status == 401 or "invalid credentials" in lowered or "login failed" in lowered:
                classification = "invalid_password"
            elif status == 429 or "too many" in lowered:
                classification = "rate_limited"
            return FlowResult(False, "password_verify", classification, status=status, proxy=self.proxy, error=error)

        page_type = str(((data.get("page") or {}).get("type")) or "")
        if page_type != "contact_verification":
            return FlowResult(False, "password_verify", "unexpected_page", status=status, page_type=page_type, proxy=self.proxy)

        if otp:
            otp_ok, otp_data, otp_status = self._validate_otp(otp)
            otp_page = str(((otp_data.get("page") or {}).get("type")) or "")
            return FlowResult(
                otp_ok,
                "phone_otp_validate",
                "otp_validated" if otp_ok else "otp_validate_failed",
                sms_sent_inferred=True,
                otp_validated=otp_ok,
                status=otp_status,
                page_type=otp_page,
                proxy=self.proxy,
            )

        return FlowResult(True, "password_verify", "contact_verification", sms_sent_inferred=True, status=status, page_type=page_type, proxy=self.proxy)


def build_proxy_list(args: argparse.Namespace) -> list[str]:
    explicit = normalize_proxy_url(args.proxy)
    if explicit:
        return [explicit]
    mode = str(args.proxy_mode or "pool").strip().lower()
    if mode == "direct":
        return [""]
    if mode == "specified":
        return [""]
    proxies = iter_pool_proxies(limit=args.max_proxies, min_score=args.min_score, country_code=args.proxy_country_code)
    return proxies or [""]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="按 HAR 路径测试已有手机号账号登录短信验证发码",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--phone", help="手机号，E.164 或纯数字，如 +573234744365")
    parser.add_argument("--password", help="该手机号账号密码；不填且有 --har 时从 HAR 提取")
    parser.add_argument("--har", help="从 HAR 提取 phone/password，例如 /root/LLL/auth.openai.com-----发码.har")
    parser.add_argument("--otp", help="可选：收到验证码后直接提交验证")
    parser.add_argument("--proxy", default="", help="指定代理 URL；提供后只用这个代理")
    parser.add_argument("--proxy-mode", choices=("pool", "direct", "specified"), default="pool", help="未指定 --proxy 时的代理选择")
    parser.add_argument("--proxy-country-code", default="", help="代理池国家过滤，例如 US/JP；空为不过滤")
    parser.add_argument("--max-proxies", type=int, default=8, help="代理池最多尝试数量")
    parser.add_argument("--min-score", type=float, default=50, help="代理池最低健康分")
    parser.add_argument("--quiet", action="store_true", help="少打印过程日志")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    phone = str(args.phone or "").strip()
    password = str(args.password or "")

    if args.har:
        har_phone, har_password = extract_har_credentials(args.har)
        phone = phone or har_phone
        password = password or har_password

    if not phone:
        if sys.stdin.isatty():
            phone = input("请输入手机号：").strip()
        else:
            print("缺少 --phone；或提供 --har 自动提取")
            return 2
    if not password:
        if sys.stdin.isatty():
            password = getpass.getpass("请输入账号密码：")
        else:
            print("缺少 --password；或提供 --har 自动提取")
            return 2

    last: FlowResult | None = None
    for index, proxy in enumerate(build_proxy_list(args), start=1):
        if index > 1:
            print(f"\n切换代理重试 {index}/{args.max_proxies} ...", flush=True)
        tester = PhoneLoginSmsTester(proxy=proxy, verbose=not args.quiet)
        try:
            result = tester.run(phone=phone, password=password, otp=args.otp or "")
        except KeyboardInterrupt:
            print("\n已中断。")
            return 130
        except Exception as exc:
            result = FlowResult(False, "exception", "exception", proxy=proxy, error=redact_text(str(exc), phone=phone, password=password)[:500])
        last = result
        if result.ok or result.classification in {"invalid_password", "signup_path_ready", "unexpected_page"}:
            break
        if result.classification not in {"session_blocked", "rate_limited", "password_verify_failed", "exception", "not_login_password"}:
            break

    assert last is not None
    print("\n结果摘要：")
    print(json.dumps(asdict(last), ensure_ascii=False, indent=2))
    if last.sms_sent_inferred and not args.otp:
        print("\n已进入 contact_verification：按该链路可视为短信验证码已触发；未提交验证码。")
    return 0 if last.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
