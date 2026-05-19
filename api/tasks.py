from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlmodel import Session, select
from typing import Any, Optional
from copy import deepcopy
from core.db import AccountModel, PendingBusinessInviteModel, TaskLog, engine
from core.task_runtime import (
    AttemptOutcome,
    AttemptResult,
    RegisterTaskStore,
    SkipCurrentAttemptRequested,
    StopTaskRequested,
)
from services.chatgpt_core.payment_link_cache import (
    cache_checkout_link_in_extra,
    payment_link_cache_matches,
    normalize_payment_link_params,
)
import time, json, asyncio, threading, logging

router = APIRouter(prefix="/tasks", tags=["tasks"])
logger = logging.getLogger(__name__)

MAX_FINISHED_TASKS = 200
CLEANUP_THRESHOLD = 250
_task_store = RegisterTaskStore(
    max_finished_tasks=MAX_FINISHED_TASKS,
    cleanup_threshold=CLEANUP_THRESHOLD,
)


class RegisterTaskRequest(BaseModel):
    platform: str
    email: Optional[str] = None
    password: Optional[str] = None
    count: int = 1
    concurrency: int = 1
    register_delay_seconds: float = 0
    proxy: Optional[str] = None
    executor_type: str = "protocol"
    captcha_solver: str = "yescaptcha"
    extra: dict = Field(default_factory=dict)


class TaskLogBatchDeleteRequest(BaseModel):
    ids: list[int]


class SubmitVerificationRequest(BaseModel):
    challenge_id: str
    code: str


class ResumeSubscriptionAuthTaskRequest(BaseModel):
    account_id: int


class BatchResumeSubscriptionAuthTaskRequest(BaseModel):
    account_ids: list[int] = Field(default_factory=list)
    all_filtered: bool = False
    email: str = ""
    status: str = ""


class BatchPaymentLinkTaskRequest(BaseModel):
    account_ids: list[int] = Field(default_factory=list)
    all_filtered: bool = False
    email: str = ""
    status: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    skip_existing: bool = True
    force_refresh: bool = False
    limit: int = 0


def _ensure_task_exists(task_id: str) -> None:
    if not _task_store.exists(task_id):
        raise HTTPException(404, "任务不存在")


def _ensure_task_mutable(task_id: str) -> None:
    _ensure_task_exists(task_id)
    snapshot = _task_store.snapshot(task_id)
    if snapshot.get("status") in {"done", "failed", "stopped"}:
        raise HTTPException(409, "任务已结束，无法再执行控制操作")


def _prepare_register_request(req: RegisterTaskRequest) -> RegisterTaskRequest:
    from core.config_store import config_store

    req_data = req.model_dump()
    req_data["extra"] = deepcopy(req_data.get("extra") or {})
    prepared = RegisterTaskRequest(**req_data)

    mail_provider = prepared.extra.get("mail_provider") or config_store.get(
        "mail_provider", ""
    )

    if mail_provider == "manual_email_otp":
        if prepared.platform != "chatgpt":
            raise HTTPException(400, "manual_email_otp 模式目前只支持 ChatGPT")
        prepared.email = str(prepared.email or "").strip()
        if not prepared.email:
            raise HTTPException(400, "manual_email_otp 模式必须填写邮箱地址")
        existing_account_capture = prepared.extra.get("chatgpt_existing_account_capture")
        if isinstance(existing_account_capture, str):
            existing_account_capture = existing_account_capture.strip().lower() in {"1", "true", "yes", "on"}
        else:
            existing_account_capture = bool(existing_account_capture)
        prepared.count = 1
        prepared.concurrency = 1
        if not existing_account_capture:
            prepared.password = None
        prepared.extra["manual_email_address"] = prepared.email
        prepared.extra["chatgpt_existing_account_capture"] = existing_account_capture

    if mail_provider == "luckmail":
        prepared.extra["luckmail_project_code"] = "openai"

    return prepared


def _create_task_record(
    task_id: str, req: RegisterTaskRequest, source: str, meta: dict | None = None
):
    _task_store.create(
        task_id,
        platform=req.platform,
        total=req.count,
        source=source,
        meta=meta,
    )


def _create_standalone_task_record(
    task_id: str,
    *,
    platform: str,
    source: str,
    total: int = 1,
    meta: dict | None = None,
) -> None:
    _task_store.create(
        task_id,
        platform=platform,
        total=max(int(total or 1), 1),
        source=source,
        meta=meta,
    )


def enqueue_register_task(
    req: RegisterTaskRequest,
    *,
    background_tasks: BackgroundTasks | None = None,
    source: str = "manual",
    meta: dict | None = None,
) -> str:
    prepared = _prepare_register_request(req)
    task_id = f"task_{int(time.time() * 1000)}"
    _create_task_record(task_id, prepared, source, meta)
    prepared_extra = _build_effective_register_extra(prepared)
    _save_task_log(
        prepared.platform,
        prepared.email or "",
        "running",
        detail=_build_task_log_detail(
            task_id,
            {
                "email": prepared.email or "",
                "attempt_outcome": "task_created",
                "requested_count": int(prepared.count or 0),
                "requested_concurrency": int(prepared.concurrency or 0),
                "requested_delay_seconds": float(prepared.register_delay_seconds or 0),
                "source": source,
                "meta": dict(meta or {}),
                "extra_flags": {
                    "mail_provider": str(prepared_extra.get("mail_provider") or ""),
                    "existing_account_capture": _is_truthy(prepared_extra.get("chatgpt_existing_account_capture")),
                    "capture_free_workspace": _is_truthy(prepared_extra.get("chatgpt_capture_free_workspace")),
                    "capture_business_workspace": _is_truthy(prepared_extra.get("chatgpt_capture_business_workspace")),
                    "enable_team_invite": _is_truthy(prepared_extra.get("chatgpt_enable_team_invite")),
                    "deferred_activation": _is_truthy(prepared_extra.get("chatgpt_team_invite_deferred_activation")),
                    "zero_amount_stop_enabled": _is_truthy(
                        prepared_extra.get("chatgpt_access_token_only_zero_amount_stop_enabled")
                    ),
                    "zero_amount_stop_threshold": str(
                        prepared_extra.get("chatgpt_access_token_only_zero_amount_stop_threshold") or ""
                    ),
                },
            },
        ),
    )
    if background_tasks is None:
        thread = threading.Thread(
            target=_run_register, args=(task_id, prepared), daemon=True
        )
        thread.start()
    else:
        background_tasks.add_task(_run_register, task_id, prepared)
    return task_id


def has_active_register_task(
    *, platform: str | None = None, source: str | None = None
) -> bool:
    return _task_store.has_active(platform=platform, source=source)


def enqueue_resume_subscription_auth_task(
    account_id: int,
    *,
    background_tasks: BackgroundTasks | None = None,
) -> str:
    account_id_value = int(account_id or 0)
    if account_id_value <= 0:
        raise HTTPException(400, "account_id 无效")

    with Session(engine) as session:
        account = session.get(AccountModel, account_id_value)
        if account is None or account.platform != "chatgpt":
            raise HTTPException(404, "ChatGPT 账号不存在")
        email = str(account.email or "")

    task_id = f"task_{int(time.time() * 1000)}"
    source = "resume_subscription_auth"
    meta = {"account_id": account_id_value, "email": email}
    _create_standalone_task_record(
        task_id,
        platform="chatgpt",
        source=source,
        total=1,
        meta=meta,
    )
    _save_task_log(
        "chatgpt",
        email,
        "running",
        detail=_build_task_log_detail(
            task_id,
            {
                "email": email,
                "account_id": account_id_value,
                "attempt_outcome": "task_created",
                "source": source,
                "meta": meta,
            },
        ),
    )
    if background_tasks is None:
        thread = threading.Thread(
            target=_run_resume_subscription_auth,
            args=(task_id, account_id_value),
            daemon=True,
        )
        thread.start()
    else:
        background_tasks.add_task(_run_resume_subscription_auth, task_id, account_id_value)
    return task_id


def _normalize_batch_account_ids(account_ids: list[int] | None) -> list[int]:
    normalized: list[int] = []
    seen: set[int] = set()
    for raw in account_ids or []:
        try:
            value = int(raw or 0)
        except Exception:
            continue
        if value <= 0 or value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return normalized


def _is_resume_auth_candidate(account: AccountModel, pending_status: str = "") -> bool:
    status = str(getattr(account, "status", "") or "").strip().lower()
    if status == "pending_payment":
        return True

    pending_status_value = str(pending_status or "").strip().lower()
    if pending_status_value and pending_status_value not in {"completed", "abandoned"}:
        return True

    extra = account.get_extra()
    capabilities = extra.get("chatgpt_capabilities") if isinstance(extra.get("chatgpt_capabilities"), dict) else {}
    auth_level = str(capabilities.get("auth_level") or "").strip().lower()
    upload_gate = str(capabilities.get("upload_gate") or "").strip().lower()
    if auth_level in {"access_token_only", "invalid"}:
        return True
    if upload_gate in {"blocked_missing_rt", "blocked_missing_workspace"}:
        return True
    return False


def _resolve_batch_resume_auth_accounts(
    req: BatchResumeSubscriptionAuthTaskRequest,
) -> tuple[list[dict[str, Any]], list[int], list[dict[str, Any]], list[dict[str, Any]]]:
    requested_ids = _normalize_batch_account_ids(req.account_ids)
    if requested_ids:
        if len(requested_ids) > 1000:
            raise HTTPException(400, "单次最多处理 1000 个账号")
        with Session(engine) as session:
            rows = session.exec(
                select(AccountModel)
                .where(AccountModel.platform == "chatgpt")
                .where(AccountModel.id.in_(requested_ids))
            ).all()
            row_map = {int(row.id or 0): row for row in rows if int(row.id or 0) > 0}
            pending_rows = session.exec(
                select(PendingBusinessInviteModel)
                .where(PendingBusinessInviteModel.account_id.in_(requested_ids))
            ).all()
            pending_by_account: dict[int, str] = {}
            for row in pending_rows:
                account_id = int(getattr(row, "account_id", 0) or 0)
                if account_id <= 0:
                    continue
                pending_by_account[account_id] = str(getattr(row, "status", "") or "")

        eligible: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        missing_ids: list[int] = []
        for account_id in requested_ids:
            account = row_map.get(account_id)
            if account is None:
                missing_ids.append(account_id)
                continue
            pending_status = str(pending_by_account.get(account_id) or "")
            if _is_resume_auth_candidate(account, pending_status=pending_status):
                eligible.append(
                    {
                        "account_id": account_id,
                        "email": str(account.email or ""),
                        "status": str(account.status or ""),
                        "pending_status": pending_status,
                    }
                )
            else:
                skipped.append(
                    {
                        "account_id": account_id,
                        "email": str(account.email or ""),
                        "status": str(account.status or ""),
                        "pending_status": pending_status,
                        "reason": "账号当前无需补抓 Auth",
                    }
                )
        return eligible, missing_ids, skipped, []

    if not bool(req.all_filtered):
        raise HTTPException(400, "请提供 account_ids，或指定 all_filtered=true")

    with Session(engine) as session:
        query = select(AccountModel).where(AccountModel.platform == "chatgpt")
        if req.status:
            query = query.where(AccountModel.status == req.status)
        if req.email:
            query = query.where(AccountModel.email.contains(req.email))
        rows = session.exec(query).all()
        if len(rows) > 1000:
            raise HTTPException(400, "单次最多处理 1000 个账号")
        account_ids = [int(row.id or 0) for row in rows if int(row.id or 0) > 0]
        pending_rows = session.exec(
            select(PendingBusinessInviteModel).where(PendingBusinessInviteModel.account_id.in_(account_ids))
        ).all() if account_ids else []

    pending_by_account: dict[int, str] = {}
    for row in pending_rows:
        account_id = int(getattr(row, "account_id", 0) or 0)
        if account_id <= 0:
            continue
        pending_by_account[account_id] = str(getattr(row, "status", "") or "")

    eligible = []
    skipped = []
    matched = []
    for account in rows:
        account_id = int(account.id or 0)
        if account_id <= 0:
            continue
        pending_status = str(pending_by_account.get(account_id) or "")
        matched.append(
            {
                "account_id": account_id,
                "email": str(account.email or ""),
                "status": str(account.status or ""),
                "pending_status": pending_status,
            }
        )
        if _is_resume_auth_candidate(account, pending_status=pending_status):
            eligible.append(matched[-1])
        else:
            skipped.append(
                {
                    **matched[-1],
                    "reason": "账号当前无需补抓 Auth",
                }
            )
    return eligible, [], skipped, matched


def _json_object_from_config(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    text = str(value or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except Exception:
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _filtered_payment_link_request_params(params: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(params, dict):
        return {}
    allowed_keys = {
        "plan",
        "country",
        "currency",
        "proxy",
        "promo_code",
        "workspace_name",
        "seat_quantity",
        "price_interval",
        "billing_name",
        "billing_email",
        "billing_country",
        "billing_line1",
        "billing_city",
        "billing_state",
        "billing_postal_code",
        "reuse_cached_link",
    }
    return {key: value for key, value in params.items() if key in allowed_keys and value is not None}


def _build_batch_payment_link_params(account: AccountModel, request_params: dict[str, Any]) -> dict[str, Any]:
    from core.config_store import config_store

    global_defaults = _json_object_from_config(config_store.get("chatgpt_payment_link_defaults", ""))
    extra = account.get_extra()
    account_defaults = extra.get("chatgpt_payment_link_defaults") if isinstance(extra.get("chatgpt_payment_link_defaults"), dict) else {}
    merged = {
        **global_defaults,
        **account_defaults,
        **_filtered_payment_link_request_params(request_params),
    }
    params = normalize_payment_link_params(merged)
    for key in (
        "billing_name",
        "billing_email",
        "billing_country",
        "billing_line1",
        "billing_city",
        "billing_state",
        "billing_postal_code",
    ):
        if key in merged:
            params[key] = str(merged.get(key) or "").strip()
    return params


def _payment_link_skip_reason(account: AccountModel, *, force_refresh: bool = False) -> str:
    if bool(force_refresh):
        return ""
    status = str(getattr(account, "status", "") or "").strip().lower()
    if status == "subscribed":
        return "账号已订阅，默认不预生成订阅链接"
    if status == "invalid":
        return "账号已失效，默认不预生成订阅链接"
    return ""


def _resolve_batch_payment_link_accounts(
    req: BatchPaymentLinkTaskRequest,
) -> tuple[list[dict[str, Any]], list[int], list[dict[str, Any]], list[dict[str, Any]]]:
    requested_ids = _normalize_batch_account_ids(req.account_ids)
    force_refresh = bool(req.force_refresh)
    limit = max(int(req.limit or 0), 0)

    if requested_ids:
        if len(requested_ids) > 1000:
            raise HTTPException(400, "单次最多处理 1000 个账号")
        with Session(engine) as session:
            rows = session.exec(
                select(AccountModel)
                .where(AccountModel.platform == "chatgpt")
                .where(AccountModel.id.in_(requested_ids))
            ).all()
        row_map = {int(row.id or 0): row for row in rows if int(row.id or 0) > 0}
        eligible: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        missing_ids: list[int] = []
        for account_id in requested_ids:
            account = row_map.get(account_id)
            if account is None:
                missing_ids.append(account_id)
                continue
            item = {
                "account_id": account_id,
                "email": str(account.email or ""),
                "status": str(account.status or ""),
            }
            reason = _payment_link_skip_reason(account, force_refresh=force_refresh)
            if reason:
                skipped.append({**item, "reason": reason})
            else:
                eligible.append(item)
        if limit > 0:
            overflow = eligible[limit:]
            eligible = eligible[:limit]
            skipped.extend({**item, "reason": f"超过本次限制 limit={limit}"} for item in overflow)
        return eligible, missing_ids, skipped, []

    if not bool(req.all_filtered):
        raise HTTPException(400, "请提供 account_ids，或指定 all_filtered=true")

    with Session(engine) as session:
        query = select(AccountModel).where(AccountModel.platform == "chatgpt")
        if req.status:
            query = query.where(AccountModel.status == req.status)
        if req.email:
            query = query.where(AccountModel.email.contains(req.email))
        rows = session.exec(query).all()

    if limit > 0:
        rows = rows[:limit]
    if len(rows) > 1000:
        raise HTTPException(400, "单次最多处理 1000 个账号")

    eligible = []
    skipped = []
    matched = []
    for account in rows:
        account_id = int(account.id or 0)
        if account_id <= 0:
            continue
        item = {
            "account_id": account_id,
            "email": str(account.email or ""),
            "status": str(account.status or ""),
        }
        matched.append(item)
        reason = _payment_link_skip_reason(account, force_refresh=force_refresh)
        if reason:
            skipped.append({**item, "reason": reason})
        else:
            eligible.append(item)
    return eligible, [], skipped, matched


def enqueue_batch_resume_subscription_auth_task(
    req: BatchResumeSubscriptionAuthTaskRequest,
    *,
    background_tasks: BackgroundTasks | None = None,
) -> dict[str, Any]:
    eligible_accounts, missing_ids, skipped_accounts, matched_accounts = _resolve_batch_resume_auth_accounts(req)
    total_requested = len(_normalize_batch_account_ids(req.account_ids)) if req.account_ids else len(matched_accounts)

    task_id = f"task_{int(time.time() * 1000)}"
    source = "batch_resume_subscription_auth"
    meta = {
        "total_requested": total_requested,
        "matched": len(matched_accounts),
        "eligible": len(eligible_accounts),
        "missing_ids": list(missing_ids),
        "account_ids": [int(item["account_id"]) for item in eligible_accounts],
        "emails": [str(item["email"] or "") for item in eligible_accounts],
        "filter": {
            "all_filtered": bool(req.all_filtered),
            "status": str(req.status or ""),
            "email": str(req.email or ""),
        },
        "skipped_items": list(skipped_accounts),
    }
    _create_standalone_task_record(
        task_id,
        platform="chatgpt",
        source=source,
        total=max(len(eligible_accounts), 1),
        meta=meta,
    )

    primary_email = str(eligible_accounts[0]["email"] or "") if eligible_accounts else ""
    _save_task_log(
        "chatgpt",
        primary_email,
        "running",
        detail=_build_task_log_detail(
            task_id,
            {
                "email": primary_email,
                "attempt_outcome": "task_created",
                "source": source,
                "meta": meta,
            },
        ),
    )

    account_ids = [int(item["account_id"]) for item in eligible_accounts]
    if background_tasks is None:
        thread = threading.Thread(
            target=_run_batch_resume_subscription_auth,
            args=(task_id, account_ids),
            daemon=True,
        )
        thread.start()
    else:
        background_tasks.add_task(_run_batch_resume_subscription_auth, task_id, account_ids)

    return {
        "task_id": task_id,
        "total_requested": total_requested,
        "matched": len(matched_accounts),
        "eligible": len(eligible_accounts),
        "skipped": len(skipped_accounts),
        "missing": len(missing_ids),
        "items": eligible_accounts,
        "skipped_items": skipped_accounts,
        "missing_ids": missing_ids,
    }


def enqueue_batch_payment_link_task(
    req: BatchPaymentLinkTaskRequest,
    *,
    background_tasks: BackgroundTasks | None = None,
) -> dict[str, Any]:
    eligible_accounts, missing_ids, skipped_accounts, matched_accounts = _resolve_batch_payment_link_accounts(req)
    total_requested = len(_normalize_batch_account_ids(req.account_ids)) if req.account_ids else len(matched_accounts)

    task_id = f"task_{int(time.time() * 1000)}"
    source = "batch_payment_link"
    request_params = _filtered_payment_link_request_params(req.params)
    meta = {
        "total_requested": total_requested,
        "matched": len(matched_accounts),
        "eligible": len(eligible_accounts),
        "missing_ids": list(missing_ids),
        "account_ids": [int(item["account_id"]) for item in eligible_accounts],
        "emails": [str(item["email"] or "") for item in eligible_accounts],
        "filter": {
            "all_filtered": bool(req.all_filtered),
            "status": str(req.status or ""),
            "email": str(req.email or ""),
        },
        "params": dict(request_params),
        "skip_existing": bool(req.skip_existing),
        "force_refresh": bool(req.force_refresh),
        "limit": int(req.limit or 0),
        "skipped_items": list(skipped_accounts),
    }
    _create_standalone_task_record(
        task_id,
        platform="chatgpt",
        source=source,
        total=max(len(eligible_accounts), 1),
        meta=meta,
    )

    primary_email = str(eligible_accounts[0]["email"] or "") if eligible_accounts else ""
    _save_task_log(
        "chatgpt",
        primary_email,
        "running",
        detail=_build_task_log_detail(
            task_id,
            {
                "email": primary_email,
                "attempt_outcome": "task_created",
                "source": source,
                "meta": meta,
            },
        ),
    )

    account_ids = [int(item["account_id"]) for item in eligible_accounts]
    if background_tasks is None:
        thread = threading.Thread(
            target=_run_batch_payment_links,
            args=(task_id, account_ids),
            daemon=True,
        )
        thread.start()
    else:
        background_tasks.add_task(_run_batch_payment_links, task_id, account_ids)

    return {
        "task_id": task_id,
        "total_requested": total_requested,
        "matched": len(matched_accounts),
        "eligible": len(eligible_accounts),
        "skipped": len(skipped_accounts),
        "missing": len(missing_ids),
        "items": eligible_accounts,
        "skipped_items": skipped_accounts,
        "missing_ids": missing_ids,
    }


def _log(task_id: str, msg: str):
    """向任务追加一条日志"""
    ts = time.strftime("%H:%M:%S")
    entry = f"[{ts}] {msg}"
    _task_store.append_log(task_id, entry)
    print(entry)


def _save_task_log(
    platform: str, email: str, status: str, error: str = "", detail: dict = None
):
    """Write or update one TaskLog record per task_id."""
    detail = detail or {}
    task_id = str(detail.get("task_id") or "").strip()
    with Session(engine) as s:
        log = None
        if task_id:
            log = s.exec(
                select(TaskLog)
                .where(TaskLog.task_id == task_id)
                .order_by(TaskLog.id.desc())
            ).first()

        if log is None:
            log = TaskLog(
                task_id=task_id,
                platform=platform,
                email=email,
                status=status,
                error=error,
                detail_json=json.dumps(detail, ensure_ascii=False),
            )
            s.add(log)
        else:
            log.platform = platform
            log.email = email
            log.status = status
            log.error = error
            log.detail_json = json.dumps(detail, ensure_ascii=False)
            s.add(log)
        s.commit()


def _build_task_log_detail(task_id: str, extra: dict | None = None) -> dict:
    try:
        snapshot = _task_store.snapshot(task_id)
    except Exception:
        snapshot = {}
    detail = {
        "task_id": task_id,
        "status_snapshot": str(snapshot.get("status") or ""),
        "progress": str(snapshot.get("progress") or ""),
        "success": int(snapshot.get("success") or 0),
        "skipped": int(snapshot.get("skipped") or 0),
        "errors": list(snapshot.get("errors") or []),
        "cashier_urls": list(snapshot.get("cashier_urls") or []),
        "source": str(snapshot.get("source") or ""),
        "meta": dict(snapshot.get("meta") or {}),
        "logs": list(snapshot.get("logs") or []),
    }
    if extra:
        detail.update(extra)
    return detail


def _task_log_summary(log: TaskLog) -> dict:
    detail = _task_log_detail_dict(log)
    return {
        "id": log.id,
        "platform": log.platform,
        "email": log.email,
        "status": _effective_task_log_status(log, detail=detail),
        "error": log.error,
        "created_at": log.created_at,
        "task_id": str(detail.get("task_id") or ""),
    }


def _task_log_detail_payload(log: TaskLog) -> dict:
    detail = _task_log_detail_dict(log)
    return {
        **_task_log_summary(log),
        "detail": detail,
    }


def _task_log_detail_dict(log: TaskLog) -> dict[str, Any]:
    try:
        detail = json.loads(log.detail_json or "{}")
        if not isinstance(detail, dict):
            detail = {}
    except Exception:
        detail = {}
    return detail


def _task_log_runtime_status(task_id: str) -> str:
    task_id_value = str(task_id or "").strip()
    if not task_id_value:
        return ""
    if not _task_store.exists(task_id_value):
        return "stopped"
    snapshot = _task_store.snapshot(task_id_value)
    runtime_status = str(snapshot.get("status") or "").strip().lower()
    if runtime_status == "done":
        return "success"
    if runtime_status in {"failed", "stopped", "running", "pending"}:
        return runtime_status
    return ""


def _effective_task_log_status(log: TaskLog, *, detail: dict[str, Any] | None = None) -> str:
    base_status = str(log.status or "").strip().lower()
    if base_status != "running":
        return base_status
    payload = detail or _task_log_detail_dict(log)
    runtime_status = _task_log_runtime_status(str(payload.get("task_id") or ""))
    return runtime_status or base_status


def _group_latest_task_logs(logs: list[TaskLog]) -> list[TaskLog]:
    grouped: list[TaskLog] = []
    seen: set[str] = set()
    for log in logs:
        detail = _task_log_detail_dict(log)
        task_id = str(detail.get("task_id") or "").strip()
        group_key = f"task:{task_id}" if task_id else f"log:{int(log.id or 0)}"
        if group_key in seen:
            continue
        seen.add(group_key)
        grouped.append(log)
    return grouped


def _auto_upload_integrations(task_id: str, account):
    """注册成功后自动导入外部系统。"""
    _log(task_id, f"[Auto Upload] 开始自动同步外部系统，account_id={getattr(account, 'id', 'unknown')}")
    try:
        from services.external_sync import sync_account

        for result in sync_account(account):
            name = result.get("name", "Auto Upload")
            ok = bool(result.get("ok"))
            msg = result.get("msg", "")
            _log(task_id, f"  [{name}] {'[OK] ' + msg if ok else '[FAIL] ' + msg}")
    except Exception as e:
        _log(task_id, f"  [Auto Upload] 自动导入异常: {e}")


def _is_truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _parse_positive_int(value, default: int = 1) -> int:
    try:
        parsed = int(str(value or "").strip())
    except (TypeError, ValueError):
        return max(int(default or 1), 1)
    return parsed if parsed > 0 else max(int(default or 1), 1)


def _cache_chatgpt_checkout_link(extra: dict[str, Any], *, source: str) -> dict[str, Any]:
    return cache_checkout_link_in_extra(extra, source=source)


def _build_effective_register_extra(req: RegisterTaskRequest) -> dict:
    from core.config_store import config_store

    merged_extra = config_store.get_all().copy()
    merged_extra.update(
        {k: v for k, v in req.extra.items() if v is not None and v != ""}
    )
    if req.platform == "chatgpt":
        checkout_country = str(
            merged_extra.get("chatgpt_access_token_only_checkout_country")
            or ""
        ).strip()
        checkout_currency = str(
            merged_extra.get("chatgpt_access_token_only_checkout_currency")
            or ""
        ).strip()
        if checkout_country and not merged_extra.get("chatgpt_checkout_country"):
            merged_extra["chatgpt_checkout_country"] = checkout_country
        if checkout_currency and not merged_extra.get("chatgpt_checkout_currency"):
            merged_extra["chatgpt_checkout_currency"] = checkout_currency
    if req.email:
        merged_extra.setdefault("manual_email_address", req.email)
        merged_extra.setdefault("email", req.email)
    return merged_extra


def _should_precheck_chatgpt_deferred_invite(
    req: RegisterTaskRequest,
    merged_extra: dict | None = None,
) -> bool:
    effective_extra = merged_extra or _build_effective_register_extra(req)
    return (
        req.platform == "chatgpt"
        and _is_truthy(effective_extra.get("chatgpt_enable_team_invite"))
        and _is_truthy(effective_extra.get("chatgpt_team_invite_deferred_activation"))
    )


def _check_chatgpt_deferred_invite_availability(
    req: RegisterTaskRequest,
    *,
    merged_extra: dict | None = None,
    log_fn=None,
) -> tuple[bool, str]:
    effective_extra = merged_extra or _build_effective_register_extra(req)
    if not _should_precheck_chatgpt_deferred_invite(req, effective_extra):
        return True, ""

    try:
        from services.chatgpt_core.business_workspace_recovery import BusinessWorkspaceRecovery
        from services.team_embedded_backend import team_embedded_backend

        recovery = BusinessWorkspaceRecovery(
            effective_extra,
            proxy=req.proxy,
            browser_mode=req.executor_type or effective_extra.get("default_executor") or "protocol",
            log_fn=(lambda message, *_args: log_fn(message)) if callable(log_fn) else None,
        )
        if not recovery.is_enabled():
            return False, "本地 Team 运行时不可用，无法执行延迟邀请"
        teams = recovery.list_available_teams()
        if not teams:
            return False, "当前没有可邀请 team，延迟邀请任务终止"

        candidate_team_ids = []
        for item in teams:
            try:
                team_id = int((item or {}).get("id") or 0)
            except Exception:
                team_id = 0
            if team_id > 0 and team_id not in candidate_team_ids:
                candidate_team_ids.append(team_id)

        # 预检查不能只看旧 DB 状态；先强制刷新候选 Team，剔除 token 失效/已满/过期项。
        verified_team_ids = []
        for team_id in candidate_team_ids:
            try:
                sync_result = team_embedded_backend.sync_team_info(team_id, force_refresh=True)
            except Exception as exc:
                if callable(log_fn):
                    log_fn(f"[邀请] 预检查刷新 team={team_id} 失败: {exc}")
                continue
            if bool((sync_result or {}).get("success")):
                verified_team_ids.append(team_id)
                continue
            if callable(log_fn):
                error_text = str((sync_result or {}).get("error") or "unknown").strip() or "unknown"
                log_fn(f"[邀请] 预检查剔除 team={team_id}: {error_text}")

        if not verified_team_ids:
            return False, "预检查刷新后候选 Team 全部不可用，延迟邀请任务终止"

        teams = recovery.list_available_teams()
        if not teams:
            return False, "预检查刷新后没有可邀请 team，延迟邀请任务终止"

        team_ids = []
        verified_team_id_set = set(verified_team_ids)
        for item in teams:
            try:
                team_id = int((item or {}).get("id") or 0)
            except Exception:
                team_id = 0
            if team_id <= 0 or team_id not in verified_team_id_set:
                continue
            if team_id not in team_ids:
                team_ids.append(team_id)
        if isinstance(effective_extra, dict) and team_ids:
            effective_extra["chatgpt_deferred_invite_team_ids"] = list(team_ids)
            effective_extra["chatgpt_deferred_invite_team_id"] = team_ids[0]
        if team_ids:
            available_text = ",".join(str(team_id) for team_id in team_ids)
            return True, f"延迟邀请预检查通过，可用 team_id={available_text}"
        return False, "预检查刷新后候选 Team 全部不可用，延迟邀请任务终止"
    except Exception as exc:
        return False, f"延迟邀请预检查失败: {exc}"


def _run_resume_subscription_auth(task_id: str, account_id: int):
    from api.actions import (
        _apply_chatgpt_resume_auth_result,
        _execute_chatgpt_resume_subscription_auth,
    )

    control = _task_store.control_for(task_id)
    _task_store.mark_running(task_id)
    _task_store.set_progress(task_id, "0/1")
    email = ""
    errors: list[str] = []
    try:
        control.checkpoint()
        with Session(engine) as session:
            account = session.get(AccountModel, int(account_id or 0))
            if account is None or account.platform != "chatgpt":
                raise ValueError("ChatGPT 账号不存在")
            email = str(account.email or "")

        _log(task_id, f"[补抓] -------- 账号 {account_id} --------")
        _log(task_id, f"[补抓] 开始补抓 Auth：{email or account_id}")
        attempt_id = control.start_attempt()
        try:
            control.checkpoint(attempt_id=attempt_id)
            with Session(engine) as session:
                account = session.get(AccountModel, int(account_id or 0))
                if account is None or account.platform != "chatgpt":
                    raise ValueError("ChatGPT 账号不存在")
                result = _execute_chatgpt_resume_subscription_auth(
                    account,
                    log_fn=lambda message: _log(task_id, message),
                )
                session.refresh(account)
                _apply_chatgpt_resume_auth_result(account, result, session)
                if not bool(result.get("ok")):
                    data = result.get("data") if isinstance(result.get("data"), dict) else {}
                    raise ValueError(str(result.get("error") or data.get("message") or "补抓Auth失败"))
            control.checkpoint(attempt_id=attempt_id)
        finally:
            control.finish_attempt(attempt_id)

        _task_store.set_progress(task_id, "1/1")
        _log(task_id, f"[OK] 补抓Auth完成: {email or account_id}")
        _save_task_log(
            "chatgpt",
            email,
            "success",
            detail=_build_task_log_detail(
                task_id,
                {
                    "attempt_outcome": "resume_subscription_auth_success",
                    "email": email,
                    "account_id": int(account_id or 0),
                },
            ),
        )
        _task_store.finish(task_id, status="done", success=1, skipped=0, errors=[])
    except SkipCurrentAttemptRequested as exc:
        _log(task_id, f"[SKIP] 已跳过补抓Auth: {exc}")
        _save_task_log(
            "chatgpt",
            email,
            "skipped",
            error=str(exc),
            detail=_build_task_log_detail(
                task_id,
                {
                    "attempt_outcome": "resume_subscription_auth_skipped",
                    "email": email,
                    "account_id": int(account_id or 0),
                },
            ),
        )
        _task_store.finish(task_id, status="stopped", success=0, skipped=1, errors=[])
    except StopTaskRequested as exc:
        _log(task_id, f"[STOP] {exc}")
        _save_task_log(
            "chatgpt",
            email,
            "stopped",
            error=str(exc),
            detail=_build_task_log_detail(
                task_id,
                {
                    "attempt_outcome": "resume_subscription_auth_stopped",
                    "email": email,
                    "account_id": int(account_id or 0),
                },
            ),
        )
        _task_store.finish(task_id, status="stopped", success=0, skipped=0, errors=[])
    except Exception as exc:
        error_text = str(exc) or "补抓Auth失败"
        errors.append(error_text)
        _log(task_id, f"[FAIL] 补抓Auth失败: {error_text}")
        _save_task_log(
            "chatgpt",
            email,
            "failed",
            error=error_text,
            detail=_build_task_log_detail(
                task_id,
                {
                    "attempt_outcome": "resume_subscription_auth_failed",
                    "email": email,
                    "account_id": int(account_id or 0),
                },
            ),
        )
        _task_store.finish(
            task_id,
            status="failed",
            success=0,
            skipped=0,
            errors=errors,
            error=error_text,
        )
    finally:
        _task_store.cleanup()


def _run_batch_payment_links(task_id: str, account_ids: list[int]):
    from api.actions import _execute_platform_action
    from core.base_platform import RegisterConfig
    from core.config_store import config_store
    from services.chatgpt_core import ChatGPTPlatform
    from datetime import datetime, timezone

    control = _task_store.control_for(task_id)
    total = max(len(account_ids), 1)
    success_count = 0
    skipped_count = 0
    errors: list[str] = []
    primary_email = ""

    _task_store.mark_running(task_id)
    _task_store.set_progress(task_id, f"0/{total}")

    meta = dict(_task_store.snapshot(task_id).get("meta") or {})
    skipped_items = list(meta.get("skipped_items") or [])
    missing_ids = list(meta.get("missing_ids") or [])
    request_params = dict(meta.get("params") or {})
    skip_existing = bool(meta.get("skip_existing", True))
    force_refresh = bool(meta.get("force_refresh", False))
    instance = ChatGPTPlatform(config=RegisterConfig(extra=config_store.get_all()))

    try:
        for missing_id in missing_ids:
            _log(task_id, f"[MISS] 账号不存在: account_id={missing_id}")
            errors.append(f"account_id={missing_id}: 账号不存在")

        for item in skipped_items:
            _log(task_id, f"[SKIP] {item.get('email') or item.get('account_id') or '-'} - {item.get('reason') or '已跳过'}")

        for index, account_id in enumerate(account_ids, start=1):
            control.checkpoint(consume_skip=False)
            email = ""
            attempt_id = control.start_attempt()
            try:
                control.checkpoint(attempt_id=attempt_id)
                with Session(engine) as session:
                    account = session.get(AccountModel, int(account_id or 0))
                    if account is None or account.platform != "chatgpt":
                        raise ValueError("ChatGPT 账号不存在")
                    email = str(account.email or "")
                    if not primary_email:
                        primary_email = email

                    skip_reason = _payment_link_skip_reason(account, force_refresh=force_refresh)
                    if skip_reason:
                        skipped_count += 1
                        _log(task_id, f"[SKIP] 订阅链接跳过: {email or account_id} - {skip_reason}")
                        continue

                    params = _build_batch_payment_link_params(account, request_params)
                    params["save_defaults"] = False
                    if force_refresh:
                        params["reuse_cached_link"] = False
                    elif "reuse_cached_link" in request_params:
                        params["reuse_cached_link"] = request_params.get("reuse_cached_link") is not False
                    else:
                        params["reuse_cached_link"] = True

                    extra = account.get_extra()
                    cached = extra.get("chatgpt_last_payment_link") if isinstance(extra.get("chatgpt_last_payment_link"), dict) else {}
                    cached_url = str(cached.get("url") or "").strip()
                    if skip_existing and not force_refresh and payment_link_cache_matches(cached, params):
                        if cached_url:
                            if str(account.cashier_url or "").strip() != cached_url:
                                account.cashier_url = cached_url
                                account.updated_at = datetime.now(timezone.utc)
                                session.add(account)
                                session.commit()
                            _task_store.add_cashier_url(task_id, cached_url)
                        skipped_count += 1
                        _log(task_id, f"[SKIP] 已有可复用订阅链接: {email or account_id}")
                        continue

                    proxy_label = "直连" if not str(params.get("proxy") or "").strip() else "指定代理"
                    _log(
                        task_id,
                        "[订阅链接] "
                        f"{index}/{total} {email or account_id} "
                        f"plan={params.get('plan')} country={params.get('country')} "
                        f"currency={params.get('currency')} proxy={proxy_label}",
                    )
                    result = _execute_platform_action(
                        instance,
                        "chatgpt",
                        account,
                        "payment_link",
                        params,
                        session,
                    )
                    ok = bool(result.get("ok"))
                    data = result.get("data") if isinstance(result.get("data"), dict) else {}
                    checkout_url = str(data.get("url") or data.get("checkout_url") or data.get("cashier_url") or "").strip()
                    if not ok or not checkout_url:
                        session.rollback()
                        raise ValueError(str(result.get("error") or data.get("message") or "订阅链接生成失败"))

                    session.commit()
                    _task_store.add_cashier_url(task_id, checkout_url)
                    success_count += 1
                    if data.get("cache_reused"):
                        _log(task_id, f"[OK] 已复用缓存订阅链接: {email or account_id}")
                    else:
                        _log(task_id, f"[OK] 订阅链接已生成并保存: {email or account_id}")
            except SkipCurrentAttemptRequested as exc:
                skipped_count += 1
                _log(task_id, f"[SKIP] 已跳过订阅链接生成: {email or account_id} - {exc}")
            except StopTaskRequested:
                raise
            except Exception as exc:
                error_text = str(exc or "订阅链接生成失败")
                errors.append(f"{email or account_id}: {error_text}")
                _log(task_id, f"[FAIL] 订阅链接生成失败: {email or account_id} - {error_text}")
            finally:
                control.finish_attempt(attempt_id)
                _task_store.set_progress(task_id, f"{index}/{total}")

        final_status = "done" if not errors else "failed"
        summary_message = (
            f"批量订阅链接完成: 成功 {success_count} 个，跳过 {skipped_count + len(skipped_items)} 个，失败 {len(errors)} 个"
        )
        _log(task_id, f"[SUMMARY] {summary_message}")
        log_status = "success" if not errors else "failed"
        _save_task_log(
            "chatgpt",
            primary_email,
            log_status,
            error="" if log_status == "success" else summary_message,
            detail=_build_task_log_detail(
                task_id,
                {
                    "email": primary_email,
                    "attempt_outcome": "batch_payment_link_success" if log_status == "success" else "batch_payment_link_failed",
                    "source": "batch_payment_link",
                    "meta": {
                        **meta,
                        "runtime_success": success_count,
                        "runtime_skipped": skipped_count,
                        "runtime_errors": errors,
                    },
                },
            ),
        )
        _task_store.finish(
            task_id,
            status=final_status,
            success=success_count,
            skipped=skipped_count + len(skipped_items),
            errors=errors,
            error="" if not errors else summary_message,
        )
    except StopTaskRequested as exc:
        _log(task_id, f"[STOP] {exc}")
        _save_task_log(
            "chatgpt",
            primary_email,
            "stopped",
            error=str(exc),
            detail=_build_task_log_detail(
                task_id,
                {
                    "email": primary_email,
                    "attempt_outcome": "batch_payment_link_stopped",
                    "source": "batch_payment_link",
                    "meta": {
                        **meta,
                        "runtime_success": success_count,
                        "runtime_skipped": skipped_count,
                        "runtime_errors": errors,
                    },
                },
            ),
        )
        _task_store.finish(
            task_id,
            status="stopped",
            success=success_count,
            skipped=skipped_count + len(skipped_items),
            errors=errors,
            error=str(exc),
        )
    finally:
        _task_store.cleanup()


def _run_batch_resume_subscription_auth(task_id: str, account_ids: list[int]):
    from api.actions import (
        _apply_chatgpt_resume_auth_result,
        _execute_chatgpt_resume_subscription_auth,
    )

    control = _task_store.control_for(task_id)
    total = max(len(account_ids), 1)
    success_count = 0
    skipped_count = 0
    errors: list[str] = []
    primary_email = ""

    _task_store.mark_running(task_id)
    _task_store.set_progress(task_id, f"0/{total}")

    meta = dict(_task_store.snapshot(task_id).get("meta") or {})
    skipped_items = list(meta.get("skipped_items") or [])
    missing_ids = list(meta.get("missing_ids") or [])

    try:
        for missing_id in missing_ids:
            _log(task_id, f"[MISS] 账号不存在: account_id={missing_id}")
            errors.append(f"account_id={missing_id}: 账号不存在")

        for index, account_id in enumerate(account_ids, start=1):
            control.checkpoint(consume_skip=False)
            email = ""
            attempt_id = control.start_attempt()
            try:
                with Session(engine) as session:
                    account = session.get(AccountModel, int(account_id or 0))
                    if account is None or account.platform != "chatgpt":
                        raise ValueError("ChatGPT 账号不存在")
                    email = str(account.email or "")
                    if not primary_email:
                        primary_email = email

                _log(task_id, f"[补抓] -------- 账号 {index}/{total} | {email or account_id} --------")
                _log(task_id, f"[补抓] 开始补抓 Auth：{email or account_id}")
                control.checkpoint(attempt_id=attempt_id)
                with Session(engine) as session:
                    account = session.get(AccountModel, int(account_id or 0))
                    if account is None or account.platform != "chatgpt":
                        raise ValueError("ChatGPT 账号不存在")
                    result = _execute_chatgpt_resume_subscription_auth(
                        account,
                        log_fn=lambda message: _log(task_id, message),
                    )
                    session.refresh(account)
                    _apply_chatgpt_resume_auth_result(account, result, session)
                    if not bool(result.get("ok")):
                        data = result.get("data") if isinstance(result.get("data"), dict) else {}
                        raise ValueError(str(result.get("error") or data.get("message") or "补抓Auth失败"))
                success_count += 1
                _log(task_id, f"[OK] 补抓Auth完成: {email or account_id}")
            except SkipCurrentAttemptRequested as exc:
                skipped_count += 1
                _log(task_id, f"[SKIP] 已跳过补抓Auth: {email or account_id} - {exc}")
            except StopTaskRequested:
                raise
            except Exception as exc:
                error_text = str(exc or "补抓Auth失败")
                errors.append(f"{email or account_id}: {error_text}")
                _log(task_id, f"[FAIL] 补抓Auth失败: {email or account_id} - {error_text}")
            finally:
                control.finish_attempt(attempt_id)
                _task_store.set_progress(task_id, f"{index}/{total}")

        final_status = "done" if not errors else "failed"
        summary_message = (
            f"批量补抓Auth完成: 成功 {success_count} 个，跳过 {skipped_count + len(skipped_items)} 个，失败 {len(errors)} 个"
        )
        _log(task_id, f"[SUMMARY] {summary_message}")
        log_status = "success" if not errors else "failed"
        _save_task_log(
            "chatgpt",
            primary_email,
            log_status,
            error="" if log_status == "success" else summary_message,
            detail=_build_task_log_detail(
                task_id,
                {
                    "email": primary_email,
                    "attempt_outcome": "batch_resume_subscription_auth_success" if log_status == "success" else "batch_resume_subscription_auth_failed",
                    "source": "batch_resume_subscription_auth",
                    "meta": {
                        **meta,
                        "runtime_success": success_count,
                        "runtime_skipped": skipped_count,
                        "runtime_errors": errors,
                    },
                },
            ),
        )
        _task_store.finish(
            task_id,
            status=final_status,
            success=success_count,
            skipped=skipped_count + len(skipped_items),
            errors=errors,
            error="" if not errors else summary_message,
        )
    except StopTaskRequested as exc:
        _log(task_id, f"[STOP] {exc}")
        _save_task_log(
            "chatgpt",
            primary_email,
            "stopped",
            error=str(exc),
            detail=_build_task_log_detail(
                task_id,
                {
                    "email": primary_email,
                    "attempt_outcome": "batch_resume_subscription_auth_stopped",
                    "source": "batch_resume_subscription_auth",
                    "meta": {
                        **meta,
                        "runtime_success": success_count,
                        "runtime_skipped": skipped_count,
                        "runtime_errors": errors,
                    },
                },
            ),
        )
        _task_store.finish(
            task_id,
            status="stopped",
            success=success_count,
            skipped=skipped_count + len(skipped_items),
            errors=errors,
            error=str(exc),
        )
    finally:
        _task_store.cleanup()


def _run_register(task_id: str, req: RegisterTaskRequest):
    from core.base_platform import RegisterConfig
    from core.db import save_account
    from core.base_mailbox import create_mailbox
    from core.proxy_utils import normalize_proxy_url
    from services.chatgpt_account_state import classify_chatgpt_capabilities
    from services.chatgpt_core import ChatGPTPlatform

    control = _task_store.control_for(task_id)
    _task_store.mark_running(task_id)
    success = 0
    skipped = 0
    errors = []
    start_gate_lock = threading.Lock()
    next_start_time = time.time()

    def _sleep_with_control(
        wait_seconds: float,
        *,
        attempt_id: int | None = None,
    ) -> None:
        remaining = max(float(wait_seconds or 0), 0.0)
        while remaining > 0:
            control.checkpoint(attempt_id=attempt_id)
            chunk = min(0.25, remaining)
            time.sleep(chunk)
            remaining -= chunk

    try:
        if req.platform != "chatgpt":
            raise RuntimeError(f"不支持的平台: {req.platform}")
        PlatformCls = ChatGPTPlatform

        initial_merged_extra = _build_effective_register_extra(req)
        deferred_activation_enabled = _should_precheck_chatgpt_deferred_invite(req, initial_merged_extra)
        chatgpt_zero_amount_stop_enabled = (
            req.platform == "chatgpt"
            and _is_truthy(initial_merged_extra.get("chatgpt_access_token_only_zero_amount_stop_enabled"))
        )
        chatgpt_zero_amount_stop_threshold = _parse_positive_int(
            initial_merged_extra.get("chatgpt_access_token_only_zero_amount_stop_threshold"),
            default=1,
        )
        pending_invite_ids: list[int] = []
        pending_invite_lock = threading.Lock()
        registration_success = 0
        chatgpt_checkout_amount_zero = 0
        chatgpt_checkout_amount_nonzero = 0
        chatgpt_zero_amount_stop_triggered = False
        chatgpt_checkout_amount_lock = threading.Lock()
        deferred_team_ids: list[int] = []
        deferred_team_ids_lock = threading.Lock()
        deferred_phase_close_reason = ""
        deferred_phase_close_marker = "__deferred_invite_phase_close__"
        registration_phase_close_event = threading.Event()
        if deferred_activation_enabled:
            ok, check_message = _check_chatgpt_deferred_invite_availability(
                req,
                merged_extra=initial_merged_extra,
                log_fn=lambda message: _log(task_id, message),
            )
            if isinstance(initial_merged_extra.get("chatgpt_deferred_invite_team_ids"), list):
                deferred_team_ids = [
                    int(team_id)
                    for team_id in initial_merged_extra.get("chatgpt_deferred_invite_team_ids")
                    if str(team_id).strip().isdigit() and int(team_id) > 0
                ]
                if deferred_team_ids:
                    req.extra["chatgpt_deferred_invite_team_ids"] = list(deferred_team_ids)
                    req.extra["chatgpt_deferred_invite_team_id"] = deferred_team_ids[0]
            if ok and check_message:
                _log(task_id, f"[邀请] {check_message}")
            if not ok:
                _log(task_id, f"[邀请] {check_message}")
                _task_store.finish(
                    task_id,
                    status="failed",
                    success=success,
                    skipped=skipped,
                    errors=[check_message],
                    error=check_message,
                )
                _task_store.cleanup()
                return

        def _build_mailbox(proxy: Optional[str]):
            merged_extra = _build_effective_register_extra(req)
            return create_mailbox(
                provider=merged_extra.get("mail_provider", "luckmail"),
                extra=merged_extra,
                proxy=proxy,
            )

        def _do_one(i: int):
            nonlocal next_start_time, deferred_phase_close_reason
            nonlocal chatgpt_checkout_amount_zero, chatgpt_checkout_amount_nonzero
            nonlocal chatgpt_zero_amount_stop_triggered
            current_email = req.email or ""
            attempt_id: int | None = None
            try:
                from core.proxy_utils import (
                    is_proxy_error_text,
                    iter_enabled_runtime_proxies,
                    resolve_runtime_proxy_with_metadata,
                )

                control.checkpoint()
                attempt_id = control.start_attempt()
                control.checkpoint(attempt_id=attempt_id)
                if req.register_delay_seconds > 0:
                    with start_gate_lock:
                        control.checkpoint(attempt_id=attempt_id)
                        now = time.time()
                        wait_seconds = max(0.0, next_start_time - now)
                        if wait_seconds > 0:
                            _log(
                                task_id,
                                f"第 {i + 1} 个账号启动前延迟 {wait_seconds:g} 秒",
                            )
                            _sleep_with_control(
                                wait_seconds,
                                attempt_id=attempt_id,
                            )
                        next_start_time = time.time() + req.register_delay_seconds
                control.checkpoint(attempt_id=attempt_id)
                if registration_phase_close_event.is_set():
                    return AttemptResult.stopped(
                        f"{deferred_phase_close_marker}{deferred_phase_close_reason or '当前所有可用 team 都已不可邀请，结束注册阶段并进入激活阶段'}"
                    )
                candidate_proxies: list[tuple[str, object | None, str]] = [("", None, "direct")]

                merged_extra = _build_effective_register_extra(req)
                if deferred_activation_enabled:
                    with deferred_team_ids_lock:
                        current_deferred_team_ids = list(deferred_team_ids)
                    if not current_deferred_team_ids:
                        return AttemptResult.stopped(
                            f"{deferred_phase_close_marker}当前所有可用 team 都已不可邀请，结束注册阶段并进入激活阶段"
                        )
                    merged_extra["chatgpt_deferred_invite_team_ids"] = current_deferred_team_ids
                    merged_extra["chatgpt_deferred_invite_team_id"] = current_deferred_team_ids[0]

                target_successes = max(int(req.count or 1), 1)
                _task_store.set_progress(task_id, f"{success}/{target_successes}")
                _log(task_id, f"[账号] -------- 尝试 {i + 1} / 目标成功 {target_successes} --------")
                _log(task_id, f"开始第 {i + 1} 次尝试，目标成功数 {target_successes}")

                last_proxy_error = ""
                last_proxy_error_email = current_email
                for proxy_index, (candidate_proxy, candidate_proxy_pool, candidate_proxy_source) in enumerate(candidate_proxies, start=1):
                    _proxy = candidate_proxy
                    proxy_pool = candidate_proxy_pool
                    proxy_source = candidate_proxy_source

                    try:
                        _config = RegisterConfig(
                            executor_type=req.executor_type,
                            captcha_solver=req.captcha_solver,
                            proxy=_proxy,
                            extra=merged_extra,
                        )
                        _mailbox = _build_mailbox(_proxy)
                        _platform = PlatformCls(config=_config, mailbox=_mailbox)
                        _platform._task_attempt_token = attempt_id
                        _platform._log_fn = lambda msg: _log(task_id, msg)
                        _platform.bind_task_control(control)
                        if getattr(_platform, "mailbox", None) is not None:
                            _platform.mailbox._task_attempt_token = attempt_id
                            _platform.mailbox._log_fn = _platform._log_fn
                        account = _platform.register(
                            email=req.email or None,
                            password=req.password,
                        )
                        break
                    except SkipCurrentAttemptRequested:
                        raise
                    except StopTaskRequested:
                        raise
                    except Exception as proxy_exc:
                        current_email = current_email or req.email or ""
                        error_text = str(proxy_exc or "").strip()
                        if _proxy and proxy_pool is not None and proxy_source == "pool":
                            proxy_pool.report_fail(_proxy)
                        if is_proxy_error_text(error_text) and proxy_index < len(candidate_proxies):
                            last_proxy_error = error_text
                            last_proxy_error_email = current_email
                            _log(task_id, f"[代理] 当前代理失败，切换下一个代理: {error_text}")
                            continue
                        raise
                else:
                    final_error = last_proxy_error or "所有代理尝试失败"
                    raise RuntimeError(final_error)

                current_email = account.email or current_email
                if isinstance(account.extra, dict):
                    mail_provider = merged_extra.get("mail_provider", "")
                    if mail_provider:
                        account.extra.setdefault("mail_provider", mail_provider)
                    if req.platform == "chatgpt":
                        for key in (
                            "chatgpt_enable_team_invite",
                            "chatgpt_team_invite_deferred_activation",
                            "chatgpt_capture_free_workspace",
                            "chatgpt_capture_business_workspace",
                            "chatgpt_existing_account_capture",
                            "chatgpt_deferred_invite_team_id",
                            "chatgpt_deferred_invite_team_ids",
                        ):
                            if key in merged_extra:
                                account.extra[key] = merged_extra.get(key)
                    if mail_provider == "luckmail" and req.platform == "chatgpt":
                        mailbox_token = getattr(_mailbox, "_token", "") or ""
                        if mailbox_token:
                            account.extra.setdefault("mailbox_token", mailbox_token)
                        if merged_extra.get("luckmail_project_code"):
                            account.extra.setdefault(
                                "luckmail_project_code",
                                merged_extra.get("luckmail_project_code"),
                            )
                        if merged_extra.get("luckmail_email_type"):
                            account.extra.setdefault(
                                "luckmail_email_type",
                                merged_extra.get("luckmail_email_type"),
                            )
                        if merged_extra.get("luckmail_domain"):
                            account.extra.setdefault(
                                "luckmail_domain", merged_extra.get("luckmail_domain")
                            )
                        if merged_extra.get("luckmail_base_url"):
                            account.extra.setdefault(
                                "luckmail_base_url",
                                merged_extra.get("luckmail_base_url"),
                            )
                additional_accounts_payload = []
                saved_linked_accounts = []
                if isinstance(account.extra, dict):
                    payload = account.extra.pop("_linked_accounts_to_save", None)
                    if isinstance(payload, list):
                        additional_accounts_payload = [item for item in payload if isinstance(item, dict)]
                account_extra = account.extra if isinstance(account.extra, dict) else {}
                account_extra = _cache_chatgpt_checkout_link(
                    account_extra,
                    source="registration_checkout_probe",
                )
                if isinstance(account.extra, dict):
                    account.extra = account_extra
                if req.platform == "chatgpt" and isinstance(account.extra, dict):
                    account.extra["chatgpt_capabilities"] = classify_chatgpt_capabilities(account)
                skip_save_account = req.platform == "chatgpt" and _is_truthy(account_extra.get("chatgpt_skip_save_account"))
                checkout_amount_seen = "chatgpt_checkout_amount_is_zero" in account_extra
                checkout_amount_is_zero = bool(account_extra.get("chatgpt_checkout_amount_is_zero"))
                should_stop_after_current_account = False
                zero_amount_stop_reason = ""
                if req.platform == "chatgpt" and checkout_amount_seen:
                    with chatgpt_checkout_amount_lock:
                        if checkout_amount_is_zero:
                            chatgpt_checkout_amount_zero += 1
                            if (
                                chatgpt_zero_amount_stop_enabled
                                and not chatgpt_zero_amount_stop_triggered
                                and chatgpt_checkout_amount_zero >= chatgpt_zero_amount_stop_threshold
                            ):
                                chatgpt_zero_amount_stop_triggered = True
                                should_stop_after_current_account = True
                                zero_amount_stop_reason = (
                                    "Plus checkout amount=0 命中阈值 "
                                    f"{chatgpt_checkout_amount_zero}/{chatgpt_zero_amount_stop_threshold}，"
                                    "已停止后续注册"
                                )
                        else:
                            chatgpt_checkout_amount_nonzero += 1
                if skip_save_account:
                    skip_reason = str(account_extra.get("chatgpt_skip_save_reason") or "Plus checkout amount != 0").strip()
                    amount_text = str(account_extra.get("chatgpt_checkout_amount") or account_extra.get("chatgpt_checkout_amount_raw") or "").strip()
                    currency_text = str(account_extra.get("chatgpt_checkout_currency") or "").strip()
                    checkout_url = str(account_extra.get("chatgpt_checkout_url") or account_extra.get("cashier_url") or "").strip()
                    _log(
                        task_id,
                        f"[SKIP_SAVE] 注册成功但不保存账号: {account.email} reason={skip_reason}",
                    )
                    if amount_text or currency_text:
                        _log(task_id, f"[SKIP_SAVE] Plus checkout amount={amount_text or 'unknown'} currency={currency_text or 'unknown'}")
                    if checkout_url:
                        _log(task_id, f"  [升级链接] {checkout_url}")
                        _task_store.add_cashier_url(task_id, checkout_url)
                    if should_stop_after_current_account and zero_amount_stop_reason:
                        control.request_stop()
                        _log(task_id, f"[STOP] {zero_amount_stop_reason}")
                    _save_task_log(
                        req.platform,
                        account.email,
                        "success",
                        detail=_build_task_log_detail(
                            task_id,
                            {
                                "attempt_outcome": "success_skip_save",
                                "email": account.email,
                                "skip_save_reason": skip_reason,
                                "chatgpt_checkout_amount": amount_text,
                                "chatgpt_checkout_currency": currency_text,
                                "chatgpt_checkout_amount_is_zero": checkout_amount_is_zero,
                                "chatgpt_checkout_url": checkout_url,
                                "chatgpt_zero_amount_stop_enabled": chatgpt_zero_amount_stop_enabled,
                                "chatgpt_zero_amount_stop_threshold": chatgpt_zero_amount_stop_threshold,
                                "chatgpt_zero_amount_stop_triggered": should_stop_after_current_account,
                                "chatgpt_zero_amount_stop_reason": zero_amount_stop_reason,
                            },
                        ),
                    )
                    return AttemptResult.success()
                saved_account = save_account(account)
                pending_invite = None
                if req.platform == "chatgpt" and saved_account is not None:
                    try:
                        from services.chatgpt_core.pending_business_invites import upsert_pending_invite_from_account

                        pending_invite = upsert_pending_invite_from_account(saved_account)
                        if pending_invite is not None:
                            pending_payload = {}
                            if isinstance(saved_account.get_extra(), dict):
                                pending_payload = dict(saved_account.get_extra().get("chatgpt_pending_business_invite") or {})
                            exhausted_team_ids = []
                            for raw in (pending_payload.get("exhausted_team_ids") or []):
                                try:
                                    exhausted_team_id = int(raw)
                                except Exception:
                                    exhausted_team_id = 0
                                if exhausted_team_id > 0 and exhausted_team_id not in exhausted_team_ids:
                                    exhausted_team_ids.append(exhausted_team_id)
                            if deferred_activation_enabled and exhausted_team_ids:
                                with deferred_team_ids_lock:
                                    before_team_ids = list(deferred_team_ids)
                                    deferred_team_ids[:] = [team_id for team_id in deferred_team_ids if team_id not in exhausted_team_ids]
                                    req.extra["chatgpt_deferred_invite_team_ids"] = list(deferred_team_ids)
                                    if deferred_team_ids:
                                        req.extra["chatgpt_deferred_invite_team_id"] = deferred_team_ids[0]
                                    else:
                                        req.extra.pop("chatgpt_deferred_invite_team_id", None)
                                remaining_text = ",".join(str(team_id) for team_id in deferred_team_ids) or "无"
                                removed_text = ",".join(str(team_id) for team_id in exhausted_team_ids)
                                if before_team_ids != deferred_team_ids:
                                    _log(task_id, f"[邀请] team_id={removed_text} 已不可邀请，剩余 team_id={remaining_text}")
                            _log(task_id, f"[邀请] 已保存 pending invite id={pending_invite.id} team={pending_invite.team_id}")
                            if deferred_activation_enabled:
                                with pending_invite_lock:
                                    pending_invite_ids.append(int(pending_invite.id))
                    except Exception as pending_exc:
                        _log(task_id, f"[邀请] 保存 pending invite 失败: {pending_exc}")
                if additional_accounts_payload:
                    from core.base_platform import Account, AccountStatus

                    for extra_account_payload in additional_accounts_payload:
                        try:
                            linked_account = Account(
                                platform=str(extra_account_payload.get("platform") or req.platform),
                                email=str(extra_account_payload.get("email") or account.email or ""),
                                password=str(extra_account_payload.get("password") or account.password or ""),
                                user_id=str(extra_account_payload.get("user_id") or ""),
                                region=str(extra_account_payload.get("region") or ""),
                                token=str(extra_account_payload.get("token") or ""),
                                status=AccountStatus(str(extra_account_payload.get("status") or "registered")),
                                extra=dict(extra_account_payload.get("extra") or {}),
                            )
                            if linked_account.platform == "chatgpt" and isinstance(linked_account.extra, dict):
                                linked_account.extra["chatgpt_capabilities"] = classify_chatgpt_capabilities(linked_account)
                            saved_linked = save_account(linked_account)
                            if saved_linked is not None:
                                saved_linked_accounts.append(saved_linked)
                            if isinstance(linked_account.extra, dict):
                                scope_label = str(linked_account.extra.get("chatgpt_workspace_label") or "").strip()
                                if scope_label:
                                    _log(task_id, f"[OK] 已保存附加工作空间: {linked_account.email} [{scope_label}]")
                        except Exception as save_exc:
                            _log(task_id, f"[WARN] 保存附加工作空间失败: {save_exc}")
                if _proxy and proxy_pool is not None and proxy_source == "pool":
                    proxy_pool.report_success(_proxy)
                if deferred_activation_enabled and pending_invite is not None:
                    if should_stop_after_current_account and zero_amount_stop_reason:
                        control.request_stop()
                        _log(task_id, f"[STOP] {zero_amount_stop_reason}")
                    _log(task_id, f"[OK] 注册完成并已发送邀请: {account.email}")
                    _save_task_log(
                        req.platform,
                        account.email,
                        "pending_activation",
                        detail=_build_task_log_detail(
                            task_id,
                            {
                                "attempt_outcome": "invite_saved_pending_activation",
                                "email": account.email,
                                "pending_invite_id": int(pending_invite.id or 0),
                                "chatgpt_zero_amount_stop_enabled": chatgpt_zero_amount_stop_enabled,
                                "chatgpt_zero_amount_stop_threshold": chatgpt_zero_amount_stop_threshold,
                                "chatgpt_zero_amount_stop_triggered": should_stop_after_current_account,
                                "chatgpt_zero_amount_stop_reason": zero_amount_stop_reason,
                            },
                        ),
                    )
                    return AttemptResult.success()
                if should_stop_after_current_account and zero_amount_stop_reason:
                    control.request_stop()
                    _log(task_id, f"[STOP] {zero_amount_stop_reason}")
                _log(task_id, f"[OK] 注册成功: {account.email}")
                _auto_upload_integrations(task_id, saved_account or account)
                for linked_saved_account in saved_linked_accounts:
                    _auto_upload_integrations(task_id, linked_saved_account)
                cashier_url = (account.extra or {}).get("cashier_url", "")
                if cashier_url:
                    _log(task_id, f"  [升级链接] {cashier_url}")
                    _task_store.add_cashier_url(task_id, cashier_url)
                _save_task_log(
                    req.platform,
                    account.email,
                    "success",
                    detail=_build_task_log_detail(
                        task_id,
                        {
                            "attempt_outcome": "success",
                            "email": account.email,
                            "chatgpt_zero_amount_stop_enabled": chatgpt_zero_amount_stop_enabled,
                            "chatgpt_zero_amount_stop_threshold": chatgpt_zero_amount_stop_threshold,
                            "chatgpt_zero_amount_stop_triggered": should_stop_after_current_account,
                            "chatgpt_zero_amount_stop_reason": zero_amount_stop_reason,
                        },
                    ),
                )
                return AttemptResult.success()
            except SkipCurrentAttemptRequested as e:
                _log(task_id, f"[SKIP] 已跳过当前账号: {e}")
                _save_task_log(
                    req.platform,
                    current_email,
                    "skipped",
                    error=str(e),
                    detail=_build_task_log_detail(
                        task_id,
                        {
                            "attempt_outcome": "skipped",
                            "email": current_email,
                        },
                    ),
                )
                return AttemptResult.skipped(str(e))
            except StopTaskRequested as e:
                _log(task_id, f"[STOP] {e}")
                return AttemptResult.stopped(str(e))
            except Exception as e:
                error_text = str(e)
                if deferred_activation_enabled and (
                    "所有可用 team 都已不可邀请" in error_text
                    or "预选 team_id 列表当前不可用" in error_text
                ):
                    registration_phase_close_event.set()
                    deferred_phase_close_reason = error_text
                    _log(task_id, f"[邀请] {error_text}")
                    _save_task_log(
                        req.platform,
                        current_email,
                        "failed",
                        error=error_text,
                        detail=_build_task_log_detail(
                            task_id,
                            {
                                "attempt_outcome": "invite_exhausted_stop_phase",
                                "email": current_email,
                            },
                        ),
                    )
                    return AttemptResult.stopped(f"{deferred_phase_close_marker}{error_text}")
                _log(task_id, f"[FAIL] 注册失败: {e}")
                _save_task_log(
                    req.platform,
                    current_email,
                    "failed",
                    error=error_text,
                    detail=_build_task_log_detail(
                        task_id,
                        {
                            "attempt_outcome": "failed",
                            "email": current_email,
                        },
                    ),
                )
                return AttemptResult.failed(error_text)
            finally:
                control.finish_attempt(attempt_id)

        from concurrent.futures import FIRST_COMPLETED, CancelledError, ThreadPoolExecutor, wait

        target_successes = max(int(req.count or 1), 1)
        max_workers = min(req.concurrency, target_successes, 5)
        stopped = False
        registration_phase_closed_for_activation = False
        next_attempt_index = 0
        in_flight: dict[Any, int] = {}

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            while (
                next_attempt_index < target_successes
                and len(in_flight) < max_workers
                and not stopped
                and not registration_phase_closed_for_activation
                and not control.is_stop_requested()
            ):
                future = pool.submit(_do_one, next_attempt_index)
                in_flight[future] = next_attempt_index
                next_attempt_index += 1

            while in_flight:
                done, _ = wait(tuple(in_flight.keys()), return_when=FIRST_COMPLETED)
                for f in done:
                    attempt_no = in_flight.pop(f, None)
                    try:
                        result = f.result()
                    except CancelledError:
                        continue
                    except Exception as e:
                        _log(task_id, f"[ERROR] 任务线程异常: {e}")
                        errors.append(str(e))
                        result = AttemptResult.failed(str(e))

                    if result.outcome == AttemptOutcome.SUCCESS:
                        success += 1
                        registration_success += 1
                    elif result.outcome == AttemptOutcome.SKIPPED:
                        skipped += 1
                    elif result.outcome == AttemptOutcome.STOPPED:
                        if result.message.startswith(deferred_phase_close_marker):
                            registration_phase_closed_for_activation = True
                            deferred_phase_close_reason = result.message[len(deferred_phase_close_marker):].strip()
                            if deferred_phase_close_reason:
                                errors.append(deferred_phase_close_reason)
                        else:
                            stopped = True
                    else:
                        errors.append(result.message)

                    _task_store.set_progress(task_id, f"{success}/{target_successes}")

                    if success >= target_successes:
                        stopped = True
                        control.request_stop()
                        for pending in list(in_flight.keys()):
                            pending.cancel()
                        in_flight.clear()
                        break

                    if registration_phase_closed_for_activation or stopped or control.is_stop_requested():
                        for pending in list(in_flight.keys()):
                            pending.cancel()
                        in_flight.clear()
                        break

                    if success < target_successes:
                        future = pool.submit(_do_one, next_attempt_index)
                        in_flight[future] = next_attempt_index
                        next_attempt_index += 1

        if deferred_activation_enabled and (registration_phase_closed_for_activation or not (control.is_stop_requested() or stopped)):
            from core.db import AccountModel
            from services.chatgpt_core.pending_business_invites import activate_pending_invites

            pending_ids = list(pending_invite_ids)
            if registration_phase_closed_for_activation and deferred_phase_close_reason:
                _log(task_id, f"[邀请] {deferred_phase_close_reason}")
            _log(task_id, "[阶段] ================ 统一激活阶段 ================")
            _log(task_id, f"[激活] 注册/邀请阶段完成，准备统一激活 {len(pending_ids)} 个账号")

            def _upload_activation_accounts(item: dict[str, Any]) -> None:
                local_account_id = int(item.get("local_account_id") or 0)
                linked_account_ids = [int(x) for x in item.get("linked_account_ids", []) if x]
                email = str(item.get("email") or "")
                upload_ids = [acc_id for acc_id in [local_account_id] + linked_account_ids if acc_id > 0]

                for index, acc_id in enumerate(upload_ids, start=1):
                    with Session(engine) as s:
                        account_to_upload = s.get(AccountModel, acc_id)
                    if account_to_upload is None:
                        _log(task_id, f"[激活上传] {email} {index}/{len(upload_ids)} 跳过：本地账号不存在 id={acc_id}")
                        continue
                    _log(task_id, f"[激活上传] {email} {index}/{len(upload_ids)} 开始上传")
                    _auto_upload_integrations(task_id, account_to_upload)

            activation_result = activate_pending_invites(
                invite_ids=pending_ids,
                log_fn=lambda message: _log(task_id, message),
                on_success=_upload_activation_accounts,
            )
            success = int(activation_result.get("success") or 0)
            failed_items = [item for item in (activation_result.get("errors") or []) if isinstance(item, dict)]
            for item in failed_items:
                email = str(item.get("email") or "") or f"invite#{item.get('invite_id') or '-'}"
                error_message = str(item.get("error") or "激活失败")
                errors.append(error_message)
                _save_task_log(
                    req.platform,
                    email,
                    "failed",
                    error=error_message,
                    detail=_build_task_log_detail(
                        task_id,
                        {
                            "attempt_outcome": "activation_failed",
                            "email": email,
                            "invite_id": int(item.get("invite_id") or 0),
                        },
                    ),
                )
            for item in [entry for entry in (activation_result.get("results") or []) if isinstance(entry, dict)]:
                local_account_id = int(item.get("local_account_id") or 0)
                linked_account_ids = [int(x) for x in item.get("linked_account_ids", []) if x]
                email = str(item.get("email") or "")
                upload_ids = [acc_id for acc_id in [local_account_id] + linked_account_ids if acc_id > 0]

                _save_task_log(
                    req.platform,
                    email,
                    "success",
                    detail=_build_task_log_detail(
                        task_id,
                        {
                            "attempt_outcome": "activation_success",
                            "email": email,
                            "invite_id": int(item.get("invite_id") or 0),
                            "uploaded_count": len([x for x in upload_ids if x > 0]),
                        },
                    ),
                )
            _log(task_id, f"[激活] 统一激活完成：成功 {success} / {len(pending_ids)}")
    except Exception as e:
        _log(task_id, f"致命错误: {e}")
        _task_store.finish(
            task_id,
            status="failed",
            success=success,
            skipped=skipped,
            errors=errors,
            error=str(e),
        )
        _task_store.cleanup()
        return

    final_status = "stopped" if control.is_stop_requested() or stopped else "done"
    if deferred_activation_enabled:
        if final_status == "stopped":
            summary = (
                f"任务已停止: 注册完成 {registration_success} 个, 激活成功 {success} 个, 跳过 {skipped} 个, 失败 {len(errors)} 个"
            )
        else:
            summary = f"完成: 注册完成 {registration_success} 个, 激活成功 {success} 个, 跳过 {skipped} 个, 失败 {len(errors)} 个"
    elif final_status == "stopped":
        summary = (
            f"任务已停止: 成功 {success} 个, 跳过 {skipped} 个, 失败 {len(errors)} 个"
        )
    else:
        summary = f"完成: 成功 {success} 个, 跳过 {skipped} 个, 失败 {len(errors)} 个"
    if req.platform == "chatgpt" and (chatgpt_checkout_amount_zero + chatgpt_checkout_amount_nonzero) > 0:
        summary = (
            f"{summary}; Plus checkout amount=0: {chatgpt_checkout_amount_zero} 个, "
            f"amount!=0: {chatgpt_checkout_amount_nonzero} 个"
        )
        if chatgpt_zero_amount_stop_enabled:
            summary = (
                f"{summary}; zero-stop={'触发' if chatgpt_zero_amount_stop_triggered else '未触发'} "
                f"threshold={chatgpt_zero_amount_stop_threshold}"
            )
    _log(task_id, summary)
    _task_store.finish(
        task_id,
        status=final_status,
        success=success,
        skipped=skipped,
        errors=errors,
    )
    _task_store.cleanup()


@router.post("/register")
def create_register_task(
    req: RegisterTaskRequest,
    background_tasks: BackgroundTasks,
):
    task_id = enqueue_register_task(req, background_tasks=background_tasks)
    return {"task_id": task_id}


@router.post("/chatgpt/resume-subscription-auth")
def create_resume_subscription_auth_task(
    req: ResumeSubscriptionAuthTaskRequest,
    background_tasks: BackgroundTasks,
):
    task_id = enqueue_resume_subscription_auth_task(
        int(req.account_id or 0),
        background_tasks=background_tasks,
    )
    return {"task_id": task_id}


@router.post("/chatgpt/resume-subscription-auth/batch")
def create_batch_resume_subscription_auth_task(
    req: BatchResumeSubscriptionAuthTaskRequest,
    background_tasks: BackgroundTasks,
):
    return enqueue_batch_resume_subscription_auth_task(
        req,
        background_tasks=background_tasks,
    )


@router.post("/chatgpt/payment-links/batch")
def create_batch_payment_link_task(
    req: BatchPaymentLinkTaskRequest,
    background_tasks: BackgroundTasks,
):
    return enqueue_batch_payment_link_task(
        req,
        background_tasks=background_tasks,
    )


@router.post("/{task_id}/submit-verification")
def submit_verification(task_id: str, body: SubmitVerificationRequest):
    _ensure_task_mutable(task_id)
    control = _task_store.control_for(task_id)
    try:
        challenge = control.submit_verification(
            challenge_id=body.challenge_id,
            code=body.code,
        )
    except KeyError as e:
        raise HTTPException(404, e.args[0] if e.args else "验证码挑战不存在")
    except ValueError as e:
        raise HTTPException(400, str(e))

    _log(
        task_id,
        "收到人工验证码提交: "
        f"phase={challenge.get('phase')} email={challenge.get('email')}",
    )
    return {"ok": True, "task_id": task_id, "challenge": challenge}


@router.post("/{task_id}/skip-current")
def skip_current_account(task_id: str):
    _ensure_task_mutable(task_id)
    control = _task_store.request_skip_current(task_id)
    _log(task_id, "收到手动跳过当前账号请求")
    return {"ok": True, "task_id": task_id, "control": control}


@router.post("/{task_id}/stop")
def stop_task(task_id: str):
    _ensure_task_mutable(task_id)
    control = _task_store.request_stop(task_id)
    _log(task_id, "收到手动停止任务请求")
    return {"ok": True, "task_id": task_id, "control": control}


@router.get("/logs")
def get_logs(platform: str = None, page: int = 1, page_size: int = 50):
    with Session(engine) as s:
        q = select(TaskLog)
        if platform:
            q = q.where(TaskLog.platform == platform)
        q = q.order_by(TaskLog.id.desc())
        all_items = s.exec(q).all()
    grouped_items = _group_latest_task_logs(all_items)
    total = len(grouped_items)
    start = max(page - 1, 0) * page_size
    items = grouped_items[start:start + page_size]
    return {"total": total, "items": [_task_log_summary(item) for item in items]}


@router.get("/logs/{log_id}")
def get_log_detail(log_id: int):
    with Session(engine) as s:
        log = s.get(TaskLog, log_id)
        if log is None:
            raise HTTPException(404, "任务历史不存在")
    return _task_log_detail_payload(log)


@router.post("/logs/batch-delete")
def batch_delete_logs(body: TaskLogBatchDeleteRequest):
    if not body.ids:
        raise HTTPException(400, "任务历史 ID 列表不能为空")

    unique_ids = list(dict.fromkeys(body.ids))
    if len(unique_ids) > 1000:
        raise HTTPException(400, "单次最多删除 1000 条任务历史")

    with Session(engine) as s:
        try:
            logs = s.exec(select(TaskLog).where(TaskLog.id.in_(unique_ids))).all()
            found_ids = {log.id for log in logs if log.id is not None}
            selected_task_ids = {
                str(_task_log_detail_dict(log).get("task_id") or "").strip()
                for log in logs
                if str(_task_log_detail_dict(log).get("task_id") or "").strip()
            }
            all_logs = s.exec(select(TaskLog)).all()
            logs_to_delete: list[TaskLog] = []
            for log in all_logs:
                detail = _task_log_detail_dict(log)
                task_id = str(detail.get("task_id") or "").strip()
                if task_id and task_id in selected_task_ids:
                    logs_to_delete.append(log)
                    continue
                if log.id in found_ids and not task_id:
                    logs_to_delete.append(log)

            for log in logs_to_delete:
                s.delete(log)

            s.commit()
            deleted_count = len(found_ids)
            not_found_ids = [log_id for log_id in unique_ids if log_id not in found_ids]
            logger.info("批量删除任务历史成功: %s 条", deleted_count)

            return {
                "deleted": deleted_count,
                "deleted_records": len(logs_to_delete),
                "not_found": not_found_ids,
                "total_requested": len(unique_ids),
            }
        except Exception as e:
            s.rollback()
            logger.exception("批量删除任务历史失败")
            raise HTTPException(500, f"批量删除任务历史失败: {str(e)}")


@router.get("/{task_id}/logs/stream")
async def stream_logs(task_id: str, since: int = 0):
    """SSE 实时日志流"""
    _ensure_task_exists(task_id)

    async def event_generator():
        sent = since
        while True:
            logs, status = _task_store.log_state(task_id)
            while sent < len(logs):
                yield f"data: {json.dumps({'line': logs[sent]})}\n\n"
                sent += 1
            if status in ("done", "failed", "stopped"):
                yield f"data: {json.dumps({'done': True, 'status': status})}\n\n"
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/active-summary")
def list_active_task_summaries():
    snapshots = _task_store.list_snapshots()
    active_items = []
    for item in snapshots:
        status = str(item.get("status") or "").strip().lower()
        if status not in {"pending", "running"}:
            continue
        meta = dict(item.get("meta") or {})
        source = str(item.get("source") or "").strip()
        progress = str(item.get("progress") or "").strip()
        if source == "gopay_payment" and progress == "1/1":
            continue
        active_items.append(
            {
                "id": item.get("id"),
                "status": item.get("status"),
                "platform": item.get("platform"),
                "source": source,
                "meta": meta,
                "progress": item.get("progress"),
                "success": item.get("success"),
                "skipped": item.get("skipped"),
                "error": item.get("error", ""),
                "control": dict(item.get("control") or {}),
                "pending_verification": item.get("pending_verification"),
            }
        )
    return active_items


@router.get("/{task_id}")
def get_task(task_id: str):
    _ensure_task_exists(task_id)
    return _task_store.snapshot(task_id)


@router.get("")
def list_tasks():
    return _task_store.list_snapshots()
