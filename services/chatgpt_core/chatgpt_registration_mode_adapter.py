"""ChatGPT 注册模式适配器。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Optional

from core.base_platform import Account, AccountStatus
from services.chatgpt_core.account_fingerprint import persist_account_browser_fingerprint
from services.chatgpt_core.mailbox_state import sanitize_mailbox_state

CHATGPT_REGISTRATION_MODE_REFRESH_TOKEN = "refresh_token"
CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY = "access_token_only"
# Registration owns signup and the credentials produced by that browser
# session.  Full Auth/refresh-token capture is a separate task.
DEFAULT_CHATGPT_REGISTRATION_MODE = CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY


def normalize_chatgpt_registration_mode(value) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    if normalized in {
        CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY,
        "access_token",
        "at_only",
        "without_rt",
        "without_refresh_token",
        "no_rt",
        "0",
        "false",
    }:
        return CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY
    if normalized in {
        CHATGPT_REGISTRATION_MODE_REFRESH_TOKEN,
        "rt",
        "with_rt",
        "has_rt",
        "1",
        "true",
    }:
        return CHATGPT_REGISTRATION_MODE_REFRESH_TOKEN
    return DEFAULT_CHATGPT_REGISTRATION_MODE


def resolve_chatgpt_registration_mode(extra: Optional[dict]) -> str:
    extra = extra or {}
    if "chatgpt_registration_mode" in extra:
        return normalize_chatgpt_registration_mode(extra.get("chatgpt_registration_mode"))
    if "chatgpt_has_refresh_token_solution" in extra:
        return (
            CHATGPT_REGISTRATION_MODE_REFRESH_TOKEN
            if bool(extra.get("chatgpt_has_refresh_token_solution"))
            else CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY
        )
    return DEFAULT_CHATGPT_REGISTRATION_MODE


@dataclass(frozen=True)
class ChatGPTRegistrationContext:
    email_service: object
    proxy_url: Optional[str]
    callback_logger: Callable[[str], None]
    email: Optional[str]
    password: Optional[str]
    browser_mode: str
    max_retries: int
    extra_config: dict


class BaseChatGPTRegistrationModeAdapter(ABC):
    mode: str

    @abstractmethod
    def _create_engine(self, context: ChatGPTRegistrationContext):
        """按模式构造底层注册引擎。"""

    def _run_registration_only(self, context: ChatGPTRegistrationContext):
        """Run signup once and stop after the signup Web session is saved.

        Legacy callers may still send ``refresh_token`` mode.  That value is
        accepted for request compatibility, but it must never turn a signup
        attempt into an OAuth/Auth capture workflow.
        """
        from services.chatgpt_core.access_token_only_registration_engine import (
            AccessTokenOnlyRegistrationEngine,
        )

        extra_config = dict(context.extra_config or {})
        extra_config.update(
            {
                "chatgpt_registration_mode": CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY,
                "chatgpt_has_refresh_token_solution": False,
                "chatgpt_access_token_only_checkout_amount_check_enabled": False,
            }
        )
        engine = AccessTokenOnlyRegistrationEngine(
            email_service=context.email_service,
            proxy_url=context.proxy_url,
            browser_mode=context.browser_mode,
            callback_logger=context.callback_logger,
            max_retries=context.max_retries,
            extra_config=extra_config,
        )
        if context.email is not None:
            engine.email = context.email
        if context.password is not None:
            engine.password = context.password
        result = engine.run()
        metadata = getattr(result, "metadata", None)
        if not isinstance(metadata, dict):
            metadata = {}
            result.metadata = metadata
        if getattr(result, "success", False):
            access_token_saved = bool(str(getattr(result, "access_token", "") or "").strip())
            # Do not leak historical two-stage/Auth failure markers into a
            # signup-only result.  ``registered_auth_pending`` is retained as
            # an explicit browser committed-signup state, but it is not an
            # Auth failure produced by this registration task.
            for key in (
                "chatgpt_rt_registration_two_stage",
                "auth_capture_stage",
                "auth_capture_method",
                "needs_auth_capture",
                "auth_capture_required",
                "registration_full_auth_failed",
                "registration_full_auth_error",
                "registration_full_auth_failed_policy",
            ):
                metadata.pop(key, None)
            metadata.update(
                {
                    "registration_stage": "access_token_saved" if access_token_saved else "registered_auth_pending",
                    "registration_stage_complete": True,
                    "registration_access_token_saved": access_token_saved,
                    "registration_auth_capture": "not_requested",
                }
            )
            if access_token_saved:
                result.source = "registration_session"
                self._log_registration_complete(result, context)
        return result

    @staticmethod
    def _log_registration_complete(result, context: ChatGPTRegistrationContext) -> None:
        logger = context.callback_logger
        if not callable(logger):
            return
        try:
            logger(
                "[注册] signup 已完成，已保存 AccessToken/Session/Cookie；"
                "注册任务结束，不执行独立 Auth/refresh_token 捕获"
            )
        except TypeError:
            logger(
                "[注册] signup 已完成，已保存 AccessToken/Session/Cookie；"
                "注册任务结束，不执行独立 Auth/refresh_token 捕获",
                "info",
            )

    def run(self, context: ChatGPTRegistrationContext):
        return self._run_registration_only(context)

    def build_account(self, result, fallback_password: str) -> Account:
        extra = self._build_account_extra(result)
        if extra.get("chatgpt_payment_already_paid") or extra.get("chatgpt_account_unavailable"):
            status = AccountStatus.INVALID
        elif extra.get("partial_auth") or extra.get("auth_level") == "access_token_only":
            status = AccountStatus.PENDING_PAYMENT
        else:
            status = AccountStatus.REGISTERED
        return Account(
            platform="chatgpt",
            email=getattr(result, "email", ""),
            password=getattr(result, "password", "") or fallback_password,
            user_id=str(getattr(result, "account_id", "") or ""),
            token=str(getattr(result, "access_token", "") or ""),
            status=status,
            extra=extra,
        )

    def _build_account_extra(self, result) -> dict:
        metadata = getattr(result, "metadata", None)
        metadata = metadata if isinstance(metadata, dict) else {}
        access_token = str(getattr(result, "access_token", "") or "").strip()
        refresh_token = str(getattr(result, "refresh_token", "") or "").strip()
        registered_auth_pending = bool(metadata.get("registered_auth_pending")) and not (
            access_token or refresh_token
        )
        partial_auth = registered_auth_pending or self.mode == CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY or (
            not refresh_token
            and bool(
                metadata.get("registration_access_token_saved")
                or metadata.get("registration_access_token_checkpoint_created")
                or metadata.get("registration_full_auth_failed")
            )
        )
        return self._build_account_extra_from_auth(
            {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "id_token": getattr(result, "id_token", ""),
                "session_token": getattr(result, "session_token", ""),
                "workspace_id": getattr(result, "workspace_id", ""),
                "account_id": getattr(result, "account_id", ""),
                "source": getattr(result, "source", "register"),
                "auth_level": (
                    "registered_auth_pending"
                    if registered_auth_pending
                    else "access_token_only" if partial_auth else "full"
                ),
                "partial_auth": partial_auth,
            },
            result,
        )

    def _build_account_extra_from_auth(self, auth: dict, result) -> dict:
        email = getattr(result, "email", "")
        workspace_id = auth.get("workspace_id") or getattr(result, "workspace_id", "")
        account_id = auth.get("account_id") or getattr(result, "account_id", "")
        def _auth_or_result(key: str, attr: str) -> Any:
            if key in auth:
                return auth.get(key) or ""
            return getattr(result, attr, "")

        access_token = _auth_or_result("access_token", "access_token")
        refresh_token = _auth_or_result("refresh_token", "refresh_token")
        id_token = _auth_or_result("id_token", "id_token")
        session_token = _auth_or_result("session_token", "session_token")
        metadata = getattr(result, "metadata", None) or {}
        registration_only = bool(
            metadata.get("registration_auth_capture") == "not_requested"
            or getattr(result, "source", "") == "registration_session"
        )
        extra = {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "id_token": id_token,
            "session_token": session_token,
            "workspace_id": workspace_id,
            "account_id": account_id,
            "chatgpt_registration_mode": (
                CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY
                if registration_only
                else self.mode
            ),
            "chatgpt_has_refresh_token_solution": bool(str(refresh_token or "").strip()),
            "chatgpt_token_source": auth.get("source") or getattr(result, "source", "register"),
        }
        if auth.get("cookies"):
            extra["cookies"] = auth.get("cookies")
            extra.setdefault("cookie_header", auth.get("cookies"))
        if auth.get("cookie_header"):
            extra["cookie_header"] = auth.get("cookie_header")
            extra.setdefault("cookies", auth.get("cookie_header"))
        if auth.get("auth_level"):
            extra["auth_level"] = auth.get("auth_level")
        if auth.get("partial_auth"):
            extra["partial_auth"] = True
        if isinstance(metadata, dict):
            for key in (
                "chatgpt_rt_registration_two_stage",
                "registration_stage",
                "registration_stage_complete",
                "registration_access_token_saved",
                "registration_auth_capture",
                "registration_access_token_checkpoint_created",
                "registration_access_token_checkpoint_policy",
                "registration_access_token_partial_reason",
                "registration_session_account_id",
                "registration_session_workspace_id",
                "registration_stage1_saved_account_id",
                "auth_capture_stage",
                "auth_capture_method",
                "chatgpt_existing_account_login_route",
                "existing_account_login_routed",
                "chatgpt_register_unique_exit_ip_enabled",
                "chatgpt_register_exit_ip",
                "chatgpt_browser_fingerprint",
                "chatgpt_browser_fingerprint_isolated",
                "chatgpt_browser_fingerprint_signature",
                "chatgpt_browser_fingerprint_source",
                "chatgpt_browser_fingerprint_saved_at",
                "chatgpt_checkout_plan",
                "chatgpt_checkout_url",
                "chatgpt_checkout_country",
                "chatgpt_checkout_currency",
                "chatgpt_checkout_amount",
                "chatgpt_checkout_amount_raw",
                "chatgpt_checkout_amount_source",
                "chatgpt_checkout_amount_is_zero",
                "chatgpt_access_token_only_zero_amount_stop_enabled",
                "chatgpt_access_token_only_zero_amount_stop_threshold",
                "chatgpt_checkout_probe",
                "chatgpt_checkout_error_code",
                "chatgpt_checkout_error_status",
                "chatgpt_checkout_error_body",
                "chatgpt_account_unavailable",
                "chatgpt_unavailable_reason",
                "chatgpt_payment_already_paid",
                "chatgpt_skip_save_account",
                "chatgpt_skip_save_reason",
                "cookies",
                "cookie_header",
                "registration_web_session_material_preserved",
                "registered_auth_pending",
                "chatgpt_browser_runtime_profile",
            ):
                if key in metadata:
                    if key in {"cookies", "cookie_header"} and extra.get(key):
                        continue
                    extra[key] = metadata.get(key)
            if metadata.get("chatgpt_checkout_url"):
                extra.setdefault("cashier_url", metadata.get("chatgpt_checkout_url"))
                from services.chatgpt_core.payment_link_cache import cache_checkout_link_in_extra

                cache_checkout_link_in_extra(
                    extra,
                    source="access_token_only_registration",
                )
            mailbox_state = metadata.get("mailbox_state") or metadata.get("chatgpt_mailbox_state")
            if mailbox_state:
                cleaned_mailbox_state = sanitize_mailbox_state(mailbox_state, account_email=str(email or ""))
                if cleaned_mailbox_state:
                    extra["chatgpt_mailbox_state"] = cleaned_mailbox_state
            if metadata.get("registration_context"):
                registration_context = metadata.get("registration_context")
                extra["chatgpt_registration_context"] = registration_context
                if isinstance(registration_context, dict):
                    extra["requested_executor_type"] = str(
                        registration_context.get("requested_executor") or ""
                    )
                    extra["effective_executor_type"] = str(
                        registration_context.get("effective_executor") or ""
                    )
                    extra["chatgpt_registration_transport"] = str(
                        registration_context.get("registration_transport") or ""
                    )
                    extra["chatgpt_registration_stage_transports"] = list(
                        registration_context.get("stage_transports") or []
                    )
            if metadata.get("needs_auth_capture"):
                extra["needs_auth_capture"] = True
                extra["auth_capture_required"] = True
            if metadata.get("registered_auth_pending"):
                extra["registered_auth_pending"] = True
            if metadata.get("registration_full_auth_failed"):
                extra["registration_full_auth_failed"] = True
                extra["registration_full_auth_error"] = metadata.get("registration_full_auth_error") or metadata.get(
                    "registration_access_token_partial_reason"
                ) or ""
                extra["registration_full_auth_failed_policy"] = metadata.get(
                    "registration_full_auth_failed_policy"
                ) or (
                    "keep_registered_auth_pending"
                    if metadata.get("registered_auth_pending")
                    else "keep_access_token_checkpoint"
                )
            if metadata.get("chatgpt_phone_challenge"):
                extra["chatgpt_phone_challenge"] = metadata.get("chatgpt_phone_challenge")
            if metadata.get("chatgpt_phone_challenge_history"):
                extra["chatgpt_phone_challenge_history"] = metadata.get("chatgpt_phone_challenge_history")
            if metadata.get("chatgpt_phone_binding"):
                extra["chatgpt_phone_binding"] = metadata.get("chatgpt_phone_binding")
            if metadata.get("chatgpt_phone_binding_history"):
                extra["chatgpt_phone_binding_history"] = metadata.get("chatgpt_phone_binding_history")
            if metadata.get("chatgpt_bound_phone"):
                extra["chatgpt_bound_phone"] = metadata.get("chatgpt_bound_phone")
            if metadata.get("chatgpt_bound_phone_number"):
                extra["chatgpt_bound_phone_number"] = metadata.get("chatgpt_bound_phone_number")
            if metadata.get("chatgpt_bound_phone_masked"):
                extra["chatgpt_bound_phone_masked"] = metadata.get("chatgpt_bound_phone_masked")
        metadata_fingerprint = None
        if isinstance(metadata, dict):
            metadata_fingerprint = metadata.get("chatgpt_browser_fingerprint")
            if not metadata_fingerprint and isinstance(metadata.get("registration_context"), dict):
                metadata_fingerprint = (metadata.get("registration_context") or {}).get("browser_fingerprint")
        if not metadata_fingerprint and isinstance(auth.get("browser_fingerprint"), dict):
            metadata_fingerprint = auth.get("browser_fingerprint")
        metadata_source = metadata if isinstance(metadata, dict) else {}
        extra = persist_account_browser_fingerprint(
            extra,
            metadata_fingerprint,
            source=str(metadata_source.get("chatgpt_browser_fingerprint_source") or "registration"),
            overwrite=False,
        )
        return extra

class RefreshTokenChatGPTRegistrationAdapter(BaseChatGPTRegistrationModeAdapter):
    mode = CHATGPT_REGISTRATION_MODE_REFRESH_TOKEN

    @staticmethod
    def _parse_bool(value: Any, *, default: bool = False) -> bool:
        if value is None or value == "":
            return bool(default)
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        text = str(value or "").strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
        return bool(default)

    def _two_stage_enabled(self, extra: Optional[dict]) -> bool:
        extra = extra or {}
        if self._parse_bool(extra.get("chatgpt_existing_account_capture"), default=False):
            return False
        return self._parse_bool(extra.get("chatgpt_rt_registration_two_stage_enabled"), default=True)

    def _mark_access_token_checkpoint(self, result) -> None:
        metadata = getattr(result, "metadata", None)
        if not isinstance(metadata, dict):
            metadata = {}
            result.metadata = metadata

        result.source = "registration_session"
        metadata["registration_access_token_checkpoint_created"] = True

    @staticmethod
    def _merge_stage_logs(target_result, *source_results) -> None:
        try:
            merged: list[Any] = []
            for source_result in source_results:
                for item in list(getattr(source_result, "logs", None) or []):
                    if item not in merged:
                        merged.append(item)
            if merged:
                target_result.logs = merged
        except Exception:
            pass

    @staticmethod
    def _suppress_failure_email_service(email_service):
        class _Stage2EmailService:
            def __init__(self, delegate):
                self._delegate = delegate

            def __getattr__(self, name):
                return getattr(self._delegate, name)

            def finalize_failure(self, error_message: str = "", task_id: str = ""):
                return None

        return _Stage2EmailService(email_service)

    @staticmethod
    def _suppress_success_email_service(email_service):
        class _Stage1EmailService:
            def __init__(self, delegate):
                self._delegate = delegate

            def __getattr__(self, name):
                return getattr(self._delegate, name)

            def finalize_success(self, account_email: str = "", task_id: str = ""):
                return None

        return _Stage1EmailService(email_service)

    @staticmethod
    def _finalize_original_email_success(
        email_service,
        *,
        account_email: str,
        task_id: str = "",
        result_code: str = "login_alive",
        access_token_saved: bool = False,
    ) -> dict:
        try:
            email_service._registration_result_code = str(result_code or "login_alive")
            email_service._registration_access_token_saved = bool(access_token_saved)
        except Exception:
            pass
        finalize = getattr(email_service, "finalize_success", None)
        if callable(finalize):
            try:
                finalize(account_email=account_email, task_id=task_id)
            except Exception:
                pass
        exporter = getattr(email_service, "export_state", None)
        if callable(exporter):
            try:
                state = exporter() or {}
                return dict(state) if isinstance(state, dict) else {}
            except Exception:
                pass
        return {}

    @staticmethod
    def _first_non_empty_string(*values: Any) -> str:
        for value in values:
            text = str(value or "").strip()
            if text:
                return text
        return ""

    def _inherit_stage1_web_session_material(self, target_result, stage1_result) -> None:
        """第二阶段只补 OAuth/RT 时，保留第一阶段已落地的 ChatGPT Web 会话材料。"""
        stage1_metadata = getattr(stage1_result, "metadata", None)
        stage1_metadata = stage1_metadata if isinstance(stage1_metadata, dict) else {}
        stage1_session_token = self._first_non_empty_string(
            getattr(stage1_result, "session_token", ""),
        )
        stage1_cookies = self._first_non_empty_string(
            stage1_metadata.get("cookies"),
            stage1_metadata.get("cookie_header"),
        )
        stage1_cookie_header = self._first_non_empty_string(stage1_metadata.get("cookie_header"))

        if not stage1_session_token and not stage1_cookies and not stage1_cookie_header:
            return

        target_metadata = getattr(target_result, "metadata", None)
        if not isinstance(target_metadata, dict):
            target_metadata = {}
            target_result.metadata = target_metadata

        inherited = False
        if stage1_session_token and not self._first_non_empty_string(getattr(target_result, "session_token", "")):
            target_result.session_token = stage1_session_token
            inherited = True

        if stage1_cookies and not self._first_non_empty_string(target_metadata.get("cookies")):
            target_metadata["cookies"] = stage1_cookies
            inherited = True
        if stage1_cookie_header and not self._first_non_empty_string(target_metadata.get("cookie_header")):
            target_metadata["cookie_header"] = stage1_cookie_header
            inherited = True

        if inherited:
            target_metadata["registration_web_session_material_preserved"] = True

    def _save_checkpoint_account(self, result, fallback_password: str):
        from core.db import save_account

        account = self.build_account(result, fallback_password)
        return save_account(account)

    def _capture_stage2_from_stage1_session(
        self,
        *,
        context: ChatGPTRegistrationContext,
        stage1_engine,
        stage1_result,
        stage2_extra: dict,
        saved_stage1_id: int,
    ):
        from services.chatgpt_core.refresh_token_registration_engine import (
            EmailServiceAdapter,
            RefreshTokenRegistrationEngine,
            RegistrationResult,
        )
        from services.chatgpt_core.utils import (
            decode_jwt_payload,
            generate_random_birthday,
            generate_random_name,
        )

        email = str(getattr(stage1_result, "email", "") or context.email or "").strip()
        password = str(getattr(stage1_result, "password", "") or context.password or "")
        stage2_engine = RefreshTokenRegistrationEngine(
            email_service=self._suppress_failure_email_service(context.email_service),
            proxy_url=context.proxy_url,
            callback_logger=context.callback_logger,
            browser_mode=context.browser_mode,
            max_retries=1,
            extra_config=stage2_extra,
        )
        stage2_engine.email = email
        stage2_engine.password = password

        register_client = getattr(stage1_engine, "_last_chatgpt_client", None)
        if register_client is None:
            build_client = getattr(stage2_engine, "_build_chatgpt_client", None)
            if callable(build_client):
                register_client = build_client()
            else:
                return RegistrationResult(
                    success=False,
                    email=email,
                    password=password,
                    error_message="第一阶段未保留注册上下文，无法执行第二阶段 RT 捕获",
                    logs=list(getattr(stage2_engine, "logs", None) or []),
                    metadata={},
                )

        stage2_engine._log("[注册] 第二阶段：使用注册邮箱抓取完整 Auth/RT")
        email_adapter = EmailServiceAdapter(stage2_engine.email_service, email, stage2_engine._log)
        stage1_metadata = getattr(stage1_result, "metadata", None)
        stage1_metadata = stage1_metadata if isinstance(stage1_metadata, dict) else {}
        stage1_context = stage1_metadata.get("registration_context")
        stage1_context = stage1_context if isinstance(stage1_context, dict) else {}
        first_name = str(stage1_context.get("first_name") or "").strip()
        last_name = str(stage1_context.get("last_name") or "").strip()
        birthdate = str(stage1_context.get("birthdate") or "").strip()
        if not first_name or not last_name:
            first_name, last_name = generate_random_name()
        if not birthdate:
            birthdate = generate_random_birthday()

        if context.browser_mode in {"headless", "headed"}:
            browser_ok, browser_payload = stage1_engine._capture_browser_oauth_tokens(
                chatgpt_client=register_client,
                email_addr=email,
                password=password,
                skymail_adapter=email_adapter,
            )
            if browser_ok and isinstance(browser_payload, dict):
                access_token = str(browser_payload.get("access_token") or "").strip()
                auth_claims = (
                    decode_jwt_payload(access_token).get("https://api.openai.com/auth")
                    or {}
                )
                account_id = str(
                    browser_payload.get("account_id")
                    or auth_claims.get("chatgpt_account_id")
                    or ""
                ).strip()
                auth_payload = {
                    "access_token": access_token,
                    "refresh_token": str(browser_payload.get("refresh_token") or "").strip(),
                    "id_token": str(browser_payload.get("id_token") or "").strip(),
                    "session_token": str(browser_payload.get("session_token") or "").strip(),
                    "account_id": account_id,
                    "workspace_id": str(browser_payload.get("workspace_id") or account_id),
                    "source": "registration_stage2_browser_oauth",
                }
            else:
                auth_payload = None
                stage2_engine._last_auth_capture_error = str(
                    browser_payload or "注册第二阶段浏览器 OAuth 捕获失败"
                )
        else:
            auth_payload = stage2_engine._capture_auth_via_fresh_login(
                email=email,
                password=password,
                device_id=getattr(register_client, "device_id", "") or "",
                user_agent=getattr(register_client, "ua", None),
                sec_ch_ua=getattr(register_client, "sec_ch_ua", None),
                impersonate=getattr(register_client, "impersonate", None),
                browser_fingerprint=getattr(register_client, "fingerprint", None),
                email_adapter=email_adapter,
                first_name=first_name,
                last_name=last_name,
                birthdate=birthdate,
                login_source="registration_stage2_full_auth",
            )
        if not auth_payload:
            error = str(
                getattr(stage2_engine, "_last_auth_capture_error", "")
                or "注册第二阶段抓取 RT 失败"
            ).strip()
            return RegistrationResult(
                success=False,
                email=email,
                password=password,
                error_message=error,
                logs=list(getattr(stage2_engine, "logs", None) or []),
                metadata={},
            )
        if not stage2_engine._auth_payload_has_refresh_token(auth_payload):
            return RegistrationResult(
                success=False,
                email=email,
                password=password,
                error_message="注册第二阶段已拿到 OAuth access_token，但未返回 refresh_token",
                logs=list(getattr(stage2_engine, "logs", None) or []),
                metadata={},
            )

        result = RegistrationResult(
            success=True,
            email=email,
            password=password,
            logs=list(getattr(stage2_engine, "logs", None) or []),
            metadata={},
        )
        stage2_engine._apply_auth_payload_to_result(result, auth_payload)
        result.metadata = {
            key: value
            for key, value in stage1_metadata.items()
            if key
            in {
                "mailbox_state",
                "chatgpt_mailbox_state",
                "registration_context",
                "registration_session_account_id",
                "registration_session_workspace_id",
                "cookies",
                "cookie_header",
                "chatgpt_browser_runtime_profile",
            }
        }
        if context.browser_mode in {"headless", "headed"}:
            build_context = getattr(stage1_engine, "_build_registration_context_payload", None)
            if callable(build_context):
                registration_context = build_context(
                    chatgpt_client=register_client,
                    first_name=first_name,
                    last_name=last_name,
                    birthdate=birthdate,
                )
                result.metadata["registration_context"] = registration_context
                if isinstance(registration_context, dict) and isinstance(
                    registration_context.get("browser_runtime_profile"),
                    dict,
                ):
                    result.metadata["chatgpt_browser_runtime_profile"] = dict(
                        registration_context["browser_runtime_profile"]
                    )
        result.metadata.update(
            {
                "chatgpt_rt_registration_two_stage": True,
                "registration_stage": "full_auth_saved",
                "registration_stage_complete": True,
                "registration_access_token_saved": True,
                "registration_stage1_saved_account_id": saved_stage1_id,
                "auth_capture_stage": "success",
                "auth_capture_method": (
                    "registration_stage2_browser_oauth"
                    if context.browser_mode in {"headless", "headed"}
                    else "registration_stage2_full_auth"
                ),
            }
        )
        return result

    def run(self, context: ChatGPTRegistrationContext):
        # ``refresh_token`` is a legacy registration setting.  Registration
        # itself never performs the independent OAuth/Auth capture; callers
        # must enqueue the dedicated subscription-auth task for that work.
        return self._run_registration_only(context)

    def _run_browser_existing_account_capture(
        self,
        context: ChatGPTRegistrationContext,
    ):
        from services.chatgpt_core.access_token_only_registration_engine import (
            AccessTokenOnlyRegistrationEngine,
        )

        engine = AccessTokenOnlyRegistrationEngine(
            email_service=context.email_service,
            proxy_url=context.proxy_url,
            browser_mode=context.browser_mode,
            callback_logger=context.callback_logger,
            max_retries=context.max_retries,
            extra_config=dict(context.extra_config or {}),
        )
        if context.email is not None:
            engine.email = context.email
        if context.password is not None:
            engine.password = context.password
        return engine.run()

    def _run_two_stage_registration(self, context: ChatGPTRegistrationContext):
        from services.chatgpt_core.access_token_only_registration_engine import AccessTokenOnlyRegistrationEngine

        stage1_extra = dict(context.extra_config or {})
        stage1_extra.update(
            {
                "chatgpt_registration_mode": CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY,
                "chatgpt_has_refresh_token_solution": False,
                "chatgpt_existing_account_capture": False,
                # 第一阶段只负责注册/登录并拿 ChatGPT Web access_token；不要让账单、
                # GoPay 或 RT/full-auth 探测改变“先落库”的语义。
                "chatgpt_access_token_only_checkout_amount_check_enabled": False,
            }
        )

        stage1_engine = AccessTokenOnlyRegistrationEngine(
            email_service=self._suppress_success_email_service(context.email_service),
            proxy_url=context.proxy_url,
            browser_mode=context.browser_mode,
            callback_logger=context.callback_logger,
            max_retries=context.max_retries,
            extra_config=stage1_extra,
        )
        if context.email is not None:
            stage1_engine.email = context.email
        if context.password is not None:
            stage1_engine.password = context.password

        stage1_result = stage1_engine.run()
        if not getattr(stage1_result, "success", False):
            return stage1_result
        if not str(getattr(stage1_result, "access_token", "") or "").strip():
            stage1_metadata = getattr(stage1_result, "metadata", None)
            if isinstance(stage1_metadata, dict) and stage1_metadata.get(
                "registered_auth_pending"
            ):
                # Browser signup is already committed. Returning the pending
                # account prevents a second signup submission for the same email.
                stage1_metadata.update(
                    {
                        "chatgpt_rt_registration_two_stage": True,
                        "registration_stage": "registered_auth_pending",
                        "registration_stage_complete": True,
                        "auth_capture_stage": "pending",
                        "registration_full_auth_failed": True,
                        "registration_full_auth_failed_policy": "keep_registered_auth_pending",
                    }
                )
                mailbox_state = self._finalize_original_email_success(
                    context.email_service,
                    account_email=str(
                        getattr(stage1_result, "email", "") or context.email or ""
                    ).strip(),
                    task_id=str((context.extra_config or {}).get("_current_task_id") or ""),
                    result_code="registered_auth_pending",
                    access_token_saved=False,
                )
                if mailbox_state:
                    stage1_metadata["mailbox_state"] = mailbox_state
                return stage1_result
            stage1_result.success = False
            stage1_result.error_message = "第一阶段无 RT 注册成功但未获取 access_token"
            return stage1_result

        stage1_result.metadata = dict(getattr(stage1_result, "metadata", None) or {})
        stage1_result.metadata.update(
            {
                "chatgpt_rt_registration_two_stage": True,
                "registration_stage": "access_token_saved",
                "registration_stage_complete": True,
                "registration_access_token_saved": True,
                "registration_access_token_checkpoint_created": True,
                "registration_access_token_checkpoint_policy": "save_before_full_auth",
            }
        )
        stage1_result.metadata.setdefault("registration_session_account_id", str(getattr(stage1_result, "account_id", "") or ""))
        stage1_result.metadata.setdefault("registration_session_workspace_id", str(getattr(stage1_result, "workspace_id", "") or ""))
        self._mark_access_token_checkpoint(stage1_result)

        saved_stage1 = self._save_checkpoint_account(
            stage1_result,
            str(getattr(stage1_result, "password", "") or context.password or ""),
        )
        saved_stage1_id = int(getattr(saved_stage1, "id", 0) or 0)
        if saved_stage1_id > 0:
            stage1_result.metadata["registration_stage1_saved_account_id"] = saved_stage1_id
            self._finalize_original_email_success(
                context.email_service,
                account_email=str(getattr(stage1_result, "email", "") or context.email or "").strip(),
                task_id=str((context.extra_config or {}).get("_current_task_id") or ""),
                result_code="login_alive",
                access_token_saved=bool(
                    str(getattr(stage1_result, "access_token", "") or "").strip()
                ),
            )

        stage2_extra = dict(context.extra_config or {})
        stage2_extra.update(
            {
                "chatgpt_registration_mode": CHATGPT_REGISTRATION_MODE_REFRESH_TOKEN,
                "chatgpt_has_refresh_token_solution": True,
                "chatgpt_existing_account_capture": False,
                "chatgpt_rt_registration_two_stage_enabled": False,
                "chatgpt_rt_registration_second_stage_session_reuse_only": False,
                "chatgpt_rt_registration_second_stage_context": "registration_stage2_full_auth",
                "_current_account_email": str(getattr(stage1_result, "email", "") or context.email or "").strip(),
            }
        )
        if saved_stage1_id > 0:
            stage2_extra["_current_account_id"] = saved_stage1_id
        stage2_result = self._capture_stage2_from_stage1_session(
            context=context,
            stage1_engine=stage1_engine,
            stage1_result=stage1_result,
            stage2_extra=stage2_extra,
            saved_stage1_id=saved_stage1_id,
        )
        if getattr(stage2_result, "success", False):
            stage2_result.metadata = dict(getattr(stage2_result, "metadata", None) or {})
            stage2_result.metadata.update(
                {
                    "chatgpt_rt_registration_two_stage": True,
                    "registration_stage": "full_auth_saved",
                    "registration_stage_complete": True,
                    "registration_access_token_saved": True,
                    "registration_stage1_saved_account_id": saved_stage1_id,
                    "auth_capture_stage": "success",
                }
            )
            self._inherit_stage1_web_session_material(stage2_result, stage1_result)
            if not str(getattr(stage2_result, "email", "") or "").strip():
                stage2_result.email = str(getattr(stage1_result, "email", "") or "")
            if not str(getattr(stage2_result, "password", "") or "").strip():
                stage2_result.password = str(getattr(stage1_result, "password", "") or "")
            self._save_checkpoint_account(
                stage2_result,
                str(getattr(stage2_result, "password", "") or getattr(stage1_result, "password", "") or context.password or ""),
            )
            self._merge_stage_logs(stage2_result, stage1_result, stage2_result)
            return stage2_result

        error_message = str(getattr(stage2_result, "error_message", "") or "第二阶段完整 Auth 捕获失败").strip()
        self._merge_stage_logs(stage1_result, stage1_result, stage2_result)
        stage1_result.success = True
        stage1_result.error_message = ""
        stage1_result.metadata.update(
            {
                "registration_stage": "access_token_saved",
                "auth_capture_stage": "failed",
                "needs_auth_capture": True,
                "auth_capture_required": True,
                "registration_full_auth_failed": True,
                "registration_full_auth_error": error_message,
                "registration_full_auth_failed_policy": "keep_access_token_checkpoint",
                "registration_access_token_partial_reason": error_message,
            }
        )
        stage1_result.metadata.setdefault("registration_stage1_saved_account_id", saved_stage1_id)
        self._mark_access_token_checkpoint(stage1_result)
        return stage1_result

    def _create_engine(self, context: ChatGPTRegistrationContext):
        # The legacy class is retained for ``build_account`` compatibility,
        # but it must never construct the Auth-capable engine for signup.
        from services.chatgpt_core.access_token_only_registration_engine import AccessTokenOnlyRegistrationEngine

        return AccessTokenOnlyRegistrationEngine(
            email_service=context.email_service,
            proxy_url=context.proxy_url,
            callback_logger=context.callback_logger,
            browser_mode=context.browser_mode,
            max_retries=context.max_retries,
            extra_config=context.extra_config,
        )


class AccessTokenOnlyChatGPTRegistrationAdapter(BaseChatGPTRegistrationModeAdapter):
    mode = CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY

    def _create_engine(self, context: ChatGPTRegistrationContext):
        from services.chatgpt_core.access_token_only_registration_engine import AccessTokenOnlyRegistrationEngine

        return AccessTokenOnlyRegistrationEngine(
            email_service=context.email_service,
            proxy_url=context.proxy_url,
            browser_mode=context.browser_mode,
            callback_logger=context.callback_logger,
            max_retries=context.max_retries,
            extra_config=context.extra_config,
        )


def build_chatgpt_registration_mode_adapter(
    extra: Optional[dict],
) -> BaseChatGPTRegistrationModeAdapter:
    # Keep the legacy adapter object available to callers that use its
    # account-building helpers, but its ``run`` method is registration-only
    # and can never enter the historical two-stage OAuth path.
    mode = resolve_chatgpt_registration_mode(extra)
    if mode == CHATGPT_REGISTRATION_MODE_REFRESH_TOKEN:
        return RefreshTokenChatGPTRegistrationAdapter()
    return AccessTokenOnlyChatGPTRegistrationAdapter()
