from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from sqlmodel import select

from core.db import AccountModel
from services.chatgpt_account_state import AUTH_INVALID_STATES, classify_chatgpt_capabilities


def _safe_str(value: Any) -> str:
    return str(value or "").strip()


def _lower_text(value: Any) -> str:
    return _safe_str(value).lower()


def _parse_subscription_time(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
        if numeric <= 0:
            return None
        return numeric / 1000 if numeric > 1_000_000_000_000 else numeric

    text = _safe_str(value)
    if not text:
        return None
    try:
        numeric = float(text)
    except ValueError:
        numeric = None
    if numeric is not None:
        if numeric <= 0:
            return None
        return numeric / 1000 if numeric > 1_000_000_000_000 else numeric

    iso_text = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(iso_text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _split_values(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, (list, tuple, set)):
        items: list[str] = []
        for item in value:
            items.extend(_split_values(item))
        return {item for item in items if item}
    return {item.strip().lower() for item in str(value).split(",") if item.strip()}


def account_base_query(*, platform: str | None = None, status: Any = None, email: str | None = None):
    query = select(AccountModel)
    platform_value = _safe_str(platform)
    if platform_value:
        query = query.where(AccountModel.platform == platform_value)

    status_values = _split_values(status)
    if len(status_values) == 1:
        query = query.where(AccountModel.status == next(iter(status_values)))
    elif len(status_values) > 1:
        query = query.where(AccountModel.status.in_(sorted(status_values)))

    email_value = _safe_str(email)
    if email_value:
        query = query.where(AccountModel.email.contains(email_value))
    return query


def _extra(account: AccountModel) -> dict[str, Any]:
    try:
        extra = account.get_extra()
    except Exception:
        return {}
    return extra if isinstance(extra, dict) else {}


def _chatgpt_capabilities(account: AccountModel, extra: dict[str, Any]) -> dict[str, Any]:
    capabilities = extra.get("chatgpt_capabilities") if isinstance(extra.get("chatgpt_capabilities"), dict) else {}
    if capabilities:
        return capabilities
    local_probe = extra.get("chatgpt_local") if isinstance(extra.get("chatgpt_local"), dict) else None
    return classify_chatgpt_capabilities(account, local_probe=local_probe)


def _has_refresh_token(account: AccountModel, extra: dict[str, Any]) -> bool:
    return bool(_safe_str(extra.get("refresh_token") or getattr(account, "refresh_token", "")))


def _has_access_token(account: AccountModel, extra: dict[str, Any]) -> bool:
    return bool(_safe_str(extra.get("access_token") or getattr(account, "access_token", "") or account.token))


def account_auth_type(account: AccountModel, extra: dict[str, Any] | None = None) -> str:
    extra = extra if isinstance(extra, dict) else _extra(account)
    if _has_refresh_token(account, extra):
        return "refresh_token"
    if _has_access_token(account, extra):
        return "access_token_only"
    return "unknown"


def _normalize_subscription_type(plan: Any, workspace_scope: Any = "") -> str:
    normalized = _lower_text(plan).replace("-", "_")
    scope = _lower_text(workspace_scope)
    if "enterprise" in normalized:
        return "enterprise"
    if "team" in normalized or "business" in normalized or scope == "business":
        return "team"
    if "pro" in normalized:
        return "pro"
    if "plus" in normalized:
        return "plus"
    if "free" in normalized or scope == "free":
        return "free"
    return "unknown"


def account_subscription_type(account: AccountModel, extra: dict[str, Any] | None = None) -> str:
    extra = extra if isinstance(extra, dict) else _extra(account)
    capabilities = _chatgpt_capabilities(account, extra)
    local_probe = extra.get("chatgpt_local") if isinstance(extra.get("chatgpt_local"), dict) else {}
    subscription = local_probe.get("subscription") if isinstance(local_probe.get("subscription"), dict) else {}
    workspace_scope = extra.get("chatgpt_workspace_scope")
    for candidate in (
        capabilities.get("subscription_plan"),
        subscription.get("plan"),
        extra.get("chatgpt_plan_type"),
        extra.get("chatgpt_subscription_plan"),
    ):
        resolved = _normalize_subscription_type(candidate, workspace_scope)
        if resolved != "unknown":
            return resolved
    return _normalize_subscription_type("", workspace_scope)


def account_validity(account: AccountModel, extra: dict[str, Any] | None = None) -> str:
    if _lower_text(account.status) == "invalid":
        return "invalid"
    extra = extra if isinstance(extra, dict) else _extra(account)
    capabilities = _chatgpt_capabilities(account, extra)
    if _lower_text(capabilities.get("auth_level")) == "invalid":
        return "invalid"
    if _lower_text(capabilities.get("upload_gate")) == "blocked_auth_invalid":
        return "invalid"

    local_probe = extra.get("chatgpt_local") if isinstance(extra.get("chatgpt_local"), dict) else {}
    for section_name in ("auth", "codex"):
        section = local_probe.get(section_name) if isinstance(local_probe.get(section_name), dict) else {}
        if _lower_text(section.get("state")) in AUTH_INVALID_STATES:
            return "invalid"
        if int(section.get("http_status") or 0) == 401:
            return "invalid"
    return "valid"


def account_sub2api_state(account: AccountModel, extra: dict[str, Any] | None = None) -> str:
    extra = extra if isinstance(extra, dict) else _extra(account)
    sync_statuses = extra.get("sync_statuses") if isinstance(extra.get("sync_statuses"), dict) else {}
    sub2api = sync_statuses.get("sub2api") if isinstance(sync_statuses.get("sub2api"), dict) else {}
    return _lower_text(sub2api.get("remote_state")) or "unknown"


def account_subscription_active_until_timestamp(account: AccountModel, extra: dict[str, Any] | None = None) -> float | None:
    extra = extra if isinstance(extra, dict) else _extra(account)
    local_probe = extra.get("chatgpt_local") if isinstance(extra.get("chatgpt_local"), dict) else {}
    subscription = local_probe.get("subscription") if isinstance(local_probe.get("subscription"), dict) else {}
    for candidate in (
        subscription.get("subscription_active_until"),
        subscription.get("subscription_expires_at_iso"),
        subscription.get("subscription_expires_at"),
        extra.get("subscription_active_until"),
        extra.get("subscription_expires_at"),
        extra.get("chatgpt_subscription_active_until"),
    ):
        timestamp = _parse_subscription_time(candidate)
        if timestamp is not None:
            return timestamp
    return None


def normalize_account_sort_order(value: Any) -> str:
    text = _lower_text(value)
    if text in {"asc", "ascend", "ascending"}:
        return "asc"
    if text in {"desc", "descend", "descending"}:
        return "desc"
    return ""


def should_sort_account_rows(sort_by: Any, sort_order: Any) -> bool:
    return _lower_text(sort_by) == "subscription_active_until" and bool(normalize_account_sort_order(sort_order))


def sort_account_rows(rows: Iterable[AccountModel], *, sort_by: Any = None, sort_order: Any = None) -> list[AccountModel]:
    items = list(rows)
    if not should_sort_account_rows(sort_by, sort_order):
        return items

    reverse = normalize_account_sort_order(sort_order) == "desc"

    def sort_key(row: AccountModel) -> tuple[int, float]:
        timestamp = account_subscription_active_until_timestamp(row)
        if timestamp is None:
            return (1, 0)
        return (0, -timestamp if reverse else timestamp)

    return sorted(items, key=sort_key)


def filter_account_rows(
    rows: Iterable[AccountModel],
    *,
    manually_used: bool | None = None,
    auth_type: Any = None,
    subscription_type: Any = None,
    account_validity_filter: Any = None,
    sub2api_state: Any = None,
) -> list[AccountModel]:
    auth_types = _split_values(auth_type)
    subscription_types = _split_values(subscription_type)
    validity_values = _split_values(account_validity_filter)
    sub2api_states = _split_values(sub2api_state)

    filtered: list[AccountModel] = []
    for row in rows:
        extra = _extra(row)
        if manually_used is not None and bool(extra.get("manually_used")) is not manually_used:
            continue
        if auth_types and account_auth_type(row, extra) not in auth_types:
            continue
        if subscription_types and account_subscription_type(row, extra) not in subscription_types:
            continue
        if validity_values and account_validity(row, extra) not in validity_values:
            continue
        if sub2api_states and account_sub2api_state(row, extra) not in sub2api_states:
            continue
        filtered.append(row)
    return filtered
