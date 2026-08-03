"""Restore an account's mailbox channel for normal auth recovery flows."""

from __future__ import annotations

from typing import Any, Callable

from sqlmodel import Session, select

from core.base_mailbox import MailboxAccount, create_mailbox
from core.config_store import config_store
from core.db import AccountModel, IcloudHmeAliasModel, engine

from .mailbox_state import normalize_mailbox_provider, sanitize_mailbox_state


TEMPMAIL_CONFIG_KEYS = (
    "tempmail_api_url",
    "tempmail_api_key",
    "tempmail_api_key_header",
    "tempmail_primary_domain",
    "tempmail_fixed_domains",
    "tempmail_mode",
    "tempmail_wait_timeout_seconds",
    "tempmail_ttl_minutes",
    "tempmail_reuse_window_minutes",
    "tempmail_permanent",
    "tempmail_platform",
)
HME_READY_CONFIG_KEYS = (
    "icloud_hme_mode",
    "icloud_forward_to",
    "icloud_hme_helper_api_url",
    "icloud_hme_helper_internal_key",
    "icloud_hme_helper_api_key_header",
    "icloud_hme_helper_consumer",
    "icloud_hme_helper_checkout_ttl_seconds",
    "icloud_hme_helper_wait_timeout_seconds",
    "icloud_hme_helper_max_cache_age_seconds",
    "tempmail_api_url",
    "tempmail_api_key",
    "tempmail_api_key_header",
    "tempmail_wait_timeout_seconds",
)
HME_READY_PROVIDERS = {
    "hme_ready_api",
    "helper_ready_api",
    # Read-only aliases for account rows written before the provider cutover.
    "icloud_hme",
    "icloud_hme_ready",
    "icloud_hme_helper_ready",
}


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _current_config(keys: tuple[str, ...]) -> dict[str, Any]:
    try:
        values = config_store.get_all() or {}
    except Exception:
        values = {}
    return {key: values[key] for key in keys if _has_value(values.get(key))}


def _with_current_mailbox_config(raw_state: dict[str, Any]) -> dict[str, Any]:
    state = sanitize_mailbox_state(raw_state)
    if not state:
        return {}
    provider = str(state.get("provider") or "").strip()
    provider = normalize_mailbox_provider(provider)
    if provider not in {"tempmail_local", "tempmail_api", *HME_READY_PROVIDERS}:
        return state

    keys = HME_READY_CONFIG_KEYS if provider in HME_READY_PROVIDERS else TEMPMAIL_CONFIG_KEYS
    current = _current_config(keys)
    if not current:
        return state

    config = dict(state.get("config") or {})
    config.update(current)
    if provider == "hme_ready_api":
        config["icloud_hme_mode"] = "helper_ready_api"

    state["config"] = config

    account = dict(state.get("account") or {})
    account_extra = dict(account.get("extra") or {})
    if provider == "hme_ready_api":
        # The global list is only a fallback for legacy rows which have no
        # account-level routing metadata.  Never overwrite a concrete target
        # returned by HME Ready with the global candidate list.
        if not _has_value(account_extra.get("forward_to")) and _has_value(config.get("icloud_forward_to")):
            account_extra["forward_to"] = str(config.get("icloud_forward_to") or "").strip()
        # `icloud_forward_mailbox_id` is a removed global hard pointer.  A
        # per-account cached id may remain and is validated/refreshed by the
        # TempMail reader instead of being replaced from global config.
        config.pop("icloud_forward_mailbox_id", None)
    if account_extra:
        account["extra"] = account_extra
    if not str(account.get("email") or "").strip() and str(state.get("email") or "").strip():
        account["email"] = str(state.get("email") or "").strip()
    if account:
        state["account"] = account
    state["config_refreshed_from_current"] = True
    return sanitize_mailbox_state(state)


def mailbox_state_from_account(
    account: AccountModel,
    *,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    account_extra = dict(extra or account.get_extra() or {})
    persisted = dict(
        account_extra.get("chatgpt_mailbox_state")
        or account_extra.get("mailbox_state")
        or {}
    )
    if persisted:
        return _with_current_mailbox_config(persisted)

    provider = str(
        account_extra.get("mail_provider")
        or account_extra.get("email_service")
        or account_extra.get("mailbox_provider")
        or ""
    ).strip()
    email = str(getattr(account, "email", "") or account_extra.get("email") or "").strip()
    if not email:
        return {}

    provider = normalize_mailbox_provider(provider)
    if provider == "hme_ready_api":
        lease_id = str(
            account_extra.get("lease_id")
            or account_extra.get("checkout_id")
            or ""
        ).strip()
        anonymous_id = str(
            account_extra.get("anonymous_id")
            or account_extra.get("mailbox_id")
            or account_extra.get("service_id")
            or ""
        ).strip()
        if not anonymous_id:
            try:
                with Session(engine) as session:
                    alias = session.exec(
                        select(IcloudHmeAliasModel)
                        .where(IcloudHmeAliasModel.hme == email)
                        .where(IcloudHmeAliasModel.bound_service == "chatgpt")
                    ).first()
                    if alias is not None:
                        anonymous_id = str(alias.anonymous_id or "").strip()
            except Exception:
                anonymous_id = ""

        current = _current_config(HME_READY_CONFIG_KEYS)
        mailbox_config = {
            key: account_extra.get(key, current.get(key))
            for key in HME_READY_CONFIG_KEYS
            if _has_value(account_extra.get(key, current.get(key)))
        }
        mailbox_config.setdefault("icloud_forward_to", "b@cccy.me")
        mailbox_config.setdefault("icloud_hme_mode", "helper_ready_api")
        if (
            not _has_value(mailbox_config.get("icloud_hme_helper_api_url"))
            or not _has_value(
                mailbox_config.get("icloud_hme_helper_internal_key")
                or mailbox_config.get("icloud_hme_helper_api_key")
            )
            or not _has_value(mailbox_config.get("tempmail_api_url"))
            or not _has_value(mailbox_config.get("tempmail_api_key"))
        ):
            return {}
        account_forward_to = str(
            account_extra.get("forward_to")
            or mailbox_config.get("icloud_forward_to")
            or ""
        ).strip()
        hme_extra = {
            "provider": "hme_ready_api",
            "platform": "chatgpt",
            "registration_platform": "chatgpt",
            "forward_to": account_forward_to,
            "forward_mailbox_id": str(account_extra.get("forward_mailbox_id") or "").strip(),
        }
        if anonymous_id:
            hme_extra["anonymous_id"] = anonymous_id
        if not lease_id:
            hme_extra["source"] = "legacy-icloud-hme"
        for key in (
            "registration_id",
            "logical_address_id",
            "physical_alias_id",
            "lease_id",
            "platform",
            "registration_platform",
            "lease_state",
            "physical_hme",
            "address_mode",
            "logical_type",
            "tag",
            "tag_namespace",
            "tag_slot",
            "external_account_ref",
        ):
            if account_extra.get(key) not in (None, ""):
                hme_extra[key] = account_extra[key]
        return sanitize_mailbox_state(
            {
                "provider": "hme_ready_api",
                "email": email,
                "account": {
                    "email": email,
                    "account_id": lease_id or anonymous_id,
                    "extra": hme_extra,
                },
                "before_ids": [],
                "config": mailbox_config,
                "proxy": str(account_extra.get("proxy") or account_extra.get("proxy_url") or "").strip(),
                "recovered_from_account_config": True,
            },
            account_email=email,
        )

    if provider in {"email_api", "api_email", "email_otp_api", "mail_api_otp"}:
        raw_url = str(
            account_extra.get("email_api_url")
            or account_extra.get("api_url")
            or account_extra.get("mail_api_url")
            or ""
        ).strip()
        if not raw_url:
            return {}
        try:
            from core.base_mailbox import normalize_email_api_url

            api_url = normalize_email_api_url(raw_url)
        except Exception:
            return {}
        return sanitize_mailbox_state(
            {
                "provider": "email_api",
                "email": email,
                "account": {
                    "email": email,
                    "account_id": email,
                    "extra": {
                        "provider": "email_api",
                        "api_url": api_url,
                        "source_email": str(account_extra.get("source_email") or email),
                        "variant": str(account_extra.get("variant") or "restored"),
                    },
                },
                "before_ids": [],
                "config": {
                    "mail_provider": "email_api",
                    "email_api_poll_interval_seconds": account_extra.get("email_api_poll_interval_seconds", 3),
                    "email_api_request_timeout_seconds": account_extra.get("email_api_request_timeout_seconds", 15),
                    "email_api_gmail_dot_variant_enabled": account_extra.get(
                        "email_api_gmail_dot_variant_enabled", True
                    ),
                },
                "proxy": str(
                    account_extra.get("email_api_proxy")
                    or account_extra.get("proxy")
                    or account_extra.get("proxy_url")
                    or ""
                ).strip(),
                "recovered_from_account_config": True,
            },
            account_email=email,
        )

    if provider not in {"tempmail_local", "tempmail_api"}:
        return {}
    current = _current_config(TEMPMAIL_CONFIG_KEYS)
    mailbox_config = {
        key: account_extra.get(key, current.get(key))
        for key in TEMPMAIL_CONFIG_KEYS
        if _has_value(account_extra.get(key, current.get(key)))
    }
    if not _has_value(mailbox_config.get("tempmail_api_url")) or not _has_value(
        mailbox_config.get("tempmail_api_key")
    ):
        return {}
    return sanitize_mailbox_state(
        {
            "provider": provider,
            "email": email,
            "account": {
                "email": email,
                "account_id": str(
                    account_extra.get("mailbox_id") or account_extra.get("service_id") or ""
                ).strip(),
                "extra": {},
            },
            "before_ids": [],
            "config": mailbox_config,
            "proxy": str(account_extra.get("proxy") or account_extra.get("proxy_url") or "").strip(),
            "recovered_from_account_config": True,
        },
        account_email=email,
    )


class RestoredEmailService:
    def __init__(
        self,
        *,
        state: dict[str, Any],
        proxy: str | None = None,
        log_fn: Callable[[str, str], None] | None = None,
        task_control: Any | None = None,
        attempt_id: int | None = None,
    ):
        self._state = _with_current_mailbox_config(dict(state or {}))
        self._provider = str(self._state.get("provider") or "").strip()
        if not self._provider:
            raise ValueError("mailbox_state.provider is required")
        self._config = dict(self._state.get("config") or {})
        self._proxy = proxy if proxy is not None else self._state.get("proxy")
        self._log_fn = log_fn
        self._mailbox = create_mailbox(self._provider, extra=self._config, proxy=self._proxy)
        setattr(self._mailbox, "_log_fn", lambda message: self._log(str(message)))
        setattr(self._mailbox, "_task_control", task_control)
        setattr(self._mailbox, "_task_attempt_token", attempt_id)
        account_payload = dict(self._state.get("account") or {})
        self._acct = MailboxAccount(
            email=str(account_payload.get("email") or self._state.get("email") or "").strip(),
            account_id=str(account_payload.get("account_id") or "").strip(),
            extra=dict(account_payload.get("extra") or {}),
        )
        if self._provider in HME_READY_PROVIDERS:
            account_extra = dict(self._acct.extra or {})
            account_extra.setdefault("platform", "chatgpt")
            account_extra.setdefault("registration_platform", "chatgpt")
            legacy_source = str(account_extra.get("source") or "").strip().lower() in {
                "legacy-icloud-hme",
                "icloud-hme-legacy",
            }
            if (
                not legacy_source
                and not str(account_extra.get("anonymous_id") or "").strip()
                and not str(account_extra.get("lease_id") or "").strip()
                and self._acct.account_id
            ):
                account_extra["lease_id"] = self._acct.account_id
            self._acct.extra = account_extra
        self._email = self._acct.email
        self._before_ids = set(self._state.get("before_ids") or [])
        self._last_verification_result: dict[str, Any] = {}
        self.service_type = type("RestoredServiceType", (), {"value": self._provider})()

    def _log(self, message: str, level: str = "info") -> None:
        if not callable(self._log_fn):
            return
        try:
            self._log_fn(message, level)
        except TypeError:
            self._log_fn(message)

    def _ensure_restored_tempmail_account(self) -> None:
        if self._provider not in {"tempmail_local", "tempmail_api"}:
            return
        email = str(self._acct.email or self._email or self._state.get("email") or "").strip()
        ensure = getattr(self._mailbox, "ensure_mailbox_by_email", None)
        if not email or not callable(ensure):
            return
        previous_id = str(self._acct.account_id or "").strip()
        account = ensure(email)
        if not account or not str(getattr(account, "account_id", "") or "").strip():
            raise RuntimeError(f"TempMail mailbox is unavailable: {email}")
        self._acct = account
        self._email = str(getattr(account, "email", "") or email).strip()
        action = str((getattr(account, "extra", None) or {}).get("mailbox_action") or "")
        current_id = str(getattr(account, "account_id", "") or "").strip()
        if action == "created_exact_address":
            self._log(f"[mailbox] recreated expired TempMail address: {self._email}")
        elif previous_id and current_id and previous_id != current_id:
            self._log(f"[mailbox] refreshed TempMail mailbox id: {self._email}")

    def create_email(self, config=None) -> dict[str, str]:
        if not self._acct or not str(self._acct.email or "").strip():
            raise RuntimeError("restored mailbox state has no email")
        self._ensure_restored_tempmail_account()
        get_current_ids = getattr(self._mailbox, "get_current_ids", None)
        if callable(get_current_ids):
            try:
                self._before_ids = set(get_current_ids(self._acct) or self._before_ids or [])
            except Exception:
                self._before_ids = set(self._before_ids or [])
        self._email = str(self._acct.email or "").strip()
        action = str((self._acct.extra or {}).get("mailbox_action") or "restored_existing")
        return {
            "email": self._email,
            "service_id": str(self._acct.account_id or ""),
            "token": str(self._acct.account_id or ""),
            "mailbox_action": action,
        }

    def get_verification_code(
        self,
        email=None,
        email_id=None,
        timeout=120,
        pattern=None,
        otp_sent_at=None,
        exclude_codes=None,
        phase=None,
        phase_label=None,
    ):
        code = self._mailbox.wait_for_code(
            self._acct,
            keyword="",
            timeout=int(timeout or 120),
            before_ids=self._before_ids,
            otp_sent_at=otp_sent_at,
            exclude_codes=exclude_codes,
            phase=phase,
            phase_label=phase_label,
        )
        self._last_verification_result = dict(
            getattr(self._mailbox, "_last_verification_result", None) or {}
        )
        return code

    def mark_verification_message_processed(self, message_id: str | None) -> None:
        normalized = str(message_id or "").strip()
        if normalized:
            self._before_ids.add(normalized)

    def export_state(self) -> dict[str, Any]:
        return sanitize_mailbox_state(
            {
                **dict(self._state),
                "provider": self._provider,
                "email": str(self._email or self._acct.email or "").strip(),
                "account": {
                    "email": str(self._acct.email or self._email or "").strip(),
                    "account_id": str(self._acct.account_id or ""),
                    "extra": self._acct.extra or {},
                },
                "before_ids": sorted(self._before_ids),
                "config": dict(self._config or {}),
                "proxy": self._proxy,
            }
        )

    def finalize_success(self, account_email: str = "", task_id: str = "") -> None:
        finalize = getattr(self._mailbox, "finalize_success", None)
        if callable(finalize):
            finalize(
                self._acct,
                registered_email=str(account_email or self._email or "").strip(),
                task_id=str(task_id or "").strip(),
            )
        self._state = self.export_state()

    def finalize_failure(self, error_message: str = "", task_id: str = "") -> None:
        finalize = getattr(self._mailbox, "finalize_failure", None)
        if callable(finalize):
            finalize(
                self._acct,
                error_message=str(error_message or "").strip(),
                task_id=str(task_id or "").strip(),
            )
        self._state = self.export_state()


__all__ = ["RestoredEmailService", "mailbox_state_from_account"]
