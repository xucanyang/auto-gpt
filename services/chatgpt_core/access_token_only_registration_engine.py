"""
注册流程引擎 V2
基于 curl_cffi 的注册状态机，注册成功后直接复用同一会话提取 ChatGPT Session。
"""

import json
import time
import logging
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
from .task_logging import classify_task_log_level
from .utils import generate_random_name, generate_random_birthday

logger = logging.getLogger(__name__)

class EmailServiceAdapter:
    """\u5c06 V1 \u7684 email_service \u9002\u914d\u6210 V2 \u6240\u9700\u7684\u63a5\u7801\u63a5\u53e3\u3002"""
    def __init__(self, email_service, email, log_fn, otp_budget: RegistrationOtpBudget | None = None):
        self.es = email_service
        self.email = email
        self.log_fn = log_fn
        self._used_codes_by_phase: dict[str, set[str]] = {}
        self._otp_budget = otp_budget

    def is_otp_wait_budget_exhausted(self) -> bool:
        budget = self._otp_budget
        return bool(budget and budget.is_exhausted())

    def wait_for_verification_code(
        self,
        email,
        timeout=60,
        otp_sent_at=None,
        exclude_codes=None,
        phase=None,
        phase_label=None,
    ):
        phase_key = str(phase or "email_otp").strip() or "email_otp"
        phase_title = str(phase_label or phase_key).strip() or phase_key
        used_codes = self._used_codes_by_phase.setdefault(phase_key, set())
        wait_plan = self._otp_budget.plan_wait(timeout) if self._otp_budget else None
        if wait_plan and wait_plan.exhausted:
            self.log_fn(
                f"[验证码] {phase_title} 已超过单账号验证码等待预算 "
                f"budget={self._otp_budget.total_seconds}s，停止等待当前账号"
            )
            return None
        try:
            fallback_timeout = max(int(timeout or 0), 1)
        except (TypeError, ValueError):
            fallback_timeout = 1
        effective_timeout = wait_plan.timeout_seconds if wait_plan else fallback_timeout
        if wait_plan and wait_plan.clamped:
            msg = (
                f"[验证码] 等待邮箱验证码：{phase_title} "
                f"timeout={effective_timeout}s requested={wait_plan.requested_seconds}s "
                f"single_account_remaining={wait_plan.remaining_seconds}s"
            )
        else:
            msg = f"[验证码] 等待邮箱验证码：{phase_title} timeout={effective_timeout}s"
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
            self.log_fn(f"[验证码] {phase_title} 等待超时: {exc}")
            return None
        if code:
            code = str(code).strip()
            used_codes.add(code)
            self.log_fn(f"[验证码] 验证码已获取：{phase_title}")
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
        self.browser_mode = browser_mode or "protocol"
        self.callback_logger = callback_logger
        self.task_uuid = task_uuid
        self.max_retries = max(1, int(max_retries or 1))
        self.extra_config = dict(extra_config or {})
        
        self.email = None
        self.password = None
        self.logs = []
        self._prepared_register_client: ChatGPTClient | None = None
        self._last_chatgpt_client: ChatGPTClient | None = None
        self._last_email_adapter = None

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

    def _should_capture_gopay_provider_link(self) -> bool:
        for key in (
            "chatgpt_access_token_only_gopay_provider_link_enabled",
            "chatgpt_gopay_provider_link_enabled",
        ):
            if key in self.extra_config and self.extra_config.get(key) not in (None, ""):
                return self._parse_bool(self.extra_config.get(key))
        return False

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
        gopay_defaults = self._json_object(self.extra_config.get("chatgpt_gopay_defaults"))
        mapping = {
            "name": "billing_name",
            "email": "billing_email",
            "country": "billing_country",
            "line1": "billing_line1",
            "city": "billing_city",
            "state": "billing_state",
            "postal_code": "billing_postal_code",
        }
        for target_key, source_key in mapping.items():
            value = gopay_defaults.get(source_key)
            if value not in (None, "") and not resolved.get(target_key):
                resolved[target_key] = value
        resolved.setdefault("email", email_addr)
        resolved.setdefault("country", country)
        resolved.setdefault("currency", currency)
        return resolved

    def _capture_gopay_provider_link(
        self,
        account: Any,
        *,
        checkout_url: str,
        country: str,
        currency: str,
        billing: dict,
        proxy: str,
    ) -> dict:
        metadata = {
            "chatgpt_gopay_provider_link_enabled": True,
            "chatgpt_gopay_provider_link_ready": False,
            "chatgpt_gopay_provider_link": "",
            "chatgpt_gopay_provider_link_error": "",
        }
        try:
            from services.chatgpt_core.gopay_flow import create_gopay_provider_link

            self._log("GoPay 平台链接: 开始进入 GoPay/Midtrans 平台链接阶段")
            snapshot = create_gopay_provider_link(
                account,
                account_id=0,
                plan="plus",
                country=country,
                currency=currency,
                proxy=proxy,
                checkout_url=checkout_url,
                billing=billing,
                proxy_source="registration_checkout_proxy",
                browser_profile=(
                    self.extra_config.get("gopay_browser_profile")
                    if isinstance(self.extra_config.get("gopay_browser_profile"), dict)
                    else None
                ),
                return_on_error=True,
            )
            provider_link = str(
                snapshot.get("payment_platform_url")
                or snapshot.get("midtrans_redirect_url")
                or ""
            ).strip()
            ready = bool(provider_link)
            if ready:
                self._log(f"GoPay 平台链接已获取: {provider_link}")
            else:
                error = str(snapshot.get("last_error") or "未返回有效 URL").strip()
                self._log(f"GoPay 平台链接未返回有效 URL: {error}", "warning")
            metadata.update(
                {
                    "chatgpt_gopay_provider_link_ready": ready,
                    "chatgpt_gopay_provider_link": provider_link,
                    "chatgpt_gopay_provider_link_error": "" if ready else str(snapshot.get("last_error") or "未返回有效 URL"),
                    "chatgpt_gopay_provider_link_snapshot": snapshot,
                    "chatgpt_gopay_provider_link_checkout_url": snapshot.get("checkout_url") or checkout_url,
                    "chatgpt_gopay_provider_link_cs_id": snapshot.get("cs_id") or "",
                    "chatgpt_gopay_provider_link_snap_token": snapshot.get("snap_token") or "",
                    "chatgpt_gopay_provider_link_stripe_redirect_url": snapshot.get("stripe_redirect_url") or "",
                    "chatgpt_gopay_provider_link_midtrans_redirect_url": snapshot.get("midtrans_redirect_url") or "",
                    "chatgpt_gopay_provider_link_payment_method_types": (
                        (snapshot.get("result") or {}).get("payment_method_types")
                        if isinstance(snapshot.get("result"), dict)
                        else []
                    ),
                    "chatgpt_gopay_provider_link_phase": snapshot.get("phase") or "",
                }
            )
        except Exception as exc:
            error = str(exc).strip() or exc.__class__.__name__
            self._log(f"GoPay 平台链接获取失败，账号仍按注册结果保存: {error}", "warning")
            metadata["chatgpt_gopay_provider_link_error"] = error
        return metadata

    def _probe_plus_checkout_billing(self, session_result: dict, email_addr: str) -> dict:
        if not self._should_probe_plus_checkout():
            return {}
        amount_check_enabled = self._is_checkout_amount_check_enabled()
        gopay_provider_link_enabled = self._should_capture_gopay_provider_link()
        if not amount_check_enabled and not gopay_provider_link_enabled:
            self._log("Plus 额度验证已关闭，跳过订阅链接生成和 amount 校验")
            return {
                "chatgpt_checkout_plan": "plus",
                "chatgpt_checkout_url": "",
                "chatgpt_checkout_amount_check_enabled": False,
                "chatgpt_skip_save_account": False,
                "chatgpt_skip_save_reason": "",
                "chatgpt_gopay_provider_link_enabled": False,
            }

        from services.chatgpt_core.gopay_flow import probe_chatgpt_checkout_amount
        from services.chatgpt_core.payment import (
            CheckoutRequestError,
            generate_plus_link,
            normalize_checkout_country,
            normalize_checkout_currency,
        )
        from core.proxy_utils import iter_enabled_runtime_proxies

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
        if self.extra_config.get("gopay_browser_profile"):
            account.extra["gopay_browser_profile"] = self.extra_config.get("gopay_browser_profile")
        if self.extra_config.get("gopay_processor_entity"):
            account.extra["gopay_processor_entity"] = self.extra_config.get("gopay_processor_entity")

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
        if gopay_provider_link_enabled:
            billing = {**billing, "country": country, "currency": currency}

        proxy_candidates = [str(item or "").strip() for item in iter_enabled_runtime_proxies(self.proxy_url) if str(item or "").strip()]
        if not proxy_candidates:
            raise RuntimeError("当前没有可用代理，无法生成订阅链接")
        checkout_proxy = proxy_candidates[0]

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
            "chatgpt_gopay_provider_link_enabled": gopay_provider_link_enabled,
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
                browser_profile=(
                    self.extra_config.get("gopay_browser_profile")
                    if isinstance(self.extra_config.get("gopay_browser_profile"), dict)
                    else None
                ),
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
            self._log("Plus 额度验证已关闭，仅为 GoPay 平台链接生成 checkout")
            metadata.update(
                {
                    "chatgpt_skip_save_account": False,
                    "chatgpt_skip_save_reason": "",
                }
            )

        if gopay_provider_link_enabled:
            metadata.update(
                self._capture_gopay_provider_link(
                    account,
                    checkout_url=checkout_url,
                    country=country,
                    currency=currency,
                    billing=billing,
                    proxy=checkout_proxy,
                )
            )
        return metadata

    @staticmethod
    def _artifact_key(artifact: dict[str, Any]) -> str:
        return str(
            artifact.get("variant_key")
            or f"{artifact.get('scope') or ''}:{artifact.get('workspace_id') or artifact.get('account_id') or ''}"
        ).strip()

    def _build_registration_session_artifact(
        self,
        *,
        result: RegistrationResult,
        session_result: dict[str, Any],
    ) -> dict[str, Any]:
        account_id = str(
            getattr(result, "account_id", "")
            or session_result.get("account_id")
            or session_result.get("user_id")
            or ""
        ).strip()
        workspace_id = str(
            getattr(result, "workspace_id", "")
            or session_result.get("workspace_id")
            or account_id
            or ""
        ).strip()
        stable_id = account_id or workspace_id or str(getattr(result, "email", "") or "").strip() or "unknown"
        return {
            "scope": "free",
            "label": "free",
            "account_id": account_id,
            "workspace_id": workspace_id,
            "access_token": str(getattr(result, "access_token", "") or session_result.get("access_token") or "").strip(),
            "refresh_token": str(getattr(result, "refresh_token", "") or "").strip(),
            "id_token": str(getattr(result, "id_token", "") or session_result.get("id_token") or "").strip(),
            "session_token": str(getattr(result, "session_token", "") or session_result.get("session_token") or "").strip(),
            "cookies": str(session_result.get("cookies") or session_result.get("cookie_header") or "").strip(),
            "source": "registration_session",
            "variant_key": f"free:{stable_id}",
            "auth_level": "access_token_only",
            "partial_auth": True,
            "display_name": "free",
            "space": {
                "name": "Personal",
                "structure": "personal",
                "plan_type": "",
                "is_default": True,
                "source": "registration_session",
            },
        }

    def _capture_k12_workspace_artifacts(
        self,
        *,
        result: RegistrationResult,
        chatgpt_client: ChatGPTClient,
        session_result: dict[str, Any],
    ) -> tuple[bool, str]:
        try:
            from services.chatgpt_core.k12_workspace import (
                capture_k12_and_all_spaces,
                k12_capture_enabled,
                safe_k12_error,
            )
        except Exception as exc:
            return True, f"K12 模块不可用: {exc}"

        if not k12_capture_enabled(self.extra_config):
            return True, ""

        try:
            capture = capture_k12_and_all_spaces(
                chatgpt_client=chatgpt_client,
                base_session=session_result,
                access_token=str(getattr(result, "access_token", "") or session_result.get("access_token") or ""),
                session_token=str(getattr(result, "session_token", "") or session_result.get("session_token") or ""),
                cookies=str(session_result.get("cookies") or session_result.get("cookie_header") or ""),
                target_workspace_ids=self.extra_config.get("chatgpt_k12_workspace_ids"),
                proxy=self.proxy_url or "",
                config=self.extra_config,
                log_fn=lambda msg, level="info": self._log(msg, level),
                stop_checker=self.extra_config.get("_task_stop_checker"),
            )
        except Exception as exc:
            if isinstance(exc, TaskInterruption):
                raise
            error = safe_k12_error(str(exc or exc.__class__.__name__).strip() or exc.__class__.__name__)
            if self._parse_bool(self.extra_config.get("chatgpt_k12_strict_join")):
                return False, f"K12 workspace 捕获异常: {error}"
            self._log(f"K12 workspace 捕获异常，已保留基础账号: {error}", "warning")
            return True, error
        metadata = getattr(result, "metadata", None)
        if not isinstance(metadata, dict):
            metadata = {}
            result.metadata = metadata
        summary = capture.get("summary") if isinstance(capture.get("summary"), dict) else {}
        metadata["chatgpt_k12_join_summary"] = summary
        metadata["chatgpt_k12_join_results"] = capture.get("join_results") or []
        metadata["chatgpt_all_spaces"] = capture.get("spaces") or []
        if capture.get("exchange_failures"):
            metadata["chatgpt_k12_exchange_failures"] = capture.get("exchange_failures")

        if summary.get("strict_join_failed"):
            return False, "K12 workspace join 失败（strict_join=true）"

        primary_artifact = self._build_registration_session_artifact(
            result=result,
            session_result=session_result,
        )
        artifacts: list[dict[str, Any]] = [primary_artifact]
        seen = {self._artifact_key(primary_artifact)}
        for artifact in capture.get("artifacts") or []:
            if not isinstance(artifact, dict):
                continue
            key = self._artifact_key(artifact)
            if not key or key in seen:
                continue
            seen.add(key)
            artifacts.append(artifact)
        result.workspace_artifacts = artifacts
        metadata["workspace_artifact_summaries"] = [
            {
                "scope": str(item.get("scope") or ""),
                "label": str(item.get("label") or ""),
                "account_id": str(item.get("account_id") or ""),
                "workspace_id": str(item.get("workspace_id") or ""),
                "source": str(item.get("source") or ""),
            }
            for item in artifacts
        ]
        return True, ""

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

    def _finalize_email_service_success(self, result: RegistrationResult) -> None:
        finalize = getattr(self.email_service, "finalize_success", None)
        if not callable(finalize):
            return
        try:
            finalize(
                account_email=str(getattr(result, "email", "") or self.email or "").strip(),
                task_id=str(self.task_uuid or "").strip(),
            )
        except Exception as exc:
            self._log(f"[邮箱] finalize_success 执行失败: {exc}", "warning")

    def _finalize_email_service_failure(
        self,
        result: RegistrationResult,
        *,
        fallback_error: str = "",
    ) -> None:
        finalize = getattr(self.email_service, "finalize_failure", None)
        if not callable(finalize):
            return
        error_message = str(getattr(result, "error_message", "") or fallback_error or "").strip()
        try:
            finalize(
                error_message=error_message,
                task_id=str(self.task_uuid or "").strip(),
            )
        except Exception as exc:
            self._log(f"[邮箱] finalize_failure 执行失败: {exc}", "warning")

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
        return ChatGPTClient(
            proxy=self.proxy_url,
            verbose=False,
            browser_mode=self.browser_mode,
            fingerprint=(
                self.extra_config.get("chatgpt_browser_fingerprint")
                or self.extra_config.get("browser_fingerprint")
            ),
        )

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
        try:
            last_error = ""
            for attempt in range(self.max_retries):
                try:
                    if attempt == 0:
                        self._log("=" * 60)
                        self._log("开始注册流程 V2 (Session 复用直取 AccessToken)")
                        self._log(f"请求模式: {self.browser_mode}")
                        if self._zero_amount_stop_enabled():
                            self._log(
                                f"Zero amount auto-stop enabled: threshold={self._zero_amount_stop_threshold()}"
                            )
                        self._log("=" * 60)
                    else:
                        self._log(f"整流程重试 {attempt + 1}/{self.max_retries} ...")
                        time.sleep(1)

                    if not existing_account_capture:
                        self._log(
                            "[已有账号] 注册阶段遇到已注册邮箱时"
                            f"{'允许路由到登录恢复' if existing_account_login_route_allowed else '禁止路由到登录恢复，将跳过且不保存'}"
                        )

                    homepage_ok, homepage_error = (True, "") if existing_account_capture else self._probe_homepage_before_email_creation()
                    if not existing_account_capture:
                        self._report_homepage_probe(homepage_ok, homepage_error)
                    if not homepage_ok:
                        last_error = homepage_error or "访问首页失败"
                        result.error_message = last_error
                        self._log(f"预热失败，跳过邮箱创建: {last_error}")
                        if attempt < self.max_retries - 1 and self._should_retry(last_error):
                            continue
                        self._finalize_email_service_failure(result, fallback_error=result.error_message)
                        return result

                    # 1. 创建邮箱
                    email_data = self.email_service.create_email()
                    email_addr = self.email or (email_data.get('email') if email_data else None)
                    if not email_addr:
                        result.error_message = "创建邮箱失败"
                        self._finalize_email_service_failure(result, fallback_error=result.error_message)
                        return result

                    result.email = email_addr

                    pwd = self.password or "AAb1234567890!"
                    result.password = pwd

                    # 随机姓名、生日
                    first_name, last_name = generate_random_name()
                    birthdate = generate_random_birthday()

                    self._log(f"邮箱: {email_addr}, 密码: {pwd}")
                    self._log(f"注册信息: {first_name} {last_name}, 生日: {birthdate}")

                    # 使用包装器为底层客户端提供接码服务
                    skymail_adapter = EmailServiceAdapter(
                        self.email_service,
                        email_addr,
                        self._log,
                        otp_budget=register_otp_budget,
                    )

                    # 2. 初始化 V2 客户端
                    chatgpt_client = self._prepared_register_client or self._build_chatgpt_client()
                    self._prepared_register_client = None
                    chatgpt_client._log = self._log
                    self._last_chatgpt_client = chatgpt_client
                    self._last_email_adapter = skymail_adapter

                    if existing_account_capture:
                        self._log("步骤 1/2: 已启用已有账号抓 AT，跳过注册状态机，直接登录...")
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
                            if attempt < self.max_retries - 1 and self._should_retry(last_error):
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
                        self._log("步骤 1/2: 执行注册状态机...")

                        success, msg = chatgpt_client.register_complete_flow(
                            email_addr,
                            pwd,
                            first_name,
                            last_name,
                            birthdate,
                            skymail_adapter,
                            otp_wait_timeout=register_otp_wait_seconds,
                            otp_resend_wait_timeout=register_otp_resend_wait_seconds,
                            otp_account_budget_timeout=register_otp_account_budget_seconds,
                            allow_existing_account_login_route=existing_account_login_route_allowed,
                        )

                    if not existing_account_capture:
                        if not success:
                            if is_existing_account_login_route_message(msg):
                                existing_account_login_route_event = build_existing_account_login_route_event(
                                    email=email_addr,
                                    reason=msg,
                                    stage="register_complete_flow",
                                    enabled=existing_account_login_route_allowed,
                                    routed=existing_account_login_route_allowed,
                                    blocked=not existing_account_login_route_allowed,
                                    action="login_recovery" if existing_account_login_route_allowed else "skip_save",
                                    source="access_token_only_registration",
                                    base_event=getattr(chatgpt_client, "last_registration_route_event", None),
                                )
                                if not existing_account_login_route_allowed:
                                    last_error = "注册阶段检测到该邮箱已存在，已按配置禁止路由到登录，账号未保存"
                                    result.error_message = last_error
                                    result.email = email_addr
                                    result.password = pwd
                                    result.metadata = {
                                        "chatgpt_existing_account_login_route": existing_account_login_route_event,
                                    }
                                    self._log(
                                        f"[已有账号] 已跳过并禁止保存: {email_addr or '-'} reason={msg}",
                                        "warning",
                                    )
                                    self._finalize_email_service_failure(result, fallback_error=last_error)
                                    raise ExistingAccountLoginRouteBlocked(
                                        email_addr,
                                        msg,
                                        existing_account_login_route_event,
                                    )

                                self._log(
                                    f"[已有账号] 注册阶段命中已注册邮箱，无 RT 方案切换到登录恢复: {email_addr}",
                                    "warning",
                                )
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
                                    last_error = str(login_exc or "已有账号登录恢复失败")
                                if not tokens:
                                    last_error = str(getattr(oauth_client, "last_error", "") or last_error or "已有账号登录恢复失败")
                                    if attempt < self.max_retries - 1 and self._should_retry(last_error):
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
                                    attempt < self.max_retries - 1
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
                        else:
                            self._log("步骤 2/2: 复用注册会话，直接获取 ChatGPT Session / AccessToken...")
                            session_ok, session_result = chatgpt_client.reuse_session_and_get_tokens()

                    if session_ok:
                        self._log("Token 提取完成！")
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
                        result.metadata = {
                            "auth_provider": session_result.get("auth_provider", ""),
                            "expires": session_result.get("expires", ""),
                            "user_id": session_result.get("user_id", ""),
                            "user": session_result.get("user") or {},
                            "account": session_result.get("account") or {},
                            "cookies": session_result.get("cookies") or "",
                            "cookie_header": session_result.get("cookie_header") or session_result.get("cookies") or "",
                        }
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

                        k12_ok, k12_error = self._capture_k12_workspace_artifacts(
                            result=result,
                            chatgpt_client=chatgpt_client,
                            session_result=session_result,
                        )
                        if not k12_ok:
                            result.success = False
                            result.error_message = k12_error or "K12 workspace 捕获失败"
                            self._log(result.error_message, "warning")
                            self._finalize_email_service_failure(result, fallback_error=result.error_message)
                            return result

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

                    last_error = f"注册成功，但复用会话获取 AccessToken 失败: {session_result}"
                    if attempt < self.max_retries - 1:
                        self._log(f"{last_error}，准备整流程重试")
                        continue
                    result.error_message = last_error
                    self._finalize_email_service_failure(result, fallback_error=last_error)
                    return result
                except TaskInterruption:
                    raise
                except Exception as attempt_error:
                    last_error = str(attempt_error)
                    if attempt < self.max_retries - 1 and self._should_retry(last_error):
                        self._log(f"本轮出现异常，准备整流程重试: {last_error}")
                        continue
                    raise

            result.error_message = last_error or "注册失败"
            self._finalize_email_service_failure(result, fallback_error=result.error_message)
            return result
                
        except TaskInterruption:
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
