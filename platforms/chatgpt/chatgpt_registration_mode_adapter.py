"""ChatGPT 注册模式适配器。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Optional

from core.base_platform import Account, AccountStatus
from services.chatgpt_core.mailbox_state import sanitize_mailbox_state

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
            if metadata.get("mailbox_state"):
                cleaned_mailbox_state = sanitize_mailbox_state(
                    metadata.get("mailbox_state"),
                    account_email=str(getattr(result, "email", "") or ""),
                )
                if cleaned_mailbox_state:
                    extra["chatgpt_mailbox_state"] = cleaned_mailbox_state
            if metadata.get("registration_context"):
                extra["chatgpt_registration_context"] = metadata.get("registration_context")
            if metadata.get("pending_business_invite"):
                extra["chatgpt_pending_business_invite"] = metadata.get("pending_business_invite")
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
            status = (
                AccountStatus.PENDING_PAYMENT
                if extra.get("partial_auth") or extra.get("auth_level") == "access_token_only"
                else AccountStatus.REGISTERED
            )
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

    def _create_engine(self, context: ChatGPTRegistrationContext):
        from platforms.chatgpt.refresh_token_registration_engine import RefreshTokenRegistrationEngine

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
        from platforms.chatgpt.access_token_only_registration_engine import AccessTokenOnlyRegistrationEngine

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
