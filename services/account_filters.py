from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

from pydantic import BaseModel, Field
from sqlalchemy import String, and_, cast, exists, func, or_, text
from sqlmodel import Session, select

from core.db import (
    AccountFixedGroupMemberModel,
    AccountFixedGroupModel,
    AccountListStateModel,
    AccountModel,
)
from services.chatgpt_account_state import (
    AUTH_INVALID_STATES,
    classify_chatgpt_capabilities,
    is_paid_subscription_plan,
    normalize_subscription_plan,
)

AUTO_DELETE_REVIVAL_TASK_ID = "icloud_hme_auto_delete"
ACCOUNT_LIST_STATE_DERIVATION_VERSION = "integration-upload-state-v1-payment-link-history-v4-all-status-delete-checkout-link-type-v1"
ACCOUNT_FILTER_RESOLVER_VERSION = "account-list-state-v12-split-unknown-subscription"
SUBSCRIPTION_STATUS_UNCONFIRMABLE = "unconfirmable"
SUBSCRIPTION_STATUS_PENDING_REFRESH = "pending_refresh"
SUBSCRIPTION_STATUS_FILTER_VALUES = frozenset({
    SUBSCRIPTION_STATUS_UNCONFIRMABLE,
    SUBSCRIPTION_STATUS_PENDING_REFRESH,
})
SUBSCRIPTION_STATUS_FILTER_ALIASES = {
    "unconfirmed": SUBSCRIPTION_STATUS_UNCONFIRMABLE,
    "unavailable": SUBSCRIPTION_STATUS_UNCONFIRMABLE,
    "waiting": SUBSCRIPTION_STATUS_PENDING_REFRESH,
    "waiting_refresh": SUBSCRIPTION_STATUS_PENDING_REFRESH,
    "stale": SUBSCRIPTION_STATUS_PENDING_REFRESH,
}
ACCOUNT_FILTER_FIELD_NAMES = (
    "email",
    "status",
    "manually_used",
    "auth_type",
    "phone_binding_state",
    "payment_link_platform",
    "payment_link_generated",
    "subscription_type",
    "account_validity",
    "sub2api_state",
    "oaipay_state",
    "idea_submit_state",
    "submit_state",
    "zero_amount_eligibility_state",
    "gcash_payment_method_state",
    "checkout_link_type",
    "has_submitted",
    "primary_preset_id",
    "secondary_scope",
    "fixed_group_id",
    "fixed_group_revision",
)
ACCOUNT_SORT_CREATED_AT = "created_at"
ACCOUNT_SORT_SUBSCRIPTION_ACTIVE_UNTIL = "subscription_active_until"
ACCOUNT_SORT_FIELDS = frozenset({
    ACCOUNT_SORT_CREATED_AT,
    ACCOUNT_SORT_SUBSCRIPTION_ACTIVE_UNTIL,
})
DEFAULT_ACCOUNT_SORT_SPECS = ((ACCOUNT_SORT_CREATED_AT, "desc"),)
logger = logging.getLogger(__name__)


class AccountFilterRequestMixin(BaseModel):
    """Flat account-filter contract shared by task and action requests."""

    email: str = ""
    status: str = ""
    manually_used: str | None = None
    auth_type: str = ""
    phone_binding_state: str = ""
    payment_link_platform: str = ""
    payment_link_generated: str | None = None
    subscription_type: str = ""
    account_validity: str = ""
    sub2api_state: str = ""
    oaipay_state: str = ""
    idea_submit_state: str = ""
    submit_state: str = ""
    zero_amount_eligibility_state: str = ""
    gcash_payment_method_state: str = ""
    checkout_link_type: str = ""
    has_submitted: str | None = None
    primary_preset_id: str = ""
    secondary_scope: str = ""
    fixed_group_id: str = ""
    fixed_group_revision: int | None = Field(default=None, ge=1)
    expected_total: int | None = Field(default=None, ge=0)


class AccountFilterScopeChangedError(ValueError):
    status_code = 409

    def __init__(self, *, expected_total: int, matched_total: int):
        message = (
            f"筛选结果已变化：页面确认 {expected_total} 个账号，当前匹配 {matched_total} 个账号。"
            "请刷新列表并重新确认任务范围。"
        )
        self.detail = {
            "code": "FILTER_SCOPE_CHANGED",
            "expected_total": int(expected_total),
            "matched_total": int(matched_total),
            "message": message,
        }
        super().__init__(message)


class AccountFixedGroupScopeChangedError(AccountFilterScopeChangedError):
    def __init__(self, message: str, *, code: str = "FIXED_GROUP_SCOPE_CHANGED"):
        self.detail = {
            "code": code,
            "message": message,
        }
        ValueError.__init__(self, message)


@dataclass(frozen=True)
class AccountFilterResolution:
    rows: tuple[AccountModel, ...]
    account_ids: tuple[int, ...]
    normalized_filter: dict[str, Any]
    matched_total: int
    expected_total: int | None
    verified: bool
    audit: dict[str, Any]


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


_IDEA_SUBMIT_STATE_FILTER_ALIASES: dict[str, set[str]] = {
    "unsubmitted": {"available"},
    "available": {"available"},
    "not_submitted": {"available"},
    "pending_submit": {"available"},
    "submitting": {"submitted", "processing"},
    "pending": {"submitted", "processing"},
    "polling": {"submitted", "processing"},
    "submitted": {"submitted"},
    "processing": {"processing"},
    "paid": {"paid"},
    "success": {"paid"},
    "completed": {"paid"},
    "failed": {"failed"},
    "fail": {"failed"},
    "error": {"failed"},
    "timeout": {"timeout"},
    "manual_review": {"timeout"},
    "unknown_submit": {"timeout"},
    "stopped": {"stopped"},
    "unavailable": {"unavailable"},
}

_PHONE_BINDING_STATE_FILTER_ALIASES: dict[str, set[str]] = {
    "bound": {"confirmed"},
    "confirmed": {"confirmed"},
    "unbound": {"unconfirmed", "unknown"},
    "not_bound": {"unconfirmed", "unknown"},
    "not_confirmed": {"unconfirmed", "unknown"},
    "unconfirmed": {"unconfirmed"},
    "unknown": {"unknown"},
}

_PAYMENT_LINK_PRESENT_PLATFORMS = frozenset({
    "hosted",
    "paypal",
    "ideal",
    "upi",
    "pix",
    "twint",
    "kakao_pay",
    "team",
    "other",
})

_PAYMENT_LINK_PLATFORM_FILTER_ALIASES: dict[str, set[str]] = {
    "has_link": set(_PAYMENT_LINK_PRESENT_PLATFORMS),
    "current_has_link": set(_PAYMENT_LINK_PRESENT_PLATFORMS),
    "with_link": set(_PAYMENT_LINK_PRESENT_PLATFORMS),
    "has_payment_link": set(_PAYMENT_LINK_PRESENT_PLATFORMS),
    "none": {"none"},
    "without_link": {"none"},
    "no_link": {"none"},
    "no_payment_link": {"none"},
    "current_no_link": {"none"},
    "missing": {"none"},
    "pix": {"pix"},
    "upi": {"upi"},
    "paypal": {"paypal"},
    "paypal_url": {"paypal"},
    "ideal": {"ideal"},
    "ideal-pay": {"ideal"},
    "ideal_pay": {"ideal"},
    "twint": {"twint"},
    "kakao": {"kakao_pay"},
    "kakaopay": {"kakao_pay"},
    "kakao-pay": {"kakao_pay"},
    "kakao_pay": {"kakao_pay"},
    "team": {"team"},
    "team_checkout": {"team"},
    "chatgptteamplan": {"team"},
    "chatgpt": {"hosted", "team"},
    "hosted": {"hosted"},
    "payment": {"hosted"},
    "pay": {"hosted"},
    "long": {"hosted"},
    "chatgpt_hosted": {"hosted"},
    "stripe_hosted": {"hosted"},
    "other": {"other"},
}

_PAYMENT_LINK_TYPE_ALIASES: dict[str, str] = {
    "payment": "hosted",
    "pay": "hosted",
    "long": "hosted",
    "chatgpt": "hosted",
    "chatgpt_hosted": "hosted",
    "stripe_hosted": "hosted",
    "checkout": "hosted",
    "pp": "paypal",
    "paypal_url": "paypal",
    "ideal_pay": "ideal",
    "qr": "pix",
    "pix_qr": "pix",
    "upi_qr": "upi",
    "upi_qr_code": "upi",
    "kakao": "kakao_pay",
    "kakaopay": "kakao_pay",
    "team_checkout": "team",
    "chatgptteamplan": "team",
}
_PAYMENT_LINK_CONCRETE_TYPES = frozenset({
    "paypal",
    "ideal",
    "upi",
    "pix",
    "twint",
    "kakao_pay",
})


def _normalize_payment_link_type(value: Any) -> str:
    normalized = _lower_text(value).replace("-", "_")
    return _PAYMENT_LINK_TYPE_ALIASES.get(normalized, normalized)


def _payment_link_host_matches(host: str, *domains: str) -> bool:
    return any(host == domain or host.endswith(f".{domain}") for domain in domains)


_PAYMENT_LINK_CLEANED_STATUSES = frozenset({
    "expired_cleaned",
    "paid_cleaned",
    "cancelled_cleaned",
    "upi_expired_cleaned",
    "upi_paid_cleaned",
    "upi_cancelled_cleaned",
    "ideal_expired_cleaned",
    "ideal_paid_cleaned",
    "ideal_cancelled_cleaned",
    "payment_link_deleted",
})

_INTEGRATION_UPLOAD_STATE_FILTER_ALIASES: dict[str, str] = {
    "true": "uploaded",
    "uploaded": "uploaded",
    "exists": "uploaded",
    "false": "not_uploaded",
    "not_uploaded": "not_uploaded",
    "unknown": "not_uploaded",
    "not_found": "not_uploaded",
    "cross_workspace_only": "not_uploaded",
    "deleted_exact_match": "not_uploaded",
    "ambiguous": "not_uploaded",
    "unreachable": "not_uploaded",
}


def _split_idea_submit_filter_values(value: Any) -> set[str]:
    expanded: set[str] = set()
    for item in _split_values(value):
        expanded.update(_IDEA_SUBMIT_STATE_FILTER_ALIASES.get(item, {item}))
    return expanded


def _split_phone_binding_state_filter_values(value: Any) -> set[str]:
    expanded: set[str] = set()
    for item in _split_values(value):
        expanded.update(_PHONE_BINDING_STATE_FILTER_ALIASES.get(item, {item}))
    return expanded


def _split_payment_link_platform_filter_values(value: Any) -> set[str]:
    expanded: set[str] = set()
    for item in _split_values(value):
        expanded.update(_PAYMENT_LINK_PLATFORM_FILTER_ALIASES.get(item, {item}))
    return expanded


def _split_integration_upload_state_filter_values(value: Any) -> set[str]:
    normalized = {
        _INTEGRATION_UPLOAD_STATE_FILTER_ALIASES.get(item, item)
        for item in _split_values(value)
    }
    normalized.discard("")
    if normalized == {"uploaded", "not_uploaded"}:
        return set()
    return normalized


def normalize_optional_bool(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    normalized = _lower_text(value)
    if normalized in {"1", "true", "yes", "on", "used", "generated", "succeeded"}:
        return True
    if normalized in {"0", "false", "no", "off", "unused", "not_generated", "never"}:
        return False
    return None


def _filter_source_value(source: Any, field_name: str) -> Any:
    if isinstance(source, Mapping):
        return source.get(field_name)
    return getattr(source, field_name, None)


def _normalize_filter_values(
    value: Any,
    *,
    idea_submit: bool = False,
    phone_binding_state: bool = False,
    payment_link_platform: bool = False,
    integration_upload_state: bool = False,
) -> str:
    if idea_submit:
        values = _split_idea_submit_filter_values(value)
    elif phone_binding_state:
        values = _split_phone_binding_state_filter_values(value)
    elif payment_link_platform:
        values = _split_payment_link_platform_filter_values(value)
    elif integration_upload_state:
        values = _split_integration_upload_state_filter_values(value)
    else:
        values = _split_values(value)
    return ",".join(sorted(values))


def normalize_account_filter(source: Any) -> dict[str, Any]:
    """Return the canonical account filter represented by a flat request."""

    raw_fixed_group_revision = _filter_source_value(source, "fixed_group_revision")
    try:
        fixed_group_revision = int(raw_fixed_group_revision) if raw_fixed_group_revision is not None else None
    except (TypeError, ValueError):
        fixed_group_revision = None
    return {
        "email": _safe_str(_filter_source_value(source, "email")),
        "status": _normalize_filter_values(_filter_source_value(source, "status")),
        "manually_used": normalize_optional_bool(_filter_source_value(source, "manually_used")),
        "auth_type": _normalize_filter_values(_filter_source_value(source, "auth_type")),
        "phone_binding_state": _normalize_filter_values(
            _filter_source_value(source, "phone_binding_state"),
            phone_binding_state=True,
        ),
        "payment_link_platform": _normalize_filter_values(
            _filter_source_value(source, "payment_link_platform"),
            payment_link_platform=True,
        ),
        "payment_link_generated": normalize_optional_bool(
            _filter_source_value(source, "payment_link_generated")
        ),
        "subscription_type": _normalize_filter_values(_filter_source_value(source, "subscription_type")),
        "account_validity": _normalize_filter_values(_filter_source_value(source, "account_validity")),
        "sub2api_state": _normalize_filter_values(
            _filter_source_value(source, "sub2api_state"),
            integration_upload_state=True,
        ),
        "oaipay_state": _normalize_filter_values(
            _filter_source_value(source, "oaipay_state"),
            integration_upload_state=True,
        ),
        "idea_submit_state": _normalize_filter_values(
            _filter_source_value(source, "idea_submit_state"),
            idea_submit=True,
        ),
        "submit_state": _normalize_filter_values(
            _filter_source_value(source, "submit_state"),
            idea_submit=True,
        ),
        "zero_amount_eligibility_state": _normalize_filter_values(
            _filter_source_value(source, "zero_amount_eligibility_state"),
        ),
        "gcash_payment_method_state": _normalize_filter_values(
            _filter_source_value(source, "gcash_payment_method_state"),
        ),
        "checkout_link_type": _normalize_filter_values(
            _filter_source_value(source, "checkout_link_type"),
        ),
        "has_submitted": normalize_optional_bool(_filter_source_value(source, "has_submitted")),
        "primary_preset_id": _safe_str(_filter_source_value(source, "primary_preset_id")),
        "secondary_scope": normalize_secondary_scope(_filter_source_value(source, "secondary_scope")),
        "fixed_group_id": _safe_str(_filter_source_value(source, "fixed_group_id")),
        "fixed_group_revision": fixed_group_revision,
    }


def normalize_secondary_scope(value: Any) -> str:
    normalized = _safe_str(value).lower().replace("-", "_")
    if normalized in {"unassigned", "unfixed", "available"}:
        return "unassigned"
    if normalized in {"fixed", "group", "fixed_group"}:
        return "fixed"
    return ""


def _normalized_account_ids(account_ids: Iterable[Any]) -> list[int]:
    normalized: set[int] = set()
    for raw in account_ids:
        try:
            account_id = int(raw or 0)
        except (TypeError, ValueError):
            continue
        if account_id > 0:
            normalized.add(account_id)
    return sorted(normalized)


def build_account_filter_audit(
    source: Any,
    account_ids: Iterable[Any],
    *,
    matched_total: int | None = None,
    matched_account_ids: Iterable[Any] | None = None,
    all_filtered: bool | None = None,
) -> dict[str, Any]:
    frozen_ids = _normalized_account_ids(account_ids)
    matched_ids = _normalized_account_ids(matched_account_ids) if matched_account_ids is not None else frozen_ids
    total = len(matched_ids) if matched_total is None else max(int(matched_total), 0)
    raw_expected_total = _filter_source_value(source, "expected_total")
    expected_total = int(raw_expected_total) if raw_expected_total is not None else None
    filtered = bool(_filter_source_value(source, "all_filtered")) if all_filtered is None else bool(all_filtered)
    digest_payload = json.dumps(frozen_ids, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    matched_digest_payload = json.dumps(matched_ids, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return {
        "filter": normalize_account_filter(source),
        "expected_total": expected_total,
        "matched_total": total,
        "verified": bool(filtered and expected_total is not None and expected_total == total),
        "account_ids_sha256": hashlib.sha256(digest_payload).hexdigest(),
        "account_ids_count": len(frozen_ids),
        "matched_account_ids_sha256": hashlib.sha256(matched_digest_payload).hexdigest(),
        "matched_account_ids_count": len(matched_ids),
        "resolver_version": ACCOUNT_FILTER_RESOLVER_VERSION,
    }


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
    return bool(_safe_str(extra.get("refresh_token") or extra.get("refreshToken") or getattr(account, "refresh_token", "")))


def _has_access_token(account: AccountModel, extra: dict[str, Any]) -> bool:
    return bool(
        _safe_str(
            extra.get("access_token")
            or extra.get("accessToken")
            or extra.get("webAccessToken")
            or getattr(account, "access_token", "")
            or account.token
        )
    )


def _dict_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _normalize_revival_mode(value: Any) -> str:
    mode = _lower_text(value)
    if mode in {"revive_existing", "create_new"}:
        return mode
    return ""


def _normalize_revival_kind(*, source: Any, task_id: Any = "", mode: Any = "") -> str:
    normalized_source = _lower_text(source)
    normalized_task_id = _safe_str(task_id)
    normalized_mode = _normalize_revival_mode(mode)
    if normalized_source == "invalid_account_recheck":
        return "auto_delete_recheck" if normalized_task_id == AUTO_DELETE_REVIVAL_TASK_ID else "invalid_recheck"
    if normalized_source == "custom_email_recheck":
        return "custom_email_recheck_new" if normalized_mode == "create_new" else "custom_email_recheck"
    if normalized_mode == "create_new":
        return "custom_email_recheck_new"
    return normalized_source or "unknown"


def _revival_label(kind: str) -> str:
    labels = {
        "invalid_recheck": "失效测活恢复",
        "auto_delete_recheck": "删前测活恢复",
        "custom_email_recheck": "邮箱测活恢复",
        "custom_email_recheck_new": "邮箱测活新建",
        "unknown": "已恢复",
        "none": "普通",
    }
    return labels.get(kind, "已恢复")


def _revival_state(kind: str) -> str:
    if kind in {"invalid_recheck", "auto_delete_recheck", "custom_email_recheck", "unknown"}:
        return "revived"
    if kind == "custom_email_recheck_new":
        return "recovery_new"
    return "none"


def _build_revival_info(
    *,
    kind: str,
    source: Any = "",
    mode: Any = "",
    at: Any = "",
    task_id: Any = "",
    auth_level: Any = "",
    legacy_inferred: bool = False,
) -> dict[str, Any]:
    normalized_kind = kind or "none"
    normalized_mode = _normalize_revival_mode(mode)
    state = _revival_state(normalized_kind)
    return {
        "state": state,
        "kind": normalized_kind,
        "label": _revival_label(normalized_kind),
        "source": _safe_str(source),
        "mode": normalized_mode,
        "revived": state == "revived",
        "recovery_flow": state in {"revived", "recovery_new"},
        "at": _safe_str(at),
        "task_id": _safe_str(task_id),
        "auth_level": _safe_str(auth_level),
        "legacy_inferred": bool(legacy_inferred),
    }


def account_revival_info(account: AccountModel, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    extra = extra if isinstance(extra, dict) else _extra(account)

    last_marker = _dict_value(extra.get("chatgpt_last_revival"))
    if last_marker:
        mode = _normalize_revival_mode(last_marker.get("mode"))
        kind = _normalize_revival_kind(
            source=last_marker.get("source"),
            task_id=last_marker.get("task_id"),
            mode=mode,
        )
        return _build_revival_info(
            kind=kind,
            source=last_marker.get("source"),
            mode=mode,
            at=last_marker.get("revived_at"),
            task_id=last_marker.get("task_id"),
            auth_level=last_marker.get("auth_level"),
            legacy_inferred=False,
        )

    invalid_recheck = _dict_value(extra.get("chatgpt_invalid_recheck"))
    invalid_recheck_marker = _dict_value(invalid_recheck.get("revival_marker"))
    if invalid_recheck_marker:
        mode = _normalize_revival_mode(invalid_recheck_marker.get("mode"))
        kind = _normalize_revival_kind(
            source=invalid_recheck_marker.get("source"),
            task_id=invalid_recheck_marker.get("task_id"),
            mode=mode,
        )
        return _build_revival_info(
            kind=kind,
            source=invalid_recheck_marker.get("source"),
            mode=mode,
            at=invalid_recheck_marker.get("revived_at"),
            task_id=invalid_recheck_marker.get("task_id"),
            auth_level=invalid_recheck_marker.get("auth_level"),
            legacy_inferred=False,
        )
    if _lower_text(invalid_recheck.get("status")) == "recovered_access_token":
        auth_level = _safe_str(invalid_recheck.get("final_auth_level"))
        if not auth_level:
            if bool(invalid_recheck.get("followup_auth_ok") or invalid_recheck.get("has_refresh_token")):
                auth_level = "refresh_token"
            elif bool(invalid_recheck.get("has_access_token")):
                auth_level = "access_token_only"
        source = _safe_str(invalid_recheck.get("source") or "invalid_account_recheck")
        task_id = _safe_str(invalid_recheck.get("task_id"))
        return _build_revival_info(
            kind=_normalize_revival_kind(source=source, task_id=task_id, mode="revive_existing"),
            source=source,
            mode="revive_existing",
            at=invalid_recheck.get("checked_at") or invalid_recheck.get("revived_at"),
            task_id=task_id,
            auth_level=auth_level,
            legacy_inferred=True,
        )

    custom_recheck = _dict_value(extra.get("chatgpt_custom_email_recheck"))
    custom_recheck_marker = _dict_value(custom_recheck.get("revival_marker"))
    if custom_recheck_marker:
        mode = _normalize_revival_mode(custom_recheck_marker.get("mode"))
        kind = _normalize_revival_kind(
            source=custom_recheck_marker.get("source"),
            task_id=custom_recheck_marker.get("task_id"),
            mode=mode,
        )
        return _build_revival_info(
            kind=kind,
            source=custom_recheck_marker.get("source"),
            mode=mode,
            at=custom_recheck_marker.get("revived_at"),
            task_id=custom_recheck_marker.get("task_id"),
            auth_level=custom_recheck_marker.get("auth_level"),
            legacy_inferred=False,
        )
    if bool(custom_recheck.get("revived_existing_account")) or bool(custom_recheck.get("created_new_account")):
        mode = "revive_existing" if bool(custom_recheck.get("revived_existing_account")) else "create_new"
        auth_level = "refresh_token" if bool(custom_recheck.get("followup_auth_ok") or custom_recheck.get("has_refresh_token")) else ""
        if not auth_level and bool(custom_recheck.get("has_access_token")):
            auth_level = "access_token_only"
        source = _safe_str(custom_recheck.get("source") or "custom_email_recheck")
        task_id = _safe_str(custom_recheck.get("task_id"))
        return _build_revival_info(
            kind=_normalize_revival_kind(source=source, task_id=task_id, mode=mode),
            source=source,
            mode=mode,
            at=custom_recheck.get("checked_at") or custom_recheck.get("revived_at"),
            task_id=task_id,
            auth_level=auth_level,
            legacy_inferred=True,
        )

    return _build_revival_info(kind="none")


def account_revival_kind(account: AccountModel, extra: dict[str, Any] | None = None) -> str:
    extra = extra if isinstance(extra, dict) else _extra(account)
    return _safe_str(account_revival_info(account, extra).get("kind")) or "none"


def account_revival_state(account: AccountModel, extra: dict[str, Any] | None = None) -> str:
    extra = extra if isinstance(extra, dict) else _extra(account)
    return _safe_str(account_revival_info(account, extra).get("state")) or "none"


def account_auth_type(account: AccountModel, extra: dict[str, Any] | None = None) -> str:
    extra = extra if isinstance(extra, dict) else _extra(account)
    if _has_refresh_token(account, extra):
        return "refresh_token"
    if _has_access_token(account, extra):
        return "access_token_only"
    return "unknown"


def account_phone_binding_state(account: AccountModel, extra: dict[str, Any] | None = None) -> str:
    """Classify only locally confirmed full phone bindings.

    The classifier deliberately ignores RT presence and passive phone hints so
    list filtering follows the same fail-closed rule as account serialization.
    """

    extra = extra if isinstance(extra, dict) else _extra(account)
    local_probe = extra.get("chatgpt_local") if isinstance(extra.get("chatgpt_local"), dict) else {}
    capabilities = classify_chatgpt_capabilities(account, local_probe=local_probe)
    state = _lower_text(capabilities.get("phone_binding_state"))
    return state if state in {"confirmed", "unconfirmed", "unknown"} else "unknown"


_PAYMENT_LINK_URL_FIELDS = (
    "url",
    "paypal_url",
    "provider_redirect_url",
    "approval_url",
    "checkout_url",
    "cashier_url",
)


def _validated_payment_link_url(value: Any) -> str:
    """Return only a bounded browser-openable URL from persisted link metadata."""

    url = _safe_str(value)
    if not url or len(url) > 8192:
        return ""
    try:
        parsed = urlsplit(url)
    except (TypeError, ValueError):
        return ""
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return ""
    return url


def _account_payment_link_payload(
    account: AccountModel,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Find the current generated link while retaining legacy PayPal compatibility.

    ``cashier_url`` is intentionally not used as an account-level fallback:
    it predates the generated-link cache and is also used for unrelated trial
    checkout records.  A row is therefore "无支付链接" until a supported
    generated-link cache exists.
    """

    extra = extra if isinstance(extra, dict) else _extra(account)
    last_link = extra.get("chatgpt_last_payment_link")
    legacy_paypal = extra.get("chatgpt_paypal_url")
    candidates = [
        last_link if isinstance(last_link, dict) else {},
        legacy_paypal if isinstance(legacy_paypal, dict) else {},
    ]
    if isinstance(last_link, dict) and _lower_text(last_link.get("link_status")) in _PAYMENT_LINK_CLEANED_STATUSES:
        # The newest cleanup tombstone is terminal for the current-link view;
        # do not resurrect an older legacy PayPal cache beneath it.
        return {}
    for candidate in candidates:
        for field_name in _PAYMENT_LINK_URL_FIELDS:
            url = _validated_payment_link_url(candidate.get(field_name))
            if not url:
                continue
            payload = dict(candidate)
            payload["url"] = url
            if candidate is legacy_paypal and not _safe_str(payload.get("link_type")):
                payload["link_type"] = "paypal"
            return payload
    return {}


def payment_link_platform_from_payload(payload: dict[str, Any]) -> str:
    """Classify one normalized current-link payload into the public platform contract."""

    url = _validated_payment_link_url(payload.get("url"))
    if not url:
        return "none"

    link_type = _normalize_payment_link_type(payload.get("link_type"))
    payment_method_type = _normalize_payment_link_type(payload.get("payment_method_type"))
    generation_kind = _normalize_payment_link_type(payload.get("generation_kind"))
    plan = _normalize_payment_link_type(payload.get("plan"))
    plan_name = _normalize_payment_link_type(payload.get("plan_name"))
    link_format = _lower_text(payload.get("payment_link_format")).replace("-", "_")
    payment_source = _lower_text(payload.get("payment_source")).replace("-", "_")
    try:
        parsed_url = urlsplit(url)
        host = (parsed_url.hostname or "").lower()
        path = (parsed_url.path or "").lower()
    except (TypeError, ValueError):
        host = ""
        path = ""

    if link_type == "team" or generation_kind == "team" or plan == "team" or plan_name == "team":
        return "team"
    if link_type == "upi" or payment_method_type == "upi":
        return "upi"
    if link_type == "pix" or payment_method_type == "pix":
        return "pix"
    if "/upi/instructions/" in path:
        return "upi"
    if "/qr/instructions/" in path:
        return "pix"
    for candidate in (link_type, payment_method_type):
        if candidate in _PAYMENT_LINK_CONCRETE_TYPES:
            return candidate
    if (
        link_format in {"paypal", "paypal_url", "paypal_approval", "provider_url"}
        or "paypal" in payment_source
        or _payment_link_host_matches(host, "paypal.com")
    ):
        return "paypal"
    if link_format in {"ideal", "ideal_url"} or (host == "pay.ideal.nl" and path.startswith("/transactions/")):
        return "ideal"
    if link_format in {"twint", "twint_url"} or _payment_link_host_matches(host, "twint.ch"):
        return "twint"
    if link_format in {"kakao_pay", "kakao_pay_url"} or _payment_link_host_matches(
        host,
        "kakao.com",
        "kakaopay.com",
        "kakaopay.co.kr",
        "nicepay.com",
        "nicepay.co.kr",
    ):
        return "kakao_pay"
    if (
        link_type == "hosted"
        or payment_method_type == "hosted"
        or link_format in {"short", "short_chatgpt", "long", "long_hosted", "hosted", "hosted_checkout", "pay_openai", "stripe_hosted"}
        or payment_source == "chatgpt_hosted"
        or _payment_link_host_matches(host, "chatgpt.com")
        or host == "pay.openai.com"
        or host.endswith(".openai.com")
    ):
        return "hosted"
    return "other"


# Keep the private name stable for older in-repo imports while scanners and
# other shared services consume the explicit public classifier above.
_payment_link_platform_from_payload = payment_link_platform_from_payload


def account_payment_link_platform(account: AccountModel, extra: dict[str, Any] | None = None) -> str:
    """Classify generated payment links by payment platform, not transport source."""

    extra = extra if isinstance(extra, dict) else _extra(account)
    return payment_link_platform_from_payload(_account_payment_link_payload(account, extra))


def account_checkout_link_type(account: AccountModel, extra: dict[str, Any] | None = None) -> str:
    """Classify checkout link type (oaics / cs / none)."""
    extra = extra if isinstance(extra, dict) else _extra(account)
    chk = extra.get("chatgpt_checkout_link_type")
    if isinstance(chk, dict):
        st = str(chk.get("state") or chk.get("link_type") or chk.get("confirmed_state") or "").strip().lower()
        if st in {"oaics", "cs"}:
            return st
    last_link = extra.get("chatgpt_last_payment_link")
    last_url = ""
    if isinstance(last_link, dict):
        last_url = str(last_link.get("url") or last_link.get("checkout_url") or "").strip().lower()
        sess_id = str(last_link.get("session_id") or "").strip().lower()
        if sess_id.startswith("oaics_"):
            return "oaics"
        if sess_id.startswith("cs_"):
            return "cs"
    if not last_url:
        last_url = str(getattr(account, "cashier_url", "") or "").strip().lower()
    if "oaics_" in last_url or "/checkout/openai" in last_url:
        return "oaics"
    if "cs_" in last_url or "checkout.stripe.com" in last_url:
        return "cs"
    zero = extra.get("chatgpt_zero_amount_eligibility")
    if isinstance(zero, dict):
        ev = zero.get("evidence") or zero.get("last_attempt", {}).get("evidence") or {}
        prov = str(ev.get("session_provider") or "").strip().lower()
        sess_id = str(ev.get("session_id") or "").strip().lower()
        if prov == "open_ai" or sess_id.startswith("oaics_"):
            return "oaics"
        if prov == "stripe" or sess_id.startswith("cs_"):
            return "cs"
    pm = extra.get("chatgpt_payment_methods")
    if isinstance(pm, dict):
        ev = pm.get("evidence") or pm.get("last_attempt", {}).get("evidence") or {}
        prov = str(ev.get("provider") or ev.get("session_provider") or "").strip().lower()
        sess_id = str(ev.get("session_id") or "").strip().lower()
        if prov == "open_ai" or sess_id.startswith("oaics_"):
            return "oaics"
        if prov == "stripe" or sess_id.startswith("cs_"):
            return "cs"
    gcash = extra.get("chatgpt_gcash_payment_method")
    if isinstance(gcash, dict):
        ev = gcash.get("evidence") or gcash.get("last_attempt", {}).get("evidence") or {}
        prov = str(ev.get("session_provider") or "").strip().lower()
        sess_id = str(ev.get("session_id") or "").strip().lower()
        if prov == "open_ai" or sess_id.startswith("oaics_"):
            return "oaics"
        if prov == "stripe" or sess_id.startswith("cs_"):
            return "cs"
    return "none"



def _account_payment_link_metadata_payload(
    account: AccountModel,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the latest non-secret link marker even after its URL is cleaned.

    Cleanup intentionally removes every URL field from the marker while
    retaining lifecycle metadata.  This helper must never be used as a source
    of an openable URL; it exists only so list consumers can explain why the
    current-link platform is ``none``.
    """

    extra = extra if isinstance(extra, dict) else _extra(account)
    last_link = extra.get("chatgpt_last_payment_link")
    if isinstance(last_link, dict):
        return dict(last_link)
    legacy_paypal = extra.get("chatgpt_paypal_url")
    if isinstance(legacy_paypal, dict):
        payload = dict(legacy_paypal)
        if not _safe_str(payload.get("link_type")):
            payload["link_type"] = "paypal"
        return payload
    return {}


def account_payment_link_generated(
    account: AccountModel,
    extra: dict[str, Any] | None = None,
    *,
    persisted_history: bool | None = None,
) -> bool:
    """Return whether an account has durable evidence of a successful link.

    ``persisted_history`` is supplied by the SQL list-state projection when a
    ``payment_link_generations`` row exists.  The pure-Python fallback still
    recognizes current URLs and cleaned PIX tombstones without doing a
    per-account database lookup.
    """

    extra = extra if isinstance(extra, dict) else _extra(account)
    if _validated_payment_link_url(_account_payment_link_payload(account, extra).get("url")):
        return True
    marker = _account_payment_link_metadata_payload(account, extra)
    if _lower_text(marker.get("link_status")) in _PAYMENT_LINK_CLEANED_STATUSES:
        return True
    return bool(persisted_history)


def account_payment_link_summary(account: AccountModel, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a redacted payment-link summary, including cleaned tombstones."""

    extra = extra if isinstance(extra, dict) else _extra(account)
    payload = _account_payment_link_payload(account, extra)
    current_url = _validated_payment_link_url(payload.get("url"))
    if _lower_text(payload.get("link_status")) in _PAYMENT_LINK_CLEANED_STATUSES:
        current_url = ""
    if not payload:
        payload = _account_payment_link_metadata_payload(account, extra)
    platform = _payment_link_platform_from_payload(payload) if current_url else "none"
    summary: dict[str, Any] = {"platform": platform}
    if current_url:
        summary["url"] = current_url
    for key, max_length in (
        ("link_type", 64),
        ("link_status", 64),
        ("link_status_reason", 128),
        ("payment_link_format", 64),
        ("generated_at", 128),
        ("created_at", 128),
        ("link_status_updated_at", 128),
        ("cleaned_at", 128),
        ("expired_at", 128),
        ("payment_link_cleanup_mode", 64),
        ("payment_link_cleanup_type", 64),
        ("payment_link_cleanup_through_at", 128),
        ("pix_cleanup_mode", 64),
        ("link_expiry_source", 64),
        ("previous_link_status", 64),
    ):
        value = _safe_str(payload.get(key))
        if value:
            summary[key] = value[:max_length]
    expires_at = payload.get("link_expires_at")
    if isinstance(expires_at, (int, float)) and not isinstance(expires_at, bool) and expires_at > 0:
        summary["link_expires_at"] = int(expires_at)
    return summary


def _normalize_subscription_type(plan: Any) -> str:
    return normalize_subscription_plan(plan)


def _truthy_value(value: Any) -> bool:
    return _lower_text(value) in {"1", "true", "yes", "on"}


def account_subscription_type(account: AccountModel, extra: dict[str, Any] | None = None) -> str:
    extra = extra if isinstance(extra, dict) else _extra(account)
    capabilities = _chatgpt_capabilities(account, extra)
    local_probe = extra.get("chatgpt_local") if isinstance(extra.get("chatgpt_local"), dict) else {}
    subscription = local_probe.get("subscription") if isinstance(local_probe.get("subscription"), dict) else {}
    local_plan = _normalize_subscription_type(subscription.get("plan"))
    if local_plan != "unknown":
        return local_plan
    capabilities_plan = _normalize_subscription_type(capabilities.get("subscription_plan"))
    if capabilities_plan != "unknown" and _truthy_value(capabilities.get("subscription_checked")):
        return capabilities_plan
    return "unknown"


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
        if _lower_text(section.get("state")) == "probe_failed":
            return "refresh_failed"
    auth_section = local_probe.get("auth") if isinstance(local_probe.get("auth"), dict) else {}
    if not _lower_text(auth_section.get("state")) and not _lower_text(capabilities.get("auth_level")):
        return "not_checked"
    return "valid"


def account_subscription_status(account: AccountModel, extra: dict[str, Any] | None = None) -> str:
    """Return the four-bucket subscription status used by operational summaries."""

    extra = extra if isinstance(extra, dict) else _extra(account)
    plan = account_subscription_type(account, extra)
    if plan == "free":
        return "free"
    if is_paid_subscription_plan(plan):
        return "plus"
    if account_validity(account, extra) == "invalid":
        return SUBSCRIPTION_STATUS_UNCONFIRMABLE
    return SUBSCRIPTION_STATUS_PENDING_REFRESH


def _split_subscription_filter_values(value: Any) -> set[str]:
    return {
        SUBSCRIPTION_STATUS_FILTER_ALIASES.get(item, item)
        for item in _split_values(value)
    }


def _subscription_filter_predicate(subscription_type: Any) -> Any | None:
    values = _split_subscription_filter_values(subscription_type)
    if not values:
        return None

    predicates: list[Any] = []
    exact_types = values - SUBSCRIPTION_STATUS_FILTER_VALUES
    if exact_types:
        predicates.append(AccountListStateModel.subscription_type.in_(sorted(exact_types)))
    if SUBSCRIPTION_STATUS_UNCONFIRMABLE in values:
        predicates.append(and_(
            AccountListStateModel.subscription_type == "unknown",
            AccountListStateModel.account_validity == "invalid",
        ))
    if SUBSCRIPTION_STATUS_PENDING_REFRESH in values:
        predicates.append(and_(
            AccountListStateModel.subscription_type == "unknown",
            AccountListStateModel.account_validity != "invalid",
        ))
    return or_(*predicates)


def account_sub2api_state(account: AccountModel, extra: dict[str, Any] | None = None) -> str:
    extra = extra if isinstance(extra, dict) else _extra(account)
    sync_statuses = extra.get("sync_statuses") if isinstance(extra.get("sync_statuses"), dict) else {}
    sub2api = sync_statuses.get("sub2api") if isinstance(sync_statuses.get("sub2api"), dict) else {}
    return _lower_text(sub2api.get("remote_state")) or "unknown"


def account_oaipay_state(account: AccountModel, extra: dict[str, Any] | None = None) -> str:
    extra = extra if isinstance(extra, dict) else _extra(account)
    sync_statuses = extra.get("sync_statuses") if isinstance(extra.get("sync_statuses"), dict) else {}
    oaipay = sync_statuses.get("oaipay") if isinstance(sync_statuses.get("oaipay"), dict) else {}
    return _lower_text(oaipay.get("remote_state")) or "unknown"


def _integration_upload_state(sync_state: Any) -> str:
    state = sync_state if isinstance(sync_state, dict) else {}
    remote_state = _lower_text(state.get("remote_state"))
    last_upload = state.get("last_upload") if isinstance(state.get("last_upload"), dict) else {}
    uploaded = (
        normalize_optional_bool(state.get("uploaded")) is True
        or remote_state in {"exists", "uploaded"}
        or _lower_text(last_upload.get("status")) == "success"
    )
    return "uploaded" if uploaded else "not_uploaded"


def account_sub2api_upload_state(account: AccountModel, extra: dict[str, Any] | None = None) -> str:
    extra = extra if isinstance(extra, dict) else _extra(account)
    sync_statuses = extra.get("sync_statuses") if isinstance(extra.get("sync_statuses"), dict) else {}
    return _integration_upload_state(sync_statuses.get("sub2api"))


def account_oaipay_upload_state(account: AccountModel, extra: dict[str, Any] | None = None) -> str:
    extra = extra if isinstance(extra, dict) else _extra(account)
    sync_statuses = extra.get("sync_statuses") if isinstance(extra.get("sync_statuses"), dict) else {}
    return _integration_upload_state(sync_statuses.get("oaipay"))


_SUBMISSION_RESULT_STATES = {"paid", "submitted", "processing", "failed", "timeout", "stopped"}
_SUBMISSION_EVIDENCE_STATES = {"paid", "submitted", "processing"}
_PIX_LINK_SUBMITTED_STATUS = "pix_submitted"


def account_submission_info(account: AccountModel, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the channel-neutral current submission state and evidence flags.

    ``idea_submit_state`` remains a legacy eligibility-first contract.  The
    canonical submission state keeps a real order outcome authoritative and
    exposes link-consumption evidence independently, so a failed PIX order can
    still report that its current saved link has already been submitted.
    """

    extra = extra if isinstance(extra, dict) else _extra(account)
    marker = extra.get("idea_submit") if isinstance(extra.get("idea_submit"), dict) else {}
    baxigpt_cdk = extra.get("baxigpt_cdk") if isinstance(extra.get("baxigpt_cdk"), dict) else {}
    payment_link = (
        extra.get("chatgpt_last_payment_link")
        if isinstance(extra.get("chatgpt_last_payment_link"), dict)
        else {}
    )
    cdk_status = _lower_text(baxigpt_cdk.get("status"))
    marker_status = _lower_text(marker.get("status"))
    result_status = cdk_status if cdk_status in _SUBMISSION_RESULT_STATES else marker_status
    if result_status not in _SUBMISSION_RESULT_STATES:
        result_status = ""

    unavailable = _truthy_value(marker.get("unavailable")) or _truthy_value(extra.get("idea_submit_unavailable"))
    if not unavailable and _truthy_value(extra.get("chatgpt_account_unavailable")) and cdk_status == "failed":
        unavailable = True

    link_status = _lower_text(payment_link.get("link_status"))
    link_submitted = link_status == _PIX_LINK_SUBMITTED_STATUS
    order_id = _safe_str(baxigpt_cdk.get("order_id") or marker.get("order_id"))
    display_id = _safe_str(baxigpt_cdk.get("display_id") or marker.get("display_id"))
    has_submitted = bool(
        link_submitted
        or cdk_status in _SUBMISSION_EVIDENCE_STATES
        or marker_status in _SUBMISSION_EVIDENCE_STATES
        or order_id
        or display_id
    )

    state = result_status
    if not state and link_submitted:
        state = "submitted"
    if not state and unavailable:
        state = "unavailable"
    if not state:
        state = "available"

    return {
        "state": state,
        "has_submitted": has_submitted,
        "link_submitted": link_submitted,
        "link_status": link_status,
        "unavailable": unavailable,
    }


def account_submit_state(account: AccountModel, extra: dict[str, Any] | None = None) -> str:
    return str(account_submission_info(account, extra).get("state") or "available")


def account_has_submitted(account: AccountModel, extra: dict[str, Any] | None = None) -> bool:
    return bool(account_submission_info(account, extra).get("has_submitted"))


def account_idea_submit_state(account: AccountModel, extra: dict[str, Any] | None = None) -> str:
    """Legacy eligibility-first state retained for old presets and clients."""

    extra = extra if isinstance(extra, dict) else _extra(account)
    marker = extra.get("idea_submit") if isinstance(extra.get("idea_submit"), dict) else {}
    baxigpt_cdk = extra.get("baxigpt_cdk") if isinstance(extra.get("baxigpt_cdk"), dict) else {}
    cdk_status = _lower_text(baxigpt_cdk.get("status"))
    unavailable = _truthy_value(marker.get("unavailable")) or _truthy_value(extra.get("idea_submit_unavailable"))
    if not unavailable and _truthy_value(extra.get("chatgpt_account_unavailable")) and cdk_status == "failed":
        unavailable = True
    if unavailable:
        return "unavailable"
    if cdk_status in {"paid", "submitted", "processing", "failed", "timeout", "stopped"}:
        return cdk_status
    return "available"


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


def ensure_account_list_state_schema(session: Session) -> None:
    """Create/upgrade the denormalized account list state table for request-time use."""

    session.exec(
        text(
            """
            CREATE TABLE IF NOT EXISTS account_list_state (
                account_id INTEGER PRIMARY KEY,
                platform TEXT NOT NULL DEFAULT '',
                manually_used INTEGER NOT NULL DEFAULT 0,
                auth_type TEXT NOT NULL DEFAULT 'unknown',
                phone_binding_state TEXT NOT NULL DEFAULT 'unknown',
                payment_link_platform TEXT NOT NULL DEFAULT 'none',
                payment_link_generated INTEGER NOT NULL DEFAULT 0,
                auth_level TEXT NOT NULL DEFAULT '',
                subscription_type TEXT NOT NULL DEFAULT 'unknown',
                account_validity TEXT NOT NULL DEFAULT 'valid',
                sub2api_state TEXT NOT NULL DEFAULT 'unknown',
                idea_submit_state TEXT NOT NULL DEFAULT 'available',
                submit_state TEXT NOT NULL DEFAULT 'available',
                zero_amount_eligibility_state TEXT NOT NULL DEFAULT 'unknown',
                gcash_payment_method_state TEXT NOT NULL DEFAULT 'unknown',
                has_submitted INTEGER NOT NULL DEFAULT 0,
                revival_state TEXT NOT NULL DEFAULT 'none',
                revival_kind TEXT NOT NULL DEFAULT 'none',
                subscription_active_until TEXT NOT NULL DEFAULT '',
                subscription_active_until_ts REAL,
                source_updated_at TEXT NOT NULL DEFAULT '',
                refreshed_at TEXT NOT NULL DEFAULT '',
                derivation_version TEXT NOT NULL DEFAULT ''
            )
            """
        )
    )
    required_columns = {
        "platform": "TEXT NOT NULL DEFAULT ''",
        "manually_used": "INTEGER NOT NULL DEFAULT 0",
        "auth_type": "TEXT NOT NULL DEFAULT 'unknown'",
        "phone_binding_state": "TEXT NOT NULL DEFAULT 'unknown'",
        "payment_link_platform": "TEXT NOT NULL DEFAULT 'none'",
        "payment_link_generated": "INTEGER NOT NULL DEFAULT 0",
        "checkout_link_type": "TEXT NOT NULL DEFAULT 'none'",
        "auth_level": "TEXT NOT NULL DEFAULT ''",
        "subscription_type": "TEXT NOT NULL DEFAULT 'unknown'",
        "account_validity": "TEXT NOT NULL DEFAULT 'valid'",
        "sub2api_state": "TEXT NOT NULL DEFAULT 'unknown'",
        "oaipay_state": "TEXT NOT NULL DEFAULT 'unknown'",
        "idea_submit_state": "TEXT NOT NULL DEFAULT 'available'",
        "submit_state": "TEXT NOT NULL DEFAULT 'available'",
        "zero_amount_eligibility_state": "TEXT NOT NULL DEFAULT 'unknown'",
        "gcash_payment_method_state": "TEXT NOT NULL DEFAULT 'unknown'",
        "has_submitted": "INTEGER NOT NULL DEFAULT 0",
        "revival_state": "TEXT NOT NULL DEFAULT 'none'",
        "revival_kind": "TEXT NOT NULL DEFAULT 'none'",
        "subscription_active_until": "TEXT NOT NULL DEFAULT ''",
        "subscription_active_until_ts": "REAL",
        "source_updated_at": "TEXT NOT NULL DEFAULT ''",
        "refreshed_at": "TEXT NOT NULL DEFAULT ''",
        "derivation_version": "TEXT NOT NULL DEFAULT ''",
    }
    existing_columns: set[str] = set()
    for row in session.exec(text("PRAGMA table_info(account_list_state)")).all():
        try:
            column_name = str(row[1] or "")
        except Exception:
            column_name = ""
        if column_name:
            existing_columns.add(column_name)
    for column_name, ddl in required_columns.items():
        if column_name in existing_columns:
            continue
        session.exec(text(f"ALTER TABLE account_list_state ADD COLUMN {column_name} {ddl}"))
    for index_sql in (
        "CREATE INDEX IF NOT EXISTS idx_account_list_state_platform ON account_list_state(platform)",
        "CREATE INDEX IF NOT EXISTS idx_account_list_state_manually_used ON account_list_state(manually_used)",
        "CREATE INDEX IF NOT EXISTS idx_account_list_state_auth_type ON account_list_state(auth_type)",
        "CREATE INDEX IF NOT EXISTS idx_account_list_state_phone_binding_state ON account_list_state(phone_binding_state)",
        "CREATE INDEX IF NOT EXISTS idx_account_list_state_payment_link_platform ON account_list_state(payment_link_platform)",
        "CREATE INDEX IF NOT EXISTS idx_account_list_state_payment_link_generated ON account_list_state(payment_link_generated)",
        "CREATE INDEX IF NOT EXISTS idx_account_list_state_checkout_link_type ON account_list_state(checkout_link_type)",
        "CREATE INDEX IF NOT EXISTS idx_account_list_state_subscription_type ON account_list_state(subscription_type)",
        "CREATE INDEX IF NOT EXISTS idx_account_list_state_account_validity ON account_list_state(account_validity)",
        "CREATE INDEX IF NOT EXISTS idx_account_list_state_sub2api_state ON account_list_state(sub2api_state)",
        "CREATE INDEX IF NOT EXISTS idx_account_list_state_oaipay_state ON account_list_state(oaipay_state)",
        "CREATE INDEX IF NOT EXISTS idx_account_list_state_idea_submit_state ON account_list_state(idea_submit_state)",
        "CREATE INDEX IF NOT EXISTS idx_account_list_state_submit_state ON account_list_state(submit_state)",
        "CREATE INDEX IF NOT EXISTS idx_account_list_state_zero_amount_eligibility ON account_list_state(zero_amount_eligibility_state)",
        "CREATE INDEX IF NOT EXISTS idx_account_list_state_gcash_payment_method ON account_list_state(gcash_payment_method_state)",
        "CREATE INDEX IF NOT EXISTS idx_account_list_state_has_submitted ON account_list_state(has_submitted)",
        "CREATE INDEX IF NOT EXISTS idx_account_list_state_revival_state ON account_list_state(revival_state)",
        "CREATE INDEX IF NOT EXISTS idx_account_list_state_subscription_active_until_ts ON account_list_state(subscription_active_until_ts)",
        "CREATE INDEX IF NOT EXISTS idx_account_list_state_derivation_version ON account_list_state(derivation_version)",
    ):
        session.exec(text(index_sql))


def _normalize_account_list_state_ids(account_ids: Any) -> list[int]:
    if account_ids is None:
        return []
    if isinstance(account_ids, (str, bytes)) or not isinstance(account_ids, Iterable):
        raw_items = [account_ids]
    else:
        raw_items = list(account_ids)
    ids: list[int] = []
    seen: set[int] = set()
    for value in raw_items:
        try:
            account_id = int(value or 0)
        except Exception:
            continue
        if account_id <= 0 or account_id in seen:
            continue
        seen.add(account_id)
        ids.append(account_id)
    return ids


def _account_list_state_target_where(
    *,
    account_ids: Any = None,
    stale_only: bool = False,
    platform: Any = None,
) -> str:
    terms = ["1 = 1"]
    ids = _normalize_account_list_state_ids(account_ids)
    if ids:
        terms.append("id IN (" + ",".join(str(account_id) for account_id in ids) + ")")
    platform_value = _safe_str(platform)
    if platform_value:
        terms.append("platform = '" + platform_value.replace("'", "''") + "'")
    if stale_only:
        terms.append(
            """
            (
                NOT EXISTS (
                    SELECT 1
                    FROM account_list_state AS state
                    WHERE state.account_id = accounts.id
                )
                OR coalesce((
                    SELECT state.source_updated_at
                    FROM account_list_state AS state
                    WHERE state.account_id = accounts.id
                ), '') != CAST(accounts.updated_at AS TEXT)
                OR coalesce((
                    SELECT state.derivation_version
                    FROM account_list_state AS state
                    WHERE state.account_id = accounts.id
                ), '') != '__ACCOUNT_LIST_STATE_DERIVATION_VERSION__'
                OR (
                    coalesce((
                        SELECT state.payment_link_generated
                        FROM account_list_state AS state
                        WHERE state.account_id = accounts.id
                    ), 0) = 0
                    AND EXISTS (
                        SELECT 1
                        FROM payment_link_generations AS generation
                        WHERE generation.account_id = accounts.id
                          AND lower(trim(coalesce(generation.account_email, ''))) = lower(trim(coalesce(accounts.email, '')))
                          AND trim(coalesce(generation.account_created_at, '')) = trim(CAST(accounts.created_at AS TEXT))
                          AND lower(trim(coalesce(generation.status, ''))) = 'succeeded'
                          AND length(trim(coalesce(generation.url, ''))) BETWEEN 8 AND 8192
                          AND (
                              (
                                  lower(trim(generation.url)) LIKE 'http://%'
                                  AND substr(lower(trim(generation.url)), 8, 1) NOT IN ('', '/', '?', '#')
                                  AND trim(generation.url) NOT LIKE '% %'
                              )
                              OR (
                                  lower(trim(generation.url)) LIKE 'https://%'
                                  AND substr(lower(trim(generation.url)), 9, 1) NOT IN ('', '/', '?', '#')
                                  AND trim(generation.url) NOT LIKE '% %'
                              )
                          )
                    )
                )
            )
            """
            .replace("__ACCOUNT_LIST_STATE_DERIVATION_VERSION__", ACCOUNT_LIST_STATE_DERIVATION_VERSION.replace("'", "''"))
        )
    return " AND ".join(terms)


def _looks_like_sql_session(session: Any) -> bool:
    if session is None:
        return False
    if type(session).__module__.startswith("unittest.mock"):
        return False
    exec_fn = getattr(session, "exec", None)
    return callable(exec_fn)


def _log_account_list_state_sync_skip(action: str, account_ids: list[int], exc: Exception) -> None:
    logger.warning(
        "Account list-state %s skipped account_ids=%s error=%s",
        action,
        account_ids[:20],
        exc,
        exc_info=True,
    )


def refresh_account_list_state(
    session: Session,
    *,
    account_ids: Any = None,
    stale_only: bool = False,
    platform: Any = None,
    cleanup_orphans: bool = True,
    commit: bool = True,
) -> int:
    """Refresh list-derived state from accounts with SQL-only extraction.

    The table intentionally stores only non-secret summaries used by list
    filters/sorts.  Token values are read only as presence checks and are never
    persisted.
    """

    ensure_account_list_state_schema(session)
    session.flush()
    target_where = _account_list_state_target_where(
        account_ids=account_ids,
        stale_only=stale_only,
        platform=platform,
    )
    target_count_row = session.exec(text(f"SELECT COUNT(*) FROM accounts WHERE {target_where}")).one()
    try:
        target_count_value = target_count_row[0]
    except Exception:
        target_count_value = target_count_row
    target_count = int(target_count_value or 0)
    if target_count > 0:
        session.exec(
            text(
                """
            WITH account_json AS (
                SELECT
                    id AS account_id,
                    platform,
                    lower(trim(coalesce(email, ''))) AS account_email,
                    trim(CAST(created_at AS TEXT)) AS account_created_at,
                    status,
                    token,
                    CAST(updated_at AS TEXT) AS source_updated_at,
                    CASE WHEN json_valid(extra_json) THEN extra_json ELSE '{}' END AS extra
                FROM accounts
                WHERE __ACCOUNT_LIST_STATE_TARGET_WHERE__
            ),
            extracted AS (
                SELECT
                    account_id,
                    platform,
                    status,
                    token,
                    source_updated_at,
                    extra,
                    lower(trim(status)) AS account_status,
                    trim(coalesce(
                        json_extract(extra, '$.refresh_token'),
                        json_extract(extra, '$.refreshToken'),
                        ''
                    )) AS refresh_token_value,
                    trim(coalesce(
                        json_extract(extra, '$.access_token'),
                        json_extract(extra, '$.accessToken'),
                        json_extract(extra, '$.webAccessToken'),
                        token,
                        ''
                    )) AS access_token_value,
                    CASE
                        WHEN lower(trim(CAST(coalesce(json_extract(extra, '$.manually_used'), '') AS TEXT)))
                             IN ('1', 'true', 'yes', 'on', 'used')
                        THEN 1
                        ELSE 0
                    END AS manually_used,
                    CASE
                        WHEN json_type(extra, '$.chatgpt_phone_binding') = 'object'
                             AND trim(CAST(json_extract(extra, '$.chatgpt_phone_binding') AS TEXT)) NOT IN ('', '{}')
                        THEN 1
                        ELSE 0
                    END AS phone_binding_present,
                    lower(trim(coalesce(
                        nullif(trim(CAST(json_extract(extra, '$.chatgpt_phone_binding.status') AS TEXT)), ''),
                        nullif(trim(CAST(json_extract(extra, '$.chatgpt_phone_binding.result') AS TEXT)), ''),
                        ''
                    ))) AS phone_binding_status,
                    coalesce(
                        nullif(trim(CAST(json_extract(extra, '$.chatgpt_phone_binding.phone') AS TEXT)), ''),
                        nullif(trim(CAST(json_extract(extra, '$.chatgpt_phone_binding.phone_number') AS TEXT)), ''),
                        ''
                    ) AS phone_binding_phone,
                    CASE WHEN json_type(extra, '$.chatgpt_bound_phone') = 'object' THEN 1 ELSE 0 END AS bound_phone_present,
                    trim(coalesce(json_extract(extra, '$.chatgpt_bound_phone_number'), '')) AS bound_phone_number,
                    trim(coalesce(json_extract(extra, '$.chatgpt_bound_phone_masked'), '')) AS bound_phone_masked,
                    CASE WHEN json_type(extra, '$.chatgpt_phone_challenge') = 'object' THEN 1 ELSE 0 END AS phone_challenge_present,
                    trim(coalesce(
                        (
                            SELECT trim(CAST(value AS TEXT))
                            FROM json_each(
                                CASE
                                    WHEN json_type(extra, '$.chatgpt_last_payment_link') = 'object'
                                    THEN json_extract(extra, '$.chatgpt_last_payment_link')
                                    ELSE '{}'
                                END
                            )
                            WHERE key IN ('url', 'paypal_url', 'provider_redirect_url', 'approval_url', 'checkout_url', 'cashier_url')
                              AND lower(trim(coalesce(json_extract(extra, '$.chatgpt_last_payment_link.link_status'), ''))) NOT IN ('expired_cleaned', 'paid_cleaned', 'cancelled_cleaned', 'upi_expired_cleaned', 'upi_paid_cleaned', 'upi_cancelled_cleaned', 'ideal_expired_cleaned', 'ideal_paid_cleaned', 'ideal_cancelled_cleaned', 'payment_link_deleted')
                              AND (
                                  (
                                      lower(trim(CAST(value AS TEXT))) LIKE 'http://%'
                                      AND length(trim(CAST(value AS TEXT))) BETWEEN 8 AND 8192
                                      AND substr(lower(trim(CAST(value AS TEXT))), 8, 1) NOT IN ('', '/', '?', '#')
                                      AND trim(CAST(value AS TEXT)) NOT LIKE '% %'
                                  )
                                  OR (
                                      lower(trim(CAST(value AS TEXT))) LIKE 'https://%'
                                      AND length(trim(CAST(value AS TEXT))) BETWEEN 9 AND 8192
                                      AND substr(lower(trim(CAST(value AS TEXT))), 9, 1) NOT IN ('', '/', '?', '#')
                                      AND trim(CAST(value AS TEXT)) NOT LIKE '% %'
                                  )
                              )
                            ORDER BY CASE key
                                WHEN 'url' THEN 1
                                WHEN 'paypal_url' THEN 2
                                WHEN 'provider_redirect_url' THEN 3
                                WHEN 'approval_url' THEN 4
                                WHEN 'checkout_url' THEN 5
                                WHEN 'cashier_url' THEN 6
                                ELSE 99
                            END
                            LIMIT 1
                        ),
                        ''
                    )) AS last_payment_link_url,
                    trim(coalesce(
                        (
                            SELECT trim(CAST(value AS TEXT))
                            FROM json_each(
                                CASE
                                    WHEN json_type(extra, '$.chatgpt_paypal_url') = 'object'
                                    THEN json_extract(extra, '$.chatgpt_paypal_url')
                                    ELSE '{}'
                                END
                            )
                            WHERE key IN ('url', 'paypal_url', 'provider_redirect_url', 'approval_url', 'checkout_url', 'cashier_url')
                              AND (
                                  (
                                      lower(trim(CAST(value AS TEXT))) LIKE 'http://%'
                                      AND length(trim(CAST(value AS TEXT))) BETWEEN 8 AND 8192
                                      AND substr(lower(trim(CAST(value AS TEXT))), 8, 1) NOT IN ('', '/', '?', '#')
                                      AND trim(CAST(value AS TEXT)) NOT LIKE '% %'
                                  )
                                  OR (
                                      lower(trim(CAST(value AS TEXT))) LIKE 'https://%'
                                      AND length(trim(CAST(value AS TEXT))) BETWEEN 9 AND 8192
                                      AND substr(lower(trim(CAST(value AS TEXT))), 9, 1) NOT IN ('', '/', '?', '#')
                                      AND trim(CAST(value AS TEXT)) NOT LIKE '% %'
                                  )
                              )
                            ORDER BY CASE key
                                WHEN 'url' THEN 1
                                WHEN 'paypal_url' THEN 2
                                WHEN 'provider_redirect_url' THEN 3
                                WHEN 'approval_url' THEN 4
                                WHEN 'checkout_url' THEN 5
                                WHEN 'cashier_url' THEN 6
                                ELSE 99
                            END
                            LIMIT 1
                        ),
                        ''
                    )) AS legacy_paypal_link_url,
                    replace(lower(trim(coalesce(
                        nullif(trim(CAST(CASE
                            WHEN lower(trim(coalesce(json_extract(extra, '$.chatgpt_last_payment_link.link_status'), ''))) IN ('expired_cleaned', 'paid_cleaned', 'cancelled_cleaned', 'upi_expired_cleaned', 'upi_paid_cleaned', 'upi_cancelled_cleaned', 'ideal_expired_cleaned', 'ideal_paid_cleaned', 'ideal_cancelled_cleaned', 'payment_link_deleted')
                            THEN NULL
                            ELSE json_extract(extra, '$.chatgpt_last_payment_link.link_type')
                        END AS TEXT)), ''),
                        nullif(trim(CAST(json_extract(extra, '$.chatgpt_paypal_url.link_type') AS TEXT)), ''),
                        CASE WHEN json_type(extra, '$.chatgpt_paypal_url') = 'object' THEN 'paypal' ELSE '' END,
                        ''
                    ))), '-', '_') AS payment_link_type,
                    replace(lower(trim(coalesce(
                        nullif(trim(CAST(CASE
                            WHEN lower(trim(coalesce(json_extract(extra, '$.chatgpt_last_payment_link.link_status'), ''))) IN ('expired_cleaned', 'paid_cleaned', 'cancelled_cleaned', 'upi_expired_cleaned', 'upi_paid_cleaned', 'upi_cancelled_cleaned', 'ideal_expired_cleaned', 'ideal_paid_cleaned', 'ideal_cancelled_cleaned', 'payment_link_deleted')
                            THEN NULL
                            ELSE json_extract(extra, '$.chatgpt_last_payment_link.payment_method_type')
                        END AS TEXT)), ''),
                        nullif(trim(CAST(json_extract(extra, '$.chatgpt_paypal_url.payment_method_type') AS TEXT)), ''),
                        ''
                    ))), '-', '_') AS payment_link_method_type,
                    replace(lower(trim(coalesce(
                        nullif(trim(CAST(json_extract(extra, '$.chatgpt_last_payment_link.generation_kind') AS TEXT)), ''),
                        ''
                    ))), '-', '_') AS payment_link_generation_kind,
                    replace(lower(trim(coalesce(
                        nullif(trim(CAST(json_extract(extra, '$.chatgpt_last_payment_link.plan') AS TEXT)), ''),
                        ''
                    ))), '-', '_') AS payment_link_plan,
                    replace(lower(trim(coalesce(
                        nullif(trim(CAST(json_extract(extra, '$.chatgpt_last_payment_link.plan_name') AS TEXT)), ''),
                        ''
                    ))), '-', '_') AS payment_link_plan_name,
                    replace(lower(trim(coalesce(
                        nullif(trim(CAST(CASE
                            WHEN lower(trim(coalesce(json_extract(extra, '$.chatgpt_last_payment_link.link_status'), ''))) IN ('expired_cleaned', 'paid_cleaned', 'cancelled_cleaned', 'upi_expired_cleaned', 'upi_paid_cleaned', 'upi_cancelled_cleaned', 'ideal_expired_cleaned', 'ideal_paid_cleaned', 'ideal_cancelled_cleaned', 'payment_link_deleted')
                            THEN NULL
                            ELSE json_extract(extra, '$.chatgpt_last_payment_link.payment_link_format')
                        END AS TEXT)), ''),
                        nullif(trim(CAST(json_extract(extra, '$.chatgpt_paypal_url.payment_link_format') AS TEXT)), ''),
                        ''
                    ))), '-', '_') AS payment_link_format,
                    replace(lower(trim(coalesce(
                        nullif(trim(CAST(CASE
                            WHEN lower(trim(coalesce(json_extract(extra, '$.chatgpt_last_payment_link.link_status'), ''))) IN ('expired_cleaned', 'paid_cleaned', 'cancelled_cleaned', 'upi_expired_cleaned', 'upi_paid_cleaned', 'upi_cancelled_cleaned', 'ideal_expired_cleaned', 'ideal_paid_cleaned', 'ideal_cancelled_cleaned', 'payment_link_deleted')
                            THEN NULL
                            ELSE json_extract(extra, '$.chatgpt_last_payment_link.payment_source')
                        END AS TEXT)), ''),
                        nullif(trim(CAST(json_extract(extra, '$.chatgpt_paypal_url.payment_source') AS TEXT)), ''),
                        ''
                    ))), '-', '_') AS payment_link_source,
                    lower(trim(coalesce(
                        nullif(trim(CAST(json_extract(extra, '$.chatgpt_last_payment_link.link_status') AS TEXT)), ''),
                        ''
                    ))) AS payment_link_status,
                    CASE
                        WHEN EXISTS (
                            SELECT 1
                            FROM payment_link_generations AS generation
                            WHERE generation.account_id = account_json.account_id
                              AND lower(trim(coalesce(generation.account_email, ''))) = account_json.account_email
                              AND trim(coalesce(generation.account_created_at, '')) = account_json.account_created_at
                              AND lower(trim(coalesce(generation.status, ''))) = 'succeeded'
                              AND length(trim(coalesce(generation.url, ''))) BETWEEN 8 AND 8192
                              AND (
                                  (
                                      lower(trim(generation.url)) LIKE 'http://%'
                                      AND substr(lower(trim(generation.url)), 8, 1) NOT IN ('', '/', '?', '#')
                                      AND trim(generation.url) NOT LIKE '% %'
                                  )
                                  OR (
                                      lower(trim(generation.url)) LIKE 'https://%'
                                      AND substr(lower(trim(generation.url)), 9, 1) NOT IN ('', '/', '?', '#')
                                      AND trim(generation.url) NOT LIKE '% %'
                                  )
                              )
                        ) THEN 1
                        ELSE 0
                    END AS payment_generation_succeeded,
                    lower(trim(coalesce(
                        json_extract(extra, '$.chatgpt_capabilities.auth_level'),
                        json_extract(extra, '$.auth_level'),
                        ''
                    ))) AS auth_level,
                    lower(trim(coalesce(json_extract(extra, '$.chatgpt_capabilities.upload_gate'), ''))) AS upload_gate,
                    lower(trim(coalesce(json_extract(extra, '$.chatgpt_local.auth.state'), ''))) AS auth_state,
                    CAST(coalesce(json_extract(extra, '$.chatgpt_local.auth.http_status'), 0) AS INTEGER) AS auth_http_status,
                    lower(trim(coalesce(json_extract(extra, '$.chatgpt_local.codex.state'), ''))) AS codex_state,
                    CAST(coalesce(json_extract(extra, '$.chatgpt_local.codex.http_status'), 0) AS INTEGER) AS codex_http_status,
                    replace(lower(trim(coalesce(json_extract(extra, '$.chatgpt_capabilities.subscription_plan'), ''))), '-', '_') AS cap_subscription_plan,
                    CASE
                        WHEN lower(trim(CAST(coalesce(json_extract(extra, '$.chatgpt_capabilities.subscription_checked'), '') AS TEXT)))
                             IN ('1', 'true', 'yes', 'on')
                        THEN 1
                        ELSE 0
                    END AS cap_subscription_checked,
                    replace(lower(trim(coalesce(json_extract(extra, '$.chatgpt_local.subscription.plan'), ''))), '-', '_') AS local_subscription_plan,
                    replace(lower(trim(coalesce(json_extract(extra, '$.chatgpt_plan_type'), ''))), '-', '_') AS plan_type,
                    replace(lower(trim(coalesce(json_extract(extra, '$.chatgpt_subscription_plan'), ''))), '-', '_') AS extra_subscription_plan,
                    lower(trim(coalesce(json_extract(extra, '$.chatgpt_workspace_scope'), ''))) AS workspace_scope,
                    lower(trim(coalesce(json_extract(extra, '$.sync_statuses.sub2api.remote_state'), ''))) AS sub2api_remote_state,
                    lower(trim(CAST(coalesce(json_extract(extra, '$.sync_statuses.sub2api.uploaded'), '') AS TEXT))) AS sub2api_uploaded_marker,
                    lower(trim(coalesce(json_extract(extra, '$.sync_statuses.sub2api.last_upload.status'), ''))) AS sub2api_last_upload_status,
                    lower(trim(coalesce(json_extract(extra, '$.sync_statuses.oaipay.remote_state'), ''))) AS oaipay_remote_state,
                    lower(trim(CAST(coalesce(json_extract(extra, '$.sync_statuses.oaipay.uploaded'), '') AS TEXT))) AS oaipay_uploaded_marker,
                    lower(trim(coalesce(json_extract(extra, '$.sync_statuses.oaipay.last_upload.status'), ''))) AS oaipay_last_upload_status,
                    lower(trim(coalesce(json_extract(extra, '$.baxigpt_cdk.status'), ''))) AS baxigpt_cdk_status,
                    lower(trim(coalesce(json_extract(extra, '$.idea_submit.status'), ''))) AS idea_marker_status,
                    trim(coalesce(json_extract(extra, '$.baxigpt_cdk.order_id'), '')) AS baxigpt_order_id,
                    trim(coalesce(json_extract(extra, '$.baxigpt_cdk.display_id'), '')) AS baxigpt_display_id,
                    trim(coalesce(json_extract(extra, '$.idea_submit.order_id'), '')) AS idea_marker_order_id,
                    trim(coalesce(json_extract(extra, '$.idea_submit.display_id'), '')) AS idea_marker_display_id,
                    CASE
                        WHEN lower(trim(CAST(coalesce(json_extract(extra, '$.idea_submit.unavailable'), '') AS TEXT)))
                             IN ('1', 'true', 'yes', 'on')
                        THEN 1
                        ELSE 0
                    END AS idea_marker_unavailable,
                    CASE
                        WHEN lower(trim(CAST(coalesce(json_extract(extra, '$.idea_submit_unavailable'), '') AS TEXT)))
                             IN ('1', 'true', 'yes', 'on')
                        THEN 1
                        ELSE 0
                    END AS idea_submit_unavailable,
                    CASE
                        WHEN lower(trim(CAST(coalesce(json_extract(extra, '$.chatgpt_account_unavailable'), '') AS TEXT)))
                             IN ('1', 'true', 'yes', 'on')
                        THEN 1
                        ELSE 0
                    END AS chatgpt_account_unavailable,
                    coalesce(
                        nullif(trim(CAST(json_extract(extra, '$.chatgpt_local.subscription.subscription_active_until') AS TEXT)), ''),
                        nullif(trim(CAST(json_extract(extra, '$.chatgpt_local.subscription.subscription_expires_at_iso') AS TEXT)), ''),
                        nullif(trim(CAST(json_extract(extra, '$.chatgpt_local.subscription.subscription_expires_at') AS TEXT)), ''),
                        nullif(trim(CAST(json_extract(extra, '$.subscription_active_until') AS TEXT)), ''),
                        nullif(trim(CAST(json_extract(extra, '$.subscription_expires_at') AS TEXT)), ''),
                        nullif(trim(CAST(json_extract(extra, '$.chatgpt_subscription_active_until') AS TEXT)), ''),
                        ''
                    ) AS raw_subscription_active_until,
                    CASE WHEN json_type(extra, '$.chatgpt_last_revival') = 'object' THEN 1 ELSE 0 END AS last_revival_present,
                    lower(trim(coalesce(json_extract(extra, '$.chatgpt_last_revival.source'), ''))) AS last_revival_source,
                    trim(coalesce(json_extract(extra, '$.chatgpt_last_revival.task_id'), '')) AS last_revival_task_id,
                    CASE
                        WHEN lower(trim(coalesce(json_extract(extra, '$.chatgpt_last_revival.mode'), '')))
                             IN ('revive_existing', 'create_new')
                        THEN lower(trim(coalesce(json_extract(extra, '$.chatgpt_last_revival.mode'), '')))
                        ELSE ''
                    END AS last_revival_mode,
                    CASE WHEN json_type(extra, '$.chatgpt_invalid_recheck.revival_marker') = 'object' THEN 1 ELSE 0 END AS invalid_marker_present,
                    lower(trim(coalesce(json_extract(extra, '$.chatgpt_invalid_recheck.revival_marker.source'), ''))) AS invalid_marker_source,
                    trim(coalesce(json_extract(extra, '$.chatgpt_invalid_recheck.revival_marker.task_id'), '')) AS invalid_marker_task_id,
                    CASE
                        WHEN lower(trim(coalesce(json_extract(extra, '$.chatgpt_invalid_recheck.revival_marker.mode'), '')))
                             IN ('revive_existing', 'create_new')
                        THEN lower(trim(coalesce(json_extract(extra, '$.chatgpt_invalid_recheck.revival_marker.mode'), '')))
                        ELSE ''
                    END AS invalid_marker_mode,
                    lower(trim(coalesce(json_extract(extra, '$.chatgpt_invalid_recheck.status'), ''))) AS invalid_recheck_status,
                    lower(trim(coalesce(json_extract(extra, '$.chatgpt_invalid_recheck.source'), 'invalid_account_recheck'))) AS invalid_recheck_source,
                    trim(coalesce(json_extract(extra, '$.chatgpt_invalid_recheck.task_id'), '')) AS invalid_recheck_task_id,
                    CASE WHEN json_type(extra, '$.chatgpt_custom_email_recheck.revival_marker') = 'object' THEN 1 ELSE 0 END AS custom_marker_present,
                    lower(trim(coalesce(json_extract(extra, '$.chatgpt_custom_email_recheck.revival_marker.source'), ''))) AS custom_marker_source,
                    trim(coalesce(json_extract(extra, '$.chatgpt_custom_email_recheck.revival_marker.task_id'), '')) AS custom_marker_task_id,
                    CASE
                        WHEN lower(trim(coalesce(json_extract(extra, '$.chatgpt_custom_email_recheck.revival_marker.mode'), '')))
                             IN ('revive_existing', 'create_new')
                        THEN lower(trim(coalesce(json_extract(extra, '$.chatgpt_custom_email_recheck.revival_marker.mode'), '')))
                        ELSE ''
                    END AS custom_marker_mode,
                    CASE
                        WHEN lower(trim(CAST(coalesce(json_extract(extra, '$.chatgpt_custom_email_recheck.revived_existing_account'), '') AS TEXT)))
                             IN ('1', 'true', 'yes', 'on')
                        THEN 1
                        ELSE 0
                    END AS custom_revived_existing,
                    CASE
                        WHEN lower(trim(CAST(coalesce(json_extract(extra, '$.chatgpt_custom_email_recheck.created_new_account'), '') AS TEXT)))
                             IN ('1', 'true', 'yes', 'on')
                        THEN 1
                        ELSE 0
                    END AS custom_created_new,
                    lower(trim(coalesce(json_extract(extra, '$.chatgpt_custom_email_recheck.source'), 'custom_email_recheck'))) AS custom_recheck_source,
                    trim(coalesce(json_extract(extra, '$.chatgpt_custom_email_recheck.task_id'), '')) AS custom_recheck_task_id
                FROM account_json
            ),
            revival_inputs AS (
                SELECT
                    *,
                    trim(coalesce(
                        nullif(last_payment_link_url, ''),
                        nullif(legacy_paypal_link_url, ''),
                        ''
                    )) AS payment_link_url,
                    CASE
                        WHEN (
                            (
                                lower(trim(coalesce(nullif(last_payment_link_url, ''), nullif(legacy_paypal_link_url, ''), ''))) LIKE 'http://%'
                                AND length(trim(coalesce(nullif(last_payment_link_url, ''), nullif(legacy_paypal_link_url, ''), ''))) BETWEEN 8 AND 8192
                                AND substr(lower(trim(coalesce(nullif(last_payment_link_url, ''), nullif(legacy_paypal_link_url, ''), ''))), 8, 1) NOT IN ('', '/', '?', '#')
                                AND trim(coalesce(nullif(last_payment_link_url, ''), nullif(legacy_paypal_link_url, ''), '')) NOT LIKE '% %'
                            )
                            OR (
                                lower(trim(coalesce(nullif(last_payment_link_url, ''), nullif(legacy_paypal_link_url, ''), ''))) LIKE 'https://%'
                                AND length(trim(coalesce(nullif(last_payment_link_url, ''), nullif(legacy_paypal_link_url, ''), ''))) BETWEEN 9 AND 8192
                                AND substr(lower(trim(coalesce(nullif(last_payment_link_url, ''), nullif(legacy_paypal_link_url, ''), ''))), 9, 1) NOT IN ('', '/', '?', '#')
                                AND trim(coalesce(nullif(last_payment_link_url, ''), nullif(legacy_paypal_link_url, ''), '')) NOT LIKE '% %'
                            )
                        ) THEN 1
                        ELSE 0
                    END AS payment_link_url_valid,
                    CASE
                        WHEN lower(last_payment_link_url) NOT LIKE 'http://%'
                             AND lower(last_payment_link_url) NOT LIKE 'https://%'
                             AND (
                                 (
                                     lower(legacy_paypal_link_url) LIKE 'http://%'
                                     AND length(trim(legacy_paypal_link_url)) BETWEEN 8 AND 8192
                                     AND substr(lower(trim(legacy_paypal_link_url)), 8, 1) NOT IN ('', '/', '?', '#')
                                     AND trim(legacy_paypal_link_url) NOT LIKE '% %'
                                 )
                                 OR (
                                     lower(legacy_paypal_link_url) LIKE 'https://%'
                                     AND length(trim(legacy_paypal_link_url)) BETWEEN 9 AND 8192
                                     AND substr(lower(trim(legacy_paypal_link_url)), 9, 1) NOT IN ('', '/', '?', '#')
                                     AND trim(legacy_paypal_link_url) NOT LIKE '% %'
                                 )
                             )
                        THEN 1
                        ELSE 0
                    END AS payment_link_uses_legacy_paypal,
                    CASE
                        WHEN last_revival_present = 1 THEN 1
                        WHEN invalid_marker_present = 1 THEN 1
                        WHEN invalid_recheck_status = 'recovered_access_token' THEN 1
                        WHEN custom_marker_present = 1 THEN 1
                        WHEN custom_revived_existing = 1 OR custom_created_new = 1 THEN 1
                        ELSE 0
                    END AS has_revival,
                    CASE
                        WHEN last_revival_present = 1 THEN last_revival_source
                        WHEN invalid_marker_present = 1 THEN invalid_marker_source
                        WHEN invalid_recheck_status = 'recovered_access_token' THEN invalid_recheck_source
                        WHEN custom_marker_present = 1 THEN custom_marker_source
                        WHEN custom_revived_existing = 1 OR custom_created_new = 1 THEN custom_recheck_source
                        ELSE ''
                    END AS revival_source,
                    CASE
                        WHEN last_revival_present = 1 THEN last_revival_task_id
                        WHEN invalid_marker_present = 1 THEN invalid_marker_task_id
                        WHEN invalid_recheck_status = 'recovered_access_token' THEN invalid_recheck_task_id
                        WHEN custom_marker_present = 1 THEN custom_marker_task_id
                        WHEN custom_revived_existing = 1 OR custom_created_new = 1 THEN custom_recheck_task_id
                        ELSE ''
                    END AS revival_task_id,
                    CASE
                        WHEN last_revival_present = 1 THEN last_revival_mode
                        WHEN invalid_marker_present = 1 THEN invalid_marker_mode
                        WHEN invalid_recheck_status = 'recovered_access_token' THEN 'revive_existing'
                        WHEN custom_marker_present = 1 THEN custom_marker_mode
                        WHEN custom_created_new = 1 THEN 'create_new'
                        WHEN custom_revived_existing = 1 THEN 'revive_existing'
                        ELSE ''
                    END AS revival_mode
                FROM extracted
            ),
            derived AS (
                SELECT
                    *,
                    CASE
                        WHEN refresh_token_value != '' THEN 'refresh_token'
                        WHEN access_token_value != '' THEN 'access_token_only'
                        ELSE 'unknown'
                    END AS derived_auth_type,
                    (
                        length(phone_binding_phone) - length(replace(phone_binding_phone, '0', ''))
                        + length(phone_binding_phone) - length(replace(phone_binding_phone, '1', ''))
                        + length(phone_binding_phone) - length(replace(phone_binding_phone, '2', ''))
                        + length(phone_binding_phone) - length(replace(phone_binding_phone, '3', ''))
                        + length(phone_binding_phone) - length(replace(phone_binding_phone, '4', ''))
                        + length(phone_binding_phone) - length(replace(phone_binding_phone, '5', ''))
                        + length(phone_binding_phone) - length(replace(phone_binding_phone, '6', ''))
                        + length(phone_binding_phone) - length(replace(phone_binding_phone, '7', ''))
                        + length(phone_binding_phone) - length(replace(phone_binding_phone, '8', ''))
                        + length(phone_binding_phone) - length(replace(phone_binding_phone, '9', ''))
                    ) AS phone_binding_digit_count,
                    CASE
                        WHEN account_status = 'invalid'
                            OR auth_level = 'invalid'
                            OR upload_gate = 'blocked_auth_invalid'
                            OR auth_state IN (
                                'refresh_token_invalidated',
                                'access_token_invalidated',
                                'unauthorized',
                                'account_deactivated',
                                'banned_like'
                            )
                            OR codex_state IN (
                                'refresh_token_invalidated',
                                'access_token_invalidated',
                                'unauthorized',
                                'account_deactivated',
                                'banned_like'
                            )
                            OR auth_http_status = 401
                            OR codex_http_status = 401
                        THEN 'invalid'
                        WHEN auth_state = 'probe_failed' OR codex_state = 'probe_failed'
                        THEN 'refresh_failed'
                        WHEN account_status != 'invalid'
                            AND auth_level = ''
                            AND auth_state = ''
                        THEN 'not_checked'
                        ELSE 'valid'
                    END AS derived_account_validity,
                    CASE
                        WHEN local_subscription_plan LIKE '%enterprise%' THEN 'enterprise'
                        WHEN local_subscription_plan LIKE '%team%' OR local_subscription_plan LIKE '%business%' THEN 'team'
                        WHEN local_subscription_plan LIKE '%pro%' THEN 'pro'
                        WHEN local_subscription_plan LIKE '%plus%' THEN 'plus'
                        WHEN local_subscription_plan LIKE '%free%' THEN 'free'
                        WHEN cap_subscription_checked = 1 AND cap_subscription_plan LIKE '%enterprise%' THEN 'enterprise'
                        WHEN cap_subscription_checked = 1 AND (cap_subscription_plan LIKE '%team%' OR cap_subscription_plan LIKE '%business%') THEN 'team'
                        WHEN cap_subscription_checked = 1 AND cap_subscription_plan LIKE '%pro%' THEN 'pro'
                        WHEN cap_subscription_checked = 1 AND cap_subscription_plan LIKE '%plus%' THEN 'plus'
                        WHEN cap_subscription_checked = 1 AND cap_subscription_plan LIKE '%free%' THEN 'free'
                        ELSE 'unknown'
                    END AS derived_subscription_type,
                    CASE
                        WHEN has_revival = 0 THEN 'none'
                        WHEN revival_source = 'invalid_account_recheck' THEN
                            CASE
                                WHEN revival_task_id = 'icloud_hme_auto_delete' THEN 'auto_delete_recheck'
                                ELSE 'invalid_recheck'
                            END
                        WHEN revival_source = 'custom_email_recheck' THEN
                            CASE
                                WHEN revival_mode = 'create_new' THEN 'custom_email_recheck_new'
                                ELSE 'custom_email_recheck'
                            END
                        WHEN revival_mode = 'create_new' THEN 'custom_email_recheck_new'
                        WHEN revival_source != '' THEN revival_source
                        ELSE 'unknown'
                    END AS derived_revival_kind
                FROM revival_inputs
            ),
            final_rows AS (
                SELECT
                    account_id,
                    platform,
                    manually_used,
                    derived_auth_type AS auth_type,
                    CASE
                        WHEN phone_binding_present = 1
                             AND phone_binding_status IN ('bound', 'success', 'completed')
                             AND phone_binding_digit_count >= 8
                        THEN 'confirmed'
                        WHEN phone_binding_present = 1
                             OR bound_phone_present = 1
                             OR bound_phone_number != ''
                             OR bound_phone_masked != ''
                             OR phone_challenge_present = 1
                        THEN 'unconfirmed'
                        ELSE 'unknown'
                    END AS phone_binding_state,
                    CASE
                        WHEN (
                            (
                                lower(payment_link_url) LIKE 'http://%'
                                AND length(trim(payment_link_url)) BETWEEN 8 AND 8192
                                AND substr(lower(trim(payment_link_url)), 8, 1) NOT IN ('', '/', '?', '#')
                                AND trim(payment_link_url) NOT LIKE '% %'
                            )
                            OR (
                                lower(payment_link_url) LIKE 'https://%'
                                AND length(trim(payment_link_url)) BETWEEN 9 AND 8192
                                AND substr(lower(trim(payment_link_url)), 9, 1) NOT IN ('', '/', '?', '#')
                                AND trim(payment_link_url) NOT LIKE '% %'
                            )
                            OR (
                                lower(legacy_paypal_link_url) LIKE 'http://%'
                                AND length(trim(legacy_paypal_link_url)) BETWEEN 8 AND 8192
                                AND substr(lower(trim(legacy_paypal_link_url)), 8, 1) NOT IN ('', '/', '?', '#')
                                AND trim(legacy_paypal_link_url) NOT LIKE '% %'
                            )
                            OR (
                                lower(legacy_paypal_link_url) LIKE 'https://%'
                                AND length(trim(legacy_paypal_link_url)) BETWEEN 9 AND 8192
                                AND substr(lower(trim(legacy_paypal_link_url)), 9, 1) NOT IN ('', '/', '?', '#')
                                AND trim(legacy_paypal_link_url) NOT LIKE '% %'
                            )
                            OR payment_link_status IN ('expired_cleaned', 'paid_cleaned', 'cancelled_cleaned', 'upi_expired_cleaned', 'upi_paid_cleaned', 'upi_cancelled_cleaned', 'ideal_expired_cleaned', 'ideal_paid_cleaned', 'ideal_cancelled_cleaned', 'payment_link_deleted')
                            OR payment_generation_succeeded = 1
                        )
                        THEN 1
                        ELSE 0
                    END AS payment_link_generated,
                    CASE
                        WHEN payment_link_status IN ('expired_cleaned', 'paid_cleaned', 'cancelled_cleaned', 'upi_expired_cleaned', 'upi_paid_cleaned', 'upi_cancelled_cleaned', 'ideal_expired_cleaned', 'ideal_paid_cleaned', 'ideal_cancelled_cleaned', 'payment_link_deleted')
                        THEN 'none'
                        WHEN payment_link_uses_legacy_paypal = 1
                        THEN 'paypal'
                        WHEN payment_link_url_valid = 0
                        THEN 'none'
                        WHEN payment_link_type IN ('team', 'team_checkout', 'chatgptteamplan')
                             OR payment_link_generation_kind = 'team_checkout'
                             OR payment_link_plan = 'team'
                             OR payment_link_plan_name = 'chatgptteamplan'
                        THEN 'team'
                        WHEN payment_link_type IN ('upi', 'upi_qr', 'upi_qr_code')
                             OR payment_link_method_type IN ('upi', 'upi_qr', 'upi_qr_code')
                        THEN 'upi'
                        WHEN payment_link_type IN ('pix', 'qr', 'pix_qr')
                             OR payment_link_method_type IN ('pix', 'qr', 'pix_qr')
                        THEN 'pix'
                        WHEN lower(payment_link_url) LIKE '%/upi/instructions/%'
                        THEN 'upi'
                        WHEN lower(payment_link_url) LIKE '%/qr/instructions/%'
                        THEN 'pix'
                        WHEN payment_link_type IN ('paypal', 'pp', 'paypal_url')
                        THEN 'paypal'
                        WHEN payment_link_type IN ('ideal', 'ideal_pay')
                        THEN 'ideal'
                        WHEN payment_link_type = 'twint'
                        THEN 'twint'
                        WHEN payment_link_type IN ('kakao', 'kakaopay', 'kakao_pay')
                        THEN 'kakao_pay'
                        WHEN payment_link_method_type IN ('paypal', 'pp', 'paypal_url')
                        THEN 'paypal'
                        WHEN payment_link_method_type IN ('ideal', 'ideal_pay')
                        THEN 'ideal'
                        WHEN payment_link_method_type = 'twint'
                        THEN 'twint'
                        WHEN payment_link_method_type IN ('kakao', 'kakaopay', 'kakao_pay')
                        THEN 'kakao_pay'
                        WHEN payment_link_format IN ('paypal', 'paypal_url', 'paypal_approval', 'provider_url')
                             OR payment_link_source LIKE '%paypal%'
                             OR lower(payment_link_url) LIKE 'http://paypal.com'
                             OR lower(payment_link_url) LIKE 'https://paypal.com'
                             OR lower(payment_link_url) LIKE 'http://paypal.com/%'
                             OR lower(payment_link_url) LIKE 'https://paypal.com/%'
                             OR lower(payment_link_url) LIKE 'http://paypal.com?%'
                             OR lower(payment_link_url) LIKE 'https://paypal.com?%'
                             OR lower(payment_link_url) LIKE 'http://paypal.com#%'
                             OR lower(payment_link_url) LIKE 'https://paypal.com#%'
                             OR lower(payment_link_url) LIKE 'http://%.paypal.com'
                             OR lower(payment_link_url) LIKE 'https://%.paypal.com'
                             OR lower(payment_link_url) LIKE 'http://%.paypal.com/%'
                             OR lower(payment_link_url) LIKE 'https://%.paypal.com/%'
                             OR lower(payment_link_url) LIKE 'http://%.paypal.com?%'
                             OR lower(payment_link_url) LIKE 'https://%.paypal.com?%'
                             OR lower(payment_link_url) LIKE 'http://%.paypal.com#%'
                             OR lower(payment_link_url) LIKE 'https://%.paypal.com#%'
                        THEN 'paypal'
                        WHEN payment_link_format IN ('ideal', 'ideal_url')
                             OR lower(payment_link_url) LIKE 'http://pay.ideal.nl/transactions/%'
                             OR lower(payment_link_url) LIKE 'https://pay.ideal.nl/transactions/%'
                        THEN 'ideal'
                        WHEN payment_link_format IN ('twint', 'twint_url')
                             OR lower(payment_link_url) LIKE 'http://twint.ch/%'
                             OR lower(payment_link_url) LIKE 'https://twint.ch/%'
                             OR lower(payment_link_url) LIKE 'http://%.twint.ch/%'
                             OR lower(payment_link_url) LIKE 'https://%.twint.ch/%'
                        THEN 'twint'
                        WHEN payment_link_format IN ('kakao_pay', 'kakao_pay_url')
                             OR lower(payment_link_url) LIKE 'http://kakao.com/%'
                             OR lower(payment_link_url) LIKE 'https://kakao.com/%'
                             OR lower(payment_link_url) LIKE 'http://%.kakao.com/%'
                             OR lower(payment_link_url) LIKE 'https://%.kakao.com/%'
                             OR lower(payment_link_url) LIKE 'http://kakaopay.com/%'
                             OR lower(payment_link_url) LIKE 'https://kakaopay.com/%'
                             OR lower(payment_link_url) LIKE 'http://%.kakaopay.com/%'
                             OR lower(payment_link_url) LIKE 'https://%.kakaopay.com/%'
                             OR lower(payment_link_url) LIKE 'http://kakaopay.co.kr/%'
                             OR lower(payment_link_url) LIKE 'https://kakaopay.co.kr/%'
                             OR lower(payment_link_url) LIKE 'http://%.kakaopay.co.kr/%'
                             OR lower(payment_link_url) LIKE 'https://%.kakaopay.co.kr/%'
                             OR lower(payment_link_url) LIKE 'http://nicepay.com/%'
                             OR lower(payment_link_url) LIKE 'https://nicepay.com/%'
                             OR lower(payment_link_url) LIKE 'http://%.nicepay.com/%'
                             OR lower(payment_link_url) LIKE 'https://%.nicepay.com/%'
                             OR lower(payment_link_url) LIKE 'http://nicepay.co.kr/%'
                             OR lower(payment_link_url) LIKE 'https://nicepay.co.kr/%'
                             OR lower(payment_link_url) LIKE 'http://%.nicepay.co.kr/%'
                             OR lower(payment_link_url) LIKE 'https://%.nicepay.co.kr/%'
                        THEN 'kakao_pay'
                        WHEN payment_link_type IN ('hosted', 'payment', 'pay', 'long', 'chatgpt', 'chatgpt_hosted', 'stripe_hosted', 'checkout')
                             OR payment_link_method_type IN ('hosted', 'payment', 'pay', 'long', 'chatgpt', 'chatgpt_hosted', 'stripe_hosted', 'checkout')
                             OR payment_link_format IN ('short', 'short_chatgpt', 'long', 'long_hosted', 'hosted', 'hosted_checkout', 'pay_openai', 'stripe_hosted')
                             OR payment_link_source = 'chatgpt_hosted'
                             OR lower(payment_link_url) LIKE 'http://chatgpt.com'
                             OR lower(payment_link_url) LIKE 'https://chatgpt.com'
                             OR lower(payment_link_url) LIKE 'http://chatgpt.com/%'
                             OR lower(payment_link_url) LIKE 'https://chatgpt.com/%'
                             OR lower(payment_link_url) LIKE 'http://chatgpt.com?%'
                             OR lower(payment_link_url) LIKE 'https://chatgpt.com?%'
                             OR lower(payment_link_url) LIKE 'http://chatgpt.com#%'
                             OR lower(payment_link_url) LIKE 'https://chatgpt.com#%'
                             OR lower(payment_link_url) LIKE 'http://%.chatgpt.com'
                             OR lower(payment_link_url) LIKE 'https://%.chatgpt.com'
                             OR lower(payment_link_url) LIKE 'http://%.chatgpt.com/%'
                             OR lower(payment_link_url) LIKE 'https://%.chatgpt.com/%'
                             OR lower(payment_link_url) LIKE 'http://pay.openai.com'
                             OR lower(payment_link_url) LIKE 'https://pay.openai.com'
                             OR lower(payment_link_url) LIKE 'http://pay.openai.com/%'
                             OR lower(payment_link_url) LIKE 'https://pay.openai.com/%'
                             OR lower(payment_link_url) LIKE 'http://pay.openai.com?%'
                             OR lower(payment_link_url) LIKE 'https://pay.openai.com?%'
                             OR lower(payment_link_url) LIKE 'http://pay.openai.com#%'
                             OR lower(payment_link_url) LIKE 'https://pay.openai.com#%'
                             OR lower(payment_link_url) LIKE 'http://%.openai.com'
                             OR lower(payment_link_url) LIKE 'https://%.openai.com'
                             OR lower(payment_link_url) LIKE 'http://%.openai.com/%'
                             OR lower(payment_link_url) LIKE 'https://%.openai.com/%'
                             OR lower(payment_link_url) LIKE 'http://%.openai.com?%'
                             OR lower(payment_link_url) LIKE 'https://%.openai.com?%'
                             OR lower(payment_link_url) LIKE 'http://%.openai.com#%'
                             OR lower(payment_link_url) LIKE 'https://%.openai.com#%'
                        THEN 'hosted'
                        ELSE 'other'
                    END AS payment_link_platform,
                    CASE
                        WHEN lower(trim(coalesce(json_extract(extra, '$.chatgpt_checkout_link_type.state'), ''))) = 'oaics'
                             OR lower(trim(coalesce(json_extract(extra, '$.chatgpt_checkout_link_type.link_type'), ''))) = 'oaics'
                             OR lower(trim(coalesce(json_extract(extra, '$.chatgpt_checkout_link_type.confirmed_state'), ''))) = 'oaics'
                        THEN 'oaics'
                        WHEN lower(trim(coalesce(json_extract(extra, '$.chatgpt_checkout_link_type.state'), ''))) = 'cs'
                             OR lower(trim(coalesce(json_extract(extra, '$.chatgpt_checkout_link_type.link_type'), ''))) = 'cs'
                             OR lower(trim(coalesce(json_extract(extra, '$.chatgpt_checkout_link_type.confirmed_state'), ''))) = 'cs'
                        THEN 'cs'
                        WHEN lower(payment_link_url) LIKE '%oaics_%'
                             OR lower(payment_link_url) LIKE '%/checkout/openai%'
                             OR lower(trim(coalesce(json_extract(extra, '$.chatgpt_last_payment_link.session_id'), ''))) LIKE 'oaics_%'
                        THEN 'oaics'
                        WHEN lower(payment_link_url) LIKE '%cs_%'
                             OR lower(payment_link_url) LIKE '%checkout.stripe.com%'
                             OR lower(trim(coalesce(json_extract(extra, '$.chatgpt_last_payment_link.session_id'), ''))) LIKE 'cs_%'
                        THEN 'cs'
                        WHEN lower(trim(coalesce(json_extract(extra, '$.chatgpt_zero_amount_eligibility.evidence.session_provider'), ''))) = 'open_ai'
                             OR lower(trim(coalesce(json_extract(extra, '$.chatgpt_zero_amount_eligibility.last_attempt.evidence.session_provider'), ''))) = 'open_ai'
                        THEN 'oaics'
                        WHEN lower(trim(coalesce(json_extract(extra, '$.chatgpt_zero_amount_eligibility.evidence.session_provider'), ''))) = 'stripe'
                             OR lower(trim(coalesce(json_extract(extra, '$.chatgpt_zero_amount_eligibility.last_attempt.evidence.session_provider'), ''))) = 'stripe'
                        THEN 'cs'
                        WHEN lower(trim(coalesce(json_extract(extra, '$.chatgpt_gcash_payment_method.evidence.session_provider'), ''))) = 'open_ai'
                             OR lower(trim(coalesce(json_extract(extra, '$.chatgpt_gcash_payment_method.last_attempt.evidence.session_provider'), ''))) = 'open_ai'
                        THEN 'oaics'
                        WHEN lower(trim(coalesce(json_extract(extra, '$.chatgpt_gcash_payment_method.evidence.session_provider'), ''))) = 'stripe'
                             OR lower(trim(coalesce(json_extract(extra, '$.chatgpt_gcash_payment_method.last_attempt.evidence.session_provider'), ''))) = 'stripe'
                        THEN 'cs'
                        ELSE 'none'
                    END AS checkout_link_type,
                    auth_level,
                    derived_subscription_type AS subscription_type,
                    derived_account_validity AS account_validity,
                    CASE
                        WHEN sub2api_uploaded_marker IN ('1', 'true', 'yes', 'on')
                            OR sub2api_remote_state IN ('exists', 'uploaded')
                            OR sub2api_last_upload_status = 'success'
                        THEN 'uploaded'
                        ELSE 'not_uploaded'
                    END AS sub2api_state,
                    CASE
                        WHEN oaipay_uploaded_marker IN ('1', 'true', 'yes', 'on')
                            OR oaipay_remote_state IN ('exists', 'uploaded')
                            OR oaipay_last_upload_status = 'success'
                        THEN 'uploaded'
                        ELSE 'not_uploaded'
                    END AS oaipay_state,
                    CASE
                        WHEN idea_marker_unavailable = 1
                            OR idea_submit_unavailable = 1
                            OR (chatgpt_account_unavailable = 1 AND baxigpt_cdk_status = 'failed')
                        THEN 'unavailable'
                        WHEN baxigpt_cdk_status IN ('paid', 'submitted', 'processing', 'failed', 'timeout', 'stopped')
                        THEN baxigpt_cdk_status
                        ELSE 'available'
                    END AS idea_submit_state,
                    CASE
                        WHEN baxigpt_cdk_status IN ('paid', 'submitted', 'processing', 'failed', 'timeout', 'stopped')
                        THEN baxigpt_cdk_status
                        WHEN idea_marker_status IN ('paid', 'submitted', 'processing', 'failed', 'timeout', 'stopped')
                        THEN idea_marker_status
                        WHEN payment_link_status = 'pix_submitted'
                        THEN 'submitted'
                        WHEN idea_marker_unavailable = 1
                            OR idea_submit_unavailable = 1
                            OR (chatgpt_account_unavailable = 1 AND baxigpt_cdk_status = 'failed')
                        THEN 'unavailable'
                        ELSE 'available'
                    END AS submit_state,
                    CASE
                        WHEN payment_link_status = 'pix_submitted'
                            OR baxigpt_cdk_status IN ('paid', 'submitted', 'processing', 'stopped')
                            OR idea_marker_status IN ('paid', 'submitted', 'processing', 'stopped')
                            OR baxigpt_order_id != ''
                            OR baxigpt_display_id != ''
                            OR idea_marker_order_id != ''
                            OR idea_marker_display_id != ''
                        THEN 1
                        ELSE 0
                    END AS has_submitted,
                    CASE
                        WHEN derived_revival_kind IN ('invalid_recheck', 'auto_delete_recheck', 'custom_email_recheck', 'unknown') THEN 'revived'
                        WHEN derived_revival_kind = 'custom_email_recheck_new' THEN 'recovery_new'
                        ELSE 'none'
                    END AS revival_state,
                    derived_revival_kind AS revival_kind,
                    raw_subscription_active_until AS subscription_active_until,
                    CASE
                        WHEN raw_subscription_active_until = '' THEN NULL
                        WHEN raw_subscription_active_until GLOB '[0-9]*'
                             AND raw_subscription_active_until NOT GLOB '*[^0-9.]*'
                        THEN
                            CASE
                                WHEN CAST(raw_subscription_active_until AS REAL) <= 0 THEN NULL
                                WHEN CAST(raw_subscription_active_until AS REAL) > 1000000000000
                                THEN CAST(raw_subscription_active_until AS REAL) / 1000
                                ELSE CAST(raw_subscription_active_until AS REAL)
                            END
                        ELSE CAST(strftime('%s', replace(raw_subscription_active_until, 'Z', '+00:00')) AS REAL)
                    END AS subscription_active_until_ts,
                    source_updated_at,
                    strftime('%Y-%m-%dT%H:%M:%fZ', 'now') AS refreshed_at,
                    '__ACCOUNT_LIST_STATE_DERIVATION_VERSION__' AS derivation_version
                FROM derived
            )
            INSERT INTO account_list_state (
                account_id,
                platform,
                manually_used,
                auth_type,
                phone_binding_state,
                payment_link_platform,
                payment_link_generated,
                checkout_link_type,
                auth_level,
                subscription_type,
                account_validity,
                sub2api_state,
                oaipay_state,
                idea_submit_state,
                submit_state,
                zero_amount_eligibility_state,
                gcash_payment_method_state,
                has_submitted,
                revival_state,
                revival_kind,
                subscription_active_until,
                subscription_active_until_ts,
                source_updated_at,
                refreshed_at,
                derivation_version
            )
            SELECT
                account_id,
                platform,
                manually_used,
                auth_type,
                phone_binding_state,
                payment_link_platform,
                payment_link_generated,
                checkout_link_type,
                auth_level,
                subscription_type,
                account_validity,
                sub2api_state,
                oaipay_state,
                idea_submit_state,
                submit_state,
                'unknown',
                'unknown',
                has_submitted,
                revival_state,
                revival_kind,
                subscription_active_until,
                subscription_active_until_ts,
                source_updated_at,
                refreshed_at,
                derivation_version
            FROM final_rows
            WHERE 1 = 1
            ON CONFLICT(account_id) DO UPDATE SET
                platform = excluded.platform,
                manually_used = excluded.manually_used,
                auth_type = excluded.auth_type,
                phone_binding_state = excluded.phone_binding_state,
                payment_link_platform = excluded.payment_link_platform,
                payment_link_generated = excluded.payment_link_generated,
                checkout_link_type = excluded.checkout_link_type,
                auth_level = excluded.auth_level,
                subscription_type = excluded.subscription_type,
                account_validity = excluded.account_validity,
                sub2api_state = excluded.sub2api_state,
                oaipay_state = excluded.oaipay_state,
                idea_submit_state = excluded.idea_submit_state,
                submit_state = excluded.submit_state,
                zero_amount_eligibility_state = excluded.zero_amount_eligibility_state,
                gcash_payment_method_state = excluded.gcash_payment_method_state,
                has_submitted = excluded.has_submitted,
                revival_state = excluded.revival_state,
                revival_kind = excluded.revival_kind,
                subscription_active_until = excluded.subscription_active_until,
                subscription_active_until_ts = excluded.subscription_active_until_ts,
                source_updated_at = excluded.source_updated_at,
                refreshed_at = excluded.refreshed_at,
                derivation_version = excluded.derivation_version
            """
                .replace("__ACCOUNT_LIST_STATE_TARGET_WHERE__", target_where)
                .replace("__ACCOUNT_LIST_STATE_DERIVATION_VERSION__", ACCOUNT_LIST_STATE_DERIVATION_VERSION.replace("'", "''"))
        )
    )
    if target_count > 0:
        # Capability markers live in account extra JSON, but the list endpoint
        # must filter through the denormalized state table. Technical attempts
        # intentionally leave ``confirmed_state`` untouched, so this projection
        # never turns a prior positive/negative into a failure.
        session.exec(
            text(
                """
                UPDATE account_list_state
                SET zero_amount_eligibility_state = COALESCE(
                        (
                            SELECT CASE lower(trim(CAST(json_extract(
                                CASE WHEN json_valid(a.extra_json) THEN a.extra_json ELSE '{}' END,
                                '$.chatgpt_zero_amount_eligibility.confirmed_state'
                            ) AS TEXT)))
                                WHEN 'eligible' THEN 'eligible'
                                WHEN 'ineligible' THEN 'ineligible'
                                ELSE 'unknown'
                            END
                            FROM accounts AS a
                            WHERE a.id = account_list_state.account_id
                        ),
                        'unknown'
                    ),
                    gcash_payment_method_state = COALESCE(
                        (
                            SELECT CASE lower(trim(CAST(json_extract(
                                CASE WHEN json_valid(a.extra_json) THEN a.extra_json ELSE '{}' END,
                                '$.chatgpt_gcash_payment_method.confirmed_state'
                            ) AS TEXT)))
                                WHEN 'available' THEN 'available'
                                WHEN 'unavailable' THEN 'unavailable'
                                ELSE 'unknown'
                            END
                            FROM accounts AS a
                            WHERE a.id = account_list_state.account_id
                        ),
                        'unknown'
                    ),
                    checkout_link_type = COALESCE(
                        (
                            SELECT CASE
                                WHEN lower(trim(CAST(json_extract(
                                    CASE WHEN json_valid(a.extra_json) THEN a.extra_json ELSE '{}' END,
                                    '$.chatgpt_checkout_link_type.confirmed_state'
                                ) AS TEXT))) IN ('oaics', 'cs')
                                THEN lower(trim(CAST(json_extract(
                                    CASE WHEN json_valid(a.extra_json) THEN a.extra_json ELSE '{}' END,
                                    '$.chatgpt_checkout_link_type.confirmed_state'
                                ) AS TEXT)))
                                WHEN lower(trim(CAST(json_extract(
                                    CASE WHEN json_valid(a.extra_json) THEN a.extra_json ELSE '{}' END,
                                    '$.chatgpt_checkout_link_type.state'
                                ) AS TEXT))) IN ('oaics', 'cs')
                                THEN lower(trim(CAST(json_extract(
                                    CASE WHEN json_valid(a.extra_json) THEN a.extra_json ELSE '{}' END,
                                    '$.chatgpt_checkout_link_type.state'
                                ) AS TEXT)))
                                WHEN lower(trim(CAST(json_extract(
                                    CASE WHEN json_valid(a.extra_json) THEN a.extra_json ELSE '{}' END,
                                    '$.chatgpt_checkout_link_type.link_type'
                                ) AS TEXT))) IN ('oaics', 'cs')
                                THEN lower(trim(CAST(json_extract(
                                    CASE WHEN json_valid(a.extra_json) THEN a.extra_json ELSE '{}' END,
                                    '$.chatgpt_checkout_link_type.link_type'
                                ) AS TEXT)))
                                WHEN lower(trim(CAST(json_extract(
                                    CASE WHEN json_valid(a.extra_json) THEN a.extra_json ELSE '{}' END,
                                    '$.chatgpt_last_payment_link.url'
                                ) AS TEXT))) LIKE '%oaics_%'
                                     OR lower(trim(CAST(json_extract(
                                    CASE WHEN json_valid(a.extra_json) THEN a.extra_json ELSE '{}' END,
                                    '$.chatgpt_last_payment_link.url'
                                ) AS TEXT))) LIKE '%/checkout/openai%'
                                     OR lower(trim(CAST(json_extract(
                                    CASE WHEN json_valid(a.extra_json) THEN a.extra_json ELSE '{}' END,
                                    '$.chatgpt_last_payment_link.session_id'
                                ) AS TEXT))) LIKE 'oaics_%'
                                     OR lower(trim(CAST(a.cashier_url AS TEXT))) LIKE '%oaics_%'
                                     OR lower(trim(CAST(a.cashier_url AS TEXT))) LIKE '%/checkout/openai%'
                                THEN 'oaics'
                                WHEN lower(trim(CAST(json_extract(
                                    CASE WHEN json_valid(a.extra_json) THEN a.extra_json ELSE '{}' END,
                                    '$.chatgpt_last_payment_link.url'
                                ) AS TEXT))) LIKE '%cs_%'
                                     OR lower(trim(CAST(json_extract(
                                    CASE WHEN json_valid(a.extra_json) THEN a.extra_json ELSE '{}' END,
                                    '$.chatgpt_last_payment_link.url'
                                ) AS TEXT))) LIKE '%checkout.stripe.com%'
                                     OR lower(trim(CAST(json_extract(
                                    CASE WHEN json_valid(a.extra_json) THEN a.extra_json ELSE '{}' END,
                                    '$.chatgpt_last_payment_link.session_id'
                                ) AS TEXT))) LIKE 'cs_%'
                                     OR lower(trim(CAST(a.cashier_url AS TEXT))) LIKE '%cs_%'
                                     OR lower(trim(CAST(a.cashier_url AS TEXT))) LIKE '%checkout.stripe.com%'
                                THEN 'cs'
                                WHEN lower(trim(CAST(json_extract(
                                    CASE WHEN json_valid(a.extra_json) THEN a.extra_json ELSE '{}' END,
                                    '$.chatgpt_zero_amount_eligibility.evidence.session_provider'
                                ) AS TEXT))) = 'open_ai'
                                     OR lower(trim(CAST(json_extract(
                                    CASE WHEN json_valid(a.extra_json) THEN a.extra_json ELSE '{}' END,
                                    '$.chatgpt_zero_amount_eligibility.last_attempt.evidence.session_provider'
                                ) AS TEXT))) = 'open_ai'
                                     OR lower(trim(CAST(json_extract(
                                    CASE WHEN json_valid(a.extra_json) THEN a.extra_json ELSE '{}' END,
                                    '$.chatgpt_zero_amount_eligibility.evidence.session_id'
                                ) AS TEXT))) LIKE 'oaics_%'
                                THEN 'oaics'
                                WHEN lower(trim(CAST(json_extract(
                                    CASE WHEN json_valid(a.extra_json) THEN a.extra_json ELSE '{}' END,
                                    '$.chatgpt_zero_amount_eligibility.evidence.session_provider'
                                ) AS TEXT))) = 'stripe'
                                     OR lower(trim(CAST(json_extract(
                                    CASE WHEN json_valid(a.extra_json) THEN a.extra_json ELSE '{}' END,
                                    '$.chatgpt_zero_amount_eligibility.last_attempt.evidence.session_provider'
                                ) AS TEXT))) = 'stripe'
                                     OR lower(trim(CAST(json_extract(
                                    CASE WHEN json_valid(a.extra_json) THEN a.extra_json ELSE '{}' END,
                                    '$.chatgpt_zero_amount_eligibility.evidence.session_id'
                                ) AS TEXT))) LIKE 'cs_%'
                                THEN 'cs'
                                WHEN lower(trim(CAST(json_extract(
                                    CASE WHEN json_valid(a.extra_json) THEN a.extra_json ELSE '{}' END,
                                    '$.chatgpt_gcash_payment_method.evidence.session_provider'
                                ) AS TEXT))) = 'open_ai'
                                     OR lower(trim(CAST(json_extract(
                                    CASE WHEN json_valid(a.extra_json) THEN a.extra_json ELSE '{}' END,
                                    '$.chatgpt_gcash_payment_method.last_attempt.evidence.session_provider'
                                ) AS TEXT))) = 'open_ai'
                                     OR lower(trim(CAST(json_extract(
                                    CASE WHEN json_valid(a.extra_json) THEN a.extra_json ELSE '{}' END,
                                    '$.chatgpt_gcash_payment_method.evidence.session_id'
                                ) AS TEXT))) LIKE 'oaics_%'
                                THEN 'oaics'
                                WHEN lower(trim(CAST(json_extract(
                                    CASE WHEN json_valid(a.extra_json) THEN a.extra_json ELSE '{}' END,
                                    '$.chatgpt_gcash_payment_method.evidence.session_provider'
                                ) AS TEXT))) = 'stripe'
                                     OR lower(trim(CAST(json_extract(
                                    CASE WHEN json_valid(a.extra_json) THEN a.extra_json ELSE '{}' END,
                                    '$.chatgpt_gcash_payment_method.last_attempt.evidence.session_provider'
                                ) AS TEXT))) = 'stripe'
                                     OR lower(trim(CAST(json_extract(
                                    CASE WHEN json_valid(a.extra_json) THEN a.extra_json ELSE '{}' END,
                                    '$.chatgpt_gcash_payment_method.evidence.session_id'
                                ) AS TEXT))) LIKE 'cs_%'
                                THEN 'cs'
                                ELSE 'none'
                            END
                            FROM accounts AS a
                            WHERE a.id = account_list_state.account_id
                        ),
                        'none'
                    )
                WHERE account_id IN (
                    SELECT id FROM accounts WHERE __ACCOUNT_LIST_STATE_TARGET_WHERE__
                )
                """.replace("__ACCOUNT_LIST_STATE_TARGET_WHERE__", target_where)
            )
        )
    if cleanup_orphans:
        session.exec(
            text(
                """
                DELETE FROM account_list_state
                WHERE account_id NOT IN (SELECT id FROM accounts)
                """
            )
        )
    if commit:
        session.commit()
    return target_count


def refresh_stale_account_list_state(session: Session, *, platform: Any = None, commit: bool = True) -> int:
    """Refresh only missing/stale derived list-state rows.

    A row is stale when its cached ``source_updated_at`` differs from the
    current ``accounts.updated_at`` text.  This keeps derived filters/sorts
    accurate without rebuilding the whole cache on every list request.
    """

    return refresh_account_list_state(
        session,
        stale_only=True,
        platform=platform,
        # Account delete paths remove their cache rows transactionally.  Running
        # an unconditional orphan DELETE here turns every filtered GET into a
        # SQLite writer even when no account state is stale.
        cleanup_orphans=False,
        commit=commit,
    )


def upsert_account_list_state_for_account_ids(
    session: Session,
    account_ids: Any,
    *,
    commit: bool = True,
) -> int:
    """Synchronize derived list state for known account write points."""

    ids = _normalize_account_list_state_ids(account_ids)
    if not ids or not _looks_like_sql_session(session):
        return 0
    try:
        return refresh_account_list_state(
            session,
            account_ids=ids,
            cleanup_orphans=False,
            commit=commit,
        )
    except Exception as exc:
        _log_account_list_state_sync_skip("upsert", ids, exc)
        return 0


def delete_account_list_state_for_account_ids(
    session: Session,
    account_ids: Any,
    *,
    commit: bool = True,
) -> int:
    """Remove derived list-state rows for deleted accounts."""

    ids = _normalize_account_list_state_ids(account_ids)
    if not ids or not _looks_like_sql_session(session):
        return 0
    try:
        ensure_account_list_state_schema(session)
        session.exec(
            text(
                "DELETE FROM account_list_state WHERE account_id IN ("
                + ",".join(str(account_id) for account_id in ids)
                + ")"
            )
        )
        if commit:
            session.commit()
        return len(ids)
    except Exception as exc:
        _log_account_list_state_sync_skip("delete", ids, exc)
        return 0


def normalize_account_sort_order(value: Any) -> str:
    text = _lower_text(value)
    if text in {"asc", "ascend", "ascending"}:
        return "asc"
    if text in {"desc", "descend", "descending"}:
        return "desc"
    return ""


def _split_account_sort_values(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        raw_values = value
    else:
        raw_values = str(value or "").split(",")
    return [_lower_text(item) for item in raw_values]


def normalize_account_sort_specs(sort_by: Any = None, sort_order: Any = None) -> tuple[tuple[str, str], ...]:
    """Return the canonical, deterministic account-list sort specification.

    The legacy API accepted one ``sort_by`` / ``sort_order`` pair.  Comma-
    separated values extend that contract without breaking old callers.  An
    expiry-only request implicitly uses newest registration first for equal
    expiry timestamps; an omitted or invalid request defaults to newest
    registration first.
    """

    fields = _split_account_sort_values(sort_by)
    orders = _split_account_sort_values(sort_order)
    specs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for index, field in enumerate(fields):
        if field not in ACCOUNT_SORT_FIELDS or field in seen:
            continue
        order = normalize_account_sort_order(orders[index] if index < len(orders) else "")
        if not order:
            continue
        specs.append((field, order))
        seen.add(field)

    if not specs:
        return DEFAULT_ACCOUNT_SORT_SPECS
    if ACCOUNT_SORT_SUBSCRIPTION_ACTIVE_UNTIL in seen and ACCOUNT_SORT_CREATED_AT not in seen:
        specs.append((ACCOUNT_SORT_CREATED_AT, DEFAULT_ACCOUNT_SORT_SPECS[0][1]))
    return tuple(specs)


def should_sort_account_rows(sort_by: Any, sort_order: Any) -> bool:
    return any(
        field == ACCOUNT_SORT_SUBSCRIPTION_ACTIVE_UNTIL
        for field, _ in normalize_account_sort_specs(sort_by, sort_order)
    )


def should_use_account_list_state(
    *,
    manually_used: bool | None = None,
    auth_type: Any = None,
    phone_binding_state: Any = None,
    payment_link_platform: Any = None,
    payment_link_generated: bool | None = None,
    subscription_type: Any = None,
    account_validity_filter: Any = None,
    sub2api_state: Any = None,
    oaipay_state: Any = None,
    idea_submit_state: Any = None,
    submit_state: Any = None,
    zero_amount_eligibility_state: Any = None,
    gcash_payment_method_state: Any = None,
    checkout_link_type: Any = None,
    has_submitted: bool | None = None,
    revival_state: Any = None,
    sort_by: Any = None,
    sort_order: Any = None,
) -> bool:
    return any(
        [
            manually_used is not None,
            bool(_split_values(auth_type)),
            bool(_split_phone_binding_state_filter_values(phone_binding_state)),
            bool(_split_payment_link_platform_filter_values(payment_link_platform)),
            payment_link_generated is not None,
            bool(_split_subscription_filter_values(subscription_type)),
            bool(_split_values(account_validity_filter)),
            bool(_split_integration_upload_state_filter_values(sub2api_state)),
            bool(_split_integration_upload_state_filter_values(oaipay_state)),
            bool(_split_values(idea_submit_state)),
            bool(_split_values(submit_state)),
            bool(_split_values(zero_amount_eligibility_state)),
            bool(_split_values(gcash_payment_method_state)),
            bool(_split_values(checkout_link_type)),
            has_submitted is not None,
            bool(_split_values(revival_state)),
            should_sort_account_rows(sort_by, sort_order),
        ]
    )


def apply_account_list_state_filters(
    query: Any,
    *,
    manually_used: bool | None = None,
    auth_type: Any = None,
    phone_binding_state: Any = None,
    payment_link_platform: Any = None,
    payment_link_generated: bool | None = None,
    subscription_type: Any = None,
    account_validity_filter: Any = None,
    sub2api_state: Any = None,
    oaipay_state: Any = None,
    idea_submit_state: Any = None,
    submit_state: Any = None,
    zero_amount_eligibility_state: Any = None,
    gcash_payment_method_state: Any = None,
    checkout_link_type: Any = None,
    has_submitted: bool | None = None,
    revival_state: Any = None,
) -> Any:
    if manually_used is not None:
        query = query.where(AccountListStateModel.manually_used == manually_used)

    auth_types = _split_values(auth_type)
    if auth_types:
        query = query.where(AccountListStateModel.auth_type.in_(sorted(auth_types)))

    phone_binding_states = _split_phone_binding_state_filter_values(phone_binding_state)
    if phone_binding_states:
        query = query.where(AccountListStateModel.phone_binding_state.in_(sorted(phone_binding_states)))

    payment_link_platforms = _split_payment_link_platform_filter_values(payment_link_platform)
    if payment_link_platforms:
        query = query.where(AccountListStateModel.payment_link_platform.in_(sorted(payment_link_platforms)))

    if payment_link_generated is not None:
        query = query.where(AccountListStateModel.payment_link_generated == payment_link_generated)

    subscription_predicate = _subscription_filter_predicate(subscription_type)
    if subscription_predicate is not None:
        query = query.where(subscription_predicate)

    validity_values = _split_values(account_validity_filter)
    if validity_values:
        query = query.where(AccountListStateModel.account_validity.in_(sorted(validity_values)))

    sub2api_states = _split_integration_upload_state_filter_values(sub2api_state)
    if sub2api_states:
        query = query.where(AccountListStateModel.sub2api_state.in_(sorted(sub2api_states)))

    oaipay_states = _split_integration_upload_state_filter_values(oaipay_state)
    if oaipay_states:
        query = query.where(AccountListStateModel.oaipay_state.in_(sorted(oaipay_states)))

    idea_submit_states = _split_idea_submit_filter_values(idea_submit_state)
    if idea_submit_states:
        query = query.where(AccountListStateModel.idea_submit_state.in_(sorted(idea_submit_states)))

    submit_states = _split_idea_submit_filter_values(submit_state)
    if submit_states:
        query = query.where(AccountListStateModel.submit_state.in_(sorted(submit_states)))

    zero_amount_states = _split_values(zero_amount_eligibility_state)
    if zero_amount_states:
        query = query.where(AccountListStateModel.zero_amount_eligibility_state.in_(sorted(zero_amount_states)))

    gcash_states = _split_values(gcash_payment_method_state)
    if gcash_states:
        query = query.where(AccountListStateModel.gcash_payment_method_state.in_(sorted(gcash_states)))

    checkout_link_types = _split_values(checkout_link_type)
    if checkout_link_types:
        query = query.where(AccountListStateModel.checkout_link_type.in_(sorted(checkout_link_types)))

    if has_submitted is not None:
        query = query.where(AccountListStateModel.has_submitted == has_submitted)

    revival_states = _split_values(revival_state)
    if revival_states:
        query = query.where(AccountListStateModel.revival_state.in_(sorted(revival_states)))

    return query


def account_filtered_query(
    session: Session,
    *,
    platform: str | None,
    filter_source: Any,
    refresh_state: bool = True,
    include_fixed_members: bool = False,
) -> tuple[Any, bool, dict[str, Any]]:
    """Build the canonical account-list query used by list and batch scopes."""

    normalized = normalize_account_filter(filter_source)
    revival_state = _normalize_filter_values(_filter_source_value(filter_source, "revival_state"))
    sort_by = _filter_source_value(filter_source, "sort_by")
    sort_order = _filter_source_value(filter_source, "sort_order")
    use_list_state = should_use_account_list_state(
        manually_used=normalized["manually_used"],
        auth_type=normalized["auth_type"],
        phone_binding_state=normalized["phone_binding_state"],
        payment_link_platform=normalized["payment_link_platform"],
        payment_link_generated=normalized["payment_link_generated"],
        subscription_type=normalized["subscription_type"],
        account_validity_filter=normalized["account_validity"],
        sub2api_state=normalized["sub2api_state"],
        oaipay_state=normalized["oaipay_state"],
        idea_submit_state=normalized["idea_submit_state"],
        submit_state=normalized["submit_state"],
        zero_amount_eligibility_state=normalized["zero_amount_eligibility_state"],
        gcash_payment_method_state=normalized["gcash_payment_method_state"],
        checkout_link_type=normalized["checkout_link_type"],
        has_submitted=normalized["has_submitted"],
        revival_state=revival_state,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    if use_list_state and refresh_state:
        refresh_stale_account_list_state(session, platform=platform)

    query = account_base_query(
        platform=platform,
        status=normalized["status"],
        email=normalized["email"],
    )
    primary_preset_id = normalized.get("primary_preset_id") or ""
    secondary_scope = normalized.get("secondary_scope") or ""
    fixed_group_id = normalized.get("fixed_group_id") or ""
    stable_membership = select(AccountFixedGroupMemberModel.account_id).where(
        AccountFixedGroupMemberModel.account_id == AccountModel.id,
        func.lower(AccountFixedGroupMemberModel.account_email) == func.lower(AccountModel.email),
        AccountFixedGroupMemberModel.account_created_at
        == func.replace(cast(AccountModel.created_at, String), " ", "T"),
    )
    if secondary_scope == "fixed":
        fixed_membership = stable_membership.where(
            AccountFixedGroupMemberModel.fixed_group_id == fixed_group_id
        )
        query = query.where(exists(fixed_membership))
    elif not include_fixed_members and (
        secondary_scope == "unassigned"
        or _safe_str(platform).lower() == "chatgpt"
    ):
        excluded_membership = stable_membership
        if secondary_scope == "unassigned" and primary_preset_id:
            excluded_membership = excluded_membership.join(
                AccountFixedGroupModel,
                AccountFixedGroupModel.id == AccountFixedGroupMemberModel.fixed_group_id,
            ).where(AccountFixedGroupModel.parent_preset_id == primary_preset_id)
        # A selected primary preset owns its own exclusive pool. Requests
        # without explicit primary context retain the legacy global boundary.
        query = query.where(~exists(excluded_membership))
    if not use_list_state:
        return query, False, normalized

    query = query.join(
        AccountListStateModel,
        AccountListStateModel.account_id == AccountModel.id,
    )
    query = apply_account_list_state_filters(
        query,
        manually_used=normalized["manually_used"],
        auth_type=normalized["auth_type"],
        phone_binding_state=normalized["phone_binding_state"],
        payment_link_platform=normalized["payment_link_platform"],
        payment_link_generated=normalized["payment_link_generated"],
        subscription_type=normalized["subscription_type"],
        account_validity_filter=normalized["account_validity"],
        sub2api_state=normalized["sub2api_state"],
        oaipay_state=normalized["oaipay_state"],
        idea_submit_state=normalized["idea_submit_state"],
        submit_state=normalized["submit_state"],
        zero_amount_eligibility_state=normalized["zero_amount_eligibility_state"],
        gcash_payment_method_state=normalized["gcash_payment_method_state"],
        checkout_link_type=normalized["checkout_link_type"],
        has_submitted=normalized["has_submitted"],
        revival_state=revival_state,
    )
    return query, True, normalized


def resolve_filtered_accounts(
    session: Session,
    *,
    platform: str,
    filter_source: Any,
    verify_expected_total: bool = False,
) -> AccountFilterResolution:
    secondary_scope = normalize_secondary_scope(_filter_source_value(filter_source, "secondary_scope"))
    if secondary_scope == "fixed":
        fixed_group_id = _safe_str(_filter_source_value(filter_source, "fixed_group_id"))
        primary_preset_id = _safe_str(_filter_source_value(filter_source, "primary_preset_id"))
        if not fixed_group_id:
            raise AccountFixedGroupScopeChangedError("固定账号组合范围缺少组合 ID")
        group = session.get(AccountFixedGroupModel, fixed_group_id)
        if group is None:
            raise AccountFixedGroupScopeChangedError("固定账号组合已不存在，请刷新列表后重试")
        if primary_preset_id and group.parent_preset_id != primary_preset_id:
            raise AccountFixedGroupScopeChangedError("固定账号组合所属的条件组合已变化，请刷新列表后重试")
        raw_revision = _filter_source_value(filter_source, "fixed_group_revision")
        if raw_revision is not None and int(raw_revision) != int(group.revision or 1):
            raise AccountFixedGroupScopeChangedError("固定账号组合成员已变化，请刷新列表后重新确认范围")

    query, _, normalized = account_filtered_query(
        session,
        platform=platform,
        filter_source=filter_source,
    )
    rows = tuple(session.exec(query.order_by(AccountModel.id.asc())).all())
    account_ids = tuple(int(row.id or 0) for row in rows if int(row.id or 0) > 0)
    matched_total = len(account_ids)
    raw_expected_total = _filter_source_value(filter_source, "expected_total")
    expected_total = int(raw_expected_total) if raw_expected_total is not None else None
    if verify_expected_total and expected_total is not None and expected_total != matched_total:
        raise AccountFilterScopeChangedError(
            expected_total=expected_total,
            matched_total=matched_total,
        )

    audit = build_account_filter_audit(
        filter_source,
        account_ids,
        matched_total=matched_total,
        all_filtered=verify_expected_total,
    )
    return AccountFilterResolution(
        rows=rows,
        account_ids=account_ids,
        normalized_filter=normalized,
        matched_total=matched_total,
        expected_total=expected_total,
        verified=bool(audit["verified"]),
        audit=audit,
    )


def apply_account_list_state_sort(
    query: Any,
    *,
    sort_by: Any = None,
    sort_order: Any = None,
) -> Any:
    specs = normalize_account_sort_specs(sort_by, sort_order)
    order_by: list[Any] = []
    created_at_order = DEFAULT_ACCOUNT_SORT_SPECS[0][1]
    for field, order in specs:
        if field == ACCOUNT_SORT_SUBSCRIPTION_ACTIVE_UNTIL:
            timestamp = AccountListStateModel.subscription_active_until_ts
            order_by.append(timestamp.is_(None).asc())
            order_by.append(timestamp.desc() if order == "desc" else timestamp.asc())
            continue
        if field == ACCOUNT_SORT_CREATED_AT:
            created_at_order = order
            created_at = AccountModel.created_at
            # AccountModel.created_at is NOT NULL and this exact expression
            # keeps SQLite eligible for the (platform, created_at, id) index.
            order_by.append(created_at.desc() if order == "desc" else created_at.asc())

    order_by.append(AccountModel.id.desc() if created_at_order == "desc" else AccountModel.id.asc())
    return query.order_by(*order_by)


def sort_account_rows(rows: Iterable[AccountModel], *, sort_by: Any = None, sort_order: Any = None) -> list[AccountModel]:
    items = list(rows)
    specs = normalize_account_sort_specs(sort_by, sort_order)
    created_at_order = next(
        (order for field, order in specs if field == ACCOUNT_SORT_CREATED_AT),
        DEFAULT_ACCOUNT_SORT_SPECS[0][1],
    )
    items.sort(key=lambda row: int(getattr(row, "id", 0) or 0), reverse=created_at_order == "desc")

    def created_at_timestamp(row: AccountModel) -> float | None:
        value = getattr(row, "created_at", None)
        if isinstance(value, datetime):
            normalized = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
            return normalized.timestamp()
        text = _safe_str(value)
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
        except ValueError:
            return None
        normalized = parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
        return normalized.timestamp()

    # Python's sort is stable. Apply lower-priority fields first so this helper
    # exactly mirrors the SQL ORDER BY used before pagination.
    for field, order in reversed(specs):
        reverse = order == "desc"
        if field == ACCOUNT_SORT_CREATED_AT:
            present: list[tuple[AccountModel, float]] = []
            missing: list[AccountModel] = []
            for row in items:
                timestamp = created_at_timestamp(row)
                if timestamp is None:
                    missing.append(row)
                else:
                    present.append((row, timestamp))
            present.sort(key=lambda item: item[1], reverse=reverse)
            items = [row for row, _ in present] + missing
            continue
        if field == ACCOUNT_SORT_SUBSCRIPTION_ACTIVE_UNTIL:
            present: list[tuple[AccountModel, float]] = []
            missing: list[AccountModel] = []
            for row in items:
                timestamp = account_subscription_active_until_timestamp(row)
                if timestamp is None:
                    missing.append(row)
                else:
                    present.append((row, timestamp))
            present.sort(key=lambda item: item[1], reverse=reverse)
            items = [row for row, _ in present] + missing
    return items


def filter_account_rows(
    rows: Iterable[AccountModel],
    *,
    manually_used: bool | None = None,
    auth_type: Any = None,
    phone_binding_state: Any = None,
    payment_link_platform: Any = None,
    payment_link_generated: bool | None = None,
    subscription_type: Any = None,
    account_validity_filter: Any = None,
    sub2api_state: Any = None,
    oaipay_state: Any = None,
    idea_submit_state: Any = None,
    submit_state: Any = None,
    has_submitted: bool | None = None,
    revival_state: Any = None,
) -> list[AccountModel]:
    auth_types = _split_values(auth_type)
    phone_binding_states = _split_phone_binding_state_filter_values(phone_binding_state)
    payment_link_platforms = _split_payment_link_platform_filter_values(payment_link_platform)
    subscription_types = _split_subscription_filter_values(subscription_type)
    validity_values = _split_values(account_validity_filter)
    sub2api_states = _split_integration_upload_state_filter_values(sub2api_state)
    oaipay_states = _split_integration_upload_state_filter_values(oaipay_state)
    idea_submit_states = _split_idea_submit_filter_values(idea_submit_state)
    submit_states = _split_idea_submit_filter_values(submit_state)
    revival_states = _split_values(revival_state)

    filtered: list[AccountModel] = []
    for row in rows:
        extra = _extra(row)
        if manually_used is not None and bool(extra.get("manually_used")) is not manually_used:
            continue
        if auth_types and account_auth_type(row, extra) not in auth_types:
            continue
        if phone_binding_states and account_phone_binding_state(row, extra) not in phone_binding_states:
            continue
        if payment_link_platforms and account_payment_link_platform(row, extra) not in payment_link_platforms:
            continue
        if payment_link_generated is not None and account_payment_link_generated(row, extra) is not payment_link_generated:
            continue
        if subscription_types:
            exact_types = subscription_types - SUBSCRIPTION_STATUS_FILTER_VALUES
            exact_match = account_subscription_type(row, extra) in exact_types
            status_match = (
                bool(subscription_types & SUBSCRIPTION_STATUS_FILTER_VALUES)
                and account_subscription_status(row, extra) in subscription_types
            )
            if not exact_match and not status_match:
                continue
        if validity_values and account_validity(row, extra) not in validity_values:
            continue
        if sub2api_states and account_sub2api_upload_state(row, extra) not in sub2api_states:
            continue
        if oaipay_states and account_oaipay_upload_state(row, extra) not in oaipay_states:
            continue
        if idea_submit_states and account_idea_submit_state(row, extra) not in idea_submit_states:
            continue
        if submit_states and account_submit_state(row, extra) not in submit_states:
            continue
        if has_submitted is not None and account_has_submitted(row, extra) is not has_submitted:
            continue
        if revival_states and account_revival_state(row, extra) not in revival_states:
            continue
        filtered.append(row)
    return filtered
