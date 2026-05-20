"""
注册流程引擎 V2
基于 curl_cffi 的注册状态机，注册成功后直接复用同一会话提取 ChatGPT Session。
"""

import time
import logging
from datetime import datetime
from typing import Any, Optional, Callable

from core.task_runtime import TaskInterruption
from services.chatgpt_core.refresh_token_registration_engine import RegistrationResult

from .chatgpt_client import ChatGPTClient
from .utils import generate_random_name, generate_random_birthday

logger = logging.getLogger(__name__)

class EmailServiceAdapter:
    """\u5c06 V1 \u7684 email_service \u9002\u914d\u6210 V2 \u6240\u9700\u7684\u63a5\u7801\u63a5\u53e3\u3002"""
    def __init__(self, email_service, email, log_fn):
        self.es = email_service
        self.email = email
        self.log_fn = log_fn
        self._used_codes_by_phase: dict[str, set[str]] = {}

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
        msg = f"\u6b63\u5728\u7b49\u5f85\u90ae\u7bb1 {email} \u7684\u9a8c\u8bc1\u7801（{phase_title}, {timeout}s）..."
        self.log_fn(msg)
        code = self.es.get_verification_code(
            timeout=timeout,
            otp_sent_at=otp_sent_at,
            exclude_codes=set(exclude_codes or set()) | set(used_codes),
            phase=phase_key,
            phase_label=phase_title,
        )
        if code:
            code = str(code).strip()
            used_codes.add(code)
            self.log_fn(f"\u6210\u529f\u83b7\u53d6\u9a8c\u8bc1\u7801（{phase_title}）")
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
    def _parse_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        return str(value or "").strip().lower() in {"1", "true", "yes", "on"}

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

    def _probe_plus_checkout_billing(self, session_result: dict, email_addr: str) -> dict:
        if not self._should_probe_plus_checkout():
            return {}
        if not self._is_checkout_amount_check_enabled():
            self._log("Plus 额度验证已关闭，跳过订阅链接生成和 amount 校验")
            return {
                "chatgpt_checkout_plan": "plus",
                "chatgpt_checkout_url": "",
                "chatgpt_checkout_amount_check_enabled": False,
                "chatgpt_skip_save_account": False,
                "chatgpt_skip_save_reason": "",
            }

        from services.chatgpt_core.gopay_flow import probe_chatgpt_checkout_amount
        from services.chatgpt_core.payment import (
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
        account.email = email_addr
        account.extra = {
            "account_id": str(session_result.get("account_id") or session_result.get("user_id") or ""),
            "workspace_id": str(session_result.get("workspace_id") or ""),
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
        billing = self.extra_config.get("chatgpt_checkout_billing")
        if not isinstance(billing, dict):
            billing = self.extra_config.get("billing") if isinstance(self.extra_config.get("billing"), dict) else {}

        proxy_candidates = [str(item or "").strip() for item in iter_enabled_runtime_proxies(self.proxy_url) if str(item or "").strip()]
        if not proxy_candidates:
            raise RuntimeError("当前没有可用代理，无法生成订阅链接")
        checkout_proxy = proxy_candidates[0]

        self._log(f"Plus 账单探测: 生成订阅链接 country={country} currency={currency}")
        checkout_url = generate_plus_link(
            account,
            proxy=checkout_proxy,
            country=country,
            currency=currency,
            billing=billing,
        )
        self._log(f"Plus checkout created: {checkout_url}")
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
            self._log(f"{reason}，注册成功但不保存账号", "warning")
        return {
            "chatgpt_checkout_plan": "plus",
            "chatgpt_checkout_url": checkout_url,
            "chatgpt_checkout_country": country,
            "chatgpt_checkout_currency": currency,
            "chatgpt_checkout_amount_check_enabled": True,
            "chatgpt_checkout_amount": amount_text,
            "chatgpt_checkout_amount_raw": probe.get("amount"),
            "chatgpt_checkout_amount_source": source_text,
            "chatgpt_checkout_amount_is_zero": not skip_save,
            "chatgpt_access_token_only_zero_amount_stop_enabled": self._zero_amount_stop_enabled(),
            "chatgpt_access_token_only_zero_amount_stop_threshold": self._zero_amount_stop_threshold(),
            "chatgpt_checkout_probe": probe,
            "chatgpt_skip_save_account": skip_save,
            "chatgpt_skip_save_reason": reason,
        }

    @staticmethod
    def _classify_log_level(message: str, level: str = "info") -> str:
        normalized_level = str(level or "info").strip().lower() or "info"
        if normalized_level in {"error", "warning", "debug"}:
            return normalized_level
        text = str(message or "").strip()
        normalized_text = text
        while normalized_text.startswith("[") and "]" in normalized_text:
            _, _, rest = normalized_text.partition("]")
            if not rest:
                break
            normalized_text = rest.strip()
        debug_prefixes = (
            "开始 OAuth 登录流程...",
            "OAuth 策略:",
            "OAuth 状态起点:",
            "注册状态起点:",
            "注册状态推进:",
            "状态步进[",
            "follow[",
            "workspace 解析入口:",
            "workspace 候选:",
            "workspace session 数据为空:",
            "Sentinel Browser 模式:",
            "Sentinel Browser 启动:",
            "Sentinel Browser 成功:",
            "business recovery:",
            "Authorize →",
            "请求模式:",
            "实现策略:",
            "流程策略:",
            "验证码等待策略:",
            "邮箱:",
            "密码:",
            "注册信息:",
            "正在创建 ",
            "生成固定域名邮箱:",
            "命中验证码:",
            "成功获取验证码（",
            "正在等待邮箱 ",
            "成功创建邮箱:",
        )
        if text.startswith("="):
            return "debug"
        if text[:2].isdigit() and len(text) > 2 and text[2] == ".":
            return "debug"
        if normalized_text.startswith(debug_prefixes) or text.startswith(debug_prefixes) or "page=" in text or "authorize_continue:" in text or "/oauth/authorize ->" in text or "login_session: 已获取" in text:
            return "debug"
        return "info"

    def _log(self, message: str, level: str = "info"):
        effective_level = self._classify_log_level(message, level)
        clean_message = str(message or "").strip()
        log_message = f"[DEBUG] {clean_message}" if effective_level == "debug" else clean_message
        self.logs.append(log_message)
        if self.callback_logger:
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

    def _probe_homepage_before_email_creation(self) -> tuple[bool, str]:
        client = ChatGPTClient(
            proxy=self.proxy_url,
            verbose=False,
            browser_mode=self.browser_mode,
        )
        client._log = self._log
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
                return True, ""
            return False, last_error
        except Exception as exc:
            return False, str(exc)
        finally:
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

                    homepage_ok, homepage_error = self._probe_homepage_before_email_creation()
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
                    skymail_adapter = EmailServiceAdapter(self.email_service, email_addr, self._log)

                    # 2. 初始化 V2 客户端
                    chatgpt_client = ChatGPTClient(
                        proxy=self.proxy_url,
                        verbose=False,
                        browser_mode=self.browser_mode,
                    )
                    chatgpt_client._log = self._log

                    self._log("步骤 1/2: 执行注册状态机...")

                    success, msg = chatgpt_client.register_complete_flow(
                        email_addr, pwd, first_name, last_name, birthdate, skymail_adapter
                    )

                    if not success:
                        last_error = f"注册流失败: {msg}"
                        if attempt < self.max_retries - 1 and self._should_retry(msg):
                            self._log(f"注册流失败，准备整流程重试: {msg}")
                            continue
                        result.error_message = last_error
                        self._finalize_email_service_failure(result, fallback_error=last_error)
                        return result

                    self._log("步骤 2/2: 复用注册会话，直接获取 ChatGPT Session / AccessToken...")
                    session_ok, session_result = chatgpt_client.reuse_session_and_get_tokens()

                    if session_ok:
                        self._log("Token 提取完成！")
                        result.success = True
                        result.access_token = session_result.get("access_token", "")
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
                        }
                        result.metadata.update(checkout_metadata)

                        if result.workspace_id:
                            self._log(f"Session Workspace ID: {result.workspace_id}")

                        self._log("=" * 60)
                        self._log("注册流程成功结束!")
                        self._log("=" * 60)
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
            result.error_message = str(e)
            self._finalize_email_service_failure(result, fallback_error=result.error_message)
            return result


# 兼容旧命名，逐步迁移到更见名知意的类名。
RegistrationEngineV2 = AccessTokenOnlyRegistrationEngine
