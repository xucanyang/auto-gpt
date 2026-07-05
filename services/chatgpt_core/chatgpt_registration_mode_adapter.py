"""ChatGPT 注册模式适配器。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Optional

from core.base_platform import Account, AccountStatus

CHATGPT_REGISTRATION_MODE_REFRESH_TOKEN = "refresh_token"
CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY = "access_token_only"
DEFAULT_CHATGPT_REGISTRATION_MODE = CHATGPT_REGISTRATION_MODE_REFRESH_TOKEN


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

    def run(self, context: ChatGPTRegistrationContext):
        engine = self._create_engine(context)
        if context.email is not None:
            engine.email = context.email
        if context.password is not None:
            engine.password = context.password
        return engine.run()

    def build_account(self, result, fallback_password: str) -> Account:
        accounts = self._build_workspace_accounts(result, fallback_password)
        primary = accounts[0]
        if len(accounts) > 1:
            primary.extra = dict(primary.extra or {})
            primary.extra["_linked_accounts_to_save"] = [
                {
                    "platform": account.platform,
                    "email": account.email,
                    "password": account.password,
                    "user_id": account.user_id,
                    "region": account.region,
                    "token": account.token,
                    "status": account.status.value,
                    "extra": account.extra,
                }
                for account in accounts[1:]
            ]
            primary.extra["chatgpt_workspace_variants"] = [
                {
                    "scope": str((account.extra or {}).get("chatgpt_workspace_scope") or ""),
                    "label": str((account.extra or {}).get("chatgpt_workspace_label") or ""),
                    "workspace_id": str((account.extra or {}).get("workspace_id") or ""),
                    "display_name": str((account.extra or {}).get("chatgpt_workspace_display_name") or ""),
                }
                for account in accounts
            ]
        return primary

    @staticmethod
    def _normalize_workspace_scope(value) -> str:
        normalized = str(value or "").strip().lower().replace("-", "_")
        if normalized in {"free", "personal", "personal_free"}:
            return "free"
        if normalized in {"business", "team", "workspace", "enterprise"}:
            return "business"
        return ""

    @staticmethod
    def _dedupe_workspace_artifacts(artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[tuple[str, str, str, str]] = set()
        deduped: list[dict[str, Any]] = []
        for item in artifacts:
            scope = BaseChatGPTRegistrationModeAdapter._normalize_workspace_scope(item.get("scope") or "") or "free"
            workspace_id = str(item.get("workspace_id") or "").strip()
            account_id = str(item.get("account_id") or "").strip()
            refresh_token = str(item.get("refresh_token") or "").strip()
            variant_key = str(item.get("variant_key") or "").strip()
            key = (scope, workspace_id, account_id, refresh_token or variant_key)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        return deduped

    def _build_account_extra(self, result) -> dict:
        scope = self._normalize_workspace_scope(
            getattr(result, "source", "") == "business_recovery" and "business" or ""
        ) or "free"
        return self._build_account_extra_for_artifact(
            {
                "scope": scope,
                "access_token": getattr(result, "access_token", ""),
                "refresh_token": getattr(result, "refresh_token", ""),
                "id_token": getattr(result, "id_token", ""),
                "session_token": getattr(result, "session_token", ""),
                "workspace_id": getattr(result, "workspace_id", ""),
                "account_id": getattr(result, "account_id", ""),
                "source": getattr(result, "source", "register"),
                "variant_key": f"{scope}:{getattr(result, 'workspace_id', '') or getattr(result, 'account_id', '') or 'default'}",
            },
            result,
        )

    def _build_account_extra_for_artifact(self, artifact: dict, result) -> dict:
        scope = self._normalize_workspace_scope(artifact.get("scope") or "") or "free"
        label = "business" if scope == "business" else "free"
        email = getattr(result, "email", "")
        workspace_id = artifact.get("workspace_id") or getattr(result, "workspace_id", "")
        account_id = artifact.get("account_id") or getattr(result, "account_id", "")
        variant_key = artifact.get("variant_key") or f"{scope}:{workspace_id or account_id or 'default'}"
        extra = {
            "access_token": artifact.get("access_token") or getattr(result, "access_token", ""),
            "refresh_token": artifact.get("refresh_token") or getattr(result, "refresh_token", ""),
            "id_token": artifact.get("id_token") or getattr(result, "id_token", ""),
            "session_token": artifact.get("session_token") or getattr(result, "session_token", ""),
            "workspace_id": workspace_id,
            "chatgpt_registration_mode": self.mode,
            "chatgpt_has_refresh_token_solution": self.mode == CHATGPT_REGISTRATION_MODE_REFRESH_TOKEN,
            "chatgpt_token_source": artifact.get("source") or getattr(result, "source", "register"),
            "chatgpt_workspace_scope": scope,
            "chatgpt_workspace_label": label,
            "chatgpt_workspace_display_name": f"{email} [{label}]" if email else f"[{label}]",
            "chatgpt_workspace_variant_key": variant_key,
        }
        if artifact.get("auth_level"):
            extra["auth_level"] = artifact.get("auth_level")
        if artifact.get("partial_auth"):
            extra["partial_auth"] = True
        metadata = getattr(result, "metadata", None) or {}
        if isinstance(metadata, dict):
            for key in (
                "chatgpt_rt_registration_two_stage",
                "registration_stage",
                "registration_stage_complete",
                "registration_access_token_saved",
                "registration_access_token_checkpoint_created",
                "registration_access_token_checkpoint_policy",
                "registration_access_token_partial_reason",
                "registration_session_account_id",
                "registration_session_workspace_id",
                "registration_stage1_saved_account_id",
                "auth_capture_stage",
                "auth_capture_method",
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
                "chatgpt_gopay_provider_link_enabled",
                "chatgpt_gopay_provider_link_ready",
                "chatgpt_gopay_provider_link",
                "chatgpt_gopay_provider_link_error",
                "chatgpt_gopay_provider_link_snapshot",
                "chatgpt_gopay_provider_link_checkout_url",
                "chatgpt_gopay_provider_link_cs_id",
                "chatgpt_gopay_provider_link_snap_token",
                "chatgpt_gopay_provider_link_stripe_redirect_url",
                "chatgpt_gopay_provider_link_midtrans_redirect_url",
                "chatgpt_gopay_provider_link_payment_method_types",
                "chatgpt_gopay_provider_link_phase",
                "cookies",
                "cookie_header",
                "registration_web_session_material_preserved",
            ):
                if key in metadata:
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
                extra["chatgpt_mailbox_state"] = mailbox_state
            if metadata.get("registration_context"):
                extra["chatgpt_registration_context"] = metadata.get("registration_context")
            if metadata.get("pending_business_invite"):
                extra["chatgpt_pending_business_invite"] = metadata.get("pending_business_invite")
            if metadata.get("needs_auth_capture"):
                extra["needs_auth_capture"] = True
                extra["auth_capture_required"] = True
            if metadata.get("registration_full_auth_failed"):
                extra["registration_full_auth_failed"] = True
                extra["registration_full_auth_error"] = metadata.get("registration_full_auth_error") or metadata.get(
                    "registration_access_token_partial_reason"
                ) or ""
                extra["registration_full_auth_failed_policy"] = metadata.get(
                    "registration_full_auth_failed_policy"
                ) or "keep_access_token_checkpoint"
            if metadata.get("chatgpt_phone_challenge"):
                extra["chatgpt_phone_challenge"] = metadata.get("chatgpt_phone_challenge")
            if metadata.get("chatgpt_phone_challenge_history"):
                extra["chatgpt_phone_challenge_history"] = metadata.get("chatgpt_phone_challenge_history")
            if metadata.get("chatgpt_bound_phone"):
                extra["chatgpt_bound_phone"] = metadata.get("chatgpt_bound_phone")
            if metadata.get("chatgpt_bound_phone_number"):
                extra["chatgpt_bound_phone_number"] = metadata.get("chatgpt_bound_phone_number")
            if metadata.get("chatgpt_bound_phone_masked"):
                extra["chatgpt_bound_phone_masked"] = metadata.get("chatgpt_bound_phone_masked")
            if metadata.get("deferred_activation"):
                deferred_status = str(
                    metadata.get("deferred_activation_status") or "invite_sent_pending_activation"
                )
                extra["chatgpt_deferred_activation"] = True
                extra["chatgpt_deferred_activation_status"] = deferred_status
                extra["chatgpt_workspace_scope"] = "pending_activation"
                extra["chatgpt_workspace_label"] = "pending_activation"
                extra["chatgpt_workspace_display_name"] = (
                    f"{email} [pending_activation]" if email else "[pending_activation]"
                )
        return extra

    def _build_workspace_accounts(self, result, fallback_password: str) -> list[Account]:
        artifacts = [
            item
            for item in (getattr(result, "workspace_artifacts", None) or [])
            if isinstance(item, dict)
        ]
        artifacts = self._dedupe_workspace_artifacts(artifacts)
        if not artifacts:
            artifacts = [{
                "scope": "business" if getattr(result, "source", "") == "business_recovery" else "free",
                "access_token": getattr(result, "access_token", ""),
                "refresh_token": getattr(result, "refresh_token", ""),
                "id_token": getattr(result, "id_token", ""),
                "session_token": getattr(result, "session_token", ""),
                "workspace_id": getattr(result, "workspace_id", ""),
                "account_id": getattr(result, "account_id", ""),
                "source": getattr(result, "source", "register"),
                "variant_key": f"{getattr(result, 'source', '') == 'business_recovery' and 'business' or 'free'}:{getattr(result, 'workspace_id', '') or getattr(result, 'account_id', '') or 'default'}",
            }]

        accounts: list[Account] = []
        for artifact in artifacts:
            extra = self._build_account_extra_for_artifact(artifact, result)
            if extra.get("chatgpt_payment_already_paid") or extra.get("chatgpt_account_unavailable"):
                status = AccountStatus.INVALID
            elif extra.get("partial_auth") or extra.get("auth_level") == "access_token_only":
                status = AccountStatus.PENDING_PAYMENT
            else:
                status = AccountStatus.REGISTERED
            accounts.append(
                Account(
                    platform="chatgpt",
                    email=getattr(result, "email", ""),
                    password=getattr(result, "password", "") or fallback_password,
                    user_id=str(artifact.get("account_id") or getattr(result, "account_id", "") or ""),
                    token=str(artifact.get("access_token") or getattr(result, "access_token", "") or ""),
                    status=status,
                    extra=extra,
                )
            )
        return accounts


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
        # team invite / deferred activation 还有自己的 business join 语义，暂时保留原专用链路，
        # 避免把“注册后入 team”误改成普通已有账号抓 auth。
        if self._parse_bool(extra.get("chatgpt_enable_team_invite"), default=False):
            return False
        return self._parse_bool(extra.get("chatgpt_rt_registration_two_stage_enabled"), default=True)

    @staticmethod
    def _checkpoint_variant_key(result) -> str:
        metadata = getattr(result, "metadata", None) or {}
        account_id = str(
            getattr(result, "account_id", "")
            or (metadata.get("registration_session_account_id") if isinstance(metadata, dict) else "")
            or ""
        ).strip()
        workspace_id = str(
            getattr(result, "workspace_id", "")
            or (metadata.get("registration_session_workspace_id") if isinstance(metadata, dict) else "")
            or ""
        ).strip()
        email = str(getattr(result, "email", "") or "").strip()
        return f"free:{account_id or workspace_id or email or 'unknown'}"

    def _ensure_access_token_checkpoint_artifact(self, result) -> dict:
        metadata = getattr(result, "metadata", None)
        if not isinstance(metadata, dict):
            metadata = {}
            result.metadata = metadata

        account_id = str(getattr(result, "account_id", "") or metadata.get("registration_session_account_id") or "").strip()
        workspace_id = str(getattr(result, "workspace_id", "") or metadata.get("registration_session_workspace_id") or account_id).strip()
        artifact = {
            "scope": "free",
            "label": "free",
            "account_id": account_id,
            "workspace_id": workspace_id,
            "access_token": str(getattr(result, "access_token", "") or "").strip(),
            "refresh_token": "",
            "id_token": str(getattr(result, "id_token", "") or "").strip(),
            "session_token": str(getattr(result, "session_token", "") or "").strip(),
            "source": "registration_session",
            "variant_key": self._checkpoint_variant_key(result),
            "auth_level": "access_token_only",
            "partial_auth": True,
        }
        result.source = "registration_session"
        result.workspace_artifacts = [artifact]
        return artifact

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
    def _finalize_original_email_success(email_service, *, account_email: str, task_id: str = "") -> None:
        finalize = getattr(email_service, "finalize_success", None)
        if not callable(finalize):
            return
        try:
            finalize(account_email=account_email, task_id=task_id)
        except Exception:
            pass

    @staticmethod
    def _align_free_artifacts_to_checkpoint_variant(result, checkpoint_variant_key: str) -> None:
        variant_key = str(checkpoint_variant_key or "").strip()
        if not variant_key:
            return
        artifacts = [
            item
            for item in (getattr(result, "workspace_artifacts", None) or [])
            if isinstance(item, dict)
        ]
        if artifacts:
            for artifact in artifacts:
                scope = BaseChatGPTRegistrationModeAdapter._normalize_workspace_scope(artifact.get("scope") or "") or "free"
                if scope == "free":
                    artifact["variant_key"] = variant_key
            return
        source = str(getattr(result, "source", "") or "")
        if source.startswith("workspace_capture_free") or source in {"register", "login", "registration_session"}:
            result.workspace_artifacts = [
                {
                    "scope": "free",
                    "label": "free",
                    "account_id": str(getattr(result, "account_id", "") or ""),
                    "workspace_id": str(getattr(result, "workspace_id", "") or ""),
                    "access_token": str(getattr(result, "access_token", "") or ""),
                    "refresh_token": str(getattr(result, "refresh_token", "") or ""),
                    "id_token": str(getattr(result, "id_token", "") or ""),
                    "session_token": str(getattr(result, "session_token", "") or ""),
                    "source": source or "workspace_capture_free",
                    "variant_key": variant_key,
                }
            ]

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
        stage1_artifacts = [
            item
            for item in (getattr(stage1_result, "workspace_artifacts", None) or [])
            if isinstance(item, dict)
        ]
        stage1_session_token = self._first_non_empty_string(
            getattr(stage1_result, "session_token", ""),
            *(item.get("session_token") for item in stage1_artifacts),
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

        artifacts = [
            item
            for item in (getattr(target_result, "workspace_artifacts", None) or [])
            if isinstance(item, dict)
        ]
        if stage1_session_token:
            for artifact in artifacts:
                if not self._first_non_empty_string(artifact.get("session_token")):
                    artifact["session_token"] = stage1_session_token
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
        checkpoint_variant_key: str,
        saved_stage1_id: int,
    ):
        from services.chatgpt_core.refresh_token_registration_engine import (
            EmailServiceAdapter,
            RefreshTokenRegistrationEngine,
            RegistrationResult,
        )
        from services.chatgpt_core.utils import generate_random_birthday, generate_random_name

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

        stage2_engine._log("[注册] 第二阶段：使用注册邮箱抓取 free Auth/RT")
        email_adapter = EmailServiceAdapter(stage2_engine.email_service, email, stage2_engine._log)
        first_name, last_name = generate_random_name()
        birthdate = generate_random_birthday()
        artifact = stage2_engine._capture_workspace_artifact_via_fresh_login(
            scope="free",
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
        if not artifact:
            error = str(
                getattr(stage2_engine, "_last_workspace_capture_error", "")
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
        if not stage2_engine._artifact_has_refresh_token(artifact):
            return RegistrationResult(
                success=False,
                email=email,
                password=password,
                error_message="注册第二阶段已拿到 OAuth access_token，但未返回 refresh_token",
                logs=list(getattr(stage2_engine, "logs", None) or []),
                metadata={},
            )

        artifact["scope"] = "free"
        artifact["label"] = "free"
        artifact["variant_key"] = checkpoint_variant_key
        result = RegistrationResult(
            success=True,
            email=email,
            password=password,
            logs=list(getattr(stage2_engine, "logs", None) or []),
            metadata={},
        )
        stage2_engine._apply_workspace_artifact_to_result(result, artifact)
        result.workspace_artifacts = [artifact]
        stage1_metadata = getattr(stage1_result, "metadata", None)
        stage1_metadata = stage1_metadata if isinstance(stage1_metadata, dict) else {}
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
            }
        }
        result.metadata.update(
            {
                "chatgpt_rt_registration_two_stage": True,
                "registration_stage": "full_auth_saved",
                "registration_stage_complete": True,
                "registration_access_token_saved": True,
                "registration_stage1_saved_account_id": saved_stage1_id,
                "auth_capture_stage": "success",
                "auth_capture_method": "registration_stage2_full_auth",
                "selected_workspace_scopes": ["free"],
                "workspace_capture_optional_failures": [],
                "workspace_capture_partial_success": False,
                "workspace_artifact_summaries": [
                    {
                        "scope": "free",
                        "label": "free",
                        "account_id": str(artifact.get("account_id") or ""),
                        "workspace_id": str(artifact.get("workspace_id") or ""),
                        "source": str(artifact.get("source") or ""),
                    }
                ],
            }
        )
        try:
            stage2_engine._append_gopay_provider_link_metadata(result, {})
        except Exception:
            pass
        return result

    def run(self, context: ChatGPTRegistrationContext):
        if not self._two_stage_enabled(context.extra_config):
            return super().run(context)
        return self._run_two_stage_registration(context)

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
                "chatgpt_access_token_only_gopay_provider_link_enabled": False,
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
        stage1_artifact = self._ensure_access_token_checkpoint_artifact(stage1_result)
        checkpoint_variant_key = str(stage1_artifact.get("variant_key") or self._checkpoint_variant_key(stage1_result))

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
        # 标准注册没有 team invite 时，第二阶段只补 free Auth/RT，避免 existing-account
        # 默认同时尝试 business，把第一阶段 free checkpoint 升级成另一条变体。
        if not self._parse_bool(stage2_extra.get("chatgpt_enable_team_invite"), default=False):
            stage2_extra["chatgpt_capture_free_workspace"] = True
            stage2_extra["chatgpt_capture_business_workspace"] = False

        stage2_result = self._capture_stage2_from_stage1_session(
            context=context,
            stage1_engine=stage1_engine,
            stage1_result=stage1_result,
            stage2_extra=stage2_extra,
            checkpoint_variant_key=checkpoint_variant_key,
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
            self._align_free_artifacts_to_checkpoint_variant(stage2_result, checkpoint_variant_key)
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
        stage1_result.metadata.setdefault("registration_stage1_saved_account_id", saved_stage1_id)
        self._ensure_access_token_checkpoint_artifact(stage1_result)
        return stage1_result

    def _create_engine(self, context: ChatGPTRegistrationContext):
        from services.chatgpt_core.refresh_token_registration_engine import RefreshTokenRegistrationEngine

        return RefreshTokenRegistrationEngine(
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
    mode = resolve_chatgpt_registration_mode(extra)
    if mode == CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY:
        return AccessTokenOnlyChatGPTRegistrationAdapter()
    return RefreshTokenChatGPTRegistrationAdapter()
