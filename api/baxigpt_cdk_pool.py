"""BaxiGPT 卡密池 HTTP API。"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from core import db as core_db
from core.db import AccountModel, TaskLog
from core.timezone import beijing_iso, beijing_log_time
from services.chatgpt_core.baxigpt_cdk_repository import (
    ALL_STATUSES,
    STATUS_AVAILABLE,
    STATUS_DISABLED,
    STATUS_FAILED,
    STATUS_PAID,
    STATUS_PROCESSING,
    STATUS_SUBMITTED,
    BaxiGptCdkRepository,
    mask_code,
)
from services.chatgpt_core.baxigpt_client import BaxiGptClient
from services.chatgpt_core.baxigpt_status_poller import (
    DEFAULT_INTERVAL_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    enqueue_many,
    snapshot as poller_snapshot,
)
from services.chatgpt_core.local_status_refresh import (
    summarize_status_refresh,
    sync_chatgpt_account_local_status,
)

router = APIRouter(prefix="/baxigpt-cdk-pool", tags=["baxigpt-cdk-pool"])
_repo = BaxiGptCdkRepository()
_import_job_lock = threading.RLock()
_import_jobs: dict[str, dict[str, Any]] = {}
_MAX_FINISHED_IMPORT_JOBS = 100


def _refresh_bound_account_local_status(record: Any, *, trigger: str) -> dict[str, Any] | None:
    if record is None or str(getattr(record, "status", "") or "").strip().lower() != STATUS_PAID:
        return None
    account_id = int(getattr(record, "bound_account_id", 0) or 0)
    email = str(getattr(record, "bound_account_email", "") or getattr(record, "remote_email", "") or "").strip()
    with Session(core_db.engine) as session:
        account = session.get(AccountModel, account_id) if account_id > 0 else None
        if account is None and email:
            account = session.exec(
                select(AccountModel)
                .where(AccountModel.platform == "chatgpt")
                .where(AccountModel.email == email)
            ).first()
        if account is None or account.platform != "chatgpt":
            return None
        try:
            refresh_result = sync_chatgpt_account_local_status(session, account)
            summary = summarize_status_refresh(refresh_result, trigger=trigger)
        except Exception as exc:
            summary = {
                "trigger": str(trigger or ""),
                "refreshed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "status": str(getattr(account, "status", "") or ""),
                "error": str(exc),
            }
        _repo.persist_account_binding_extra(
            account,
            record,
            status=STATUS_PAID,
            local_status_refresh=summary,
            apply_payment_state=False,
        )
        session.add(account)
        session.commit()
        return summary


class CdkImportRequest(BaseModel):
    text: str


class CdkUpdateRequest(BaseModel):
    label: Optional[str] = None
    status: Optional[str] = None


class CdkStatusQueryRequest(BaseModel):
    ids: list[int] = Field(default_factory=list)
    limit: int = 100


class CdkQuotaCheckRequest(BaseModel):
    ids: list[int] = Field(default_factory=list)
    limit: int = 100
    include_query: bool = True


class CdkStatusPollRequest(BaseModel):
    ids: list[int] = Field(default_factory=list)
    limit: int = 100
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS


class CdkSnapshotRequest(BaseModel):
    ids: list[int] = Field(default_factory=list)


class CdkCodeQueryRequest(BaseModel):
    codes: list[str] = Field(default_factory=list)
    text: str = ""


def _now_ts() -> float:
    return time.time()


def _new_import_job_id() -> str:
    return f"cdk_import_{int(time.time() * 1000)}"


def _append_import_job_log(job_id: str, message: str) -> None:
    with _import_job_lock:
        job = _import_jobs.get(job_id)
        if not job:
            return
        logs = list(job.get("logs") or [])
        logs.append(f"[{beijing_log_time()}] {message}")
        job["logs"] = logs[-300:]
        job["updated_at"] = _now_ts()


def _update_import_job(job_id: str, patch: dict[str, Any]) -> None:
    with _import_job_lock:
        job = _import_jobs.get(job_id)
        if not job:
            return
        job.update(dict(patch or {}))
        job["updated_at"] = _now_ts()


def _import_job_snapshot(job_id: str) -> dict[str, Any] | None:
    with _import_job_lock:
        job = _import_jobs.get(job_id)
        return dict(job) if job else None


def _cleanup_import_jobs() -> None:
    with _import_job_lock:
        finished = [
            (job_id, job)
            for job_id, job in _import_jobs.items()
            if str(job.get("status") or "") in {"done", "failed", "stopped"}
        ]
        if len(finished) <= _MAX_FINISHED_IMPORT_JOBS:
            return
        finished.sort(key=lambda item: float(item[1].get("created_at") or 0))
        for job_id, _job in finished[: len(finished) - _MAX_FINISHED_IMPORT_JOBS]:
            _import_jobs.pop(job_id, None)


@router.get("")
def list_baxigpt_cdk_pool(status: str = "", search: str = "", for_submit: bool = False):
    # Idea 提交需要按真实剩余额度选候选；多额度卡的最后一个订单可能已经
    # paid/failed，但仍有额度，任务会在真正提交前再次 code-info 校验。
    records = _repo.list_submit_candidates() if for_submit else _repo.list(status=status, search=search)
    if for_submit and str(search or "").strip():
        query = str(search or "").strip().lower()
        records = [
            item
            for item in records
            if query in item.code_value.lower()
            or query in item.code_masked.lower()
            or query in item.bound_account_email.lower()
            or query in item.order_id.lower()
            or query in item.display_id.lower()
            or query in item.label.lower()
        ]
    all_records = _repo.list()
    summary = _repo.summarize(all_records)
    if for_submit:
        summary["submit_candidates"] = len(records)
    return {
        "items": [item.to_dict(include_code=True) for item in records],
        "total": len(records),
        "available": summary.get("submit_candidates", summary.get("available", 0)),
        "summary": summary,
        "poller": poller_snapshot(),
    }


@router.get("/summary")
def get_baxigpt_cdk_pool_summary():
    records = _repo.list()
    summary = _repo.summarize(records)
    return {
        "available": summary.get("available", 0),
        "summary": summary,
    }


@router.post("/snapshot")
def snapshot_baxigpt_cdk_pool(body: CdkSnapshotRequest):
    ids: list[int] = []
    seen: set[int] = set()
    for value in body.ids or []:
        record_id = int(value or 0)
        if record_id <= 0 or record_id in seen:
            continue
        seen.add(record_id)
        ids.append(record_id)
    if len(ids) > 500:
        raise HTTPException(400, "单次最多读取 500 条卡密快照")
    items = []
    for record_id in ids:
        record = _repo.get_by_id(record_id)
        if record is not None:
            items.append(record.to_dict(include_code=True))
    return {
        "items": items,
        "total": len(items),
        "poller": poller_snapshot(),
    }


def _safe_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _poller_target_for(snapshot: dict[str, Any], record_id: int) -> dict[str, Any] | None:
    for target in snapshot.get("targets") or []:
        if not isinstance(target, dict):
            continue
        try:
            target_id = int(target.get("record_id") or target.get("id") or target.get("cdk_id") or 0)
        except Exception:
            target_id = 0
        if target_id == int(record_id or 0):
            return dict(target)
    return None


def _bound_account_payload(record: Any) -> dict[str, Any] | None:
    account_id = int(getattr(record, "bound_account_id", 0) or 0)
    candidate_email = str(
        getattr(record, "bound_account_email", "")
        or getattr(record, "remote_email", "")
        or ""
    ).strip()
    with Session(core_db.engine) as session:
        account = session.get(AccountModel, account_id) if account_id > 0 else None
        match_by = "id" if account is not None else ""
        if account is None and candidate_email:
            account = session.exec(
                select(AccountModel)
                .where(AccountModel.platform == "chatgpt")
                .where(AccountModel.email == candidate_email)
            ).first()
            if account is not None:
                match_by = "email"
        if account is None:
            return None
        extra = account.get_extra()
        baxigpt_cdk = extra.get("baxigpt_cdk") if isinstance(extra.get("baxigpt_cdk"), dict) else {}
        history = extra.get("baxigpt_cdk_history") if isinstance(extra.get("baxigpt_cdk_history"), list) else []
        idea_submit = extra.get("idea_submit") if isinstance(extra.get("idea_submit"), dict) else {}
        return {
            "id": int(account.id or 0),
            "email": str(account.email or ""),
            "platform": str(account.platform or ""),
            "status": str(account.status or ""),
            "updated_at": beijing_iso(account.updated_at) or None,
            "match_by": match_by,
            "baxigpt_cdk": baxigpt_cdk,
            "baxigpt_cdk_history": history[-10:],
            "idea_submit": idea_submit,
        }


def _task_log_payloads(record: Any, *, limit: int = 5) -> list[dict[str, Any]]:
    task_id = str(getattr(record, "task_id", "") or "").strip()
    email = str(getattr(record, "bound_account_email", "") or getattr(record, "remote_email", "") or "").strip()
    if not task_id and not email:
        return []
    with Session(core_db.engine) as session:
        stmt = select(TaskLog).where(TaskLog.platform == "chatgpt")
        if task_id:
            stmt = stmt.where(TaskLog.task_id == task_id)
        elif email:
            stmt = stmt.where(TaskLog.email == email)
        stmt = stmt.order_by(TaskLog.created_at.desc()).limit(max(1, min(int(limit or 5), 20)))
        logs = session.exec(stmt).all()
    items: list[dict[str, Any]] = []
    for log in logs:
        detail = _safe_json_object(log.detail_json)
        items.append({
            "id": int(log.id or 0),
            "task_id": str(log.task_id or ""),
            "email": str(log.email or ""),
            "status": str(log.status or ""),
            "error": str(log.error or ""),
            "created_at": beijing_iso(log.created_at) or None,
            "summary": {
                "source": detail.get("source"),
                "attempt_outcome": detail.get("attempt_outcome"),
                "task_id": detail.get("task_id"),
            },
        })
    return items


def _diagnostic_notes(record: Any, *, poller_target: dict[str, Any] | None, bound_account: dict[str, Any] | None) -> list[dict[str, str]]:
    status = str(getattr(record, "status", "") or "").strip().lower()
    order_id = str(getattr(record, "order_id", "") or "").strip()
    remaining = int(getattr(record, "code_info_remaining", 0) or 0)
    total = int(getattr(record, "code_info_total", 0) or 0)
    account_status = str((bound_account or {}).get("status") or "").strip().lower()
    account_cdk = (bound_account or {}).get("baxigpt_cdk") if isinstance((bound_account or {}).get("baxigpt_cdk"), dict) else {}
    account_cdk_status = str(account_cdk.get("status") or "").strip().lower()
    notes: list[dict[str, str]] = []

    if status in {STATUS_SUBMITTED, STATUS_PROCESSING} and order_id and poller_target is None:
        notes.append({"level": "warning", "message": "本地是待查询订单，但当前没有后台 poller target。可手动点“轮询”。"})
    if status in {STATUS_SUBMITTED, STATUS_PROCESSING} and not order_id:
        notes.append({"level": "error", "message": "本地状态是待查询，但没有 order_id，不能轮询上游订单状态。"})
    if status == STATUS_AVAILABLE and total > 0 and remaining <= 0:
        notes.append({"level": "warning", "message": "本地状态可用，但配额字段显示 remaining<=0，建议重新校验配额。"})
    if status == STATUS_FAILED and remaining > 0:
        notes.append({"level": "info", "message": "卡密状态为 failed，但仍有 remaining 配额，通常是历史订单失败或提交失败，可重新校验后决定是否恢复。"})
    if status == STATUS_PAID and account_status and account_status != "subscribed":
        notes.append({"level": "warning", "message": f"卡密已 paid，但绑定账号主状态是 {account_status}，建议检查账号同步。"})
    if status == STATUS_FAILED and account_status and account_status != "payment_failed":
        notes.append({"level": "warning", "message": f"卡密已 failed，但绑定账号主状态是 {account_status}，建议检查账号同步。"})
    if bound_account is not None and account_cdk_status and account_cdk_status != status:
        notes.append({"level": "warning", "message": f"账号 extra.baxigpt_cdk 状态是 {account_cdk_status}，和卡密池状态 {status or '-'} 不一致。"})
    if not notes:
        notes.append({"level": "success", "message": "当前没有发现明显链路不一致。"})
    return notes


@router.get("/{record_id}/diagnostics")
def get_baxigpt_cdk_diagnostics(record_id: int):
    record = _repo.get_by_id(record_id)
    if record is None:
        raise HTTPException(404, "卡密不存在")
    poller = poller_snapshot()
    poller_target = _poller_target_for(poller, int(record.id or 0))
    bound_account = _bound_account_payload(record)
    query_response = dict(record.last_query_response or {})
    orders = [
        order for order in (query_response.get("orders") if isinstance(query_response.get("orders"), list) else [])
        if isinstance(order, dict)
    ]
    remaining = query_response.get("remaining", record.code_info_remaining)
    total = query_response.get("total", record.code_info_total)
    try:
        used = int(query_response.get("used", max(int(total or 0) - int(remaining or 0), 0)))
    except Exception:
        used = 0
    return {
        "ok": True,
        "item": record.to_dict(include_code=True),
        "quota": {
            "remaining": remaining,
            "total": total,
            "used": used,
            "status_code": query_response.get("status_code") or record.upstream_status,
            "last_checked_at": record.last_checked_at or None,
            "source": "query" if query_response else "code_info_fields",
            "note": "code-info 原始响应未单独持久化；这里展示本地配额字段和 query 中的配额。"
        },
        "query_orders": orders,
        "query_response": query_response,
        "submit_response": dict(record.submit_response or {}),
        "last_status_response": dict(record.last_status_response or {}),
        "poller": poller,
        "poller_target": poller_target,
        "bound_account": bound_account,
        "task_logs": _task_log_payloads(record),
        "notes": _diagnostic_notes(record, poller_target=poller_target, bound_account=bound_account),
    }


def _refresh_imported_cdk_record(record_id: int, *, client: BaxiGptClient | None = None) -> dict[str, Any]:
    """逐个同步新入库卡密的配额和历史。"""
    record = _repo.get_by_id(record_id)
    if record is None:
        return {"id": record_id, "ok": False, "message": "卡密不存在"}

    upstream = client or BaxiGptClient()
    updated = record
    row: dict[str, Any] = {
        "id": record_id,
        "ok": True,
        "code_masked": record.code_masked,
        "code_value": record.code_value,
    }

    try:
        code_info = upstream.code_info(record.code_value)
        row["code_info"] = code_info
        checked_record = _repo.mark_code_info(record_id, code_info)
        if checked_record is not None:
            updated = checked_record
    except Exception as exc:
        row["ok"] = False
        row["code_info_error"] = str(exc)

    try:
        query_response = upstream.query(record.code_value)
        row["query"] = query_response
        queried = _repo.mark_query_response(record.code_value, query_response)
        if queried is not None:
            updated = queried
    except Exception as exc:
        row["ok"] = False
        row["query_error"] = str(exc)

    if updated is not None and updated.status == STATUS_PAID:
        local_status_refresh = _refresh_bound_account_local_status(updated, trigger="pix_cdk_import_query")
        row["account_synced"] = local_status_refresh is not None
        row["local_status_refresh"] = local_status_refresh
    else:
        row["account_synced"] = _repo.persist_bound_account_extra(updated)
    row["item"] = updated.to_dict(include_code=True)
    return row


def _run_import_query_job(job_id: str, record_ids: list[int]) -> None:
    client = BaxiGptClient()
    _update_import_job(job_id, {"status": "running"})
    _append_import_job_log(job_id, f"开始入库查询: {len(record_ids)} 个卡密")
    items: list[dict[str, Any]] = []
    success = 0
    failed = 0
    try:
        for index, record_id in enumerate(record_ids, start=1):
            _update_import_job(job_id, {"progress": index - 1})
            record = _repo.get_by_id(record_id)
            label = record.code_value if record is not None else str(record_id)
            _append_import_job_log(job_id, f"{index}/{len(record_ids)} 查询: {label}")
            row = _refresh_imported_cdk_record(record_id, client=client)
            items.append(row)
            if row.get("ok"):
                success += 1
                item = row.get("item") if isinstance(row.get("item"), dict) else {}
                status = str(item.get("status") or "-")
                remaining = item.get("code_info_remaining")
                total = item.get("code_info_total")
                _append_import_job_log(job_id, f"[OK] {label} status={status} remaining={remaining}/{total}")
            else:
                failed += 1
                message = str(row.get("code_info_error") or row.get("query_error") or row.get("message") or "查询失败")
                _append_import_job_log(job_id, f"[FAIL] {label} - {message}")
            _update_import_job(
                job_id,
                {
                    "progress": index,
                    "success": success,
                    "failed": failed,
                    "items": list(items),
                },
            )
        status = "done" if failed == 0 else "failed"
        _update_import_job(job_id, {"status": status, "error": "" if status == "done" else f"入库查询失败 {failed} 个"})
        _append_import_job_log(job_id, f"完成: 成功 {success} 个，失败 {failed} 个")
    except Exception as exc:
        _update_import_job(job_id, {"status": "failed", "error": str(exc)})
        _append_import_job_log(job_id, f"[FAIL] 入库查询任务异常: {exc}")
    finally:
        _cleanup_import_jobs()


def _create_import_query_job(record_ids: list[int]) -> dict[str, Any] | None:
    normalized_ids = [int(value or 0) for value in record_ids if int(value or 0) > 0]
    if not normalized_ids:
        return None
    job_id = _new_import_job_id()
    now = _now_ts()
    with _import_job_lock:
        _import_jobs[job_id] = {
            "id": job_id,
            "status": "pending",
            "total": len(normalized_ids),
            "progress": 0,
            "success": 0,
            "failed": 0,
            "items": [],
            "logs": [],
            "error": "",
            "created_at": now,
            "updated_at": now,
        }
    thread = threading.Thread(target=_run_import_query_job, args=(job_id, normalized_ids), daemon=True)
    thread.start()
    return _import_job_snapshot(job_id)


@router.post("/import")
def import_baxigpt_cdks(body: CdkImportRequest):
    result = _repo.import_lines(body.text)
    record_ids = [
        int(item.get("id") or 0)
        for item in result.get("items", [])
        if isinstance(item, dict) and int(item.get("id") or 0) > 0
    ]
    job = _create_import_query_job(record_ids)
    return {
        **result,
        "query_job_id": str((job or {}).get("id") or ""),
        "query_job": job,
        "checked": 0,
        "code_info_checked": 0,
        "query_checked": 0,
        "check_failed": 0,
        "query_items": [],
    }


@router.get("/import-jobs/{job_id}")
def get_baxigpt_cdk_import_job(job_id: str):
    job = _import_job_snapshot(job_id)
    if not job:
        raise HTTPException(404, "入库查询任务不存在")
    return job



@router.patch("/{record_id}")
def update_baxigpt_cdk(record_id: int, body: CdkUpdateRequest):
    if body.status is not None and body.status not in ALL_STATUSES:
        raise HTTPException(400, "卡密状态无效")
    if body.status is not None:
        record = _repo.set_status(record_id, body.status)
        if not record:
            raise HTTPException(404, "卡密不存在")
        return record.to_dict(include_code=True)
    record = _repo.get_by_id(record_id)
    if not record:
        raise HTTPException(404, "卡密不存在")
    return record.to_dict(include_code=True)


@router.post("/{record_id}/reset")
def reset_baxigpt_cdk(record_id: int):
    record = _repo.set_status(record_id, STATUS_AVAILABLE)
    if not record:
        raise HTTPException(404, "卡密不存在")
    return record.to_dict(include_code=True)


@router.post("/{record_id}/disable")
def disable_baxigpt_cdk(record_id: int):
    record = _repo.set_status(record_id, STATUS_DISABLED)
    if not record:
        raise HTTPException(404, "卡密不存在")
    return record.to_dict(include_code=True)


@router.post("/{record_id}/fail")
def fail_baxigpt_cdk(record_id: int):
    record = _repo.set_status(record_id, STATUS_FAILED)
    if not record:
        raise HTTPException(404, "卡密不存在")
    return record.to_dict(include_code=True)


@router.delete("/{record_id}")
def delete_baxigpt_cdk(record_id: int):
    ok = _repo.delete(record_id)
    if not ok:
        raise HTTPException(404, "卡密不存在")
    return {"ok": True, "id": record_id}


@router.post("/status")
def query_baxigpt_order_status(body: CdkStatusQueryRequest):
    ids = [int(value or 0) for value in body.ids if int(value or 0) > 0]
    if not ids:
        raise HTTPException(400, "请选择要查询的卡密记录")
    if len(ids) > max(int(body.limit or 100), 1):
        raise HTTPException(400, f"单次最多查询 {max(int(body.limit or 100), 1)} 条")
    client = BaxiGptClient()
    items: list[dict[str, Any]] = []
    for record_id in ids:
        record = _repo.get_by_id(record_id)
        if record is None:
            items.append({"id": record_id, "ok": False, "message": "卡密不存在"})
            continue
        if not record.order_id:
            items.append({"id": record_id, "ok": False, "code_masked": record.code_masked, "message": "没有 order_id，无法查订单状态"})
            continue
        try:
            response = client.status(record.order_id)
            updated = _repo.mark_status_response(record_id, response)
            if updated is not None and updated.status == STATUS_PAID:
                local_status_refresh = _refresh_bound_account_local_status(updated, trigger="pix_cdk_manual_status")
                account_synced = local_status_refresh is not None
            else:
                account_synced = _repo.persist_bound_account_extra(updated)
                local_status_refresh = None
            items.append({
                "id": record_id,
                "ok": True,
                "account_synced": account_synced,
                "local_status_refresh": local_status_refresh,
                "item": updated.to_dict(include_code=True) if updated else record.to_dict(include_code=True),
                "raw": response,
            })
        except Exception as exc:
            items.append({"id": record_id, "ok": False, "code_masked": record.code_masked, "message": str(exc)})
    return {"items": items, "total": len(items)}


@router.post("/quota")
def check_baxigpt_cdk_quota(body: CdkQuotaCheckRequest):
    ids = [int(value or 0) for value in body.ids if int(value or 0) > 0]
    if not ids:
        raise HTTPException(400, "请选择要校验配额的卡密记录")
    if len(ids) > max(int(body.limit or 100), 1):
        raise HTTPException(400, f"单次最多校验 {max(int(body.limit or 100), 1)} 条")
    client = BaxiGptClient()
    items: list[dict[str, Any]] = []
    for record_id in ids:
        record = _repo.get_by_id(record_id)
        if record is None:
            items.append({"id": record_id, "ok": False, "message": "卡密不存在"})
            continue
        try:
            code_info = client.code_info(record.code_value)
            updated = _repo.mark_code_info(record_id, code_info)
            query_response: dict[str, Any] | None = None
            if bool(body.include_query):
                try:
                    query_response = client.query(record.code_value)
                    queried = _repo.mark_query_response(record.code_value, query_response)
                    if queried is not None:
                        updated = queried
                except Exception as query_exc:
                    query_response = {"ok": False, "message": str(query_exc)}
            if updated is not None and updated.status == STATUS_PAID:
                local_status_refresh = _refresh_bound_account_local_status(updated, trigger="pix_cdk_quota_query")
                account_synced = local_status_refresh is not None
            else:
                account_synced = _repo.persist_bound_account_extra(updated)
                local_status_refresh = None
            items.append({
                "id": record_id,
                "ok": True,
                "account_synced": account_synced,
                "local_status_refresh": local_status_refresh,
                "item": updated.to_dict(include_code=True) if updated else record.to_dict(include_code=True),
                "code_info": code_info,
                "query": query_response,
            })
        except Exception as exc:
            items.append({"id": record_id, "ok": False, "code_masked": record.code_masked, "message": str(exc)})
    return {"items": items, "total": len(items)}


@router.get("/poll")
def get_baxigpt_status_poller():
    return poller_snapshot()


@router.post("/poll")
def start_baxigpt_order_status_poll(body: CdkStatusPollRequest):
    ids = [int(value or 0) for value in body.ids if int(value or 0) > 0]
    if not ids:
        raise HTTPException(400, "请选择要轮询的卡密记录")
    if len(ids) > max(int(body.limit or 100), 1):
        raise HTTPException(400, f"单次最多轮询 {max(int(body.limit or 100), 1)} 条")
    valid_ids: list[int] = []
    skipped: list[dict[str, Any]] = []
    for record_id in ids:
        record = _repo.get_by_id(record_id)
        if record is None:
            skipped.append({"id": record_id, "ok": False, "message": "卡密不存在"})
            continue
        if not record.order_id:
            skipped.append({"id": record_id, "ok": False, "code_masked": record.code_masked, "message": "没有 order_id，无法轮询订单状态"})
            continue
        if record.status in {"paid", "failed", "disabled"}:
            _repo.persist_bound_account_extra(record)
            skipped.append({"id": record_id, "ok": False, "code_masked": record.code_masked, "message": f"已是终态 {record.status}"})
            continue
        if record.status not in {STATUS_SUBMITTED, STATUS_PROCESSING}:
            skipped.append({
                "id": record_id,
                "ok": False,
                "code_masked": record.code_masked,
                "message": f"状态 {record.status or '-'} 不需要轮询",
            })
            continue
        valid_ids.append(record_id)
    queued = enqueue_many(
        valid_ids,
        interval_seconds=int(body.interval_seconds or DEFAULT_INTERVAL_SECONDS),
        timeout_seconds=int(body.timeout_seconds or DEFAULT_TIMEOUT_SECONDS),
        immediate=True,
        source="manual_poll",
    )
    return {
        "ok": True,
        "queued": queued,
        "skipped": skipped,
        "poller": poller_snapshot(),
    }


@router.post("/query")
def query_baxigpt_codes(body: CdkCodeQueryRequest):
    code_values: list[str] = []
    for raw in body.codes or []:
        text = str(raw or "").strip()
        if text:
            code_values.append(text)
    if body.text:
        for raw_line in str(body.text or "").splitlines():
            text = str(raw_line or "").strip()
            if text:
                code_values.append(text)
    deduped: list[str] = []
    seen: set[str] = set()
    for code in code_values:
        if code in seen:
            continue
        seen.add(code)
        deduped.append(code)
    if not deduped:
        raise HTTPException(400, "请粘贴要查询的卡密")
    if len(deduped) > 100:
        raise HTTPException(400, "单次最多查询 100 个卡密")
    client = BaxiGptClient()
    items: list[dict[str, Any]] = []
    for code in deduped:
        try:
            response = client.query(code)
            updated = _repo.mark_query_response(code, response)
            if updated is not None and updated.status == STATUS_PAID:
                local_status_refresh = _refresh_bound_account_local_status(updated, trigger="pix_cdk_query_text")
                account_synced = local_status_refresh is not None
            else:
                local_status_refresh = None
                account_synced = _repo.persist_bound_account_extra(updated)
            items.append({
                "ok": True,
                "account_synced": account_synced,
                "local_status_refresh": local_status_refresh,
                "code_masked": mask_code(code),
                "item": updated.to_dict(include_code=True) if updated else None,
                "raw": response,
            })
        except Exception as exc:
            items.append({"ok": False, "code_masked": mask_code(code), "message": str(exc)})
    return {"items": items, "total": len(items)}
