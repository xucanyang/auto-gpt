"""Bounded, provider-scoped ChatGPT mailbox recovery state.

Mailbox state is persisted once per ChatGPT account.  It must therefore contain
only data needed to reopen that account's mailbox.  In particular, callers must
never copy the global registration ``extra_config`` object into this payload.
"""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Mapping
from typing import Any


MAILBOX_STATE_SCHEMA_VERSION = 2
DEFAULT_MAX_BEFORE_IDS = 128
DEFAULT_MAX_BEFORE_IDS_BYTES = 16 * 1024
MAX_STATE_STRING_BYTES = 16 * 1024


_COMMON_CONFIG_KEYS = (
    "mailbox_proxy",
    "email_proxy",
    "mail_api_proxy",
    "mailbox_use_task_proxy",
)

_PROVIDER_CONFIG_KEYS: dict[str, tuple[str, ...]] = {
    "manual_email_otp": (
        "manual_email_address",
        "email",
        "manual_email_code",
        "tempmail_api_url",
        "tempmail_api_key",
        "tempmail_api_key_header",
        "tempmail_fixed_domains",
    ),
    "email_api": (
        "mail_provider",
        "email_api_poll_interval_seconds",
        "email_api_request_timeout_seconds",
        "email_api_gmail_dot_variant_enabled",
        "email_api_gmail_variant_count",
        "email_api_gmail_variant_rules",
        "email_api_gmail_plus_tag_template",
        "email_api_default_scheme",
        "email_api_proxy",
        "email_api_api_proxy",
        "email_api_use_task_proxy",
    ),
    "tempmail_local": (
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
        "tempmail_proxy",
        "tempmail_api_proxy",
        "tempmail_use_task_proxy",
    ),
    "hme_ready_api": (
        # These keys retain their historical storage names for compatibility
        # with the shared config store.  They are the HME Ready + TempMail
        # consumer contract, not a direct Apple/iCloud client contract.
        "icloud_hme_mode",
        "icloud_forward_to",
        "icloud_hme_helper_api_url",
        "icloud_hme_helper_internal_key",
        "icloud_hme_helper_api_key",
        "icloud_hme_helper_api_key_header",
        "icloud_hme_helper_header",
        "icloud_hme_helper_consumer",
        "icloud_hme_helper_checkout_ttl_seconds",
        "icloud_hme_helper_wait_timeout_seconds",
        "icloud_hme_helper_max_cache_age_seconds",
        "tempmail_api_url",
        "tempmail_api_key",
        "tempmail_api_key_header",
        "tempmail_wait_timeout_seconds",
        "tempmail_proxy",
        "tempmail_api_proxy",
        "tempmail_use_task_proxy",
    ),
    "skymail": ("skymail_api_base", "skymail_token", "skymail_domain"),
    "cloudmail": (
        "cloudmail_api_base",
        "cloudmail_admin_email",
        "cloudmail_admin_password",
        "cloudmail_domain",
        "cloudmail_subdomain",
        "cloudmail_timeout",
    ),
    "duckmail": (
        "duckmail_api_url",
        "duckmail_provider_url",
        "duckmail_bearer",
        "duckmail_domain",
        "duckmail_api_key",
    ),
    "freemail": (
        "freemail_api_url",
        "freemail_admin_token",
        "freemail_username",
        "freemail_password",
        "freemail_domain",
    ),
    "moemail": ("moemail_api_url", "moemail_api_key"),
    "maliapi": (
        "maliapi_base_url",
        "maliapi_api_key",
        "maliapi_domain",
        "maliapi_auto_domain_strategy",
    ),
    "gptmail": ("gptmail_base_url", "gptmail_api_key", "gptmail_domain"),
    "applemail": (
        "applemail_base_url",
        "applemail_pool_file",
        "applemail_pool_dir",
        "applemail_mailboxes",
    ),
    "opentrashmail": (
        "opentrashmail_api_url",
        "opentrashmail_domain",
        "opentrashmail_password",
    ),
    "cfworker": (
        "cfworker_api_url",
        "cfworker_admin_token",
        "cfworker_domain",
        "cfworker_domain_override",
        "cfworker_domains",
        "cfworker_enabled_domains",
        "cfworker_subdomain",
        "cfworker_random_subdomain",
        "cfworker_fingerprint",
        "cfworker_custom_auth",
    ),
    "luckmail": (
        "luckmail_base_url",
        "luckmail_api_key",
        "luckmail_project_code",
        "luckmail_email_type",
        "luckmail_domain",
    ),
    "outlook": (
        "outlook_imap_server",
        "outlook_imap_port",
        "outlook_token_endpoint",
    ),
    "laoudo": ("laoudo_auth", "laoudo_email", "laoudo_account_id"),
}

_PROVIDER_ALIASES = {
    "api_email": "email_api",
    "email_otp_api": "email_api",
    "mail_api_otp": "email_api",
    "tempmail_api": "tempmail_local",
    # HME Ready is the only supported HME provider.  The older values remain
    # readable so persisted account state can be upgraded without a data wipe.
    "hme_ready_api": "hme_ready_api",
    "helper_ready_api": "hme_ready_api",
    "icloud_hme": "hme_ready_api",
    "icloud_hme_ready": "hme_ready_api",
    "icloud_hme_helper_ready": "hme_ready_api",
}

_COMMON_ACCOUNT_EXTRA_KEYS = {
    "provider",
    "mailbox_action",
}

_PROVIDER_ACCOUNT_EXTRA_KEYS: dict[str, set[str]] = {
    "manual_email_otp": {
        "mailbox",
    },
    "email_api": {
        "api_url",
        "api_url_masked",
        "source_email",
        "gmail_root",
        "variant",
        "line",
        "warnings",
    },
    "tempmail_local": {
        "mailbox",
        "lease",
        "task_key",
        "tempmail_mode",
    },
    "hme_ready_api": {
        "mode",
        "source",
        "anonymous_id",
        "hme",
        # Helper platform-registration identity.  These fields are bounded
        # account metadata, not secrets; keeping them here makes a restored
        # ChatGPT mailbox addressable without reconstructing a tag or slot.
        "platform",
        "registration_platform",
        "registration_id",
        "logical_address_id",
        "physical_alias_id",
        "lease_id",
        "checkout_id",
        "lease_state",
        "physical_hme",
        "logical_type",
        "tag",
        "tag_namespace",
        "tag_slot",
        "external_account_ref",
        "mailbox_id",
        "service_id",
        "forward_to",
        "forward_mailbox_id",
        "alias_id",
        "alias_key",
        "helper_account_id",
    },
    "applemail": {
        "client_id",
        "refresh_token",
        "mailbox",
    },
    "maliapi": {
        "temp_token",
        "inbox_id",
    },
    "gptmail": {
        "domain",
        "local_address",
    },
    "opentrashmail": {
        "domain",
        "local_address",
    },
    "cfworker": {
        "cfworker_domain",
    },
    "luckmail": {
        "token",
        "project_code",
    },
    "outlook": {
        "password",
        "client_id",
        "refresh_token",
    },
}

_MAILBOX_NESTED_KEYS = {
    "id",
    "mailbox_id",
    "full_address",
    "email",
    "address",
    "domain",
    "domain_name",
    "domain_value",
    "name",
    "anonymous_id",
    "status",
    "permanent",
    "created_at",
    "updated_at",
    "expires_at",
}


def _bounded_string(value: Any, *, max_bytes: int = MAX_STATE_STRING_BYTES) -> str:
    text = str(value or "")
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    return raw[:max_bytes].decode("utf-8", "ignore")


def _bounded_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _bounded_string(value)
    return None


def _bounded_simple_value(value: Any) -> Any:
    scalar = _bounded_scalar(value)
    if scalar is not None or value is None:
        return scalar
    if isinstance(value, (list, tuple, set)):
        result = []
        for item in list(value)[:128]:
            item_value = _bounded_scalar(item)
            if item_value is not None:
                result.append(item_value)
        return result
    return None


def normalize_mailbox_provider(value: Any) -> str:
    provider = str(value or "").strip().lower()
    return _PROVIDER_ALIASES.get(provider, provider)


def export_mailbox_state_config(provider: Any, config: Mapping[str, Any] | Any) -> dict[str, Any]:
    """Return only explicitly approved provider configuration keys."""

    source = config if isinstance(config, Mapping) else {}
    raw_provider = str(provider or "").strip().lower()
    normalized_provider = normalize_mailbox_provider(raw_provider)
    keys = (*_COMMON_CONFIG_KEYS, *_PROVIDER_CONFIG_KEYS.get(normalized_provider, ()))
    exported: dict[str, Any] = {}
    for key in keys:
        if key not in source:
            continue
        value = _bounded_simple_value(source.get(key))
        if value is not None:
            exported[key] = value
    if normalized_provider == "hme_ready_api" and raw_provider in {
        "hme_ready_api",
        "helper_ready_api",
        "icloud_hme",
        "icloud_hme_ready",
        "icloud_hme_helper_ready",
    }:
        # Helper checkout/recovery never talks to Apple directly.  Retaining
        # the global iCloud cookie here both leaks a credential into every
        # account and recreates the exact per-account-copy failure this module
        # exists to prevent.
        exported.pop("icloud_cookie", None)
        exported.pop("icloud_domain_base", None)
        exported.pop("icloud_forward_mailbox_id", None)
        exported["icloud_hme_mode"] = "helper_ready_api"
    return exported


def export_mailbox_account_extra(
    extra: Mapping[str, Any] | Any,
    *,
    provider: Any = "",
) -> dict[str, Any]:
    """Bound account-specific mailbox metadata without retaining provider dumps."""

    source = extra if isinstance(extra, Mapping) else {}
    normalized_provider = normalize_mailbox_provider(provider or source.get("provider"))
    keys = _COMMON_ACCOUNT_EXTRA_KEYS | _PROVIDER_ACCOUNT_EXTRA_KEYS.get(normalized_provider, set())

    # A few early helper states put forward_to below account.extra.account.extra.
    # Flatten only this documented compatibility shape before applying the
    # provider whitelist; never retain the nested object itself.
    source = dict(source)
    nested_account = source.get("account")
    nested_extra = nested_account.get("extra") if isinstance(nested_account, Mapping) else {}
    if normalized_provider == "hme_ready_api" and isinstance(nested_extra, Mapping):
        for key in (
            "forward_to",
            "forward_mailbox_id",
            "lease_id",
            "checkout_id",
            "registration_id",
            "logical_address_id",
            "physical_alias_id",
            "platform",
            "registration_platform",
            "lease_state",
            "physical_hme",
            "logical_type",
            "tag",
            "tag_namespace",
            "tag_slot",
        ):
            if source.get(key) in (None, "") and nested_extra.get(key) not in (None, ""):
                source[key] = nested_extra.get(key)

    exported: dict[str, Any] = {}
    for key in keys:
        if key not in source:
            continue
        value = source.get(key)
        if key == "mailbox" and isinstance(value, Mapping):
            nested = {
                nested_key: nested_value
                for nested_key in _MAILBOX_NESTED_KEYS
                if nested_key in value
                and (nested_value := _bounded_simple_value(value.get(nested_key))) is not None
            }
            if nested:
                exported[key] = nested
            continue
        if key == "provider":
            value = normalized_provider
        bounded = _bounded_simple_value(value)
        if bounded is not None:
            exported[key] = bounded
    return exported


def bound_before_ids(
    values: Any,
    *,
    max_items: int = DEFAULT_MAX_BEFORE_IDS,
    max_bytes: int = DEFAULT_MAX_BEFORE_IDS_BYTES,
) -> list[str]:
    """Return a deterministic, de-duplicated and byte-bounded baseline list."""

    if not isinstance(values, (list, tuple, set, frozenset)):
        return []
    max_items = max(int(max_items or 0), 0)
    max_bytes = max(int(max_bytes or 0), 0)
    if max_items <= 0 or max_bytes <= 2:
        return []
    # A list/tuple may carry the provider's chronological order, so preserve
    # it.  A set has no chronology and falls back to deterministic order only
    # for repeatability.  Restored providers refresh the baseline from the live
    # mailbox before waiting whenever their API supports it.
    raw_values = sorted(values, key=lambda item: str(item or "")) if isinstance(values, (set, frozenset)) else values
    normalized: list[str] = []
    seen: set[str] = set()
    for item in raw_values:
        value = _bounded_string(item, max_bytes=1024).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    candidates = normalized[-max_items:]
    result: list[str] = []
    used = 2  # JSON []
    for item in candidates:
        encoded = json.dumps(item, ensure_ascii=False).encode("utf-8")
        # AccountModel.set_extra/json.dumps uses the default `, ` separator.
        extra_bytes = len(encoded) + (2 if result else 0)
        if used + extra_bytes > max_bytes:
            continue
        result.append(item)
        used += extra_bytes
    return result


def sanitize_mailbox_state(
    state: Mapping[str, Any] | Any,
    *,
    account_email: str = "",
    max_before_ids: int = DEFAULT_MAX_BEFORE_IDS,
    max_before_ids_bytes: int = DEFAULT_MAX_BEFORE_IDS_BYTES,
) -> dict[str, Any]:
    """Normalize legacy/current state into the bounded v2 recovery contract."""

    if not isinstance(state, Mapping):
        return {}
    provider_raw = str(state.get("provider") or "").strip().lower()
    if not provider_raw:
        return {}

    account_raw = state.get("account") if isinstance(state.get("account"), Mapping) else {}
    raw_account_extra = account_raw.get("extra") if isinstance(account_raw.get("extra"), Mapping) else {}
    email = str(
        state.get("email")
        or account_raw.get("email")
        or account_email
        or ""
    ).strip()
    account_id = _bounded_string(account_raw.get("account_id") or "", max_bytes=4096).strip()
    account_extra = export_mailbox_account_extra(raw_account_extra, provider=provider_raw)
    normalized_provider = normalize_mailbox_provider(provider_raw)
    explicit_hme_lease = bool(
        raw_account_extra.get("lease_id")
        or raw_account_extra.get("checkout_id")
    )
    # A historical `icloud_hme` row may carry `mode=helper_ready_api` even
    # though its account_id is the old Apple anonymous ID.  Never promote that
    # implicit ID into a current Helper lease; only explicit lease metadata is
    # authoritative for the legacy provider name.
    is_legacy_direct_hme = bool(
        normalized_provider == "hme_ready_api"
        and provider_raw == "icloud_hme"
        and not explicit_hme_lease
    )
    if not account_id and normalized_provider == "hme_ready_api":
        for key in ("lease_id", "checkout_id", "mailbox_id", "service_id", "anonymous_id"):
            candidate = _bounded_string(account_extra.get(key) or "", max_bytes=4096).strip()
            if candidate:
                account_id = candidate
                break

    state_config = export_mailbox_state_config(provider_raw, state.get("config"))
    if normalized_provider == "hme_ready_api":
        state_config.pop("icloud_cookie", None)
        state_config["icloud_hme_mode"] = "helper_ready_api"
        # auto-gpt only consumes ChatGPT registrations.  Old Helper snapshots
        # lack this field, so default only inside the helper-HME scope and keep
        # the original provider-independent state contract intact.
        account_extra.setdefault("platform", "chatgpt")
        account_extra.setdefault("registration_platform", "chatgpt")
        account_extra["platform"] = str(account_extra.get("platform") or "chatgpt").strip().lower() or "chatgpt"
        account_extra["registration_platform"] = (
            str(account_extra.get("registration_platform") or account_extra["platform"] or "chatgpt")
            .strip()
            .lower()
            or "chatgpt"
        )
        if is_legacy_direct_hme:
            account_extra.setdefault("source", "legacy-icloud-hme")
            if account_id:
                account_extra.setdefault("anonymous_id", account_id)
        for key in (
            "platform",
            "registration_platform",
            "lease_state",
            "logical_type",
            "tag",
            "tag_namespace",
        ):
            value = account_extra.get(key)
            if isinstance(value, str):
                account_extra[key] = unicodedata.normalize("NFKC", value).strip().lower()

    before_ids = bound_before_ids(
        state.get("before_ids"),
        max_items=max_before_ids,
        max_bytes=max_before_ids_bytes,
    )
    original_before_count = (
        len({str(item or "").strip() for item in state.get("before_ids") or [] if str(item or "").strip()})
        if isinstance(state.get("before_ids"), (list, tuple, set, frozenset))
        else 0
    )
    result: dict[str, Any] = {
        "schema_version": MAILBOX_STATE_SCHEMA_VERSION,
        "provider": normalized_provider,
        "email": _bounded_string(email, max_bytes=4096),
        "account": {
            "email": _bounded_string(str(account_raw.get("email") or email or ""), max_bytes=4096),
            "account_id": account_id,
            "extra": account_extra,
        },
        "before_ids": before_ids,
        "config": state_config,
    }
    if bool(state.get("before_ids_truncated")) or original_before_count > len(before_ids):
        result["before_ids_truncated"] = True
    proxy = state.get("proxy")
    if proxy not in (None, ""):
        result["proxy"] = _bounded_string(proxy, max_bytes=4096)

    # Preserve small provenance/compatibility markers used by recovery flows.
    for key in (
        "recovered_from_alias",
        "recovered_from_domain_match",
        "recovered_from_account_config",
        "config_refreshed_from_current",
    ):
        if key in state:
            result[key] = bool(state.get(key))
    return result


def build_mailbox_state(
    *,
    provider: str,
    email: str,
    account_email: str,
    account_id: str,
    account_extra: Mapping[str, Any] | Any,
    before_ids: Any,
    config: Mapping[str, Any] | Any,
    proxy: Any = "",
) -> dict[str, Any]:
    """Build a state payload and apply the same contract used by migrations."""

    return sanitize_mailbox_state(
        {
            "provider": provider,
            "email": email,
            "account": {
                "email": account_email,
                "account_id": account_id,
                "extra": account_extra,
            },
            "before_ids": before_ids,
            "config": config,
            "proxy": proxy,
        },
        account_email=email or account_email,
    )


def mailbox_state_summary(
    state: Mapping[str, Any] | Any,
    *,
    account_email: str = "",
) -> dict[str, Any]:
    """Small audit marker for result payloads; never a second recovery copy."""

    cleaned = sanitize_mailbox_state(state, account_email=account_email)
    if not cleaned:
        return {}
    return {
        "has_mailbox_state": True,
        "schema_version": int(cleaned.get("schema_version") or MAILBOX_STATE_SCHEMA_VERSION),
        "provider": str(cleaned.get("provider") or ""),
        "email": str(cleaned.get("email") or account_email or ""),
        "before_count": len(cleaned.get("before_ids") or []),
    }


__all__ = [
    "DEFAULT_MAX_BEFORE_IDS",
    "DEFAULT_MAX_BEFORE_IDS_BYTES",
    "MAILBOX_STATE_SCHEMA_VERSION",
    "bound_before_ids",
    "build_mailbox_state",
    "export_mailbox_account_extra",
    "export_mailbox_state_config",
    "mailbox_state_summary",
    "normalize_mailbox_provider",
    "sanitize_mailbox_state",
]
