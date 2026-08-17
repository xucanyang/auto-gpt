"""
OAuth 客户端模块 - 处理 Codex OAuth 登录流程
"""

import time
import secrets
import uuid
import json
import math
import random
import re
from contextlib import ExitStack
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs, urlencode
from core.proxy_utils import build_requests_proxy_config
from core.task_runtime import TaskInterruption, StopTaskRequested, SkipCurrentAttemptRequested
from services.chatgpt_account_state import is_account_deactivated_message
from services.chatgpt_core.task_logging import mask_phone_for_log, redact_log_text

try:
    from curl_cffi import requests as curl_requests
except ImportError:
    import requests as curl_requests

from .phone_service import create_phone_service
from .utils import (
    FlowState,
    apply_browser_fingerprint,
    build_sec_ch_ua_full_version_list,
    build_browser_headers,
    coerce_browser_fingerprint,
    describe_flow_state,
    extract_flow_state,
    extract_chrome_full_version,
    generate_datadog_trace,
    generate_pkce,
    normalize_flow_url,
    random_delay,
    seed_oai_device_cookie,
)
from .browser_identity import infer_browser_family
from .sentinel_token import build_sentinel_token
from .sentinel_browser import (
    create_account_via_browser,
    export_session_cookies_for_playwright,
    get_sentinel_token_via_browser,
    merge_playwright_cookies_into_session,
)
from .sentinel_constants import PINNED_CHROMIUM_VERSION


OTP_SENT_AT_CLOCK_SKEW_GRACE_SECONDS = 5
OTP_SENT_AT_FALLBACK_GRACE_SECONDS = 60


def _otp_request_started_at() -> float:
    """Anchor OTP mail filtering before the request that can trigger delivery."""

    return time.time() - OTP_SENT_AT_CLOCK_SKEW_GRACE_SECONDS


class OAuthClient:
    """OAuth 客户端 - 用于获取 Access Token 和 Refresh Token"""

    def __init__(self, config, proxy=None, verbose=True, browser_mode="protocol"):
        """
        初始化 OAuth 客户端

        Args:
            config: 配置字典
            proxy: 代理地址
            verbose: 是否输出详细日志
            browser_mode: protocol | headless | headed
        """
        self.config = dict(config or {})
        self.oauth_issuer = self.config.get("oauth_issuer", "https://auth.openai.com")
        self.oauth_client_id = self.config.get(
            "oauth_client_id", "app_EMoamEEZ73f0CkXaXp7hrann"
        )
        self.oauth_redirect_uri = self.config.get(
            "oauth_redirect_uri", "http://localhost:1455/auth/callback"
        )
        self.proxy = proxy
        self.verbose = verbose
        normalized_browser_mode = str(browser_mode or "protocol").strip().lower()
        if normalized_browser_mode not in {"protocol", "headless", "headed"}:
            raise ValueError(f"unsupported ChatGPT executor: {browser_mode}")
        self.browser_mode = normalized_browser_mode
        self.allow_browser = self.browser_mode in {"headless", "headed"}
        self.last_error = ""
        self.last_workspace_id = ""
        self.last_workspace_candidates = []
        self.last_organization_candidates = []
        self.last_organization_continue_url = ""
        self.last_state = FlowState()
        self.browser_fingerprint = None
        self._about_you_existing_account_detected = False
        self._about_you_should_skip_create_account = False
        self.shared_phone_service = self.config.get("_shared_phone_service")
        self.stop_checker = self.config.get("_task_stop_checker")
        self.task_control = self.config.get("_task_control")
        self.task_attempt_id = self.config.get("_task_attempt_id")
        self._phone_challenge_events = []
        self._phone_binding_events = []

        # 创建 session
        self.session = curl_requests.Session()
        if self.proxy:
            self.session.proxies = build_requests_proxy_config(self.proxy)

    def adopt_browser_context(
        self,
        session,
        *,
        device_id: str = "",
        user_agent=None,
        sec_ch_ua=None,
        accept_language=None,
        browser_fingerprint=None,
    ):
        """承接前序注册阶段的 session / cookie / 指纹。"""
        if session is not None:
            self.session = session

        if self.proxy:
            try:
                if not getattr(self.session, "proxies", None):
                    self.session.proxies = build_requests_proxy_config(self.proxy)
            except Exception:
                pass

        effective_device_id = str(device_id or "").strip()
        if browser_fingerprint is not None:
            self.browser_fingerprint = coerce_browser_fingerprint(
                browser_fingerprint,
                device_id=effective_device_id or None,
                user_agent=user_agent,
                sec_ch_ua=sec_ch_ua,
                accept_language=accept_language,
            )
            apply_browser_fingerprint(self.session, self.browser_fingerprint)
            effective_device_id = str(
                getattr(self.browser_fingerprint, "device_id", "") or effective_device_id
            ).strip()
        else:
            header_updates = {}
            if user_agent:
                header_updates["User-Agent"] = user_agent
            if sec_ch_ua:
                header_updates["sec-ch-ua"] = sec_ch_ua
            if accept_language:
                header_updates["Accept-Language"] = accept_language
            if header_updates:
                try:
                    self.session.headers.update(header_updates)
                except Exception:
                    pass

        if effective_device_id:
            seed_oai_device_cookie(self.session, effective_device_id)
            self._log(f"已承接前序注册上下文: device_id={effective_device_id}")

    def _log(self, msg):
        """输出日志"""
        if self.verbose:
            print(f"  [OAuth] {redact_log_text(msg)}")

    def _check_stop(self) -> None:
        if callable(self.stop_checker):
            self.stop_checker()
            return
        if self.task_control is not None:
            self.task_control.checkpoint(attempt_id=self.task_attempt_id)

    def _set_error(self, message):
        self.last_error = redact_log_text(str(message or "").strip())
        if self.last_error:
            self._log(self.last_error)

    def _record_phone_challenge_event(
        self,
        *,
        challenge_type: str,
        status: str,
        phone: str = "",
        masked: str = "",
        source: str = "",
        message: str = "",
        allow_add_phone_verification=None,
        allow_existing_phone_verification=None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        payload = {
            "type": str(challenge_type or "").strip(),
            "challenge_type": str(challenge_type or "").strip(),
            "status": str(status or "").strip(),
            "phone": str(phone or "").strip(),
            "phone_number": str(phone or "").strip(),
            "masked": str(masked or "").strip(),
            "masked_phone": str(masked or "").strip(),
            "source": str(source or "").strip(),
            "message": str(message or "").strip(),
            "seen_at": now,
            "updated_at": now,
            "allow_add_phone_verification": bool(allow_add_phone_verification),
            "allow_existing_phone_verification": bool(allow_existing_phone_verification),
        }
        if not payload["type"] and not payload["status"]:
            return
        if not payload["phone"] and not payload["masked"] and payload["type"] == "add_phone":
            payload["display"] = "未绑定手机号"
        else:
            payload["display"] = payload["phone"] or payload["masked"]
        self._phone_challenge_events.append(payload)

        try:
            from services.chatgpt_core.bound_phone import upsert_chatgpt_phone_challenge

            upsert_chatgpt_phone_challenge(
                account_id=self.config.get("_current_account_id") or self.config.get("account_id") or 0,
                email=self.config.get("_current_account_email") or self.config.get("email") or "",
                challenge_type=payload["type"],
                status=payload["status"],
                phone=payload["phone"],
                masked=payload["masked"],
                source=payload["source"],
                message=payload["message"],
                allow_add_phone_verification=allow_add_phone_verification,
                allow_existing_phone_verification=allow_existing_phone_verification,
                log_fn=self._log,
            )
        except Exception as exc:
            self._log(f"[手机号验证] 记录手机号挑战失败: {exc}")

    def _record_confirmed_phone_binding_event(self, *, entry, email: str = "") -> None:
        """Record a successful add-phone OTP without treating RT as proof.

        Existing accounts are persisted immediately. New registrations have no
        database row at this point, so the same payload is kept for the
        registration engine to attach when it creates the account.
        """
        account_id = self.config.get("_current_account_id") or self.config.get("account_id") or 0
        account_email = (
            str(email or "").strip()
            or str(self.config.get("_current_account_email") or self.config.get("email") or "").strip()
        )
        if not account_id and not account_email:
            return

        try:
            from services.chatgpt_core.bound_phone import record_chatgpt_confirmed_phone_binding

            result = record_chatgpt_confirmed_phone_binding(
                account_id=account_id,
                email=account_email,
                phone=getattr(entry, "phone", ""),
                # Only UploadedPhoneEntry exposes a reusable API URL. Gateway
                # and SMSToMe detail URLs are not fabricated as delivery APIs.
                api_url=getattr(entry, "api_url", ""),
                source_api_url=getattr(entry, "source_api_url", ""),
                raw_line=getattr(entry, "raw_line", ""),
                task_id=self.config.get("_current_task_id") or self.config.get("task_id") or "",
                source="oauth_add_phone",
                flow="add_phone",
                log_fn=self._log,
            )
        except Exception as exc:
            self._log(f"[手机号验证] 生成已确认手机号绑定事件失败: {exc}")
            return

        payload = result.get("phone_binding") if isinstance(result, dict) else None
        if isinstance(payload, dict) and payload.get("phone"):
            self._phone_binding_events.append(dict(payload))

    @staticmethod
    def _coerce_bool(value, *, default=False):
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "on", "y", "是", "开启", "允许", "启用"}:
            return True
        if text in {"0", "false", "no", "off", "n", "否", "关闭", "禁止", "禁用"}:
            return False
        return default

    def _browser_pause(self, low=0.15, high=0.4):
        """在 headed 模式下注入轻微延迟，模拟真实浏览器操作节奏。"""
        if self.browser_mode == "headed":
            random_delay(low, high)

    def _sleep_with_stop(self, wait_seconds) -> None:
        try:
            remaining = max(float(wait_seconds or 0), 0.0)
        except Exception:
            remaining = 0.0
        while remaining > 0:
            self._check_stop()
            chunk = min(1.0, remaining)
            time.sleep(chunk)
            remaining -= chunk
        self._check_stop()

    def _sleep_before_phone_resend(self, wait_seconds) -> None:
        self._sleep_with_stop(wait_seconds)

    def _ensure_oauth_fingerprint(self, user_agent, sec_ch_ua, impersonate, device_id=None):
        accept_language = None
        try:
            accept_language = self.session.headers.get("Accept-Language")
        except Exception:
            accept_language = None

        self.browser_fingerprint = coerce_browser_fingerprint(
            self.browser_fingerprint,
            device_id=device_id,
            user_agent=user_agent,
            sec_ch_ua=sec_ch_ua,
            impersonate=impersonate,
            accept_language=accept_language,
        )
        apply_browser_fingerprint(self.session, self.browser_fingerprint)

        self._log(
            "OAuth 指纹: "
            f"ua={self.browser_fingerprint.user_agent.split('Chrome/')[-1][:24]}..., "
            f"sec-ch-ua={self.browser_fingerprint.sec_ch_ua}, "
            f"impersonate={self.browser_fingerprint.impersonate}, "
            f"device_id={self.browser_fingerprint.device_id}"
        )
        return (
            self.browser_fingerprint.user_agent,
            self.browser_fingerprint.sec_ch_ua,
            self.browser_fingerprint.impersonate,
        )


    @staticmethod
    def _iter_text_fragments(value):
        if isinstance(value, str):
            text = value.strip()
            if text:
                yield text
            return
        if isinstance(value, dict):
            for item in value.values():
                yield from OAuthClient._iter_text_fragments(item)
            return
        if isinstance(value, (list, tuple, set)):
            for item in value:
                yield from OAuthClient._iter_text_fragments(item)

    @classmethod
    def _should_blacklist_phone_failure(cls, detail="", state: FlowState | None = None):
        fragments = [str(detail or "").strip()]
        if state is not None:
            fragments.extend(
                cls._iter_text_fragments(
                    {
                        "page_type": state.page_type,
                        "continue_url": state.continue_url,
                        "current_url": state.current_url,
                        "payload": state.payload,
                        "raw": state.raw,
                    }
                )
            )

        combined = " | ".join(fragment for fragment in fragments if fragment).lower()
        if not combined:
            return False

        if cls._phone_reached_account_limit(detail, state):
            return True

        non_blacklist_markers = (
            "whatsapp",
            "未收到短信验证码",
            "手机号验证码错误",
            "phone-otp/resend",
            "phone-otp/validate 异常",
            "phone-otp/validate 响应不是 json",
            "phone-otp/validate 失败",
            "timeout",
            "timed out",
            "network",
            "connection",
            "proxy",
            "ssl",
            "tls",
            "captcha",
            "too many phone",
            "too many phone numbers",
            "too many verification requests",
            "验证请求过多",
            "接受短信次数过多",
            "session limit",
            "rate limit",
        )
        if any(marker in combined for marker in non_blacklist_markers):
            return False

        blacklist_markers = (
            "phone number is invalid",
            "invalid phone number",
            "invalid phone",
            "phone number invalid",
            "sms verification failed",
            "send sms verification failed",
            "unable to send sms",
            "not a valid mobile number",
            "unsupported phone number",
            "phone number not supported",
            "carrier not supported",
            "电话号码无效",
            "手机号无效",
            "发送短信验证失败",
            "号码无效",
            "号码不支持",
            "手机号不支持",
        )
        return any(marker in combined for marker in blacklist_markers)

    @staticmethod
    def _is_openai_phone_send_rejected(detail=""):
        combined = str(detail or "").strip().lower()
        if not combined:
            return False

        direct_markers = (
            "detected suspicious behavior from phone numbers",
            "suspicious behavior from phone numbers",
            "phone number is invalid",
            "invalid phone number",
            "invalid phone",
            "not a valid mobile number",
            "unsupported phone number",
            "phone number not supported",
            "carrier not supported",
            "unable to send sms",
            "send sms verification failed",
            "sms verification failed",
            "电话号码无效",
            "手机号无效",
            "发送短信验证失败",
            "号码无效",
            "号码不支持",
            "手机号不支持",
        )
        if any(marker in combined for marker in direct_markers):
            return True

        phone_context_markers = ("phone", "sms", "手机号", "号码", "短信")
        delayed_markers = (
            "please try again later",
            "try again later",
            "temporarily unavailable",
            "temporarily unable",
        )
        return any(marker in combined for marker in phone_context_markers) and any(
            marker in combined for marker in delayed_markers
        )

    @classmethod
    def _phone_reached_account_limit(cls, detail="", state: FlowState | None = None):
        fragments = [str(detail or "").strip()]
        if state is not None:
            fragments.extend(
                cls._iter_text_fragments(
                    {
                        "page_type": state.page_type,
                        "continue_url": state.continue_url,
                        "current_url": state.current_url,
                        "payload": state.payload,
                        "raw": state.raw,
                    }
                )
            )
        combined = " | ".join(fragment for fragment in fragments if fragment).lower()
        if not combined:
            return False
        return (
            "maximum number of accounts" in combined
            or "already linked to the maximum" in combined
            or "绑定上限" in combined
            or "账号绑定上限" in combined
            or "phone_max" in combined
            or "phone_number_max" in combined
            or "phone_number_already_linked" in combined
        )

    def _blacklist_phone_if_needed(
        self, phone_service, entry, detail="", state: FlowState | None = None
    ):
        if not entry or not self._should_blacklist_phone_failure(detail, state):
            return False
        try:
            try:
                phone_service.mark_blacklisted(entry.phone, reason=detail)
            except TypeError:
                phone_service.mark_blacklisted(entry.phone)
            self._log(f"已将手机号加入黑名单: {entry.phone}")
            return True
        except Exception as e:
            if self._is_stop_exception(e):
                self._raise_stop(e)
            self._log(f"写入手机号黑名单失败: {e}")
            return False

    def _reject_phone_and_continue(
        self,
        phone_service,
        entry,
        reason,
        *,
        state: FlowState | None = None,
    ) -> None:
        if self._phone_reached_account_limit(reason, state):
            reason = f"手机号已达到 OpenAI 账号绑定上限，需要取新号: {reason}"
            self._log(reason)
        if self._blacklist_phone_if_needed(phone_service, entry, reason, state):
            return
        phone_service.cancel(entry, reason=reason)

    def _is_stop_exception(self, exc: BaseException) -> bool:
        if isinstance(exc, TaskInterruption):
            return True
        return "手动停止" in str(exc or "")

    def _raise_stop(self, exc: BaseException | None = None) -> None:
        if isinstance(exc, TaskInterruption):
            raise exc
        raise StopTaskRequested()

    @staticmethod
    def _looks_like_cloudflare_challenge(
        text: str = "",
        *,
        status_code: int | None = None,
        url: str = "",
    ) -> bool:
        try:
            code = int(status_code or 0)
        except Exception:
            code = 0
        combined = " | ".join(
            fragment
            for fragment in (
                str(text or "")[:2000],
                str(url or "")[:400],
            )
            if fragment
        ).lower()
        if not combined and code not in {403, 429, 503}:
            return False
        markers = (
            "just a moment",
            "cf-chl",
            "challenge-platform",
            "cdn-cgi/challenge",
            "cf-browser-verification",
            "__cf_chl",
            "cloudflare",
        )
        if any(marker in combined for marker in markers):
            return True
        return code == 403 and ("<!doctype html" in combined or "<html" in combined)

    def _has_cookie(self, name: str) -> bool:
        target = str(name or "").strip()
        if not target:
            return False
        try:
            jar = getattr(getattr(self.session, "cookies", None), "jar", None)
            if jar is not None:
                for item in jar:
                    item_name = getattr(item, "name", None) or getattr(item, "key", None)
                    item_value = getattr(item, "value", None)
                    if item_name == target and item_value:
                        return True
        except Exception:
            pass
        try:
            value = self.session.cookies.get(target)
            return bool(value)
        except Exception:
            return False

    def _export_session_cookies_for_playwright(
        self,
        *,
        fallback_domain: str = "auth.openai.com",
    ) -> list[dict[str, object]]:
        return export_session_cookies_for_playwright(
            self.session,
            fallback_domain=fallback_domain,
        )

    def _export_session_cookie_header_for_browser(self) -> str:
        pairs = []
        seen = set()
        for item in self._export_session_cookies_for_playwright():
            name = str(item.get("name") or "").strip()
            value = str(item.get("value") or "").strip()
            if not name or not value or name in seen:
                continue
            seen.add(name)
            pairs.append(f"{name}={value}")
        return "; ".join(pairs)

    def _merge_playwright_cookies_into_session(self, cookies: list[dict[str, object]]) -> int:
        return merge_playwright_cookies_into_session(self.session, cookies)

    def _browser_bootstrap_oauth_session(
        self,
        authorize_url,
        authorize_params,
        *,
        device_id=None,
        user_agent=None,
        sec_ch_ua=None,
    ) -> tuple[str, bool]:
        if not self.allow_browser:
            self._log("browser bootstrap: 纯协议执行器禁止启动 Playwright")
            return "", False
        try:
            from playwright.sync_api import sync_playwright
            from core.browser_runtime import (
                ensure_browser_display_available,
                resolve_browser_headless,
            )
            from core.playwright_proxy import playwright_proxy_context
        except Exception as exc:
            self._log(f"browser bootstrap: Playwright 不可用: {exc}")
            return "", False

        self._check_stop()
        requested_headless = self.browser_mode != "headed"
        headless, reason = resolve_browser_headless(
            requested_headless,
            override_env_names=(),
        )
        ensure_browser_display_available(headless)

        fingerprint = getattr(self, "browser_fingerprint", None)
        effective_user_agent = (
            user_agent
            or (fingerprint.user_agent if fingerprint else None)
            or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{PINNED_CHROMIUM_VERSION} Safari/537.36"
        )
        effective_accept_language = (
            (fingerprint.accept_language if fingerprint else None) or "en-US,en;q=0.9"
        )
        effective_locale = (
            effective_accept_language.split(",", 1)[0].split(";", 1)[0].strip() or "en-US"
        )
        effective_platform_version = str(
            (fingerprint.platform_version if fingerprint else None) or "15.0.0"
        ).strip('"')
        effective_chrome_full = (
            (fingerprint.chrome_full_version if fingerprint else None)
            or extract_chrome_full_version(effective_user_agent)
        )
        effective_viewport_width = int((fingerprint.viewport_width if fingerprint else None) or 1440)
        effective_viewport_height = int((fingerprint.viewport_height if fingerprint else None) or 900)

        extra_http_headers = {"Accept-Language": effective_accept_language}
        family = infer_browser_family(
            effective_user_agent,
            getattr(fingerprint, "impersonate", "") if fingerprint else "",
        )
        if family == "chrome":
            extra_http_headers.update(
                {
                    "sec-ch-ua-mobile": "?0",
                    "sec-ch-ua-platform": (
                        '"macOS"'
                        if "Macintosh" in effective_user_agent
                        else '"Windows"'
                    ),
                    "sec-ch-ua-arch": '"x86"',
                    "sec-ch-ua-bitness": '"64"',
                }
            )
        if family == "chrome" and sec_ch_ua:
            extra_http_headers["sec-ch-ua"] = sec_ch_ua
        if family == "chrome" and effective_chrome_full:
            extra_http_headers["sec-ch-ua-full-version"] = f'"{effective_chrome_full}"'
        if family == "chrome" and effective_platform_version:
            extra_http_headers["sec-ch-ua-platform-version"] = f'"{effective_platform_version}"'
        full_version_list = build_sec_ch_ua_full_version_list(sec_ch_ua, effective_chrome_full)
        if family == "chrome" and full_version_list:
            extra_http_headers["sec-ch-ua-full-version-list"] = full_version_list

        launch_kwargs: dict[str, object] = {
            "headless": headless,
            "args": ["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        }

        encoded_params = urlencode(authorize_params or {}, doseq=True)
        nav_targets = []
        primary_url = str(authorize_url or "").strip()
        if primary_url:
            nav_targets.append(
                primary_url if not encoded_params else f"{primary_url}?{encoded_params}"
            )
        oauth2_url = f"{self.oauth_issuer}/api/oauth/oauth2/auth"
        nav_targets.append(oauth2_url if not encoded_params else f"{oauth2_url}?{encoded_params}")

        cookie_payload = self._export_session_cookies_for_playwright(
            fallback_domain=urlparse(self.oauth_issuer).hostname or "auth.openai.com"
        )
        auth_origin = self.oauth_issuer.rstrip("/") + "/"
        sentinel_origin = "https://sentinel.openai.com/"
        if device_id:
            def _covers_host(item, host: str) -> bool:
                domain = str(item.get("domain") or "").strip().lstrip(".").lower()
                return bool(domain and (host == domain or host.endswith(f".{domain}")))

            if not any(
                str(item.get("name") or "") == "oai-did"
                and _covers_host(item, "auth.openai.com")
                for item in cookie_payload
            ):
                cookie_payload.append(
                    {
                        "name": "oai-did",
                        "value": str(device_id),
                        "url": auth_origin,
                        "secure": True,
                        "sameSite": "Lax",
                    }
                )
            if not any(
                str(item.get("name") or "") == "oai-did"
                and _covers_host(item, "sentinel.openai.com")
                for item in cookie_payload
            ):
                cookie_payload.append(
                    {
                        "name": "oai-did",
                        "value": str(device_id),
                        "url": sentinel_origin,
                        "secure": True,
                        "sameSite": "Lax",
                    }
                )

        self._log(
            "browser bootstrap: 启动浏览器预热 auth.openai.com "
            f"(cookies={len(cookie_payload)}, mode={'headless' if headless else 'headed'}, {reason})"
        )

        final_url = ""
        try:
            with ExitStack() as stack:
                proxy_config = stack.enter_context(
                    playwright_proxy_context(
                        self.proxy,
                        logger=lambda message: self._log(f"browser bootstrap: {message}"),
                    )
                )
                if proxy_config:
                    launch_kwargs["proxy"] = proxy_config
                p = stack.enter_context(sync_playwright())
                browser = p.chromium.launch(**launch_kwargs)
                try:
                    context = browser.new_context(
                        viewport={
                            "width": effective_viewport_width,
                            "height": effective_viewport_height,
                        },
                        user_agent=effective_user_agent,
                        locale=effective_locale,
                        extra_http_headers=extra_http_headers,
                        ignore_https_errors=True,
                    )
                    if cookie_payload:
                        context.add_cookies(cookie_payload)
                    page = context.new_page()
                    page.set_default_timeout(45000)
                    page.set_default_navigation_timeout(45000)

                    for index, target_url in enumerate(nav_targets, start=1):
                        self._check_stop()
                        self._log(
                            "browser bootstrap: "
                            f"goto[{index}/{len(nav_targets)}] -> {target_url[:180]}"
                        )
                        try:
                            page.goto(target_url, wait_until="domcontentloaded", timeout=45000)
                        except Exception as exc:
                            self._log(f"browser bootstrap: goto[{index}] 异常: {exc}")
                        page.wait_for_timeout(1500)

                        for _ in range(15):
                            self._check_stop()
                            final_url = str(page.url or target_url)
                            try:
                                body_hint = (
                                    page.locator("body").inner_text(timeout=1500) or ""
                                )[:240]
                            except Exception:
                                body_hint = ""
                            cookies_now = context.cookies()
                            has_login_session = any(
                                str(cookie.get("name") or "") == "login_session"
                                and str(cookie.get("value") or "").strip()
                                for cookie in cookies_now
                            )
                            if has_login_session:
                                break
                            if not self._looks_like_cloudflare_challenge(
                                body_hint,
                                url=final_url,
                            ):
                                break
                            page.wait_for_timeout(1000)

                        cookies_now = context.cookies()
                        merged = self._merge_playwright_cookies_into_session(cookies_now)
                        has_login_session = any(
                            str(cookie.get("name") or "") == "login_session"
                            and str(cookie.get("value") or "").strip()
                            for cookie in cookies_now
                        )
                        has_cf_clearance = any(
                            str(cookie.get("name") or "") == "cf_clearance"
                            and str(cookie.get("value") or "").strip()
                            for cookie in cookies_now
                        )
                        self._log(
                            "browser bootstrap: "
                            f"goto[{index}] 完成 final_url={final_url[:180]} "
                            f"login_session={'✓' if has_login_session else '✗'} "
                            f"cf_clearance={'✓' if has_cf_clearance else '✗'} "
                            f"merged={merged}"
                        )
                        if has_login_session:
                            return final_url, True
                finally:
                    browser.close()
        except Exception as exc:
            self._log(f"browser bootstrap: 浏览器预热失败: {exc}")
            return final_url, False

        return final_url, self._has_cookie("login_session")

    def _headers(
        self,
        url,
        *,
        user_agent=None,
        sec_ch_ua=None,
        accept,
        referer=None,
        origin=None,
        content_type=None,
        navigation=False,
        fetch_mode=None,
        fetch_dest=None,
        fetch_site=None,
        extra_headers=None,
    ):
        fingerprint = getattr(self, "browser_fingerprint", None)
        accept_language = None
        chrome_full_version = None
        platform_version = None
        try:
            accept_language = (
                (fingerprint.accept_language if fingerprint else None)
                or self.session.headers.get("Accept-Language")
            )
            chrome_full_version = (
                fingerprint.chrome_full_version if fingerprint else None
            )
            platform_version = (
                fingerprint.platform_version if fingerprint else None
            ) or str(self.session.headers.get("sec-ch-ua-platform-version") or "").strip('"')
        except Exception:
            accept_language = None

        return build_browser_headers(
            url=url,
            user_agent=user_agent or (fingerprint.user_agent if fingerprint else "Mozilla/5.0"),
            sec_ch_ua=sec_ch_ua or (fingerprint.sec_ch_ua if fingerprint else None),
            chrome_full_version=chrome_full_version,
            sec_ch_platform_version=platform_version,
            accept=accept,
            accept_language=accept_language or "en-US,en;q=0.9",
            referer=referer,
            origin=origin,
            content_type=content_type,
            navigation=navigation,
            fetch_mode=fetch_mode,
            fetch_dest=fetch_dest,
            fetch_site=fetch_site,
            headed=self.browser_mode == "headed",
            extra_headers=extra_headers,
        )

    def _state_from_url(self, url, method="GET"):
        state = extract_flow_state(
            current_url=normalize_flow_url(url, auth_base=self.oauth_issuer),
            auth_base=self.oauth_issuer,
            default_method=method,
        )
        if method:
            state.method = str(method).upper()
        return state

    def _state_from_payload(self, data, current_url=""):
        return extract_flow_state(
            data=data,
            current_url=current_url,
            auth_base=self.oauth_issuer,
        )

    def _state_signature(self, state: FlowState):
        return (
            state.page_type or "",
            state.method or "",
            state.continue_url or "",
            state.current_url or "",
        )

    def _extract_code_from_state(self, state: FlowState):
        for candidate in (
            state.continue_url,
            state.current_url,
            (state.payload or {}).get("url", ""),
        ):
            code = self._extract_code_from_url(candidate)
            if code:
                return code
        return None

    def _state_is_login_password(self, state: FlowState):
        return state.page_type == "login_password"

    def _state_is_create_account_password(self, state: FlowState):
        target = f"{state.continue_url} {state.current_url}".lower()
        return state.page_type == "create_account_password" or "create-account/password" in target

    def _state_is_email_otp(self, state: FlowState):
        target = f"{state.continue_url} {state.current_url}".lower()
        return (
            state.page_type == "email_otp_verification"
            or "email-verification" in target
            or "email-otp" in target
        )

    def _state_is_add_phone(self, state: FlowState):
        target = f"{state.continue_url} {state.current_url}".lower()
        return state.page_type == "add_phone" or "add-phone" in target

    def _state_is_existing_phone_otp(self, state: FlowState):
        target = f"{state.continue_url} {state.current_url}".lower()
        page_type = str(state.page_type or "").strip().lower()
        return (
            page_type in {
                "phone_otp_select_channel",
                "phone_otp_verification",
                "phone_verification",
            }
            or "phone-otp/select-channel" in target
            or "phone-otp" in target
            or "phone-verification" in target
        )

    def _state_is_about_you(self, state: FlowState):
        target = f"{state.continue_url} {state.current_url}".lower()
        return state.page_type == "about_you" or "about-you" in target

    def _state_requires_navigation(self, state: FlowState):
        method = (state.method or "GET").upper()
        if method != "GET":
            return False
        if (
            state.source == "api"
            and state.current_url
            and state.page_type not in {"login_password", "email_otp_verification"}
        ):
            return True
        if state.page_type == "external_url" and state.continue_url:
            return True
        if state.continue_url and state.continue_url != state.current_url:
            return True
        return False

    @staticmethod
    def _normalize_phone_hint(value):
        text = str(value or "").strip()
        if not text:
            return ""
        digits = re.sub(r"\D", "", text)
        if len(digits) < 8:
            return ""
        if text.startswith("+"):
            return f"+{digits}"
        if text.startswith("00") and len(digits) > 2:
            return f"+{digits[2:]}"
        return f"+{digits}"

    @staticmethod
    def _looks_like_masked_phone(value):
        text = str(value or "").strip()
        if not text:
            return ""
        lowered = text.lower()
        if any(marker in lowered for marker in ("phone", "sms")) or any(ch in text for ch in ("*", "•", "x", "X")):
            if re.search(r"\d", text):
                return text[:120]
        return ""

    def _collect_phone_hints(self, value, *, source="", depth=0):
        if depth > 5:
            return []
        hints = []
        if isinstance(value, dict):
            for key, item in value.items():
                key_text = str(key or "")
                next_source = f"{source}.{key_text}" if source else key_text
                key_lower = key_text.lower()
                if any(marker in key_lower for marker in ("phone", "mobile", "手机号")):
                    phone = self._normalize_phone_hint(item)
                    if phone:
                        hints.append({"phone": phone, "source": next_source, "masked": ""})
                    else:
                        masked = self._looks_like_masked_phone(item)
                        if masked:
                            hints.append({"phone": "", "source": next_source, "masked": masked})
                if isinstance(item, str):
                    stripped = item.strip()
                    if stripped and stripped[0:1] in {"{", "["}:
                        try:
                            parsed = json.loads(stripped)
                        except Exception:
                            parsed = None
                        if parsed is not None:
                            hints.extend(self._collect_phone_hints(parsed, source=next_source, depth=depth + 1))
                    if any(marker in key_lower for marker in ("message", "raw", "payload", "response")):
                        for match in re.findall(r'"phone_number"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"', item):
                            phone = self._normalize_phone_hint(match)
                            if phone:
                                hints.append({"phone": phone, "source": f"{next_source}.phone_number", "masked": ""})
                elif isinstance(item, (dict, list, tuple)):
                    hints.extend(self._collect_phone_hints(item, source=next_source, depth=depth + 1))
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value[:50]):
                hints.extend(self._collect_phone_hints(item, source=f"{source}[{index}]", depth=depth + 1))
        return hints

    def _resolve_existing_phone_hint(self, state: FlowState):
        sources = []
        session_data = self._decode_oauth_session_cookie() or {}
        if session_data:
            sources.append(("oauth_session", session_data))
        if state.raw:
            sources.append(("state.raw", state.raw))
        if state.payload:
            sources.append(("state.payload", state.payload))
        sources.append(("account_config", self.config))

        seen = set()
        masked_candidates = []
        for source, payload in sources:
            for hint in self._collect_phone_hints(payload, source=source):
                phone = str(hint.get("phone") or "").strip()
                masked = str(hint.get("masked") or "").strip()
                hint_source = str(hint.get("source") or source).strip()
                key = (phone, masked, hint_source)
                if key in seen:
                    continue
                seen.add(key)
                if phone:
                    return {"phone": phone, "source": hint_source, "masked": masked}
                if masked:
                    masked_candidates.append({"phone": "", "source": hint_source, "masked": masked})
        return masked_candidates[0] if masked_candidates else {"phone": "", "source": "", "masked": ""}

    def _lookup_existing_phone_pool_record(self, phone):
        phone = self._normalize_phone_hint(phone)
        if not phone:
            return None, "bound_phone_missing", "未读取到完整手机号，无法判断手机号池"
        try:
            from services.chatgpt_core.phone_pool_repository import PhonePoolRepository

            repo = PhonePoolRepository()
            exact = repo.get(phone)
            if exact is not None:
                api_url = str(getattr(exact, "api_url", "") or "").strip()
                if not api_url:
                    return exact, "bound_phone_pool_record_no_api", f"手机号 {phone} 命中手机号池，但 API URL 为空，无法自动收码"
                return exact, "", (
                    f"手机号 {phone} 命中手机号池: record_id={getattr(exact, 'id', 0) or '-'} "
                    f"status={getattr(exact, 'status', '') or '-'} "
                    f"api_host={getattr(exact, 'api_host', '') or '-'}"
                )
            from services.chatgpt_core.phone_pool_repository import _phone_prefix4

            phone_prefix = _phone_prefix4(phone)
            pool_records = repo.list()
            for item in pool_records:
                item_prefix = _phone_prefix4(getattr(item, "phone_e164", "") or "")
                if not item_prefix or item_prefix != phone_prefix:
                    continue
                return None, "bound_phone_prefix_matched_but_exact_missing", f"手机号 {phone} 属于手机号池号段 {phone_prefix}，但池中没有该完整号码，无法自动收码"
            return None, "bound_phone_not_in_pool_prefix", f"手机号 {phone} 非手机号池号段，无法自动验证"
        except Exception as exc:
            return None, "bound_phone_pool_lookup_failed", f"手机号 {phone} 手机号池查询失败: {exc}"

    @staticmethod
    def _existing_phone_matches_pool_segment(record, error_code: str = "") -> bool:
        return bool(record is not None) or str(error_code or "") in {
            "bound_phone_pool_record_no_api",
            "bound_phone_prefix_matched_but_exact_missing",
        }

    def _record_existing_phone_pool_openai_rejected(self, phone: str, reason: str) -> None:
        try:
            from services.chatgpt_core.phone_pool_repository import PhonePoolRepository

            PhonePoolRepository().record_task_status(phone, "openai_rejected", reason=reason)
        except Exception as exc:
            if self._is_stop_exception(exc):
                self._raise_stop(exc)
            self._log(f"[手机号验证] 记录手机号池 OpenAI 拒绝状态失败: {exc}")

    def _record_existing_phone_pool_forward_error(self, phone: str, reason: str) -> None:
        """Keep the number active while retaining a task-level Relay failure."""
        try:
            from services.chatgpt_core.phone_pool_repository import PhonePoolRepository

            PhonePoolRepository().record_task_status(phone, "api_forward_error", reason=reason)
        except Exception as exc:
            if self._is_stop_exception(exc):
                self._raise_stop(exc)
            self._log(f"[手机号验证] 记录 API 转发临时失败状态失败: {exc}")

    def _handle_existing_phone_otp_verification(
        self,
        device_id,
        user_agent,
        sec_ch_ua,
        impersonate,
        state: FlowState,
        *,
        allow_existing_phone_verification: bool,
    ):
        hint = self._resolve_existing_phone_hint(state)
        phone = str(hint.get("phone") or "").strip()
        masked = str(hint.get("masked") or "").strip()
        source = str(hint.get("source") or "unknown").strip() or "unknown"
        self._log(f"[手机号验证] 命中已绑定手机号二次验证: {describe_flow_state(state)}")

        if phone:
            self._log(f"[手机号验证] 二次验证手机号: {phone} source={source}")
        elif masked:
            self._log(f"[手机号验证] 二次验证手机号仅解析到掩码: {masked} source={source}")
        else:
            self._log("[手机号验证] 二次验证手机号未能从账号记录 / OAuth session / 页面 payload 读取")

        self._record_phone_challenge_event(
            challenge_type="existing_phone_otp",
            status="required",
            phone=phone,
            masked=masked,
            source=source,
            message="命中已绑定手机号二次验证",
            allow_existing_phone_verification=allow_existing_phone_verification,
        )

        if phone or masked:
            try:
                from services.chatgpt_core.bound_phone import upsert_chatgpt_bound_phone

                upsert_chatgpt_bound_phone(
                    account_id=self.config.get("_current_account_id") or self.config.get("account_id") or 0,
                    email=self.config.get("_current_account_email") or self.config.get("email") or "",
                    phone=phone,
                    masked=masked,
                    source=source,
                    reason="existing_phone_otp",
                    verification_status="required",
                    log_fn=self._log,
                )
            except Exception as exc:
                self._log(f"[手机号验证] 记录绑定手机号失败: {exc}")

        if not allow_existing_phone_verification:
            detail = f"账号要求已绑定手机号二次验证，手机号={phone or masked or 'unknown'}，但当前开关不允许自动验证"
            self._set_error(detail)
            return None

        if not phone:
            detail = (
                f"账号要求已绑定手机号二次验证，但仅解析到掩码 {masked}，无法自动匹配手机号池"
                if masked
                else "账号要求已绑定手机号二次验证，但未读取到完整手机号"
            )
            if self._manual_phone_otp_enabled():
                self._log(f"[手机号验证] {detail}，切换为人工输入验证码")
                return self._wait_for_manual_phone_otp(
                    phone="",
                    masked=masked,
                    channel=self._current_phone_otp_channel("sms"),
                    reason=detail,
                    device_id=device_id,
                    user_agent=user_agent,
                    sec_ch_ua=sec_ch_ua,
                    impersonate=impersonate,
                    state=state,
                )
            self._set_error(detail)
            return None

        record, error_code, pool_message = self._lookup_existing_phone_pool_record(phone)
        self._log(f"[手机号验证] {pool_message}")
        pool_segment_matched = self._existing_phone_matches_pool_segment(record, error_code)
        allow_whatsapp_channel = not pool_segment_matched
        if error_code:
            if self._manual_phone_otp_enabled():
                self._log("[手机号验证] 手机号池无法自动收码，优先选择 SMS 并等待人工输入验证码")
                if self._current_phone_otp_channel("sms") != "sms" or state.page_type == "phone_otp_select_channel":
                    ok, selected_state, detail = self._select_phone_otp_channel(
                        "sms",
                        device_id,
                        user_agent,
                        sec_ch_ua,
                        impersonate,
                        state,
                    )
                    if ok and selected_state:
                        state = selected_state
                    elif detail:
                        if pool_segment_matched:
                            if self._is_openai_phone_send_rejected(detail):
                                rejected_reason = (
                                    f"手机号 {mask_phone_for_log(phone)} 属于手机号池号段，OpenAI 已拒绝发送 SMS 验证码: {redact_log_text(detail)}"
                                )
                                self._record_existing_phone_pool_openai_rejected(phone, rejected_reason)
                                self._set_error(rejected_reason)
                                return None
                            self._set_error(
                                f"手机号 {phone} 属于手机号池号段，无法切换/触发 SMS 通道，且不允许 WhatsApp 验证: {detail}"
                            )
                            return None
                        self._log(f"[手机号验证] 自动选择 SMS 通道失败，将保留当前通道: {detail}")
                return self._wait_for_manual_phone_otp(
                    phone=phone,
                    masked=masked,
                    channel=self._current_phone_otp_channel("sms"),
                    reason=pool_message,
                    device_id=device_id,
                    user_agent=user_agent,
                    sec_ch_ua=sec_ch_ua,
                    impersonate=impersonate,
                    state=state,
                    allow_whatsapp_channel=allow_whatsapp_channel,
                )
            self._set_error(pool_message)
            return None

        source_api_url = str(getattr(record, "api_url", "") or "").strip()
        if not source_api_url:
            if self._manual_phone_otp_enabled():
                detail = f"手机号 {phone} 命中手机号池但缺少 API URL，无法自动验证"
                self._log(f"[手机号验证] {detail}，切换为人工输入验证码")
                return self._wait_for_manual_phone_otp(
                    phone=phone,
                    masked=masked,
                    channel=self._current_phone_otp_channel("sms"),
                    reason=detail,
                    device_id=device_id,
                    user_agent=user_agent,
                    sec_ch_ua=sec_ch_ua,
                    impersonate=impersonate,
                    state=state,
                    allow_whatsapp_channel=False,
                )
            self._set_error(f"手机号 {phone} 命中手机号池但缺少 API URL，无法自动验证")
            return None

        try:
            from services.chatgpt_core.phone_api_forwarding import PhoneApiForwardError, resolve_phone_api_url

            resolution = resolve_phone_api_url(source_api_url, strict=True)
            api_url = str(resolution.request_api_url or "").strip()
        except PhoneApiForwardError as exc:
            detail = f"api_forward_error: 已绑定手机号 {phone} API 转发暂时不可用: {exc}"
            self._record_existing_phone_pool_forward_error(phone, detail)
            self._set_error(detail)
            return None

        try:
            from services.chatgpt_core.phone_service import UploadedPhoneEntry, UploadedPhoneService
        except Exception as exc:
            self._set_error(f"加载上传手机号接码服务失败: {exc}")
            return None

        service_entry = UploadedPhoneEntry(
            country_slug="existing_bound_phone",
            phone=phone,
            detail_url=api_url,
            api_url=api_url,
            raw_line=f"{phone}----{api_url}",
            line_no=0,
            source_api_url=source_api_url,
        )
        phone_service = UploadedPhoneService(
            [service_entry],
            {
                "_task_stop_checker": self.stop_checker,
                "uploaded_phone_timeout_seconds": self.config.get("existing_phone_otp_timeout_seconds")
                or self.config.get("uploaded_phone_timeout_seconds")
                or self.config.get("local_phone_gateway_timeout_seconds")
                or self.config.get("smstome_otp_timeout_seconds")
                or 180,
                "uploaded_phone_poll_interval_seconds": self.config.get("existing_phone_otp_poll_interval_seconds")
                or self.config.get("uploaded_phone_poll_interval_seconds")
                or self.config.get("local_phone_gateway_poll_interval_seconds")
                or self.config.get("smstome_poll_interval_seconds")
                or 5,
                "uploaded_phone_max_resend_attempts": self.config.get("existing_phone_otp_max_resend_attempts")
                or self.config.get("uploaded_phone_max_resend_attempts")
                or 1,
                "uploaded_phone_resend_interval_seconds": self.config.get("existing_phone_otp_resend_interval_seconds")
                or self.config.get("uploaded_phone_resend_interval_seconds")
                or self.config.get("local_phone_gateway_resend_interval_seconds")
                or 30,
            },
            log_fn=self._log,
        )
        phone_service.bind_entry(service_entry)

        if self._current_phone_otp_channel("sms") != "sms" or state.page_type == "phone_otp_select_channel":
            ok, selected_state, detail = self._select_phone_otp_channel(
                "sms",
                device_id,
                user_agent,
                sec_ch_ua,
                impersonate,
                state,
            )
            if ok and selected_state:
                state = selected_state
                self._log(f"[手机号验证] 已优先选择 SMS 通道: {phone}")
            elif detail:
                if self._is_openai_phone_send_rejected(detail):
                    rejected_reason = f"手机号 {mask_phone_for_log(phone)} 属于手机号池号段，OpenAI 已拒绝发送 SMS 验证码: {redact_log_text(detail)}"
                    self._record_existing_phone_pool_openai_rejected(phone, rejected_reason)
                    self._set_error(rejected_reason)
                    return None
                self._set_error(
                    f"手机号 {phone} 属于手机号池号段，无法切换/触发 SMS 通道，且不允许 WhatsApp 验证: {detail}"
                )
                return None

        resend_ok, resend_detail = self._resend_phone_otp(
            device_id,
            user_agent,
            sec_ch_ua,
            impersonate,
            state,
        )
        if not resend_ok:
            if self._is_openai_phone_send_rejected(resend_detail):
                rejected_reason = f"手机号 {mask_phone_for_log(phone)} 属于手机号池号段，OpenAI 已拒绝发送 SMS 验证码: {redact_log_text(resend_detail)}"
                self._record_existing_phone_pool_openai_rejected(phone, rejected_reason)
                self._set_error(rejected_reason)
                return None
            if self._manual_phone_otp_enabled():
                detail = f"已绑定手机号 {phone} 验证短信触发失败: {resend_detail}"
                self._log(f"[手机号验证] {detail}，切换为人工输入验证码")
                return self._wait_for_manual_phone_otp(
                    phone=phone,
                    masked=masked,
                    channel=self._current_phone_otp_channel("sms"),
                    reason=detail,
                    device_id=device_id,
                    user_agent=user_agent,
                    sec_ch_ua=sec_ch_ua,
                    impersonate=impersonate,
                    state=state,
                    allow_whatsapp_channel=False,
                )
            self._set_error(f"已绑定手机号 {phone} 验证短信触发失败: {resend_detail}")
            return None
        self._log(f"[手机号验证] 已请求绑定手机号短信验证码: {phone}")
        phone_service.mark_sms_sent(service_entry)

        try:
            code = phone_service.wait_for_code(service_entry)
        except PhoneApiForwardError as exc:
            detail = f"api_forward_error: 已绑定手机号 {phone} API 转发暂时不可用: {exc}"
            self._record_existing_phone_pool_forward_error(phone, detail)
            self._set_error(detail)
            return None
        max_resends = int(getattr(phone_service, "max_resend_attempts", 1) or 1)
        resend_interval_seconds = int(getattr(phone_service, "resend_interval_seconds", 0) or 0)
        for resend_attempt in range(1, max_resends + 1):
            if code:
                break
            if resend_interval_seconds > 0:
                self._log(
                    f"[手机号验证] 绑定手机号验证码暂未收到，等待 {resend_interval_seconds:g}s 后重发 {resend_attempt}/{max_resends}: {phone}"
                )
                self._sleep_with_stop(resend_interval_seconds)
            resend_ok, resend_detail = self._resend_phone_otp(
                device_id,
                user_agent,
                sec_ch_ua,
                impersonate,
                state,
            )
            if not resend_ok:
                self._set_error(f"已绑定手机号 {phone} 验证短信重发失败: {resend_detail}")
                return None
            phone_service.mark_sms_sent(service_entry)
            try:
                code = phone_service.wait_for_code(service_entry)
            except PhoneApiForwardError as exc:
                detail = f"api_forward_error: 已绑定手机号 {phone} API 转发暂时不可用: {exc}"
                self._record_existing_phone_pool_forward_error(phone, detail)
                self._set_error(detail)
                return None

        if not code:
            if self._manual_phone_otp_enabled():
                detail = f"已绑定手机号 {mask_phone_for_log(phone)} API 未收到验证码"
                phone_service.cancel(service_entry, reason=detail)
                self._log(f"[手机号验证] {detail}，切换为人工输入验证码")
                return self._wait_for_manual_phone_otp(
                    phone=phone,
                    masked=masked,
                    channel=self._current_phone_otp_channel("sms"),
                    reason=detail,
                    device_id=device_id,
                    user_agent=user_agent,
                    sec_ch_ua=sec_ch_ua,
                    impersonate=impersonate,
                    state=state,
                    allow_whatsapp_channel=False,
                )
            self._set_error(f"已绑定手机号 {mask_phone_for_log(phone)} API 未收到验证码")
            return None

        try:
            validate_delay_seconds = float(getattr(phone_service, "validate_delay_seconds", 0) or 0)
        except (TypeError, ValueError):
            validate_delay_seconds = 0
        if validate_delay_seconds > 0:
            self._log(f"[手机号验证] 准备提交绑定手机号验证码 otp={code} otp_present={bool(code)} otp_length={len(str(code or ''))}，等待 {validate_delay_seconds:g}s 后验证")
            self._sleep_with_stop(validate_delay_seconds)
        else:
            self._log(f"[手机号验证] 准备提交绑定手机号验证码 otp={code} otp_present={bool(code)} otp_length={len(str(code or ''))}")

        valid, validated_state, detail = self._validate_phone_otp(
            code,
            device_id,
            user_agent,
            sec_ch_ua,
            impersonate,
            state,
        )
        if not valid or not validated_state:
            phone_service.cancel(service_entry, reason=detail or "bound phone otp validate failed")
            self._set_error(f"已绑定手机号 {phone} OTP 验证失败: {detail or 'unknown'}")
            return None
        phone_service.complete(service_entry)
        self._log(f"[手机号验证] 绑定手机号 OTP 验证通过: {phone}")
        return validated_state

    def _state_supports_workspace_resolution(self, state: FlowState):
        target = f"{state.continue_url} {state.current_url}".lower()
        if state.page_type in {
            "consent",
            "workspace_selection",
            "organization_selection",
        }:
            return True
        if any(
            marker in target
            for marker in (
                "sign-in-with-chatgpt",
                "consent",
                "workspace",
                "organization",
            )
        ):
            return True
        session_data = self._decode_oauth_session_cookie() or {}
        return bool(session_data.get("workspaces"))

    def _follow_flow_state(
        self,
        state: FlowState,
        referer=None,
        user_agent=None,
        impersonate=None,
        max_hops=16,
    ):
        """跟随服务端返回的 continue_url / current_url，返回新的状态或 authorization code。"""
        import re

        self._check_stop()
        current_url = state.continue_url or state.current_url
        last_url = current_url or ""
        referer_url = referer

        if not current_url:
            return None, state

        initial_code = self._extract_code_from_url(current_url)
        if initial_code:
            return initial_code, self._state_from_url(current_url)

        for hop in range(max_hops):
            self._check_stop()
            try:
                headers = self._headers(
                    current_url,
                    user_agent=user_agent,
                    accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    referer=referer_url,
                    navigation=True,
                )
                kwargs = {"headers": headers, "allow_redirects": False, "timeout": 30}
                if impersonate:
                    kwargs["impersonate"] = impersonate

                self._browser_pause(0.12, 0.3)
                r = self.session.get(current_url, **kwargs)
                last_url = str(r.url)
                self._log(f"follow[{hop + 1}] {r.status_code} {last_url[:120]}")
            except Exception as e:
                if self._is_stop_exception(e):
                    self._raise_stop(e)
                maybe_localhost = re.search(r"(https?://localhost[^\s\'\"]+)", str(e))
                if maybe_localhost:
                    location = maybe_localhost.group(1)
                    code = self._extract_code_from_url(location)
                    if code:
                        self._log("从 localhost 异常提取到 authorization code")
                        return code, self._state_from_url(location)
                self._log(f"follow[{hop + 1}] 异常: {str(e)[:160]}")
                return None, self._state_from_url(last_url or current_url)

            self._check_stop()
            code = self._extract_code_from_url(last_url)
            if code:
                return code, self._state_from_url(last_url)

            if r.status_code in (301, 302, 303, 307, 308):
                location = normalize_flow_url(
                    r.headers.get("Location", ""), auth_base=self.oauth_issuer
                )
                if not location:
                    return None, self._state_from_url(last_url or current_url)
                code = self._extract_code_from_url(location)
                if code:
                    return code, self._state_from_url(location)
                referer_url = last_url or referer_url
                current_url = location
                continue

            content_type = (r.headers.get("content-type", "") or "").lower()
            if "application/json" in content_type:
                try:
                    next_state = self._state_from_payload(
                        r.json(), current_url=last_url or current_url
                    )
                except Exception:
                    next_state = self._state_from_url(last_url or current_url)
            else:
                next_state = self._state_from_url(last_url or current_url)

            return None, next_state

        return None, self._state_from_url(last_url or current_url)

    def _bootstrap_oauth_session(
        self,
        authorize_url,
        authorize_params,
        device_id=None,
        user_agent=None,
        sec_ch_ua=None,
        impersonate=None,
    ):
        """启动 OAuth 会话，确保 auth 域上的 login_session 已建立。"""
        self._check_stop()
        if device_id:
            seed_oai_device_cookie(self.session, device_id)

        has_login_session = False
        authorize_final_url = ""
        authorize_status_code = 0
        authorize_text = ""
        oauth2_status_code = 0
        oauth2_text = ""

        try:
            self._check_stop()
            headers = self._headers(
                authorize_url,
                user_agent=user_agent,
                sec_ch_ua=sec_ch_ua,
                accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                referer="https://chatgpt.com/",
                navigation=True,
            )
            kwargs = {
                "params": authorize_params,
                "headers": headers,
                "allow_redirects": True,
                "timeout": 30,
            }
            if impersonate:
                kwargs["impersonate"] = impersonate

            self._browser_pause()
            r = self.session.get(authorize_url, **kwargs)
            authorize_status_code = int(getattr(r, "status_code", 0) or 0)
            authorize_text = str(getattr(r, "text", "") or "")
            authorize_final_url = str(r.url)
            redirects = len(getattr(r, "history", []) or [])
            self._log(f"/oauth/authorize -> {authorize_status_code}, redirects={redirects}")

            has_login_session = any(
                (cookie.name if hasattr(cookie, "name") else str(cookie))
                == "login_session"
                for cookie in self.session.cookies
            )
            self._log(f"login_session: {'已获取' if has_login_session else '未获取'}")
        except Exception as e:
            if self._is_stop_exception(e):
                self._raise_stop(e)
            self._log(f"/oauth/authorize 异常: {e}")

        if has_login_session:
            return authorize_final_url

        self._log("未获取到 login_session，尝试 /api/oauth/oauth2/auth...")
        try:
            self._check_stop()
            oauth2_url = f"{self.oauth_issuer}/api/oauth/oauth2/auth"
            kwargs = {
                "params": authorize_params,
                "headers": self._headers(
                    oauth2_url,
                    user_agent=user_agent,
                    sec_ch_ua=sec_ch_ua,
                    accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    referer="https://chatgpt.com/",
                    navigation=True,
                ),
                "allow_redirects": True,
                "timeout": 30,
            }
            if impersonate:
                kwargs["impersonate"] = impersonate

            self._browser_pause()
            r2 = self.session.get(oauth2_url, **kwargs)
            oauth2_status_code = int(getattr(r2, "status_code", 0) or 0)
            oauth2_text = str(getattr(r2, "text", "") or "")
            authorize_final_url = str(r2.url)
            redirects2 = len(getattr(r2, "history", []) or [])
            self._log(
                f"/api/oauth/oauth2/auth -> {oauth2_status_code}, redirects={redirects2}"
            )

            has_login_session = any(
                (cookie.name if hasattr(cookie, "name") else str(cookie))
                == "login_session"
                for cookie in self.session.cookies
            )
            self._log(
                f"login_session(重试): {'已获取' if has_login_session else '未获取'}"
            )
        except Exception as e:
            if self._is_stop_exception(e):
                self._raise_stop(e)
            self._log(f"/api/oauth/oauth2/auth 异常: {e}")

        challenge_detected = self._looks_like_cloudflare_challenge(
            authorize_text,
            status_code=authorize_status_code,
            url=authorize_final_url or authorize_url,
        ) or self._looks_like_cloudflare_challenge(
            oauth2_text,
            status_code=oauth2_status_code,
            url=authorize_final_url or f"{self.oauth_issuer}/api/oauth/oauth2/auth",
        )

        if not has_login_session and challenge_detected and self.allow_browser:
            self._log("bootstrap: 检测到 Cloudflare challenge，尝试浏览器预热并回灌 auth cookies...")
            browser_final_url, browser_ready = self._browser_bootstrap_oauth_session(
                authorize_url,
                authorize_params,
                device_id=device_id,
                user_agent=user_agent,
                sec_ch_ua=sec_ch_ua,
            )
            if browser_final_url:
                authorize_final_url = browser_final_url
            has_login_session = browser_ready or self._has_cookie("login_session")
            self._log(
                "login_session(浏览器回灌): "
                + ("已获取" if has_login_session else "未获取")
            )
        elif not has_login_session and challenge_detected:
            self._log(
                "bootstrap: Cloudflare challenge，纯协议执行器禁止浏览器预热"
            )

        if not has_login_session:
            self._log("bootstrap: 仍未建立 login_session，后续 authorize/continue 大概率会被拦截")

        return authorize_final_url

    def _bootstrap_chatgpt_entry(
        self,
        email: str,
        device_id: str,
        *,
        user_agent=None,
        sec_ch_ua=None,
        impersonate=None,
    ) -> str:
        """模拟注册链路一致的 ChatGPT 首页 -> CSRF -> signin/openai。"""
        self._check_stop()
        homepage_url = "https://chatgpt.com/"
        csrf_url = "https://chatgpt.com/api/auth/csrf"
        signin_url = "https://chatgpt.com/api/auth/signin/openai"

        try:
            self._check_stop()
            self._log("force_chatgpt_entry: 访问 ChatGPT 首页...")
            self._browser_pause()
            r_home = self.session.get(
                homepage_url,
                headers=self._headers(
                    homepage_url,
                    user_agent=user_agent,
                    sec_ch_ua=sec_ch_ua,
                    accept="text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                    navigation=True,
                ),
                allow_redirects=True,
                timeout=30,
            )
            self._log(f"force_chatgpt_entry: 首页状态 {r_home.status_code}")
        except Exception as e:
            if self._is_stop_exception(e):
                self._raise_stop(e)
            self._log(f"force_chatgpt_entry: 首页访问异常: {e}")

        csrf_token = ""
        try:
            self._check_stop()
            self._log("force_chatgpt_entry: 获取 CSRF token...")
            r_csrf = self.session.get(
                csrf_url,
                headers=self._headers(
                    csrf_url,
                    user_agent=user_agent,
                    sec_ch_ua=sec_ch_ua,
                    accept="application/json",
                    referer=homepage_url,
                    fetch_site="same-origin",
                ),
                timeout=30,
            )
            if r_csrf.status_code == 200:
                csrf_token = (r_csrf.json() or {}).get("csrfToken", "") or ""
                if csrf_token:
                    self._log(f"force_chatgpt_entry: CSRF token={csrf_token[:16]}...")
        except Exception as e:
            if self._is_stop_exception(e):
                self._raise_stop(e)
            self._log(f"force_chatgpt_entry: 获取 CSRF 异常: {e}")

        authorize_url = ""
        try:
            self._check_stop()
            self._log("force_chatgpt_entry: 提交邮箱获取 authorize URL...")
            params = {
                "prompt": "login",
                "ext-oai-did": device_id,
                "auth_session_logging_id": str(uuid.uuid4()),
                "screen_hint": "login_or_signup",
                "login_hint": email,
            }
            form_data = {
                "callbackUrl": "https://chatgpt.com/",
                "csrfToken": csrf_token,
                "json": "true",
            }
            r_signin = self.session.post(
                signin_url,
                params=params,
                data=form_data,
                headers=self._headers(
                    signin_url,
                    user_agent=user_agent,
                    sec_ch_ua=sec_ch_ua,
                    accept="application/json",
                    referer=homepage_url,
                    origin="https://chatgpt.com",
                    content_type="application/x-www-form-urlencoded",
                    fetch_site="same-origin",
                ),
                timeout=30,
            )
            if r_signin.status_code == 200:
                authorize_url = (r_signin.json() or {}).get("url", "") or ""
                if authorize_url:
                    self._log("force_chatgpt_entry: 已获取 authorize URL")
            else:
                self._log(
                    f"force_chatgpt_entry: authorize URL 获取失败 {r_signin.status_code}"
                )
        except Exception as e:
            if self._is_stop_exception(e):
                self._raise_stop(e)
            self._log(f"force_chatgpt_entry: 提交邮箱异常: {e}")

        if not authorize_url:
            return ""

        try:
            self._check_stop()
            self._log("force_chatgpt_entry: 访问 authorize URL...")
            self._browser_pause()
            kwargs = {
                "headers": self._headers(
                    authorize_url,
                    user_agent=user_agent,
                    sec_ch_ua=sec_ch_ua,
                    accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    referer=homepage_url,
                    navigation=True,
                ),
                "allow_redirects": True,
                "timeout": 30,
            }
            if impersonate:
                kwargs["impersonate"] = impersonate
            r_auth = self.session.get(authorize_url, **kwargs)
            final_url = str(r_auth.url)
            self._log(
                f"force_chatgpt_entry: authorize 最终跳转 {final_url[:160]}"
            )
            return final_url
        except Exception as e:
            if self._is_stop_exception(e):
                self._raise_stop(e)
            self._log(f"force_chatgpt_entry: 访问 authorize 异常: {e}")
            return authorize_url

    def _submit_authorize_continue(
        self,
        email,
        device_id,
        continue_referer,
        *,
        user_agent=None,
        sec_ch_ua=None,
        impersonate=None,
        authorize_url=None,
        authorize_params=None,
        screen_hint=None,
    ):
        """提交邮箱，获取 OAuth 流程的第一页状态。"""
        self._check_stop()
        self._log("步骤2: POST /api/accounts/authorize/continue")

        request_url = f"{self.oauth_issuer}/api/accounts/authorize/continue"
        payload = {"username": {"kind": "email", "value": email}}
        if screen_hint:
            payload["screen_hint"] = str(screen_hint).strip()

        current_referer = continue_referer
        for attempt in range(2):
            self._check_stop()
            self._log(f"authorize_continue: device_id={device_id}")
            if not self._has_cookie("login_session"):
                self._set_error(
                    "OAuth bootstrap 未建立 login_session，已跳过 authorize/continue 直提邮箱"
                )
                return None
            sentinel_token = build_sentinel_token(
                self.session,
                device_id,
                flow="authorize_continue",
                user_agent=user_agent,
                sec_ch_ua=sec_ch_ua,
                impersonate=impersonate,
            )
            if sentinel_token:
                self._log("authorize_continue: 已通过 HTTP PoW 获取 token")
            elif self.allow_browser:
                self._log("authorize_continue: HTTP PoW 获取 token 失败，回退到 Playwright SentinelSDK")
                sentinel_token = get_sentinel_token_via_browser(
                    flow="authorize_continue",
                    proxy=self.proxy,
                    page_url=current_referer or f"{self.oauth_issuer}/log-in",
                    headless=self.browser_mode != "headed",
                    device_id=device_id,
                    user_agent=user_agent,
                    sec_ch_ua=sec_ch_ua,
                    accept_language=(self.browser_fingerprint.accept_language if self.browser_fingerprint else None),
                    chrome_full_version=(self.browser_fingerprint.chrome_full_version if self.browser_fingerprint else None),
                    platform_version=(self.browser_fingerprint.platform_version if self.browser_fingerprint else None),
                    viewport_width=(self.browser_fingerprint.viewport_width if self.browser_fingerprint else None),
                    viewport_height=(self.browser_fingerprint.viewport_height if self.browser_fingerprint else None),
                    stop_check=self._check_stop,
                    log_fn=lambda msg: self._log(f"authorize_continue: {msg}"),
                )
                if sentinel_token:
                    self._log("authorize_continue: 已通过 Playwright SentinelSDK 获取 token")
                else:
                    self._set_error("无法获取 sentinel token (authorize_continue)")
                    return None
            else:
                self._set_error(
                    "sentinel_protocol_unavailable (authorize_continue): "
                    "纯协议执行器禁止启动浏览器"
                )
                return None

            headers = self._headers(
                request_url,
                user_agent=user_agent,
                sec_ch_ua=sec_ch_ua,
                accept="application/json",
                referer=current_referer,
                origin=self.oauth_issuer,
                content_type="application/json",
                fetch_site="same-origin",
                extra_headers={
                    "oai-device-id": device_id,
                    "openai-sentinel-token": sentinel_token,
                },
            )
            headers.update(generate_datadog_trace())

            try:
                kwargs = {
                    "json": payload,
                    "headers": headers,
                    "timeout": 30,
                    "allow_redirects": False,
                }
                if impersonate:
                    kwargs["impersonate"] = impersonate

                self._browser_pause()
                request_started_at = _otp_request_started_at()
                r = self.session.post(request_url, **kwargs)
                self._log(f"/authorize/continue -> {r.status_code}")

                if r.status_code == 429 and attempt == 0:
                    wait_seconds = round(random.uniform(2.0, 4.5), 2)
                    self._log(
                        f"authorize_continue: 429 限流，等待 {wait_seconds}s 后重试"
                    )
                    self._sleep_with_stop(wait_seconds)
                    continue

                if (
                    r.status_code == 409
                    and "invalid_state" in (r.text or "")
                    and authorize_url
                    and authorize_params
                    and attempt == 0
                ):
                    self._log("invalid_state，重新 bootstrap 后重试...")
                    authorize_final_url = self._bootstrap_oauth_session(
                        authorize_url,
                        authorize_params,
                        device_id=device_id,
                        user_agent=user_agent,
                        sec_ch_ua=sec_ch_ua,
                        impersonate=impersonate,
                    )
                    current_referer = (
                        authorize_final_url
                        if authorize_final_url.startswith(self.oauth_issuer)
                        else f"{self.oauth_issuer}/log-in"
                    )
                    continue

                if (
                    r.status_code == 400
                    and "invalid_auth_step" in (r.text or "")
                    and authorize_url
                    and authorize_params
                ):
                    self._log("invalid_auth_step，重新 bootstrap...")
                    authorize_final_url = self._bootstrap_oauth_session(
                        authorize_url,
                        authorize_params,
                        device_id=device_id,
                        user_agent=user_agent,
                        sec_ch_ua=sec_ch_ua,
                        impersonate=impersonate,
                    )
                    current_referer = (
                        authorize_final_url
                        if authorize_final_url.startswith(self.oauth_issuer)
                        else f"{self.oauth_issuer}/log-in"
                    )
                    headers["Referer"] = current_referer
                    headers["Sec-Fetch-Site"] = "same-origin"
                    headers.update(generate_datadog_trace())
                    kwargs = {
                        "json": payload,
                        "headers": headers,
                        "timeout": 30,
                        "allow_redirects": False,
                    }
                    if impersonate:
                        kwargs["impersonate"] = impersonate
                    self._browser_pause()
                    request_started_at = _otp_request_started_at()
                    r = self.session.post(request_url, **kwargs)
                    self._log(f"/authorize/continue(重试) -> {r.status_code}")

                if r.status_code != 200:
                    self._set_error(f"提交邮箱失败: {r.status_code} - {r.text[:180]}")
                    return None

                data = r.json()
                flow_state = self._state_from_payload(
                    data, current_url=str(r.url) or request_url
                )
                if self._state_is_email_otp(flow_state):
                    flow_state.otp_sent_at = request_started_at
                self._log(describe_flow_state(flow_state))
                if self._state_is_email_otp(flow_state):
                    self._log("authorize_continue 分支判定: 进入 email_otp_first / 既有账号恢复倾向链")
                elif self._state_is_login_password(flow_state):
                    self._log("authorize_continue 分支判定: 进入 login_password / 标准密码登录链")
                return flow_state
            except Exception as e:
                if self._is_stop_exception(e):
                    self._raise_stop(e)
                self._set_error(f"提交邮箱异常: {e}")
                return None
        return None

    def _submit_password_verify(
        self,
        email,
        password,
        device_id,
        *,
        user_agent=None,
        sec_ch_ua=None,
        impersonate=None,
        referer=None,
    ):
        """提交密码，获取下一步状态。"""
        self._check_stop()
        self._log("步骤3: POST /api/accounts/password/verify")

        request_url = f"{self.oauth_issuer}/api/accounts/password/verify"
        payload = {"password": password}

        for attempt in range(2):
            self._check_stop()
            self._log(f"password_verify: device_id={device_id}")
            sentinel_pwd = None
            if self.allow_browser:
                sentinel_pwd = get_sentinel_token_via_browser(
                    flow="password_verify",
                    proxy=self.proxy,
                    page_url=referer or f"{self.oauth_issuer}/log-in/password",
                    headless=self.browser_mode != "headed",
                    device_id=device_id,
                    user_agent=user_agent,
                    sec_ch_ua=sec_ch_ua,
                    accept_language=(self.browser_fingerprint.accept_language if self.browser_fingerprint else None),
                    chrome_full_version=(self.browser_fingerprint.chrome_full_version if self.browser_fingerprint else None),
                    platform_version=(self.browser_fingerprint.platform_version if self.browser_fingerprint else None),
                    viewport_width=(self.browser_fingerprint.viewport_width if self.browser_fingerprint else None),
                    viewport_height=(self.browser_fingerprint.viewport_height if self.browser_fingerprint else None),
                    stop_check=self._check_stop,
                    log_fn=lambda msg: self._log(f"password_verify: {msg}"),
                )
            if sentinel_pwd:
                self._log("password_verify: 已通过 Playwright SentinelSDK 获取 token")
            else:
                sentinel_pwd = build_sentinel_token(
                    self.session,
                    device_id,
                    flow="password_verify",
                    user_agent=user_agent,
                    sec_ch_ua=sec_ch_ua,
                    impersonate=impersonate,
                )
                if sentinel_pwd:
                    self._log("password_verify: 已通过 HTTP PoW 获取 token")
                else:
                    self._set_error("无法获取 sentinel token (password_verify)")
                    return None

            headers = self._headers(
                request_url,
                user_agent=user_agent,
                sec_ch_ua=sec_ch_ua,
                accept="application/json",
                referer=referer or f"{self.oauth_issuer}/log-in/password",
                origin=self.oauth_issuer,
                content_type="application/json",
                fetch_site="same-origin",
                extra_headers={
                    "oai-device-id": device_id,
                    "openai-sentinel-token": sentinel_pwd,
                },
            )
            headers.update(generate_datadog_trace())

            try:
                kwargs = {
                    "json": payload,
                    "headers": headers,
                    "timeout": 30,
                    "allow_redirects": False,
                }
                if impersonate:
                    kwargs["impersonate"] = impersonate

                self._browser_pause()
                request_started_at = _otp_request_started_at()
                r = self.session.post(request_url, **kwargs)
                self._log(f"/password/verify -> {r.status_code}")

                if r.status_code == 429 and attempt == 0:
                    wait_seconds = round(random.uniform(2.0, 4.5), 2)
                    self._log(
                        f"password_verify: 429 限流，等待 {wait_seconds}s 后重试"
                    )
                    self._sleep_with_stop(wait_seconds)
                    continue

                if r.status_code != 200:
                    response_text = ""
                    try:
                        response_text = r.text or ""
                    except Exception:
                        response_text = ""

                    if self._is_password_login_rejection(
                        r.status_code,
                        response_text,
                    ):
                        self._log(
                            "密码验证失败，自动退回邮箱验证码登录链路"
                        )
                        next_state = self._send_passwordless_login_otp(
                            email,
                            device_id,
                            user_agent=user_agent,
                            sec_ch_ua=sec_ch_ua,
                            impersonate=impersonate,
                            referer=referer or f"{self.oauth_issuer}/log-in/password",
                        )
                        if next_state:
                            return next_state

                        passwordless_error = str(self.last_error or "").strip()
                        self._set_error(
                            f"密码验证失败: {r.status_code} - {response_text[:180]}; "
                            f"邮箱验证码兜底失败: {passwordless_error or 'unknown'}"
                        )
                        return None

                    self._set_error(
                        f"密码验证失败: {r.status_code} - {response_text[:180]}"
                    )
                    return None

                data = r.json()
                flow_state = self._state_from_payload(
                    data, current_url=str(r.url) or request_url
                )
                if self._state_is_email_otp(flow_state):
                    flow_state.otp_sent_at = request_started_at
                self._log(f"verify {describe_flow_state(flow_state)}")
                return flow_state
            except Exception as e:
                if self._is_stop_exception(e):
                    self._raise_stop(e)
                self._set_error(f"密码验证异常: {e}")
                return None
        return None

    @staticmethod
    def _is_password_login_rejection(status_code, response_text) -> bool:
        try:
            status = int(status_code or 0)
        except (TypeError, ValueError):
            status = 0
        if status not in {400, 401}:
            return False
        lowered = str(response_text or "").strip().lower()
        if any(
            marker in lowered
            for marker in (
                "login failed",
                "invalid credentials",
                "incorrect email address or password",
                "incorrect email or password",
                "invalid email or password",
                "incorrect password",
                "wrong password",
                "密码不正确",
                "密码错误",
            )
        ):
            return True
        return status == 401 and "invalid_request_error" in lowered

    def _send_passwordless_login_otp(
        self,
        email,
        device_id,
        *,
        user_agent=None,
        sec_ch_ua=None,
        impersonate=None,
        referer=None,
    ):
        """在 login_password 状态下直接切到 passwordless OTP。"""
        self._check_stop()
        self._log("步骤3: 命中 login_password，按新链路直接触发 passwordless OTP")

        request_url = f"{self.oauth_issuer}/api/accounts/passwordless/send-otp"
        headers = self._headers(
            request_url,
            user_agent=user_agent,
            sec_ch_ua=sec_ch_ua,
            accept="application/json",
            referer=referer or f"{self.oauth_issuer}/log-in/password",
            origin=self.oauth_issuer,
            content_type="application/json",
            fetch_site="same-origin",
            extra_headers={
                "oai-device-id": device_id,
            },
        )
        headers.update(generate_datadog_trace())

        try:
            kwargs = {
                "headers": headers,
                "timeout": 30,
                "allow_redirects": False,
            }
            if impersonate:
                kwargs["impersonate"] = impersonate

            self._browser_pause()
            request_started_at = _otp_request_started_at()
            r = self.session.post(request_url, **kwargs)
            self._log(f"/passwordless/send-otp -> {r.status_code}")

            if r.status_code != 200:
                self._set_error(f"触发 passwordless OTP 失败: {r.status_code} - {r.text[:180]}")
                return None

            try:
                data = r.json()
            except Exception:
                data = {}

            flow_state = self._state_from_payload(
                data,
                current_url=str(r.url) or f"{self.oauth_issuer}/email-verification",
            )
            if not self._state_is_email_otp(flow_state):
                flow_state = self._state_from_url(f"{self.oauth_issuer}/email-verification")
            flow_state.otp_sent_at = request_started_at
            self._log(f"passwordless OTP 已触发 {describe_flow_state(flow_state)}")
            self._check_stop()
            return flow_state
        except Exception as e:
            if self._is_stop_exception(e):
                self._raise_stop(e)
            self._set_error(f"触发 passwordless OTP 异常: {e}")
            return None

    def _advance_existing_account_login(
        self,
        email,
        password,
        device_id,
        *,
        user_agent=None,
        sec_ch_ua=None,
        impersonate=None,
        referer=None,
        prefer_passwordless_login=True,
        force_password_login=False,
    ):
        """Advance an existing email account from login_password."""
        if prefer_passwordless_login and not force_password_login:
            self._log("已有邮箱账号优先使用一次性验证码登录；库存密码仅作显式密码模式凭据")
            return self._send_passwordless_login_otp(
                email,
                device_id,
                user_agent=user_agent,
                sec_ch_ua=sec_ch_ua,
                impersonate=impersonate,
                referer=referer,
            )

        if not str(password or ""):
            self._set_error("当前登录策略需要密码，但库存密码为空")
            return None
        return self._submit_password_verify(
            email,
            password,
            device_id,
            user_agent=user_agent,
            sec_ch_ua=sec_ch_ua,
            impersonate=impersonate,
            referer=referer,
        )

    def _submit_about_you_create_account_via_protocol(
        self,
        *,
        full_name,
        birthdate,
        device_id,
        user_agent=None,
        sec_ch_ua=None,
        impersonate=None,
        referer=None,
    ):
        # 对齐 any-auto：create_account 前推进 client_auth_session_dump
        dump_url = f"{self.oauth_issuer}/api/accounts/client_auth_session_dump"
        try:
            dump_headers = self._headers(
                dump_url,
                user_agent=user_agent,
                sec_ch_ua=sec_ch_ua,
                accept="application/json",
                referer=f"{self.oauth_issuer}/email-verification",
                fetch_site="same-origin",
                extra_headers={"oai-device-id": device_id},
            )
            dump_kwargs = {"headers": dump_headers, "timeout": 20}
            if impersonate:
                dump_kwargs["impersonate"] = impersonate
            dump_resp = self.session.get(dump_url, **dump_kwargs)
            self._log(
                f"client_auth_session_dump 状态: {getattr(dump_resp, 'status_code', 0)}"
            )
        except Exception as dump_exc:
            self._log(f"client_auth_session_dump 异常: {dump_exc}")

        sentinel_token = build_sentinel_token(
            self.session,
            device_id,
            flow="oauth_create_account",
            user_agent=user_agent,
            sec_ch_ua=sec_ch_ua,
            impersonate=impersonate,
        )
        if not sentinel_token:
            self._set_error(
                "sentinel_protocol_unavailable (oauth_create_account): "
                "纯协议执行器禁止启动浏览器"
            )
            return None

        request_url = f"{self.oauth_issuer}/api/accounts/create_account"
        headers = self._headers(
            request_url,
            user_agent=user_agent,
            sec_ch_ua=sec_ch_ua,
            accept="application/json",
            referer=referer or f"{self.oauth_issuer}/about-you",
            origin=self.oauth_issuer,
            content_type="application/json",
            fetch_site="same-origin",
            extra_headers={
                "oai-device-id": device_id,
                "openai-sentinel-token": sentinel_token,
            },
        )
        headers.update(generate_datadog_trace())
        try:
            import json as _json

            kwargs = {
                "data": _json.dumps(
                    {"name": full_name, "birthdate": str(birthdate).strip()},
                    separators=(",", ":"),
                ),
                "headers": headers,
                "timeout": 30,
                "allow_redirects": False,
            }
            if impersonate:
                kwargs["impersonate"] = impersonate
            self._check_stop()
            response = self.session.post(request_url, **kwargs)
            response_text = str(getattr(response, "text", "") or "")
            try:
                data = response.json() or {}
            except Exception:
                data = {}
            if response.status_code != 200:
                lowered = response_text.lower()
                if response.status_code == 400 and any(
                    marker in lowered
                    for marker in (
                        "account already exists",
                        "please login instead",
                        "user_already_exists",
                    )
                ):
                    self._about_you_existing_account_detected = True
                error_info = data.get("error") if isinstance(data, dict) else {}
                error_code = str(
                    (error_info or {}).get("code")
                    if isinstance(error_info, dict)
                    else ""
                ).strip()
                detail = f"about_you 提交失败: {response.status_code}"
                if error_code:
                    detail += f": {error_code}"
                elif response_text:
                    detail += f" - {response_text[:180]}"
                self._set_error(detail)
                return None
            flow_state = self._state_from_payload(
                data,
                current_url=str(getattr(response, "url", "") or request_url),
            )
            self._log(f"about_you 纯协议提交成功 {describe_flow_state(flow_state)}")
            return flow_state
        except Exception as exc:
            if self._is_stop_exception(exc):
                self._raise_stop(exc)
            self._set_error(f"about_you 纯协议提交异常: {exc}")
            return None

    def _submit_about_you_create_account(
        self,
        first_name,
        last_name,
        birthdate,
        device_id,
        *,
        user_agent=None,
        sec_ch_ua=None,
        impersonate=None,
        referer=None,
    ):
        """在 OAuth 登录态命中 about_you 后提交资料，完成账户创建。"""
        self._check_stop()
        self._log("步骤5: 命中 about_you，提交姓名和生日完成注册")
        self._log(
            "about_you 参数: "
            f"first_name={'已设置' if str(first_name or '').strip() else '缺失'}, "
            f"last_name={'已设置' if str(last_name or '').strip() else '缺失'}, "
            f"birthdate={str(birthdate or '').strip() or '缺失'}"
        )

        full_name = f"{str(first_name or '').strip()} {str(last_name or '').strip()}".strip()
        if not full_name or not str(birthdate or "").strip():
            self._set_error("about_you 资料不完整: 缺少姓名或生日")
            return None

        if not self.allow_browser:
            return self._submit_about_you_create_account_via_protocol(
                full_name=full_name,
                birthdate=birthdate,
                device_id=device_id,
                user_agent=user_agent,
                sec_ch_ua=sec_ch_ua,
                impersonate=impersonate,
                referer=referer,
            )

        request_url = f"{self.oauth_issuer}/api/accounts/create_account"
        self._log("about_you 请求体已构建，交由同一 Auth 浏览器上下文提交")

        try:
            self._check_stop()
            result = create_account_via_browser(
                name=full_name,
                birthdate=str(birthdate).strip(),
                proxy=self.proxy,
                page_url=referer or f"{self.oauth_issuer}/about-you",
                headless=self.browser_mode == "headless",
                device_id=device_id,
                user_agent=user_agent,
                sec_ch_ua=sec_ch_ua,
                accept_language=(
                    self.browser_fingerprint.accept_language
                    if self.browser_fingerprint
                    else None
                ),
                chrome_full_version=(
                    self.browser_fingerprint.chrome_full_version
                    if self.browser_fingerprint
                    else None
                ),
                platform_version=(
                    self.browser_fingerprint.platform_version
                    if self.browser_fingerprint
                    else None
                ),
                viewport_width=(
                    self.browser_fingerprint.viewport_width
                    if self.browser_fingerprint
                    else None
                ),
                viewport_height=(
                    self.browser_fingerprint.viewport_height
                    if self.browser_fingerprint
                    else None
                ),
                cookies=self._export_session_cookies_for_playwright(),
                trace_headers=generate_datadog_trace(),
                stop_check=self._check_stop,
                log_fn=lambda msg: self._log(f"oauth_create_account: {msg}"),
            )
            if result is None:
                self._set_error(
                    "auth_browser_finalize_unavailable: "
                    "oauth_create_account 浏览器事务没有返回结果"
                )
                return None

            merged = self._merge_playwright_cookies_into_session(result.cookies)
            self._log(
                "oauth_create_account 浏览器事务完成: "
                f"status={result.status_code} merged_cookies={merged} "
                f"cf_clearance={'✓' if result.cf_clearance_present else '✗'} "
                f"oai-sc={'✓' if result.oai_sc_present else '✗'}"
            )
            if not result.status_code:
                self._set_error(
                    "auth_browser_finalize_unavailable: "
                    + str(result.error or "create_account 浏览器请求未完成")[:300]
                )
                return None

            response_text = result.response_text or ""
            if not result.ok:
                lowered = response_text.lower()
                if (
                    result.status_code == 400
                    and (
                        "account already exists" in lowered
                        or "please login instead" in lowered
                        or "user_already_exists" in lowered
                    )
                ):
                    self._about_you_existing_account_detected = True
                    self._log(
                        "about_you 返回 user_already_exists，说明账号已存在；"
                        "当前 OAuth 会话仍停留在补注册路径，准备重开一次既有账号登录恢复"
                    )
                    return None

                self._set_error(
                    f"about_you 提交失败: {result.status_code} - {response_text[:180]}"
                )
                return None

            data = result.response_json or {}

            flow_state = self._state_from_payload(
                data,
                current_url=result.response_url or request_url,
            )
            if self._state_is_add_phone(flow_state):
                raw_text = response_text
                try:
                    raw_json = json.dumps(data, ensure_ascii=False)
                except Exception:
                    raw_json = ""
                if raw_text:
                    self._log("add_phone 触发响应体(raw): " + raw_text)
                if raw_json and raw_json != raw_text:
                    self._log("add_phone 触发响应体(json): " + raw_json)
            self._log(f"about_you 提交成功 {describe_flow_state(flow_state)}")
            self._check_stop()
            return flow_state
        except Exception as e:
            if self._is_stop_exception(e):
                self._raise_stop(e)
            self._set_error(f"about_you 提交异常: {e}")
            return None

    def _recreate_session(self):
        """重建会话，确保恢复链路使用全新 cookie 容器。"""
        self.session = curl_requests.Session()
        if self.proxy:
            self.session.proxies = build_requests_proxy_config(self.proxy)
        if self.browser_fingerprint:
            apply_browser_fingerprint(self.session, self.browser_fingerprint)

    def login_and_get_tokens(
        self,
        email,
        password,
        device_id,
        user_agent=None,
        sec_ch_ua=None,
        impersonate=None,
        browser_fingerprint=None,
        skymail_client=None,
        prefer_passwordless_login=False,
        allow_phone_verification=True,
        allow_add_phone_verification=None,
        allow_existing_phone_verification=None,
        phone_sms_probe_only=False,
        force_new_browser=False,
        force_password_login=False,
        force_chatgpt_entry=False,
        screen_hint="login",
        complete_about_you_if_needed=False,
        first_name="",
        last_name="",
        birthdate="",
        login_source="",
        stop_after_login=False,
        workspace_scope_preference="free",
        allow_add_phone_session_recovery=True,
        _recovery_depth=0,
    ):
        """
        完整的 OAuth 登录流程，获取 tokens

        Args:
            email: 邮箱
            password: 密码
            device_id: 设备 ID
            user_agent: User-Agent
            sec_ch_ua: sec-ch-ua header
            impersonate: curl_cffi impersonate 参数
            skymail_client: Skymail 客户端（用于获取 OTP，如果需要）
            prefer_passwordless_login: 是否强制走 passwordless OTP 链路
            allow_phone_verification: 兼容旧开关；未单独指定时同时控制 add_phone 新绑和已绑定手机号二次验证
            allow_add_phone_verification: add_phone 新绑/未接码账号是否允许自动接码绑定
            allow_existing_phone_verification: 已绑定手机号二次验证是否允许从手机号池按完整号码自动接码
            phone_sms_probe_only: add_phone 发码并收到短信后停止，不提交 OTP
            force_password_login: 即使 prefer_passwordless_login=true，也强制走密码登录
            force_chatgpt_entry: 在 OAuth 前先走 ChatGPT 首页 -> CSRF -> signin/openai
            complete_about_you_if_needed: 命中 about_you 后是否自动提交资料完成注册
            screen_hint: authorize/continue 的 screen_hint（login/signup）
            first_name: about_you 名字
            last_name: about_you 姓氏
            birthdate: about_you 生日，格式 YYYY-MM-DD
            login_source: 当前登录场景，仅用于日志
            allow_add_phone_session_recovery: allow_add_phone_verification=false 时是否允许内部重启一次 add_phone OAuth session

        Returns:
            dict: tokens 字典，包含 access_token, refresh_token, id_token
        """
        self._check_stop()
        self.last_error = ""
        self.last_workspace_id = ""
        self.last_workspace_candidates = []
        self.last_organization_candidates = []
        self.last_organization_continue_url = ""
        self.last_state = FlowState()
        self._about_you_existing_account_detected = False
        self._about_you_should_skip_create_account = False
        allow_phone_verification = self._coerce_bool(allow_phone_verification, default=True)
        if allow_add_phone_verification is None:
            allow_add_phone_verification = allow_phone_verification
        else:
            allow_add_phone_verification = self._coerce_bool(allow_add_phone_verification, default=False)
        if allow_existing_phone_verification is None:
            allow_existing_phone_verification = allow_phone_verification
        else:
            allow_existing_phone_verification = self._coerce_bool(allow_existing_phone_verification, default=True)
        phone_sms_probe_only = self._coerce_bool(phone_sms_probe_only, default=False)
        self._log(
            "开始 OAuth 登录流程..."
            + (f" (source={login_source})" if login_source else "")
        )
        self._log(
            "OAuth 策略: "
            f"prefer_passwordless_login={'on' if prefer_passwordless_login else 'off'}, "
            f"allow_phone_verification={'on' if allow_phone_verification else 'off'}, "
            f"allow_add_phone={'on' if allow_add_phone_verification else 'off'}, "
            f"allow_existing_phone_otp={'on' if allow_existing_phone_verification else 'off'}, "
            f"phone_sms_probe_only={'on' if phone_sms_probe_only else 'off'}, "
            f"complete_about_you_if_needed={'on' if complete_about_you_if_needed else 'off'}, "
            f"force_new_browser={'on' if force_new_browser else 'off'}, "
            f"force_password_login={'on' if force_password_login else 'off'}, "
            f"force_chatgpt_entry={'on' if force_chatgpt_entry else 'off'}, "
            f"screen_hint={screen_hint or 'login'}, "
            f"stop_after_login={'on' if stop_after_login else 'off'}, "
            f"workspace_scope={str(workspace_scope_preference or 'auto').strip() or 'auto'}"
        )

        if browser_fingerprint is not None:
            self.browser_fingerprint = coerce_browser_fingerprint(browser_fingerprint)
            if not device_id:
                device_id = self.browser_fingerprint.device_id
            user_agent = user_agent or self.browser_fingerprint.user_agent
            sec_ch_ua = sec_ch_ua or self.browser_fingerprint.sec_ch_ua
            impersonate = impersonate or self.browser_fingerprint.impersonate

        if force_new_browser:
            self._log("force_new_browser: 重新创建 OAuth 会话容器")
            self._recreate_session()
            if device_id:
                self._log(f"force_new_browser: 复用上游 device_id={device_id}")
            else:
                device_id = str(uuid.uuid4())
                self._log(f"force_new_browser: 新 device_id={device_id}")
        else:
            if not device_id:
                device_id = str(uuid.uuid4())
                self._log(f"OAuth device_id 缺失，已生成新的 device_id={device_id}")

        user_agent, sec_ch_ua, impersonate = self._ensure_oauth_fingerprint(
            user_agent, sec_ch_ua, impersonate, device_id=device_id
        )

        code_verifier, code_challenge = generate_pkce()
        oauth_state = secrets.token_urlsafe(32)
        authorize_params = {
            "response_type": "code",
            "client_id": self.oauth_client_id,
            "redirect_uri": self.oauth_redirect_uri,
            "scope": "openid profile email offline_access",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": oauth_state,
        }
        authorize_url = f"{self.oauth_issuer}/oauth/authorize"

        seed_oai_device_cookie(self.session, device_id)

        if force_chatgpt_entry:
            self._log("force_chatgpt_entry: 启动 ChatGPT 首页链路（不影响 OAuth PKCE）")
            _ = self._bootstrap_chatgpt_entry(
                email,
                device_id,
                user_agent=user_agent,
                sec_ch_ua=sec_ch_ua,
                impersonate=impersonate,
            )

        self._log("步骤1: Bootstrap OAuth session...")
        self._check_stop()
        authorize_final_url = self._bootstrap_oauth_session(
            authorize_url,
            authorize_params,
            device_id=device_id,
            user_agent=user_agent,
            sec_ch_ua=sec_ch_ua,
            impersonate=impersonate,
        )
        if not authorize_final_url:
            self._set_error("Bootstrap 失败")
            return None

        continue_referer = (
            authorize_final_url
            if authorize_final_url.startswith(self.oauth_issuer)
            else f"{self.oauth_issuer}/log-in"
        )

        state = self._submit_authorize_continue(
            email,
            device_id,
            continue_referer,
            user_agent=user_agent,
            sec_ch_ua=sec_ch_ua,
            impersonate=impersonate,
            authorize_url=authorize_url,
            authorize_params=authorize_params,
            screen_hint=str(screen_hint or "login"),
        )
        if not state:
            if not self.last_error:
                self._set_error("提交邮箱后未进入有效的 OAuth 状态")
            return None

        self._log(f"OAuth 状态起点: {describe_flow_state(state)}")
        seen_states = {}
        referer = continue_referer

        def _should_stop_after_login(state_to_check: FlowState):
            if not stop_after_login:
                return False
            target = f"{state_to_check.continue_url} {state_to_check.current_url}".lower()
            if state_to_check.page_type in {
                "external_url",
                "callback",
                "oauth_callback",
                "chatgpt_home",
            }:
                return True
            if "chatgpt.com/api/auth/callback/" in target:
                return True
            if "localhost:1455/auth/callback" in target:
                return True
            return False

        for step in range(20):
            self._check_stop()
            self.last_state = state
            self._log(f"状态步进[{step + 1}/20]: {describe_flow_state(state)}")
            signature = self._state_signature(state)
            seen_states[signature] = seen_states.get(signature, 0) + 1
            if seen_states[signature] > 2:
                self._set_error(f"OAuth 状态卡住: {describe_flow_state(state)}")
                return None

            code = self._extract_code_from_state(state)
            if code:
                self._log("获取到 authorization code: [REDACTED_OTP]")
                self._log("步骤7: POST /oauth/token")
                self._check_stop()
                tokens = self._exchange_code_for_tokens(
                    code, code_verifier, user_agent, impersonate
                )
                if tokens:
                    self._log("✅ OAuth 登录成功")
                else:
                    self._log("换取 tokens 失败")
                return tokens

            if self._state_is_create_account_password(state) and force_password_login:
                self._log("命中 create_account_password，按强制密码登录路径继续")
                next_state = self._submit_password_verify(
                    email,
                    password,
                    device_id,
                    user_agent=user_agent,
                    sec_ch_ua=sec_ch_ua,
                    impersonate=impersonate,
                    referer=state.current_url or state.continue_url or f"{self.oauth_issuer}/log-in/password",
                )
                if not next_state:
                    if not self.last_error:
                        self._set_error("密码验证后未进入下一步 OAuth 状态")
                    return None
                self._check_stop()
                if _should_stop_after_login(next_state):
                    self._log(
                        "登录链路已完成（密码验证后进入下一状态），按要求停止"
                    )
                    self.last_state = next_state
                    self._set_error("登录链路已完成，按要求停止")
                    return None
                referer = state.current_url or referer
                state = next_state
                continue

            if self._state_is_login_password(state):
                next_state = self._advance_existing_account_login(
                    email,
                    password,
                    device_id,
                    user_agent=user_agent,
                    sec_ch_ua=sec_ch_ua,
                    impersonate=impersonate,
                    referer=state.current_url or state.continue_url or referer,
                    prefer_passwordless_login=prefer_passwordless_login,
                    force_password_login=force_password_login,
                )
                if not next_state:
                    if not self.last_error:
                        self._set_error("已有账号登录后未进入下一步 OAuth 状态")
                    return None
                self._check_stop()
                if _should_stop_after_login(next_state):
                    self._log(
                        "登录链路已完成（密码验证后进入下一状态），按要求停止"
                    )
                    self.last_state = next_state
                    self._set_error("登录链路已完成，按要求停止")
                    return None
                referer = state.current_url or referer
                state = next_state
                continue

            if (
                prefer_passwordless_login
                and self._state_is_add_phone(state)
                and self._state_requires_navigation(state)
                and not allow_add_phone_verification
            ):
                self._log("步骤5: OTP 后命中 add_phone，先实际访问 continue_url 争取重签 workspace Cookie")
                code, next_state = self._follow_flow_state(
                    state,
                    referer=referer,
                    user_agent=user_agent,
                    impersonate=impersonate,
                )
                if code:
                    self._log("获取到 authorization code: [REDACTED_OTP]")
                    self._log("步骤7: POST /oauth/token")
                    self._check_stop()
                    tokens = self._exchange_code_for_tokens(
                        code, code_verifier, user_agent, impersonate
                    )
                    if tokens:
                        self._log("✅ OAuth 登录成功")
                    else:
                        self._log("换取 tokens 失败")
                    return tokens
                referer = state.current_url or referer
                state = next_state
                continue

            # add_phone responses may retain the preceding email-otp URL in
            # current_url. Once OpenAI declares add_phone, that explicit state
            # must win over the stale URL marker.
            if self._state_is_email_otp(state) and not self._state_is_add_phone(state):
                if not skymail_client:
                    self._set_error("当前流程需要邮箱 OTP，但缺少接码客户端")
                    return None
                next_state = self._handle_otp_verification(
                    email,
                    device_id,
                    user_agent,
                    sec_ch_ua,
                    impersonate,
                    skymail_client,
                    state,
                    scope_log_prefix="[free]",
                )
                if not next_state:
                    if not self.last_error:
                        self._set_error("邮箱 OTP 验证后未进入下一步 OAuth 状态")
                    return None
                self._check_stop()
                if _should_stop_after_login(next_state):
                    self._log(
                        "登录链路已完成（OTP 验证后进入下一状态），按要求停止"
                    )
                    self.last_state = next_state
                    self._set_error("登录链路已完成，按要求停止")
                    return None
                referer = state.current_url or referer
                state = next_state
                continue

            if complete_about_you_if_needed and self._state_is_about_you(state):
                self._log("步骤5: 命中 about_you，执行 interrupt 新链路的资料补全提交")
                next_state = self._submit_about_you_create_account(
                    first_name,
                    last_name,
                    birthdate,
                    device_id,
                    user_agent=user_agent,
                    sec_ch_ua=sec_ch_ua,
                    impersonate=impersonate,
                    referer=state.current_url or state.continue_url or referer,
                )
                if not next_state:
                    if self._about_you_existing_account_detected and _recovery_depth < 1:
                        self._log(
                            "about_you 命中既有账号，重开一次全新 OAuth 会话，切换到既有账号恢复路径"
                        )
                        self._recreate_session()
                        return self.login_and_get_tokens(
                            email,
                            password,
                            device_id,
                            user_agent=user_agent,
                            sec_ch_ua=sec_ch_ua,
                            impersonate=impersonate,
                            browser_fingerprint=self.browser_fingerprint,
                            skymail_client=skymail_client,
                            prefer_passwordless_login=prefer_passwordless_login,
                            allow_phone_verification=allow_phone_verification,
                            allow_add_phone_verification=allow_add_phone_verification,
                            allow_existing_phone_verification=allow_existing_phone_verification,
                            force_new_browser=True,
                            force_password_login=force_password_login,
                            force_chatgpt_entry=force_chatgpt_entry,
                            screen_hint="login",
                            complete_about_you_if_needed=False,
                            first_name=first_name,
                            last_name=last_name,
                            birthdate=birthdate,
                            login_source=(
                                f"{login_source}:existing_account_after_about_you"
                                if login_source
                                else "existing_account_after_about_you"
                            ),
                            stop_after_login=stop_after_login,
                            workspace_scope_preference=workspace_scope_preference,
                            allow_add_phone_session_recovery=allow_add_phone_session_recovery,
                            _recovery_depth=_recovery_depth + 1,
                        )
                    if not self.last_error:
                        self._set_error("about_you 提交后未进入下一步 OAuth 状态")
                    return None
                self._check_stop()
                referer = state.current_url or referer
                state = next_state
                continue

            if self._state_is_add_phone(state):
                try:
                    raw_dump = json.dumps(state.raw or {}, ensure_ascii=False)
                except Exception:
                    raw_dump = ""
                if raw_dump:
                    self._log(f"add_phone 状态响应体(raw): {raw_dump}")
                if not allow_add_phone_verification:
                    self._record_phone_challenge_event(
                        challenge_type="add_phone",
                        status="unbound_required",
                        source=login_source or "oauth_login",
                        message="命中 add_phone，账号尚未绑定手机号，未启用自动新绑",
                        allow_add_phone_verification=allow_add_phone_verification,
                        allow_existing_phone_verification=allow_existing_phone_verification,
                    )
                    if self._state_supports_workspace_resolution(state):
                        self._log(
                            "步骤5: add_phone 命中，但检测到 workspace 线索，继续尝试 workspace/org 选择"
                        )
                    elif prefer_passwordless_login and allow_add_phone_session_recovery and _recovery_depth < 1:
                        self._log(
                            "步骤5: add_phone 新绑开关关闭，且仍无 workspace/callback，重启一次全新 OAuth session + 新 PKCE"
                        )
                        self._recreate_session()
                        return self.login_and_get_tokens(
                            email,
                            password,
                            device_id,
                            user_agent=user_agent,
                            sec_ch_ua=sec_ch_ua,
                            impersonate=impersonate,
                            browser_fingerprint=self.browser_fingerprint,
                            skymail_client=skymail_client,
                            prefer_passwordless_login=prefer_passwordless_login,
                            allow_phone_verification=allow_phone_verification,
                            allow_add_phone_verification=allow_add_phone_verification,
                            allow_existing_phone_verification=allow_existing_phone_verification,
                            complete_about_you_if_needed=complete_about_you_if_needed,
                            first_name=first_name,
                            last_name=last_name,
                            birthdate=birthdate,
                            login_source=(
                                f"{login_source}:add_phone_recovery"
                                if login_source
                                else "add_phone_recovery"
                            ),
                            workspace_scope_preference=workspace_scope_preference,
                            allow_add_phone_session_recovery=allow_add_phone_session_recovery,
                            _recovery_depth=_recovery_depth + 1,
                        )
                    else:
                        self._set_error(
                            "passwordless 登录后仍停留在 add_phone，且 add_phone 新绑开关关闭，未获取到 workspace / callback"
                        )
                        return None
                else:
                    next_state = self._handle_add_phone_verification(
                        device_id,
                        user_agent,
                        sec_ch_ua,
                        impersonate,
                        state,
                        email=email,
                        sms_probe_only=phone_sms_probe_only,
                    )
                    if not next_state:
                        if not self.last_error:
                            self._set_error("手机号验证后未进入下一步 OAuth 状态")
                        return None
                    self._check_stop()
                    referer = state.current_url or referer
                    state = next_state
                    continue

            if self._state_is_existing_phone_otp(state):
                next_state = self._handle_existing_phone_otp_verification(
                    device_id,
                    user_agent,
                    sec_ch_ua,
                    impersonate,
                    state,
                    allow_existing_phone_verification=allow_existing_phone_verification,
                )
                if not next_state:
                    return None
                self._check_stop()
                referer = state.current_url or referer
                state = next_state
                continue

            if self._state_requires_navigation(state):
                code, next_state = self._follow_flow_state(
                    state,
                    referer=referer,
                    user_agent=user_agent,
                    impersonate=impersonate,
                )
                if code:
                    self._log("获取到 authorization code: [REDACTED_OTP]")
                    self._log("步骤7: POST /oauth/token")
                    self._check_stop()
                    tokens = self._exchange_code_for_tokens(
                        code, code_verifier, user_agent, impersonate
                    )
                    if tokens:
                        self._log("✅ OAuth 登录成功")
                    else:
                        self._log("换取 tokens 失败")
                    return tokens
                referer = state.current_url or referer
                state = next_state
                self._log(f"follow state -> {describe_flow_state(state)}")
                continue

            if self._state_supports_workspace_resolution(state):
                code, next_state = self.resolve_codex_workspace(
                    state,
                    device_id,
                    user_agent,
                    impersonate,
                    workspace_scope_preference=workspace_scope_preference,
                    authorize_url=authorize_url,
                    authorize_params=authorize_params,
                )
                if code:
                    self._log("获取到 authorization code: [REDACTED_OTP]")
                    self._log("步骤7: POST /oauth/token")
                    self._check_stop()
                    tokens = self._exchange_code_for_tokens(
                        code, code_verifier, user_agent, impersonate
                    )
                    if tokens:
                        self._log("✅ OAuth 登录成功")
                    else:
                        self._log("换取 tokens 失败")
                    return tokens
                if next_state:
                    referer = state.current_url or referer
                    state = next_state
                    self._log(f"workspace state -> {describe_flow_state(state)}")
                    continue

                if not self.last_error:
                    self._set_error(
                        f"workspace/org 选择失败: {describe_flow_state(state)}"
                    )
                return None

            self._set_error(f"未支持的 OAuth 状态: {describe_flow_state(state)}")
            return None

        self._set_error("OAuth 状态机超出最大步数")
        return None

    def _extract_code_from_url(self, url):
        """从 URL 中提取 code"""
        if not url or "code=" not in url:
            return None
        try:
            return parse_qs(urlparse(url).query).get("code", [None])[0]
        except Exception:
            return None

    def _oauth_follow_for_code(
        self, start_url, referer, user_agent, impersonate, max_hops=16
    ):
        """跟随 URL 获取 authorization code（手动跟随重定向）"""
        code, next_state = self._follow_flow_state(
            self._state_from_url(start_url),
            referer=referer,
            user_agent=user_agent,
            impersonate=impersonate,
            max_hops=max_hops,
        )
        return code, (next_state.current_url or next_state.continue_url or start_url)

    @staticmethod
    def _normalize_workspace_scope_preference(value):
        normalized = str(value or "").strip().lower().replace("-", "_")
        if normalized in {"free", "personal", "default", "personal_free"}:
            return "free"
        return ""

    def _classify_workspace_candidate(self, workspace):
        if not isinstance(workspace, dict):
            return ""

        raw_values = []
        for key in (
            "kind",
            "type",
            "plan_type",
            "workspace_type",
            "subscription_plan",
            "name",
            "title",
            "display_name",
            "label",
            "slug",
        ):
            value = workspace.get(key)
            if value not in (None, ""):
                raw_values.append(str(value).strip().lower())

        if workspace.get("is_default") is True:
            raw_values.append("is_default")

        joined = " | ".join(raw_values)
        if any(token in joined for token in ("team", "business", "enterprise")):
            return "business"
        if any(token in joined for token in ("free", "personal", "individual")):
            return "free"
        if workspace.get("is_default") is True:
            return "free"
        return ""

    def _pick_workspace_candidate(self, workspaces, scope_preference):
        items = list(workspaces or [])
        if not items:
            return None
        classified = [(item, self._classify_workspace_candidate(item)) for item in items]
        for item, scope in classified:
            if scope == "free":
                return item
        return None

    def resolve_codex_workspace(
        self,
        state: FlowState,
        device_id,
        user_agent,
        impersonate,
        workspace_scope_preference="free",
        authorize_url=None,
        authorize_params=None,
    ):
        """解析并提交 Codex workspace / org，返回 code 或下一状态。"""
        self._log("步骤6: 解析 Codex workspace / org / code")
        consent_entry = (
            state.continue_url
            or state.current_url
            or f"{self.oauth_issuer}/sign-in-with-chatgpt/codex/consent"
        )
        if self._state_is_add_phone(state):
            consent_entry = f"{self.oauth_issuer}/sign-in-with-chatgpt/codex/consent"
            self._log("步骤6: 当前处于 add_phone，改用 canonical consent URL 继续")
        if self._state_is_codex_organization(state):
            orgs = self._extract_orgs_from_payload_object(state.raw)
            if not orgs:
                orgs = list(self.last_organization_candidates or [])
            if not orgs:
                orgs = self._load_organization_page_orgs(
                    consent_entry,
                    user_agent=user_agent,
                    impersonate=impersonate,
                )
            if orgs:
                return self._oauth_submit_organization_selection(
                    orgs,
                    organization_url=consent_entry,
                    fallback_referer=consent_entry,
                    device_id=device_id,
                    user_agent=user_agent,
                    impersonate=impersonate,
                )
            self._set_error(
                f"organization 状态缺少 org/project 信息: {describe_flow_state(state)}"
            )
            return None, None
        return self._oauth_submit_workspace_and_org(
            consent_entry,
            device_id,
            user_agent,
            impersonate,
            workspace_scope_preference=workspace_scope_preference,
            authorize_url=authorize_url,
            authorize_params=authorize_params,
        )


    def _state_is_codex_organization(self, state: FlowState):
        target = f"{state.page_type} {state.continue_url} {state.current_url}".lower()
        return (
            state.page_type in {
                "organization_selection",
                "sign_in_with_chatgpt_codex_org",
                "sign_in_with_chatgpt_codex_organization",
            }
            or "codex/organization" in target
            or "codex_org" in target
            or "codex_organization" in target
        )

    def _auth_error_info_from_response(self, response):
        info = {
            "status_code": getattr(response, "status_code", None),
            "error_type": "",
            "error_code": "",
            "error_message": "",
            "data": {},
            "text": "",
        }
        try:
            data = response.json()
        except Exception:
            data = None
        if isinstance(data, dict):
            info["data"] = data
            error = data.get("error") if isinstance(data.get("error"), dict) else {}
            info["error_type"] = str(error.get("type") or "").strip()
            info["error_code"] = str(error.get("code") or "").strip()
            info["error_message"] = str(error.get("message") or "").strip()
        if not info["error_message"]:
            try:
                info["text"] = str(response.text or "")[:300]
            except Exception:
                info["text"] = ""
        return info

    def _format_auth_error_info(self, label, info):
        parts = [f"{label} 失败: HTTP {info.get('status_code') or '-'}"]
        if info.get("error_code"):
            parts.append(f"code={info.get('error_code')}")
        if info.get("error_type"):
            parts.append(f"type={info.get('error_type')}")
        message = str(info.get("error_message") or info.get("text") or "").strip()
        if message:
            parts.append(f"message={message[:240]}")
        return ", ".join(parts)

    def _set_terminal_otp_error_if_needed(self, label, response) -> bool:
        info = self._auth_error_info_from_response(response)
        message = str(info.get("error_message") or info.get("text") or "").strip()
        lowered = message.lower()
        if is_account_deactivated_message(info.get("error_code"), message):
            self._set_error(f"account_deactivated: {message or self._format_auth_error_info(label, info)}")
            return True
        if (
            "too many tries" in lowered
            or "too many attempts" in lowered
            or "please wait a few minutes" in lowered
            or "rate limit" in lowered
            or "rate_limited" in lowered
        ):
            detail = message or self._format_auth_error_info(label, info)
            self._set_error(f"otp_rate_limited: OpenAI OTP 校验次数过多，当前邮箱进入冷却，稍后重试。{detail}")
            return True
        return False

    @staticmethod
    def _is_fatal_mailbox_config_error(exc) -> bool:
        text = str(exc or "").lower()
        if "helper_ready_api" in text and "lease_id" in text:
            return True
        if "hme ready api" in text:
            return any(
                marker in text
                for marker in (
                    "status=401",
                    "status=403",
                    "status=404",
                    "invalid api_key",
                    "missing api_key",
                    "lease_not_found",
                    "alias_not_found",
                    "checkout_id",
                    "alias_id",
                    "当前任务缺少 lease_id",
                    "helper ready api 未配置",
                )
            )
        if "tempmail ready api" in text:
            return (
                "401" in text
                or "invalid api_key" in text
                or "missing api_key" in text
                or "tempmail_api_key" in text
            )

        mailbox_markers = (
            "api/mailboxes",
            "tempmail",
            "邮箱服务",
            "mailbox",
        )
        connection_markers = (
            "connection refused",
            "failed to establish a new connection",
            "max retries exceeded",
            "name or service not known",
            "temporary failure in name resolution",
            "connection timed out",
            "connect timeout",
            "read timed out",
            "network is unreachable",
        )
        return any(marker in text for marker in mailbox_markers) and any(
            marker in text for marker in connection_markers
        )

    def _is_duplicate_default_project_error(self, info):
        haystack = " ".join(
            str(info.get(key) or "")
            for key in ("error_code", "error_type", "error_message", "text")
        ).lower()
        return "duplicate" in haystack or "default project" in haystack

    def _is_invalid_session_error(self, info):
        haystack = " ".join(
            str(info.get(key) or "")
            for key in ("error_code", "error_type", "error_message", "text")
        ).lower()
        return "invalid_state" in haystack or "invalid session" in haystack

    def _is_no_valid_organizations_error(self, info):
        haystack = " ".join(
            str(info.get(key) or "")
            for key in ("error_code", "error_type", "error_message", "text")
        ).lower()
        return "no_valid_organizations" in haystack or "no valid organizations" in haystack

    def _coerce_positive_delay_list(self, value, default):
        if value is None:
            return list(default)
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return list(default)
            items = raw.replace("，", ",").replace(";", ",").split(",")
        elif isinstance(value, (list, tuple, set)):
            items = list(value)
        else:
            items = [value]

        delays = []
        for item in items:
            try:
                delay = int(float(str(item).strip()))
            except Exception:
                continue
            if delay > 0:
                delays.append(min(delay, 120))
        return delays

    def _workspace_select_no_org_retry_delays(self):
        raw = self.config.get("chatgpt_workspace_select_no_org_retry_delays_seconds")
        if raw in (None, ""):
            raw = self.config.get(
                "chatgpt_workspace_select_no_valid_organizations_retry_delays_seconds"
            )
        return self._coerce_positive_delay_list(raw, [5, 10, 20])

    def _normalize_org_candidate(self, item):
        if not isinstance(item, dict):
            return None
        org_id = str(
            item.get("id")
            or item.get("org_id")
            or item.get("organization_id")
            or ""
        ).strip()
        if not org_id.startswith("org-"):
            return None

        projects = []
        seen_projects = set()

        def add_project(project):
            if isinstance(project, dict):
                project_id = str(
                    project.get("id")
                    or project.get("project_id")
                    or project.get("default_project_id")
                    or ""
                ).strip()
                title = str(project.get("title") or project.get("name") or "").strip()
            else:
                project_id = str(project or "").strip()
                title = ""
            if not project_id or project_id in seen_projects:
                return
            seen_projects.add(project_id)
            payload = {"id": project_id}
            if title:
                payload["title"] = title
            projects.append(payload)

        raw_projects = item.get("projects")
        if isinstance(raw_projects, list):
            for project in raw_projects:
                add_project(project)
        for key in ("project_id", "default_project_id", "initial_project_id"):
            if item.get(key):
                add_project({"id": item.get(key)})

        normalized = dict(item)
        normalized["id"] = org_id
        if projects:
            normalized["projects"] = projects
        return normalized

    def _extract_orgs_from_payload_object(self, payload):
        found = []
        seen = set()

        def add_candidate(item):
            normalized = self._normalize_org_candidate(item)
            if not normalized:
                return
            org_id = normalized.get("id")
            if org_id in seen:
                return
            seen.add(org_id)
            found.append(normalized)

        def walk(obj, depth=0):
            if depth > 8:
                return
            if isinstance(obj, dict):
                orgs = obj.get("orgs")
                if isinstance(orgs, list):
                    for org in orgs:
                        add_candidate(org)
                add_candidate(obj)
                for value in obj.values():
                    walk(value, depth + 1)
            elif isinstance(obj, list):
                for value in obj:
                    walk(value, depth + 1)

        walk(payload)
        return found

    def _extract_orgs_from_text(self, text):
        if not text:
            return []
        import re

        normalized = str(text).replace('\\"', '"')
        org_ids = []
        for org_id in re.findall(r"org-[A-Za-z0-9]+", normalized):
            if org_id not in org_ids:
                org_ids.append(org_id)
        project_ids = []
        for project_id in re.findall(r"proj[_-][A-Za-z0-9]+", normalized):
            if project_id not in project_ids:
                project_ids.append(project_id)
        if not org_ids:
            return []
        projects = [{"id": project_id} for project_id in project_ids[:3]]
        return [{"id": org_ids[0], "projects": projects}]

    def _load_organization_page_orgs(self, organization_url, user_agent, impersonate):
        candidates = []
        for url in (
            organization_url,
            self.last_organization_continue_url,
            f"{self.oauth_issuer}/sign-in-with-chatgpt/codex/organization",
        ):
            url = str(url or "").strip()
            if url and url not in candidates:
                candidates.append(url)

        for url in candidates:
            try:
                headers = self._headers(
                    url,
                    user_agent=user_agent,
                    accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    referer=f"{self.oauth_issuer}/sign-in-with-chatgpt/codex/consent",
                    navigation=True,
                )
                kwargs = {"headers": headers, "allow_redirects": False, "timeout": 30}
                if impersonate:
                    kwargs["impersonate"] = impersonate
                self._browser_pause(0.12, 0.3)
                response = self.session.get(url, **kwargs)
                location = normalize_flow_url(
                    response.headers.get("Location", ""), auth_base=self.oauth_issuer
                )
                content_type = (response.headers.get("content-type", "") or "").lower()
                self._log(
                    f"organization 页面请求 -> {response.status_code}, url={str(response.url)[:120]}, "
                    f"location={location[:120] if location else ''}, content-type={content_type[:80]}"
                )
                data = None
                if "application/json" in content_type:
                    try:
                        data = response.json()
                    except Exception:
                        data = None
                orgs = self._extract_orgs_from_payload_object(data) if data else []
                if not orgs and response.status_code == 200:
                    orgs = self._extract_orgs_from_text(response.text)
                if orgs:
                    self.last_organization_candidates = list(orgs)
                    self.last_organization_continue_url = url
                    self._log(f"organization 页面提取到 {len(orgs)} 个 org 候选")
                    return orgs
            except Exception as exc:
                if self._is_stop_exception(exc):
                    self._raise_stop(exc)
                self._log(f"organization 页面解析异常: {exc}")
        return []

    def _pick_organization_candidate(self, orgs):
        for org in orgs or []:
            normalized = self._normalize_org_candidate(org)
            if not normalized:
                continue
            projects = normalized.get("projects") if isinstance(normalized.get("projects"), list) else []
            project_id = ""
            if projects:
                first_project = projects[0] if isinstance(projects[0], dict) else {}
                project_id = str(
                    (first_project or {}).get("id")
                    or (first_project or {}).get("project_id")
                    or ""
                ).strip()
            return str(normalized.get("id") or "").strip(), project_id
        return "", ""

    def _oauth_submit_organization_selection(
        self,
        orgs,
        organization_url,
        fallback_referer,
        device_id,
        user_agent,
        impersonate,
        scope_log_prefix="",
    ):
        org_id, project_id = self._pick_organization_candidate(orgs)
        if not org_id:
            self._set_error("organization 候选为空，无法提交 organization/select")
            return None, None

        self.last_organization_candidates = list(orgs or [])
        if organization_url:
            self.last_organization_continue_url = str(organization_url)
        self._log(f"选择 organization: {org_id}")

        org_body = {"org_id": org_id}
        if project_id:
            org_body["project_id"] = project_id

        org_referer = (
            organization_url
            if str(organization_url or "").startswith("http")
            else fallback_referer
        )
        headers = self._headers(
            f"{self.oauth_issuer}/api/accounts/organization/select",
            user_agent=user_agent,
            accept="application/json",
            referer=org_referer,
            origin=self.oauth_issuer,
            content_type="application/json",
            fetch_site="same-origin",
            extra_headers={"oai-device-id": device_id},
        )
        headers.update(generate_datadog_trace())

        kwargs = {
            "json": org_body,
            "headers": headers,
            "allow_redirects": False,
            "timeout": 30,
        }
        if impersonate:
            kwargs["impersonate"] = impersonate

        try:
            self._check_stop()
            self._browser_pause()
            r_org = self.session.post(
                f"{self.oauth_issuer}/api/accounts/organization/select",
                **kwargs,
            )
            self._check_stop()
        except Exception as exc:
            if self._is_stop_exception(exc):
                self._raise_stop(exc)
            self._set_error(f"organization/select 异常: {exc}")
            return None, None

        self._log(f"organization/select -> {r_org.status_code}")

        if r_org.status_code in (301, 302, 303, 307, 308):
            location = normalize_flow_url(
                r_org.headers.get("Location", ""), auth_base=self.oauth_issuer
            )
            if "code=" in location:
                code = self._extract_code_from_url(location)
                if code:
                    self._log("从 organization/select 重定向获取到 code")
                    return code, self._state_from_url(location)
            if location:
                return None, self._state_from_url(location)

        if r_org.status_code == 200:
            try:
                org_state = self._state_from_payload(
                    r_org.json(), current_url=str(r_org.url)
                )
                self._log(f"organization/select -> {describe_flow_state(org_state)}")
                code = self._extract_code_from_state(org_state)
                if code:
                    return code, org_state
                return None, org_state
            except Exception as exc:
                if self._is_stop_exception(exc):
                    self._raise_stop(exc)
                self._set_error(f"解析 organization/select 响应异常: {exc}")
                return None, None

        error_info = self._auth_error_info_from_response(r_org)
        self._set_error(self._format_auth_error_info("organization/select", error_info))
        return None, None

    def _oauth_authorize_url_with_params(self, base_url, authorize_params, workspace_id=""):
        params = dict(authorize_params or {})
        if workspace_id:
            params["workspace_id"] = str(workspace_id).strip()
        query = urlencode(params)
        if not query:
            return str(base_url or "")
        separator = "&" if "?" in str(base_url or "") else "?"
        return f"{base_url}{separator}{query}"

    def _oauth_try_workspace_only_authorization(
        self,
        *,
        consent_url,
        authorize_url,
        authorize_params,
        workspace_id,
        device_id,
        user_agent,
        impersonate,
    ):
        """When workspace exists but org selection is empty, try to continue OAuth with the workspace only."""
        workspace_id = str(workspace_id or "").strip()
        if not workspace_id:
            return None, None
        if not authorize_params:
            self._log("workspace-only fallback 缺少 OAuth 授权参数，无法继续")
            return None, None

        auth_url = str(authorize_url or f"{self.oauth_issuer}/oauth/authorize").strip()
        oauth2_url = f"{self.oauth_issuer}/api/oauth/oauth2/auth"
        attempts = [
            ("oauth2+workspace", oauth2_url, True),
            ("authorize+workspace", auth_url, True),
            ("oauth2", oauth2_url, False),
            ("authorize", auth_url, False),
        ]

        seen_urls = set()
        for label, base_url, include_workspace in attempts:
            full_url = self._oauth_authorize_url_with_params(
                base_url,
                authorize_params,
                workspace_id=workspace_id if include_workspace else "",
            )
            if not full_url or full_url in seen_urls:
                continue
            seen_urls.add(full_url)
            self._log(f"workspace-only fallback: 尝试 {label} 继续 OAuth")
            code, next_state = self._follow_flow_state(
                self._state_from_url(full_url),
                referer=consent_url,
                user_agent=user_agent,
                impersonate=impersonate,
            )
            if code:
                self._log("workspace-only fallback 获取到 authorization code")
                return code, next_state
            if next_state and self._state_is_codex_organization(next_state):
                orgs = self._extract_orgs_from_payload_object(next_state.raw)
                if not orgs:
                    orgs = self._load_organization_page_orgs(
                        next_state.continue_url or next_state.current_url,
                        user_agent=user_agent,
                        impersonate=impersonate,
                    )
                if orgs:
                    return self._oauth_submit_organization_selection(
                        orgs,
                        organization_url=next_state.continue_url or next_state.current_url,
                        fallback_referer=consent_url,
                        device_id=device_id,
                        user_agent=user_agent,
                        impersonate=impersonate,
                    )
            if next_state:
                self._log(f"workspace-only fallback state -> {describe_flow_state(next_state)}")
        return None, None

    def _oauth_submit_workspace_and_org(
        self,
        consent_url,
        device_id,
        user_agent,
        impersonate,
        max_retries=3,
        workspace_scope_preference="free",
        authorize_url=None,
        authorize_params=None,
    ):
        """提交 workspace 和 organization 选择（带重试）"""
        session_data = None
        self._check_stop()
        self._log(f"workspace 解析入口: {consent_url}")

        for attempt in range(max_retries):
            self._check_stop()
            session_data = self._load_workspace_session_data(
                consent_url=consent_url,
                user_agent=user_agent,
                impersonate=impersonate,
            )
            if session_data:
                break

            if attempt < max_retries - 1:
                self._log(
                    f"无法获取 consent session 数据 (尝试 {attempt + 1}/{max_retries})"
                )
                self._sleep_with_stop(0.3)
            else:
                self._set_error("无法获取 consent session 数据")
                return None, None

        workspaces = session_data.get("workspaces", [])
        self.last_workspace_candidates = list(workspaces or [])
        if not workspaces:
            try:
                session_keys = sorted((session_data or {}).keys())
            except Exception:
                session_keys = []
            self._log(
                "workspace session 数据为空: "
                f"keys={session_keys}, session_id={str((session_data or {}).get('session_id') or '')[:24]}"
            )
            self._set_error("session 中没有 workspace 信息")
            return None, None

        preferred_scope = "free"
        scope_log_prefix = "[free]"
        self._log(f"{scope_log_prefix} 已读取 {len(workspaces)} 个 workspace")
        selected_workspace = self._pick_workspace_candidate(workspaces, preferred_scope)
        workspace_id = str((selected_workspace or {}).get("id") or "").strip()
        if not workspace_id:
            self._set_error("workspace_id 为空")
            return None, None

        candidate_summaries = []
        for item in workspaces:
            if not isinstance(item, dict):
                continue
            candidate_summaries.append(
                {
                    "id": str(item.get("id") or "")[:8],
                    "kind": str(item.get("kind") or item.get("type") or ""),
                    "name": str(item.get("name") or item.get("title") or item.get("display_name") or ""),
                    "is_default": bool(item.get("is_default")),
                }
            )
        if candidate_summaries:
            self._log(f"workspace 候选: {candidate_summaries}")

        selected_scope = self._classify_workspace_candidate(selected_workspace)
        self.last_workspace_id = str(workspace_id).strip()
        if preferred_scope:
            self._log(
                f"选择 workspace: {workspace_id} (target={preferred_scope}, actual={selected_scope or 'unknown'})"
            )
        else:
            self._log(f"选择 workspace: {workspace_id}")
        self._log("[free] 已选择 personal workspace")

        headers = self._headers(
            f"{self.oauth_issuer}/api/accounts/workspace/select",
            user_agent=user_agent,
            accept="application/json",
            referer=consent_url,
            origin=self.oauth_issuer,
            content_type="application/json",
            fetch_site="same-origin",
            extra_headers={
                "oai-device-id": device_id,
            },
        )
        headers.update(generate_datadog_trace())

        def _post_workspace_select():
            kwargs = {
                "json": {"workspace_id": workspace_id},
                "headers": headers,
                "allow_redirects": False,
                "timeout": 30,
            }
            if impersonate:
                kwargs["impersonate"] = impersonate
            self._browser_pause()
            response = self.session.post(
                f"{self.oauth_issuer}/api/accounts/workspace/select", **kwargs
            )
            self._check_stop()
            return response

        no_org_retry_delays = self._workspace_select_no_org_retry_delays()
        try:
            r = _post_workspace_select()

            self._log(f"workspace/select -> {r.status_code}")

            no_org_retry_count = 0
            while r.status_code not in (200, 301, 302, 303, 307, 308):
                retry_error_info = self._auth_error_info_from_response(r)
                if not (
                    self._is_no_valid_organizations_error(retry_error_info)
                    and workspace_id
                    and no_org_retry_count < len(no_org_retry_delays)
                ):
                    break
                delay_seconds = no_org_retry_delays[no_org_retry_count]
                no_org_retry_count += 1
                self._log(
                    "workspace/select 返回 no_valid_organizations，"
                    f"workspace 已存在，等待 {delay_seconds}s 后重试 "
                    f"({no_org_retry_count}/{len(no_org_retry_delays)})"
                )
                self._sleep_with_stop(delay_seconds)
                session_data = self._load_workspace_session_data(
                    consent_url=consent_url,
                    user_agent=user_agent,
                    impersonate=impersonate,
                )
                workspaces = (session_data or {}).get("workspaces", [])
                if workspaces:
                    self.last_workspace_candidates = list(workspaces or [])
                r = _post_workspace_select()
                self._log(f"workspace/select -> {r.status_code}")

            # 检查重定向
            if r.status_code in (301, 302, 303, 307, 308):
                location = normalize_flow_url(
                    r.headers.get("Location", ""), auth_base=self.oauth_issuer
                )
                if "code=" in location:
                    code = self._extract_code_from_url(location)
                    if code:
                        self._log("从 workspace/select 重定向获取到 code")
                        return code, self._state_from_url(location)
                if location:
                    return None, self._state_from_url(location)

            # 如果返回 200，检查响应中的 orgs
            if r.status_code == 200:
                try:
                    data = r.json()
                    orgs = self._extract_orgs_from_payload_object(data)
                    workspace_state = self._state_from_payload(
                        data, current_url=str(r.url)
                    )
                    continue_url = workspace_state.continue_url

                    if orgs:
                        self.last_organization_candidates = list(orgs)
                        self.last_organization_continue_url = continue_url or ""
                        return self._oauth_submit_organization_selection(
                            orgs,
                            organization_url=continue_url,
                            fallback_referer=consent_url,
                            device_id=device_id,
                            user_agent=user_agent,
                            impersonate=impersonate,
                            scope_log_prefix=scope_log_prefix,
                        )

                    if self._state_is_codex_organization(workspace_state):
                        orgs = self._load_organization_page_orgs(
                            continue_url,
                            user_agent=user_agent,
                            impersonate=impersonate,
                        )
                        if orgs:
                            return self._oauth_submit_organization_selection(
                                orgs,
                                organization_url=continue_url,
                                fallback_referer=consent_url,
                                device_id=device_id,
                                user_agent=user_agent,
                                impersonate=impersonate,
                                scope_log_prefix=scope_log_prefix,
                            )

                    # 如果有 continue_url，跟随它
                    if continue_url:
                        code, _ = self._oauth_follow_for_code(
                            continue_url, consent_url, user_agent, impersonate
                        )
                        if code:
                            return code, self._state_from_url(continue_url)
                    return None, workspace_state

                except Exception as e:
                    if self._is_stop_exception(e):
                        self._raise_stop(e)
                    self._set_error(f"处理 workspace/select 响应异常: {e}")
                    return None, None

            error_info = self._auth_error_info_from_response(r)
            error_summary = self._format_auth_error_info("workspace/select", error_info)
            self._log(error_summary)
            if self._is_no_valid_organizations_error(error_info) and workspace_id:
                self._log(
                    "workspace/select 返回 no_valid_organizations，但 workspace 已存在；"
                    "按 workspace-only 继续尝试 OAuth"
                )
                code, next_state = self._oauth_try_workspace_only_authorization(
                    consent_url=consent_url,
                    authorize_url=authorize_url,
                    authorize_params=authorize_params,
                    workspace_id=workspace_id,
                    device_id=device_id,
                    user_agent=user_agent,
                    impersonate=impersonate,
                )
                if code:
                    return code, next_state
                if next_state and self._state_signature(next_state) != self._state_signature(
                    self._state_from_url(consent_url)
                ):
                    return None, next_state
            if self._is_duplicate_default_project_error(error_info):
                orgs = self._extract_orgs_from_payload_object(error_info.get("data"))
                if not orgs:
                    orgs = list(self.last_organization_candidates or [])
                organization_url = (
                    self.last_organization_continue_url
                    or f"{self.oauth_issuer}/sign-in-with-chatgpt/codex/organization"
                )
                if not orgs:
                    orgs = self._load_organization_page_orgs(
                        organization_url,
                        user_agent=user_agent,
                        impersonate=impersonate,
                    )
                if orgs:
                    self._log("workspace/select 返回 duplicate，改走 organization/select 恢复")
                    return self._oauth_submit_organization_selection(
                        orgs,
                        organization_url=organization_url,
                        fallback_referer=consent_url,
                        device_id=device_id,
                        user_agent=user_agent,
                        impersonate=impersonate,
                        scope_log_prefix=scope_log_prefix,
                    )
                self._set_error(f"{error_summary}; 未找到可提交的 organization/project")
                return None, None
            if self._is_invalid_session_error(error_info):
                self._set_error(f"{error_summary}; 当前 OAuth session 已失效，请重开 OAuth 会话")
                return None, None
            self._set_error(error_summary)
            return None, None

        except Exception as e:
            if self._is_stop_exception(e):
                self._raise_stop(e)
            self._set_error(f"workspace/select 异常: {e}")
            return None, None

        return None, None

    def _load_workspace_session_data(self, consent_url, user_agent, impersonate):
        """优先从 cookie 解码 session，失败时回退到 consent HTML 中提取 workspace 数据。"""
        session_data = self._decode_oauth_session_cookie()
        if session_data and session_data.get("workspaces"):
            self._log(
                f"从 oai-client-auth-session cookie 读取到 {len(session_data.get('workspaces', []))} 个 workspace"
            )
            return session_data
        if session_data:
            self._log(
                "oai-client-auth-session 已存在，但其中没有 workspaces 字段"
            )
        else:
            self._log("当前未从 cookie 解出 oai-client-auth-session workspace 数据")

        html = self._fetch_consent_page_html(consent_url, user_agent, impersonate)
        if not html:
            self._log("consent HTML 为空，无法从页面提取 workspace 数据")
            return session_data

        parsed = self._extract_session_data_from_consent_html(html)
        if parsed and parsed.get("workspaces"):
            self._log(
                f"从 consent HTML 提取到 {len(parsed.get('workspaces', []))} 个 workspace"
            )
            return parsed

        self._log("consent HTML 中也未提取到 workspace 数据")
        return session_data

    def _fetch_consent_page_html(self, consent_url, user_agent, impersonate):
        """获取 consent 页 HTML，用于解析 React Router stream 中的 session 数据。"""
        try:
            self._check_stop()
            headers = self._headers(
                consent_url,
                user_agent=user_agent,
                accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                referer=f"{self.oauth_issuer}/email-verification",
                navigation=True,
            )
            kwargs = {"headers": headers, "allow_redirects": False, "timeout": 30}
            if impersonate:
                kwargs["impersonate"] = impersonate
            self._browser_pause(0.12, 0.3)
            r = self.session.get(consent_url, **kwargs)
            self._check_stop()
            location = normalize_flow_url(
                r.headers.get("Location", ""), auth_base=self.oauth_issuer
            )
            content_type = (r.headers.get("content-type", "") or "").lower()
            self._log(
                f"consent 页面请求 -> {r.status_code}, url={str(r.url)[:120]}, "
                f"location={location[:120] if location else ''}, content-type={content_type[:80]}"
            )
            if r.status_code in (301, 302, 303, 307, 308) and location:
                self._log(f"consent 页面发生重定向 -> {location}")
            if r.status_code == 200 and "text/html" in content_type:
                return r.text
        except Exception as e:
            if self._is_stop_exception(e):
                self._raise_stop(e)
            self._log(f"获取 consent HTML 异常: {e}")
        return ""

    def _extract_session_data_from_consent_html(self, html):
        """从 consent HTML 的 React Router stream 中提取 workspace session 数据。"""
        import json
        import re

        if not html or "workspaces" not in html:
            return None

        def _first_match(patterns, text):
            for pattern in patterns:
                m = re.search(pattern, text, re.S)
                if m:
                    return m.group(1)
            return ""

        def _build_from_text(text):
            if not text or "workspaces" not in text:
                return None

            normalized = text.replace('\\"', '"')

            session_id = _first_match(
                [
                    r'"session_id","([^"]+)"',
                    r'"session_id":"([^"]+)"',
                ],
                normalized,
            )
            client_id = _first_match(
                [
                    r'"openai_client_id","([^"]+)"',
                    r'"openai_client_id":"([^"]+)"',
                ],
                normalized,
            )

            start = normalized.find('"workspaces"')
            if start < 0:
                start = normalized.find("workspaces")
            if start < 0:
                return None

            end = normalized.find('"openai_client_id"', start)
            if end < 0:
                end = normalized.find("openai_client_id", start)
            if end < 0:
                end = min(len(normalized), start + 4000)
            else:
                end = min(len(normalized), end + 600)

            workspace_chunk = normalized[start:end]
            ids = re.findall(r'"id"(?:,|:)"([0-9a-fA-F-]{36})"', workspace_chunk)
            if not ids:
                return None

            kinds = re.findall(r'"kind"(?:,|:)"([^"]+)"', workspace_chunk)
            workspaces = []
            seen = set()
            for idx, wid in enumerate(ids):
                if wid in seen:
                    continue
                seen.add(wid)
                item = {"id": wid}
                if idx < len(kinds):
                    item["kind"] = kinds[idx]
                workspaces.append(item)

            if not workspaces:
                return None

            return {
                "session_id": session_id,
                "openai_client_id": client_id,
                "workspaces": workspaces,
            }

        candidates = [html]

        for quoted in re.findall(
            r'streamController\.enqueue\(("(?:\\.|[^"\\])*")\)',
            html,
            re.S,
        ):
            try:
                decoded = json.loads(quoted)
            except Exception:
                continue
            if decoded:
                candidates.append(decoded)

        if '\\"' in html:
            candidates.append(html.replace('\\"', '"'))

        for candidate in candidates:
            parsed = _build_from_text(candidate)
            if parsed and parsed.get("workspaces"):
                return parsed

        return None

    def _decode_oauth_session_cookie(self):
        """解码 oai-client-auth-session cookie"""
        try:
            for cookie in self.session.cookies:
                try:
                    name = cookie.name if hasattr(cookie, "name") else str(cookie)
                    if name == "oai-client-auth-session":
                        value = (
                            cookie.value
                            if hasattr(cookie, "value")
                            else self.session.cookies.get(name)
                        )
                        if value:
                            data = self._decode_cookie_json_value(value)
                            if data:
                                return data
                except Exception:
                    continue
        except Exception:
            pass

        return None

    @staticmethod
    def _decode_cookie_json_value(value):
        import base64
        import json

        raw_value = str(value or "").strip()
        if not raw_value:
            return None

        candidates = [raw_value]
        if "." in raw_value:
            candidates.insert(0, raw_value.split(".", 1)[0])

        for candidate in candidates:
            candidate = candidate.strip()
            if not candidate:
                continue
            padded = candidate + "=" * (-len(candidate) % 4)
            for decoder in (base64.urlsafe_b64decode, base64.b64decode):
                try:
                    decoded = decoder(padded).decode("utf-8")
                    parsed = json.loads(decoded)
                except Exception:
                    continue
                if isinstance(parsed, dict):
                    return parsed

        return None

    def _exchange_code_for_tokens(self, code, code_verifier, user_agent, impersonate):
        """用 authorization code 换取 tokens"""
        self._check_stop()
        url = f"{self.oauth_issuer}/oauth/token"

        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.oauth_redirect_uri,
            "client_id": self.oauth_client_id,
            "code_verifier": code_verifier,
        }

        headers = self._headers(
            url,
            user_agent=user_agent,
            accept="application/json",
            referer=f"{self.oauth_issuer}/sign-in-with-chatgpt/codex/consent",
            origin=self.oauth_issuer,
            content_type="application/x-www-form-urlencoded",
            fetch_site="same-origin",
        )

        try:
            kwargs = {"data": payload, "headers": headers, "timeout": 60}
            if impersonate:
                kwargs["impersonate"] = impersonate

            self._browser_pause()
            self._check_stop()
            r = self.session.post(url, **kwargs)
            self._check_stop()

            if r.status_code == 200:
                return r.json()
            else:
                self._set_error(f"换取 tokens 失败: {r.status_code} - {r.text[:200]}")

        except Exception as e:
            if self._is_stop_exception(e):
                self._raise_stop(e)
            self._set_error(f"换取 tokens 异常: {e}")

        return None

    def _send_phone_number(self, phone, device_id, user_agent, sec_ch_ua, impersonate):
        self._check_stop()
        request_url = f"{self.oauth_issuer}/api/accounts/add-phone/send"
        headers = self._headers(
            request_url,
            user_agent=user_agent,
            sec_ch_ua=sec_ch_ua,
            accept="application/json",
            referer=f"{self.oauth_issuer}/add-phone",
            origin=self.oauth_issuer,
            content_type="application/json",
            fetch_site="same-origin",
            extra_headers={"oai-device-id": device_id},
        )
        headers.update(generate_datadog_trace())

        try:
            kwargs = {
                "json": {"phone_number": phone},
                "headers": headers,
                "timeout": 30,
                "allow_redirects": False,
            }
            if impersonate:
                kwargs["impersonate"] = impersonate

            self._browser_pause(0.12, 0.25)
            self._check_stop()
            resp = self.session.post(request_url, **kwargs)
            self._check_stop()
        except Exception as e:
            if self._is_stop_exception(e):
                self._raise_stop(e)
            return False, None, f"add-phone/send 异常: {e}"

        self._log(f"/add-phone/send -> {resp.status_code}")
        if resp.status_code != 200:
            return (
                False,
                None,
                f"add-phone/send 失败: {resp.status_code} - {resp.text[:180]}",
            )

        try:
            data = resp.json()
        except Exception:
            return False, None, "add-phone/send 响应不是 JSON"

        next_state = self._state_from_payload(
            data, current_url=str(resp.url) or request_url
        )
        self._log(f"add-phone/send {describe_flow_state(next_state)}")
        return True, next_state, ""

    def _resend_phone_otp(
        self, device_id, user_agent, sec_ch_ua, impersonate, state: FlowState
    ):
        self._check_stop()
        request_url = f"{self.oauth_issuer}/api/accounts/phone-otp/resend"
        headers = self._headers(
            request_url,
            user_agent=user_agent,
            sec_ch_ua=sec_ch_ua,
            accept="application/json",
            referer=state.current_url
            or state.continue_url
            or f"{self.oauth_issuer}/phone-verification",
            origin=self.oauth_issuer,
            content_type="application/json",
            fetch_site="same-origin",
            extra_headers={"oai-device-id": device_id},
        )
        headers.update(generate_datadog_trace())

        try:
            kwargs = {"json": {}, "headers": headers, "timeout": 30, "allow_redirects": False}
            if impersonate:
                kwargs["impersonate"] = impersonate
            self._browser_pause(0.12, 0.25)
            self._check_stop()
            resp = self.session.post(request_url, **kwargs)
            self._check_stop()
        except Exception as e:
            if self._is_stop_exception(e):
                self._raise_stop(e)
            return False, f"phone-otp/resend 异常: {e}"

        self._log(f"/phone-otp/resend -> {resp.status_code}")
        if resp.status_code == 200:
            return True, ""
        return False, f"phone-otp/resend 失败: {resp.status_code} - {resp.text[:180]}"

    def _validate_phone_otp(
        self, code, device_id, user_agent, sec_ch_ua, impersonate, state: FlowState
    ):
        self._check_stop()
        request_url = f"{self.oauth_issuer}/api/accounts/phone-otp/validate"
        headers = self._headers(
            request_url,
            user_agent=user_agent,
            sec_ch_ua=sec_ch_ua,
            accept="application/json",
            referer=state.current_url
            or state.continue_url
            or f"{self.oauth_issuer}/phone-verification",
            origin=self.oauth_issuer,
            content_type="application/json",
            fetch_site="same-origin",
            extra_headers={"oai-device-id": device_id},
        )
        headers.update(generate_datadog_trace())

        try:
            kwargs = {
                "json": {"code": code},
                "headers": headers,
                "timeout": 30,
                "allow_redirects": False,
            }
            if impersonate:
                kwargs["impersonate"] = impersonate
            self._browser_pause(0.12, 0.25)
            self._check_stop()
            resp = self.session.post(request_url, **kwargs)
            self._check_stop()
        except Exception as e:
            if self._is_stop_exception(e):
                self._raise_stop(e)
            return False, None, f"phone-otp/validate 异常: {e}"

        self._log(f"/phone-otp/validate -> {resp.status_code}")
        if resp.status_code != 200:
            if resp.status_code == 401:
                return False, None, "手机号验证码错误"
            return (
                False,
                None,
                f"phone-otp/validate 失败: {resp.status_code} - {resp.text[:180]}",
            )

        try:
            data = resp.json()
        except Exception:
            return False, None, "phone-otp/validate 响应不是 JSON"

        next_state = self._state_from_payload(
            data, current_url=str(resp.url) or request_url
        )
        self._log(f"手机号 OTP 验证通过 {describe_flow_state(next_state)}")
        return True, next_state, ""

    def _current_phone_otp_channel(self, default="sms"):
        session_data = self._decode_oauth_session_cookie() or {}
        channel = str(session_data.get("phone_verification_channel") or default or "sms").strip().lower()
        return channel if channel in {"sms", "whatsapp"} else str(default or "sms").strip().lower() or "sms"

    def _manual_phone_otp_enabled(self):
        if not self._coerce_bool(self.config.get("_manual_phone_otp_enabled"), default=False):
            return False
        return hasattr(self.task_control, "wait_for_verification_code")

    def _manual_phone_otp_timeout_seconds(self):
        try:
            return max(int(float(self.config.get("_manual_phone_otp_timeout_seconds") or 60)), 1)
        except Exception:
            return 60

    def _select_phone_otp_channel(
        self,
        channel,
        device_id,
        user_agent,
        sec_ch_ua,
        impersonate,
        state: FlowState,
    ):
        self._check_stop()
        channel_value = str(channel or "").strip().lower()
        if channel_value not in {"sms", "whatsapp"}:
            return False, None, f"不支持的手机号验证码通道: {channel}"

        request_url = f"{self.oauth_issuer}/api/accounts/phone-otp/send"
        headers = self._headers(
            request_url,
            user_agent=user_agent,
            sec_ch_ua=sec_ch_ua,
            accept="application/json",
            referer=state.current_url
            or state.continue_url
            or f"{self.oauth_issuer}/phone-otp/select-channel",
            origin=self.oauth_issuer,
            content_type="application/json",
            fetch_site="same-origin",
            extra_headers={"oai-device-id": device_id},
        )
        headers.update(generate_datadog_trace())

        try:
            kwargs = {
                "json": {"channel": channel_value},
                "headers": headers,
                "timeout": 30,
                "allow_redirects": False,
            }
            if impersonate:
                kwargs["impersonate"] = impersonate
            self._browser_pause(0.12, 0.25)
            resp = self.session.post(request_url, **kwargs)
            self._check_stop()
        except Exception as exc:
            if self._is_stop_exception(exc):
                self._raise_stop(exc)
            return False, None, f"phone-otp/send({channel_value}) 异常: {exc}"

        self._log(f"/phone-otp/send({channel_value}) -> {resp.status_code}")
        if resp.status_code != 200:
            return False, None, f"phone-otp/send({channel_value}) 失败: {resp.status_code} - {resp.text[:180]}"

        try:
            data = resp.json()
        except Exception:
            return True, state, ""

        next_state = self._state_from_payload(data, current_url=str(resp.url) or request_url)
        if next_state:
            self._log(f"phone-otp/send({channel_value}) {describe_flow_state(next_state)}")
        return True, next_state or state, ""

    def _wait_for_manual_phone_otp(
        self,
        *,
        phone,
        masked,
        channel,
        reason,
        device_id,
        user_agent,
        sec_ch_ua,
        impersonate,
        state: FlowState,
        allow_whatsapp_channel: bool = True,
        allow_resend_action: bool = True,
    ):
        if not self._manual_phone_otp_enabled():
            self._set_error(reason or "需要人工手机号验证码，但当前任务不支持人工输入")
            return None

        phone_value = str(phone or "").strip()
        masked_value = str(masked or "").strip()
        channel_value = str(channel or self._current_phone_otp_channel("sms") or "sms").strip().lower() or "sms"
        if channel_value not in {"sms", "whatsapp"}:
            channel_value = "sms"
        if not allow_whatsapp_channel and channel_value == "whatsapp":
            channel_value = "sms"
        timeout_seconds = self._manual_phone_otp_timeout_seconds()
        email = str(self.config.get("_current_account_email") or self.config.get("email") or "").strip()
        state_box = {"state": state, "channel": channel_value}
        available_channels = ["sms"]
        if allow_whatsapp_channel:
            available_channels.append("whatsapp")
        actions = []
        if allow_whatsapp_channel:
            actions.append("switch_channel")
        if allow_resend_action:
            actions.append("resend")

        self._log(
            "[手机号验证] 等待人工输入验证码: "
            f"phone={phone_value or masked_value or '-'} channel={channel_value} timeout={timeout_seconds}s"
        )

        def _handle_action(action, payload):
            action_value = str(action or "").strip().lower()
            payload = dict(payload or {})
            if action_value == "switch_channel":
                next_channel = str(payload.get("channel") or "").strip().lower()
                if next_channel not in {"sms", "whatsapp"}:
                    raise ValueError(f"不支持的手机号验证码通道: {next_channel}")
                if not allow_whatsapp_channel and next_channel == "whatsapp":
                    raise ValueError("当前手机号属于手机号池号段，默认不允许 WhatsApp 验证")
                ok, next_state, detail = self._select_phone_otp_channel(
                    next_channel,
                    device_id,
                    user_agent,
                    sec_ch_ua,
                    impersonate,
                    state_box["state"],
                )
                if not ok:
                    self._log(f"[手机号验证] 切换通道失败: channel={next_channel} detail={detail}")
                    return {"metadata": {"channel": state_box["channel"], "last_action_detail": detail}}
                state_box["channel"] = next_channel
                if next_state:
                    state_box["state"] = next_state
                self._log(f"[手机号验证] 已切换验证码通道: {next_channel}")
                return {"metadata": {"channel": next_channel, "last_action_detail": "通道已切换并重新发送"}}

            if action_value == "resend":
                if not allow_resend_action:
                    raise ValueError("当前不允许重新发送手机号验证码")
                ok, detail = self._resend_phone_otp(
                    device_id,
                    user_agent,
                    sec_ch_ua,
                    impersonate,
                    state_box["state"],
                )
                if not ok:
                    self._log(f"[手机号验证] 重新发送验证码失败: {detail}")
                    return {"metadata": {"channel": state_box["channel"], "last_action_detail": detail}}
                self._log(f"[手机号验证] 已重新发送验证码: channel={state_box['channel']}")
                return {"metadata": {"channel": state_box["channel"], "last_action_detail": "验证码已重新发送"}}

            raise ValueError(f"不支持的验证码动作: {action_value}")

        try:
            code = self.task_control.wait_for_verification_code(
                attempt_id=self.task_attempt_id,
                phase="phone_otp",
                phase_label="手机号验证码",
                email=phone_value or masked_value or email,
                timeout_seconds=timeout_seconds,
                metadata={
                    "kind": "phone_otp",
                    "phone": phone_value,
                    "masked_phone": masked_value,
                    "account_email": email,
                    "channel": channel_value,
                    "preferred_channel": "sms",
                    "available_channels": available_channels,
                    "reason": str(reason or ""),
                    "can_switch_channel": bool(allow_whatsapp_channel),
                    "can_resend": bool(allow_resend_action),
                },
                actions=actions,
                action_handler=_handle_action,
            )
        except TimeoutError:
            message = f"手机号验证码 {timeout_seconds}s 内未输入，自动跳过当前账号"
            self._log(f"[手机号验证] {message}")
            raise SkipCurrentAttemptRequested(message)

        self._log(f"[手机号验证] 准备提交人工手机号验证码 otp_present={bool(code)} otp_length={len(str(code or ''))}")
        valid, validated_state, detail = self._validate_phone_otp(
            code,
            device_id,
            user_agent,
            sec_ch_ua,
            impersonate,
            state_box["state"],
        )
        if not valid or not validated_state:
            self._set_error(f"人工手机号 OTP 验证失败: {detail or 'unknown'}")
            return None
        self._log("[手机号验证] 人工手机号 OTP 验证通过")
        return validated_state

    def _handle_add_phone_verification(
        self,
        device_id,
        user_agent,
        sec_ch_ua,
        impersonate,
        state: FlowState,
        email: str = "",
        sms_probe_only: bool = False,
    ):
        self._check_stop()
        phone_service = self.shared_phone_service or create_phone_service(self.config, log_fn=self._log)
        if not phone_service.enabled:
            self._set_error(
                "OAuth 登录被 add_phone 阻断，当前账号需要手机号验证；未配置可用的接码服务"
            )
            return None

        excluded_prefixes = set()
        last_failure = ""
        last_rejected_phone_failure = ""

        for attempt in range(phone_service.max_attempts):
            self._check_stop()
            try:
                entry = phone_service.acquire_phone(
                    exclude_prefixes=excluded_prefixes,
                    email=email,
                    account_id=self.config.get("_current_account_id") or self.config.get("account_id") or 0,
                    task_id=self.config.get("_current_task_id") or self.config.get("task_id") or "",
                )
            except Exception as e:
                if self._is_stop_exception(e):
                    self._raise_stop(e)
                last_failure = f"获取手机号失败: {e}"
                if last_rejected_phone_failure:
                    last_failure = (
                        f"{last_rejected_phone_failure}；随后取新号失败: {e}"
                    )
                self._log(last_failure)
                break

            if not entry:
                last_failure = last_failure or "接码服务中无可用手机号"
                if last_rejected_phone_failure:
                    last_failure = (
                        f"{last_rejected_phone_failure}；随后取新号失败: "
                        f"{last_failure}"
                    )
                break

            self._check_stop()
            prefix = phone_service.prefix_hint(entry.phone)
            self._log(
                f"步骤5: add_phone 选择手机号 {attempt + 1}/{phone_service.max_attempts}: {mask_phone_for_log(entry.phone)} ({entry.country_slug})"
            )

            sent, next_state, detail = self._send_phone_number(
                entry.phone,
                device_id,
                user_agent,
                sec_ch_ua,
                impersonate,
            )
            if not sent or not next_state:
                last_failure = detail or "add-phone/send 未返回有效状态"
                self._log(last_failure)
                self._reject_phone_and_continue(phone_service, entry, last_failure)
                if self._should_blacklist_phone_failure(last_failure):
                    last_rejected_phone_failure = last_failure
                excluded_prefixes.add(prefix)
                continue

            self._check_stop()
            if (
                next_state.page_type != "phone_otp_verification"
                and "phone-verification"
                not in f"{next_state.continue_url} {next_state.current_url}".lower()
            ):
                last_failure = f"add-phone/send 未进入手机验证码页: {describe_flow_state(next_state)}"
                self._log(last_failure)
                self._reject_phone_and_continue(
                    phone_service, entry, last_failure, state=next_state
                )
                if self._should_blacklist_phone_failure(last_failure, next_state):
                    last_rejected_phone_failure = last_failure
                excluded_prefixes.add(prefix)
                continue

            session_data = self._decode_oauth_session_cookie() or {}
            verification_channel = (
                str(session_data.get("phone_verification_channel") or "sms")
                .strip()
                .lower()
                or "sms"
            )
            bound_phone = (
                str(session_data.get("phone_number") or entry.phone).strip()
                or entry.phone
            )
            self._log(
                f"add_phone 发码成功: phone={mask_phone_for_log(bound_phone)}, channel={verification_channel}"
            )
            self._check_stop()
            phone_service.mark_sms_sent(entry)
            self._check_stop()

            if verification_channel != "sms":
                last_failure = f"add_phone 已切到 {verification_channel} 通道，当前接码服务仅支持短信接码"
                self._log(last_failure)
                phone_service.cancel(entry, reason=last_failure)
                excluded_prefixes.add(prefix)
                continue

            code = phone_service.wait_for_code(entry)
            self._check_stop()
            max_resends = getattr(phone_service, "max_resend_attempts", 1)
            resend_interval_seconds = getattr(
                phone_service, "resend_interval_seconds", 0
            )
            resend_attempt = 0
            while not code and resend_attempt < max_resends:
                self._check_stop()
                resend_attempt += 1
                self._log(
                    f"手机号验证码暂未收到，等待 {resend_interval_seconds:g} 秒后继续使用当前手机号重发 {resend_attempt}/{max_resends}..."
                )
                self._sleep_before_phone_resend(resend_interval_seconds)
                resend_ok, resend_detail = self._resend_phone_otp(
                    device_id,
                    user_agent,
                    sec_ch_ua,
                    impersonate,
                    next_state,
                )
                if not resend_ok:
                    last_failure = resend_detail or "OpenAI 无法继续发送手机号验证码"
                    break
                if not phone_service.request_next_code(entry):
                    last_failure = "接码网关无法继续为当前手机号请求下一条短信"
                    break
                code = phone_service.wait_for_code(entry)
                self._check_stop()

            if not code:
                if not last_failure:
                    if resend_attempt >= max_resends:
                        last_failure = (
                            f"手机号 {mask_phone_for_log(entry.phone)} 达到同号重发上限 {max_resends} 次，仍未收到短信验证码"
                        )
                    else:
                        last_failure = f"手机号 {mask_phone_for_log(entry.phone)} 未收到短信验证码"
                self._log(last_failure)
                phone_service.cancel(entry, reason=last_failure)
                excluded_prefixes.add(prefix)
                continue

            try:
                validate_delay_seconds = float(getattr(phone_service, "validate_delay_seconds", 0) or 0)
            except (TypeError, ValueError):
                validate_delay_seconds = 0
            if validate_delay_seconds > 0:
                if sms_probe_only:
                    self._log(f"号段短信探测已收到验证码 code_received=true otp_length={len(str(code or ''))}；按设置不提交验证码")
                    phone_service.complete(entry)
                    self._set_error("号段短信探测完成：OpenAI 已发码且收码 API 已收到验证码，未提交验证码")
                    return None
                self._log(f"准备提交手机号验证码 otp_present={bool(code)} otp_length={len(str(code or ''))}，等待 {validate_delay_seconds:g}s 后验证")
                self._sleep_with_stop(validate_delay_seconds)
            else:
                if sms_probe_only:
                    self._log(f"号段短信探测已收到验证码 code_received=true otp_length={len(str(code or ''))}；按设置不提交验证码")
                    phone_service.complete(entry)
                    self._set_error("号段短信探测完成：OpenAI 已发码且收码 API 已收到验证码，未提交验证码")
                    return None
                self._log(f"准备提交手机号验证码 otp_present={bool(code)} otp_length={len(str(code or ''))}")
            valid, validated_state, detail = self._validate_phone_otp(
                code,
                device_id,
                user_agent,
                sec_ch_ua,
                impersonate,
                next_state,
            )
            if not valid or not validated_state:
                last_failure = detail or "手机号 OTP 验证失败"
                self._log(last_failure)
                phone_service.cancel(entry, reason=last_failure)
                excluded_prefixes.add(prefix)
                continue

            self._check_stop()
            self._record_confirmed_phone_binding_event(entry=entry, email=email)
            phone_service.complete(entry)
            return validated_state

        self._set_error(f"add_phone 阶段失败: {last_failure or '未完成手机号验证'}")
        return None

    def _handle_otp_verification(
        self,
        email,
        device_id,
        user_agent,
        sec_ch_ua,
        impersonate,
        skymail_client,
        state,
        scope_log_prefix="",
    ):
        """处理 OAuth 阶段的邮箱 OTP 验证，返回服务端声明的下一步状态。"""
        self._check_stop()
        self._log("步骤4: 检测到邮箱 OTP 验证")
        scope_log_prefix = str(scope_log_prefix or "").strip()

        def _resend_email_otp() -> float | None:
            self._check_stop()
            prefer_passwordless = bool(
                self.config.get("prefer_passwordless_login")
                or self.config.get("force_passwordless_login")
            )
            resend_ok = False
            if prefer_passwordless:
                request_url = f"{self.oauth_issuer}/api/accounts/passwordless/send-otp"
                headers = self._headers(
                    request_url,
                    user_agent=user_agent,
                    sec_ch_ua=sec_ch_ua,
                    accept="application/json",
                    referer=state.current_url
                    or state.continue_url
                    or f"{self.oauth_issuer}/log-in/password",
                    origin=self.oauth_issuer,
                    content_type="application/json",
                    fetch_site="same-origin",
                    extra_headers={
                        "oai-device-id": device_id,
                    },
                )
                headers.update(generate_datadog_trace())
                try:
                    kwargs = {"headers": headers, "timeout": 30, "allow_redirects": False}
                    if impersonate:
                        kwargs["impersonate"] = impersonate
                    self._browser_pause()
                    self._check_stop()
                    resend_started_at = _otp_request_started_at()
                    resp = self.session.post(request_url, **kwargs)
                    self._check_stop()
                    self._log(f"/passwordless/send-otp -> {resp.status_code}")
                    if resp.status_code == 200:
                        resend_ok = True
                except Exception as e:
                    if self._is_stop_exception(e):
                        self._raise_stop(e)
                    self._log(f"passwordless resend 异常: {e}")

            if resend_ok:
                self._log("已触发 passwordless OTP 重发")
                return resend_started_at

            request_url = f"{self.oauth_issuer}/api/accounts/email-otp/resend"
            headers = self._headers(
                request_url,
                user_agent=user_agent,
                sec_ch_ua=sec_ch_ua,
                accept="*/*",
                referer=state.current_url
                or state.continue_url
                or f"{self.oauth_issuer}/email-verification",
                origin=self.oauth_issuer,
                content_type="application/json",
                fetch_site="same-origin",
                extra_headers={
                    "oai-device-id": device_id,
                },
            )
            headers.update(generate_datadog_trace())
            try:
                kwargs = {"headers": headers, "timeout": 30, "allow_redirects": True}
                if impersonate:
                    kwargs["impersonate"] = impersonate
                self._browser_pause()
                self._check_stop()
                resend_started_at = _otp_request_started_at()
                resp = self.session.post(request_url, **kwargs)
                self._check_stop()
                self._log(f"/email-otp/resend -> {resp.status_code}")
                if 200 <= int(resp.status_code or 0) < 300:
                    self._log("已触发 email-otp 重发")
                    return resend_started_at
                self._log(f"email-otp/resend 重发失败: {resp.text[:120]}")
            except Exception as e:
                if self._is_stop_exception(e):
                    self._raise_stop(e)
                self._log(f"email-otp/resend 重发异常: {e}")
            return None

        request_url = f"{self.oauth_issuer}/api/accounts/email-otp/validate"
        self._log(f"email_otp_validate: device_id={device_id}")
        self._check_stop()
        sentinel_otp = None
        if self.allow_browser:
            sentinel_otp = get_sentinel_token_via_browser(
                flow="email_otp_validate",
                proxy=self.proxy,
                page_url=state.current_url
                or state.continue_url
                or f"{self.oauth_issuer}/email-verification",
                headless=self.browser_mode != "headed",
                device_id=device_id,
                user_agent=user_agent,
                sec_ch_ua=sec_ch_ua,
                accept_language=(self.browser_fingerprint.accept_language if self.browser_fingerprint else None),
                chrome_full_version=(self.browser_fingerprint.chrome_full_version if self.browser_fingerprint else None),
                platform_version=(self.browser_fingerprint.platform_version if self.browser_fingerprint else None),
                viewport_width=(self.browser_fingerprint.viewport_width if self.browser_fingerprint else None),
                viewport_height=(self.browser_fingerprint.viewport_height if self.browser_fingerprint else None),
                stop_check=self._check_stop,
                log_fn=lambda msg: self._log(f"email_otp_validate: {msg}"),
            )
        if sentinel_otp:
            self._log("email_otp_validate: 已通过 Playwright SentinelSDK 获取 token")
        else:
            sentinel_otp = build_sentinel_token(
                self.session,
                device_id,
                flow="email_otp_validate",
                user_agent=user_agent,
                sec_ch_ua=sec_ch_ua,
                impersonate=impersonate,
            )
            if sentinel_otp:
                self._log("email_otp_validate: 已通过 HTTP PoW 获取 token")
            else:
                self._log("email_otp_validate: 未生成 sentinel token（继续尝试）")

        headers_otp = self._headers(
            request_url,
            user_agent=user_agent,
            sec_ch_ua=sec_ch_ua,
            accept="application/json",
            referer=state.current_url
            or state.continue_url
            or f"{self.oauth_issuer}/email-verification",
            origin=self.oauth_issuer,
            content_type="application/json",
            fetch_site="same-origin",
            extra_headers={
                "oai-device-id": device_id,
                "openai-sentinel-token": sentinel_otp or "",
            },
        )
        headers_otp.update(generate_datadog_trace())

        if not hasattr(skymail_client, "_used_codes"):
            skymail_client._used_codes = set()

        tried_codes = set(getattr(skymail_client, "_used_codes", set()))
        tried_message_ids = set()
        try:
            otp_wait_seconds = int(
                self.config.get(
                    "chatgpt_oauth_otp_wait_seconds",
                    self.config.get("chatgpt_otp_wait_seconds", 600),
                )
                or 600
            )
        except Exception:
            otp_wait_seconds = 600
        otp_wait_seconds = max(30, min(otp_wait_seconds, 3600))
        otp_poll_window = min(30, max(10, otp_wait_seconds))
        try:
            otp_resend_wait_seconds = int(
                self.config.get(
                    "chatgpt_oauth_otp_resend_wait_seconds",
                    self.config.get("chatgpt_otp_resend_wait_seconds", 120),
                )
                or 120
            )
        except Exception:
            otp_resend_wait_seconds = 120
        otp_resend_wait_seconds = max(30, min(otp_resend_wait_seconds, 900))
        now_ts = time.time()
        otp_deadline = now_ts + otp_wait_seconds
        otp_sent_at_source = "trigger_request"
        try:
            otp_sent_at = float(getattr(state, "otp_sent_at", None))
        except (TypeError, ValueError):
            otp_sent_at = 0.0
        if (
            not math.isfinite(otp_sent_at)
            or otp_sent_at <= 0
            or otp_sent_at > now_ts + OTP_SENT_AT_CLOCK_SKEW_GRACE_SECONDS
        ):
            otp_sent_at = now_ts - OTP_SENT_AT_FALLBACK_GRACE_SECONDS
            otp_sent_at_source = "fallback"
        next_resend_at = now_ts + otp_resend_wait_seconds
        self._log(
            f"OAuth OTP 等待窗口: total={otp_wait_seconds}s, poll_window={otp_poll_window}s, "
            f"cutoff_source={otp_sent_at_source}, cutoff_age={max(0, int(now_ts - otp_sent_at))}s"
        )

        def validate_otp(code, *, message_id=""):
            self._check_stop()
            tried_codes.add(code)
            if message_id:
                tried_message_ids.add(str(message_id).strip())
            self._log(f"尝试 OTP otp_present={bool(code)} otp_length={len(str(code or ''))}")

            try:
                kwargs = {
                    "json": {"code": code},
                    "headers": headers_otp,
                    "timeout": 30,
                    "allow_redirects": False,
                }
                if impersonate:
                    kwargs["impersonate"] = impersonate

                self._browser_pause(0.12, 0.25)
                self._check_stop()
                resp_otp = self.session.post(request_url, **kwargs)
                self._check_stop()
            except Exception as e:
                if self._is_stop_exception(e):
                    self._raise_stop(e)
                self._log(f"email-otp/validate 异常: {e}")
                return None

            self._log(f"/email-otp/validate -> {resp_otp.status_code}")
            if resp_otp.status_code != 200:
                if self._set_terminal_otp_error_if_needed("email-otp/validate", resp_otp):
                    return None
                self._log(f"OTP 无效: {resp_otp.text[:160]}")
                return None

            try:
                otp_data = resp_otp.json()
            except Exception:
                self._log("email-otp/validate 响应不是 JSON")
                return None

            next_state = self._state_from_payload(
                otp_data,
                current_url=str(resp_otp.url)
                or (state.current_url or state.continue_url or request_url),
            )
            self._log(f"OTP 验证通过 {describe_flow_state(next_state)}")
            if scope_log_prefix:
                self._log(f"{scope_log_prefix} OTP 验证通过")
            skymail_client._used_codes.add(code)
            return next_state

        if hasattr(skymail_client, "wait_for_verification_code"):
            self._log("[验证码] 等待 OAuth 邮箱验证码：timeout=600s，poll=30s")
            last_wait_debug_at = 0.0
            while time.time() < otp_deadline:
                self._check_stop()
                remaining = max(1, int(otp_deadline - time.time()))
                wait_time = min(otp_poll_window, remaining)
                try:
                    code = skymail_client.wait_for_verification_code(
                        email,
                        timeout=wait_time,
                        otp_sent_at=otp_sent_at,
                        exclude_codes=tried_codes,
                        phase="oauth_email_otp",
                        phase_label="OAuth 登录邮箱验证码",
                    )
                    verification_meta = {}
                    getter = getattr(skymail_client, "get_last_verification_result", None)
                    if callable(getter):
                        try:
                            verification_meta = getter("oauth_email_otp") or {}
                        except Exception:
                            verification_meta = {}
                    current_message_id = str(
                        verification_meta.get("message_id")
                        or verification_meta.get("id")
                        or ""
                    ).strip()
                except TaskInterruption:
                    raise
                except Exception as e:
                    if "手动停止" in str(e):
                        raise TaskInterruption("任务已手动停止") from e
                    if self._is_fatal_mailbox_config_error(e):
                        self._set_error(f"邮箱服务配置错误，停止等待 OTP: {str(e)[:240]}")
                        break
                    self._log(f"等待 OTP 异常: {e}")
                    self._sleep_with_stop(min(2, wait_time))
                    code = None
                    current_message_id = ""

                if not code:
                    now_ts = time.time()
                    if time.time() >= next_resend_at and not self.last_error:
                        self._log(
                            f"暂未收到 OTP，触发重发（间隔 {otp_resend_wait_seconds}s）"
                        )
                        resent_at = _resend_email_otp()
                        if resent_at is not None:
                            otp_sent_at = resent_at
                        next_resend_at = time.time() + otp_resend_wait_seconds
                    elapsed = max(0, int(now_ts - (otp_deadline - otp_wait_seconds)))
                    if now_ts - last_wait_debug_at >= 120:
                        self._log(
                            f"still waiting oauth_email_otp elapsed={elapsed}/{otp_wait_seconds}s poll_window={otp_poll_window}s"
                        )
                        last_wait_debug_at = now_ts
                    if self.last_error:
                        break
                    continue

                if code in tried_codes and (not current_message_id or current_message_id in tried_message_ids):
                    self._log(f"跳过已尝试验证码 otp_present={bool(code)} otp_length={len(str(code or ''))}")
                    continue

                next_state = validate_otp(code, message_id=current_message_id)
                if next_state:
                    return next_state
                if self.last_error:
                    break
        else:
            while time.time() < otp_deadline:
                self._check_stop()
                messages = skymail_client.fetch_emails(email) or []
                candidate_codes = []

                for msg in messages[:12]:
                    content = msg.get("content") or msg.get("text") or ""
                    code = skymail_client.extract_verification_code(content)
                    if code and code not in tried_codes:
                        candidate_codes.append(code)

                if not candidate_codes:
                    elapsed = int(otp_wait_seconds - max(0, otp_deadline - time.time()))
                    self._log(f"等待新的 OTP... ({elapsed}s/{otp_wait_seconds}s)")
                    self._sleep_with_stop(2)
                    continue

                for otp_code in candidate_codes:
                    next_state = validate_otp(otp_code)
                    if next_state:
                        return next_state

                self._sleep_with_stop(2)
                if self.last_error:
                    break

        if not self.last_error:
            self._set_error(
                f"OAuth 阶段 OTP 验证失败，已尝试 {len(tried_codes)} 个验证码，等待窗口 {otp_wait_seconds}s"
            )
        return None
