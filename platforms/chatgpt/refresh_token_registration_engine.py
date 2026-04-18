"""
ChatGPT Refresh Token 注册引擎。

新实现不再沿用旧的分步补丁式注册链路，而是直接复用：
1. `ChatGPTClient.register_complete_flow()` 负责完整注册状态机
2. `OAuthClient.login_and_get_tokens()` 负责全新 OAuth + passwordless OTP 登录拿 RT

目标是让 refresh_token 模式与当前主状态机链路保持一致，不再以旧流程做兜底。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from core.task_runtime import TaskInterruption

from .chatgpt_client import ChatGPTClient
from .oauth import OAuthManager
from .oauth_client import OAuthClient
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

    def __init__(self, email_service, email: str, log_fn: Callable[[str], None]):
        self.email_service = email_service
        self.email = email
        self.log_fn = log_fn
        self._used_codes_by_phase: dict[str, set[str]] = {}
        self._used_message_ids_by_phase: dict[str, set[str]] = {}
        self._last_verification_result_by_phase: dict[str, dict[str, Any]] = {}

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
        deadline = time.monotonic() + max(int(timeout or 0), 1)
        self._log(f"正在等待邮箱 {email} 的验证码（{phase_title}, {timeout}s）...")

        while time.monotonic() < deadline:
            remaining = max(1, int(deadline - time.monotonic()))
            code = self.email_service.get_verification_code(
                email=email,
                timeout=remaining,
                otp_sent_at=otp_sent_at,
                exclude_codes=None,
                phase=phase_key,
                phase_label=phase_title,
            )
            if not code:
                return code

            normalized_code = str(code).strip()
            meta = self._read_last_verification_result()
            message_id = str(meta.get("message_id") or meta.get("id") or "").strip()

            if message_id:
                if message_id in used_message_ids:
                    self._log(f"跳过已处理验证码邮件（{phase_title}）", "debug")
                    continue
                used_message_ids.add(message_id)
                used_codes.add(normalized_code)
                meta["code"] = normalized_code
                meta["phase"] = phase_key
                self._last_verification_result_by_phase[phase_key] = meta
                self._log(f"成功获取验证码（{phase_title}）")
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
            self._log(f"成功获取验证码（{phase_title}）")
            return normalized_code

        return None


class RefreshTokenRegistrationEngine:
    """Refresh token 注册引擎。"""

    def __init__(
        self,
        email_service,
        proxy_url: Optional[str] = None,
        callback_logger: Optional[Callable[[str], None]] = None,
        task_uuid: Optional[str] = None,
        browser_mode: str = "protocol",
        max_retries: int = 3,
        extra_config: Optional[dict] = None,
    ):
        self.email_service = email_service
        self.proxy_url = proxy_url
        self.callback_logger = callback_logger or (lambda msg: logger.info(msg))
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

    @staticmethod
    def _classify_log_level(message: str, level: str = "info") -> str:
        normalized_level = str(level or "info").strip().lower() or "info"
        if normalized_level in {"error", "warning", "debug"}:
            return normalized_level

        text = str(message or "").strip()
        allowed_info_prefixes = ("[主链路]", "[注册]", "[邀请]", "[business]", "[free]", "[结果]")
        if text.startswith(allowed_info_prefixes):
            return "info"
        if text.startswith("[") and "]" in text:
            return "debug"

        normalized_text = text
        while normalized_text.startswith("[") and "]" in normalized_text:
            prefix, _, rest = normalized_text.partition("]")
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
            "consent 页面请求 ->",
            "consent HTML 中也未提取到 workspace 数据",
            "Sentinel Browser 模式:",
            "Sentinel Browser 启动:",
            "Sentinel Browser 成功:",
            "force_new_browser:",
            "OAuth 指纹:",
            "步骤1: Bootstrap OAuth session...",
            "步骤2: POST /api/accounts/authorize/continue",
            "步骤3: 命中 login_password，按新链路直接触发 passwordless OTP",
            "步骤4: 检测到邮箱 OTP 验证",
            "步骤5: 当前 about_you 属于既有账号恢复链路，跳过 create_account，直接转 consent/workspace",
            "步骤6: 解析 Codex workspace / org / code",
            "步骤7: POST /oauth/token",
            "获取到 authorization code:",
            "✅ OAuth 登录成功",
            "换取 tokens 失败",
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
            "复用已登录 auth 会话抓取 workspace",
            "复用会话状态步进[",
            "复用会话遇到未支持的 OAuth 状态:",
        )
        debug_contains = (
            "page=",
            "method=GET next=",
            "method=POST next=",
            "workspace/select ->",
            "organization/select ->",
            "authorize_continue:",
            "email_otp_validate:",
            "passwordless OTP 已触发",
            "OAuth OTP 等待窗口:",
            "使用 wait_for_verification_code 进行阻塞式获取新验证码",
            "/oauth/authorize ->",
            "/authorize/continue ->",
            "/passwordless/send-otp ->",
            "/email-otp/validate ->",
            "/email-otp/send ->",
            "login_session: 已获取",
            "authorize_continue 分支判定:",
            "等待 OTP 异常:",
            "已触发 email-otp 重发",
            "暂未收到新的 OTP，继续等待",
            "尝试 OTP:",
            "session 中没有 workspace 信息",
            "oai-client-auth-session 已存在，但其中没有 workspaces 字段",
            "从 oai-client-auth-session cookie 读取到",
            "选择 workspace:",
            "选择 organization:",
            "验证码发送状态:",
            "触发发送验证码",
            "发送注册验证码成功",
            "等待邮箱验证码",
            "验证 OTP 码:",
            "完成账号创建:",
            "username_password_create:",
            "oauth_create_account:",
            "create_account: 已生成 sentinel token",
            "Session Account ID:",
            "Session User ID:",
            "步骤 1/4:",
            "步骤 2/4:",
            "步骤 3/4:",
            "步骤 4/4:",
            "Account ID:",
            "Workspace ID:",
            "复用会话 workspace/org 选择失败",
            "复用已登录 auth 会话时仍回到了登录/OTP/about_you",
        )
        if text.startswith("="):
            return "debug"
        if text[:2].isdigit() and len(text) > 2 and text[2] == ".":
            return "debug"
        if normalized_text.startswith(debug_prefixes) or text.startswith(debug_prefixes) or any(marker in text for marker in debug_contains):
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

    def _log_stage(self, title: str, *, level: str = "debug"):
        self._log(f"================ {title} ================", level)

    def _create_email(self) -> bool:
        try:
            self._log(f"正在创建 {self.email_service.service_type.value} 邮箱...")
            self.email_info = self.email_service.create_email()

            email_value = str(
                self.email
                or (self.email_info or {}).get("email")
                or ""
            ).strip()
            if not email_value:
                self._log(
                    f"创建邮箱失败: {self.email_service.service_type.value} 返回空邮箱地址",
                    "error",
                )
                return False

            if self.email_info is None:
                self.email_info = {}
            self.email_info["email"] = email_value
            self.email = email_value
            self._log(f"成功创建邮箱: {self.email}")
            return True
        except Exception as e:
            self._log(f"创建邮箱失败: {e}", "error")
            return False

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
            log_fn=lambda msg: self._log(msg),
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
        )
        client._log = lambda msg: self._log(f"[注册链路] {msg}")
        return client

    def _build_oauth_client(self) -> OAuthClient:
        client = OAuthClient(
            self.extra_config,
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
                return [normalized_current]
            if has_free_flag and self._read_bool_config("chatgpt_capture_free_workspace", default=True):
                return ["free"]
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
            log_fn=lambda msg: self._log(msg),
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
            log_fn=lambda msg: self._log(msg),
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

        if "business" not in available_artifacts:
            result.success = False
            result.error_message = "未能获取所选工作空间: business"
            self._log(result.error_message, "warning")
            return False

        if not self._artifact_has_refresh_token(available_artifacts.get("business")):
            result.success = False
            result.error_message = "business 工作空间未获取到 refresh_token"
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
    ) -> Optional[dict[str, Any]]:
        normalized_scope = self._normalize_workspace_scope(scope)
        if not normalized_scope:
            return None

        self._log(f"[{normalized_scope}] 开始真实 auth 登录")
        scoped_oauth_client = self._build_oauth_client()
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
            allow_phone_verification=False,
            force_new_browser=True,
            force_chatgpt_entry=False,
            screen_hint="login",
            force_password_login=False,
            complete_about_you_if_needed=True,
            first_name=first_name,
            last_name=last_name,
            birthdate=birthdate,
            login_source=f"workspace_capture_{normalized_scope}",
            stop_after_login=False,
            workspace_scope_preference=normalized_scope,
        )
        if not tokens:
            self._log(
                f"抓取 {normalized_scope} 工作空间失败: {scoped_oauth_client.last_error or 'OAuth 登录失败'}",
                "warning",
            )
            return None

        artifact = self._build_workspace_artifact(
            tokens=tokens,
            oauth_client=scoped_oauth_client,
            source=f"workspace_capture_{normalized_scope}",
            scope_hint=normalized_scope,
        )
        actual_scope = self._normalize_workspace_scope(artifact.get("scope") or "")
        if actual_scope and actual_scope != normalized_scope:
            self._log(
                f"目标是 {normalized_scope} 工作空间，但实际拿到 {actual_scope}，本轮视为未命中",
                "warning",
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
            default=600,
            minimum=30,
            maximum=3600,
        )
        register_otp_resend_wait_seconds = self._read_int_config(
            "chatgpt_register_otp_resend_wait_seconds",
            fallback_keys=("chatgpt_register_otp_wait_seconds", "chatgpt_otp_wait_seconds"),
            default=300,
            minimum=30,
            maximum=3600,
        )

        try:
            registration_message = ""
            source = "register"

            self._log("[主链路] 开始 ChatGPT RT 主链路")

            existing_account_capture = self._is_existing_account_capture_enabled()
            if existing_account_capture and not fixed_email:
                result.error_message = "已有账号抓 auth 模式必须填写邮箱地址"
                return result
            if not fixed_email:
                self.email = None

            self._log_stage("登录阶段" if existing_account_capture else "注册阶段")
            self._log("[登录] 开始准备已有账号登录" if existing_account_capture else "[注册] 开始创建邮箱")
            if not self._create_email():
                last_error = "创建邮箱失败"
                result.error_message = last_error
                return result

            result.email = self.email or ""
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
            )

            register_client = self._build_chatgpt_client()
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
                    )
                    if candidate_tokens:
                        tokens = candidate_tokens
                        login_scope = preferred_scope
                        break
                    last_error = oauth_client.last_error or f"抓取 {preferred_scope} 失败"
                if not tokens or oauth_client is None:
                    result.error_message = last_error or "已有账号抓 auth 失败"
                    return result
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
                    return result
                self._log(f"[结果] 成功，account_id={result.account_id or '-'} workspace_id={result.workspace_id or '-'}")
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
            )

            if not registered:
                if not self._should_switch_to_login_after_register_failure(registration_message):
                    last_error = f"注册状态机失败: {registration_message}"
                    result.error_message = last_error
                    return result

                source = "login"
                self._log("[主链路] 注册阶段命中可恢复终态，切换到登录恢复链路", "warning")
            else:
                self._log("[注册] 注册阶段已完成")

            if registered and source == "register":
                self._log("[注册] 开始落地 ChatGPT session")
                session_ok, session_or_error = register_client.reuse_session_and_get_tokens()
                if not session_ok:
                    result.error_message = f"注册收尾失败: {session_or_error}"
                    self._log(result.error_message, "warning")
                    return result
                result.metadata = result.metadata or {}
                result.metadata["registration_session_account_id"] = str((session_or_error or {}).get("account_id") or "")
                result.metadata["registration_session_workspace_id"] = str((session_or_error or {}).get("workspace_id") or "")
                result.metadata["registration_context"] = self._build_registration_context_payload(
                    register_client=register_client,
                    first_name=first_name,
                    last_name=last_name,
                    birthdate=birthdate,
                )
                result.metadata["mailbox_state"] = self._export_mailbox_state(email_adapter)

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
                        return result

                    self._log("[business] 邀请阶段已完成，开始保存 business 工作空间")
                    if not self._capture_workspace_artifacts_after_business_join(
                        result=result,
                        register_client=register_client,
                        email_adapter=email_adapter,
                        first_name=first_name,
                        last_name=last_name,
                        birthdate=birthdate,
                        business_join_result=business_join_result,
                    ):
                        return result

                    self._log(f"[结果] 成功，account_id={result.account_id or '-'} workspace_id={result.workspace_id or '-'}")
                    return result

                result.success = False
                result.access_token = ""
                result.refresh_token = ""
                result.id_token = ""
                result.session_token = ""
                result.account_id = ""
                result.workspace_id = ""
                result.source = ""
                self._log("[free] 开始真实 auth 登录")
                if not self._finalize_workspace_artifacts(
                    result=result,
                    register_client=register_client,
                    email_adapter=email_adapter,
                    first_name=first_name,
                    last_name=last_name,
                    birthdate=birthdate,
                ):
                    return result

                self._log(f"[结果] 成功，account_id={result.account_id or '-'} workspace_id={result.workspace_id or '-'}")
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
            )

            if not tokens:
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
                        self._populate_result_from_tokens(
                            result=result,
                            tokens=tokens,
                            oauth_client=oauth_client,
                            registration_message=(registration_message or "register_complete_flow:ok") + "|business_recovery",
                            source=source,
                            register_client=register_client,
                        )
                        if not result.success:
                            self._log(result.error_message or "business recovery 未获取到 refresh_token", "warning")
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
                            return result
                        self._log(f"[结果] 成功，account_id={result.account_id or '-'} workspace_id={result.workspace_id or '-'}")
                        return result
                result.error_message = last_error
                return result

            self._populate_result_from_tokens(
                result=result,
                tokens=tokens,
                oauth_client=oauth_client,
                registration_message=registration_message or "register_complete_flow:ok",
                source=source,
                register_client=register_client,
            )
            if not result.success:
                self._log(result.error_message or "OAuth 主链路未获取到 refresh_token", "warning")
                return result
            if not self._finalize_workspace_artifacts(
                result=result,
                register_client=register_client,
                email_adapter=email_adapter,
                first_name=first_name,
                last_name=last_name,
                birthdate=birthdate,
            ):
                return result

            self._log(f"[结果] 成功，account_id={result.account_id or '-'} workspace_id={result.workspace_id or '-'}")
            return result

        except TaskInterruption:
            raise
        except Exception as e:
            self._log(f"RT 注册主链路异常: {e}", "error")
            result.error_message = str(e)
            return result

    def save_to_database(self, result: RegistrationResult) -> bool:
        """保留旧接口，占位返回。"""
        return bool(result and result.success)
