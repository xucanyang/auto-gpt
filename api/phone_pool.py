"""ChatGPT relay 自有手机号池 HTTP API。"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from core.db import AccountModel, engine
from services.chatgpt_core.phone_pool_repository import PhonePoolRecord, PhonePoolRepository, normalize_phone, serialize_phone_pool_records
from services.chatgpt_core.phone_api_forwarding import (
    PhoneApiForwardError,
    get_forwarding_config,
    get_inventory_status,
    set_forwarding_config,
    sync_phone_pool_inventory,
)

router = APIRouter(prefix="/phone-pool", tags=["phone-pool"])
_repo = PhonePoolRepository()


class PhonePoolAddRequest(BaseModel):
    phone: str
    api_url: str
    label: str = ""
    api_expired_date: str = ""
    max_accounts: int = 3


class PhonePoolImportRequest(BaseModel):
    text: str


class PhonePoolUpdateRequest(BaseModel):
    api_url: Optional[str] = None
    label: Optional[str] = None
    api_expired_date: Optional[str] = None
    max_accounts: Optional[int] = None
    status: Optional[str] = None


class PhonePoolSnapshotRequest(BaseModel):
    ids: list[int] = Field(default_factory=list)


class PhonePoolApiExpiryRefreshRequest(BaseModel):
    ids: list[int] = Field(default_factory=list)
    force: bool = False


class PhonePoolForwardingRequest(BaseModel):
    enabled: bool = False
    active_origin: str = ""
    previous_origins: list[str] = Field(default_factory=list)


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _find_record(record_id: int) -> PhonePoolRecord | None:
    target_id = int(record_id or 0)
    if target_id <= 0:
        return None
    for record in _repo.list():
        if int(record.id or 0) == target_id:
            return record
    return None


def _unique_positive_ids(values: list[int]) -> list[int]:
    ids: list[int] = []
    seen: set[int] = set()
    for value in values or []:
        try:
            item_id = int(value)
        except (TypeError, ValueError):
            continue
        if item_id <= 0 or item_id in seen:
            continue
        seen.add(item_id)
        ids.append(item_id)
    return ids


def _serialize_phone_records(records: list[PhonePoolRecord], all_records: list[PhonePoolRecord] | None = None) -> list[dict[str, Any]]:
    universe = all_records if all_records is not None else _repo.list()
    return serialize_phone_pool_records(records, all_records=universe)


def _serialize_phone_record(record: PhonePoolRecord) -> dict[str, Any]:
    return _serialize_phone_records([record])[0]


def _phone_pool_snapshot(ids: list[int]) -> dict[str, Any]:
    requested_ids = _unique_positive_ids(ids)
    records = _repo.list()
    by_id = {int(record.id or 0): record for record in records}
    items = _serialize_phone_records([by_id[item_id] for item_id in requested_ids if item_id in by_id], records)
    return {
        "items": items,
        "missing": [item_id for item_id in requested_ids if item_id not in by_id],
        "summary": _repo.summarize(records),
    }


def _extract_account_phone_binding(account: AccountModel) -> dict[str, Any]:
    try:
        extra = account.get_extra()
    except Exception:
        try:
            extra = json.loads(str(account.extra_json or "{}"))
        except Exception:
            extra = {}
    binding = extra.get("chatgpt_phone_binding") if isinstance(extra, dict) else None
    return binding if isinstance(binding, dict) else {}


def _matched_bound_accounts(phone: str) -> list[dict[str, Any]]:
    target_phone = normalize_phone(phone)
    if not target_phone:
        return []
    matches: list[dict[str, Any]] = []
    with Session(engine) as session:
        accounts = session.exec(select(AccountModel)).all()
    for account in accounts:
        binding = _extract_account_phone_binding(account)
        if not binding:
            continue
        bound_phone = normalize_phone(str(binding.get("phone") or ""))
        if bound_phone != target_phone:
            continue
        matches.append(
            {
                "id": int(account.id or 0),
                "email": str(account.email or ""),
                "account_status": str(account.status or ""),
                "binding_status": str(binding.get("status") or ""),
                "api_url": str(binding.get("api_url") or ""),
                "source_api_url": str(binding.get("source_api_url") or binding.get("api_url") or ""),
                "task_id": str(binding.get("task_id") or ""),
                "bound_at": str(binding.get("bound_at") or binding.get("updated_at") or binding.get("created_at") or ""),
                "error": str(binding.get("error") or binding.get("reason") or ""),
            }
        )
    matches.sort(key=lambda item: (item.get("email") or "", int(item.get("id") or 0)))
    return matches


def _phone_diagnostics(record: PhonePoolRecord) -> dict[str, Any]:
    item = _serialize_phone_record(record)
    matched_accounts = _matched_bound_accounts(record.phone_e164)
    bound_matches = [item for item in matched_accounts if str(item.get("binding_status") or "") == "bound"]
    now = datetime.now(timezone.utc)
    cooldown = _parse_time(record.cooldown_until)
    notes: list[dict[str, str]] = []

    def add_note(severity: str, code: str, message: str) -> None:
        notes.append({"severity": severity, "code": code, "message": message})

    if not str(record.api_url or "").strip():
        add_note("error", "missing_api_url", "这个号码没有收码 API，自动绑定任务不会选它。")
    if str(item.get("forward_status") or "").lower() in {"unavailable", "conflict", "error", "failed"}:
        add_note(
            "error",
            "api_forward_error",
            f"API 转发当前不可用：{item.get('forward_error') or item.get('forward_status') or '-'}；号码保持可用，不会回退原域名直连。",
        )
    if str(record.api_expiry_status or "") == "error":
        add_note("warning", "api_expiry_probe_failed", f"API 到期时间获取失败：{record.api_expiry_error or '-'}")
    if str(record.api_expiry_status or "") == "missing_expired_date":
        add_note("info", "api_expiry_missing", "收码 API 可访问，但响应里没有 data.expired_date。")
    if not str(record.api_expired_date or "").strip() and not str(record.api_expiry_checked_at or "").strip():
        add_note("info", "api_expiry_unchecked", "尚未获取 API 到期时间，导入后后台会自动补全一次。")
    if record.status == "disabled":
        add_note("info", "disabled", "号码已人工停用，不会进入自动取号。")
    if record.remaining_capacity <= 0:
        add_note("warning", "no_capacity", "本地剩余额度为 0，不会继续分配给新账号。")
    if record.status == "exhausted" and int(record.bound_count or 0) < int(record.max_accounts or 0):
        add_note("warning", "exhausted_desync", "状态是已绑满，但已绑定数小于上限，建议同步已绑定数或手动恢复。")
    if cooldown and cooldown > now:
        add_note("warning", "cooling", f"号码仍在冷却中，冷却至 {record.cooldown_until}。")
    if cooldown and cooldown <= now and record.status in {"cooldown", "rate_limited"}:
        add_note("warning", "cooldown_expired", "冷却时间已过，但状态还未恢复，可以点恢复可用。")
    if record.last_error_code or record.last_error_message:
        add_note(
            "error" if record.status in {"cannot_send", "rate_limited"} else "warning",
            "last_error",
            f"最近错误：{record.last_error_code or '-'} {record.last_error_message or ''}".strip(),
        )
    if int(record.bound_count or 0) != len(bound_matches):
        add_note(
            "warning",
            "bound_count_mismatch",
            f"本地已绑数 {int(record.bound_count or 0)}，账号记录中 bound 数 {len(bound_matches)}，建议同步已绑定数。",
        )
    if item.get("ordinary_task_eligible"):
        add_note("success", "ready", "号码自身可用，号段也可用，普通绑定任务会选它。")
    elif item.get("self_available") and item.get("ordinary_task_block_reason") == "prefix_unavailable":
        add_note("warning", "prefix_unavailable", "号码自身可用，但所属号段已被 OpenAI 拒绝样本判定为不可用，普通绑定任务会跳过。")

    return {
        "ok": True,
        "item": item,
        "matched_accounts": matched_accounts,
        "counts": {
            "local_bound_count": int(record.bound_count or 0),
            "matched_account_count": len(matched_accounts),
            "matched_bound_count": len(bound_matches),
            "remaining_capacity": int(record.remaining_capacity or 0),
            "max_accounts": int(record.max_accounts or 0),
        },
        "notes": notes,
    }


def _forwarding_overview(*, force_remote: bool = False) -> dict[str, Any]:
    records = _repo.list()
    config = get_forwarding_config(force=force_remote, strict=False)
    sync = get_inventory_status(force_remote=force_remote)
    source_hosts = {
        str(getattr(record, "api_host", "") or "").strip().lower()
        for record in records
        if str(getattr(record, "api_host", "") or "").strip()
    }
    return {
        **config,
        "affected_records": len(records),
        "source_host_count": len(source_hosts),
        "registry": {
            "status": str(sync.get("status") or "idle"),
            "last_error": str(sync.get("last_error") or ""),
        },
        "sync": sync,
    }


def _sync_forwarding_inventory(*, trigger: str, raise_on_error: bool = False) -> dict[str, Any]:
    return sync_phone_pool_inventory(_repo.list(), trigger=trigger, raise_on_error=raise_on_error)


@router.get("/forwarding")
def get_phone_pool_forwarding():
    return _forwarding_overview(force_remote=True)


@router.put("/forwarding")
def update_phone_pool_forwarding(body: PhonePoolForwardingRequest):
    previous = get_forwarding_config(force=True, strict=False)
    try:
        set_forwarding_config(
            enabled=bool(body.enabled),
            active_origin=body.active_origin,
            previous_origins=body.previous_origins,
        )
        _sync_forwarding_inventory(trigger="config-save", raise_on_error=True)
    except PhoneApiForwardError as exc:
        # Do not leave a newly activated origin with an empty/stale registry.
        try:
            if previous.get("relay_configured"):
                set_forwarding_config(
                    enabled=bool(previous.get("enabled")),
                    active_origin=str(previous.get("active_origin") or ""),
                    previous_origins=previous.get("previous_origins") or [],
                )
        except Exception:
            pass
        status_code = 409 if exc.code == "route_conflict" else 503
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    return _forwarding_overview(force_remote=True)


@router.post("/forwarding/sync")
def sync_phone_pool_forwarding():
    try:
        _sync_forwarding_inventory(trigger="manual", raise_on_error=True)
    except PhoneApiForwardError as exc:
        status_code = 409 if exc.code == "route_conflict" else 503
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    return _forwarding_overview(force_remote=True)


@router.get("")
def list_phone_pool(status: str = ""):
    records = _repo.list(status=status)
    all_records = _repo.list()
    items = _serialize_phone_records(records, all_records)
    summary = _repo.summarize(all_records)
    return {
        "items": items,
        "total": len(items),
        "available": summary["available"],
        "remaining_capacity": summary["remaining_capacity"],
        "summary": summary,
    }


@router.get("/summary")
def phone_pool_summary():
    records = _repo.list()
    return {"summary": _repo.summarize(records)}


@router.post("/snapshot")
def snapshot_phone_pool(body: PhonePoolSnapshotRequest):
    return _phone_pool_snapshot(body.ids)


@router.get("/{record_id}/diagnostics")
def phone_pool_diagnostics(record_id: int):
    record = _find_record(record_id)
    if not record:
        raise HTTPException(404, "号码不存在")
    return _phone_diagnostics(record)


@router.post("")
def add_phone_pool_item(body: PhonePoolAddRequest):
    record = _repo.add(
        phone=body.phone,
        api_url=body.api_url,
        label=body.label,
        max_accounts=body.max_accounts,
        api_expired_date=body.api_expired_date,
    )
    if not record:
        raise HTTPException(400, "phone 为空或 API URL 非法（需 http(s):// 开头）")
    sync = _sync_forwarding_inventory(trigger="add")
    payload = _serialize_phone_record(record)
    payload["forwarding_sync"] = sync
    return payload


@router.post("/import")
def import_phone_pool(body: PhonePoolImportRequest, background_tasks: BackgroundTasks):
    result = _repo.import_lines(body.text)
    refresh_ids = [int(value or 0) for value in result.get("refresh_ids", []) if int(value or 0) > 0]
    if refresh_ids:
        background_tasks.add_task(_repo.refresh_api_expiry_for_ids, refresh_ids, force=False)
    result["forwarding_sync"] = _sync_forwarding_inventory(trigger="import")
    return result


@router.post("/api-expiry/refresh")
def refresh_phone_pool_api_expiry(body: PhonePoolApiExpiryRefreshRequest):
    ids = _unique_positive_ids(body.ids)
    if not ids:
        raise HTTPException(400, "请选择要补全 API 到期时间的手机号")
    if len(ids) > 100:
        raise HTTPException(400, "单次最多补全 100 个手机号")
    result = _repo.refresh_api_expiry_for_ids(ids, force=bool(body.force))
    rows = _repo.list()
    enriched_by_id = {int(item.get("id") or 0): item for item in _serialize_phone_records(rows, rows)}
    for entry in result.get("results", []) if isinstance(result, dict) else []:
        try:
            entry_id = int(entry.get("id") or 0)
        except Exception:
            entry_id = 0
        if entry_id in enriched_by_id:
            entry["item"] = enriched_by_id[entry_id]
    return result


@router.patch("/{record_id}")
def update_phone_pool_item(record_id: int, body: PhonePoolUpdateRequest):
    record = _repo.update(
        record_id,
        api_url=body.api_url,
        label=body.label,
        max_accounts=body.max_accounts,
        api_expired_date=body.api_expired_date,
        status=body.status,
    )
    if not record:
        raise HTTPException(404, "号码不存在或 API URL 非法")
    sync = _sync_forwarding_inventory(trigger="update")
    payload = _serialize_phone_record(record)
    payload["forwarding_sync"] = sync
    return payload


@router.post("/{record_id}/reset")
def reset_phone_pool_item(record_id: int):
    record = _repo.reset_status(record_id)
    if not record:
        raise HTTPException(404, "号码不存在")
    return _serialize_phone_record(record)


@router.post("/{record_id}/enable")
def enable_phone_pool_item(record_id: int):
    record = _repo.set_enabled(record_id, True)
    if not record:
        raise HTTPException(404, "号码不存在")
    return _serialize_phone_record(record)


@router.post("/{record_id}/disable")
def disable_phone_pool_item(record_id: int):
    record = _repo.set_enabled(record_id, False)
    if not record:
        raise HTTPException(404, "号码不存在")
    return _serialize_phone_record(record)


@router.delete("/{record_id}")
def delete_phone_pool_item(record_id: int):
    ok = _repo.delete(record_id)
    if not ok:
        raise HTTPException(404, "号码不存在")
    return {"ok": True, "id": record_id, "forwarding_sync": _sync_forwarding_inventory(trigger="delete")}


@router.post("/reconcile")
def reconcile_phone_pool():
    result = _repo.reconcile_from_accounts()
    result["forwarding_sync"] = _sync_forwarding_inventory(trigger="reconcile")
    return result
