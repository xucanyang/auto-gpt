"""
ChatGPT Refresh Token 注册引擎。

新实现不再沿用旧的分步补丁式注册链路，而是直接复用：
1. `ChatGPTClient.register_complete_flow()` 负责完整注册状态机
2. `OAuthClient.login_and_get_tokens()` 负责全新 OAuth + passwordless OTP 登录拿 RT

目标是让 refresh_token 模式与当前主状态机链路保持一致，不再以旧流程做兜底。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from core.task_runtime import SkipCurrentAttemptRequested, TaskInterruption

from .chatgpt_client import ChatGPTClient
from .oauth import OAuthManager
from .oauth_client import OAuthClient
from .otp_budget import RegistrationOtpBudget
from .registration_route_policy import (
    ExistingAccountLoginRouteBlocked,
    build_existing_account_login_route_event,
    existing_account_login_route_enabled,
)
from .task_logging import classify_task_log_level
from .account_fingerprint import build_browser_fingerprint_payload, fingerprint_signature
from .utils import (
    decode_jwt_payload,
    generate_random_birthday,
    generate_random_name,
    generate_random_password,
)

logger = logging.getLogger(__name__)


@dataclass
class RegistrationResult:
    """注册结果。"""

    success: bool
    email: str = ""
    password: str = ""
    account_id: str = ""
    workspace_id: str = ""
    access_token: str = ""
    refresh_token: str = ""
    id_token: str = ""
    session_token: str = ""
    error_message: str = ""
    logs: list | None = None
    metadata: dict | None = None
    source: str = "register"
    workspace_artifacts: list[dict[str, Any]] | None = None

    def to_dict(self) -> Dict[str, Any]:
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
            "workspace_artifacts": [
                {
                    "scope": str(item.get("scope") or ""),
                    "label": str(item.get("label") or ""),
                    "account_id": str(item.get("account_id") or ""),
                    "workspace_id": str(item.get("workspace_id") or ""),
                }
                for item in (self.workspace_artifacts or [])
                if isinstance(item, dict)
            ],
        }


@dataclass
class SignupFormResult:
    """保留旧结构，兼容外部引用。"""

    success: bool
    page_type: str = ""
    is_existing_account: bool = False
    response_data: Dict[str, Any] | None = None
    error_message: str = ""


class EmailServiceAdapter:
    """将现有 email_service 适配给 ChatGPTClient / OAuthClient 状态机。"""

    def __init__(
        self,
        email_service,
        email: str,
        log_fn: Callable[[str], None],
        otp_budget: RegistrationOtpBudget | None = None,
    ):
        self.email_service = email_service
        self.email = email
        self.log_fn = log_fn
        self._used_codes_by_phase: dict[str, set[str]] = {}
        self._used_message_ids_by_phase: dict[str, set[str]] = {}
        self._last_verification_result_by_phase: dict[str, dict[str, Any]] = {}
        self._otp_budget = otp_budget

    def _log(self, message: str, level: str = "info") -> None:
        try:
            self.log_fn(message, level)
        except TypeError:
            self.log_fn(message)

    def _read_last_verification_result(self) -> dict[str, Any]:
        meta = getattr(self.email_service, "_last_verification_result", None)
        if isinstance(meta, dict):
            return dict(meta)
        return {}

    def is_otp_wait_budget_exhausted(self) -> bool:
        budget = self._otp_budget
        return bool(budget and budget.is_exhausted())

    def _mark_message_processed(self, message_id: str) -> None:
        marker = getattr(self.email_service, "mark_verification_message_processed", None)
        if callable(marker):
            try:
                marker(message_id)
            except Exception as exc:
                self._log(f"标记已处理验证码邮件失败: {exc}", "debug")

    def get_last_verification_result(self, phase: str | None = None) -> dict[str, Any]:
        phase_key = str(phase or "").strip()
        if phase_key:
            return dict(self._last_verification_result_by_phase.get(phase_key) or {})
        return dict(getattr(self.email_service, "_last_verification_result", None) or {})

    def wait_for_verification_code(
        self,
        email: str,
        timeout: int = 90,
        otp_sent_at: float | None = None,
        exclude_codes=None,
        phase: str | None = None,
        phase_label: str | None = None,
    ):
        phase_key = str(phase or "email_otp").strip() or "email_otp"
        phase_title = str(phase_label or phase_key).strip() or phase_key
        used_codes = self._used_codes_by_phase.setdefault(phase_key, set())
        used_message_ids = self._used_message_ids_by_phase.setdefault(phase_key, set())
        excluded_codes = {
            str(code).strip()
            for code in (exclude_codes or set())
            if str(code or "").strip()
        }
        wait_plan = self._otp_budget.plan_wait(timeout) if self._otp_budget else None
        if wait_plan and wait_plan.exhausted:
            self._log(
                f"[验证码] {phase_title} 已超过单账号验证码等待预算 "
                f"budget={self._otp_budget.total_seconds}s，停止等待当前账号"
            )
            return None
        try:
            fallback_timeout = max(int(timeout or 0), 1)
        except (TypeError, ValueError):
            fallback_timeout = 1
        effective_timeout = wait_plan.timeout_seconds if wait_plan else fallback_timeout
        deadline = time.monotonic() + effective_timeout
        if wait_plan and wait_plan.clamped:
            self._log(
                f"[验证码] 等待邮箱验证码：{phase_title} "
                f"timeout={effective_timeout}s requested={wait_plan.requested_seconds}s "
                f"single_account_remaining={wait_plan.remaining_seconds}s"
            )
        else:
            self._log(f"[验证码] 等待邮箱验证码：{phase_title} timeout={effective_timeout}s")

        while time.monotonic() < deadline:
            remaining = max(1, int(deadline - time.monotonic()))
            try:
                code = self.email_service.get_verification_code(
                    email=email,
                    timeout=remaining,
                    otp_sent_at=otp_sent_at,
                    exclude_codes=excluded_codes | used_codes,
                    phase=phase_key,
                    phase_label=phase_title,
                )
            except TimeoutError as exc:
                self._log(f"[验证码] {phase_title} 等待超时: {exc}", "debug")
                return None
            if not code:
                return code

            normalized_code = str(code).strip()
            meta = self._read_last_verification_result()
            message_id = str(meta.get("message_id") or meta.get("id") or "").strip()

            if message_id:
                if message_id in used_message_ids:
                    self._mark_message_processed(message_id)
                    self._log(f"跳过已处理验证码邮件（{phase_title}）", "debug")
                    continue
                used_message_ids.add(message_id)
                self._mark_message_processed(message_id)
                used_codes.add(normalized_code)
                meta["code"] = normalized_code
                meta["phase"] = phase_key
                self._last_verification_result_by_phase[phase_key] = meta
                self._log(f"[验证码] 验证码已获取：{phase_title}")
                return normalized_code

            if normalized_code in used_codes or normalized_code in excluded_codes:
                self._log(f"跳过缺少邮件标识的重复验证码（{phase_title}）", "debug")
                continue

            used_codes.add(normalized_code)
            self._last_verification_result_by_phase[phase_key] = {
                "message_id": "",
                "code": normalized_code,
                "phase": phase_key,
            }
            self._log(f"[验证码] 验证码已获取：{phase_title}")
            return normalized_code

        return None


class RefreshTokenRegistrationEngine:
    """Refresh token 注册引擎。"""

    def __init__(
        self,
        email_service,
        proxy_url: Optional[str] = None,
        callback_logger: Optional[Callable[..., None]] = None,
        task_uuid: Optional[str] = None,
        browser_mode: str = "protocol",
        max_retries: int = 3,
        extra_config: Optional[dict] = None,
    ):
        self.email_service = email_service
        self.proxy_url = proxy_url
        self.callback_logger = callback_logger or (lambda msg, *_: logger.info(msg))
        self.task_uuid = task_uuid
        self.browser_mode = str(browser_mode or "protocol").strip().lower() or "protocol"
        # 已移除整流程重试能力，保留参数仅兼容调用方
        self.max_retries = 1
        self.extra_config = dict(extra_config or {})

        self.email: Optional[str] = None
        self.password: Optional[str] = None
        self.email_info: Optional[Dict[str, Any]] = None
        self.logs: list[str] = []
        self._last_pending_invite_error_message: str = ""
        self._last_workspace_capture_error: str = ""
        self._last_phone_challenge_events: list[dict[str, Any]] = []
        self._prepared_register_client: ChatGPTClient | None = None

    @staticmethod
    def _classify_log_level(message: str, level: str = "info") -> str:
        return classify_task_log_level(message, level, flow="refresh_token_register")

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

    def _log_stage(self, title: str, *, level: str = "info"):
        self._log(f"[阶段] ================ {title} ================", level)

    def _create_email(self) -> bool:
        try:
            self._log(f"[邮箱] 正在创建 {self.email_service.service_type.value} 邮箱...")
            self.email_info = self.email_service.create_email()

            email_value = str(
                self.email
                or (self.email_info or {}).get("email")
                or ""
            ).strip()
            if not email_value:
                self._log(
                    f"[邮箱] 创建邮箱失败: {self.email_service.service_type.value} 返回空邮箱地址",
                    "error",
                )
                return False

            if self.email_info is None:
                self.email_info = {}
            self.email_info["email"] = email_value
            self.email = email_value
            mailbox_action = str((self.email_info or {}).get("mailbox_action") or "").strip()
            if mailbox_action in {"reused_existing", "restored_existing"}:
                self._log(f"[邮箱] 复用邮箱: {self.email}")
            elif mailbox_action == "created_exact_address":
                self._log(f"[邮箱] 已按原地址新建远端邮箱: {self.email}")
            elif mailbox_action == "recovered_after_create_error":
                self._log(f"[邮箱] 建箱异常后复用已有邮箱: {self.email}")
            else:
                self._log(f"[邮箱] 成功创建邮箱: {self.email}")
            return True
        except Exception as e:
            self._log(f"[邮箱] 创建邮箱失败: {e}", "error")
            return False

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
    def _should_switch_to_login_after_register_failure(message: str) -> bool:
        text = str(message or "").lower()
        markers = (
            "user_already_exists",
            "account already exists",
            "please login instead",
            "add_phone",
            "add-phone",
        )
        return any(marker in text for marker in markers)

    def _read_int_config(
        self,
        primary_key: str,
        *,
        fallback_keys: tuple[str, ...] = (),
        default: int,
        minimum: int,
        maximum: int,
    ) -> int:
        keys = (primary_key, *tuple(fallback_keys or ()))
        for key in keys:
            if key not in self.extra_config:
                continue
            value = self.extra_config.get(key)
            try:
                parsed = int(value)
            except Exception:
                continue
            return max(minimum, min(parsed, maximum))
        return max(minimum, min(int(default), maximum))

    @staticmethod
    def _workspace_capture_retry_delay_seconds(error_text: str, attempt: int) -> int:
        text = str(error_text or "").strip().lower()
        normalized_attempt = max(1, int(attempt or 1))
        if any(marker in text for marker in ("429", "rate limit", "rate_limited", "请求过快", "限流")):
            return min(300, 90 * normalized_attempt)
        if any(marker in text for marker in ("add_phone", "add-phone", "手机号", "phone verification")):
            return min(240, 60 * normalized_attempt)
        return min(45, 10 * normalized_attempt)

    @staticmethod
    def _is_workspace_capture_cooldown_error(error_text: str) -> bool:
        text = str(error_text or "").strip().lower()
        return any(marker in text for marker in (
            "429",
            "rate limit",
            "rate_limited",
            "请求过快",
            "限流",
            "add_phone",
            "add-phone",
            "手机号",
            "phone verification",
        ))

    @staticmethod
    def _compact_error_text(error_text: str, limit: int = 180) -> str:
        text = " ".join(str(error_text or "").split())
        return text[: max(20, int(limit or 180))]

    def _registration_full_auth_phone_policy(self) -> tuple[bool, bool]:
        """Phone policy for post-registration full-auth capture.

        有 RT 注册第二阶段只补完整 Auth：
        - 默认不主动 add_phone 新绑；
        - 默认允许已绑定手机号二次验证，用手机号池/人工面板处理；
        - 可用专用 key 覆盖；没有专用 key 时复用补抓 Auth 全局配置。
        """
        if "chatgpt_registration_full_auth_allow_add_phone_verification" in self.extra_config:
            allow_add = self._read_bool_config(
                "chatgpt_registration_full_auth_allow_add_phone_verification",
                default=False,
            )
        else:
            allow_add = self._read_bool_config(
                "chatgpt_resume_auth_allow_add_phone_verification",
                default=False,
            )

        if "chatgpt_registration_full_auth_allow_existing_phone_verification" in self.extra_config:
            allow_existing = self._read_bool_config(
                "chatgpt_registration_full_auth_allow_existing_phone_verification",
                default=True,
            )
        else:
            allow_existing = self._read_bool_config(
                "chatgpt_resume_auth_allow_existing_phone_verification",
                default=True,
            )
        return bool(allow_add), bool(allow_existing)

    def _remember_oauth_phone_challenge_events(self, oauth_client: Optional[OAuthClient]) -> None:
        events = getattr(oauth_client, "_phone_challenge_events", None) if oauth_client is not None else None
        if not isinstance(events, (list, tuple)) or not events:
            return
        for event in events:
            if not isinstance(event, dict):
                continue
            safe_event = self._to_json_safe(dict(event))
            if isinstance(safe_event, dict):
                self._last_phone_challenge_events.append(safe_event)
        self._last_phone_challenge_events = self._last_phone_challenge_events[-10:]

    def _apply_phone_challenge_metadata(self, result: RegistrationResult) -> None:
        events = [dict(item) for item in (self._last_phone_challenge_events or []) if isinstance(item, dict)]
        if not events:
            return
        result.metadata = result.metadata or {}
        last_event = dict(events[-1])
        result.metadata["chatgpt_phone_challenge"] = last_event
        result.metadata["chatgpt_phone_challenge_history"] = events[-5:]
        if str(last_event.get("type") or last_event.get("challenge_type") or "") == "existing_phone_otp":
            phone = str(last_event.get("phone") or last_event.get("phone_number") or "").strip()
            masked = str(last_event.get("masked") or last_event.get("masked_phone") or "").strip()
            if phone or masked:
                result.metadata["chatgpt_bound_phone"] = {
                    "phone": phone,
                    "phone_number": phone,
                    "masked": masked,
                    "masked_phone": masked,
                    "source": str(last_event.get("source") or "registration_full_auth").strip(),
                    "detected_at": str(last_event.get("seen_at") or "").strip(),
                    "updated_at": str(last_event.get("updated_at") or last_event.get("seen_at") or "").strip(),
                    "last_seen_reason": "existing_phone_otp",
                    "verification_status": str(last_event.get("status") or "required").strip() or "required",
                }
                if phone:
                    result.metadata["chatgpt_bound_phone_number"] = phone
                elif masked:
                    result.metadata["chatgpt_bound_phone_masked"] = masked

    @staticmethod
    def _should_attempt_business_workspace_recovery(oauth_client: OAuthClient) -> bool:
        last_error = str(getattr(oauth_client, "last_error", "") or "").strip().lower()
        last_state = getattr(oauth_client, "last_state", None)
        page_type = str(getattr(last_state, "page_type", "") or "").strip().lower()
        current_url = str(getattr(last_state, "current_url", "") or "").strip().lower()
        continue_url = str(getattr(last_state, "continue_url", "") or "").strip().lower()
        haystack = " | ".join(part for part in (last_error, page_type, current_url, continue_url) if part)
        if not haystack:
            return False
        recovery_markers = (
            "未获取到 workspace / callback",
            "workspace / callback",
            "session 中没有 workspace 信息",
            "workspace session 数据为空",
            "consent session 数据为空",
            "add_phone",
            "add-phone",
        )
        return any(marker in haystack for marker in recovery_markers)

    def _recover_workspace_with_business(
        self,
        *,
        email: str,
        password: str,
        device_id: str,
        user_agent: Optional[str],
        sec_ch_ua: Optional[str],
        impersonate: Optional[str],
        browser_fingerprint: Optional[Dict[str, Any]],
        email_adapter,
        first_name: str,
        last_name: str,
        birthdate: str,
    ) -> Optional[Dict[str, Any]]:
        from .business_workspace_recovery import BusinessWorkspaceRecovery

        if not self._is_team_invite_enabled():
            self._log("未启用 team invite，跳过 business workspace recovery", "info")
            return None

        recovery = BusinessWorkspaceRecovery(
            self.extra_config,
            proxy=self.proxy_url,
            browser_mode=self.browser_mode,
            log_fn=lambda msg, level="info": self._log(msg, level),
        )
        if not recovery.is_enabled():
            self._log("本地 Team 运行时不可用，跳过 business workspace recovery", "warning")
            return None

        return recovery.recover_workspace_for_account(
            email=email,
            password=password,
            device_id=device_id,
            user_agent=user_agent,
            sec_ch_ua=sec_ch_ua,
            impersonate=impersonate,
            browser_fingerprint=browser_fingerprint,
            email_adapter=email_adapter,
            first_name=first_name,
            last_name=last_name,
            birthdate=birthdate,
        )

    def _build_chatgpt_client(self) -> ChatGPTClient:
        client = ChatGPTClient(
            proxy=self.proxy_url,
            verbose=False,
            browser_mode=self.browser_mode,
            fingerprint=(
                self.extra_config.get("chatgpt_browser_fingerprint")
                or self.extra_config.get("browser_fingerprint")
            ),
        )
        client._log = lambda msg: self._log(f"[注册链路] {msg}")
        return client

    def _probe_homepage_before_email_creation(self) -> tuple[bool, str]:
        self._prepared_register_client = None
        client = self._build_chatgpt_client()
        keep_client = False
        try:
            max_probe_attempts = 3
            last_error = "访问首页失败"
            for probe_attempt in range(max_probe_attempts):
                if probe_attempt > 0:
                    self._log(f"[注册] 预热首页重试 {probe_attempt + 1}/{max_probe_attempts}...")
                    client._reset_session()
                self._log("[注册] 邮箱创建前预热访问 ChatGPT 首页...")
                if not client.visit_homepage():
                    probe = dict(getattr(client, "last_homepage_probe", {}) or {})
                    last_error = str(probe.get("detail") or probe.get("reason") or "访问首页失败").strip()
                    continue
                csrf_token = client.get_csrf_token()
                if not csrf_token:
                    last_error = "获取 CSRF token 失败"
                    continue
                # 这里不能只把预热当成一次独立探测。Cloudflare/ChatGPT 对同一代理下的
                # 不同 TLS/browser 指纹会给出不同结果；预热成功后如果注册状态机重新随机
                # 创建一个 client，仍然可能马上 403。保留已通过首页+CSRF 的同一 session
                # 和同一任务指纹，注册状态机继续复用它。
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

    def _build_oauth_client(self) -> OAuthClient:
        client_config = dict(self.extra_config or {})
        if client_config.get("_task_control") is not None:
            client_config.setdefault("_manual_phone_otp_enabled", True)
            client_config.setdefault("_manual_phone_otp_timeout_seconds", 60)
        client = OAuthClient(
            client_config,
            proxy=self.proxy_url,
            verbose=False,
            browser_mode=self.browser_mode,
        )
        client._log = lambda msg: self._log(f"[登录链路] {msg}")
        return client

    def _reuse_register_browser_context(
        self,
        register_client: ChatGPTClient,
        oauth_client: OAuthClient,
    ) -> None:
        oauth_client.adopt_browser_context(
            getattr(register_client, "session", None),
            device_id=getattr(register_client, "device_id", "") or "",
            user_agent=getattr(register_client, "ua", None),
            sec_ch_ua=getattr(register_client, "sec_ch_ua", None),
            accept_language=getattr(register_client, "accept_language", None),
            browser_fingerprint=getattr(register_client, "fingerprint", None),
        )

    def _extract_account_info(self, tokens: dict[str, Any]) -> dict[str, Any]:
        id_token = str((tokens or {}).get("id_token") or "").strip()
        if not id_token:
            return {}
        manager = OAuthManager(proxy_url=self.proxy_url)
        return manager.extract_account_info(id_token)

    @staticmethod
    def _extract_workspace_id(oauth_client: OAuthClient) -> str:
        workspace_id = str(getattr(oauth_client, "last_workspace_id", "") or "").strip()
        if workspace_id:
            return workspace_id

        try:
            session_data = oauth_client._decode_oauth_session_cookie() or {}
        except Exception:
            session_data = {}

        workspaces = session_data.get("workspaces") or []
        if not workspaces:
            return ""
        return str((workspaces[0] or {}).get("id") or "").strip()

    @staticmethod
    def _extract_session_token(oauth_client: OAuthClient) -> str:
        getter = getattr(oauth_client, "_get_cookie_value", None)
        if not callable(getter):
            return ""
        return str(
            getter("__Secure-next-auth.session-token", "chatgpt.com")
            or getter("__Secure-authjs.session-token", "chatgpt.com")
            or ""
        ).strip()

    def _read_bool_config(self, key: str, *, default: bool) -> bool:
        if key not in self.extra_config:
            return bool(default)
        value = self.extra_config.get(key)
        if isinstance(value, bool):
            return value
        text = str(value or "").strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
        return bool(default)

    def _is_team_invite_enabled(self) -> bool:
        return self._read_bool_config("chatgpt_enable_team_invite", default=False)

    def _is_team_invite_deferred_activation_enabled(self) -> bool:
        return self._read_bool_config("chatgpt_team_invite_deferred_activation", default=False)

    def _is_existing_account_capture_enabled(self) -> bool:
        return self._read_bool_config("chatgpt_existing_account_capture", default=False)

    def _is_existing_account_login_route_enabled(self) -> bool:
        return existing_account_login_route_enabled(self.extra_config)

    def _should_capture_gopay_provider_link(self) -> bool:
        for key in (
            "chatgpt_access_token_only_gopay_provider_link_enabled",
            "chatgpt_gopay_provider_link_enabled",
        ):
            if key in self.extra_config and self.extra_config.get(key) not in (None, ""):
                return self._read_bool_config(key, default=False)
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

    def _checkout_country_currency(self) -> tuple[str, str]:
        from services.chatgpt_core.payment import normalize_checkout_country, normalize_checkout_currency

        country = normalize_checkout_country(
            self.extra_config.get("chatgpt_checkout_country")
            or self.extra_config.get("chatgpt_access_token_only_checkout_country")
            or self.extra_config.get("checkout_country")
            or self.extra_config.get("country")
            or "US"
        )
        currency = normalize_checkout_currency(
            self.extra_config.get("chatgpt_checkout_currency")
            or self.extra_config.get("chatgpt_access_token_only_checkout_currency")
            or self.extra_config.get("checkout_currency")
            or self.extra_config.get("currency")
            or "USD",
            country,
        )
        return country, currency

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

    def _append_gopay_provider_link_metadata(
        self,
        result: RegistrationResult,
        session_result: dict[str, Any] | None = None,
    ) -> None:
        if not self._should_capture_gopay_provider_link():
            return
        result.metadata = result.metadata or {}
        metadata = {
            "chatgpt_gopay_provider_link_enabled": True,
            "chatgpt_gopay_provider_link_ready": False,
            "chatgpt_gopay_provider_link": "",
            "chatgpt_gopay_provider_link_error": "",
        }
        try:
            from core.proxy_utils import iter_enabled_runtime_proxies
            from services.chatgpt_core.gopay_flow import create_gopay_provider_link
            from services.chatgpt_core.payment import generate_plus_link

            class _CheckoutAccount:
                pass

            session_result = dict(session_result or {})
            account = _CheckoutAccount()
            account.access_token = str(session_result.get("access_token") or result.access_token or "")
            account.cookies = str(session_result.get("cookies") or session_result.get("cookie") or "")
            account.session_token = str(session_result.get("session_token") or result.session_token or "")
            account.email = str(result.email or "")
            account.extra = {
                "account_id": str(session_result.get("account_id") or result.account_id or ""),
                "workspace_id": str(session_result.get("workspace_id") or result.workspace_id or ""),
                "session_token": account.session_token,
            }
            if self.extra_config.get("stripe_publishable_key"):
                account.extra["stripe_publishable_key"] = self.extra_config.get("stripe_publishable_key")
            if self.extra_config.get("gopay_browser_profile"):
                account.extra["gopay_browser_profile"] = self.extra_config.get("gopay_browser_profile")
            if self.extra_config.get("gopay_processor_entity"):
                account.extra["gopay_processor_entity"] = self.extra_config.get("gopay_processor_entity")
            if not account.access_token:
                raise RuntimeError("缺少 access_token，无法生成 GoPay 平台链接")

            country, currency = self._checkout_country_currency()
            billing = self._checkout_billing_config(country=country, currency=currency, email_addr=account.email)
            billing = {**billing, "country": country, "currency": currency}
            proxy_candidates = [
                str(item or "").strip()
                for item in iter_enabled_runtime_proxies(self.proxy_url)
                if str(item or "").strip()
            ]
            if not proxy_candidates:
                raise RuntimeError("当前没有可用代理，无法生成 GoPay 平台链接")
            checkout_proxy = proxy_candidates[0]
            self._log(f"[GoPay] 开始生成注册后平台链接 country={country} currency={currency}")
            checkout_url = generate_plus_link(
                account,
                proxy=checkout_proxy,
                country=country,
                currency=currency,
                billing=billing,
            )
            snapshot = create_gopay_provider_link(
                account,
                account_id=0,
                plan="plus",
                country=country,
                currency=currency,
                proxy=checkout_proxy,
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
            metadata.update(
                {
                    "chatgpt_checkout_plan": "plus",
                    "chatgpt_checkout_url": checkout_url,
                    "chatgpt_checkout_country": country,
                    "chatgpt_checkout_currency": currency,
                    "chatgpt_gopay_provider_link_ready": bool(provider_link),
                    "chatgpt_gopay_provider_link": provider_link,
                    "chatgpt_gopay_provider_link_error": "" if provider_link else str(snapshot.get("last_error") or "未返回有效 URL"),
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
            if provider_link:
                self._log(f"[GoPay] 注册后平台链接已获取: {provider_link}")
            else:
                self._log("[GoPay] 注册后平台链接未返回有效 URL", "warning")
        except Exception as exc:
            error = str(exc).strip() or exc.__class__.__name__
            metadata["chatgpt_gopay_provider_link_error"] = error
            self._log(f"[GoPay] 注册后平台链接获取失败，账号仍按注册结果保存: {error}", "warning")
        result.metadata.update(metadata)

    @staticmethod
    def _normalize_workspace_scope(value: str) -> str:
        normalized = str(value or "").strip().lower().replace("-", "_")
        if normalized in {"free", "personal", "personal_free"}:
            return "free"
        if normalized in {"business", "team", "workspace", "enterprise"}:
            return "business"
        return ""

    def _resolve_workspace_capture_scopes(self, current_scope: str = "") -> list[str]:
        normalized_current = self._normalize_workspace_scope(current_scope)
        team_invite_enabled = self._is_team_invite_enabled()
        existing_account_capture = self._is_existing_account_capture_enabled()
        has_business_flag = "chatgpt_capture_business_workspace" in self.extra_config
        has_free_flag = "chatgpt_capture_free_workspace" in self.extra_config

        if existing_account_capture:
            scopes: list[str] = []
            if self._read_bool_config("chatgpt_capture_business_workspace", default=not has_free_flag or normalized_current == "business"):
                scopes.append("business")
            if self._read_bool_config("chatgpt_capture_free_workspace", default=True):
                scopes.append("free")
            if not scopes:
                if normalized_current:
                    return [normalized_current]
                return ["business"]
            return scopes

        if not team_invite_enabled:
            if normalized_current:
                if (
                    normalized_current == "free"
                    and has_free_flag
                    and not self._read_bool_config("chatgpt_capture_free_workspace", default=True)
                ):
                    return []
                return [normalized_current]
            if has_free_flag:
                if self._read_bool_config("chatgpt_capture_free_workspace", default=True):
                    return ["free"]
                return []
            return ["free"]

        if not has_business_flag and not has_free_flag:
            if normalized_current:
                return [normalized_current]
            return ["business"]

        scopes: list[str] = []
        if self._read_bool_config("chatgpt_capture_free_workspace", default=normalized_current == "free"):
            scopes.append("free")
        if self._read_bool_config("chatgpt_capture_business_workspace", default=normalized_current == "business"):
            scopes.append("business")
        if not scopes:
            if normalized_current:
                return [normalized_current]
            return ["business"]
        return scopes

    @staticmethod
    def _infer_scope_from_access_token(access_token: str, source: str = "") -> str:
        payload = decode_jwt_payload(access_token)
        auth_claims = payload.get("https://api.openai.com/auth") or {}
        plan_type = str(auth_claims.get("chatgpt_plan_type") or "").strip().lower()
        if plan_type in {"team", "business", "enterprise"}:
            return "business"
        if source == "business_recovery":
            return "business"
        return "free"

    @staticmethod
    def _scope_label(scope: str) -> str:
        return "business" if scope == "business" else "free"

    def _build_workspace_artifact(
        self,
        *,
        tokens: dict[str, Any],
        oauth_client: OAuthClient,
        source: str,
        scope_hint: str = "",
    ) -> dict[str, Any]:
        account_info = self._extract_account_info(tokens)
        access_token = str(tokens.get("access_token") or "").strip()
        refresh_token = str(tokens.get("refresh_token") or "").strip()
        id_token = str(tokens.get("id_token") or "").strip()
        workspace_id = self._extract_workspace_id(oauth_client)
        session_token = self._extract_session_token(oauth_client)
        inferred_scope = self._infer_scope_from_access_token(access_token, source=source)
        normalized_hint = self._normalize_workspace_scope(scope_hint)
        scope = normalized_hint or inferred_scope or "business"
        auth_claims = (decode_jwt_payload(access_token).get("https://api.openai.com/auth") or {}) if access_token else {}
        account_id = str(
            tokens.get("account_id")
            or account_info.get("account_id")
            or auth_claims.get("chatgpt_account_id")
            or workspace_id
            or ""
        ).strip()
        return {
            "scope": scope,
            "label": self._scope_label(scope),
            "account_id": account_id,
            "workspace_id": workspace_id,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "id_token": id_token,
            "session_token": session_token,
            "source": source,
            "variant_key": f"{scope}:{workspace_id or account_id or 'unknown'}",
        }

    @staticmethod
    def _artifact_has_refresh_token(artifact: Optional[dict[str, Any]]) -> bool:
        return bool(str((artifact or {}).get("refresh_token") or "").strip())

    def _apply_workspace_artifact_to_result(self, result: RegistrationResult, artifact: dict[str, Any]) -> None:
        result.success = True
        result.email = self.email or result.email or ""
        result.password = self.password or result.password or ""
        result.access_token = str(artifact.get("access_token") or "").strip()
        result.refresh_token = str(artifact.get("refresh_token") or "").strip()
        result.id_token = str(artifact.get("id_token") or "").strip()
        result.session_token = str(artifact.get("session_token") or "").strip()
        result.account_id = str(artifact.get("account_id") or "").strip()
        result.workspace_id = str(artifact.get("workspace_id") or "").strip()
        result.source = str(artifact.get("source") or result.source or "register")

    def _build_workspace_artifact_from_session_tokens(
        self,
        *,
        session_tokens: dict[str, Any],
        scope: str,
        source: str,
    ) -> dict[str, Any]:
        normalized_scope = self._normalize_workspace_scope(scope) or "free"
        access_token = str(session_tokens.get("access_token") or "").strip()
        account_id = str(session_tokens.get("account_id") or "").strip()
        workspace_id = str(session_tokens.get("workspace_id") or account_id or "").strip()
        return {
            "scope": normalized_scope,
            "label": self._scope_label(normalized_scope),
            "account_id": account_id,
            "workspace_id": workspace_id,
            "access_token": access_token,
            "refresh_token": str(session_tokens.get("refresh_token") or "").strip(),
            "id_token": str(session_tokens.get("id_token") or "").strip(),
            "session_token": str(session_tokens.get("session_token") or "").strip(),
            "source": source,
            "variant_key": f"{normalized_scope}:{workspace_id or account_id or 'unknown'}",
        }

    def _is_registration_access_token_save_enabled(self) -> bool:
        value = self.extra_config.get("chatgpt_save_registration_access_token_account")
        # 注册阶段已经拿到 ChatGPT session/accessToken 时，默认先保存成 AT-only 账号，
        # 后续 refresh_token / workspace 捕获失败再走补抓；否则真实注册成功会被
        # “未生成任何工作空间产物”拖成失败，账号和邮箱资源都白消耗。
        if value is None or value == "":
            return True
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def _build_registration_access_token_artifact(
        self,
        *,
        session_tokens: dict[str, Any],
    ) -> dict[str, Any]:
        artifact = self._build_workspace_artifact_from_session_tokens(
            session_tokens=session_tokens,
            scope="free",
            source="registration_session",
        )
        # 注册阶段的 AT checkpoint 本质就是同一账号的 free 工作空间半成品。
        # 这里必须使用最终 full-auth 升级时相同的 variant key，否则第一阶段
        # AT-only 和第二阶段 RT 会被保存成两条账号。
        stable_id = artifact.get("account_id") or artifact.get("workspace_id") or "unknown"
        artifact["label"] = "free"
        artifact["variant_key"] = f"free:{stable_id}"
        artifact["auth_level"] = "access_token_only"
        artifact["partial_auth"] = True
        return artifact

    def _return_registration_access_token_partial_result(
        self,
        *,
        result: RegistrationResult,
        artifact: Optional[dict[str, Any]],
        reason: str,
        session_tokens: Optional[dict[str, Any]] = None,
    ) -> bool:
        session_tokens = session_tokens if isinstance(session_tokens, dict) else {}
        if not artifact and session_tokens:
            artifact = self._build_registration_access_token_artifact(
                session_tokens=session_tokens,
            )
        if not artifact or not str(artifact.get("access_token") or "").strip():
            return False
        self._apply_workspace_artifact_to_result(result, artifact)
        result.success = True
        result.email = self.email or result.email or ""
        result.password = self.password or result.password or ""
        result.source = "registration_session"
        result.workspace_artifacts = [artifact]
        result.error_message = ""
        result.metadata = result.metadata or {}
        result.metadata["registration_stage_complete"] = True
        result.metadata["registration_access_token_saved"] = True
        if session_tokens:
            result.metadata.setdefault("registration_session_account_id", str(session_tokens.get("account_id") or ""))
            result.metadata.setdefault("registration_session_workspace_id", str(session_tokens.get("workspace_id") or ""))
            for key in ("auth_provider", "expires", "user_id", "user", "account", "cookies"):
                if key in session_tokens and key not in result.metadata:
                    result.metadata[key] = session_tokens.get(key)
        self._log(
            f"[注册] 第二阶段未获取 RT，保留第一阶段 AT-only，不改账号: {self._compact_error_text(reason)}",
            "warning",
        )
        return True

    @classmethod
    def _to_json_safe(cls, value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if type(value).__module__ == "unittest.mock":
            return str(value)
        if isinstance(value, dict):
            return {
                str(key): cls._to_json_safe(item)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple, set)):
            return [cls._to_json_safe(item) for item in value]

        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            try:
                return cls._to_json_safe(model_dump())
            except Exception:
                pass

        to_dict = getattr(value, "to_dict", None)
        if callable(to_dict):
            try:
                return cls._to_json_safe(to_dict())
            except Exception:
                pass

        try:
            attrs = vars(value)
        except Exception:
            attrs = None
        if isinstance(attrs, dict):
            return {
                str(key): cls._to_json_safe(item)
                for key, item in attrs.items()
                if not str(key).startswith("_")
            }

        return str(value)

    def _build_registration_context_payload(
        self,
        *,
        register_client: ChatGPTClient,
        first_name: str,
        last_name: str,
        birthdate: str,
    ) -> dict[str, Any]:
        return self._to_json_safe({
            "device_id": getattr(register_client, "device_id", "") or "",
            "user_agent": getattr(register_client, "ua", None),
            "sec_ch_ua": getattr(register_client, "sec_ch_ua", None),
            "impersonate": getattr(register_client, "impersonate", None),
            "browser_fingerprint": getattr(register_client, "fingerprint", None),
            "accept_language": getattr(register_client, "accept_language", None),
            "first_name": first_name,
            "last_name": last_name,
            "birthdate": birthdate,
        })

    def _attach_browser_fingerprint_metadata(
        self,
        metadata: dict[str, Any],
        register_client: ChatGPTClient,
        *,
        source: str = "registration",
    ) -> dict[str, Any]:
        fingerprint = build_browser_fingerprint_payload(getattr(register_client, "fingerprint", None))
        if not fingerprint:
            return metadata
        metadata["chatgpt_browser_fingerprint"] = fingerprint
        metadata["chatgpt_browser_fingerprint_signature"] = fingerprint_signature(fingerprint)
        metadata.setdefault("chatgpt_browser_fingerprint_source", source)
        metadata.setdefault("chatgpt_browser_fingerprint_isolated", True)
        return metadata

    def _export_mailbox_state(self, email_adapter) -> dict[str, Any]:
        email_service = getattr(email_adapter, "email_service", None)
        exporter = getattr(email_service, "export_state", None)
        if callable(exporter):
            try:
                state = exporter() or {}
                if isinstance(state, dict):
                    return self._to_json_safe(state)
            except Exception as exc:
                self._log(f"导出 mailbox_state 失败: {exc}", "warning")
        return {}

    def _prepare_pending_business_invite(
        self,
        *,
        email: str,
        email_adapter,
    ) -> Optional[Dict[str, Any]]:
        from .business_workspace_recovery import BusinessWorkspaceRecovery

        self._last_pending_invite_error_message = ""
        if not self._is_team_invite_enabled():
            self._last_pending_invite_error_message = "未启用 team invite，无法保存 pending invite"
            self._log(self._last_pending_invite_error_message, "info")
            return None

        recovery = BusinessWorkspaceRecovery(
            self.extra_config,
            proxy=self.proxy_url,
            browser_mode=self.browser_mode,
            log_fn=lambda msg, level="info": self._log(msg, level),
        )
        if not recovery.is_enabled():
            self._last_pending_invite_error_message = "本地 Team 运行时不可用，无法保存 pending invite"
            self._log(self._last_pending_invite_error_message, "warning")
            return None
        pending_invite = recovery.prepare_pending_invite_for_account(
            email=email,
            email_adapter=email_adapter,
        )
        if not pending_invite:
            self._last_pending_invite_error_message = getattr(
                recovery,
                "last_invite_failure_summary",
                "保存 pending invite 失败",
            ) or "保存 pending invite 失败"
        return pending_invite

    def _enter_business_before_workspace_capture(
        self,
        *,
        email: str,
        password: str,
        email_adapter,
        user_agent: Optional[str],
        sec_ch_ua: Optional[str],
        impersonate: Optional[str],
        browser_fingerprint: Optional[Dict[str, Any]],
        first_name: str,
        last_name: str,
        birthdate: str,
        register_client: ChatGPTClient,
    ) -> Optional[Dict[str, Any]]:
        from .business_workspace_recovery import BusinessWorkspaceRecovery

        if not self._is_team_invite_enabled():
            self._log("未启用 team invite，跳过进入 business 工作空间", "info")
            return None

        recovery = BusinessWorkspaceRecovery(
            self.extra_config,
            proxy=self.proxy_url,
            browser_mode=self.browser_mode,
            log_fn=lambda msg, level="info": self._log(msg, level),
        )
        if not recovery.is_enabled():
            self._log("本地 Team 运行时不可用，无法进入 business 工作空间", "warning")
            return None
        return recovery.join_business_for_account(
            email=email,
            password=password,
            device_id=getattr(register_client, "device_id", "") or "",
            email_adapter=email_adapter,
            user_agent=user_agent,
            accept_language=getattr(register_client, "accept_language", None),
            sec_ch_ua=sec_ch_ua,
            impersonate=impersonate,
            browser_fingerprint=browser_fingerprint,
            first_name=first_name,
            last_name=last_name,
            birthdate=birthdate,
            browser_session=getattr(register_client, "session", None),
        )

    def _capture_workspace_artifacts_after_business_join(
        self,
        *,
        result: RegistrationResult,
        register_client: ChatGPTClient,
        email_adapter,
        first_name: str,
        last_name: str,
        birthdate: str,
        business_join_result: Optional[Dict[str, Any]],
    ) -> bool:
        requested_scopes = self._resolve_workspace_capture_scopes(current_scope="business")
        selected_scopes: list[str] = ["business"]
        for scope in requested_scopes:
            if scope not in selected_scopes:
                selected_scopes.append(scope)

        available_artifacts: dict[str, dict[str, Any]] = {}
        optional_failures: list[str] = []

        if not business_join_result or not business_join_result.get("joined"):
            result.success = False
            result.error_message = "进入 business 工作空间失败"
            self._log(result.error_message, "warning")
            return False

        result.metadata = result.metadata or {}
        result.metadata["business_recovery_team_id"] = business_join_result.get("team_id")
        result.metadata["business_recovery_joined"] = bool(business_join_result.get("joined"))
        result.metadata["business_workspace_id"] = str(business_join_result.get("workspace_id") or "")

        business_tokens = business_join_result.get("tokens") if isinstance(business_join_result, dict) else None
        business_oauth_client = business_join_result.get("oauth_client") if isinstance(business_join_result, dict) else None
        business_source = str((business_join_result or {}).get("source") or "business_recovery")
        last_business_capture_error = ""
        self._remember_oauth_phone_challenge_events(business_oauth_client)

        if isinstance(business_tokens, dict) and business_oauth_client is not None:
            business_artifact = self._build_workspace_artifact(
                tokens=business_tokens,
                oauth_client=business_oauth_client,
                source=business_source,
                scope_hint="business",
            )
            business_artifact["scope"] = "business"
            business_artifact["label"] = self._scope_label("business")
            business_artifact["variant_key"] = (
                f"business:{business_artifact.get('workspace_id') or business_artifact.get('account_id') or 'unknown'}"
            )
            available_artifacts["business"] = business_artifact
            self._log("[business] OAuth 登录成功")
            self._log(
                f"[business] 已保存 account_id={business_artifact.get('account_id') or '-'} workspace_id={business_artifact.get('workspace_id') or '-'}"
            )
        else:
            self._log("[business] 开始保存 business 工作空间")
            business_capture_attempts = 3
            for attempt in range(1, business_capture_attempts + 1):
                if attempt > 1:
                    wait_seconds = self._workspace_capture_retry_delay_seconds(last_business_capture_error, attempt)
                    reason = "；检测到 add_phone/429，已延长冷却" if self._is_workspace_capture_cooldown_error(last_business_capture_error) else ""
                    self._log(
                        f"[business] 工作空间补抓重试 {attempt}/{business_capture_attempts}，等待 {wait_seconds}s{reason}",
                        "debug",
                    )
                    time.sleep(wait_seconds)
                self._last_workspace_capture_error = ""
                business_artifact = self._capture_workspace_artifact_via_fresh_login(
                    scope="business",
                    email=result.email,
                    password=result.password,
                    device_id=getattr(register_client, "device_id", "") or "",
                    user_agent=getattr(register_client, "ua", None),
                    sec_ch_ua=getattr(register_client, "sec_ch_ua", None),
                    impersonate=getattr(register_client, "impersonate", None),
                    browser_fingerprint=getattr(register_client, "fingerprint", None),
                    email_adapter=email_adapter,
                    first_name=first_name,
                    last_name=last_name,
                    birthdate=birthdate,
                )
                if business_artifact:
                    available_artifacts["business"] = business_artifact
                    if not self._artifact_has_refresh_token(business_artifact):
                        self._log("[business] 已命中 business 工作空间，但当前 artifact 缺少 refresh_token", "debug")
                    break
                last_business_capture_error = str(self._last_workspace_capture_error or "").strip()
                self._log(
                    f"[business] 第 {attempt}/{business_capture_attempts} 次补抓未命中 business 工作空间",
                    "debug",
                )

        if "business" not in available_artifacts:
            result.success = False
            if last_business_capture_error:
                result.error_message = f"未能获取所选工作空间: business（最后错误: {self._compact_error_text(last_business_capture_error)}）"
            else:
                result.error_message = "未能获取所选工作空间: business"
            self._log("[business] 激活后未拿到 business artifact", "debug")
            self._log(result.error_message, "warning")
            return False

        if not self._artifact_has_refresh_token(available_artifacts.get("business")):
            result.success = False
            business_artifact = available_artifacts.get("business") or {}
            result.error_message = "business 工作空间未获取到 refresh_token"
            self._log(
                f"[business] 已拿到 artifact 但缺少 refresh_token: account_id={business_artifact.get('account_id') or '-'} workspace_id={business_artifact.get('workspace_id') or '-'} source={business_artifact.get('source') or '-'}",
                "debug",
            )
            self._log(result.error_message, "warning")
            return False

        if not isinstance(business_tokens, dict) or business_oauth_client is None:
            self._log(
                f"[business] 已保存 account_id={available_artifacts['business'].get('account_id') or '-'} workspace_id={available_artifacts['business'].get('workspace_id') or '-'}"
            )

        self._apply_workspace_artifact_to_result(result, available_artifacts["business"])
        result.error_message = ""

        for scope in selected_scopes:
            if scope == "business" or scope in available_artifacts:
                continue
            self._log_stage(f"获取{self._scope_label(scope)}空间")
            self._log(f"[{self._scope_label(scope)}] 开始抓取工作空间")
            artifact = None
            if scope == "free" and business_oauth_client is not None:
                artifact = self._capture_workspace_artifact_via_existing_session(
                    scope=scope,
                    oauth_client=business_oauth_client,
                    device_id=getattr(register_client, "device_id", "") or "",
                    user_agent=getattr(register_client, "ua", None),
                    sec_ch_ua=getattr(register_client, "sec_ch_ua", None),
                    impersonate=getattr(register_client, "impersonate", None),
                )
            if not artifact:
                artifact = self._capture_workspace_artifact_via_fresh_login(
                    scope=scope,
                    email=result.email,
                    password=result.password,
                    device_id=getattr(register_client, "device_id", "") or "",
                    user_agent=getattr(register_client, "ua", None),
                    sec_ch_ua=getattr(register_client, "sec_ch_ua", None),
                    impersonate=getattr(register_client, "impersonate", None),
                    browser_fingerprint=getattr(register_client, "fingerprint", None),
                    email_adapter=email_adapter,
                    first_name=first_name,
                    last_name=last_name,
                    birthdate=birthdate,
                )
            if artifact and self._artifact_has_refresh_token(artifact):
                available_artifacts[scope] = artifact
                self._log(
                    f"[{self._scope_label(scope)}] 已保存 account_id={artifact.get('account_id') or '-'} workspace_id={artifact.get('workspace_id') or '-'}"
                )
                continue
            optional_failures.append(scope)
            self._log(
                f"抓取 {scope} 工作空间失败，但不会回滚已保存的 business 信息",
                "warning",
            )

        ordered_artifacts = [available_artifacts[scope] for scope in selected_scopes if scope in available_artifacts]
        if not ordered_artifacts:
            result.success = False
            result.error_message = "未生成任何工作空间产物"
            self._log(result.error_message, "warning")
            return False

        result.workspace_artifacts = ordered_artifacts
        result.metadata["selected_workspace_scopes"] = selected_scopes
        result.metadata["workspace_capture_optional_failures"] = optional_failures
        result.metadata["workspace_capture_partial_success"] = bool(optional_failures)
        result.metadata["workspace_artifact_summaries"] = [
            {
                "scope": str(item.get("scope") or ""),
                "label": str(item.get("label") or ""),
                "account_id": str(item.get("account_id") or ""),
                "workspace_id": str(item.get("workspace_id") or ""),
                "source": str(item.get("source") or ""),
            }
            for item in ordered_artifacts
        ]
        self._apply_phone_challenge_metadata(result)
        if optional_failures:
            failed_labels = " / ".join(self._scope_label(scope) for scope in optional_failures)
            self._log(f"[结果] 成功，已保留 business；未获取 {failed_labels}")
        else:
            self._log("[结果] 成功，所需工作空间均已获取")
        return True

    def _capture_workspace_artifact_via_existing_session(
        self,
        *,
        scope: str,
        oauth_client: Optional[OAuthClient],
        device_id: str,
        user_agent: Optional[str],
        sec_ch_ua: Optional[str],
        impersonate: Optional[str],
    ) -> Optional[dict[str, Any]]:
        normalized_scope = self._normalize_workspace_scope(scope)
        if not normalized_scope or oauth_client is None:
            return None

        self._log(
            f"预热｜复用已登录 auth 会话抓取 {normalized_scope} 工作空间",
            "debug",
        )
        tokens = oauth_client.capture_workspace_tokens_from_authenticated_session(
            device_id=device_id or "",
            user_agent=user_agent,
            sec_ch_ua=sec_ch_ua,
            impersonate=impersonate,
            workspace_scope_preference=normalized_scope,
        )
        self._remember_oauth_phone_challenge_events(oauth_client)
        if not tokens:
            self._log(
                f"预热｜复用已登录 auth 会话抓取 {normalized_scope} 失败: {oauth_client.last_error or 'OAuth 登录失败'}",
                "debug",
            )
            return None

        artifact = self._build_workspace_artifact(
            tokens=tokens,
            oauth_client=oauth_client,
            source=f"workspace_capture_{normalized_scope}",
            scope_hint=normalized_scope,
        )
        actual_scope = self._normalize_workspace_scope(artifact.get("scope") or "")
        if actual_scope and actual_scope != normalized_scope:
            self._log(
                f"预热｜目标是 {normalized_scope} 工作空间，但复用会话实际拿到 {actual_scope}，本轮视为未命中",
                "debug",
            )
            return None
        artifact["scope"] = normalized_scope
        artifact["label"] = self._scope_label(normalized_scope)
        artifact["variant_key"] = f"{normalized_scope}:{artifact.get('workspace_id') or artifact.get('account_id') or 'unknown'}"
        return artifact

    def _capture_workspace_artifact_via_fresh_login(
        self,
        *,
        scope: str,
        email: str,
        password: str,
        device_id: str,
        user_agent: Optional[str],
        sec_ch_ua: Optional[str],
        impersonate: Optional[str],
        browser_fingerprint: Optional[Dict[str, Any]],
        email_adapter,
        first_name: str,
        last_name: str,
        birthdate: str,
        login_source: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        normalized_scope = self._normalize_workspace_scope(scope)
        if not normalized_scope:
            return None

        self._log(f"[{normalized_scope}] 开始真实 auth 登录")
        scoped_oauth_client = self._build_oauth_client()
        allow_add_phone_verification, allow_existing_phone_verification = self._registration_full_auth_phone_policy()
        try:
            tokens = scoped_oauth_client.login_and_get_tokens(
                email,
                password,
                device_id=device_id or "",
                user_agent=user_agent,
                sec_ch_ua=sec_ch_ua,
                impersonate=impersonate,
                browser_fingerprint=browser_fingerprint,
                skymail_client=email_adapter,
                prefer_passwordless_login=True,
                allow_phone_verification=bool(allow_add_phone_verification or allow_existing_phone_verification),
                allow_add_phone_verification=allow_add_phone_verification,
                allow_existing_phone_verification=allow_existing_phone_verification,
                force_new_browser=True,
                force_chatgpt_entry=False,
                screen_hint="login",
                force_password_login=False,
                complete_about_you_if_needed=True,
                first_name=first_name,
                last_name=last_name,
                birthdate=birthdate,
                login_source=str(login_source or f"workspace_capture_{normalized_scope}").strip()
                or f"workspace_capture_{normalized_scope}",
                stop_after_login=False,
                workspace_scope_preference=normalized_scope,
                allow_add_phone_session_recovery=False,
            )
        except TaskInterruption:
            self._remember_oauth_phone_challenge_events(scoped_oauth_client)
            raise
        self._remember_oauth_phone_challenge_events(scoped_oauth_client)
        if not tokens:
            self._last_workspace_capture_error = str(scoped_oauth_client.last_error or "OAuth 登录失败")
            self._log(
                f"抓取 {normalized_scope} 工作空间失败: {scoped_oauth_client.last_error or 'OAuth 登录失败'}",
                "warning",
            )
            self._log(
                f"[{normalized_scope}] fresh auth 未返回 tokens，last_error={scoped_oauth_client.last_error or 'OAuth 登录失败'}",
                "debug",
            )
            return None

        self._last_workspace_capture_error = ""
        artifact = self._build_workspace_artifact(
            tokens=tokens,
            oauth_client=scoped_oauth_client,
            source=f"workspace_capture_{normalized_scope}",
            scope_hint=normalized_scope,
        )
        actual_scope = self._normalize_workspace_scope(artifact.get("scope") or "")
        if actual_scope and actual_scope != normalized_scope:
            self._last_workspace_capture_error = f"目标是 {normalized_scope} 工作空间，但实际拿到 {actual_scope}"
            self._log(
                f"目标是 {normalized_scope} 工作空间，但实际拿到 {actual_scope}，本轮视为未命中",
                "warning",
            )
            self._log(
                f"[{normalized_scope}] artifact scope mismatch: actual_scope={actual_scope} account_id={artifact.get('account_id') or '-'} workspace_id={artifact.get('workspace_id') or '-'} source={artifact.get('source') or '-'}",
                "debug",
            )
            return None
        artifact["scope"] = normalized_scope
        artifact["label"] = self._scope_label(normalized_scope)
        artifact["variant_key"] = f"{normalized_scope}:{artifact.get('workspace_id') or artifact.get('account_id') or 'unknown'}"
        return artifact

    def _finalize_workspace_artifacts(
        self,
        *,
        result: RegistrationResult,
        register_client: ChatGPTClient,
        email_adapter,
        first_name: str,
        last_name: str,
        birthdate: str,
        primary_oauth_client: Optional[OAuthClient] = None,
    ) -> bool:
        current_scope = self._infer_scope_from_access_token(result.access_token, source=result.source)
        selected_scopes = self._resolve_workspace_capture_scopes(current_scope=current_scope)
        allow_partial_success = self._is_existing_account_capture_enabled()
        available_artifacts: dict[str, dict[str, Any]] = {}
        optional_failures: list[str] = []

        if result.access_token:
            primary_artifact = {
                "scope": current_scope or "business",
                "label": self._scope_label(current_scope or "business"),
                "account_id": result.account_id,
                "workspace_id": result.workspace_id,
                "access_token": result.access_token,
                "refresh_token": result.refresh_token,
                "id_token": result.id_token,
                "session_token": result.session_token,
                "source": result.source,
                "variant_key": f"{current_scope or 'business'}:{result.workspace_id or result.account_id or 'unknown'}",
            }
            available_artifacts[primary_artifact["scope"]] = primary_artifact

        for scope in selected_scopes:
            if scope in available_artifacts:
                continue
            artifact = None
            if primary_oauth_client is not None:
                artifact = self._capture_workspace_artifact_via_existing_session(
                    scope=scope,
                    oauth_client=primary_oauth_client,
                    device_id=getattr(register_client, "device_id", "") or "",
                    user_agent=getattr(register_client, "ua", None),
                    sec_ch_ua=getattr(register_client, "sec_ch_ua", None),
                    impersonate=getattr(register_client, "impersonate", None),
                )
            if not artifact:
                artifact = self._capture_workspace_artifact_via_fresh_login(
                    scope=scope,
                    email=result.email,
                    password=result.password,
                    device_id=getattr(register_client, "device_id", "") or "",
                    user_agent=getattr(register_client, "ua", None),
                    sec_ch_ua=getattr(register_client, "sec_ch_ua", None),
                    impersonate=getattr(register_client, "impersonate", None),
                    browser_fingerprint=getattr(register_client, "fingerprint", None),
                    email_adapter=email_adapter,
                    first_name=first_name,
                    last_name=last_name,
                    birthdate=birthdate,
                )
            if artifact and self._artifact_has_refresh_token(artifact):
                available_artifacts[scope] = artifact
                self._log(
                    f"[{self._scope_label(scope)}] 已保存 account_id={artifact.get('account_id') or '-'} workspace_id={artifact.get('workspace_id') or '-'}"
                )
                continue
            optional_failures.append(scope)

        missing_scopes = [scope for scope in selected_scopes if scope not in available_artifacts]
        if missing_scopes and not allow_partial_success:
            result.success = False
            result.error_message = f"未能获取所选工作空间: {', '.join(missing_scopes)}"
            self._log(result.error_message, "warning")
            return False

        # 如果 free 抓取失败但 business 成功，且用户明确同时选择 business + free，
        # 才用 business artifact 复制一份 free 回退版本。订阅后补抓的默认 free-only 链路
        # 不应因为一次误判/回退保存出两条相同工作空间数据。
        if (
            "free" in selected_scopes
            and "business" in selected_scopes
            and "free" not in available_artifacts
            and "business" in available_artifacts
        ):
            business_artifact = available_artifacts["business"]
            free_artifact = dict(business_artifact)
            free_artifact["scope"] = "free"
            free_artifact["label"] = "free"
            free_artifact["variant_key"] = f"free:{free_artifact.get('workspace_id') or free_artifact.get('account_id') or 'unknown'}"
            free_artifact["source"] = free_artifact.get("source", "") + "_free_fallback"
            available_artifacts["free"] = free_artifact
            self._log("[free] 抓取失败，使用 business artifact 作为 free 回退版本，确保自动上传")
            optional_failures = [s for s in optional_failures if s != "free"]

        ordered_artifacts = [available_artifacts[scope] for scope in selected_scopes if scope in available_artifacts]
        if not ordered_artifacts and available_artifacts:
            ordered_artifacts = list(available_artifacts.values())
        if not ordered_artifacts:
            result.success = False
            result.error_message = "未生成任何工作空间产物"
            self._log(result.error_message, "warning")
            return False

        if not self._artifact_has_refresh_token(ordered_artifacts[0]):
            result.success = False
            result.error_message = "主工作空间未获取到 refresh_token"
            self._log(result.error_message, "warning")
            return False

        self._apply_workspace_artifact_to_result(result, ordered_artifacts[0])
        result.workspace_artifacts = ordered_artifacts
        result.error_message = ""
        result.metadata = result.metadata or {}
        result.metadata["selected_workspace_scopes"] = selected_scopes
        result.metadata["workspace_capture_optional_failures"] = optional_failures
        result.metadata["workspace_capture_partial_success"] = bool(optional_failures)
        result.metadata["workspace_artifact_summaries"] = [
            {
                "scope": str(item.get("scope") or ""),
                "label": str(item.get("label") or ""),
                "account_id": str(item.get("account_id") or ""),
                "workspace_id": str(item.get("workspace_id") or ""),
                "source": str(item.get("source") or ""),
            }
            for item in ordered_artifacts
        ]
        self._apply_phone_challenge_metadata(result)
        if optional_failures:
            failed_labels = " / ".join(self._scope_label(scope) for scope in optional_failures)
            if allow_partial_success:
                self._log(f"[结果] 部分成功：已保存 {len(ordered_artifacts)} 个工作空间，未获取 {failed_labels}", "warning")
            else:
                self._log(f"[结果] 未获取 {failed_labels}", "warning")
        else:
            self._log("[结果] 成功，所需工作空间均已获取")
        return True

    def _populate_result_from_tokens(
        self,
        result: RegistrationResult,
        tokens: dict[str, Any],
        oauth_client: OAuthClient,
        registration_message: str,
        source: str,
        register_client: ChatGPTClient,
    ) -> None:
        account_info = self._extract_account_info(tokens)
        workspace_id = self._extract_workspace_id(oauth_client)
        session_token = self._extract_session_token(oauth_client)

        result.email = self.email or ""
        result.password = self.password or ""
        result.access_token = str(tokens.get("access_token") or "").strip()
        result.refresh_token = str(tokens.get("refresh_token") or "").strip()
        result.id_token = str(tokens.get("id_token") or "").strip()
        if not result.refresh_token:
            result.success = False
            result.error_message = "OAuth 登录成功但未获取 refresh_token"
            return
        result.success = True
        result.account_id = str(
            tokens.get("account_id")
            or account_info.get("account_id")
            or ""
        ).strip()
        result.workspace_id = workspace_id
        result.session_token = session_token
        result.source = source
        result.metadata = {
            "email_service": self.email_service.service_type.value,
            "proxy_used": self.proxy_url,
            "registered_at": datetime.now().isoformat(),
            "registration_message": registration_message,
            "registration_flow": "chatgpt_client.register_complete_flow",
            "token_flow": "oauth_client.login_and_get_tokens",
            "token_login_mode": "passwordless",
            "browser_mode": self.browser_mode,
            "device_id": getattr(register_client, "device_id", ""),
            "impersonate": getattr(register_client, "impersonate", ""),
            "user_agent": getattr(register_client, "ua", ""),
            "workspace_id": workspace_id,
            "account_claims_email": account_info.get("email", ""),
        }

    def run(self) -> RegistrationResult:
        result = RegistrationResult(success=False, logs=self.logs)
        last_error = ""
        fixed_email = str(self.email or "").strip()
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
        save_registration_access_token_account = self._is_registration_access_token_save_enabled()
        registration_access_token_artifact: Optional[dict[str, Any]] = None
        self._last_phone_challenge_events = []

        try:
            registration_message = ""
            source = "register"
            existing_account_login_route_event: Optional[dict[str, Any]] = None

            self._log("[主链路] 开始 ChatGPT RT 主链路")

            existing_account_capture = self._is_existing_account_capture_enabled()
            existing_account_login_route_allowed = self._is_existing_account_login_route_enabled()
            if existing_account_capture and not fixed_email:
                result.error_message = "已有账号抓 auth 模式必须填写邮箱地址"
                self._finalize_email_service_failure(result)
                return result
            if not fixed_email:
                self.email = None

            self._log_stage("登录阶段" if existing_account_capture else "注册阶段")
            self._log("[登录] 开始准备已有账号登录" if existing_account_capture else "[注册] 开始预热首页并创建邮箱")
            if not existing_account_capture:
                self._log(
                    "[已有账号] 注册阶段遇到已注册邮箱时"
                    f"{'允许路由到登录恢复' if existing_account_login_route_allowed else '禁止路由到登录恢复，将跳过且不保存'}"
                )
            if not existing_account_capture:
                homepage_ok, homepage_error = self._probe_homepage_before_email_creation()
                self._report_homepage_probe(homepage_ok, homepage_error)
                if not homepage_ok:
                    last_error = homepage_error or "访问首页失败"
                    result.error_message = last_error
                    self._log(f"[注册] 预热失败，跳过邮箱创建: {last_error}", "warning")
                    self._finalize_email_service_failure(result, fallback_error=last_error)
                    return result
            if not self._create_email():
                last_error = "创建邮箱失败"
                result.error_message = last_error
                self._finalize_email_service_failure(result, fallback_error=last_error)
                return result

            result.email = self.email or ""
            self.extra_config["_current_account_email"] = result.email
            self.password = str(self.password or "").strip() if existing_account_capture else (self.password or generate_random_password(16))
            result.password = self.password

            first_name, last_name = generate_random_name()
            birthdate = generate_random_birthday()
            self._log(
                f"[登录] 已锁定账号 {result.email}，准备直接抓取 auth" if existing_account_capture else f"[注册] 邮箱已创建 {result.email}"
            )

            email_adapter = EmailServiceAdapter(
                self.email_service,
                result.email,
                self._log,
                otp_budget=register_otp_budget,
            )

            register_client = self._prepared_register_client or self._build_chatgpt_client()
            self._prepared_register_client = None

            if existing_account_capture:
                selected_scopes = self._resolve_workspace_capture_scopes(current_scope="business")
                login_scope = selected_scopes[0] if selected_scopes else "business"
                oauth_client = None
                tokens = None
                for index, preferred_scope in enumerate(selected_scopes or ["business"]):
                    oauth_client = self._build_oauth_client()
                    self._log(
                        (
                            f"[登录] 已启用已有账号抓 auth 模式，跳过注册状态机，优先抓取 {self._scope_label(preferred_scope)}"
                            if index == 0
                            else f"[登录] 主抓 {self._scope_label(login_scope)} 失败，回退抓取 {self._scope_label(preferred_scope)}"
                        )
                    )
                    candidate_tokens = oauth_client.login_and_get_tokens(
                        result.email,
                        self.password,
                        device_id=getattr(register_client, "device_id", "") or "",
                        user_agent=getattr(register_client, "ua", None),
                        sec_ch_ua=getattr(register_client, "sec_ch_ua", None),
                        impersonate=getattr(register_client, "impersonate", None),
                        browser_fingerprint=getattr(register_client, "fingerprint", None),
                        skymail_client=email_adapter,
                        prefer_passwordless_login=True,
                        allow_phone_verification=False,
                        force_new_browser=True,
                        force_chatgpt_entry=False,
                        screen_hint="login",
                        force_password_login=bool(self.password),
                        complete_about_you_if_needed=True,
                        first_name=first_name,
                        last_name=last_name,
                        birthdate=birthdate,
                        login_source=f"workspace_capture_{preferred_scope}:existing_account_capture",
                        workspace_scope_preference=preferred_scope,
                        allow_add_phone_session_recovery=False,
                    )
                    if candidate_tokens:
                        tokens = candidate_tokens
                        login_scope = preferred_scope
                        break
                    self._remember_oauth_phone_challenge_events(oauth_client)
                    last_error = oauth_client.last_error or f"抓取 {preferred_scope} 失败"
                if not tokens or oauth_client is None:
                    result.error_message = last_error or "已有账号抓 auth 失败"
                    self._finalize_email_service_failure(result, fallback_error=result.error_message)
                    return result
                self._remember_oauth_phone_challenge_events(oauth_client)
                self._populate_result_from_tokens(
                    result=result,
                    tokens=tokens,
                    oauth_client=oauth_client,
                    registration_message="existing_account_capture:ok",
                    source=f"workspace_capture_{login_scope}",
                    register_client=register_client,
                )
                if not result.success:
                    self._log(result.error_message or "已有账号主链路未获取到 refresh_token", "warning")
                    self._finalize_email_service_failure(result, fallback_error=result.error_message)
                    return result
                if not self._finalize_workspace_artifacts(
                    result=result,
                    register_client=register_client,
                    email_adapter=email_adapter,
                    first_name=first_name,
                    last_name=last_name,
                    birthdate=birthdate,
                    primary_oauth_client=oauth_client,
                ):
                    self._finalize_email_service_failure(result, fallback_error=result.error_message)
                    return result
                result.metadata = result.metadata or {}
                result.metadata["registration_context"] = self._build_registration_context_payload(
                    register_client=register_client,
                    first_name=first_name,
                    last_name=last_name,
                    birthdate=birthdate,
                )
                result.metadata = self._attach_browser_fingerprint_metadata(result.metadata, register_client)
                result.metadata["mailbox_state"] = self._export_mailbox_state(email_adapter)
                self._append_gopay_provider_link_metadata(result, tokens or {})
                self._log(f"[结果] 成功，account_id={result.account_id or '-'} workspace_id={result.workspace_id or '-'}")
                self._finalize_email_service_success(result)
                return result

            self._log("[注册] 开始执行注册状态机")
            registered, registration_message = register_client.register_complete_flow(
                result.email,
                self.password,
                first_name,
                last_name,
                birthdate,
                email_adapter,
                stop_before_about_you_submission=False,
                otp_wait_timeout=register_otp_wait_seconds,
                otp_resend_wait_timeout=register_otp_resend_wait_seconds,
                otp_account_budget_timeout=register_otp_account_budget_seconds,
                allow_existing_account_login_route=existing_account_login_route_allowed,
            )

            if not registered:
                if not self._should_switch_to_login_after_register_failure(registration_message):
                    last_error = f"注册状态机失败: {registration_message}"
                    result.error_message = last_error
                    self._finalize_email_service_failure(result, fallback_error=last_error)
                    return result

                existing_account_login_route_event = build_existing_account_login_route_event(
                    email=result.email,
                    reason=registration_message,
                    stage="register_complete_flow",
                    enabled=existing_account_login_route_allowed,
                    routed=existing_account_login_route_allowed,
                    blocked=not existing_account_login_route_allowed,
                    action="login_recovery" if existing_account_login_route_allowed else "skip_save",
                    source="refresh_token_registration",
                    base_event=getattr(register_client, "last_registration_route_event", None),
                )
                if not existing_account_login_route_allowed:
                    last_error = "注册阶段检测到该邮箱已存在，已按配置禁止路由到登录，账号未保存"
                    result.error_message = last_error
                    result.metadata = dict(result.metadata or {})
                    result.metadata["chatgpt_existing_account_login_route"] = existing_account_login_route_event
                    self._log(
                        f"[已有账号] 已跳过并禁止保存: {result.email or '-'} reason={registration_message}",
                        "warning",
                    )
                    self._finalize_email_service_failure(result, fallback_error=last_error)
                    raise ExistingAccountLoginRouteBlocked(
                        result.email,
                        registration_message,
                        existing_account_login_route_event,
                    )

                source = "login"
                self._log(
                    f"[主链路] 注册阶段命中已注册邮箱，切换到登录恢复链路: {result.email or '-'}",
                    "warning",
                )
            else:
                self._log("[注册] 注册阶段已完成")

            if registered and source == "register":
                self._log("[注册] 开始落地 ChatGPT session")
                session_ok, session_or_error = register_client.reuse_session_and_get_tokens()
                if not session_ok:
                    result.error_message = f"注册收尾失败: {session_or_error}"
                    self._log(result.error_message, "warning")
                    self._finalize_email_service_failure(result, fallback_error=result.error_message)
                    return result
                registration_access_token_artifact = self._build_registration_access_token_artifact(
                    session_tokens=session_or_error or {},
                )
                if not str(registration_access_token_artifact.get("access_token") or "").strip():
                    registration_access_token_artifact = None
                result.metadata = result.metadata or {}
                result.metadata["registration_session_account_id"] = str((session_or_error or {}).get("account_id") or "")
                result.metadata["registration_session_workspace_id"] = str((session_or_error or {}).get("workspace_id") or "")
                result.metadata["registration_context"] = self._build_registration_context_payload(
                    register_client=register_client,
                    first_name=first_name,
                    last_name=last_name,
                    birthdate=birthdate,
                )
                result.metadata = self._attach_browser_fingerprint_metadata(result.metadata, register_client)
                result.metadata["mailbox_state"] = self._export_mailbox_state(email_adapter)
                result.metadata["registration_stage_complete"] = True
                result.metadata["registration_access_token_checkpoint_created"] = bool(registration_access_token_artifact)
                result.metadata["registration_access_token_checkpoint_policy"] = "always_keep_before_full_auth"
                if not save_registration_access_token_account:
                    result.metadata["registration_access_token_save_requested"] = False
                if registration_access_token_artifact:
                    self._log("[注册] 已完成注册并建立 AT checkpoint")

                if self._is_team_invite_enabled():
                    if self._is_team_invite_deferred_activation_enabled():
                        self._log("[邀请] 开始保存 pending invite")
                        pending_invite = self._prepare_pending_business_invite(
                            email=result.email,
                            email_adapter=email_adapter,
                        )
                        if not pending_invite:
                            result.error_message = self._last_pending_invite_error_message or "保存 pending invite 失败"
                            self._log(result.error_message, "warning")
                            self._finalize_email_service_failure(result, fallback_error=result.error_message)
                            return result

                        result.success = False
                        result.error_message = "pending invite 已保存，等待统一激活"
                        result.access_token = ""
                        result.refresh_token = ""
                        result.id_token = ""
                        result.session_token = ""
                        result.account_id = ""
                        result.workspace_id = ""
                        result.source = "deferred_invite_pending"
                        result.metadata["deferred_activation"] = True
                        result.metadata["registration_stage_complete"] = True
                        result.metadata["deferred_activation_status"] = "invite_sent_pending_activation"
                        result.metadata["pending_business_invite"] = {
                            **dict(pending_invite or {}),
                            "status": "invite_sent_pending_activation",
                            "invite_sent_at": datetime.now().isoformat(),
                        }
                        result.workspace_artifacts = []
                        self._log("[结果] 已保存 pending invite，等待统一激活阶段")
                        self._finalize_email_service_failure(result, fallback_error=result.error_message)
                        return result

                    self._log("[邀请] 开始进入 business 工作空间")
                    business_join_result = self._enter_business_before_workspace_capture(
                        email=result.email,
                        password=self.password,
                        email_adapter=email_adapter,
                        user_agent=getattr(register_client, "ua", None),
                        sec_ch_ua=getattr(register_client, "sec_ch_ua", None),
                        impersonate=getattr(register_client, "impersonate", None),
                        browser_fingerprint=getattr(register_client, "fingerprint", None),
                        first_name=first_name,
                        last_name=last_name,
                        birthdate=birthdate,
                        register_client=register_client,
                    )
                    if not business_join_result:
                        result.error_message = "进入 business 工作空间失败"
                        self._log(result.error_message, "warning")
                        if self._return_registration_access_token_partial_result(
                            result=result,
                            artifact=registration_access_token_artifact,
                            reason=result.error_message,
                        ):
                            self._finalize_email_service_success(result)
                            return result
                        self._apply_phone_challenge_metadata(result)
                        self._finalize_email_service_failure(result, fallback_error=result.error_message)
                        return result

                    self._log("[business] 邀请阶段已完成，开始保存 business 工作空间")
                    self._log("[注册] 开始第二阶段完整 Auth 捕获")
                    try:
                        business_capture_ok = self._capture_workspace_artifacts_after_business_join(
                            result=result,
                            register_client=register_client,
                            email_adapter=email_adapter,
                            first_name=first_name,
                            last_name=last_name,
                            birthdate=birthdate,
                            business_join_result=business_join_result,
                        )
                    except SkipCurrentAttemptRequested as exc:
                        business_capture_ok = False
                        result.error_message = str(exc or "第二阶段完整 Auth 捕获被跳过")
                    if not business_capture_ok:
                        if not str(result.error_message or "").strip():
                            result.error_message = self._last_workspace_capture_error or "未生成任何工作空间产物"
                        if self._return_registration_access_token_partial_result(
                            result=result,
                            artifact=registration_access_token_artifact,
                            reason=result.error_message,
                        ):
                            self._finalize_email_service_success(result)
                            return result
                        self._finalize_email_service_failure(result, fallback_error=result.error_message)
                        return result

                    self._append_gopay_provider_link_metadata(
                        result,
                        session_or_error if isinstance(session_or_error, dict) else {},
                    )
                    self._log(f"[结果] 成功，account_id={result.account_id or '-'} workspace_id={result.workspace_id or '-'}")
                    self._finalize_email_service_success(result)
                    return result

                result.success = False
                result.access_token = ""
                result.refresh_token = ""
                result.id_token = ""
                result.session_token = ""
                result.account_id = ""
                result.workspace_id = ""
                result.source = ""
                self._log("[注册] 开始第二阶段完整 Auth 捕获")
                try:
                    workspace_capture_ok = self._finalize_workspace_artifacts(
                        result=result,
                        register_client=register_client,
                        email_adapter=email_adapter,
                        first_name=first_name,
                        last_name=last_name,
                        birthdate=birthdate,
                    )
                except SkipCurrentAttemptRequested as exc:
                    workspace_capture_ok = False
                    result.error_message = str(exc or "第二阶段完整 Auth 捕获被跳过")
                if not workspace_capture_ok:
                    if not str(result.error_message or "").strip():
                        result.error_message = self._last_workspace_capture_error or "未生成任何工作空间产物"
                    if self._return_registration_access_token_partial_result(
                        result=result,
                        artifact=registration_access_token_artifact,
                        reason=result.error_message,
                    ):
                        self._finalize_email_service_success(result)
                        return result
                    self._finalize_email_service_failure(result, fallback_error=result.error_message)
                    return result

                self._append_gopay_provider_link_metadata(
                    result,
                    session_or_error if isinstance(session_or_error, dict) else {},
                )
                self._log(f"[结果] 成功，account_id={result.account_id or '-'} workspace_id={result.workspace_id or '-'}")
                self._finalize_email_service_success(result)
                return result

            oauth_client = self._build_oauth_client()
            self._log("[主链路] 开始登录恢复链路")
            tokens = oauth_client.login_and_get_tokens(
                result.email,
                self.password,
                device_id="",
                user_agent=getattr(register_client, "ua", None),
                sec_ch_ua=getattr(register_client, "sec_ch_ua", None),
                impersonate=getattr(register_client, "impersonate", None),
                browser_fingerprint=getattr(register_client, "fingerprint", None),
                skymail_client=email_adapter,
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
                login_source="existing_account_recovery",
                allow_add_phone_session_recovery=False,
            )

            if not tokens:
                self._remember_oauth_phone_challenge_events(oauth_client)
                last_error = oauth_client.last_error or "OAuth 登录状态机失败"
                if self._is_team_invite_enabled() and self._should_attempt_business_workspace_recovery(oauth_client):
                    self._log("[主链路] OAuth 主链路未拿到 workspace，转入 business recovery", "warning")
                    recovery_result = self._recover_workspace_with_business(
                        email=result.email,
                        password=self.password,
                        device_id=(getattr(register_client, "device_id", "") or ""),
                        user_agent=getattr(register_client, "ua", None),
                        sec_ch_ua=getattr(register_client, "sec_ch_ua", None),
                        impersonate=getattr(register_client, "impersonate", None),
                        browser_fingerprint=getattr(register_client, "fingerprint", None),
                        email_adapter=email_adapter,
                        first_name=first_name,
                        last_name=last_name,
                        birthdate=birthdate,
                    )
                    if recovery_result:
                        tokens = recovery_result.get("tokens") or {}
                        oauth_client = recovery_result.get("oauth_client") or oauth_client
                        source = "business_recovery"
                        self._remember_oauth_phone_challenge_events(oauth_client)
                        self._populate_result_from_tokens(
                            result=result,
                            tokens=tokens,
                            oauth_client=oauth_client,
                            registration_message=(registration_message or "register_complete_flow:ok") + "|business_recovery",
                            source=source,
                            register_client=register_client,
                        )
                        if existing_account_login_route_event:
                            result.metadata = dict(result.metadata or {})
                            result.metadata["chatgpt_existing_account_login_route"] = existing_account_login_route_event
                            result.metadata["existing_account_login_routed"] = True
                        if not result.success:
                            self._log(result.error_message or "business recovery 未获取到 refresh_token", "warning")
                            self._apply_phone_challenge_metadata(result)
                            self._finalize_email_service_failure(result, fallback_error=result.error_message)
                            return result
                        result.metadata = result.metadata or {}
                        result.metadata["business_recovery_team_id"] = recovery_result.get("team_id")
                        result.metadata["business_recovery_joined"] = bool(recovery_result.get("joined"))
                        result.metadata["business_workspace_id"] = str(recovery_result.get("workspace_id") or "")
                        if not self._finalize_workspace_artifacts(
                            result=result,
                            register_client=register_client,
                            email_adapter=email_adapter,
                            first_name=first_name,
                            last_name=last_name,
                            birthdate=birthdate,
                        ):
                            self._apply_phone_challenge_metadata(result)
                            self._finalize_email_service_failure(result, fallback_error=result.error_message)
                            return result
                        self._append_gopay_provider_link_metadata(result, tokens or {})
                        self._apply_phone_challenge_metadata(result)
                        self._log(f"[结果] 成功，account_id={result.account_id or '-'} workspace_id={result.workspace_id or '-'}")
                        self._finalize_email_service_success(result)
                        return result
                result.error_message = last_error
                self._apply_phone_challenge_metadata(result)
                self._finalize_email_service_failure(result, fallback_error=last_error)
                return result

            self._remember_oauth_phone_challenge_events(oauth_client)
            self._populate_result_from_tokens(
                result=result,
                tokens=tokens,
                oauth_client=oauth_client,
                registration_message=registration_message or "register_complete_flow:ok",
                source=source,
                register_client=register_client,
            )
            if existing_account_login_route_event:
                result.metadata = dict(result.metadata or {})
                result.metadata["chatgpt_existing_account_login_route"] = existing_account_login_route_event
                result.metadata["existing_account_login_routed"] = True
            if not result.success:
                self._log(result.error_message or "OAuth 主链路未获取到 refresh_token", "warning")
                self._apply_phone_challenge_metadata(result)
                self._finalize_email_service_failure(result, fallback_error=result.error_message)
                return result
            if not self._finalize_workspace_artifacts(
                result=result,
                register_client=register_client,
                email_adapter=email_adapter,
                first_name=first_name,
                last_name=last_name,
                birthdate=birthdate,
            ):
                self._apply_phone_challenge_metadata(result)
                self._finalize_email_service_failure(result, fallback_error=result.error_message)
                return result

            self._append_gopay_provider_link_metadata(result, tokens or {})
            self._apply_phone_challenge_metadata(result)
            self._log(f"[结果] 成功，account_id={result.account_id or '-'} workspace_id={result.workspace_id or '-'}")
            self._finalize_email_service_success(result)
            return result

        except TaskInterruption:
            raise
        except Exception as e:
            self._log(f"RT 注册主链路异常: {e}", "error")
            result.error_message = str(e)
            self._finalize_email_service_failure(result, fallback_error=result.error_message)
            return result

    def save_to_database(self, result: RegistrationResult) -> bool:
        """保留旧接口，占位返回。"""
        return bool(result and result.success)
