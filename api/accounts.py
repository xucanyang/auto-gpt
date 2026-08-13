from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from fastapi.responses import StreamingResponse
from sqlmodel import Session, select, func
from pydantic import BaseModel, Field
from core.config_store import config_store
from core.db import (
    AccountFixedGroupMemberModel,
    AccountListStateModel,
    AccountModel,
    engine,
    get_session,
)
from core.timezone import beijing_iso
from services.account_filters import (
    account_auth_type,
    account_payment_link_generated,
    account_filtered_query,
    account_payment_link_summary,
    account_revival_info,
    account_submission_info,
    account_subscription_type,
    apply_account_list_state_sort,
    delete_account_list_state_for_account_ids,
    refresh_account_list_state,
    upsert_account_list_state_for_account_ids,
)
from services.account_fixed_groups import (
    FixedGroupConflictError,
    create_fixed_group,
    delete_fixed_group,
    fixed_group_member_ids,
    fixed_group_name_exists,
    get_fixed_group,
    list_fixed_groups,
    replace_fixed_group_members,
    serialize_fixed_group,
    serialize_fixed_groups,
    update_fixed_group_meta,
)
from services.account_rate_limit_recovery import (
    RATE_LIMITED_STATUS,
    account_rate_limit_payload,
    clear_account_rate_limit,
    mark_account_rate_limited,
    reconcile_rate_limited_accounts,
)
from services.chatgpt_account_state import AUTH_INVALID_STATES, classify_chatgpt_capabilities, normalize_subscription_plan
from services.chatgpt_core.bound_phone import chatgpt_bound_phone_payload, chatgpt_phone_challenge_payload
from services.chatgpt_core.codex_usage import build_codex_usage_progress_from_extra
from services.chatgpt_core.local_status_refresh import (
    prepare_chatgpt_account_for_local_status_refresh,
    schedule_chatgpt_local_status_refresh_for_account_id,
)
from services.chatgpt_core.payment_link_cache import payment_link_type_from_payload
from services.chatgpt_core.task_logging import sanitize_error_message
from typing import Any, Optional
from datetime import datetime, timezone
from pathlib import Path
import io, csv, json, logging, sqlite3, threading, time, uuid

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/accounts", tags=["accounts"])

_LIST_RATE_LIMIT_RECONCILE_SECONDS = 30
_list_rate_limit_reconcile_lock = threading.Lock()
_list_rate_limit_reconcile_at: dict[str, float] = {}


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _safe_str(value: Any) -> str:
    return str(value or "").strip()


def _safe_extra(account: AccountModel) -> dict[str, Any]:
    try:
        extra = account.get_extra()
    except Exception:
        return {}
    return extra if isinstance(extra, dict) else {}


ACCOUNT_FILTER_PRESETS_CONFIG_KEY = "chatgpt_account_filter_presets"
ACCOUNT_FILTER_PRESET_SCHEMA_VERSION = 4
ACCOUNT_FILTER_PRESET_REGISTRATION_DESC_VERSION = 3
ACCOUNT_FILTER_PRESET_MAX_CUSTOM_ITEMS = 80
ACCOUNT_FILTER_PRESET_MAX_LIST_VALUES = 32
ACCOUNT_FILTER_PRESET_MAX_ACCOUNT_IDS = 5000
ACCOUNT_FILTER_PRESET_MIN_PAGE_SIZE = 1
ACCOUNT_FILTER_PRESET_MAX_PAGE_SIZE = 200
ACCOUNT_FILTER_PRESET_MODE_DYNAMIC = "dynamic"
ACCOUNT_FILTER_PRESET_MODE_FIXED = "fixed"
ACCOUNT_FILTER_PRESET_COLUMN_KEYS = (
    "email",
    "status",
    "manuallyUsed",
    "authType",
    "phoneBindingState",
    "paymentLinkPlatform",
    "paymentLinkGenerated",
    "subscriptionType",
    "accountValidity",
    "codexState",
    "sub2apiState",
    "oaipayState",
    "zeroAmountEligibilityState",
    "gcashPaymentMethodState",
    "ideaSubmitState",
    "submitState",
    "hasSubmitted",
)
ACCOUNT_FILTER_PRESET_PENDING_OAIPAY_STATES = ["not_uploaded"]
_INTEGRATION_UPLOAD_FILTER_ALIASES = {
    "true": "uploaded",
    "uploaded": "uploaded",
    "exists": "uploaded",
    "false": "not_uploaded",
    "not_uploaded": "not_uploaded",
    "unknown": "not_uploaded",
    "not_found": "not_uploaded",
    "deleted_exact_match": "not_uploaded",
    "cross_workspace_only": "not_uploaded",
    "ambiguous": "not_uploaded",
    "unreachable": "not_uploaded",
}


class AccountFilterPresetBody(BaseModel):
    name: str
    description: str = ""
    mode: str = ACCOUNT_FILTER_PRESET_MODE_DYNAMIC
    filters: dict[str, Any] = Field(default_factory=dict)
    account_ids: list[int] = Field(default_factory=list)
    parent_preset_id: str = ""
    move_conflicts: bool = False
    pinned: bool = False


class FixedPresetMigrationBody(BaseModel):
    parent_by_preset_id: dict[str, str] = Field(default_factory=dict)
    priority_order: list[str] = Field(default_factory=list)
    commit: bool = False


def _utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _trim_text(value: Any, *, max_length: int = 160) -> str:
    text = str(value or "").strip()
    if len(text) > max_length:
        text = text[:max_length].strip()
    return text


def _filter_value_list(value: Any) -> list[str]:
    raw_items: list[Any]
    if value is None:
        raw_items = []
    elif isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        raw_items = str(value or "").split(",")

    result: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        text = _trim_text(item, max_length=80)
        if not text:
            continue
        if text in seen:
            continue
        seen.add(text)
        result.append(text)
        if len(result) >= ACCOUNT_FILTER_PRESET_MAX_LIST_VALUES:
            break
    return result


def _normalize_filter_preset_mode(value: Any) -> str:
    normalized = _trim_text(value, max_length=24).lower().replace("-", "_")
    if normalized in {"fixed", "accounts", "account_ids", "selected", "selection"}:
        return ACCOUNT_FILTER_PRESET_MODE_FIXED
    return ACCOUNT_FILTER_PRESET_MODE_DYNAMIC


def _normalize_filter_preset_account_ids(value: Any) -> list[int]:
    if value is None:
        raw_items: list[Any] = []
    elif isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        raw_items = str(value or "").split(",")

    result: list[int] = []
    seen: set[int] = set()
    for raw in raw_items:
        if isinstance(raw, bool):
            continue
        account_id = _safe_int(raw)
        if account_id <= 0 or account_id in seen:
            continue
        seen.add(account_id)
        result.append(account_id)
        if len(result) >= ACCOUNT_FILTER_PRESET_MAX_ACCOUNT_IDS:
            break
    return result


def _filter_preset_account_created_at(value: Any) -> str:
    if isinstance(value, datetime):
        normalized = value
        if normalized.tzinfo is not None:
            normalized = normalized.astimezone(timezone.utc).replace(tzinfo=None)
        return normalized.isoformat(timespec="microseconds")
    return _trim_text(value, max_length=80)


def _filter_preset_account_ref(account: AccountModel) -> dict[str, Any]:
    return {
        "id": int(account.id or 0),
        "email": _trim_text(account.email, max_length=320).lower(),
        "created_at": _filter_preset_account_created_at(account.created_at),
    }


def _normalize_filter_preset_account_refs(
    value: Any,
    account_ids: list[int],
) -> list[dict[str, Any]]:
    raw_items = value if isinstance(value, list) else []
    refs_by_id: dict[int, dict[str, Any]] = {}
    allowed_ids = set(account_ids)
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        account_id = _safe_int(raw.get("id"))
        if account_id <= 0 or account_id not in allowed_ids or account_id in refs_by_id:
            continue
        email = _trim_text(raw.get("email"), max_length=320).lower()
        created_at = _filter_preset_account_created_at(raw.get("created_at"))
        if not email or not created_at:
            continue
        refs_by_id[account_id] = {
            "id": account_id,
            "email": email,
            "created_at": created_at,
        }
    return [refs_by_id[account_id] for account_id in account_ids if account_id in refs_by_id]


def _filter_preset_account_matches_ref(
    account: AccountModel,
    account_ref: dict[str, Any] | None,
) -> bool:
    # `account_refs` did not exist in early fixed-preset development payloads.
    # Keep those readable by ID, while all API-created presets carry a strong
    # identity reference that prevents SQLite primary-key reuse from rebinding.
    if not account_ref:
        return True
    return (
        _trim_text(account.email, max_length=320).lower() == account_ref.get("email")
        and _filter_preset_account_created_at(account.created_at) == account_ref.get("created_at")
    )


def _validate_filter_preset_content(
    body: AccountFilterPresetBody,
    *,
    session: Session,
) -> tuple[str, dict[str, Any], list[int], list[dict[str, Any]], list[int]]:
    mode = _normalize_filter_preset_mode(body.mode)
    filters = _normalize_filter_preset_filters(body.filters)
    if mode != ACCOUNT_FILTER_PRESET_MODE_FIXED:
        return mode, filters, [], [], []

    raw_account_ids = list(body.account_ids or [])
    if len(raw_account_ids) > ACCOUNT_FILTER_PRESET_MAX_ACCOUNT_IDS:
        raise HTTPException(
            400,
            f"固定账号组合最多保存 {ACCOUNT_FILTER_PRESET_MAX_ACCOUNT_IDS} 个账号",
        )
    requested_ids = _normalize_filter_preset_account_ids(raw_account_ids)
    if not requested_ids:
        raise HTTPException(400, "固定账号组合至少需要一个有效账号")

    existing_rows = session.exec(
        select(AccountModel).where(
            AccountModel.platform == "chatgpt",
            AccountModel.id.in_(requested_ids),
        )
    ).all()
    accounts_by_id = {
        int(account.id): account
        for account in existing_rows
        if _safe_int(account.id) > 0
    }
    existing_ids = set(accounts_by_id)
    account_ids = [account_id for account_id in requested_ids if account_id in existing_ids]
    account_refs = [_filter_preset_account_ref(accounts_by_id[account_id]) for account_id in account_ids]
    discarded_ids = [account_id for account_id in requested_ids if account_id not in existing_ids]
    if not account_ids:
        raise HTTPException(400, "所选账号已不存在，无法保存固定账号组合")
    return mode, _empty_filter_preset_payload(), account_ids, account_refs, discarded_ids


_IDEA_SUBMIT_FILTER_PRESET_ALIASES = {
    "available": "unsubmitted",
    "not_submitted": "unsubmitted",
    "pending_submit": "unsubmitted",
    "submitted": "submitting",
    "processing": "submitting",
    "pending": "submitting",
    "polling": "submitting",
    "success": "paid",
    "completed": "paid",
    "manual_review": "timeout",
    "unknown_submit": "timeout",
    "fail": "failed",
    "error": "failed",
}


def _normalize_idea_submit_filter_values(values: Any) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for item in _filter_value_list(values):
        value = _IDEA_SUBMIT_FILTER_PRESET_ALIASES.get(item.lower(), item)
        if not value or value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return normalized


def _normalize_integration_upload_filter_values(values: Any) -> list[str]:
    normalized: list[str] = []
    for item in _filter_value_list(values):
        value = _INTEGRATION_UPLOAD_FILTER_ALIASES.get(item.lower(), item.lower())
        if value and value not in normalized:
            normalized.append(value)
    if set(normalized) == {"uploaded", "not_uploaded"}:
        return []
    return normalized


def _normalize_payment_link_generated_filter_values(values: Any) -> list[str]:
    normalized: list[str] = []
    for item in _filter_value_list(values):
        value = item.lower()
        if value in {"true", "1", "yes", "on", "generated", "succeeded"}:
            value = "true"
        elif value in {"false", "0", "no", "off", "not_generated", "never"}:
            value = "false"
        else:
            continue
        if value not in normalized:
            normalized.append(value)
    if set(normalized) == {"true", "false"}:
        return []
    return normalized


def _empty_filter_preset_payload() -> dict[str, Any]:
    return {
        "search": "",
        "status": [],
        "columnFilters": {key: [] for key in ACCOUNT_FILTER_PRESET_COLUMN_KEYS},
        "sortOrder": "",
        "registrationSortOrder": "desc",
        "pageSize": 20,
    }


def _normalize_filter_preset_filters(filters: Any) -> dict[str, Any]:
    source = filters if isinstance(filters, dict) else {}
    source_column_filters = source.get("columnFilters") if isinstance(source.get("columnFilters"), dict) else {}
    clean = _empty_filter_preset_payload()

    search = _trim_text(source.get("search") or source.get("email") or source_column_filters.get("email"), max_length=160)
    clean["search"] = search
    clean["columnFilters"]["email"] = search

    status_values = _filter_value_list(source.get("status") or source.get("filterStatus") or source_column_filters.get("status"))
    clean["status"] = status_values
    clean["columnFilters"]["status"] = status_values

    for key in ACCOUNT_FILTER_PRESET_COLUMN_KEYS:
        if key in {"email", "status"}:
            continue
        values = _filter_value_list(source_column_filters.get(key) or source.get(key))
        if key in {"ideaSubmitState", "submitState"}:
            clean["columnFilters"][key] = _normalize_idea_submit_filter_values(values)
        elif key in {"sub2apiState", "oaipayState"}:
            clean["columnFilters"][key] = _normalize_integration_upload_filter_values(values)
        elif key == "paymentLinkGenerated":
            clean["columnFilters"][key] = _normalize_payment_link_generated_filter_values(values)
        else:
            clean["columnFilters"][key] = values

    sort_source = source.get("sort") if isinstance(source.get("sort"), dict) else {}
    sort_order = _trim_text(
        source.get("sortOrder")
        or source.get("subscriptionExpirySortOrder")
        or sort_source.get("sortOrder")
        or sort_source.get("order"),
        max_length=8,
    ).lower()
    clean["sortOrder"] = sort_order if sort_order in {"asc", "desc"} else ""

    registration_sort_order = _trim_text(
        source.get("registrationSortOrder")
        or source.get("createdAtSortOrder")
        or sort_source.get("registrationSortOrder")
        or sort_source.get("createdAtOrder"),
        max_length=8,
    ).lower()
    clean["registrationSortOrder"] = registration_sort_order if registration_sort_order in {"asc", "desc"} else "desc"

    try:
        page_size = int(source.get("pageSize") or source.get("page_size") or 20)
    except Exception:
        page_size = 20
    clean["pageSize"] = (
        page_size
        if ACCOUNT_FILTER_PRESET_MIN_PAGE_SIZE <= page_size <= ACCOUNT_FILTER_PRESET_MAX_PAGE_SIZE
        else 20
    )
    return clean


def _filter_preset_summary(filters: dict[str, Any]) -> str:
    column_filters = filters.get("columnFilters") if isinstance(filters.get("columnFilters"), dict) else {}
    parts: list[str] = []
    if filters.get("search"):
        parts.append(f"搜索={filters.get('search')}")
    summary_keys = [
        ("status", "状态"),
        ("subscriptionType", "订阅"),
        ("authType", "认证"),
        ("phoneBindingState", "手机号"),
        ("paymentLinkPlatform", "支付链接"),
        ("paymentLinkGenerated", "提取记录"),
        ("accountValidity", "有效性"),
        ("manuallyUsed", "使用"),
        ("sub2apiState", "Sub2API"),
        ("oaipayState", "OAIPay"),
        ("ideaSubmitState", "Idea提交"),
        ("submitState", "提交状态"),
        ("hasSubmitted", "已提交"),
    ]
    for key, label in summary_keys:
        values = _filter_value_list(column_filters.get(key))
        if values:
            parts.append(f"{label}={','.join(values[:4])}{'…' if len(values) > 4 else ''}")
    if filters.get("sortOrder"):
        parts.append("到期排序=" + ("最早" if filters.get("sortOrder") == "asc" else "最晚"))
    if filters.get("registrationSortOrder") == "asc":
        parts.append("注册排序=最早")
    return " · ".join(parts) or "无筛选条件"


def _filter_preset_content_summary(
    mode: str,
    filters: dict[str, Any],
    account_ids: list[int],
) -> str:
    if mode == ACCOUNT_FILTER_PRESET_MODE_FIXED:
        return f"固定账号 · {len(account_ids)} 个"
    return _filter_preset_summary(filters)


def _make_builtin_filter_preset(
    *,
    preset_id: str,
    name: str,
    description: str,
    column_filters: dict[str, list[str]],
    pinned: bool = True,
) -> dict[str, Any]:
    filters = _empty_filter_preset_payload()
    for key, values in column_filters.items():
        if key not in filters["columnFilters"]:
            continue
        normalized = _filter_value_list(values)
        filters["columnFilters"][key] = normalized
        if key == "status":
            filters["status"] = normalized
    now = "builtin"
    return {
        "id": preset_id,
        "name": name,
        "description": description,
        "mode": ACCOUNT_FILTER_PRESET_MODE_DYNAMIC,
        "filters": filters,
        "account_ids": [],
        "account_refs": [],
        "account_count": 0,
        "summary": _filter_preset_summary(filters),
        "pinned": pinned,
        "built_in": True,
        "created_at": now,
        "updated_at": now,
    }


BUILTIN_ACCOUNT_FILTER_PRESETS: list[dict[str, Any]] = [
    _make_builtin_filter_preset(
        preset_id="builtin_oaipay_pending",
        name="OAIPay 待补传",
        description="OAIPay 尚无远端存在或上传成功记录。",
        column_filters={"oaipayState": ACCOUNT_FILTER_PRESET_PENDING_OAIPAY_STATES},
    ),
    _make_builtin_filter_preset(
        preset_id="builtin_plus_rt_oaipay_pending",
        name="Plus 长效未传",
        description="Plus/Pro + 有 Refresh Token + 有效 + OAIPay 待补传。",
        column_filters={
            "subscriptionType": ["plus", "pro"],
            "authType": ["refresh_token"],
            "accountValidity": ["valid"],
            "oaipayState": ACCOUNT_FILTER_PRESET_PENDING_OAIPAY_STATES,
        },
    ),
    _make_builtin_filter_preset(
        preset_id="builtin_plus_no_rt_oaipay_pending",
        name="Plus 未接码未传",
        description="Plus/Pro + 仅 AT/无认证材料 + 有效 + OAIPay 待补传。",
        column_filters={
            "subscriptionType": ["plus", "pro"],
            "authType": ["access_token_only", "unknown"],
            "accountValidity": ["valid"],
            "oaipayState": ACCOUNT_FILTER_PRESET_PENDING_OAIPAY_STATES,
        },
    ),
    _make_builtin_filter_preset(
        preset_id="builtin_free_rt_oaipay_pending",
        name="Free 带 RT 未传",
        description="Free + 有 Refresh Token + 有效 + OAIPay 待补传。",
        column_filters={
            "subscriptionType": ["free"],
            "authType": ["refresh_token"],
            "accountValidity": ["valid"],
            "oaipayState": ACCOUNT_FILTER_PRESET_PENDING_OAIPAY_STATES,
        },
    ),
    _make_builtin_filter_preset(
        preset_id="builtin_sub2api_exists_oaipay_pending",
        name="Sub2API 已有但 OAIPay 未传",
        description="Sub2API 已确认上传或远端存在，但 OAIPay 尚未上传。",
        column_filters={
            "sub2apiState": ["uploaded"],
            "oaipayState": ACCOUNT_FILTER_PRESET_PENDING_OAIPAY_STATES,
        },
    ),
]

BUILTIN_ACCOUNT_FILTER_PRESETS_BY_ID: dict[str, dict[str, Any]] = {
    str(item.get("id") or ""): item
    for item in BUILTIN_ACCOUNT_FILTER_PRESETS
    if str(item.get("id") or "")
}


def _source_bool(source: dict[str, Any], key: str, default: bool = False) -> bool:
    if key not in source:
        return bool(default)
    return bool(source.get(key))


def _normalize_custom_filter_preset(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    name = _trim_text(item.get("name"), max_length=80)
    if not name:
        return None
    preset_id = _trim_text(item.get("id"), max_length=80)
    if not preset_id or preset_id.startswith("builtin_"):
        preset_id = "preset_" + uuid.uuid4().hex[:12]
    mode = _normalize_filter_preset_mode(item.get("mode"))
    account_ids = _normalize_filter_preset_account_ids(item.get("account_ids")) if mode == ACCOUNT_FILTER_PRESET_MODE_FIXED else []
    account_refs = _normalize_filter_preset_account_refs(item.get("account_refs"), account_ids) if mode == ACCOUNT_FILTER_PRESET_MODE_FIXED else []
    if mode == ACCOUNT_FILTER_PRESET_MODE_FIXED and not account_ids:
        return None
    filters = (
        _empty_filter_preset_payload()
        if mode == ACCOUNT_FILTER_PRESET_MODE_FIXED
        else _normalize_filter_preset_filters(item.get("filters"))
    )
    created_at = _trim_text(item.get("created_at"), max_length=40) or _utc_iso()
    updated_at = _trim_text(item.get("updated_at"), max_length=40) or created_at
    return {
        "id": preset_id,
        "name": name,
        "description": _trim_text(item.get("description"), max_length=240),
        "mode": mode,
        "filters": filters,
        "account_ids": account_ids,
        "account_refs": account_refs,
        "account_count": len(account_ids),
        "summary": _filter_preset_content_summary(mode, filters, account_ids),
        "pinned": bool(item.get("pinned")),
        "built_in": False,
        "created_at": created_at,
        "updated_at": updated_at,
    }


def _normalize_builtin_filter_preset_override(preset_id: str, item: Any) -> dict[str, Any] | None:
    default = BUILTIN_ACCOUNT_FILTER_PRESETS_BY_ID.get(preset_id)
    if not default or not isinstance(item, dict):
        return None
    name = _trim_text(item.get("name") or default.get("name"), max_length=80)
    if not name:
        return None
    mode = _normalize_filter_preset_mode(item.get("mode") or default.get("mode"))
    account_ids = _normalize_filter_preset_account_ids(item.get("account_ids")) if mode == ACCOUNT_FILTER_PRESET_MODE_FIXED else []
    account_refs = _normalize_filter_preset_account_refs(item.get("account_refs"), account_ids) if mode == ACCOUNT_FILTER_PRESET_MODE_FIXED else []
    if mode == ACCOUNT_FILTER_PRESET_MODE_FIXED and not account_ids:
        return None
    filters_source = item.get("filters") if isinstance(item.get("filters"), dict) else default.get("filters")
    filters = (
        _empty_filter_preset_payload()
        if mode == ACCOUNT_FILTER_PRESET_MODE_FIXED
        else _normalize_filter_preset_filters(filters_source)
    )
    created_at = _trim_text(item.get("created_at"), max_length=40) or _trim_text(default.get("created_at"), max_length=40) or _utc_iso()
    updated_at = _trim_text(item.get("updated_at"), max_length=40) or _utc_iso()
    return {
        "id": preset_id,
        "name": name,
        "description": _trim_text(
            item.get("description") if "description" in item else default.get("description"),
            max_length=240,
        ),
        "mode": mode,
        "filters": filters,
        "account_ids": account_ids,
        "account_refs": account_refs,
        "account_count": len(account_ids),
        "summary": _filter_preset_content_summary(mode, filters, account_ids),
        "pinned": _source_bool(item, "pinned", bool(default.get("pinned"))),
        "built_in": True,
        "created_at": created_at,
        "updated_at": updated_at,
    }


def _empty_filter_preset_state() -> dict[str, Any]:
    return {
        "version": ACCOUNT_FILTER_PRESET_SCHEMA_VERSION,
        "custom": [],
        "builtin_overrides": {},
        "deleted_builtin_ids": set(),
    }


def _normalize_filter_preset_state(payload: Any) -> dict[str, Any]:
    state = _empty_filter_preset_state()
    source_version = 1

    if isinstance(payload, list):
        custom_raw = payload
        builtin_override_raw: Any = {}
        deleted_raw: Any = []
    elif isinstance(payload, dict):
        try:
            source_version = int(payload.get("version") or 1)
        except (TypeError, ValueError):
            source_version = 1
        custom_raw = payload.get("custom")
        if not isinstance(custom_raw, list):
            custom_raw = payload.get("items") if isinstance(payload.get("items"), list) else []
        builtin_override_raw = payload.get("builtin_overrides")
        deleted_raw = payload.get("deleted_builtin_ids")
    else:
        custom_raw = []
        builtin_override_raw = {}
        deleted_raw = []
    migrate_registration_sort_default = source_version < ACCOUNT_FILTER_PRESET_REGISTRATION_DESC_VERSION

    seen_custom_ids: set[str] = set()
    for raw_item in custom_raw:
        item = _normalize_custom_filter_preset(raw_item)
        if not item:
            continue
        if migrate_registration_sort_default:
            item["filters"]["registrationSortOrder"] = "desc"
            item["summary"] = _filter_preset_content_summary(
                item["mode"],
                item["filters"],
                item["account_ids"],
            )
        preset_id = str(item["id"])
        if preset_id in seen_custom_ids:
            continue
        seen_custom_ids.add(preset_id)
        state["custom"].append(item)
        if len(state["custom"]) >= ACCOUNT_FILTER_PRESET_MAX_CUSTOM_ITEMS:
            break

    deleted_ids = _filter_value_list(deleted_raw)
    state["deleted_builtin_ids"] = {
        preset_id
        for preset_id in deleted_ids
        if preset_id in BUILTIN_ACCOUNT_FILTER_PRESETS_BY_ID
    }

    overrides: dict[str, dict[str, Any]] = {}
    if isinstance(builtin_override_raw, dict):
        override_items = builtin_override_raw.items()
    elif isinstance(builtin_override_raw, list):
        override_items = (
            (str(item.get("id") or ""), item)
            for item in builtin_override_raw
            if isinstance(item, dict)
        )
    else:
        override_items = []
    for preset_id, raw_item in override_items:
        normalized_id = _trim_text(preset_id, max_length=80)
        if normalized_id in state["deleted_builtin_ids"]:
            continue
        item = _normalize_builtin_filter_preset_override(normalized_id, raw_item)
        if item:
            if migrate_registration_sort_default:
                item["filters"]["registrationSortOrder"] = "desc"
                item["summary"] = _filter_preset_content_summary(
                    item["mode"],
                    item["filters"],
                    item["account_ids"],
                )
            overrides[normalized_id] = item
    state["builtin_overrides"] = overrides
    return state


def _load_filter_preset_state() -> dict[str, Any]:
    raw = str(config_store.get(ACCOUNT_FILTER_PRESETS_CONFIG_KEY, "") or "").strip()
    if not raw:
        return _empty_filter_preset_state()
    try:
        payload = json.loads(raw)
    except Exception:
        logger.warning("failed to parse account filter presets config", exc_info=True)
        return _empty_filter_preset_state()
    return _normalize_filter_preset_state(payload)


def _save_filter_preset_state(state: dict[str, Any]) -> dict[str, Any]:
    normalized_state = _normalize_filter_preset_state(state)

    safe_custom: list[dict[str, Any]] = []
    seen_custom_ids: set[str] = set()
    for item in normalized_state["custom"][:ACCOUNT_FILTER_PRESET_MAX_CUSTOM_ITEMS]:
        normalized_item = _normalize_custom_filter_preset(item)
        if not normalized_item:
            continue
        preset_id = str(normalized_item.get("id") or "")
        if not preset_id or preset_id in seen_custom_ids:
            continue
        seen_custom_ids.add(preset_id)
        safe_custom.append(normalized_item)

    deleted_builtin_ids = {
        preset_id
        for preset_id in normalized_state["deleted_builtin_ids"]
        if preset_id in BUILTIN_ACCOUNT_FILTER_PRESETS_BY_ID
    }
    safe_overrides: dict[str, dict[str, Any]] = {}
    for preset_id, item in dict(normalized_state["builtin_overrides"]).items():
        if preset_id in deleted_builtin_ids:
            continue
        override = _normalize_builtin_filter_preset_override(preset_id, item)
        if override:
            safe_overrides[preset_id] = override

    saved_state = {
        "version": ACCOUNT_FILTER_PRESET_SCHEMA_VERSION,
        "custom": safe_custom,
        "builtin_overrides": safe_overrides,
        "deleted_builtin_ids": deleted_builtin_ids,
    }
    payload = {
        "version": ACCOUNT_FILTER_PRESET_SCHEMA_VERSION,
        "custom": safe_custom,
        "builtin_overrides": safe_overrides,
        "deleted_builtin_ids": [
            str(item.get("id") or "")
            for item in BUILTIN_ACCOUNT_FILTER_PRESETS
            if str(item.get("id") or "") in deleted_builtin_ids
        ],
    }
    config_store.set(
        ACCOUNT_FILTER_PRESETS_CONFIG_KEY,
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    )
    return saved_state


def _load_custom_filter_presets() -> list[dict[str, Any]]:
    return list(_load_filter_preset_state()["custom"])


def _save_custom_filter_presets(items: list[dict[str, Any]]) -> None:
    state = _load_filter_preset_state()
    state["custom"] = items
    _save_filter_preset_state(state)


def _visible_builtin_filter_presets(state: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    state = state if state is not None else _load_filter_preset_state()
    deleted_ids = set(state.get("deleted_builtin_ids") or set())
    overrides = state.get("builtin_overrides") if isinstance(state.get("builtin_overrides"), dict) else {}
    items: list[dict[str, Any]] = []
    for default in BUILTIN_ACCOUNT_FILTER_PRESETS:
        preset_id = str(default.get("id") or "")
        if not preset_id or preset_id in deleted_ids:
            continue
        override = overrides.get(preset_id)
        item = _normalize_builtin_filter_preset_override(preset_id, override) if override else None
        items.append(item or dict(default))
    return items


def _duplicate_filter_preset_name(state: dict[str, Any], name: str, *, ignore_id: str = "") -> bool:
    normalized = name.strip().lower()
    for item in [*_visible_builtin_filter_presets(state), *list(state.get("custom") or [])]:
        if ignore_id and str(item.get("id") or "") == ignore_id:
            continue
        if str(item.get("name") or "").strip().lower() == normalized:
            return True
    return False


def _find_visible_filter_preset(
    preset_id: str,
    state: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    normalized_id = _trim_text(preset_id, max_length=80)
    if not normalized_id:
        return None
    state = state if state is not None else _load_filter_preset_state()
    return next(
        (
            item
            for item in [*_visible_builtin_filter_presets(state), *list(state.get("custom") or [])]
            if str(item.get("id") or "") == normalized_id
        ),
        None,
    )


def _public_filter_preset(item: dict[str, Any]) -> dict[str, Any]:
    public_item = dict(item)
    public_item.pop("account_refs", None)
    return public_item


def _ordered_config_filter_presets(state: dict[str, Any]) -> list[dict[str, Any]]:
    custom = sorted(
        list(state.get("custom") or []),
        key=lambda item: (
            not bool(item.get("pinned")),
            str(item.get("updated_at") or ""),
        ),
    )
    return [*_visible_builtin_filter_presets(state), *custom]


def _find_dynamic_filter_preset(
    preset_id: str,
    state: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    item = _find_visible_filter_preset(preset_id, state)
    if item and item.get("mode") != ACCOUNT_FILTER_PRESET_MODE_FIXED:
        return item
    return None


def _filter_preset_filters_to_request(filters: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalize_filter_preset_filters(filters)
    columns = normalized.get("columnFilters") if isinstance(normalized.get("columnFilters"), dict) else {}

    def joined(key: str) -> str:
        return ",".join(_filter_value_list(columns.get(key)))

    return {
        "email": normalized.get("search") or "",
        "status": ",".join(_filter_value_list(normalized.get("status"))),
        "manually_used": joined("manuallyUsed") or None,
        "auth_type": joined("authType"),
        "phone_binding_state": joined("phoneBindingState"),
        "payment_link_platform": joined("paymentLinkPlatform"),
        "payment_link_generated": joined("paymentLinkGenerated") or None,
        "subscription_type": joined("subscriptionType"),
        "account_validity": joined("accountValidity"),
        "sub2api_state": joined("sub2apiState"),
        "oaipay_state": joined("oaipayState"),
        "zero_amount_eligibility_state": joined("zeroAmountEligibilityState"),
        "gcash_payment_method_state": joined("gcashPaymentMethodState"),
        "submit_state": joined("submitState") or joined("ideaSubmitState"),
        "has_submitted": joined("hasSubmitted") or None,
    }


def _fixed_group_parent_matching_ids(
    session: Session,
    *,
    parent: dict[str, Any],
    account_ids: list[int],
) -> set[int]:
    requested_ids = _normalize_filter_preset_account_ids(account_ids)
    if not requested_ids:
        return set()
    query, _, _ = account_filtered_query(
        session,
        platform="chatgpt",
        filter_source=_filter_preset_filters_to_request(parent.get("filters") or {}),
        include_fixed_members=True,
    )
    return {
        int(account.id)
        for account in session.exec(query.where(AccountModel.id.in_(requested_ids))).all()
        if _safe_int(account.id) > 0
    }


def _validate_fixed_group_parent_members(
    session: Session,
    *,
    parent: dict[str, Any],
    account_ids: list[int],
) -> None:
    requested_ids = _normalize_filter_preset_account_ids(account_ids)
    if not requested_ids:
        raise HTTPException(400, "固定账号组合至少需要一个有效账号")
    matched_ids = _fixed_group_parent_matching_ids(
        session,
        parent=parent,
        account_ids=requested_ids,
    )
    outside_ids = [account_id for account_id in requested_ids if account_id not in matched_ids]
    if outside_ids:
        raise HTTPException(
            409,
            {
                "code": "FIXED_GROUP_PARENT_SCOPE_CHANGED",
                "message": "所选账号已不再全部满足一级条件组合，请刷新列表后重新选择",
                "account_ids": outside_ids,
            },
        )


def _build_filter_presets_response(
    session: Session,
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    state = state if state is not None else _load_filter_preset_state()
    config_items = _ordered_config_filter_presets(state)
    dynamic_items = [item for item in config_items if item.get("mode") != ACCOUNT_FILTER_PRESET_MODE_FIXED]
    legacy_fixed_items = [item for item in config_items if item.get("mode") == ACCOUNT_FILTER_PRESET_MODE_FIXED]
    fixed_items = serialize_fixed_groups(session, list_fixed_groups(session))
    public_dynamic = [_public_filter_preset(item) for item in dynamic_items]
    public_legacy = [_public_filter_preset(item) for item in legacy_fixed_items]
    return {
        "ok": True,
        # `items` remains a compatibility union for older clients. New clients
        # consume the explicit primary/secondary arrays below.
        "items": [*public_dynamic, *fixed_items, *public_legacy],
        "dynamic_items": public_dynamic,
        "fixed_groups": fixed_items,
        "legacy_fixed_items": public_legacy,
        "migration_required": bool(public_legacy),
        "built_in_count": sum(1 for item in dynamic_items if item.get("built_in")),
        "custom_count": sum(1 for item in dynamic_items if not item.get("built_in")),
        "dynamic_count": len(dynamic_items),
        "fixed_count": len(fixed_items),
        "legacy_fixed_count": len(legacy_fixed_items),
        "deleted_builtin_count": len(set(state.get("deleted_builtin_ids") or set())),
        "builtin_override_count": len(dict(state.get("builtin_overrides") or {})),
    }


@router.get("/filter-presets")
def list_account_filter_presets(session: Session = Depends(get_session)):
    return _build_filter_presets_response(session)


def _resolved_legacy_fixed_account_ids(
    session: Session,
    preset: dict[str, Any],
) -> tuple[list[int], list[int]]:
    account_ids = _normalize_filter_preset_account_ids(preset.get("account_ids"))
    refs = _normalize_filter_preset_account_refs(preset.get("account_refs"), account_ids)
    refs_by_id = {int(ref["id"]): ref for ref in refs}
    if not account_ids:
        return [], []
    accounts = session.exec(
        select(AccountModel).where(
            AccountModel.platform == "chatgpt",
            AccountModel.id.in_(account_ids),
        )
    ).all()
    resolved_set = {
        int(account.id)
        for account in accounts
        if _safe_int(account.id) > 0
        and _filter_preset_account_matches_ref(account, refs_by_id.get(int(account.id)))
    }
    return (
        [account_id for account_id in account_ids if account_id in resolved_set],
        [account_id for account_id in account_ids if account_id not in resolved_set],
    )


def _fixed_preset_migration_plan(
    session: Session,
    body: FixedPresetMigrationBody,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    state = _load_filter_preset_state()
    legacy_items = [
        item
        for item in _ordered_config_filter_presets(state)
        if item.get("mode") == ACCOUNT_FILTER_PRESET_MODE_FIXED
    ]
    if not legacy_items:
        return state, {
            "groups": [],
            "conflict_account_count": 0,
            "missing_account_count": 0,
            "outside_parent_account_count": 0,
        }, []
    if any(item.get("built_in") for item in legacy_items):
        raise HTTPException(409, "内置组合已被改成旧固定模式，请先恢复为一级条件组合后再迁移")

    legacy_by_id = {str(item.get("id") or ""): item for item in legacy_items}
    requested_order = [
        _trim_text(item, max_length=80)
        for item in body.priority_order
        if _trim_text(item, max_length=80)
    ]
    if not requested_order:
        raise HTTPException(400, "迁移前必须明确排列全部旧固定组合的归属优先级")
    if set(requested_order) != set(legacy_by_id) or len(requested_order) != len(legacy_by_id):
        raise HTTPException(400, "迁移优先级必须完整包含全部旧固定组合")

    parent_by_id: dict[str, str] = {}
    for preset_id in requested_order:
        parent_id = _trim_text(body.parent_by_preset_id.get(preset_id), max_length=80)
        parent = _find_dynamic_filter_preset(parent_id, state)
        if not parent:
            raise HTTPException(400, f"旧固定组合 {legacy_by_id[preset_id].get('name') or preset_id} 尚未选择有效的一级条件组合")
        existing_group = get_fixed_group(session, preset_id)
        if existing_group is not None and existing_group.parent_preset_id != parent_id:
            raise HTTPException(409, f"旧固定组合 {legacy_by_id[preset_id].get('name') or preset_id} 已存在于其他一级条件组合下")
        if fixed_group_name_exists(
            session,
            parent_preset_id=parent_id,
            name=str(legacy_by_id[preset_id].get("name") or ""),
            ignore_group_id=preset_id,
        ):
            raise HTTPException(409, f"一级条件组合下已存在同名固定账号组合：{legacy_by_id[preset_id].get('name') or preset_id}")
        parent_by_id[preset_id] = parent_id

    claimed_by = {
        int(row.account_id): str(row.fixed_group_id)
        for row in session.exec(select(AccountFixedGroupMemberModel)).all()
    }
    plan_groups: list[dict[str, Any]] = []
    commit_groups: list[dict[str, Any]] = []
    all_conflicts: set[int] = set()
    all_missing: set[int] = set()
    all_outside_parent: set[int] = set()
    for priority, preset_id in enumerate(requested_order, start=1):
        preset = legacy_by_id[preset_id]
        resolved_ids, missing_ids = _resolved_legacy_fixed_account_ids(session, preset)
        parent = _find_dynamic_filter_preset(parent_by_id[preset_id], state)
        parent_matched_ids = _fixed_group_parent_matching_ids(
            session,
            parent=parent or {},
            account_ids=resolved_ids,
        )
        outside_parent_ids = [account_id for account_id in resolved_ids if account_id not in parent_matched_ids]
        eligible_ids = [account_id for account_id in resolved_ids if account_id in parent_matched_ids]
        conflict_ids = [
            account_id
            for account_id in eligible_ids
            if account_id in claimed_by and claimed_by[account_id] != preset_id
        ]
        assigned_ids = [
            account_id
            for account_id in eligible_ids
            if account_id not in claimed_by or claimed_by[account_id] == preset_id
        ]
        for account_id in assigned_ids:
            claimed_by[account_id] = preset_id
        all_conflicts.update(conflict_ids)
        all_missing.update(missing_ids)
        all_outside_parent.update(outside_parent_ids)
        plan_item = {
            "id": preset_id,
            "name": str(preset.get("name") or ""),
            "parent_preset_id": parent_by_id[preset_id],
            "priority": priority,
            "stored_account_count": len(_normalize_filter_preset_account_ids(preset.get("account_ids"))),
            "resolved_account_count": len(resolved_ids),
            "assigned_account_count": len(assigned_ids),
            "conflict_account_count": len(conflict_ids),
            "missing_account_count": len(missing_ids),
            "outside_parent_account_count": len(outside_parent_ids),
        }
        plan_groups.append(plan_item)
        commit_groups.append({"preset": preset, "plan": plan_item, "account_ids": assigned_ids})
    return state, {
        "groups": plan_groups,
        "conflict_account_count": len(all_conflicts),
        "missing_account_count": len(all_missing),
        "outside_parent_account_count": len(all_outside_parent),
    }, commit_groups


def _backup_account_database_before_fixed_migration() -> str:
    database_path = str(engine.url.database or "").strip()
    if not database_path or database_path == ":memory:":
        return ""
    source_path = Path(database_path).resolve()
    backup_dir = source_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = backup_dir / f"{source_path.stem}.before-fixed-group-migration-{stamp}{source_path.suffix or '.db'}"
    source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
    target = sqlite3.connect(str(backup_path))
    try:
        source.backup(target)
        integrity = target.execute("PRAGMA integrity_check").fetchone()
        if not integrity or str(integrity[0]).lower() != "ok":
            raise RuntimeError("固定组合迁移备份完整性检查失败")
    finally:
        target.close()
        source.close()
    return str(backup_path)


@router.post("/filter-presets/migrate-fixed")
def migrate_legacy_fixed_presets(
    body: FixedPresetMigrationBody,
    session: Session = Depends(get_session),
):
    state, preview, commit_groups = _fixed_preset_migration_plan(session, body)
    if not body.commit or not commit_groups:
        return {
            "ok": True,
            "committed": False,
            "preview": preview,
            **_build_filter_presets_response(session, state),
        }
    if int(preview.get("outside_parent_account_count") or 0) > 0:
        raise HTTPException(
            409,
            {
                "code": "FIXED_GROUP_PARENT_SCOPE_CHANGED",
                "message": "部分旧固定账号不满足所选一级条件组合，请调整父级后重新生成预览",
                "outside_parent_account_count": int(preview["outside_parent_account_count"]),
            },
        )

    try:
        backup_path = _backup_account_database_before_fixed_migration()
    except Exception as exc:
        raise HTTPException(500, f"创建固定组合迁移备份失败: {exc}") from exc

    migrated_ids: list[str] = []
    try:
        for item in commit_groups:
            preset = item["preset"]
            plan_item = item["plan"]
            preset_id = str(preset.get("id") or "")
            group = get_fixed_group(session, preset_id)
            if group is None:
                group = create_fixed_group(
                    session,
                    group_id=preset_id,
                    parent_preset_id=plan_item["parent_preset_id"],
                    name=str(preset.get("name") or ""),
                    description=str(preset.get("description") or ""),
                    pinned=bool(preset.get("pinned")),
                )
            if item["account_ids"]:
                replace_fixed_group_members(
                    session,
                    group,
                    item["account_ids"],
                    move_conflicts=False,
                    max_members=ACCOUNT_FILTER_PRESET_MAX_ACCOUNT_IDS,
                )
            migrated_ids.append(preset_id)
        session.commit()
    except Exception:
        session.rollback()
        raise

    state["custom"] = [
        item
        for item in list(state.get("custom") or [])
        if str(item.get("id") or "") not in set(migrated_ids)
    ]
    state = _save_filter_preset_state(state)
    return {
        "ok": True,
        "committed": True,
        "preview": preview,
        "migrated_ids": migrated_ids,
        "backup_path": backup_path,
        **_build_filter_presets_response(session, state),
    }


@router.post("/filter-presets")
def create_account_filter_preset(
    body: AccountFilterPresetBody,
    session: Session = Depends(get_session),
):
    name = _trim_text(body.name, max_length=80)
    if not name:
        raise HTTPException(400, "筛选组合名称不能为空")
    mode, filters, account_ids, account_refs, discarded_ids = _validate_filter_preset_content(body, session=session)
    state = _load_filter_preset_state()
    if mode == ACCOUNT_FILTER_PRESET_MODE_FIXED:
        parent_preset_id = _trim_text(body.parent_preset_id, max_length=80)
        parent = _find_dynamic_filter_preset(parent_preset_id, state)
        if not parent:
            raise HTTPException(400, "请选择有效的一级条件筛选组合")
        if fixed_group_name_exists(
            session,
            parent_preset_id=parent_preset_id,
            name=name,
        ):
            raise HTTPException(400, "当前一级组合下已存在同名固定账号组合")
        _validate_fixed_group_parent_members(
            session,
            parent=parent,
            account_ids=account_ids,
        )
        group = create_fixed_group(
            session,
            group_id="fixed_" + uuid.uuid4().hex[:12],
            parent_preset_id=parent_preset_id,
            name=name,
            description=_trim_text(body.description, max_length=240),
            pinned=bool(body.pinned),
        )
        try:
            member_result = replace_fixed_group_members(
                session,
                group,
                account_ids,
                move_conflicts=bool(body.move_conflicts),
                max_members=ACCOUNT_FILTER_PRESET_MAX_ACCOUNT_IDS,
            )
        except FixedGroupConflictError as exc:
            session.rollback()
            raise HTTPException(
                409,
                {
                    "code": "FIXED_GROUP_MEMBER_CONFLICT",
                    "message": "部分账号已属于其他固定账号组合",
                    "conflicts": exc.conflicts,
                },
            ) from exc
        except ValueError as exc:
            session.rollback()
            raise HTTPException(400, str(exc)) from exc
        session.commit()
        session.refresh(group)
        saved_group = serialize_fixed_group(session, group)
        return {
            "ok": True,
            "item": saved_group,
            "discarded_account_ids": _normalize_filter_preset_account_ids([
                *discarded_ids,
                *member_result["discarded_account_ids"],
            ]),
            **_build_filter_presets_response(session, state),
        }

    if _duplicate_filter_preset_name(state, name):
        raise HTTPException(400, "已存在同名筛选组合")
    now = _utc_iso()
    item = {
        "id": "preset_" + uuid.uuid4().hex[:12],
        "name": name,
        "description": _trim_text(body.description, max_length=240),
        "mode": mode,
        "filters": filters,
        "account_ids": account_ids,
        "account_refs": account_refs,
        "account_count": len(account_ids),
        "pinned": bool(body.pinned),
        "built_in": False,
        "created_at": now,
        "updated_at": now,
    }
    item["summary"] = _filter_preset_content_summary(mode, filters, account_ids)
    state["custom"].append(item)
    state = _save_filter_preset_state(state)
    saved_item = _find_visible_filter_preset(item["id"], state) or item
    return {
        "ok": True,
        "item": _public_filter_preset(saved_item),
        "discarded_account_ids": discarded_ids,
        **_build_filter_presets_response(session, state),
    }


@router.put("/filter-presets/{preset_id}")
def update_account_filter_preset(
    preset_id: str,
    body: AccountFilterPresetBody,
    session: Session = Depends(get_session),
):
    preset_id = _trim_text(preset_id, max_length=80)
    name = _trim_text(body.name, max_length=80)
    if not name:
        raise HTTPException(400, "筛选组合名称不能为空")
    state = _load_filter_preset_state()
    fixed_group = get_fixed_group(session, preset_id)
    if fixed_group is not None:
        if _normalize_filter_preset_mode(body.mode) != ACCOUNT_FILTER_PRESET_MODE_FIXED:
            raise HTTPException(400, "固定账号组合不能转换为一级条件组合")
        requested_parent_id = _trim_text(body.parent_preset_id, max_length=80)
        if requested_parent_id and requested_parent_id != fixed_group.parent_preset_id:
            raise HTTPException(400, "固定账号组合不能通过普通编辑更换一级组合")
        if fixed_group_name_exists(
            session,
            parent_preset_id=fixed_group.parent_preset_id,
            name=name,
            ignore_group_id=fixed_group.id,
        ):
            raise HTTPException(400, "当前一级组合下已存在同名固定账号组合")
        account_ids = _normalize_filter_preset_account_ids(body.account_ids)
        if len(list(body.account_ids or [])) > ACCOUNT_FILTER_PRESET_MAX_ACCOUNT_IDS:
            raise HTTPException(400, f"固定账号组合最多保存 {ACCOUNT_FILTER_PRESET_MAX_ACCOUNT_IDS} 个账号")
        parent = _find_dynamic_filter_preset(fixed_group.parent_preset_id, state)
        if not parent:
            raise HTTPException(409, "固定账号组合所属的一级条件组合已不存在")
        current_member_ids = set(fixed_group_member_ids(session, fixed_group.id))
        added_account_ids = [account_id for account_id in account_ids if account_id not in current_member_ids]
        if added_account_ids:
            _validate_fixed_group_parent_members(
                session,
                parent=parent,
                account_ids=added_account_ids,
            )
        update_fixed_group_meta(
            session,
            fixed_group,
            name=name,
            description=_trim_text(body.description, max_length=240),
            pinned=bool(body.pinned),
        )
        try:
            member_result = replace_fixed_group_members(
                session,
                fixed_group,
                account_ids,
                move_conflicts=bool(body.move_conflicts),
                max_members=ACCOUNT_FILTER_PRESET_MAX_ACCOUNT_IDS,
            )
        except FixedGroupConflictError as exc:
            session.rollback()
            raise HTTPException(
                409,
                {
                    "code": "FIXED_GROUP_MEMBER_CONFLICT",
                    "message": "部分账号已属于其他固定账号组合",
                    "conflicts": exc.conflicts,
                },
            ) from exc
        except ValueError as exc:
            session.rollback()
            raise HTTPException(400, str(exc)) from exc
        session.commit()
        session.refresh(fixed_group)
        saved_group = serialize_fixed_group(session, fixed_group)
        return {
            "ok": True,
            "item": saved_group,
            "discarded_account_ids": member_result["discarded_account_ids"],
            **_build_filter_presets_response(session, state),
        }

    if preset_id in BUILTIN_ACCOUNT_FILTER_PRESETS_BY_ID and preset_id in set(state.get("deleted_builtin_ids") or set()):
        raise HTTPException(404, "筛选组合不存在")
    is_builtin = preset_id in BUILTIN_ACCOUNT_FILTER_PRESETS_BY_ID
    items = list(state.get("custom") or [])
    index = next((idx for idx, item in enumerate(items) if str(item.get("id") or "") == preset_id), -1)
    if not is_builtin and index < 0:
        raise HTTPException(404, "筛选组合不存在")
    if _duplicate_filter_preset_name(state, name, ignore_id=preset_id):
        raise HTTPException(400, "已存在同名筛选组合")
    mode, filters, account_ids, account_refs, discarded_ids = _validate_filter_preset_content(body, session=session)
    existing_item = (
        (state.get("builtin_overrides") or {}).get(preset_id)
        or BUILTIN_ACCOUNT_FILTER_PRESETS_BY_ID.get(preset_id)
        if is_builtin
        else items[index]
    )
    if existing_item and existing_item.get("mode") != mode:
        raise HTTPException(400, "一级条件组合与固定账号组合不能相互转换")

    if is_builtin:
        current = dict(
            (state.get("builtin_overrides") or {}).get(preset_id)
            or BUILTIN_ACCOUNT_FILTER_PRESETS_BY_ID[preset_id]
        )
        updated = {
            **current,
            "id": preset_id,
            "name": name,
            "description": _trim_text(body.description, max_length=240),
            "mode": mode,
            "filters": filters,
            "account_ids": account_ids,
            "account_refs": account_refs,
            "account_count": len(account_ids),
            "pinned": bool(body.pinned),
            "built_in": True,
            "updated_at": _utc_iso(),
        }
        updated["summary"] = _filter_preset_content_summary(mode, filters, account_ids)
        state.setdefault("builtin_overrides", {})[preset_id] = updated
        state["deleted_builtin_ids"] = set(state.get("deleted_builtin_ids") or set()) - {preset_id}
        state = _save_filter_preset_state(state)
        saved_item = _find_visible_filter_preset(preset_id, state) or updated
        return {
            "ok": True,
            "item": _public_filter_preset(saved_item),
            "discarded_account_ids": discarded_ids,
            **_build_filter_presets_response(session, state),
        }

    current = dict(items[index])
    updated = {
        **current,
        "name": name,
        "description": _trim_text(body.description, max_length=240),
        "mode": mode,
        "filters": filters,
        "account_ids": account_ids,
        "account_refs": account_refs,
        "account_count": len(account_ids),
        "pinned": bool(body.pinned),
        "built_in": False,
        "updated_at": _utc_iso(),
    }
    updated["summary"] = _filter_preset_content_summary(mode, filters, account_ids)
    items[index] = updated
    state["custom"] = items
    state = _save_filter_preset_state(state)
    saved_item = _find_visible_filter_preset(preset_id, state) or updated
    return {
        "ok": True,
        "item": _public_filter_preset(saved_item),
        "discarded_account_ids": discarded_ids,
        **_build_filter_presets_response(session, state),
    }


@router.delete("/filter-presets/{preset_id}")
def delete_account_filter_preset(
    preset_id: str,
    session: Session = Depends(get_session),
):
    preset_id = _trim_text(preset_id, max_length=80)
    state = _load_filter_preset_state()
    fixed_group = get_fixed_group(session, preset_id)
    if fixed_group is not None:
        released_ids = delete_fixed_group(session, fixed_group)
        session.commit()
        return {
            **_build_filter_presets_response(session, state),
            "released_account_ids": released_ids,
        }

    child_groups = list_fixed_groups(session, parent_preset_id=preset_id)
    if child_groups:
        raise HTTPException(
            409,
            {
                "code": "FILTER_PRESET_HAS_FIXED_GROUPS",
                "message": "该条件组合下仍有固定账号组合，请先移动或删除子组合",
                "fixed_group_ids": [group.id for group in child_groups],
            },
        )
    if preset_id in BUILTIN_ACCOUNT_FILTER_PRESETS_BY_ID:
        deleted_ids = set(state.get("deleted_builtin_ids") or set())
        if preset_id in deleted_ids:
            raise HTTPException(404, "筛选组合不存在")
        deleted_ids.add(preset_id)
        state["deleted_builtin_ids"] = deleted_ids
        overrides = dict(state.get("builtin_overrides") or {})
        overrides.pop(preset_id, None)
        state["builtin_overrides"] = overrides
        state = _save_filter_preset_state(state)
        return _build_filter_presets_response(session, state)

    items = list(state.get("custom") or [])
    next_items = [item for item in items if str(item.get("id") or "") != preset_id]
    if len(next_items) == len(items):
        raise HTTPException(404, "筛选组合不存在")
    state["custom"] = next_items
    state = _save_filter_preset_state(state)
    return _build_filter_presets_response(session, state)


def _maybe_reconcile_rate_limited_accounts(session: Session, *, platform: Optional[str] = None) -> None:
    """Keep legacy list-side recovery but avoid doing it for every list GET."""

    key = _safe_str(platform) or "*"
    now = time.monotonic()
    with _list_rate_limit_reconcile_lock:
        last = _list_rate_limit_reconcile_at.get(key, 0)
        if now - last < _LIST_RATE_LIMIT_RECONCILE_SECONDS:
            return
        _list_rate_limit_reconcile_at[key] = now
    reconcile_rate_limited_accounts(session, platform=platform)


def _serialize_account(
    account: AccountModel,
    *,
    payment_link_generated: bool | None = None,
) -> dict[str, Any]:
    data = account.model_dump(mode="json") if hasattr(account, "model_dump") else account.dict()
    extra = _safe_extra(account)
    chatgpt_local = extra.get("chatgpt_local") if isinstance(extra.get("chatgpt_local"), dict) else {}
    chatgpt_auth = chatgpt_local.get("auth") if isinstance(chatgpt_local.get("auth"), dict) else {}
    chatgpt_capabilities = extra.get("chatgpt_capabilities") if isinstance(extra.get("chatgpt_capabilities"), dict) else {}
    auth_summary = _build_auth_summary(account, extra, chatgpt_auth, chatgpt_capabilities)
    rate_limit = account_rate_limit_payload(account, extra=extra)
    data["rate_limit"] = rate_limit
    data["rate_limit_started_at"] = rate_limit["started_at"]
    data["rate_limit_recover_at"] = rate_limit["recover_at"]
    data["rate_limit_previous_status"] = rate_limit["previous_status"]
    data["revival"] = account_revival_info(account, extra)
    bound_phone = chatgpt_bound_phone_payload(extra)
    phone_challenge = chatgpt_phone_challenge_payload(extra)
    data["bound_phone"] = bound_phone
    data["bound_phone_number"] = _safe_str(bound_phone.get("phone") or bound_phone.get("phone_number"))
    data["bound_phone_masked"] = _safe_str(bound_phone.get("masked") or bound_phone.get("masked_phone"))
    data["phone_challenge"] = phone_challenge
    data["auth"] = auth_summary
    data["has_access_token"] = bool(auth_summary["has_access_token"])
    data["has_refresh_token"] = bool(auth_summary["has_refresh_token"])
    data["has_session_token"] = bool(auth_summary["has_session_token"])
    data["has_cookies"] = bool(auth_summary["has_cookies"])
    data["has_id_token"] = bool(auth_summary["has_id_token"])
    data["has_password"] = bool(auth_summary["password_present"])
    data["password_present"] = bool(auth_summary["password_present"])
    data["credentials"] = {
        "has_access_token": bool(auth_summary["has_access_token"]),
        "has_refresh_token": bool(auth_summary["has_refresh_token"]),
        "has_session_token": bool(auth_summary["has_session_token"]),
        "has_cookies": bool(auth_summary["has_cookies"]),
        "has_id_token": bool(auth_summary["has_id_token"]),
        "has_password": bool(auth_summary["password_present"]),
    }
    # Detail endpoints must follow the same secret boundary as compact lists:
    # raw credentials stay behind /accounts/{id}/secrets, while detail responses
    # only expose booleans and curated summaries.  Historically this serializer
    # returned model_dump() verbatim, which leaked token/password/extra_json when
    # callers used /accounts/{id} or /accounts?detail=true.
    compact = _serialize_account_compact_item(
        account,
        extra=extra,
        payment_link_generated=payment_link_generated,
    )
    data.update(compact)
    for secret_key in (
        "access_token",
        "accessToken",
        "refresh_token",
        "refreshToken",
        "session_token",
        "sessionToken",
        "id_token",
        "idToken",
        "cookies",
        "cookie",
        "cookie_header",
        "cookieHeader",
    ):
        data.pop(secret_key, None)
    safe_extra = compact.get("extra") if isinstance(compact.get("extra"), dict) else {}
    data["password"] = ""
    data["token"] = ""
    data["extra"] = safe_extra
    data["extra_json"] = json.dumps(safe_extra, ensure_ascii=False)
    data["secrets_redacted"] = True
    return data


def _serialize_account_list_item(
    account: AccountModel,
    *,
    extra: Optional[dict[str, Any]] = None,
    payment_link_generated: bool | None = None,
) -> dict[str, Any]:
    """Backward-compatible name for the compact list serializer."""

    return _serialize_account_compact_item(
        account,
        extra=extra,
        payment_link_generated=payment_link_generated,
    )


class AccountCreate(BaseModel):
    platform: str
    email: str
    password: str
    status: str = "registered"
    token: str = ""
    cashier_url: str = ""


class AccountUpdate(BaseModel):
    status: Optional[str] = None
    token: Optional[str] = None
    cashier_url: Optional[str] = None


class AccountMarkUsedRequest(BaseModel):
    used: bool = True


class ImportRequest(BaseModel):
    platform: str
    lines: list[str]


class BatchDeleteRequest(BaseModel):
    ids: list[int]


class BatchDeleteByFilterRequest(BaseModel):
    platform: Optional[str] = None
    status: Optional[str] = None
    email: Optional[str] = None


class AccountSnapshotRequest(BaseModel):
    ids: list[int]


def _safe_get_extra(account: AccountModel) -> dict[str, Any]:
    try:
        extra = account.get_extra()
    except Exception:
        return {}
    return extra if isinstance(extra, dict) else {}


def _iso_datetime(value: Any) -> str:
    if isinstance(value, datetime):
        return beijing_iso(value)
    return _safe_str(value)


def _secret_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        try:
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            return str(value).strip()
    return str(value or "").strip()


def _has_secret_value(value: Any) -> bool:
    return bool(_secret_to_text(value))


def _first_secret(account: AccountModel, extra: dict[str, Any], *names: str) -> str:
    for name in names:
        if name == "access_token":
            value = (
                extra.get("access_token")
                or extra.get("accessToken")
                or extra.get("webAccessToken")
                or getattr(account, "token", "")
            )
        elif name == "refresh_token":
            value = extra.get("refresh_token") or extra.get("refreshToken")
        elif name == "session_token":
            value = extra.get("session_token") or extra.get("sessionToken") or extra.get("nextauth_session_token")
        elif name == "id_token":
            value = extra.get("id_token") or extra.get("idToken")
        elif name == "cookies":
            value = extra.get("cookies") or extra.get("cookie") or extra.get("cookie_jar")
        elif name == "cookie_header":
            value = extra.get("cookie_header") or extra.get("cookieHeader") or extra.get("cookies")
        elif name == "password":
            value = getattr(account, "password", "")
        elif name == "token":
            value = getattr(account, "token", "")
        else:
            value = extra.get(name) or getattr(account, name, "")
        text = _secret_to_text(value)
        if text:
            return text
    return ""


def _pick_fields(source: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {key: source.get(key) for key in fields if key in source}


def _build_sync_summary(sync: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(sync, dict) or not sync:
        return {}
    last_upload = sync.get("last_upload") if isinstance(sync.get("last_upload"), dict) else {}
    return _pick_fields(
        sync,
        (
            "remote_state",
            "status",
            "uploaded",
            "uploaded_at",
            "last_attempt_at",
            "checked_at",
            "last_synced_at",
            "last_refresh",
            "next_retry_after",
            "last_probe_status_code",
            "last_probe_error_code",
            "last_probe_message",
            "status_message",
            "message",
            "last_message",
            "matched_by",
            "probe_source",
            "candidate_count",
            "remote_account_id",
            "category_mode",
            "category_source",
            "category_rule",
            "category_id",
            "category_name",
            "requested_category_id",
            "fallback_category_id",
            "auto_group_name",
            "resolved_group",
            "remote_category_id",
            "remote_group",
            "name",
            "base_url",
        ),
    ) | (
        {
            "last_upload": _pick_fields(
                last_upload,
                (
                    "status",
                    "action",
                    "message",
                    "attempted_at",
                    "finished_at",
                    "remote_account_id",
                    "remote_status",
                    "probe_before",
                    "probe_after",
                    "category_mode",
                    "category_source",
                    "category_rule",
                    "category_id",
                    "category_name",
                    "requested_category_id",
                    "fallback_category_id",
                    "auto_group_name",
                    "resolved_group",
                    "remote_category_id",
                    "remote_group",
                ),
            )
        }
        if last_upload
        else {}
    )


def _build_baxigpt_cdk_summary(cdk: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(cdk, dict) or not cdk:
        return {}
    return _pick_fields(
        cdk,
        (
            "status",
            "upstream_status",
            "code_masked",
            "cdk_id",
            "order_id",
            "display_id",
            "remote_email",
            "task_id",
            "bound_at",
            "submitted_at",
            "paid_at",
            "last_checked_at",
            "last_error_message",
            "polling_disabled",
            "polling_disabled_at",
            "polling_disabled_reason",
        ),
    )


def _build_idea_submit_summary(extra: dict[str, Any], baxigpt_cdk: dict[str, Any]) -> dict[str, Any]:
    marker = extra.get("idea_submit") if isinstance(extra.get("idea_submit"), dict) else {}
    unavailable = bool(marker.get("unavailable") or extra.get("idea_submit_unavailable"))
    if (
        not unavailable
        and bool(extra.get("chatgpt_account_unavailable"))
        and _safe_str(baxigpt_cdk.get("status")).lower() == "failed"
    ):
        unavailable = True
    reason = _safe_str(
        marker.get("reason")
        or baxigpt_cdk.get("polling_disabled_reason")
        or extra.get("idea_submit_unavailable_reason")
        or (extra.get("chatgpt_unavailable_reason") if unavailable else "")
        or (baxigpt_cdk.get("last_error_message") if unavailable else "")
    )
    status = "unavailable" if unavailable else "available"
    cdk_status = _safe_str(baxigpt_cdk.get("status")).lower()
    marker_status = _safe_str(marker.get("status")).lower()
    polling_disabled = bool(baxigpt_cdk.get("polling_disabled")) or cdk_status == "stopped"
    current_status = "stopped" if polling_disabled else (
        cdk_status if cdk_status in {"paid", "submitted", "processing", "failed", "timeout", "stopped"} else marker_status
    )
    if not unavailable and current_status in {"paid", "submitted", "processing", "failed", "timeout", "stopped"}:
        status = current_status
    return {
        "status": status,
        "available": not unavailable,
        "unavailable": unavailable,
        "reason": reason,
        "polling_disabled": polling_disabled,
        "marked_at": _safe_str(marker.get("marked_at") or extra.get("idea_submit_unavailable_at")),
        "cleared_at": _safe_str(marker.get("cleared_at")),
        "source": _safe_str(marker.get("source") or ("baxigpt_cdk_submit" if marker else "")),
        "cdk_id": _safe_int(marker.get("cdk_id") or baxigpt_cdk.get("cdk_id")),
        "code_masked": _safe_str(marker.get("code_masked") or baxigpt_cdk.get("code_masked")),
        "task_id": _safe_str(marker.get("task_id") or baxigpt_cdk.get("task_id")),
        "order_id": _safe_str(marker.get("order_id") or baxigpt_cdk.get("order_id")),
        "display_id": _safe_str(marker.get("display_id") or baxigpt_cdk.get("display_id")),
    }


def _build_submission_summary(
    extra: dict[str, Any],
    baxigpt_cdk: dict[str, Any],
    submission_info: dict[str, Any],
) -> dict[str, Any]:
    """Build the non-secret channel-neutral submission list payload."""

    marker = extra.get("idea_submit") if isinstance(extra.get("idea_submit"), dict) else {}
    raw_cdk = extra.get("baxigpt_cdk") if isinstance(extra.get("baxigpt_cdk"), dict) else {}
    payment_link = (
        extra.get("chatgpt_last_payment_link")
        if isinstance(extra.get("chatgpt_last_payment_link"), dict)
        else {}
    )
    legacy = _build_idea_submit_summary(extra, baxigpt_cdk)
    state = _safe_str(submission_info.get("state")).lower() or "available"
    payment_link_type = payment_link_type_from_payload(payment_link)
    link_is_pix = bool(
        submission_info.get("link_submitted")
        or payment_link_type == "pix"
    )
    payment_channel = _safe_str(marker.get("payment_channel") or raw_cdk.get("payment_channel")).lower()
    if link_is_pix:
        payment_channel = "pix"
    elif payment_link_type == "upi":
        payment_channel = "upi"
    elif not payment_channel and (marker or raw_cdk):
        payment_channel = "ideal"
    reason = _safe_str(
        marker.get("reason")
        or raw_cdk.get("last_error_message")
        or legacy.get("reason")
    )
    source = _safe_str(marker.get("source") or legacy.get("source"))
    if not source and raw_cdk:
        source = "baxigpt_cdk_submit"
    return {
        "status": state,
        "state": state,
        "has_submitted": bool(submission_info.get("has_submitted")),
        "link_submitted": bool(submission_info.get("link_submitted")),
        "link_status": _safe_str(submission_info.get("link_status")).lower(),
        "unavailable": bool(submission_info.get("unavailable")),
        "eligibility_state": "unavailable" if submission_info.get("unavailable") else "available",
        "reason": reason,
        "source": source,
        "payment_channel": payment_channel,
        "pix_submit_mode": _safe_str(marker.get("pix_submit_mode") or raw_cdk.get("pix_submit_mode")),
        "cdk_id": _safe_int(marker.get("cdk_id") or baxigpt_cdk.get("cdk_id")),
        "code_masked": _safe_str(marker.get("code_masked") or baxigpt_cdk.get("code_masked")),
        "task_id": _safe_str(marker.get("task_id") or baxigpt_cdk.get("task_id")),
        "order_id": _safe_str(marker.get("order_id") or baxigpt_cdk.get("order_id")),
        "display_id": _safe_str(marker.get("display_id") or baxigpt_cdk.get("display_id")),
        "submitted_at": _safe_str(raw_cdk.get("submitted_at")),
        "paid_at": _safe_str(raw_cdk.get("paid_at")),
        "last_checked_at": _safe_str(raw_cdk.get("last_checked_at")),
        "pix_submitted_at": _safe_str(payment_link.get("pix_submitted_at")),
        "link_status_updated_at": _safe_str(payment_link.get("link_status_updated_at")),
    }


def _truthy_value(value: Any) -> bool:
    return _safe_str(value).lower() in {"1", "true", "yes", "on"}


def _current_subscription_plan(subscription: dict[str, Any], capabilities: dict[str, Any]) -> str:
    local_plan = normalize_subscription_plan(subscription.get("plan"))
    if local_plan != "unknown":
        return local_plan
    capabilities_plan = normalize_subscription_plan(capabilities.get("subscription_plan"))
    if capabilities_plan != "unknown" and _truthy_value(capabilities.get("subscription_checked")):
        return capabilities_plan
    return "unknown"


def _last_known_subscription_plan(
    subscription: dict[str, Any],
    capabilities: dict[str, Any],
    extra: dict[str, Any],
    current_plan: str,
) -> str:
    if current_plan != "unknown":
        return current_plan
    last_confirmed = extra.get("chatgpt_last_confirmed_subscription") if isinstance(extra.get("chatgpt_last_confirmed_subscription"), dict) else {}
    for candidate in (
        capabilities.get("last_known_subscription_plan"),
        subscription.get("last_known_plan"),
        extra.get("last_known_subscription_plan"),
        capabilities.get("subscription_plan"),
        extra.get("chatgpt_plan_type"),
        extra.get("chatgpt_subscription_plan"),
        last_confirmed.get("plan"),
    ):
        resolved = normalize_subscription_plan(candidate)
        if resolved != "unknown":
            return resolved
    return ""


def _subscription_refresh_state(
    subscription: dict[str, Any],
    capabilities: dict[str, Any],
    auth: dict[str, Any],
    refresh_meta: dict[str, Any],
    current_plan: str,
    last_known_plan: str,
) -> str:
    auth_level = _safe_str(capabilities.get("auth_level")).lower()
    upload_gate = _safe_str(capabilities.get("upload_gate")).lower()
    auth_state = _safe_str(auth.get("state")).lower()
    if auth_level == "invalid" or upload_gate == "blocked_auth_invalid" or auth_state in AUTH_INVALID_STATES:
        return "auth_invalid"
    refresh_state = _safe_str(refresh_meta.get("state")).lower()
    if refresh_state in {"pending", "running", "retry_wait"}:
        return "refreshing"
    if refresh_state == "failed":
        return "refresh_failed"
    explicit = _safe_str(capabilities.get("subscription_refresh_state")).lower()
    if explicit:
        return explicit
    if current_plan != "unknown":
        return "confirmed"
    if auth_state == "probe_failed":
        return "refresh_failed"
    if last_known_plan and subscription:
        return "refresh_failed"
    if not auth_state:
        return "not_checked"
    if auth_state in {"unknown", "not_checked", "missing_refresh_token"}:
        return "not_checked"
    return "unknown_plan"


def _build_subscription_summary(
    subscription: dict[str, Any],
    capabilities: dict[str, Any],
    extra: dict[str, Any],
    auth: dict[str, Any] | None = None,
) -> dict[str, Any]:
    auth = auth if isinstance(auth, dict) else {}
    refresh_meta = extra.get("chatgpt_local_refresh") if isinstance(extra.get("chatgpt_local_refresh"), dict) else {}
    current_plan = _current_subscription_plan(subscription, capabilities)
    last_known_plan = _last_known_subscription_plan(subscription, capabilities, extra, current_plan)
    refresh_state = _subscription_refresh_state(
        subscription,
        capabilities,
        auth,
        refresh_meta,
        current_plan,
        last_known_plan,
    )
    stale = current_plan == "unknown" and bool(last_known_plan)
    refresh_checked_at = _safe_str(
        subscription.get("checked_at")
        or refresh_meta.get("completed_at")
        or refresh_meta.get("started_at")
        or refresh_meta.get("requested_at")
    )
    return {
        "plan": current_plan,
        "last_known_plan": last_known_plan,
        "refresh_state": refresh_state,
        "stale": stale,
        "workspace_plan_type": _safe_str(subscription.get("workspace_plan_type")),
        "active_until": _safe_str(
            subscription.get("subscription_active_until")
            or subscription.get("subscription_expires_at_iso")
            or subscription.get("subscription_expires_at")
            or extra.get("subscription_active_until")
            or extra.get("subscription_expires_at")
            or extra.get("chatgpt_subscription_active_until")
        ),
        "checked_at": refresh_checked_at,
        "source": _safe_str(subscription.get("source")),
        "has_paid_subscription": current_plan in {"plus", "pro", "team", "enterprise"},
        "last_known_has_paid_subscription": last_known_plan in {"plus", "pro", "team", "enterprise"},
        "subscription_checked": current_plan != "unknown",
        "refresh_attempt_count": _safe_int(refresh_meta.get("attempt_count")),
        "refresh_max_attempts": _safe_int(refresh_meta.get("max_attempts")),
        "refresh_last_error": sanitize_error_message(refresh_meta.get("last_error"))[:500],
        "refresh_requested_at": _safe_str(refresh_meta.get("requested_at")),
        "refresh_started_at": _safe_str(refresh_meta.get("started_at")),
        "refresh_completed_at": _safe_str(refresh_meta.get("completed_at")),
        "refresh_canonical_preserved": bool(refresh_meta.get("canonical_preserved")),
    }


def _build_codex_summary(codex: dict[str, Any], capabilities: dict[str, Any]) -> dict[str, Any]:
    usage = codex.get("usage") if isinstance(codex.get("usage"), dict) else {}
    return {
        "state": _safe_str(codex.get("state") or capabilities.get("codex_state")),
        "checked_at": _safe_str(codex.get("checked_at")),
        "source": _safe_str(codex.get("source")),
        "http_status": _safe_int(codex.get("http_status")),
        "error_code": _safe_str(codex.get("error_code")),
        "message": _safe_str(codex.get("message")),
        "chatgpt_account_id": _safe_str(codex.get("chatgpt_account_id") or capabilities.get("account_id")),
        "usage": _pick_fields(
            usage,
            (
                "codex_usage_updated_at",
                "codex_5h_used_percent",
                "codex_5h_reset_after_seconds",
                "codex_5h_reset_at",
                "codex_5h_window_minutes",
                "codex_7d_used_percent",
                "codex_7d_reset_after_seconds",
                "codex_7d_reset_at",
                "codex_7d_window_minutes",
                "codex_primary_used_percent",
                "codex_primary_reset_after_seconds",
                "codex_primary_window_minutes",
                "codex_secondary_used_percent",
                "codex_secondary_reset_after_seconds",
                "codex_secondary_window_minutes",
                "codex_primary_over_secondary_percent",
            ),
        ),
        "progress": build_codex_usage_progress_from_extra(usage),
    }


def _build_auth_summary(
    account: AccountModel,
    extra: dict[str, Any],
    auth: dict[str, Any],
    capabilities: dict[str, Any],
) -> dict[str, Any]:
    has_access_token = _has_secret_value(_first_secret(account, extra, "access_token"))
    has_refresh_token = _has_secret_value(_first_secret(account, extra, "refresh_token"))
    has_session_token = _has_secret_value(_first_secret(account, extra, "session_token"))
    has_cookies = _has_secret_value(_first_secret(account, extra, "cookies"))
    has_id_token = _has_secret_value(_first_secret(account, extra, "id_token"))
    return {
        "level": _safe_str(capabilities.get("auth_level") or extra.get("auth_level")),
        "state": _safe_str(auth.get("state")),
        "checked_at": _safe_str(auth.get("checked_at")),
        "source": _safe_str(auth.get("source") or extra.get("chatgpt_token_source")),
        "http_status": _safe_int(auth.get("http_status")),
        "error_code": _safe_str(auth.get("error_code")),
        "message": _safe_str(auth.get("message")),
        "upload_gate": _safe_str(capabilities.get("upload_gate")),
        "has_access_token": has_access_token,
        "has_refresh_token": has_refresh_token,
        "has_session_token": has_session_token,
        "has_cookies": has_cookies,
        "has_id_token": has_id_token,
        "password_present": _has_secret_value(account.password),
    }


def _build_account_validity_summary(
    account: AccountModel,
    auth_summary: dict[str, Any],
    capabilities: dict[str, Any],
    codex_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    codex_summary = codex_summary if isinstance(codex_summary, dict) else {}
    auth_level = _safe_str(auth_summary.get("level") or capabilities.get("auth_level")).lower()
    upload_gate = _safe_str(capabilities.get("upload_gate")).lower()
    auth_state = _safe_str(auth_summary.get("state")).lower()
    codex_state = _safe_str(codex_summary.get("state") or capabilities.get("codex_state")).lower()
    auth_http_status = _safe_int(auth_summary.get("http_status"))
    codex_http_status = _safe_int(codex_summary.get("http_status"))
    if (
        _safe_str(account.status).lower() == "invalid"
        or auth_level == "invalid"
        or upload_gate == "blocked_auth_invalid"
        or auth_state in AUTH_INVALID_STATES
        or codex_state in AUTH_INVALID_STATES
        or auth_http_status == 401
        or codex_http_status == 401
    ):
        reason = "auth_invalid" if auth_level == "invalid" or auth_state in AUTH_INVALID_STATES else "status_invalid"
        if codex_state in AUTH_INVALID_STATES or codex_http_status == 401:
            reason = "codex_auth_invalid"
        return {"state": "invalid", "valid": False, "reason": reason}
    if auth_state == "probe_failed" or codex_state == "probe_failed":
        return {"state": "refresh_failed", "valid": False, "reason": "probe_failed"}
    if not auth_state and not auth_level:
        return {"state": "not_checked", "valid": False, "reason": "not_checked"}
    return {"state": "valid", "valid": True, "reason": ""}


def _build_phone_summary(
    phone_binding: dict[str, Any],
    bound_phone: dict[str, Any],
    phone_challenge: dict[str, Any],
) -> dict[str, Any]:
    binding_picked = _pick_fields(
        phone_binding,
        (
            "phone",
            "api_url",
            "source_api_url",
            "status",
            "status_label",
            "api_expired_date",
            "code_time",
            "code_extracted",
            "bound_at",
            "task_id",
            "source",
        ),
    )
    if not binding_picked.get("phone") and bound_phone.get("phone"):
        binding_picked["phone"] = bound_phone.get("phone")
    if not binding_picked.get("api_url") and bound_phone.get("api_url"):
        binding_picked["api_url"] = bound_phone.get("api_url")
    if not binding_picked.get("status") and bound_phone.get("status"):
        binding_picked["status"] = bound_phone.get("status")
    return {
        "binding": binding_picked,
        "bound": bound_phone,
        "challenge": phone_challenge,
    }


def _serialize_account_compact_item(
    account: AccountModel,
    *,
    extra: dict[str, Any] | None = None,
    payment_link_generated: bool | None = None,
) -> dict[str, Any]:
    extra = extra if isinstance(extra, dict) else _safe_get_extra(account)
    sync_statuses = extra.get("sync_statuses") if isinstance(extra.get("sync_statuses"), dict) else {}
    chatgpt_local = extra.get("chatgpt_local") if isinstance(extra.get("chatgpt_local"), dict) else {}
    auth = chatgpt_local.get("auth") if isinstance(chatgpt_local.get("auth"), dict) else {}
    chatgpt_subscription = chatgpt_local.get("subscription") if isinstance(chatgpt_local.get("subscription"), dict) else {}
    codex = chatgpt_local.get("codex") if isinstance(chatgpt_local.get("codex"), dict) else {}
    chatgpt_capabilities = extra.get("chatgpt_capabilities") if isinstance(extra.get("chatgpt_capabilities"), dict) else {}
    if account.platform == "chatgpt" and (
        not chatgpt_capabilities or "has_confirmed_phone_binding" not in chatgpt_capabilities
    ):
        # Older rows may have tokens/workspace IDs but no derived capability
        # snapshot for the confirmed-phone classification gate yet.
        chatgpt_capabilities = classify_chatgpt_capabilities(account, local_probe=chatgpt_local)

    phone_binding = extra.get("chatgpt_phone_binding") if isinstance(extra.get("chatgpt_phone_binding"), dict) else {}
    bound_phone = chatgpt_bound_phone_payload(extra)
    phone_challenge = chatgpt_phone_challenge_payload(extra)
    rate_limit = account_rate_limit_payload(account, extra=extra)
    revival = account_revival_info(account, extra)
    sub2api_sync = _build_sync_summary(sync_statuses.get("sub2api") if isinstance(sync_statuses.get("sub2api"), dict) else {})
    oaipay_sync = _build_sync_summary(sync_statuses.get("oaipay") if isinstance(sync_statuses.get("oaipay"), dict) else {})
    cliproxy_sync = _build_sync_summary(sync_statuses.get("cliproxyapi") if isinstance(sync_statuses.get("cliproxyapi"), dict) else {})
    auth_summary = _build_auth_summary(account, extra, auth, chatgpt_capabilities)
    subscription_summary = _build_subscription_summary(chatgpt_subscription, chatgpt_capabilities, extra, auth)
    codex_summary = _build_codex_summary(codex, chatgpt_capabilities)
    validity_summary = _build_account_validity_summary(account, auth_summary, chatgpt_capabilities, codex_summary)
    baxigpt_cdk = _build_baxigpt_cdk_summary(extra.get("baxigpt_cdk") if isinstance(extra.get("baxigpt_cdk"), dict) else {})
    idea_submit = _build_idea_submit_summary(extra, baxigpt_cdk)
    submission_info = account_submission_info(account, extra)
    submission = _build_submission_summary(extra, baxigpt_cdk, submission_info)
    payment_link = account_payment_link_summary(account, extra)
    def eligibility_summary(marker_key: str, allowed_states: set[str]) -> dict[str, Any]:
        marker = extra.get(marker_key) if isinstance(extra.get(marker_key), dict) else {}
        confirmed_state = _safe_str(marker.get("confirmed_state")).lower()
        if confirmed_state not in allowed_states:
            confirmed_state = "unknown"
        last_attempt = marker.get("last_attempt") if isinstance(marker.get("last_attempt"), dict) else {}
        last_state = _safe_str(last_attempt.get("state")).lower()
        last_evidence = last_attempt.get("evidence") if isinstance(last_attempt.get("evidence"), dict) else {}
        last_profile = last_evidence.get("profile") if isinstance(last_evidence.get("profile"), dict) else {}
        confirmed_profile = marker.get("profile") if isinstance(marker.get("profile"), dict) else {}
        display_profile = last_profile or confirmed_profile
        return {
            "state": confirmed_state,
            "confirmed_state": confirmed_state,
            "confirmed_at": _safe_str(marker.get("confirmed_at")),
            "last_attempt_state": last_state,
            "last_attempt_at": _safe_str(last_attempt.get("checked_at") or marker.get("last_attempt_at")),
            "reason_code": _safe_str(last_attempt.get("reason_code") or marker.get("reason_code")),
            "message": sanitize_error_message(
                _safe_str(last_attempt.get("message") or marker.get("message"))
            )[:500],
            "amount_minor": last_evidence.get("amount_minor"),
            "minor_unit_exponent": last_evidence.get("minor_unit_exponent"),
            "amount_display": _safe_str(last_evidence.get("amount_display")),
            "currency": _safe_str(last_evidence.get("currency")),
            "verified_stage": _safe_str(last_evidence.get("verified_stage")),
            "profile": {
                "plan": _safe_str(display_profile.get("plan")),
                "billing_country": _safe_str(display_profile.get("billing_country")),
                "currency": _safe_str(display_profile.get("currency")),
                "checkout_ui_mode": _safe_str(display_profile.get("checkout_ui_mode")),
                "proxy_chain": {
                    key: _safe_str(value).upper()
                    for key, value in (
                        display_profile.get("proxy_chain")
                        if isinstance(display_profile.get("proxy_chain"), dict)
                        else {}
                    ).items()
                    if key in {"checkout", "promotion", "taxes"}
                },
            },
        }
    zero_amount_eligibility = eligibility_summary(
        "chatgpt_zero_amount_eligibility",
        {"eligible", "ineligible"},
    )
    gcash_payment_method = eligibility_summary(
        "chatgpt_gcash_payment_method",
        {"available", "unavailable"},
    )
    generated = account_payment_link_generated(
        account,
        extra,
        persisted_history=payment_link_generated,
    )
    payload = {
        "id": account.id,
        "platform": account.platform,
        "email": account.email,
        "status": account.status,
        "created_at": _iso_datetime(account.created_at),
        "updated_at": _iso_datetime(account.updated_at),
        "user_id": account.user_id,
        "region": account.region,
        "cashier_url": account.cashier_url,
        "payment_link": payment_link,
        "payment_link_platform": _safe_str(payment_link.get("platform")) or "none",
        "payment_link_generated": generated,
        "zero_amount_eligibility": zero_amount_eligibility,
        "gcash_payment_method": gcash_payment_method,
        "manually_used": bool(extra.get("manually_used")),
        "workspace": {
            "id": _safe_str(extra.get("workspace_id") or extra.get("organization_id") or chatgpt_capabilities.get("workspace_id")),
            "account_id": _safe_str(chatgpt_capabilities.get("account_id") or account.user_id),
        },
        "auth": auth_summary,
        "subscription": subscription_summary,
        "account_validity": _safe_str(validity_summary.get("state")),
        "account_validity_summary": validity_summary,
        "auth_type": account_auth_type(account, extra),
        "subscription_type": account_subscription_type(account, extra),
        "codex": codex_summary,
        "sub2api": sub2api_sync,
        "oaipay": oaipay_sync,
        "cliproxy": cliproxy_sync,
        "phone": _build_phone_summary(phone_binding, bound_phone, phone_challenge),
        "idea_submit": idea_submit,
        "submission": submission,
        "submit_state": submission["state"],
        "has_submitted": bool(submission["has_submitted"]),
        "rate_limit": rate_limit,
        "rate_limit_started_at": rate_limit["started_at"],
        "rate_limit_recover_at": rate_limit["recover_at"],
        "rate_limit_previous_status": rate_limit["previous_status"],
        "revival": revival,
        "has_access_token": bool(auth_summary["has_access_token"]),
        "has_refresh_token": bool(auth_summary["has_refresh_token"]),
        "has_session_token": bool(auth_summary["has_session_token"]),
        "has_cookies": bool(auth_summary["has_cookies"]),
        "has_id_token": bool(auth_summary["has_id_token"]),
        "has_password": bool(auth_summary["password_present"]),
        "password_present": bool(auth_summary["password_present"]),
        "credentials": {
            "has_access_token": bool(auth_summary["has_access_token"]),
            "has_refresh_token": bool(auth_summary["has_refresh_token"]),
            "has_session_token": bool(auth_summary["has_session_token"]),
            "has_cookies": bool(auth_summary["has_cookies"]),
            "has_id_token": bool(auth_summary["has_id_token"]),
            "has_password": bool(auth_summary["password_present"]),
        },
        "auth_level": _safe_str(auth_summary.get("level")),
        "subscription_plan": _safe_str(subscription_summary.get("plan")),
        "last_known_subscription_plan": _safe_str(subscription_summary.get("last_known_plan")),
        "subscription_refresh_state": _safe_str(subscription_summary.get("refresh_state")),
        "subscription_plan_stale": bool(subscription_summary.get("stale")),
        "subscription_active_until": _safe_str(subscription_summary.get("active_until")),
        "codex_state": _safe_str(codex_summary.get("state")),
        "cliproxy_remote_state": _safe_str(cliproxy_sync.get("remote_state")),
        "sub2api_remote_state": _safe_str(sub2api_sync.get("remote_state")),
        "oaipay_remote_state": _safe_str(oaipay_sync.get("remote_state")),
        # Backward-compatible summary aliases: keep object names that the list UI
        # already reads, but make them compact rather than returning full nested
        # probes / sync records / token-bearing extra.
        "chatgptLocal": {
            "auth": auth_summary,
            "subscription": {
                "plan": subscription_summary["plan"],
                "last_known_plan": subscription_summary["last_known_plan"],
                "refresh_state": subscription_summary["refresh_state"],
                "stale": subscription_summary["stale"],
                "workspace_plan_type": subscription_summary["workspace_plan_type"],
                "subscription_active_until": subscription_summary["active_until"],
                "checked_at": subscription_summary["checked_at"],
                "source": subscription_summary["source"],
                "refresh_attempt_count": subscription_summary["refresh_attempt_count"],
                "refresh_max_attempts": subscription_summary["refresh_max_attempts"],
                "refresh_last_error": subscription_summary["refresh_last_error"],
                "refresh_requested_at": subscription_summary["refresh_requested_at"],
                "refresh_started_at": subscription_summary["refresh_started_at"],
                "refresh_completed_at": subscription_summary["refresh_completed_at"],
                "refresh_canonical_preserved": subscription_summary["refresh_canonical_preserved"],
            },
            "codex": codex_summary,
        },
        "chatgptCapabilities": {
            key: chatgpt_capabilities.get(key)
            for key in (
                "auth_level",
                "has_access_token",
                "has_refresh_token",
                "has_account_id",
                "has_workspace",
                "account_id",
                "workspace_id",
                "subscription_plan",
                "last_known_subscription_plan",
                "subscription_refresh_state",
                "subscription_plan_stale",
                "has_paid_subscription",
                "last_known_has_paid_subscription",
                "subscription_checked",
                "has_confirmed_phone_binding",
                "phone_binding_state",
                "codex_state",
                "upload_gate",
            )
            if key in chatgpt_capabilities
        },
        "bound_phone": bound_phone,
        "bound_phone_number": _safe_str(bound_phone.get("phone") or bound_phone.get("phone_number")),
        "bound_phone_masked": _safe_str(bound_phone.get("masked") or bound_phone.get("masked_phone")),
        "phone_challenge": phone_challenge,
        "phone_binding": _build_phone_summary(phone_binding, bound_phone, phone_challenge)["binding"],
        "baxigpt_cdk": baxigpt_cdk,
        "ideaSubmit": idea_submit,
        "submitState": submission["state"],
        "hasSubmitted": bool(submission["has_submitted"]),
        "sub2apiSync": sub2api_sync,
        "oaipaySync": oaipay_sync,
        "cliproxySync": cliproxy_sync,
        "extra": {
            "manually_used": bool(extra.get("manually_used")),
            "chatgpt_phone_binding": _build_phone_summary(phone_binding, bound_phone, phone_challenge)["binding"],
            "chatgpt_bound_phone": bound_phone,
            "chatgpt_phone_challenge": phone_challenge,
            "baxigpt_cdk": baxigpt_cdk,
            "idea_submit": idea_submit,
            "submission": submission,
        },
    }
    return payload


_SECRET_FIELD_ALIASES = {
    "access_token": "access_token",
    "at": "access_token",
    "token": "access_token",
    "refresh_token": "refresh_token",
    "rt": "refresh_token",
    "password": "password",
    "session_token": "session_token",
    "session": "session_token",
    "st": "session_token",
    "nextauth_session_token": "session_token",
    "cookies": "cookies",
    "cookie": "cookies",
    "web_cookies": "cookies",
    "cookie_header": "cookie_header",
    "cookieheader": "cookie_header",
    "id_token": "id_token",
    "idtoken": "id_token",
}


def _parse_secret_fields(value: Any) -> list[str]:
    requested: list[str] = []
    seen: set[str] = set()
    for raw in str(value or "").split(","):
        normalized = _SECRET_FIELD_ALIASES.get(raw.strip().lower())
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        requested.append(normalized)
    return requested


@router.get("")
def list_accounts(
    platform: Optional[str] = None,
    filter_preset_id: Optional[str] = None,
    primary_preset_id: Optional[str] = None,
    secondary_scope: Optional[str] = None,
    fixed_group_id: Optional[str] = None,
    fixed_group_revision: Optional[int] = None,
    status: Optional[str] = None,
    email: Optional[str] = None,
    payment_link_generated: Optional[str] = None,
    manually_used: Optional[str] = None,
    auth_type: Optional[str] = None,
    phone_binding_state: Optional[str] = None,
    payment_link_platform: Optional[str] = None,
    subscription_type: Optional[str] = None,
    account_validity: Optional[str] = None,
    sub2api_state: Optional[str] = None,
    oaipay_state: Optional[str] = None,
    zero_amount_eligibility_state: Optional[str] = None,
    gcash_payment_method_state: Optional[str] = None,
    idea_submit_state: Optional[str] = None,
    submit_state: Optional[str] = None,
    has_submitted: Optional[str] = None,
    revival_state: Optional[str] = None,
    sort_by: Optional[str] = None,
    sort_order: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
    detail: bool = False,
    session: Session = Depends(get_session),
):
    _maybe_reconcile_rate_limited_accounts(session, platform=platform)
    page_value = max(1, int(page or 1))
    page_size_value = max(1, min(int(page_size or 20), 200))
    fixed_preset_scope: dict[str, Any] | None = None
    fixed_account_ids: list[int] = []
    normalized_primary_preset_id = _trim_text(primary_preset_id, max_length=80)
    normalized_secondary_scope = _trim_text(secondary_scope, max_length=24).lower().replace("-", "_")
    if normalized_secondary_scope in {"unfixed", "available"}:
        normalized_secondary_scope = "unassigned"
    elif normalized_secondary_scope in {"group", "fixed_group"}:
        normalized_secondary_scope = "fixed"
    elif normalized_secondary_scope not in {"unassigned", "fixed"}:
        normalized_secondary_scope = ""
    normalized_fixed_group_id = _trim_text(fixed_group_id, max_length=80)
    normalized_filter_preset_id = _trim_text(filter_preset_id, max_length=80)
    if normalized_filter_preset_id:
        compatibility_group = get_fixed_group(session, normalized_filter_preset_id)
        if compatibility_group is not None:
            normalized_primary_preset_id = compatibility_group.parent_preset_id
            normalized_secondary_scope = "fixed"
            normalized_fixed_group_id = compatibility_group.id
        else:
            preset = _find_visible_filter_preset(normalized_filter_preset_id)
            if not preset:
                raise HTTPException(404, "筛选组合不存在")
            if preset.get("mode") != ACCOUNT_FILTER_PRESET_MODE_FIXED:
                raise HTTPException(400, "该筛选组合不是固定账号模式")
            fixed_account_ids = _normalize_filter_preset_account_ids(preset.get("account_ids"))
            if not fixed_account_ids:
                raise HTTPException(409, "固定账号组合没有可用成员")
            account_refs = _normalize_filter_preset_account_refs(
                preset.get("account_refs"),
                fixed_account_ids,
            )
            refs_by_id = {int(item["id"]): item for item in account_refs}

            existing_rows = session.exec(
                select(AccountModel).where(
                    AccountModel.platform == "chatgpt",
                    AccountModel.id.in_(fixed_account_ids),
                )
            ).all()
            existing_ids = {
                int(account.id)
                for account in existing_rows
                if _safe_int(account.id) > 0
                and _filter_preset_account_matches_ref(
                    account,
                    refs_by_id.get(int(account.id)),
                )
            }
            resolved_ids = [account_id for account_id in fixed_account_ids if account_id in existing_ids]
            missing_ids = [account_id for account_id in fixed_account_ids if account_id not in existing_ids]
            fixed_preset_scope = {
                "id": normalized_filter_preset_id,
                "stored_account_count": len(fixed_account_ids),
                "resolved_account_ids": resolved_ids,
                "missing_account_ids": missing_ids,
                "legacy": True,
            }
            fixed_account_ids = resolved_ids

    if normalized_secondary_scope:
        parent = _find_dynamic_filter_preset(normalized_primary_preset_id)
        if not parent:
            raise HTTPException(404, "一级条件筛选组合不存在")
    if normalized_secondary_scope == "fixed":
        group = get_fixed_group(session, normalized_fixed_group_id)
        if group is None:
            raise HTTPException(404, "固定账号组合不存在")
        if group.parent_preset_id != normalized_primary_preset_id:
            raise HTTPException(409, "固定账号组合不属于当前一级条件组合")
        if fixed_group_revision is not None and int(fixed_group_revision) != int(group.revision or 1):
            raise HTTPException(409, "固定账号组合成员已变化，请刷新后重试")
        resolved_ids = fixed_group_member_ids(session, group.id)
        fixed_preset_scope = {
            "id": group.id,
            "parent_preset_id": group.parent_preset_id,
            "revision": int(group.revision or 1),
            "stored_account_count": len(resolved_ids),
            "resolved_account_ids": resolved_ids,
            "missing_account_ids": [],
            "legacy": False,
        }
    q, use_list_state, _ = account_filtered_query(
        session,
        platform=platform,
        filter_source={
            "email": email,
            "payment_link_generated": payment_link_generated,
            "status": status,
            "manually_used": manually_used,
            "auth_type": auth_type,
            "phone_binding_state": phone_binding_state,
            "payment_link_platform": payment_link_platform,
            "subscription_type": subscription_type,
            "account_validity": account_validity,
            "sub2api_state": sub2api_state,
            "oaipay_state": oaipay_state,
            "zero_amount_eligibility_state": zero_amount_eligibility_state,
            "gcash_payment_method_state": gcash_payment_method_state,
            "idea_submit_state": idea_submit_state,
            "submit_state": submit_state,
            "has_submitted": has_submitted,
            "revival_state": revival_state,
            "sort_by": sort_by,
            "sort_order": sort_order,
            "primary_preset_id": normalized_primary_preset_id,
            "secondary_scope": normalized_secondary_scope,
            "fixed_group_id": normalized_fixed_group_id,
        },
    )
    if fixed_preset_scope is not None and fixed_preset_scope.get("legacy"):
        q = q.where(AccountModel.id.in_(fixed_account_ids))
    count_q = select(func.count()).select_from(q.subquery())
    total = int(session.exec(count_q).one())
    q = apply_account_list_state_sort(q, sort_by=sort_by, sort_order=sort_order)
    items = session.exec(q.offset((page_value - 1) * page_size_value).limit(page_size_value)).all()
    generated_by_id: dict[int, bool] = {}
    item_ids = [int(item.id or 0) for item in items if int(item.id or 0) > 0]
    page_state_refresh_pending = False
    if item_ids:
        if not use_list_state:
            # An unfiltered page does not otherwise touch the derived cache.
            # Refresh just this page in one SQL batch so historical successful
            # generations cannot be rendered as "never extracted".
            try:
                refreshed_count = refresh_account_list_state(
                    session,
                    account_ids=item_ids,
                    stale_only=True,
                    cleanup_orphans=False,
                    commit=False,
                )
                page_state_refresh_pending = refreshed_count > 0
            except Exception:
                # A standalone database may be serving the API before the
                # next init_db migration creates payment_link_generations.
                # Keep the list usable and let the serializer use its local
                # URL/tombstone fallback rather than failing the whole page.
                session.rollback()
        try:
            state_rows = session.exec(
                select(AccountListStateModel).where(AccountListStateModel.account_id.in_(item_ids))
            ).all()
            generated_by_id = {
                int(state.account_id): bool(state.payment_link_generated)
                for state in state_rows
            }
        except Exception:
            # Older standalone/test databases may not have the cache table yet;
            # the serializer falls back to current URL/tombstone evidence.
            generated_by_id = {}
    extras_by_id = {
        int(item.id or 0): _safe_extra(item)
        for item in items
        if int(item.id or 0) > 0
    }
    response = {
        "total": total,
        "page": page_value,
        "items": [
            (
                _serialize_account(
                    item,
                    payment_link_generated=generated_by_id.get(int(item.id or 0)),
                )
                if detail
                else _serialize_account_compact_item(
                    item,
                    extra=extras_by_id.get(int(item.id or 0)),
                    payment_link_generated=generated_by_id.get(int(item.id or 0)),
                )
            )
            for item in items
        ],
    }
    if fixed_preset_scope is not None:
        response["fixed_preset"] = fixed_preset_scope
    if page_state_refresh_pending:
        session.commit()
    return response


@router.post("")
def create_account(body: AccountCreate, session: Session = Depends(get_session)):
    acc = AccountModel(
        platform=body.platform,
        email=body.email,
        password=body.password,
        status=body.status,
        token=body.token,
        cashier_url=body.cashier_url,
    )
    if _safe_str(body.status).lower() == RATE_LIMITED_STATUS:
        mark_account_rate_limited(acc)
    session.add(acc)
    session.flush()
    upsert_account_list_state_for_account_ids(session, [acc.id], commit=False)
    session.commit()
    session.refresh(acc)
    if acc.platform == "chatgpt":
        schedule_chatgpt_local_status_refresh_for_account_id(acc.id, reason="account_create")
    return acc


@router.get("/stats")
def get_stats(session: Session = Depends(get_session)):
    """统计各平台账号数量和状态分布"""
    reconcile_rate_limited_accounts(session)
    platforms: dict = {}
    statuses: dict = {}
    rows = session.exec(
        select(
            AccountModel.platform,
            AccountModel.status,
            func.count().label("count"),
        ).group_by(AccountModel.platform, AccountModel.status)
    ).all()
    total = 0
    for platform_value, status_value, count_value in rows:
        count = int(count_value or 0)
        platform_key = _safe_str(platform_value)
        status_key = _safe_str(status_value)
        total += count
        platforms[platform_key] = platforms.get(platform_key, 0) + count
        statuses[status_key] = statuses.get(status_key, 0) + count
    return {"total": total, "by_platform": platforms, "by_status": statuses}


@router.get("/overview")
def get_accounts_overview(
    platform: Optional[str] = None,
    status: Optional[str] = None,
    email: Optional[str] = None,
    session: Session = Depends(get_session),
):
    reconcile_rate_limited_accounts(session, platform=platform)
    q = select(AccountModel)
    if platform:
        q = q.where(AccountModel.platform == platform)
    if status:
        q = q.where(AccountModel.status == status)
    if email:
        q = q.where(AccountModel.email.contains(email))

    accounts = session.exec(q).all()
    by_status: dict[str, int] = {}
    by_platform: dict[str, int] = {}
    manually_used = 0
    for acc in accounts:
        by_status[acc.status] = by_status.get(acc.status, 0) + 1
        by_platform[acc.platform] = by_platform.get(acc.platform, 0) + 1
        extra = acc.get_extra()
        if bool(extra.get("manually_used")):
            manually_used += 1
    return {
        "total": len(accounts),
        "by_status": by_status,
        "by_platform": by_platform,
        "manually_used": manually_used,
    }


@router.get("/export")
def export_accounts(
    platform: Optional[str] = None,
    status: Optional[str] = None,
    session: Session = Depends(get_session),
):
    reconcile_rate_limited_accounts(session, platform=platform)
    q = select(AccountModel)
    if platform:
        q = q.where(AccountModel.platform == platform)
    if status:
        q = q.where(AccountModel.status == status)
    accounts = session.exec(q).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["platform", "email", "password", "user_id", "region",
                     "status", "cashier_url", "created_at"])
    for acc in accounts:
        writer.writerow([acc.platform, acc.email, acc.password, acc.user_id,
                         acc.region, acc.status, acc.cashier_url,
                         beijing_iso(acc.created_at)])
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=accounts.csv"}
    )


@router.post("/import")
def import_accounts(
    body: ImportRequest,
    session: Session = Depends(get_session),
):
    """批量导入，每行格式: email password [extra]"""
    created = 0
    created_accounts: list[AccountModel] = []
    created_chatgpt_accounts: list[AccountModel] = []
    for line in body.lines:
        parts = line.strip().split()
        if len(parts) < 2:
            continue
        email, password = parts[0], parts[1]
        extra = parts[2] if len(parts) > 2 else ""
        if extra:
            try:
                json.loads(extra)
            except (json.JSONDecodeError, ValueError):
                extra = "{}"
        else:
            extra = "{}"
        acc = AccountModel(platform=body.platform, email=email,
                           password=password, extra_json=extra)
        session.add(acc)
        created_accounts.append(acc)
        if str(body.platform or "").strip().lower() == "chatgpt":
            created_chatgpt_accounts.append(acc)
        created += 1
    if created_accounts:
        session.flush()
        upsert_account_list_state_for_account_ids(
            session,
            [acc.id for acc in created_accounts],
            commit=False,
        )
    session.commit()
    if created_chatgpt_accounts:
        for acc in created_chatgpt_accounts:
            schedule_chatgpt_local_status_refresh_for_account_id(acc.id, reason="account_import")
    return {"created": created}


@router.post("/batch-delete")
def batch_delete_accounts(
    body: BatchDeleteRequest,
    session: Session = Depends(get_session)
):
    """批量删除账号"""
    requested_ids = list(dict.fromkeys(int(account_id) for account_id in (body.ids or [])))
    if not requested_ids:
        raise HTTPException(400, "账号 ID 列表不能为空")
    
    if len(requested_ids) > 1000:
        raise HTTPException(400, "单次最多删除 1000 个账号")
    
    deleted_count = 0
    deleted_ids: list[int] = []
    not_found_ids = []
    
    try:
        for account_id in requested_ids:
            acc = session.get(AccountModel, account_id)
            if acc:
                deleted_ids.append(int(account_id))
                session.delete(acc)
                deleted_count += 1
            else:
                not_found_ids.append(account_id)

        delete_account_list_state_for_account_ids(session, deleted_ids, commit=False)
        session.commit()
        logger.info(f"批量删除成功: {deleted_count} 个账号")
        
        return {
            "deleted": deleted_count,
            "not_found": not_found_ids,
            "total_requested": len(requested_ids)
        }
    except Exception as e:
        session.rollback()
        logger.exception("批量删除失败")
        raise HTTPException(500, f"批量删除失败: {str(e)}")


@router.post("/snapshot")
def snapshot_accounts(body: AccountSnapshotRequest, session: Session = Depends(get_session)):
    ids: list[int] = []
    seen: set[int] = set()
    for value in body.ids or []:
        account_id = _safe_int(value)
        if account_id <= 0 or account_id in seen:
            continue
        seen.add(account_id)
        ids.append(account_id)
    if len(ids) > 500:
        raise HTTPException(400, "单次最多读取 500 个账号快照")
    if not ids:
        return {"items": [], "total": 0}

    rows = session.exec(
        select(AccountModel)
        .where(AccountModel.id.in_(ids))
        .where(AccountModel.platform == "chatgpt")
    ).all()
    row_map = {int(row.id or 0): row for row in rows if int(row.id or 0) > 0}
    items: list[dict[str, Any]] = []
    for account_id in ids:
        account = row_map.get(account_id)
        if account is None:
            continue
        extra = account.get_extra()
        baxigpt_cdk = extra.get("baxigpt_cdk") if isinstance(extra.get("baxigpt_cdk"), dict) else {}
        items.append(
            {
                "id": int(account.id or 0),
                "status": str(account.status or ""),
                "updated_at": account.updated_at.isoformat() if account.updated_at else None,
                "baxigpt_cdk": baxigpt_cdk,
                "extra": {"baxigpt_cdk": baxigpt_cdk},
            }
        )
    return {"items": items, "total": len(items)}


@router.post("/check-all")
def check_all_accounts(platform: Optional[str] = None,
                       background_tasks: BackgroundTasks = None):
    from core.scheduler import scheduler
    background_tasks.add_task(scheduler.check_accounts_valid, platform)
    return {"message": "批量检测任务已启动"}


@router.post("/batch-delete-by-filter")
def batch_delete_accounts_by_filter(
    body: BatchDeleteByFilterRequest,
    session: Session = Depends(get_session),
):
    """按筛选条件批量删除账号。至少需要一个筛选条件。"""
    if not any([body.platform, body.status, body.email]):
        raise HTTPException(400, "至少需要一个筛选条件")

    reconcile_rate_limited_accounts(session, platform=body.platform)
    q, _, _ = account_filtered_query(
        session,
        platform=body.platform,
        filter_source={
            "status": body.status,
            "email": body.email,
            # Destructive condition scopes never absorb fixed-group members.
            # Operators can still delete those accounts through explicit IDs.
            "secondary_scope": "unassigned" if body.platform == "chatgpt" else "",
        },
    )

    accounts = session.exec(q).all()
    deleted_count = 0
    deleted_ids: list[int] = []

    try:
        for acc in accounts:
            if acc.id is None:
                continue
            deleted_ids.append(acc.id)
            session.delete(acc)
            deleted_count += 1

        delete_account_list_state_for_account_ids(session, deleted_ids, commit=False)
        session.commit()
        logger.info("按筛选条件批量删除成功: %s 个账号", deleted_count)
        filters = body.model_dump() if hasattr(body, "model_dump") else body.dict()
        return {
            "deleted": deleted_count,
            "deleted_ids": deleted_ids,
            "filters": filters,
        }
    except Exception as e:
        session.rollback()
        logger.exception("按筛选条件批量删除失败")
        raise HTTPException(500, f"按筛选条件批量删除失败: {str(e)}")


@router.get("/{account_id}")
def get_account(account_id: int, session: Session = Depends(get_session)):
    acc = session.get(AccountModel, account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    reconcile_rate_limited_accounts(session, accounts=[acc])
    session.refresh(acc)
    detail_state_refresh_pending = False
    try:
        refreshed_count = refresh_account_list_state(
            session,
            account_ids=[account_id],
            stale_only=True,
            cleanup_orphans=False,
            commit=False,
        )
        detail_state_refresh_pending = refreshed_count > 0
    except Exception:
        session.rollback()
    try:
        state = session.get(AccountListStateModel, account_id)
    except Exception:
        # Keep the detail endpoint usable for databases upgrading from a
        # pre-list-state schema; compact serialization has a local fallback.
        session.rollback()
        state = None
    payload = _serialize_account(
        acc,
        payment_link_generated=(bool(state.payment_link_generated) if state is not None else None),
    )
    if detail_state_refresh_pending:
        session.commit()
    return payload


@router.get("/{account_id}/secrets")
def get_account_secrets(
    account_id: int,
    fields: str = "",
    session: Session = Depends(get_session),
):
    requested = _parse_secret_fields(fields)
    if not requested:
        raise HTTPException(400, "fields 至少需要一个有效字段")
    acc = session.get(AccountModel, account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")

    extra = _safe_get_extra(acc)
    values: dict[str, str] = {}
    for field in requested:
        if field == "access_token":
            values[field] = _first_secret(acc, extra, "access_token")
        elif field == "refresh_token":
            values[field] = _first_secret(acc, extra, "refresh_token")
        elif field == "session_token":
            values[field] = _first_secret(acc, extra, "session_token")
        elif field == "cookies":
            values[field] = _first_secret(acc, extra, "cookies")
        elif field == "cookie_header":
            values[field] = _first_secret(acc, extra, "cookie_header")
        elif field == "id_token":
            values[field] = _first_secret(acc, extra, "id_token")
        elif field == "password":
            values[field] = _first_secret(acc, extra, "password")

    return {
        "account_id": int(acc.id or 0),
        "fields": requested,
        "secrets": values,
        "present": {field: bool(values.get(field)) for field in requested},
        "lengths": {field: len(values.get(field) or "") for field in requested},
    }


@router.patch("/{account_id}")
def update_account(account_id: int, body: AccountUpdate,
                   session: Session = Depends(get_session)):
    acc = session.get(AccountModel, account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    if body.status is not None:
        next_status = _safe_str(body.status).lower()
        if next_status == RATE_LIMITED_STATUS:
            mark_account_rate_limited(acc, previous_status=acc.status)
        else:
            acc.status = body.status
            clear_account_rate_limit(acc)
    if body.token is not None:
        next_token = str(body.token or "").strip()
        acc.token = next_token
        if str(acc.platform or "").strip().lower() == "chatgpt":
            extra = acc.get_extra()
            if next_token:
                extra["access_token"] = next_token
            else:
                extra.pop("access_token", None)
            acc.set_extra(extra)
            prepare_chatgpt_account_for_local_status_refresh(
                acc,
                reason="account_update_token",
            )
    if body.cashier_url is not None:
        acc.cashier_url = body.cashier_url
    acc.updated_at = datetime.now(timezone.utc)
    session.add(acc)
    upsert_account_list_state_for_account_ids(session, [acc.id], commit=False)
    session.commit()
    session.refresh(acc)
    if acc.platform == "chatgpt" and body.token is not None:
        schedule_chatgpt_local_status_refresh_for_account_id(acc.id, reason="account_update_token")
    return acc


@router.post("/{account_id}/mark-used")
def mark_account_used(account_id: int, body: AccountMarkUsedRequest,
                      session: Session = Depends(get_session)):
    acc = session.get(AccountModel, account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    try:
        extra = json.loads(acc.extra_json or "{}")
    except Exception:
        extra = {}
    if not isinstance(extra, dict):
        extra = {}
    extra["manually_used"] = bool(body.used)
    acc.extra_json = json.dumps(extra, ensure_ascii=False)
    acc.updated_at = datetime.now(timezone.utc)
    session.add(acc)
    upsert_account_list_state_for_account_ids(session, [acc.id], commit=False)
    session.commit()
    session.refresh(acc)
    return _serialize_account(acc)


@router.delete("/{account_id}")
def delete_account(account_id: int, session: Session = Depends(get_session)):
    acc = session.get(AccountModel, account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    session.delete(acc)
    delete_account_list_state_for_account_ids(session, [account_id], commit=False)
    session.commit()
    return {"ok": True}


@router.post("/{account_id}/check")
def check_account(account_id: int, background_tasks: BackgroundTasks,
                  session: Session = Depends(get_session)):
    acc = session.get(AccountModel, account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    background_tasks.add_task(_do_check, account_id)
    return {"message": "检测任务已启动"}


def _do_check(account_id: int):
    from core.db import engine
    from sqlmodel import Session
    from services.chatgpt_core import ChatGPTPlatform
    with Session(engine) as s:
        acc = s.get(AccountModel, account_id)
    if acc:
        from core.base_platform import Account, RegisterConfig
        try:
            if acc.platform != "chatgpt":
                return
            plugin = ChatGPTPlatform(config=RegisterConfig())
            obj = Account(platform=acc.platform, email=acc.email,
                         password=acc.password, user_id=acc.user_id,
                         region=acc.region, token=acc.token,
                         extra=json.loads(acc.extra_json or "{}"))
            valid = plugin.check_valid(obj)
            with Session(engine) as s:
                a = s.get(AccountModel, account_id)
                if a:
                    if a.platform != "chatgpt":
                        a.status = a.status if valid else "invalid"
                    a.updated_at = datetime.now(timezone.utc)
                    s.add(a)
                    upsert_account_list_state_for_account_ids(s, [a.id], commit=False)
                    s.commit()
        except Exception:
            logger.exception("检测账号 %s 时出错", account_id)
