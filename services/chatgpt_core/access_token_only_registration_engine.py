"""ChatGPT AccessToken 注册引擎。

三执行器运输层统一迁入 any-auto-register（``services/chatgpt_core/any_auto``）：

- ``protocol`` → any-auto RegistrationEngine（curl_cffi 同 session create + NextAuth AT）
- ``headless`` / ``headed`` → any-auto ChatGPTBrowserRegister（整段 Camoufox）

本引擎只做邮箱出池、OTP 回调、库存落库合同；失败不跨 transport 兜底，
也不再把独立 Codex OAuth recovery / auth_pending 半成品当作注册成功。
"""

import json
import time
import logging
import re
from datetime import datetime
from typing import Any, Optional, Callable

from core.task_runtime import TaskInterruption
from services.chatgpt_core.refresh_token_registration_engine import RegistrationResult

from .chatgpt_client import ChatGPTClient
from .otp_budget import RegistrationOtpBudget
from .registration_route_policy import (
    ExistingAccountLoginRouteBlocked,
    build_existing_account_login_route_event,
    existing_account_login_route_enabled,
    is_existing_account_login_route_message,
    parse_bool,
)
from .task_logging import (
    classify_task_log_level,
    format_http_trace_log,
    mask_email_for_log,
)
from .utils import FlowState, generate_random_name, generate_random_birthday
from .account_fingerprint import build_browser_fingerprint_payload, fingerprint_signature
from .any_auto import (
    run_any_auto_browser_registration,
    run_any_auto_protocol_registration,
)
from .sentinel_browser import (
    BrowserOAuthTokenRecoveryResult,
    BrowserRegistrationStageResult,
    run_browser_oauth_token_recovery,
    run_browser_registration_stage,
)

logger = logging.getLogger(__name__)

class EmailServiceAdapter:
    """\u5c06 V1 \u7684 email_service \u9002\u914d\u6210 V2 \u6240\u9700\u7684\u63a5\u7801\u63a5\u53e3\u3002"""
    def __init__(self, email_service, email, log_fn, otp_budget: RegistrationOtpBudget | None = None):
        self.es = email_service
        self.email = email
        self.log_fn = log_fn
        self._used_codes_by_phase: dict[str, set[str]] = {}
        self._wait_counts_by_phase: dict[str, int] = {}
        self._otp_budget = otp_budget

    def _otp_source_label(self) -> str:
        raw = str(
            getattr(getattr(self.es, "service_type", None), "value", "")
            or getattr(self.es, "provider", "")
            or self.es.__class__.__name__
            or "注册邮箱"
        ).strip().lower()
        if raw in {
            "hme_ready_api",
            "icloud_hme",
            "icloud_hme_ready",
            "icloud_hme_helper_ready",
            "helper_ready_api",
            "icloud_hme_temp_mail_forward",
        }:
            return "HME Ready + TempMail"
        if raw in {"email_api", "api_email", "email_otp_api", "mail_api_otp"}:
            return "邮箱 API"
        if raw:
            return raw
        return "注册邮箱"

    def is_otp_wait_budget_exhausted(self) -> bool:
        budget = self._otp_budget
        return bool(budget and budget.is_exhausted())

    def used_codes_for_phases(self, *phases: str) -> set[str]:
        """Return codes already consumed by any related registration phase.

        Registration and OAuth remain separate phases even inside one executor.
        Reusing an OTP across those phases is invalid and can make the flow look
        stuck, so every phase keeps an explicit consumed-code set.
        """
        result: set[str] = set()
        for phase in phases:
            key = str(phase or "").strip()
            if key:
                result.update(self._used_codes_by_phase.get(key, set()))
        return result

    def release_code(self, code: str, *phases: str) -> None:
        """Allow a previously fetched code to be reused after a non-advancing submit.

        Browser signup can get HTTP 200 from email-otp/validate while the SPA
        stays on the OTP page (or OpenAI resends the same digits). Marking the
        code as used on first fetch then permanently excludes it and the
        waiter times out even though TempMail still has a usable OTP.
        """
        normalized = str(code or "").strip()
        if not normalized:
            return
        targets = [str(phase or "").strip() for phase in phases if str(phase or "").strip()]
        if not targets:
            targets = list(self._used_codes_by_phase.keys())
        released = False
        for key in targets:
            bucket = self._used_codes_by_phase.get(key)
            if not bucket or normalized not in bucket:
                continue
            bucket.discard(normalized)
            released = True
        if released:
            self.log_fn(f"[验证码] 已释放可复用验证码（提交后页面未推进） phase={','.join(targets)}")

    def wait_for_verification_code(
        self,
        email,
        timeout=60,
        otp_sent_at=None,
        exclude_codes=None,
        phase=None,
        phase_label=None,
        ignore_budget: bool = False,
    ):
        phase_key = str(phase or "email_otp").strip() or "email_otp"
        phase_title = str(phase_label or phase_key).strip() or phase_key
        used_codes = self._used_codes_by_phase.setdefault(phase_key, set())
        wait_count = self._wait_counts_by_phase.get(phase_key, 0) + 1
        self._wait_counts_by_phase[phase_key] = wait_count
        resend_count = max(wait_count - 1, 0)
        source_label = self._otp_source_label()
        masked_email = mask_email_for_log(email or self.email)
        wait_started = time.monotonic()
        wait_plan = (
            self._otp_budget.plan_wait(timeout)
            if self._otp_budget and not ignore_budget
            else None
        )
        if wait_plan and wait_plan.exhausted:
            self.log_fn(
                f"[验证码] 验证码等待预算已耗尽｜邮箱={masked_email}｜来源={source_label} "
                f"｜等待=0秒｜重发次数={resend_count}｜预算={self._otp_budget.total_seconds}秒"
            )
            return None
        try:
            fallback_timeout = max(int(timeout or 0), 1)
        except (TypeError, ValueError):
            fallback_timeout = 1
        effective_timeout = wait_plan.timeout_seconds if wait_plan else fallback_timeout
        if wait_plan and wait_plan.clamped:
            msg = (
                f"[验证码] 等待验证码｜邮箱={masked_email}｜来源={source_label} "
                f"｜超时={effective_timeout}s｜重发次数={resend_count} "
                f"｜预算剩余={wait_plan.remaining_seconds}s｜阶段={phase_title}"
            )
        else:
            msg = (
                f"[验证码] 等待验证码｜邮箱={masked_email}｜来源={source_label} "
                f"｜超时={effective_timeout}s｜重发次数={resend_count}｜阶段={phase_title}"
            )
        self.log_fn(msg)
        try:
            code = self.es.get_verification_code(
                timeout=effective_timeout,
                otp_sent_at=otp_sent_at,
                exclude_codes=set(exclude_codes or set()) | set(used_codes),
                phase=phase_key,
                phase_label=phase_title,
            )
        except TimeoutError as exc:
            waited_seconds = max(0, int(time.monotonic() - wait_started))
            self.log_fn(
                f"[验证码] 验证码未收到｜邮箱={masked_email}｜来源={source_label} "
                f"｜等待={waited_seconds}秒｜重发次数={resend_count}｜原因=等待超时: {exc}"
            )
            return None
        if code:
            code = str(code).strip()
            used_codes.add(code)
            waited_seconds = max(0, int(time.monotonic() - wait_started))
            self.log_fn(
                f"[验证码] 验证码已收到｜邮箱={masked_email}｜长度={len(code)} "
                f"｜等待={waited_seconds}秒｜来源={source_label}｜重发次数={resend_count}"
            )
        return code

class AccessTokenOnlyRegistrationEngine:
    def __init__(
        self,
        email_service,
        proxy_url: Optional[str] = None,
        browser_mode: str = "protocol",
        callback_logger: Optional[Callable[[str], None]] = None,
        task_uuid: Optional[str] = None,
        max_retries: int = 3,
        extra_config: Optional[dict] = None,
    ):
        self.email_service = email_service
        self.proxy_url = proxy_url
        normalized_browser_mode = str(browser_mode or "protocol").strip().lower()
        if normalized_browser_mode not in {"protocol", "headless", "headed"}:
            raise ValueError(f"unsupported ChatGPT executor: {browser_mode}")
        self.browser_mode = normalized_browser_mode
        self.callback_logger = callback_logger
        self.task_uuid = task_uuid
        self.max_retries = max(1, int(max_retries or 1))
        self.extra_config = dict(extra_config or {})
        raw_log_context = self.extra_config.get("__register_task_log_context")
        self._task_log_context = (
            raw_log_context if isinstance(raw_log_context, dict) else None
        )
        
        self.email = None
        self.password = None
        self.logs = []
        self._prepared_register_client: ChatGPTClient | None = None
        self._last_chatgpt_client: ChatGPTClient | None = None
        self._last_email_adapter = None
        self._mailbox_finalized = False

    def _is_browser_executor(self) -> bool:
        return self.browser_mode in {"headless", "headed"}

    def _should_probe_plus_checkout(self) -> bool:
        plan = str(
            self.extra_config.get("chatgpt_checkout_probe_plan")
            or self.extra_config.get("chatgpt_payment_plan")
            or "plus"
        ).strip().lower()
        return plan in {"", "plus", "chatgptplusplan"}

    def _is_checkout_amount_check_enabled(self) -> bool:
        return self._parse_bool(
            self.extra_config.get("chatgpt_access_token_only_checkout_amount_check_enabled", True)
        )

    @staticmethod
    def _checkout_amount_is_zero(value: Any) -> bool:
        text = str(value if value is not None else "").strip()
        if not text:
            return False
        try:
            from decimal import Decimal

            return Decimal(text) == 0
        except Exception:
            return text == "0"

    @staticmethod
    def _is_checkout_already_paid_error(status_code: Any, body_text: str) -> bool:
        try:
            code = int(status_code or 0)
        except (TypeError, ValueError):
            code = 0
        if code and code not in {400, 409}:
            return False
        lowered = str(body_text or "").strip().lower()
        return any(
            marker in lowered
            for marker in (
                "you have paid",
                "already paid",
                "user is already paid",
                "has been paid",
                "no payment required",
            )
        )

    @staticmethod
    def _parse_bool(value: Any) -> bool:
        return parse_bool(value, default=False)

    @staticmethod
    def _parse_bool_default(value: Any, *, default: bool) -> bool:
        return parse_bool(value, default=default)

    @staticmethod
    def _parse_positive_int(value: Any, default: int = 1) -> int:
        try:
            parsed = int(str(value or "").strip())
        except (TypeError, ValueError):
            return max(int(default or 1), 1)
        return parsed if parsed > 0 else max(int(default or 1), 1)

    def _zero_amount_stop_enabled(self) -> bool:
        return self._parse_bool(self.extra_config.get("chatgpt_access_token_only_zero_amount_stop_enabled"))

    def _zero_amount_stop_threshold(self) -> int:
        return self._parse_positive_int(
            self.extra_config.get("chatgpt_access_token_only_zero_amount_stop_threshold"),
            default=1,
        )

    def _is_existing_account_login_route_enabled(self) -> bool:
        return existing_account_login_route_enabled(self.extra_config)

    def _read_int_config(
        self,
        primary_key: str,
        *,
        fallback_keys: tuple[str, ...] = (),
        default: int,
        minimum: int,
        maximum: int,
    ) -> int:
        for key in (primary_key, *tuple(fallback_keys or ())):
            if key not in self.extra_config:
                continue
            value = self.extra_config.get(key)
            if value in (None, ""):
                continue
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                continue
            return max(minimum, min(parsed, maximum))
        return max(minimum, min(int(default), maximum))

    @staticmethod
    def _json_object(value: Any) -> dict:
        if isinstance(value, dict):
            return dict(value)
        if isinstance(value, str) and value.strip():
            try:
                parsed = json.loads(value)
            except Exception:
                return {}
            if isinstance(parsed, dict):
                return dict(parsed)
        return {}

    def _checkout_billing_config(self, *, country: str, currency: str, email_addr: str) -> dict:
        billing = self.extra_config.get("chatgpt_checkout_billing")
        if not isinstance(billing, dict):
            billing = self.extra_config.get("billing") if isinstance(self.extra_config.get("billing"), dict) else {}
        resolved = dict(billing or {})
        resolved.setdefault("email", email_addr)
        resolved.setdefault("country", country)
        resolved.setdefault("currency", currency)
        return resolved

    def _probe_plus_checkout_billing(self, session_result: dict, email_addr: str) -> dict:
        if not self._should_probe_plus_checkout():
            return {}
        amount_check_enabled = self._is_checkout_amount_check_enabled()
        if not amount_check_enabled:
            self._log("Plus 额度验证已关闭，仅生成订阅链接")
            return {
                "chatgpt_checkout_plan": "plus",
                "chatgpt_checkout_url": "",
                "chatgpt_checkout_amount_check_enabled": False,
                "chatgpt_skip_save_account": False,
                "chatgpt_skip_save_reason": "",
            }

        from services.chatgpt_core.checkout_probe import probe_chatgpt_checkout_amount
        from services.chatgpt_core.payment import (
            CheckoutRequestError,
            generate_plus_link,
            normalize_checkout_country,
            normalize_checkout_currency,
        )
        from core.proxy_utils import resolve_default_chatgpt_proxy

        class _CheckoutAccount:
            pass

        account = _CheckoutAccount()
        account.access_token = str(session_result.get("access_token") or "")
        account.cookies = str(session_result.get("cookies") or session_result.get("cookie") or "")
        account.session_token = str(session_result.get("session_token") or "")
        account.email = email_addr
        account.extra = {
            "account_id": str(session_result.get("account_id") or session_result.get("user_id") or ""),
            "workspace_id": str(session_result.get("workspace_id") or ""),
            "session_token": account.session_token,
        }
        if self.extra_config.get("stripe_publishable_key"):
            account.extra["stripe_publishable_key"] = self.extra_config.get("stripe_publishable_key")

        country = normalize_checkout_country(
            self.extra_config.get("chatgpt_checkout_country")
            or self.extra_config.get("chatgpt_access_token_only_checkout_country")
            or self.extra_config.get("checkout_country")
            or self.extra_config.get("country")
            or "US"
        )
        raw_currency = (
            self.extra_config.get("chatgpt_checkout_currency")
            or self.extra_config.get("chatgpt_access_token_only_checkout_currency")
            or self.extra_config.get("checkout_currency")
            or self.extra_config.get("currency")
            or "USD"
        )
        currency = normalize_checkout_currency(
            raw_currency,
            country,
        )
        billing = self._checkout_billing_config(country=country, currency=currency, email_addr=email_addr)

        checkout_proxy = resolve_default_chatgpt_proxy(self.proxy_url)
        if not checkout_proxy:
            raise RuntimeError("当前没有可用代理，无法生成订阅链接")

        self._log(f"Plus 账单探测: 生成订阅链接 country={country} currency={currency}")
        try:
            checkout_url = generate_plus_link(
                account,
                proxy=checkout_proxy,
                country=country,
                currency=currency,
                billing=billing,
            )
        except CheckoutRequestError as exc:
            body_text = str(getattr(exc, "body", "") or str(exc)).strip()
            if self._is_checkout_already_paid_error(getattr(exc, "status_code", 0), body_text):
                reason = f"Plus checkout 已付费/不可用响应: {body_text[:300] or exc}"
                self._log(reason, "warning")
                return {
                    "chatgpt_checkout_plan": "plus",
                    "chatgpt_checkout_url": "",
                    "chatgpt_checkout_country": country,
                    "chatgpt_checkout_currency": currency,
                    "chatgpt_checkout_amount_check_enabled": True,
                    "chatgpt_checkout_error_code": "already_paid",
                    "chatgpt_checkout_error_status": int(getattr(exc, "status_code", 0) or 0),
                    "chatgpt_checkout_error_body": body_text,
                    "chatgpt_account_unavailable": True,
                    "chatgpt_unavailable_reason": reason,
                    "chatgpt_skip_save_account": True,
                    "chatgpt_skip_save_reason": reason,
                    "chatgpt_payment_already_paid": True,
                    "chatgpt_invalid_registration_failure": True,
                    "chatgpt_invalid_registration_reason": reason,
                }
            raise
        self._log(f"Plus checkout created: {checkout_url}")
        metadata = {
            "chatgpt_checkout_plan": "plus",
            "chatgpt_checkout_url": checkout_url,
            "chatgpt_checkout_country": country,
            "chatgpt_checkout_currency": currency,
            "chatgpt_checkout_amount_check_enabled": amount_check_enabled,
            "chatgpt_access_token_only_zero_amount_stop_enabled": self._zero_amount_stop_enabled(),
            "chatgpt_access_token_only_zero_amount_stop_threshold": self._zero_amount_stop_threshold(),
        }
        skip_save = False
        if amount_check_enabled:
            probe = probe_chatgpt_checkout_amount(
                account,
                checkout_url=checkout_url,
                country=country,
                currency=currency,
                proxy=checkout_proxy,
                browser_profile=None,
            )
            amount_text = str(probe.get("amount_text") or probe.get("amount") or "").strip()
            currency_text = str(probe.get("currency") or currency or "").lower()
            source_text = str(probe.get("amount_source") or "").strip()
            self._log(f"Plus checkout amount: amount={amount_text or 'unknown'} currency={currency_text} source={source_text or 'unknown'}")

            skip_save = not bool(probe.get("amount_is_zero"))
            reason = ""
            if skip_save:
                reason = f"Plus checkout amount != 0: amount={amount_text or 'unknown'} currency={currency_text or currency}"
                self._log(f"{reason}，注册失败且不保存账号", "warning")
            metadata.update(
                {
                    "chatgpt_checkout_amount": amount_text,
                    "chatgpt_checkout_amount_raw": probe.get("amount"),
                    "chatgpt_checkout_amount_source": source_text,
                    "chatgpt_checkout_amount_is_zero": not skip_save,
                    "chatgpt_checkout_probe": probe,
                    "chatgpt_skip_save_account": skip_save,
                    "chatgpt_skip_save_reason": reason,
                    "chatgpt_invalid_registration_failure": skip_save,
                    "chatgpt_invalid_registration_reason": reason,
                    "chatgpt_nonzero_checkout_amount_failure": skip_save,
                }
            )
            if skip_save:
                return metadata
        else:
            self._log("Plus 额度验证已关闭，仅生成订阅链接")
            metadata.update(
                {
                    "chatgpt_skip_save_account": False,
                    "chatgpt_skip_save_reason": "",
                }
            )
        return metadata

    @staticmethod
    def _classify_log_level(message: str, level: str = "info") -> str:
        return classify_task_log_level(message, level, flow="access_token_register")

    def _log(self, message: str, level: str = "info"):
        effective_level = self._classify_log_level(message, level)
        clean_message = str(message or "").strip()
        log_message = f"[DEBUG] {clean_message}" if effective_level == "debug" else clean_message
        self.logs.append(log_message)
        if self.callback_logger:
            try:
                self.callback_logger(log_message, effective_level)
            except TypeError:
                self.callback_logger(log_message)
        # Task callbacks already own the persisted log and stdout line. Emitting
        # the same message through the module logger produces a second unscoped
        # line in Docker logs. Standalone callers without a callback still use
        # the normal Python logger.
        if not self.callback_logger:
            if effective_level == "error":
                logger.error(log_message)
            elif effective_level == "warning":
                logger.warning(log_message)
            elif effective_level == "debug":
                logger.debug(log_message)
            else:
                logger.info(log_message)

    def _should_retry(self, message: str) -> bool:
        text = str(message or "").lower()
        if any(
            marker in text
            for marker in (
                "sentinel_browser_unavailable",
                "auth_browser_finalize_unavailable",
                "browser_registration_unavailable",
                "browser_registration_hard_timeout",
            )
        ):
            return False
        retriable_markers = [
            "tls",
            "ssl",
            "curl: (35)",
            "预授权被拦截",
            "authorize",
            "registration_disallowed",
            "http 400",
            "创建账号失败",
            "未获取到 authorization code",
            "consent",
            "workspace",
            "organization",
            "otp",
            "验证码",
            "session",
            "accessToken",
            "next-auth",
            "checkout",
            "stripe",
            "payment",
            "支付",
            "账单探测",
        ]
        return any(marker.lower() in text for marker in retriable_markers)

    def _run_any_auto_registration(
        self,
        *,
        chatgpt_client: ChatGPTClient,
        email_addr: str,
        password: str,
        skymail_adapter: EmailServiceAdapter,
        otp_wait_timeout: int,
        profile_name: str = "",
        profile_birthdate: str = "",
    ):
        """Run any-auto transport for the configured executor (protocol/headless/headed)."""
        from .any_auto.transport import AnyAutoRegistrationResult

        chatgpt_client.requested_executor = self.browser_mode
        chatgpt_client.effective_executor = self.browser_mode

        def _wait_code(
            *,
            email=None,
            timeout=120,
            pattern=None,
            otp_sent_at=None,
            exclude_codes=None,
            phase="any_auto_otp",
        ):
            nonlocal last_otp_length
            code = skymail_adapter.wait_for_verification_code(
                email or email_addr,
                timeout=max(int(timeout or otp_wait_timeout or 120), 30),
                otp_sent_at=otp_sent_at,
                exclude_codes=set(exclude_codes or set()),
                phase=str(phase or "any_auto_otp"),
                phase_label="any-auto 邮箱验证码",
            )
            last_otp_length = len(str(code or "").strip())
            return code

        def _otp_plain() -> str:
            nonlocal last_otp_length
            code = skymail_adapter.wait_for_verification_code(
                email_addr,
                timeout=max(int(otp_wait_timeout or 120), 30),
                phase="any_auto_browser_otp",
                phase_label="any-auto 浏览器邮箱验证码",
            )
            normalized = str(code or "").strip()
            last_otp_length = len(normalized)
            return normalized

        last_page_type = ""
        last_otp_length = 0

        def _legacy_http_trace(clean: str) -> str:
            """Normalize old transport chatter when a lower layer emits no hook."""

            match = re.search(
                r"(?P<method>GET|POST|PUT|PATCH|DELETE|HEAD)\s+(?P<url>https?://\S+)\s*(?:->|status=)\s*(?P<status>\d{3})",
                clean,
                flags=re.I,
            )
            if not match:
                match = re.search(
                    r"(?P<label>[^:]+?)\s*(?:->|状态[:：])\s*(?P<status>\d{3})\s*(?P<url>https?://\S+)?",
                    clean,
                    flags=re.I,
                )
                if not match:
                    return ""
                url = str(match.group("url") or "").strip()
                if not url:
                    return ""
                method = "GET" if "post" not in clean.lower() else "POST"
                return format_http_trace_log(
                    method,
                    url,
                    status=match.group("status"),
                    page=last_page_type,
                )
            return format_http_trace_log(
                match.group("method"),
                match.group("url"),
                status=match.group("status"),
                page=last_page_type,
            )

        def _transport_log(message: str) -> None:
            nonlocal last_page_type, last_otp_length
            clean = str(message or "").strip()
            if not clean:
                return

            # The protocol adapter already owns the canonical mailbox/OTP
            # summaries.  any-auto's legacy transport emits a second copy of
            # those milestones; keep the richer adapter line only.
            if clean.startswith(
                (
                    "[邮箱] 当前邮箱=",
                    "[验证码] 等待验证码",
                    "[验证码] 验证码已收到",
                    "[验证码] 验证码未收到",
                )
            ):
                return

            http_line = re.sub(r"^\s*\[DEBUG\]\s*", "", clean, flags=re.I)
            if http_line.upper().startswith("[HTTP]"):
                self._log(http_line, "debug")
                page_match = re.search(r"\bpage=([^\s｜]+)", http_line)
                if page_match:
                    last_page_type = str(page_match.group(1) or "").strip()
                return

            legacy_trace = _legacy_http_trace(clean)
            if legacy_trace:
                self._log(legacy_trace, "debug")
                return

            status_match = re.search(r"(?:状态|status)\s*[:：=]\s*(\d+)", clean, flags=re.I)
            http_status = str(status_match.group(1) or "") if status_match else ""
            page_match = re.search(r"(?:page|page_type)=([^\s｜]+)", clean, flags=re.I)
            if page_match:
                last_page_type = str(page_match.group(1) or "").strip()

            milestone = ""
            if clean.startswith(("[路由]", "[阶段]", "[结果]", "[验证码]", "[邮箱]", "[控制]", "[注册]", "[登录]")):
                milestone = clean
            elif "邮箱页已点击继续按钮" in clean:
                milestone = f"[注册] 邮箱入口已提交｜邮箱={mask_email_for_log(email_addr)}"
            elif "密码页提交状态" in clean:
                milestone = f"[注册] 注册密码已提交｜HTTP={http_status or '-'}"
            elif "验证码页提交状态" in clean:
                milestone = (
                    f"[验证码] 验证码已提交｜长度={last_otp_length or '-'} "
                    f"｜HTTP={http_status or '-'}｜下一页={last_page_type or '-'}"
                )
            elif "about_you 提交状态" in clean:
                milestone = f"[注册] about_you 资料已提交｜HTTP={http_status or '-'}"
            elif clean.startswith("注册流程完成:"):
                milestone = "[注册] OpenAI 账号创建完成"
            elif clean.startswith("开始抓取 ChatGPT Web Session:"):
                milestone = "[登录] 开始获取 ChatGPT Web Session"
            elif clean.startswith("ChatGPT Web Session 获取成功:"):
                account_match = re.search(r"\baccount_id=([^\s]+)", clean)
                account_suffix = (
                    f"｜OpenAI账号={account_match.group(1)}"
                    if account_match and account_match.group(1)
                    else ""
                )
                milestone = (
                    "[登录] ChatGPT Web Session 获取成功｜"
                    f"AT=是｜Session=是｜Cookie状态=已获取{account_suffix}"
                )
            elif clean.startswith(("失败", "异常", "错误", "未获取", "无法", "请求失败")):
                milestone = f"[结果] {clean}"

            if milestone:
                self._log(milestone, "warning" if any(marker in milestone for marker in ("失败", "异常", "错误")) else "info")

        if self._is_browser_executor():
            self._log(
                "[注册] 注册运输层已启动｜模式=浏览器｜范围=邮箱→OTP→资料→Web Session"
            )
            result = run_any_auto_browser_registration(
                email=email_addr,
                password=password,
                proxy_url=self.proxy_url,
                headless=self.browser_mode != "headed",
                otp_callback=_otp_plain,
                log_fn=_transport_log,
                profile_name=profile_name,
                profile_birthdate=profile_birthdate,
                stop_check=getattr(chatgpt_client, "_check_stop", None),
            )
        else:
            self._log(
                "[注册] 注册运输层已启动｜模式=协议｜范围=邮箱→OTP→资料→Web Session"
            )
            provider = str(
                getattr(getattr(self.email_service, "service_type", None), "value", "")
                or getattr(self.email_service, "provider", "")
                or "auto_gpt_mailbox"
            )

            def _create_email():
                return {
                    "email": email_addr,
                    "service_id": str(
                        getattr(self.email_service, "last_service_id", "")
                        or getattr(self.email_service, "token", "")
                        or ""
                    ),
                    "token": str(getattr(self.email_service, "token", "") or ""),
                }

            result = run_any_auto_protocol_registration(
                email=email_addr,
                password=password,
                proxy_url=self.proxy_url,
                wait_code=_wait_code,
                log_fn=_transport_log,
                provider=provider,
                create_email_fn=_create_email,
                prefer_password=True,
            )

        if not isinstance(result, AnyAutoRegistrationResult):
            result = AnyAutoRegistrationResult(
                success=False,
                email=email_addr,
                password=password,
                error_message="any_auto_transport_invalid_result",
                executor=self.browser_mode,
                transport="any_auto",
            )

        chatgpt_client.registration_transport = str(result.transport or "any_auto")
        chatgpt_client.effective_executor = str(result.executor or self.browser_mode)
        chatgpt_client.registration_runtime_profile = {
            "browser_family": "camoufox" if self._is_browser_executor() else "curl_cffi",
            "device_id": str(getattr(chatgpt_client, "device_id", "") or ""),
            "user_agent": "",
            "requested_executor": self.browser_mode,
            "effective_executor": str(result.executor or self.browser_mode),
            "transport": str(result.transport or "any_auto"),
            "profile_name": str(profile_name or ""),
            "profile_birthdate": str(profile_birthdate or ""),
        }
        chatgpt_client.registration_stage_transports = [
            {
                "stage": "registration",
                "transport": str(result.transport or "any_auto"),
                "executor": str(result.executor or self.browser_mode),
            },
            {
                "stage": "web_session_capture",
                "transport": str(result.transport or "any_auto"),
                "executor": str(result.executor or self.browser_mode),
                "status": "success" if result.ok else "failed",
            },
        ]
        if result.ok:
            self._log(
                "[登录] Web Session 材料已就绪｜"
                f"AT={'是' if result.access_token else '否'}｜"
                f"Session={'是' if result.session_token else '否'}｜"
                f"Cookie={'是' if result.cookie_header or result.cookies else '否'}｜"
                f"账号={result.account_id or '-'}",
            )
        else:
            self._log(
                f"[结果] 注册运输层失败｜原因={result.error_message or 'unknown'}",
                "warning",
            )
        return result

    def _capture_browser_oauth_tokens(
        self,
        *,
        chatgpt_client: ChatGPTClient,
        email_addr: str,
        password: str,
        skymail_adapter: EmailServiceAdapter,
    ) -> tuple[bool, dict[str, Any] | str]:
        if not self._is_browser_executor():
            return False, "browser_oauth_forbidden_for_protocol_executor"
        chatgpt_client.requested_executor = self.browser_mode
        chatgpt_client.effective_executor = self.browser_mode
        if not getattr(chatgpt_client, "registration_transport", None) or str(
            getattr(chatgpt_client, "registration_transport", "")
        ).startswith("protocol"):
            chatgpt_client.registration_transport = "camoufox_browser_oauth"
        if not getattr(chatgpt_client, "registration_runtime_profile", None):
            chatgpt_client.registration_runtime_profile = {
                "browser_family": "camoufox",
                "device_id": str(getattr(chatgpt_client, "device_id", "") or ""),
                "user_agent": "",
                "requested_executor": self.browser_mode,
                "effective_executor": self.browser_mode,
            }

        otp_timeout = self._read_int_config(
            "chatgpt_browser_oauth_otp_wait_seconds",
            fallback_keys=("chatgpt_otp_wait_seconds",),
            default=120,
            minimum=30,
            maximum=3600,
        )

        def _wait_for_browser_oauth_otp(request_payload: dict | None = None) -> str:
            request = dict(request_payload or {})
            sent_at = request.get("otp_sent_at")
            try:
                sent_at = float(sent_at) if sent_at is not None else None
            except (TypeError, ValueError):
                sent_at = None
            exclude_codes = skymail_adapter.used_codes_for_phases(
                "register_email_otp",
                "browser_register_email_otp",
                "oauth_email_otp",
                "browser_oauth_email_otp",
            )
            return str(
                skymail_adapter.wait_for_verification_code(
                    email_addr,
                    timeout=otp_timeout,
                    otp_sent_at=sent_at,
                    exclude_codes=exclude_codes,
                    phase="browser_oauth_email_otp",
                    phase_label="浏览器 OAuth 邮箱验证码",
                    ignore_budget=True,
                )
                or ""
            ).strip()

        hard_timeout_seconds = self._read_int_config(
            "chatgpt_browser_oauth_hard_timeout_seconds",
            default=420,
            minimum=120,
            maximum=600,
        )
        self._log("启动独立 Camoufox OAuth Token 捕获；不会接管或重放注册状态机")
        try:
            browser_result = run_browser_oauth_token_recovery(
                email=email_addr,
                password=password,
                otp_callback=_wait_for_browser_oauth_otp,
                proxy=self.proxy_url,
                device_id=str(getattr(chatgpt_client, "device_id", "") or ""),
                headless=self.browser_mode != "headed",
                stop_check=getattr(chatgpt_client, "_check_stop", None),
                hard_timeout_seconds=hard_timeout_seconds,
                log_fn=lambda message: self._log(f"[浏览器 OAuth] {message}"),
            )
        except Exception as exc:
            browser_error = str(exc or "浏览器 OAuth recovery 异常")
            self._log(f"浏览器 OAuth recovery 异常: {browser_error}", "warning")
            return False, browser_error

        if isinstance(browser_result, BrowserOAuthTokenRecoveryResult):
            if browser_result.ok:
                stages = list(getattr(chatgpt_client, "registration_stage_transports", None) or [])
                stages.append(
                    {
                        "stage": "oauth_token_capture",
                        "transport": "camoufox_browser",
                        "executor": self.browser_mode,
                        "status": "success",
                    }
                )
                chatgpt_client.registration_stage_transports = stages
                self._log("浏览器注册后 OAuth Token 提取成功")
                return True, dict(browser_result.tokens)
            browser_error = str(browser_result.error or "浏览器 OAuth recovery 未返回 Token")
        elif isinstance(browser_result, dict):
            browser_error = str(browser_result.get("error") or "")
            browser_tokens = {
                key: value
                for key, value in browser_result.items()
                if key != "error"
            }
            if (
                str(browser_tokens.get("access_token") or "").strip()
                and str(browser_tokens.get("refresh_token") or "").strip()
            ):
                stages = list(getattr(chatgpt_client, "registration_stage_transports", None) or [])
                stages.append(
                    {
                        "stage": "oauth_token_capture",
                        "transport": "camoufox_browser",
                        "executor": self.browser_mode,
                        "status": "success",
                    }
                )
                chatgpt_client.registration_stage_transports = stages
                self._log("浏览器注册后 OAuth Token 提取成功")
                return True, browser_tokens
            browser_error = browser_error or "浏览器 OAuth recovery 未返回 Token"
        else:
            browser_error = "浏览器 OAuth recovery 返回格式无效"
        stages = list(getattr(chatgpt_client, "registration_stage_transports", None) or [])
        stages.append(
            {
                "stage": "oauth_token_capture",
                "transport": "camoufox_browser",
                "executor": self.browser_mode,
                "status": "failed",
                "error": browser_error[:300],
            }
        )
        chatgpt_client.registration_stage_transports = stages
        self._log(f"浏览器 OAuth recovery 失败: {browser_error}", "warning")
        return False, browser_error

    def _finalize_email_service_success(self, result: RegistrationResult) -> None:
        if getattr(self, "_mailbox_finalized", False):
            return
        metadata = getattr(result, "metadata", None)
        metadata = metadata if isinstance(metadata, dict) else {}
        try:
            self.email_service._registration_result_code = (
                "registered_auth_pending"
                if metadata.get("registered_auth_pending")
                else "login_alive"
            )
            self.email_service._registration_access_token_saved = bool(
                str(getattr(result, "access_token", "") or "").strip()
            )
        except Exception:
            pass
        finalize = getattr(self.email_service, "finalize_success", None)
        if not callable(finalize):
            return
        try:
            finalize(
                account_email=str(getattr(result, "email", "") or self.email or "").strip(),
                task_id=str(self.task_uuid or "").strip(),
            )
            self._mailbox_finalized = True
        except Exception as exc:
            self._log(f"[邮箱] finalize_success 执行失败: {exc}", "warning")
        try:
            exporter = getattr(self.email_service, "export_state", None)
            if callable(exporter):
                refreshed = exporter() or {}
                if isinstance(refreshed, dict):
                    result.metadata = dict(result.metadata or {})
                    result.metadata["mailbox_state"] = refreshed
        except Exception as exc:
            self._log(f"[邮箱] finalize_success 后导出 mailbox_state 失败: {exc}", "warning")

    def _finalize_email_service_failure(
        self,
        result: RegistrationResult,
        *,
        fallback_error: str = "",
    ) -> None:
        if getattr(self, "_mailbox_finalized", False):
            return
        finalize = getattr(self.email_service, "finalize_failure", None)
        if not callable(finalize):
            return
        error_message = str(getattr(result, "error_message", "") or fallback_error or "").strip()
        try:
            finalize(
                error_message=error_message,
                task_id=str(self.task_uuid or "").strip(),
            )
            self._mailbox_finalized = True
        except Exception as exc:
            self._log(f"[邮箱] finalize_failure 执行失败: {exc}", "warning")
        finalize_outcome = str(
            getattr(self.email_service, "_registration_failure_outcome", "") or ""
        ).strip().lower()
        if finalize_outcome:
            result.metadata = dict(result.metadata or {})
            result.metadata["mailbox_finalize_outcome"] = finalize_outcome
        try:
            exporter = getattr(self.email_service, "export_state", None)
            if callable(exporter):
                refreshed = exporter() or {}
                if isinstance(refreshed, dict):
                    result.metadata = dict(result.metadata or {})
                    result.metadata["mailbox_state"] = refreshed
        except Exception as exc:
            self._log(f"[邮箱] finalize_failure 后导出 mailbox_state 失败: {exc}", "warning")

    def _finalize_email_on_interruption(
        self,
        result: RegistrationResult,
        *,
        stop_error: str = "",
        leased_email: str = "",
    ) -> None:
        """Always release/retire the HME lease when the task is stopped mid-attempt."""
        if getattr(self, "_mailbox_finalized", False):
            return
        email = str(
            getattr(result, "email", "") or leased_email or self.email or ""
        ).strip()
        if not email:
            # No mailbox was prepared yet — nothing to finalize.
            return
        if not str(getattr(result, "email", "") or "").strip():
            result.email = email
        message = str(
            getattr(result, "error_message", "")
            or stop_error
            or "任务已手动停止"
        ).strip()
        if "任务已手动停止" not in message and "stop" not in message.lower():
            message = f"任务已手动停止: {message}"
        result.error_message = message
        result.success = False
        self._log(f"[邮箱] 任务中断，强制 finalize HME lease: {email}", "warning")
        self._finalize_email_service_failure(result, fallback_error=message)

    @staticmethod
    def _metadata_indicates_discard_without_mailbox_writeback(metadata: dict | None) -> bool:
        if not isinstance(metadata, dict):
            return False
        return bool(metadata.get("chatgpt_payment_already_paid") or metadata.get("chatgpt_account_unavailable"))

    @staticmethod
    def _metadata_indicates_invalid_registration_failure(metadata: dict | None) -> bool:
        if not isinstance(metadata, dict):
            return False
        return bool(
            metadata.get("chatgpt_invalid_registration_failure")
            or metadata.get("chatgpt_payment_already_paid")
            or metadata.get("chatgpt_account_unavailable")
        )

    @staticmethod
    def _metadata_invalid_registration_reason(metadata: dict | None) -> str:
        if not isinstance(metadata, dict):
            return "账号无效"
        return str(
            metadata.get("chatgpt_invalid_registration_reason")
            or metadata.get("chatgpt_skip_save_reason")
            or metadata.get("chatgpt_unavailable_reason")
            or metadata.get("chatgpt_checkout_error_body")
            or "账号无效"
        ).strip()

    def _build_chatgpt_client(self) -> ChatGPTClient:
        stop_checker = self.extra_config.get("_task_stop_checker")
        task_control = self.extra_config.get("_task_control")
        task_attempt_id = self.extra_config.get("_task_attempt_id")
        if not callable(stop_checker) and task_control is not None:
            stop_checker = lambda: task_control.checkpoint(
                attempt_id=task_attempt_id
            )
        return ChatGPTClient(
            proxy=self.proxy_url,
            verbose=False,
            browser_mode=self.browser_mode,
            fingerprint=(
                self.extra_config.get("chatgpt_browser_fingerprint")
                or self.extra_config.get("browser_fingerprint")
            ),
            stop_checker=stop_checker,
        )

    def _build_registration_context_payload(
        self,
        *,
        chatgpt_client: ChatGPTClient,
        first_name: str,
        last_name: str,
        birthdate: str,
    ) -> dict[str, Any]:
        raw_runtime_profile = getattr(
            chatgpt_client,
            "registration_runtime_profile",
            None,
        )
        runtime_profile = (
            dict(raw_runtime_profile) if isinstance(raw_runtime_profile, dict) else {}
        )
        browser_owned = self._is_browser_executor()
        fingerprint = (
            None
            if browser_owned
            else build_browser_fingerprint_payload(getattr(chatgpt_client, "fingerprint", None))
        )
        return {
            "device_id": runtime_profile.get("device_id") or getattr(chatgpt_client, "device_id", "") or "",
            "user_agent": runtime_profile.get("user_agent") or getattr(chatgpt_client, "ua", None),
            "sec_ch_ua": None if browser_owned else getattr(chatgpt_client, "sec_ch_ua", None),
            "impersonate": None if browser_owned else getattr(chatgpt_client, "impersonate", None),
            "accept_language": None if browser_owned else getattr(chatgpt_client, "accept_language", None),
            "browser_fingerprint": (
                None
                if browser_owned
                else fingerprint or getattr(chatgpt_client, "fingerprint", None)
            ),
            "first_name": first_name,
            "last_name": last_name,
            "birthdate": birthdate,
            "requested_executor": str(
                getattr(chatgpt_client, "requested_executor", self.browser_mode)
                or self.browser_mode
            ),
            "effective_executor": str(
                getattr(chatgpt_client, "effective_executor", self.browser_mode)
                or self.browser_mode
            ),
            "registration_transport": str(
                getattr(chatgpt_client, "registration_transport", "protocol")
                or "protocol"
            ),
            "stage_transports": list(
                getattr(chatgpt_client, "registration_stage_transports", None)
                if isinstance(
                    getattr(chatgpt_client, "registration_stage_transports", None),
                    (list, tuple),
                )
                else []
            ),
            "browser_runtime_profile": runtime_profile,
        }

    def _attach_browser_fingerprint_metadata(self, metadata: dict[str, Any], chatgpt_client: ChatGPTClient) -> dict[str, Any]:
        raw_runtime_profile = getattr(
            chatgpt_client,
            "registration_runtime_profile",
            None,
        )
        runtime_profile = (
            dict(raw_runtime_profile) if isinstance(raw_runtime_profile, dict) else {}
        )
        if runtime_profile:
            metadata["chatgpt_browser_runtime_profile"] = runtime_profile
        if self._is_browser_executor():
            # Camoufox's measured runtime profile is authoritative. Do not label
            # its cookies or account as the temporary curl/Chrome fingerprint.
            return metadata
        fingerprint = build_browser_fingerprint_payload(getattr(chatgpt_client, "fingerprint", None))
        if not fingerprint:
            return metadata
        metadata["chatgpt_browser_fingerprint"] = fingerprint
        metadata["chatgpt_browser_fingerprint_signature"] = fingerprint_signature(fingerprint)
        metadata.setdefault("chatgpt_browser_fingerprint_source", "registration")
        metadata.setdefault("chatgpt_browser_fingerprint_isolated", True)
        return metadata

    def _probe_homepage_before_email_creation(self) -> tuple[bool, str]:
        self._prepared_register_client = None
        client = self._build_chatgpt_client()
        client._log = self._log
        keep_client = False
        try:
            max_probe_attempts = 3
            last_error = "访问首页失败"
            for probe_attempt in range(max_probe_attempts):
                if probe_attempt > 0:
                    self._log(f"预热首页重试 {probe_attempt + 1}/{max_probe_attempts}...")
                    client._reset_session()
                self._log("邮箱创建前预热访问 ChatGPT 首页...")
                if not client.visit_homepage():
                    probe = dict(getattr(client, "last_homepage_probe", {}) or {})
                    last_error = str(probe.get("detail") or probe.get("reason") or "访问首页失败").strip()
                    continue
                csrf_token = client.get_csrf_token()
                if not csrf_token:
                    last_error = "获取 CSRF token 失败"
                    continue
                # 预热成功后必须复用同一个 ChatGPTClient。不同随机浏览器/TLS 指纹
                # 在同一个代理出口下可能一会儿 200 一会儿 403；重新建 client 会把
                # 已经通过首页+CSRF 的 session 优势丢掉。
                self._prepared_register_client = client
                keep_client = True
                return True, ""
            return False, last_error
        except Exception as exc:
            return False, str(exc)
        finally:
            if not keep_client:
                try:
                    client.close()
                except Exception:
                    pass

    def _report_homepage_probe(self, ok: bool, detail: str = "") -> None:
        proxy_url = str(self.proxy_url or "").strip()
        if not proxy_url:
            return
        try:
            from core.proxy_pool import proxy_pool
        except Exception:
            return
        if ok:
            proxy_pool.report_homepage_success(proxy_url, status_code=200)
            return
        probe_text = str(detail or "").strip()
        status_code = 0
        if "status=" in probe_text:
            try:
                status_code = int(probe_text.split("status=", 1)[1].split()[0].strip())
            except Exception:
                status_code = 0
        proxy_pool.report_homepage_fail(
            proxy_url,
            error_message=probe_text,
            status_code=status_code,
        )

    def run(self) -> RegistrationResult:
        result = RegistrationResult(success=False, logs=self.logs)
        existing_account_capture = self._parse_bool(self.extra_config.get("chatgpt_existing_account_capture"))
        existing_account_login_route_allowed = self._is_existing_account_login_route_enabled()
        existing_account_login_route_event: dict[str, Any] | None = None
        registration_max_retries = self.max_retries
        if self._is_browser_executor() and not existing_account_capture:
            registration_max_retries = 1
            if self.max_retries > 1:
                self._log(
                    "浏览器注册完整链路固定为单次执行，禁止对结果不确定的 signup 重放",
                    "debug",
                )
        register_otp_wait_seconds = self._read_int_config(
            "chatgpt_register_otp_wait_seconds",
            fallback_keys=("chatgpt_otp_wait_seconds",),
            default=120,
            minimum=30,
            maximum=3600,
        )
        register_otp_resend_wait_seconds = self._read_int_config(
            "chatgpt_register_otp_resend_wait_seconds",
            fallback_keys=("chatgpt_register_otp_wait_seconds", "chatgpt_otp_wait_seconds"),
            default=90,
            minimum=30,
            maximum=3600,
        )
        register_otp_account_budget_seconds = self._read_int_config(
            "chatgpt_register_otp_account_budget_seconds",
            fallback_keys=("chatgpt_register_otp_single_account_budget_seconds",),
            default=register_otp_wait_seconds + register_otp_resend_wait_seconds,
            minimum=30,
            maximum=7200,
        )
        register_otp_budget = RegistrationOtpBudget(
            register_otp_account_budget_seconds,
            label="单账号注册邮箱验证码",
        )
        self._log(
            "验证码等待策略: "
            f"scope=single_account first_wait={register_otp_wait_seconds}s "
            f"resend_wait={register_otp_resend_wait_seconds}s "
            f"budget={register_otp_account_budget_seconds}s"
        )
        locked_email_addr = str(self.email or "").strip()
        locked_password = self.password or "AAb1234567890!"
        first_name, last_name = generate_random_name()
        birthdate = generate_random_birthday()
        attempt_client: ChatGPTClient | None = None
        email_initialized = False
        self._mailbox_finalized = False
        try:
            last_error = ""
            for attempt in range(registration_max_retries):
                try:
                    if attempt == 0:
                        self._log("=" * 60)
                        self._log("开始注册流程 V2 (Session 复用直取 AccessToken)", "debug")
                        self._log(f"请求模式: {self.browser_mode}", "debug")
                        if self._zero_amount_stop_enabled():
                            self._log(
                                f"Zero amount auto-stop enabled: threshold={self._zero_amount_stop_threshold()}"
                            )
                        self._log("=" * 60)
                    else:
                        self._log(f"整流程重试 {attempt + 1}/{registration_max_retries} ...")
                        time.sleep(1)

                    if existing_account_capture or self._is_browser_executor():
                        homepage_ok, homepage_error = True, ""
                        if self._is_browser_executor() and not existing_account_capture:
                            self._log(
                                "浏览器执行器直接进入 Camoufox 注册，跳过协议首页预热",
                                "debug",
                            )
                    elif attempt_client is not None:
                        homepage_ok, homepage_error = True, ""
                    else:
                        homepage_ok, homepage_error = self._probe_homepage_before_email_creation()
                    if not existing_account_capture and not self._is_browser_executor():
                        self._report_homepage_probe(homepage_ok, homepage_error)
                    if not homepage_ok:
                        last_error = homepage_error or "访问首页失败"
                        result.error_message = last_error
                        self._log(f"预热失败，跳过邮箱创建: {last_error}")
                        if attempt < registration_max_retries - 1 and self._should_retry(last_error):
                            continue
                        self._finalize_email_service_failure(result, fallback_error=result.error_message)
                        return result

                    # 一个 account attempt 只领取一次邮箱，并固定密码与人物资料。
                    email_claimed_now = not email_initialized
                    if not email_initialized:
                        raw_email_data = self.email_service.create_email()
                        mailbox_account = getattr(self.email_service, "_acct", None)
                        if isinstance(raw_email_data, dict):
                            email_data = dict(raw_email_data)
                        elif hasattr(raw_email_data, "items"):
                            try:
                                email_data = dict(raw_email_data)
                            except Exception:
                                email_data = {}
                        elif isinstance(raw_email_data, str):
                            # Older mailbox adapters returned the address
                            # directly instead of the current metadata dict.
                            email_data = {"email": raw_email_data}
                        else:
                            email_data = {
                                "email": str(getattr(raw_email_data, "email", "") or ""),
                                "service_id": str(
                                    getattr(raw_email_data, "service_id", "")
                                    or getattr(raw_email_data, "account_id", "")
                                    or ""
                                ),
                            }
                        email_initialized = True
                        if not locked_email_addr:
                            locked_email_addr = str(
                                email_data.get("email")
                                or getattr(mailbox_account, "email", "")
                                or ""
                            ).strip()
                    if not locked_email_addr:
                        result.error_message = "创建邮箱失败"
                        self._finalize_email_service_failure(result, fallback_error=result.error_message)
                        return result
                    email_addr = locked_email_addr
                    if self._task_log_context is not None:
                        self._task_log_context["email"] = email_addr
                    if email_claimed_now:
                        raw_mailbox_extra = getattr(mailbox_account, "extra", None)
                        if not isinstance(raw_mailbox_extra, dict):
                            raw_mailbox_extra = email_data.get("extra")
                        mailbox_extra = (
                            dict(raw_mailbox_extra)
                            if isinstance(raw_mailbox_extra, dict)
                            else {}
                        )
                        provider_label = str(
                            getattr(getattr(self.email_service, "service_type", None), "value", "")
                            or getattr(self.email_service, "provider", "")
                            or email_data.get("provider")
                            or self.email_service.__class__.__name__
                        ).strip()
                        lease_id = str(
                            mailbox_extra.get("lease_id")
                            or mailbox_extra.get("checkout_id")
                            or email_data.get("lease_id")
                            or email_data.get("checkout_id")
                            or email_data.get("service_id")
                            or getattr(mailbox_account, "account_id", "")
                            or "-"
                        ).strip()
                        mailbox_id = str(
                            mailbox_extra.get("mailbox_id")
                            or mailbox_extra.get("forward_mailbox_id")
                            or email_data.get("mailbox_id")
                            or email_data.get("forward_mailbox_id")
                            or "-"
                        ).strip()
                        mailbox_action = str(
                            email_data.get("mailbox_action")
                            or mailbox_extra.get("mailbox_action")
                            or "claimed"
                        ).strip()
                        self._log(
                            f"[邮箱] 邮箱已获取｜邮箱={mask_email_for_log(email_addr)} "
                            f"｜渠道={provider_label}｜租约={lease_id}｜邮箱ID={mailbox_id}｜动作={mailbox_action}"
                        )

                    result.email = email_addr
                    pwd = locked_password
                    result.password = pwd

                    self._log(
                        f"[注册] 注册资料已准备｜姓名=已生成｜生日=已生成｜密码长度={len(str(pwd or ''))}"
                    )

                    # 使用包装器为底层客户端提供接码服务
                    skymail_adapter = EmailServiceAdapter(
                        self.email_service,
                        email_addr,
                        self._log,
                        otp_budget=register_otp_budget,
                    )

                    # 2. 初始化 V2 客户端
                    chatgpt_client = (
                        attempt_client
                        or self._prepared_register_client
                        or self._build_chatgpt_client()
                    )
                    attempt_client = chatgpt_client
                    self._prepared_register_client = None
                    chatgpt_client._log = self._log
                    self._last_chatgpt_client = chatgpt_client
                    self._last_email_adapter = skymail_adapter

                    if existing_account_capture:
                        self._log("步骤 1/2: 已启用已有账号抓 AT，跳过注册状态机，直接登录...")
                        oauth_client = None
                        if self._is_browser_executor():
                            browser_ok, browser_payload = self._capture_browser_oauth_tokens(
                                chatgpt_client=chatgpt_client,
                                email_addr=email_addr,
                                password=pwd,
                                skymail_adapter=skymail_adapter,
                            )
                            tokens = dict(browser_payload) if browser_ok and isinstance(browser_payload, dict) else None
                            if not tokens:
                                last_error = str(browser_payload or "已有账号浏览器登录失败")
                        else:
                            try:
                                from services.chatgpt_core.oauth_client import OAuthClient

                                oauth_client = OAuthClient(
                                    self.extra_config,
                                    proxy=self.proxy_url,
                                    verbose=False,
                                    browser_mode=self.browser_mode,
                                )
                                oauth_client._log = lambda msg: self._log(f"[登录链路] {msg}")
                                tokens = oauth_client.login_and_get_tokens(
                                    email_addr,
                                    pwd,
                                    device_id=getattr(chatgpt_client, "device_id", "") or "",
                                    user_agent=getattr(chatgpt_client, "ua", None),
                                    sec_ch_ua=getattr(chatgpt_client, "sec_ch_ua", None),
                                    impersonate=getattr(chatgpt_client, "impersonate", None),
                                    browser_fingerprint=getattr(chatgpt_client, "fingerprint", None),
                                    skymail_client=skymail_adapter,
                                    prefer_passwordless_login=True,
                                    allow_phone_verification=False,
                                    force_new_browser=True,
                                    force_chatgpt_entry=False,
                                    screen_hint="login",
                                    force_password_login=bool(self.password),
                                    login_source="access_token_only:existing_account_capture",
                                )
                            except Exception as login_exc:
                                tokens = None
                                oauth_client = None
                                last_error = str(login_exc or "已有账号登录失败")
                        if not tokens:
                            last_error = str(getattr(oauth_client, "last_error", "") or last_error or "已有账号登录失败")
                            if attempt < registration_max_retries - 1 and self._should_retry(last_error):
                                self._log(f"已有账号登录失败，准备整流程重试: {last_error}")
                                continue
                            result.error_message = last_error
                            self._finalize_email_service_failure(result, fallback_error=last_error)
                            return result
                        session_ok = True
                        session_result = dict(tokens or {})
                        session_result.setdefault("access_token", str((tokens or {}).get("access_token") or ""))
                        session_result.setdefault("session_token", str((tokens or {}).get("session_token") or ""))
                        session_result.setdefault("account_id", str((tokens or {}).get("account_id") or ""))
                        session_result.setdefault("workspace_id", str((tokens or {}).get("workspace_id") or ""))
                    else:
                        any_auto_result = self._run_any_auto_registration(
                            chatgpt_client=chatgpt_client,
                            email_addr=email_addr,
                            password=pwd,
                            skymail_adapter=skymail_adapter,
                            otp_wait_timeout=register_otp_wait_seconds,
                            profile_name=f"{first_name} {last_name}".strip(),
                            profile_birthdate=birthdate,
                        )
                        success = bool(any_auto_result.ok)
                        if success:
                            msg = (
                                f"registration complete via any-auto "
                                f"executor={any_auto_result.executor} "
                                f"transport={any_auto_result.transport}"
                            )
                        else:
                            msg = (
                                any_auto_result.error_message
                                or "any_auto_registration_failed"
                            )
                        # any-auto transport already returns final AT/session material.
                        session_result = {
                            "access_token": any_auto_result.access_token,
                            "refresh_token": any_auto_result.refresh_token,
                            "id_token": any_auto_result.id_token,
                            "session_token": any_auto_result.session_token,
                            "account_id": any_auto_result.account_id,
                            "workspace_id": any_auto_result.workspace_id,
                            "cookies": any_auto_result.cookies,
                            "cookie_header": any_auto_result.cookie_header
                            or any_auto_result.cookies,
                            "user_id": any_auto_result.account_id,
                            "source": any_auto_result.source,
                            "metadata": dict(any_auto_result.metadata or {}),
                        }
                        if any_auto_result.password:
                            pwd = any_auto_result.password
                            self.password = pwd
                        session_ok = bool(any_auto_result.ok)

                    if not existing_account_capture:
                        if not success:
                            if is_existing_account_login_route_message(msg):
                                base_route_event = getattr(
                                    chatgpt_client,
                                    "last_registration_route_event",
                                    None,
                                )
                                base_route_event = (
                                    dict(base_route_event)
                                    if isinstance(base_route_event, dict)
                                    else {}
                                )
                                route_reason = str(
                                    base_route_event.get("reason") or msg or ""
                                ).strip()
                                existing_account_login_route_event = build_existing_account_login_route_event(
                                    email=email_addr,
                                    reason=route_reason,
                                    stage=str(
                                        base_route_event.get("stage")
                                        or (
                                            "browser_registration"
                                            if self._is_browser_executor()
                                            else "register_complete_flow"
                                        )
                                    ),
                                    enabled=existing_account_login_route_allowed,
                                    routed=existing_account_login_route_allowed,
                                    blocked=not existing_account_login_route_allowed,
                                    action="login_recovery" if existing_account_login_route_allowed else "skip_save",
                                    source=str(
                                        base_route_event.get("source")
                                        or "access_token_only_registration"
                                    ),
                                    deterministic=True,
                                    base_event=base_route_event,
                                )
                                if not existing_account_login_route_allowed:
                                    last_error = (
                                        "注册阶段检测到该邮箱已存在，已按配置跳过且不保存账号"
                                    )
                                    result.error_message = last_error
                                    result.email = email_addr
                                    result.password = pwd
                                    result.metadata = {
                                        "chatgpt_existing_account_login_route": existing_account_login_route_event,
                                    }
                                    self._log(
                                        "[已有账号] detection=deterministic "
                                        f"stage={existing_account_login_route_event.get('stage') or '-'} "
                                        f"signal={existing_account_login_route_event.get('signal') or '-'} "
                                        "action=skip mailbox=keep slot=0"
                                    )
                                    self._finalize_email_service_failure(result, fallback_error=last_error)
                                    raise ExistingAccountLoginRouteBlocked(
                                        email_addr,
                                        route_reason,
                                        existing_account_login_route_event,
                                    )

                                self._log(
                                    "[已有账号] detection=deterministic "
                                    f"stage={existing_account_login_route_event.get('stage') or '-'} "
                                    f"signal={existing_account_login_route_event.get('signal') or '-'} "
                                    "action=login_recovery"
                                )
                                tokens = None
                                login_error = ""
                                oauth_client = None
                                if self._is_browser_executor():
                                    try:
                                        browser_ok, browser_payload = self._capture_browser_oauth_tokens(
                                            chatgpt_client=chatgpt_client,
                                            email_addr=email_addr,
                                            password=pwd,
                                            skymail_adapter=skymail_adapter,
                                        )
                                        if browser_ok and isinstance(browser_payload, dict):
                                            tokens = dict(browser_payload)
                                        else:
                                            login_error = str(
                                                browser_payload
                                                or "浏览器已有账号登录恢复失败"
                                            )
                                    except Exception as login_exc:
                                        login_error = str(
                                            login_exc or "浏览器已有账号登录恢复失败"
                                        )
                                else:
                                    try:
                                        from services.chatgpt_core.oauth_client import OAuthClient

                                        oauth_client = OAuthClient(
                                            self.extra_config,
                                            proxy=self.proxy_url,
                                            verbose=False,
                                            browser_mode=self.browser_mode,
                                        )
                                        oauth_client._log = lambda message: self._log(f"[登录链路] {message}")
                                        tokens = oauth_client.login_and_get_tokens(
                                            email_addr,
                                            pwd,
                                            device_id=getattr(chatgpt_client, "device_id", "") or "",
                                            user_agent=getattr(chatgpt_client, "ua", None),
                                            sec_ch_ua=getattr(chatgpt_client, "sec_ch_ua", None),
                                            impersonate=getattr(chatgpt_client, "impersonate", None),
                                            browser_fingerprint=getattr(chatgpt_client, "fingerprint", None),
                                            skymail_client=skymail_adapter,
                                            prefer_passwordless_login=True,
                                            allow_phone_verification=False,
                                            force_new_browser=True,
                                            force_chatgpt_entry=False,
                                            screen_hint="login",
                                            force_password_login=False,
                                            complete_about_you_if_needed=True,
                                            first_name=first_name,
                                            last_name=last_name,
                                            birthdate=birthdate,
                                            login_source="access_token_only:existing_account_recovery",
                                            allow_add_phone_session_recovery=False,
                                        )
                                    except Exception as login_exc:
                                        tokens = None
                                        oauth_client = None
                                        login_error = str(
                                            login_exc or "已有账号登录恢复失败"
                                        )
                                if not tokens:
                                    login_error = str(
                                        getattr(oauth_client, "last_error", "")
                                        or login_error
                                        or "已有账号登录恢复失败"
                                    )
                                    last_error = (
                                        "user_already_exists: login_recovery_failed: "
                                        f"{login_error}"
                                    )
                                    if attempt < registration_max_retries - 1 and self._should_retry(last_error):
                                        self._log(f"已有账号登录恢复失败，准备整流程重试: {last_error}")
                                        continue
                                    result.error_message = last_error
                                    result.email = email_addr
                                    result.password = pwd
                                    result.metadata = {
                                        "chatgpt_existing_account_login_route": existing_account_login_route_event,
                                    }
                                    self._finalize_email_service_failure(result, fallback_error=last_error)
                                    return result
                                self._log(
                                    "[已有账号] action=login_recovery result=success saved=yes"
                                )
                                session_ok = True
                                session_result = dict(tokens or {})
                                session_result.setdefault("access_token", str((tokens or {}).get("access_token") or ""))
                                session_result.setdefault("session_token", str((tokens or {}).get("session_token") or ""))
                                session_result.setdefault("account_id", str((tokens or {}).get("account_id") or ""))
                                session_result.setdefault("workspace_id", str((tokens or {}).get("workspace_id") or ""))
                            else:
                                existing_account_login_route_event = None
                                last_error = f"注册流失败: {msg}"
                                if (
                                    attempt < registration_max_retries - 1
                                    and not skymail_adapter.is_otp_wait_budget_exhausted()
                                    and self._should_retry(msg)
                                ):
                                    self._log(f"注册流失败，准备整流程重试: {msg}")
                                    continue
                                result.error_message = last_error
                                self._finalize_email_service_failure(result, fallback_error=last_error)
                                return result
                        else:
                            existing_account_login_route_event = None

                        if not success and existing_account_login_route_event:
                            # 已按上面的登录恢复链路填充 session_result，不再复用注册会话。
                            pass
                        elif session_ok:
                            # any-auto transport already returned AT/session; no second-stage
                            # OAuth recovery or protocol reuse_session bridge.
                            self._log(
                                "步骤 2/2: any-auto 已返回 AccessToken/Session，跳过二次 OAuth recovery",
                                "debug",
                            )
                        else:
                            self._log(
                                "步骤 2/2: any-auto 未返回 AccessToken，按注册失败处理"
                                "（不再启动独立 OAuth recovery / auth_pending 半成品成功）",
                                "warning",
                            )
                            session_result = {
                                "error": msg or "any_auto_missing_access_token",
                            }

                    if session_ok:
                        # Browser any-auto already emits one canonical Web Session
                        # milestone. Keep the protocol executor's equivalent
                        # summary here without duplicating the browser milestone.
                        registration_transport = str(
                            getattr(chatgpt_client, "registration_transport", "") or ""
                        ).strip().lower()
                        if registration_transport != "any_auto_browser":
                            self._log(
                                "Token 提取完成｜"
                                f"executor={getattr(chatgpt_client, 'effective_executor', self.browser_mode)}｜"
                                f"transport={registration_transport or 'any_auto_protocol'}｜"
                                f"AT={'是' if session_result.get('access_token') else '否'}｜"
                                f"Session={'是' if session_result.get('session_token') else '否'}｜"
                                f"Cookie状态={'已获取' if session_result.get('cookie_header') or session_result.get('cookies') else '缺失'}"
                            )
                        result.access_token = session_result.get("access_token", "")
                        result.refresh_token = session_result.get("refresh_token", "")
                        result.id_token = session_result.get("id_token", "")
                        result.session_token = session_result.get("session_token", "")
                        result.account_id = (
                            session_result.get("account_id")
                            or session_result.get("user_id")
                            or ("v2_acct_" + chatgpt_client.device_id[:8])
                        )
                        result.workspace_id = session_result.get("workspace_id", "")
                        checkout_metadata = self._probe_plus_checkout_billing(session_result, email_addr)
                        transport_metadata = session_result.get("metadata")
                        transport_metadata = (
                            dict(transport_metadata)
                            if isinstance(transport_metadata, dict)
                            else {}
                        )
                        result.metadata = {
                            **transport_metadata,
                            "auth_provider": session_result.get("auth_provider", ""),
                            "expires": session_result.get("expires", ""),
                            "user_id": session_result.get("user_id", ""),
                            "user": session_result.get("user") or {},
                            "account": session_result.get("account") or {},
                            "cookies": session_result.get("cookies") or "",
                            "cookie_header": session_result.get("cookie_header") or session_result.get("cookies") or "",
                            "registration_context": self._build_registration_context_payload(
                                chatgpt_client=chatgpt_client,
                                first_name=first_name,
                                last_name=last_name,
                                birthdate=birthdate,
                            ),
                        }
                        result.metadata = self._attach_browser_fingerprint_metadata(result.metadata, chatgpt_client)
                        if existing_account_login_route_event:
                            result.metadata["chatgpt_existing_account_login_route"] = existing_account_login_route_event
                            result.metadata["existing_account_login_routed"] = True
                        export_state = getattr(self.email_service, "export_state", None)
                        if callable(export_state):
                            try:
                                result.metadata["mailbox_state"] = export_state()
                            except Exception:
                                pass
                        result.metadata.update(checkout_metadata)

                        if result.workspace_id:
                            self._log(f"Session Workspace ID: {result.workspace_id}")

                        if self._metadata_indicates_invalid_registration_failure(result.metadata):
                            failure_reason = self._metadata_invalid_registration_reason(result.metadata)
                            result.success = False
                            result.error_message = failure_reason
                            self._log(f"无 RT 注册判定为无效失败: {failure_reason}", "warning")
                            self._finalize_email_service_failure(result, fallback_error=failure_reason)
                            return result

                        result.success = True

                        self._log("=" * 60)
                        self._log("注册流程成功结束!")
                        self._log("=" * 60)
                        if self._metadata_indicates_discard_without_mailbox_writeback(result.metadata):
                            self._log("[邮箱] 账号不可用，跳过邮箱写回", "warning")
                        else:
                            self._finalize_email_service_success(result)
                        return result

                    last_error = f"注册未拿到 AccessToken（any-auto 合同失败）: {session_result}"
                    if attempt < registration_max_retries - 1:
                        self._log(f"{last_error}，准备整流程重试")
                        continue
                    result.error_message = last_error
                    self._finalize_email_service_failure(result, fallback_error=last_error)
                    return result
                except TaskInterruption as stop_exc:
                    self._finalize_email_on_interruption(
                        result,
                        stop_error=str(stop_exc or "") or "任务已手动停止",
                        leased_email=str(locked_email_addr or result.email or "").strip(),
                    )
                    raise
                except Exception as attempt_error:
                    last_error = str(attempt_error)
                    if attempt < registration_max_retries - 1 and self._should_retry(last_error):
                        self._log(f"本轮出现异常，准备整流程重试: {last_error}")
                        continue
                    raise

            result.error_message = last_error or "注册失败"
            self._finalize_email_service_failure(result, fallback_error=result.error_message)
            return result
                
        except TaskInterruption as stop_exc:
            self._finalize_email_on_interruption(
                result,
                stop_error=str(stop_exc or "") or "任务已手动停止",
                leased_email=str(locked_email_addr or result.email or "").strip(),
            )
            raise
        except Exception as e:
            self._log(f"无 RT 注册全流程执行异常: {e}", "error")
            import traceback
            traceback.print_exc()
            result.success = False
            result.error_message = str(e)
            self._finalize_email_service_failure(result, fallback_error=result.error_message)
            return result


# 兼容旧命名，逐步迁移到更见名知意的类名。
RegistrationEngineV2 = AccessTokenOnlyRegistrationEngine
