from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from fastapi.responses import StreamingResponse
from sqlmodel import Session, select, func
from pydantic import BaseModel, Field
from core.config_store import config_store
from core.db import AccountModel, get_session
from services.account_filters import (
    account_auth_type,
    account_filtered_query,
    account_revival_info,
    account_subscription_type,
    apply_account_list_state_sort,
    delete_account_list_state_for_account_ids,
    upsert_account_list_state_for_account_ids,
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
from services.chatgpt_core.local_status_refresh import schedule_chatgpt_local_status_refresh_for_account_id
from typing import Any, Optional
from datetime import datetime, timezone
import io, csv, json, logging, threading, time, uuid

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
ACCOUNT_FILTER_PRESET_MAX_CUSTOM_ITEMS = 80
ACCOUNT_FILTER_PRESET_MAX_LIST_VALUES = 32
ACCOUNT_FILTER_PRESET_PAGE_SIZES = {10, 20, 50}
ACCOUNT_FILTER_PRESET_COLUMN_KEYS = (
    "email",
    "status",
    "manuallyUsed",
    "authType",
    "subscriptionType",
    "accountValidity",
    "codexState",
    "sub2apiState",
    "oaipayState",
    "ideaSubmitState",
)
ACCOUNT_FILTER_PRESET_PENDING_OAIPAY_STATES = [
    "unknown",
    "not_found",
    "deleted_exact_match",
    "cross_workspace_only",
]


class AccountFilterPresetBody(BaseModel):
    name: str
    description: str = ""
    filters: dict[str, Any] = Field(default_factory=dict)
    pinned: bool = False


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


def _empty_filter_preset_payload() -> dict[str, Any]:
    return {
        "search": "",
        "status": [],
        "columnFilters": {key: [] for key in ACCOUNT_FILTER_PRESET_COLUMN_KEYS},
        "sortOrder": "",
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
        clean["columnFilters"][key] = _normalize_idea_submit_filter_values(values) if key == "ideaSubmitState" else values

    sort_source = source.get("sort") if isinstance(source.get("sort"), dict) else {}
    sort_order = _trim_text(
        source.get("sortOrder")
        or source.get("subscriptionExpirySortOrder")
        or sort_source.get("sortOrder")
        or sort_source.get("order"),
        max_length=8,
    ).lower()
    clean["sortOrder"] = sort_order if sort_order in {"asc", "desc"} else ""

    try:
        page_size = int(source.get("pageSize") or source.get("page_size") or 20)
    except Exception:
        page_size = 20
    clean["pageSize"] = page_size if page_size in ACCOUNT_FILTER_PRESET_PAGE_SIZES else 20
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
        ("accountValidity", "有效性"),
        ("manuallyUsed", "使用"),
        ("sub2apiState", "Sub2API"),
        ("oaipayState", "OAIPay"),
        ("ideaSubmitState", "Idea提交"),
    ]
    for key, label in summary_keys:
        values = _filter_value_list(column_filters.get(key))
        if values:
            parts.append(f"{label}={','.join(values[:4])}{'…' if len(values) > 4 else ''}")
    if filters.get("sortOrder"):
        parts.append("到期排序=" + ("最早" if filters.get("sortOrder") == "asc" else "最晚"))
    return " · ".join(parts) or "无筛选条件"


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
        "filters": filters,
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
        description="OAIPay 未同步、未发现、已删可重传或其他工作区已存在。",
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
        preset_id="builtin_oaipay_attention",
        name="OAIPay 异常待处理",
        description="OAIPay 多候选或远端不可达，需要人工复查。",
        column_filters={"oaipayState": ["ambiguous", "unreachable"]},
    ),
    _make_builtin_filter_preset(
        preset_id="builtin_sub2api_exists_oaipay_pending",
        name="Sub2API 已有但 OAIPay 未传",
        description="Sub2API 已存在，但 OAIPay 仍处于待补传状态。",
        column_filters={
            "sub2apiState": ["exists"],
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
    filters = _normalize_filter_preset_filters(item.get("filters"))
    created_at = _trim_text(item.get("created_at"), max_length=40) or _utc_iso()
    updated_at = _trim_text(item.get("updated_at"), max_length=40) or created_at
    return {
        "id": preset_id,
        "name": name,
        "description": _trim_text(item.get("description"), max_length=240),
        "filters": filters,
        "summary": _filter_preset_summary(filters),
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
    filters_source = item.get("filters") if isinstance(item.get("filters"), dict) else default.get("filters")
    filters = _normalize_filter_preset_filters(filters_source)
    created_at = _trim_text(item.get("created_at"), max_length=40) or _trim_text(default.get("created_at"), max_length=40) or _utc_iso()
    updated_at = _trim_text(item.get("updated_at"), max_length=40) or _utc_iso()
    return {
        "id": preset_id,
        "name": name,
        "description": _trim_text(
            item.get("description") if "description" in item else default.get("description"),
            max_length=240,
        ),
        "filters": filters,
        "summary": _filter_preset_summary(filters),
        "pinned": _source_bool(item, "pinned", bool(default.get("pinned"))),
        "built_in": True,
        "created_at": created_at,
        "updated_at": updated_at,
    }


def _empty_filter_preset_state() -> dict[str, Any]:
    return {
        "custom": [],
        "builtin_overrides": {},
        "deleted_builtin_ids": set(),
    }


def _normalize_filter_preset_state(payload: Any) -> dict[str, Any]:
    state = _empty_filter_preset_state()

    if isinstance(payload, list):
        custom_raw = payload
        builtin_override_raw: Any = {}
        deleted_raw: Any = []
    elif isinstance(payload, dict):
        custom_raw = payload.get("custom")
        if not isinstance(custom_raw, list):
            custom_raw = payload.get("items") if isinstance(payload.get("items"), list) else []
        builtin_override_raw = payload.get("builtin_overrides")
        deleted_raw = payload.get("deleted_builtin_ids")
    else:
        custom_raw = []
        builtin_override_raw = {}
        deleted_raw = []

    seen_custom_ids: set[str] = set()
    for raw_item in custom_raw:
        item = _normalize_custom_filter_preset(raw_item)
        if not item:
            continue
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
        "custom": safe_custom,
        "builtin_overrides": safe_overrides,
        "deleted_builtin_ids": deleted_builtin_ids,
    }
    payload = {
        "version": 2,
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


def _build_filter_presets_response(state: dict[str, Any] | None = None) -> dict[str, Any]:
    state = state if state is not None else _load_filter_preset_state()
    custom = list(state.get("custom") or [])
    builtin_items = _visible_builtin_filter_presets(state)
    ordered_custom = sorted(
        custom,
        key=lambda item: (not bool(item.get("pinned")), str(item.get("updated_at") or "")),
    )
    return {
        "ok": True,
        "items": [*builtin_items, *ordered_custom],
        "built_in_count": len(builtin_items),
        "custom_count": len(custom),
        "deleted_builtin_count": len(set(state.get("deleted_builtin_ids") or set())),
        "builtin_override_count": len(dict(state.get("builtin_overrides") or {})),
    }


@router.get("/filter-presets")
def list_account_filter_presets():
    return _build_filter_presets_response()


@router.post("/filter-presets")
def create_account_filter_preset(body: AccountFilterPresetBody):
    name = _trim_text(body.name, max_length=80)
    if not name:
        raise HTTPException(400, "筛选组合名称不能为空")
    state = _load_filter_preset_state()
    if _duplicate_filter_preset_name(state, name):
        raise HTTPException(400, "已存在同名筛选组合")
    now = _utc_iso()
    item = {
        "id": "preset_" + uuid.uuid4().hex[:12],
        "name": name,
        "description": _trim_text(body.description, max_length=240),
        "filters": _normalize_filter_preset_filters(body.filters),
        "pinned": bool(body.pinned),
        "built_in": False,
        "created_at": now,
        "updated_at": now,
    }
    item["summary"] = _filter_preset_summary(item["filters"])
    state["custom"].append(item)
    state = _save_filter_preset_state(state)
    return {"ok": True, "item": item, **_build_filter_presets_response(state)}


@router.put("/filter-presets/{preset_id}")
def update_account_filter_preset(preset_id: str, body: AccountFilterPresetBody):
    preset_id = _trim_text(preset_id, max_length=80)
    name = _trim_text(body.name, max_length=80)
    if not name:
        raise HTTPException(400, "筛选组合名称不能为空")
    state = _load_filter_preset_state()
    if preset_id in BUILTIN_ACCOUNT_FILTER_PRESETS_BY_ID and preset_id in set(state.get("deleted_builtin_ids") or set()):
        raise HTTPException(404, "筛选组合不存在")
    is_builtin = preset_id in BUILTIN_ACCOUNT_FILTER_PRESETS_BY_ID
    if not is_builtin and not preset_id:
        raise HTTPException(404, "筛选组合不存在")
    if _duplicate_filter_preset_name(state, name, ignore_id=preset_id):
        raise HTTPException(400, "已存在同名筛选组合")

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
            "filters": _normalize_filter_preset_filters(body.filters),
            "pinned": bool(body.pinned),
            "built_in": True,
            "updated_at": _utc_iso(),
        }
        updated["summary"] = _filter_preset_summary(updated["filters"])
        state.setdefault("builtin_overrides", {})[preset_id] = updated
        state["deleted_builtin_ids"] = set(state.get("deleted_builtin_ids") or set()) - {preset_id}
        state = _save_filter_preset_state(state)
        return {"ok": True, "item": updated, **_build_filter_presets_response(state)}

    items = list(state.get("custom") or [])
    index = next((idx for idx, item in enumerate(items) if str(item.get("id") or "") == preset_id), -1)
    if index < 0:
        raise HTTPException(404, "筛选组合不存在")
    current = dict(items[index])
    updated = {
        **current,
        "name": name,
        "description": _trim_text(body.description, max_length=240),
        "filters": _normalize_filter_preset_filters(body.filters),
        "pinned": bool(body.pinned),
        "built_in": False,
        "updated_at": _utc_iso(),
    }
    updated["summary"] = _filter_preset_summary(updated["filters"])
    items[index] = updated
    state["custom"] = items
    state = _save_filter_preset_state(state)
    return {"ok": True, "item": updated, **_build_filter_presets_response(state)}


@router.delete("/filter-presets/{preset_id}")
def delete_account_filter_preset(preset_id: str):
    preset_id = _trim_text(preset_id, max_length=80)
    state = _load_filter_preset_state()
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
        return _build_filter_presets_response(state)

    items = list(state.get("custom") or [])
    next_items = [item for item in items if str(item.get("id") or "") != preset_id]
    if len(next_items) == len(items):
        raise HTTPException(404, "筛选组合不存在")
    state["custom"] = next_items
    state = _save_filter_preset_state(state)
    return _build_filter_presets_response(state)


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


def _serialize_account(account: AccountModel) -> dict[str, Any]:
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
) -> dict[str, Any]:
    """Backward-compatible name for the compact list serializer."""

    return _serialize_account_compact_item(
        account,
        extra=extra,
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
        return value.isoformat()
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
        or extra.get("idea_submit_unavailable_reason")
        or (extra.get("chatgpt_unavailable_reason") if unavailable else "")
        or (baxigpt_cdk.get("last_error_message") if unavailable else "")
    )
    status = "unavailable" if unavailable else "available"
    cdk_status = _safe_str(baxigpt_cdk.get("status")).lower()
    if not unavailable and cdk_status in {"paid", "submitted", "processing", "failed"}:
        status = cdk_status
    return {
        "status": status,
        "available": not unavailable,
        "unavailable": unavailable,
        "reason": reason,
        "marked_at": _safe_str(marker.get("marked_at") or extra.get("idea_submit_unavailable_at")),
        "cleared_at": _safe_str(marker.get("cleared_at")),
        "source": _safe_str(marker.get("source") or ("baxigpt_cdk_submit" if marker else "")),
        "cdk_id": _safe_int(marker.get("cdk_id") or baxigpt_cdk.get("cdk_id")),
        "code_masked": _safe_str(marker.get("code_masked") or baxigpt_cdk.get("code_masked")),
        "task_id": _safe_str(marker.get("task_id") or baxigpt_cdk.get("task_id")),
        "order_id": _safe_str(marker.get("order_id") or baxigpt_cdk.get("order_id")),
        "display_id": _safe_str(marker.get("display_id") or baxigpt_cdk.get("display_id")),
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
    for candidate in (
        capabilities.get("last_known_subscription_plan"),
        subscription.get("last_known_plan"),
        extra.get("last_known_subscription_plan"),
        capabilities.get("subscription_plan"),
        extra.get("chatgpt_plan_type"),
        extra.get("chatgpt_subscription_plan"),
    ):
        resolved = normalize_subscription_plan(candidate)
        if resolved != "unknown":
            return resolved
    return ""


def _subscription_refresh_state(
    subscription: dict[str, Any],
    capabilities: dict[str, Any],
    auth: dict[str, Any],
    current_plan: str,
    last_known_plan: str,
) -> str:
    explicit = _safe_str(capabilities.get("subscription_refresh_state")).lower()
    if explicit:
        return explicit
    auth_level = _safe_str(capabilities.get("auth_level")).lower()
    upload_gate = _safe_str(capabilities.get("upload_gate")).lower()
    auth_state = _safe_str(auth.get("state")).lower()
    if auth_level == "invalid" or upload_gate == "blocked_auth_invalid" or auth_state in AUTH_INVALID_STATES:
        return "auth_invalid"
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
    current_plan = _current_subscription_plan(subscription, capabilities)
    last_known_plan = _last_known_subscription_plan(subscription, capabilities, extra, current_plan)
    refresh_state = _subscription_refresh_state(subscription, capabilities, auth, current_plan, last_known_plan)
    stale = current_plan == "unknown" and bool(last_known_plan)
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
        "checked_at": _safe_str(subscription.get("checked_at")),
        "source": _safe_str(subscription.get("source")),
        "has_paid_subscription": current_plan in {"plus", "pro", "team", "enterprise"},
        "last_known_has_paid_subscription": last_known_plan in {"plus", "pro", "team", "enterprise"},
        "subscription_checked": current_plan != "unknown",
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
) -> dict[str, Any]:
    extra = extra if isinstance(extra, dict) else _safe_get_extra(account)
    sync_statuses = extra.get("sync_statuses") if isinstance(extra.get("sync_statuses"), dict) else {}
    chatgpt_local = extra.get("chatgpt_local") if isinstance(extra.get("chatgpt_local"), dict) else {}
    auth = chatgpt_local.get("auth") if isinstance(chatgpt_local.get("auth"), dict) else {}
    chatgpt_subscription = chatgpt_local.get("subscription") if isinstance(chatgpt_local.get("subscription"), dict) else {}
    codex = chatgpt_local.get("codex") if isinstance(chatgpt_local.get("codex"), dict) else {}
    chatgpt_capabilities = extra.get("chatgpt_capabilities") if isinstance(extra.get("chatgpt_capabilities"), dict) else {}
    if account.platform == "chatgpt" and not chatgpt_capabilities:
        # Older rows may have tokens/workspace IDs but no derived capability snapshot yet.
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
    status: Optional[str] = None,
    email: Optional[str] = None,
    manually_used: Optional[str] = None,
    auth_type: Optional[str] = None,
    subscription_type: Optional[str] = None,
    account_validity: Optional[str] = None,
    sub2api_state: Optional[str] = None,
    oaipay_state: Optional[str] = None,
    idea_submit_state: Optional[str] = None,
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
    q, use_list_state, _ = account_filtered_query(
        session,
        platform=platform,
        filter_source={
            "email": email,
            "status": status,
            "manually_used": manually_used,
            "auth_type": auth_type,
            "subscription_type": subscription_type,
            "account_validity": account_validity,
            "sub2api_state": sub2api_state,
            "oaipay_state": oaipay_state,
            "idea_submit_state": idea_submit_state,
            "revival_state": revival_state,
            "sort_by": sort_by,
            "sort_order": sort_order,
        },
    )
    count_q = select(func.count()).select_from(q.subquery())
    total = int(session.exec(count_q).one())
    if use_list_state:
        q = apply_account_list_state_sort(q, sort_by=sort_by, sort_order=sort_order)
    else:
        q = q.order_by(AccountModel.id.desc())
    items = session.exec(q.offset((page_value - 1) * page_size_value).limit(page_size_value)).all()
    extras_by_id = {
        int(item.id or 0): _safe_extra(item)
        for item in items
        if int(item.id or 0) > 0
    }
    return {
        "total": total,
        "page": page_value,
        "items": [
            (
                _serialize_account(item)
                if detail
                else _serialize_account_compact_item(
                    item,
                    extra=extras_by_id.get(int(item.id or 0)),
                )
            )
            for item in items
        ],
    }


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
                         acc.created_at.strftime("%Y-%m-%d %H:%M:%S")])
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
    if not body.ids:
        raise HTTPException(400, "账号 ID 列表不能为空")
    
    if len(body.ids) > 1000:
        raise HTTPException(400, "单次最多删除 1000 个账号")
    
    deleted_count = 0
    deleted_ids: list[int] = []
    not_found_ids = []
    
    try:
        for account_id in body.ids:
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
            "total_requested": len(body.ids)
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
    q = select(AccountModel)
    if body.platform:
        q = q.where(AccountModel.platform == body.platform)
    if body.status:
        q = q.where(AccountModel.status == body.status)
    if body.email:
        q = q.where(AccountModel.email.contains(body.email))

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
    return _serialize_account(acc)


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
        acc.token = body.token
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
