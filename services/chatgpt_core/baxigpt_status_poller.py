"""BaxiGPT 订单状态后台轮询器。

只负责把已拿到 order_id 的卡密订单自动查到终态，并同步回卡密池和绑定账号。
不会阻塞批量提交任务。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import threading
import time
from typing import Callable, Any

from sqlalchemy import func
from sqlmodel import Session, select

from core.db import AccountModel, engine
from services.chatgpt_core.baxigpt_cdk_repository import (
    IDEA_SUBMIT_MARKER_KEY,
    IDEA_SUBMIT_UNAVAILABLE_KEYS,
    STATUS_DISABLED,
    STATUS_FAILED,
    STATUS_PAID,
    STATUS_PROCESSING,
    STATUS_SUBMITTED,
    TERMINAL_STATUSES as REPOSITORY_TERMINAL_STATUSES,
    BaxiGptCdkRepository,
)
from services.chatgpt_core.baxigpt_client import BaxiGptClient
from services.chatgpt_core.local_status_refresh import (
    summarize_status_refresh,
    sync_chatgpt_account_local_status,
)


TERMINAL_STATUSES = {STATUS_PAID, STATUS_FAILED, STATUS_DISABLED}
POLLABLE_STATUSES = {STATUS_SUBMITTED, STATUS_PROCESSING}
RESTORE_WINDOW_SECONDS = 24 * 3600
DEFAULT_INTERVAL_SECONDS = 5
DEFAULT_TIMEOUT_SECONDS = 300
ACCOUNT_RECONCILE_INTERVAL_SECONDS = 60
ACCOUNT_RECONCILE_BATCH_SIZE = 50
ACCOUNT_RECONCILE_STALE_SECONDS = 120
ACCOUNT_RECONCILE_STATUSES = {STATUS_SUBMITTED, STATUS_PROCESSING, "pending", "polling"}
# Deliberately false: account-level recovery must be an explicit operator action,
# never a hidden workload that survives a stopped local task.
ACCOUNT_RECONCILE_AUTOMATIC = False
POLLING_STOPPED_STATUS = "stopped"
POLLING_STOPPED_REASON = "本地 Idea 任务已中断，已停止后续状态轮询"


@dataclass(slots=True)
class BaxiGptStatusPollTarget:
    record_id: int
    task_id: str = ""
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    next_due_at: float = 0
    deadline_at: float = 0
    last_status: str = ""
    last_error: str = ""
    source: str = ""
    log: Callable[[str], None] | None = field(default=None, repr=False, compare=False)


_lock = threading.Lock()
_stop_event = threading.Event()
_worker_thread: threading.Thread | None = None
_targets: dict[int, BaxiGptStatusPollTarget] = {}
_account_reconcile_next_due_at = 0.0
_account_reconcile_last_result: dict[str, Any] = {
    "checked": 0,
    "updated": 0,
    "paid": 0,
    "failed": 0,
    "processing": 0,
    "skipped": 0,
    "errors": [],
    "finished_at": "",
}


def _positive_int(value: Any, default: int, *, minimum: int = 1, maximum: int = 86400) -> int:
    try:
        parsed = int(float(str(value or "").strip()))
    except Exception:
        parsed = default
    return max(minimum, min(maximum, parsed))


def _parse_time(value: Any) -> float:
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    text = str(value or "").strip()
    if not text:
        return 0.0
    normalized = text.replace("Z", "+00:00")
    try:
        if " " in normalized and "T" not in normalized:
            normalized = normalized.replace(" ", "T")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc).timestamp()
        except Exception:
            continue
    return 0.0


def _recent_record(record: Any, *, max_age_seconds: int = RESTORE_WINDOW_SECONDS) -> bool:
    now = time.time()
    # 按需求只看“最近提交”或“最近查询”，不把 updated_at 当作恢复依据，避免历史 failed/reset 之类误入队列。
    candidates = (getattr(record, "submitted_at", ""), getattr(record, "last_checked_at", ""))
    return any(ts > 0 and now - ts <= max_age_seconds for ts in (_parse_time(value) for value in candidates))


def _now_text() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_account_order_status(value: Any) -> str:
    status = str(value or "").strip().lower()
    if status in {"paid", "success", "completed"}:
        return STATUS_PAID
    if status in {"failed", "fail", "expired", "cancelled", "canceled", "invalid", "error", "used"}:
        return STATUS_FAILED
    if status in {"processing", "pending", "submitted", "polling", "extracting", "wait_scan", "wait-scan", "verifying"}:
        return STATUS_PROCESSING
    return STATUS_PROCESSING


def _account_order_payload(account: AccountModel) -> tuple[dict[str, Any], dict[str, Any]] | None:
    try:
        extra = account.get_extra()
    except Exception:
        extra = {}
    if not isinstance(extra, dict):
        extra = {}
    payload = extra.get("baxigpt_cdk") if isinstance(extra.get("baxigpt_cdk"), dict) else {}
    if bool(payload.get("polling_disabled")):
        return None
    status = str(payload.get("status") or "").strip().lower()
    if status not in ACCOUNT_RECONCILE_STATUSES:
        return None
    return extra, dict(payload)


def stop_task_polling(
    task_id: str,
    *,
    reason: str = POLLING_STOPPED_REASON,
) -> dict[str, Any]:
    """停止一个本地 Idea 任务留下的所有未终态订单轮询。

    订单已经写入账号 extra 后，不能只清理进程内 target：服务重启或其他
    调用方仍可能从旧的 ``processing`` 记录重新发现它们。这里同时写入持久化
    ``polling_disabled`` 标记，并把账号侧状态收口为 ``stopped``；上游状态
    保留在 ``upstream_status``，不假装订单已失败或已支付。
    """

    normalized_task_id = str(task_id or "").strip()
    if not normalized_task_id:
        return {"task_id": "", "accounts_marked": 0, "targets_removed": 0}
    stop_reason = str(reason or POLLING_STOPPED_REASON).strip()[:1000]
    now_text = _now_text()
    matched_account_ids: list[int] = []
    matched_record_ids: list[int] = []
    with Session(engine) as session:
        accounts = session.exec(
            select(AccountModel)
            .where(AccountModel.platform == "chatgpt")
            .where(
                func.json_extract(AccountModel.extra_json, "$.baxigpt_cdk.task_id")
                == normalized_task_id
            )
        ).all()
        for account in accounts:
            try:
                extra = account.get_extra()
            except Exception:
                extra = {}
            if not isinstance(extra, dict):
                continue
            payload = extra.get("baxigpt_cdk") if isinstance(extra.get("baxigpt_cdk"), dict) else {}
            payload_task_id = str(payload.get("task_id") or "").strip()
            if payload_task_id != normalized_task_id:
                continue
            current_status = str(payload.get("status") or "").strip().lower()
            if current_status not in ACCOUNT_RECONCILE_STATUSES and not bool(payload.get("polling_disabled")):
                continue
            payload = dict(payload)
            payload["status"] = POLLING_STOPPED_STATUS
            payload["polling_disabled"] = True
            payload["polling_disabled_at"] = now_text
            payload["polling_disabled_reason"] = stop_reason
            payload["last_error_message"] = stop_reason
            _upsert_account_order_history(extra, payload)
            extra["baxigpt_cdk"] = payload

            marker = extra.get(IDEA_SUBMIT_MARKER_KEY)
            marker = dict(marker) if isinstance(marker, dict) else {}
            marker.update(
                {
                    "status": POLLING_STOPPED_STATUS,
                    "reason": stop_reason,
                    "source": "baxigpt_cdk_submit",
                    "task_id": normalized_task_id,
                    "stopped_at": now_text,
                }
            )
            extra[IDEA_SUBMIT_MARKER_KEY] = marker
            account.set_extra(extra)
            account.updated_at = datetime.now(timezone.utc)
            session.add(account)
            if account.id:
                matched_account_ids.append(int(account.id))
            try:
                cdk_id = int(payload.get("cdk_id") or 0)
            except Exception:
                cdk_id = 0
            if cdk_id > 0:
                matched_record_ids.append(cdk_id)
        if matched_account_ids:
            session.commit()

    with _lock:
        targets_removed = 0
        matched_ids = set(matched_record_ids)
        for record_id, target in list(_targets.items()):
            if str(target.task_id or "").strip() == normalized_task_id:
                _targets.pop(record_id, None)
                targets_removed += 1
                continue
            if record_id in matched_ids:
                _targets.pop(record_id, None)
                targets_removed += 1

    if matched_account_ids:
        try:
            from services.account_filters import upsert_account_list_state_for_account_ids

            with Session(engine) as session:
                upsert_account_list_state_for_account_ids(session, matched_account_ids, commit=True)
        except Exception:
            # The order stop marker is already durable; a derived-list refresh can
            # be retried by the normal account-state maintenance path.
            pass

    return {
        "task_id": normalized_task_id,
        "accounts_marked": len(matched_account_ids),
        "targets_removed": targets_removed,
    }


def _resolve_account_order_id(payload: dict[str, Any], *, repo: BaxiGptCdkRepository) -> tuple[str, str]:
    """Return (poll_order_id, persisted_order_id).

    历史账号 extra 里有些只保存了 display_id，没有保存 `cdk::task_id`。
    轮询上游必须带卡密，所以这里按 cdk_id 回查卡密行临时拼出 order_id。
    """

    order_id = str(payload.get("order_id") or "").strip()
    if "::" in order_id:
        return order_id, order_id

    display_id = str(payload.get("display_id") or "").strip()
    if not display_id:
        return "", order_id
    try:
        cdk_id = int(payload.get("cdk_id") or 0)
    except Exception:
        cdk_id = 0
    if cdk_id <= 0:
        return "", order_id
    record = repo.get_by_id(cdk_id)
    code = str(getattr(record, "code_value", "") or "").strip() if record is not None else ""
    if not code:
        return "", order_id
    resolved = f"{code}::{display_id}"
    return resolved, resolved


def _upsert_account_order_history(extra: dict[str, Any], payload: dict[str, Any]) -> None:
    history = extra.get("baxigpt_cdk_history")
    if not isinstance(history, list):
        history = []
    last_history = history[-1] if history and isinstance(history[-1], dict) else {}
    history_changed = any(
        str(last_history.get(key) or "") != str(payload.get(key) or "")
        for key in ("status", "upstream_status", "order_id", "display_id", "last_error_message")
    )
    if not history or history_changed:
        history.append(dict(payload))
    extra["baxigpt_cdk_history"] = history[-20:]


def _clear_idea_unavailable_marker(extra: dict[str, Any]) -> None:
    marker = extra.get(IDEA_SUBMIT_MARKER_KEY)
    if isinstance(marker, dict):
        marker = dict(marker)
        marker["available"] = True
        marker["unavailable"] = False
        marker["cleared_at"] = _now_text()
        marker.pop("reason", None)
        extra[IDEA_SUBMIT_MARKER_KEY] = marker
    for key in IDEA_SUBMIT_UNAVAILABLE_KEYS:
        extra.pop(key, None)


def _account_reconcile_candidate_rows(
    *,
    limit: int,
    stale_seconds: int,
    repo: BaxiGptCdkRepository,
) -> tuple[list[dict[str, Any]], int]:
    now = time.time()
    candidates: list[dict[str, Any]] = []
    skipped = 0
    with Session(engine) as session:
        accounts = session.exec(select(AccountModel).where(AccountModel.platform == "chatgpt")).all()
        for account in accounts:
            parsed = _account_order_payload(account)
            if parsed is None:
                continue
            extra, payload = parsed
            poll_order_id, persisted_order_id = _resolve_account_order_id(payload, repo=repo)
            if not poll_order_id:
                candidates.append(
                    {
                        "account_id": int(account.id or 0),
                        "email": str(account.email or ""),
                        "payload": payload,
                        "poll_order_id": "",
                        "persisted_order_id": persisted_order_id,
                        "last_checked_ts": 0,
                        "missing_order_id": True,
                    }
                )
                continue
            last_checked_ts = _parse_time(payload.get("last_checked_at") or payload.get("submitted_at"))
            if stale_seconds > 0 and last_checked_ts > 0 and now - last_checked_ts < stale_seconds:
                skipped += 1
                continue
            candidates.append(
                {
                    "account_id": int(account.id or 0),
                    "email": str(account.email or ""),
                    "payload": payload,
                    "poll_order_id": poll_order_id,
                    "persisted_order_id": persisted_order_id,
                    "last_checked_ts": last_checked_ts,
                    "missing_order_id": False,
                }
            )
    candidates.sort(key=lambda item: (float(item.get("last_checked_ts") or 0), int(item.get("account_id") or 0)))
    if limit > 0:
        candidates = candidates[:limit]
    return candidates, skipped


def _apply_account_order_status(
    *,
    account_id: int,
    payload: dict[str, Any],
    response: dict[str, Any],
    persisted_order_id: str = "",
) -> str:
    status = _normalize_account_order_status(response.get("status"))
    now_text = _now_text()
    message = str(response.get("message") or response.get("error") or response.get("detail") or "").strip()
    with Session(engine) as session:
        account = session.get(AccountModel, int(account_id or 0))
        if account is None or account.platform != "chatgpt":
            return "missing"
        try:
            extra = account.get_extra()
        except Exception:
            extra = {}
        if not isinstance(extra, dict):
            extra = {}
        current_payload = extra.get("baxigpt_cdk") if isinstance(extra.get("baxigpt_cdk"), dict) else {}
        # 不覆盖任务运行中刚写入的其他订单：只有同一 order/display/cdk 仍处于提交态才回填。
        current_status = str(current_payload.get("status") or "").strip().lower()
        if current_status not in ACCOUNT_RECONCILE_STATUSES:
            return "not_pollable"
        current_order = str(current_payload.get("order_id") or "").strip()
        current_display = str(current_payload.get("display_id") or "").strip()
        expected_order = str(payload.get("order_id") or persisted_order_id or "").strip()
        expected_display = str(payload.get("display_id") or "").strip()
        if expected_order and current_order and current_order != expected_order:
            return "changed"
        if expected_display and current_display and current_display != expected_display:
            return "changed"

        next_payload = dict(current_payload)
        next_payload["status"] = status
        next_payload["upstream_status"] = status
        if persisted_order_id and not str(next_payload.get("order_id") or "").strip():
            next_payload["order_id"] = persisted_order_id
        if response.get("display_id"):
            next_payload["display_id"] = str(response.get("display_id") or "")
        if response.get("email"):
            next_payload["remote_email"] = str(response.get("email") or "")
        next_payload["last_checked_at"] = now_text
        next_payload["last_error_message"] = message
        if status == STATUS_PAID and not next_payload.get("paid_at"):
            next_payload["paid_at"] = now_text
        if status == STATUS_PAID:
            next_payload["last_error_message"] = ""
            _clear_idea_unavailable_marker(extra)

        extra["baxigpt_cdk"] = next_payload
        _upsert_account_order_history(extra, next_payload)
        account.set_extra(extra)
        account.updated_at = datetime.now(timezone.utc)
        session.add(account)
        session.commit()

    with Session(engine) as session:
        from services.account_filters import upsert_account_list_state_for_account_ids

        upsert_account_list_state_for_account_ids(session, [int(account_id or 0)], commit=True)
    return status


def reconcile_pending_account_statuses_once(
    *,
    limit: int = ACCOUNT_RECONCILE_BATCH_SIZE,
    stale_seconds: int = ACCOUNT_RECONCILE_STALE_SECONDS,
) -> dict[str, Any]:
    """Poll account-level Idea orders that are no longer represented by cdk_pool.

    The cdk pool table stores one mutable row per card code, but a single card can
    submit many account orders.  When a batch task is stopped/restarted, older
    account orders can remain only in `accounts.extra_json.baxigpt_cdk`; this
    reconciler polls those account-level order IDs directly and refreshes the
    derived account-list cache.
    """

    repo = BaxiGptCdkRepository()
    client = BaxiGptClient(timeout=15, retries=0)
    candidates, skipped_fresh = _account_reconcile_candidate_rows(
        limit=max(int(limit or 0), 0),
        stale_seconds=max(int(stale_seconds or 0), 0),
        repo=repo,
    )
    result: dict[str, Any] = {
        "checked": 0,
        "updated": 0,
        "paid": 0,
        "failed": 0,
        "processing": 0,
        "missing": 0,
        "skipped": int(skipped_fresh or 0),
        "errors": [],
        "finished_at": _now_text(),
    }
    for candidate in candidates:
        account_id = int(candidate.get("account_id") or 0)
        email = str(candidate.get("email") or "")
        poll_order_id = str(candidate.get("poll_order_id") or "")
        persisted_order_id = str(candidate.get("persisted_order_id") or "")
        payload = dict(candidate.get("payload") or {})
        result["checked"] += 1
        try:
            if not poll_order_id:
                response = {
                    "ok": False,
                    "status": STATUS_FAILED,
                    "message": "缺少上游 order_id/display_id，不能继续轮询",
                }
            else:
                response = client.status(poll_order_id)
            applied = _apply_account_order_status(
                account_id=account_id,
                payload=payload,
                response=response,
                persisted_order_id=persisted_order_id,
            )
            if applied in {STATUS_PAID, STATUS_FAILED, STATUS_PROCESSING}:
                result["updated"] += 1
                result[applied] += 1
            elif applied == "missing":
                result["missing"] += 1
            else:
                result["skipped"] += 1
        except Exception as exc:
            errors = result.setdefault("errors", [])
            if isinstance(errors, list) and len(errors) < 20:
                errors.append({"account_id": account_id, "email": email, "error": str(exc)[:300]})
    return result


def _target_snapshot(target: BaxiGptStatusPollTarget) -> dict[str, Any]:
    return {
        "record_id": int(target.record_id or 0),
        "task_id": str(target.task_id or ""),
        "interval_seconds": int(target.interval_seconds or 0),
        "timeout_seconds": int(target.timeout_seconds or 0),
        "next_due_at": float(target.next_due_at or 0),
        "deadline_at": float(target.deadline_at or 0),
        "last_status": str(target.last_status or ""),
        "last_error": str(target.last_error or ""),
        "source": str(target.source or ""),
    }


def _safe_log(target: BaxiGptStatusPollTarget, message: str) -> None:
    callback = target.log
    if callback is None:
        return
    try:
        callback(message)
    except Exception:
        pass


def _refresh_bound_account_after_paid(
    record: Any,
    target: BaxiGptStatusPollTarget,
    *,
    repo: BaxiGptCdkRepository | None = None,
) -> dict[str, Any] | None:
    account_id = int(getattr(record, "bound_account_id", 0) or 0)
    email = str(getattr(record, "bound_account_email", "") or getattr(record, "remote_email", "") or "").strip()
    with Session(engine) as session:
        account = session.get(AccountModel, account_id) if account_id > 0 else None
        if account is None and email:
            account = session.exec(
                select(AccountModel)
                .where(AccountModel.platform == "chatgpt")
                .where(AccountModel.email == email)
            ).first()
        if account is None or account.platform != "chatgpt":
            _safe_log(target, f"[Idea][WARN] paid 后本地状态刷新跳过: 未找到绑定账号 account_id={account_id or '-'} email={email or '-'}")
            return None
        _safe_log(target, f"[Idea] paid 已确认，开始刷新本地账号状态: {account.email or account.id}")
        refresh_ok = True
        try:
            refresh_result = sync_chatgpt_account_local_status(session, account)
            summary = summarize_status_refresh(refresh_result, trigger="pix_cdk_paid")
        except Exception as exc:
            refresh_ok = False
            summary = {
                "trigger": "pix_cdk_paid",
                "refreshed_at": datetime.now(timezone.utc).isoformat(),
                "status": str(getattr(account, "status", "") or ""),
                "error": str(exc),
            }
        (repo or BaxiGptCdkRepository()).persist_account_binding_extra(
            account,
            record,
            status=STATUS_PAID,
            local_status_refresh=summary,
            apply_payment_state=False,
        )
        session.add(account)
        session.commit()

        if refresh_ok:
            _safe_log(
                target,
                "[Idea] 本地状态刷新完成: "
                f"{account.email or account.id} "
                f"status={summary.get('status') or '-'} "
                f"plan={summary.get('subscription_plan') or '-'} "
                f"auth={summary.get('auth_state') or '-'} "
                f"gate={summary.get('upload_gate') or '-'}",
            )
        else:
            _safe_log(target, f"[Idea][WARN] paid 后本地状态刷新失败，已写入失败摘要: {account.email or account.id} - {summary.get('error')}")
        return summary


def start() -> None:
    """启动全局轮询线程。没有目标时线程会空转等待。"""
    global _worker_thread
    with _lock:
        if _worker_thread and _worker_thread.is_alive():
            return
        _stop_event.clear()
        _worker_thread = threading.Thread(target=_loop, name="baxigpt-status-poller", daemon=True)
        _worker_thread.start()


def stop() -> None:
    _stop_event.set()


def enqueue_status_poll(
    record_id: int,
    *,
    task_id: str = "",
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    log: Callable[[str], None] | None = None,
    immediate: bool = False,
    source: str = "submit_success",
) -> bool:
    """把一个卡密订单加入后台状态轮询。

    返回 False 代表 record_id 无效，或当前记录不满足“有 order_id 且 submitted/processing”的轮询条件。
    """
    rid = int(record_id or 0)
    if rid <= 0:
        return False
    record = BaxiGptCdkRepository().get_by_id(rid)
    if (
        record is None
        or not record.order_id
        or str(record.status or "").strip().lower() not in POLLABLE_STATUSES
        or _record_polling_disabled(record)
    ):
        return False

    interval = _positive_int(interval_seconds, DEFAULT_INTERVAL_SECONDS, minimum=1, maximum=3600)
    timeout = _positive_int(timeout_seconds, DEFAULT_TIMEOUT_SECONDS, minimum=interval, maximum=86400)
    now = time.time()
    target = BaxiGptStatusPollTarget(
        record_id=rid,
        task_id=str(task_id or "").strip(),
        interval_seconds=interval,
        timeout_seconds=timeout,
        next_due_at=now if immediate else now + interval,
        deadline_at=now + timeout,
        last_status=str(record.upstream_status or record.status or "").strip().lower(),
        source=str(source or "").strip() or "submit_success",
        log=log,
    )
    with _lock:
        existing = _targets.get(rid)
        if existing is not None:
            # 已在轮询时延长截止时间，并保留最近一次状态/错误，避免重复刷日志。
            target.last_status = existing.last_status
            target.last_error = existing.last_error
            if not target.source:
                target.source = existing.source
            if existing.next_due_at and not immediate:
                target.next_due_at = min(existing.next_due_at, target.next_due_at)
        _targets[rid] = target
    start()
    return True


def enqueue_many(
    record_ids: list[int],
    *,
    task_id: str = "",
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    log: Callable[[str], None] | None = None,
    immediate: bool = False,
    source: str = "submit_success",
) -> int:
    count = 0
    for record_id in record_ids:
        if enqueue_status_poll(
            record_id,
            task_id=task_id,
            interval_seconds=interval_seconds,
            timeout_seconds=timeout_seconds,
            log=log,
            immediate=immediate,
            source=source,
        ):
            count += 1
    return count


def restore_pending_targets(
    *,
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
    timeout_seconds: int = 86400,
) -> int:
    """把重启前已提交但未到终态的订单重新放回轮询队列。"""
    repo = BaxiGptCdkRepository()
    records = [
        record
        for status in (STATUS_SUBMITTED, STATUS_PROCESSING)
        for record in repo.list(status=status)
        if record.order_id and _recent_record(record, max_age_seconds=RESTORE_WINDOW_SECONDS)
    ]
    return enqueue_many(
        [int(record.id or 0) for record in records],
        interval_seconds=interval_seconds,
        timeout_seconds=timeout_seconds,
        immediate=True,
        source="restart_restore",
    )


def snapshot() -> dict[str, Any]:
    with _lock:
        targets = [_target_snapshot(target) for target in _targets.values()]
        targets.sort(key=lambda item: int(item.get("record_id") or 0))
        return {
            "running": bool(_worker_thread and _worker_thread.is_alive()),
            "queued": len(_targets),
            "ids": sorted(_targets),
            "targets": targets,
            "account_reconcile": {
                "next_due_at": float(_account_reconcile_next_due_at or 0),
                "last_result": dict(_account_reconcile_last_result),
            },
        }


def _take_due_targets(now: float) -> list[BaxiGptStatusPollTarget]:
    due: list[BaxiGptStatusPollTarget] = []
    with _lock:
        for target in list(_targets.values()):
            if target.next_due_at <= now:
                due.append(target)
    return due


def _remove_target(record_id: int) -> None:
    with _lock:
        _targets.pop(int(record_id or 0), None)


def _record_polling_disabled(record: Any) -> bool:
    """Check the bound account marker before any upstream request."""

    account_id = int(getattr(record, "bound_account_id", 0) or 0)
    email = str(
        getattr(record, "bound_account_email", "")
        or getattr(record, "remote_email", "")
        or ""
    ).strip()
    try:
        with Session(engine) as session:
            account = session.get(AccountModel, account_id) if account_id > 0 else None
            if account is None and email:
                account = session.exec(
                    select(AccountModel)
                    .where(AccountModel.platform == "chatgpt")
                    .where(AccountModel.email == email)
                ).first()
            if account is None:
                return False
            try:
                extra = account.get_extra()
            except Exception:
                extra = {}
            payload = extra.get("baxigpt_cdk") if isinstance(extra, dict) else {}
            return bool(isinstance(payload, dict) and payload.get("polling_disabled"))
    except Exception:
        # The standalone CDK-pool migration/tests can run before the account
        # table exists. In that context there is no stop marker to consult.
        return False


def _reschedule_target(target: BaxiGptStatusPollTarget) -> None:
    with _lock:
        current = _targets.get(target.record_id)
        if current is None:
            return
        current.next_due_at = time.time() + max(int(current.interval_seconds or DEFAULT_INTERVAL_SECONDS), 1)
        current.last_status = target.last_status
        current.last_error = target.last_error


def _account_reconcile_due(now: float) -> bool:
    with _lock:
        return now >= float(_account_reconcile_next_due_at or 0)


def _run_account_reconcile_if_due(now: float) -> None:
    global _account_reconcile_next_due_at, _account_reconcile_last_result
    if not ACCOUNT_RECONCILE_AUTOMATIC:
        return
    if not _account_reconcile_due(now):
        return
    with _lock:
        # 先推进下次执行时间，避免本轮网络慢时被连续重入。
        _account_reconcile_next_due_at = time.time() + ACCOUNT_RECONCILE_INTERVAL_SECONDS
    try:
        result = reconcile_pending_account_statuses_once(
            limit=ACCOUNT_RECONCILE_BATCH_SIZE,
            stale_seconds=ACCOUNT_RECONCILE_STALE_SECONDS,
        )
    except Exception as exc:
        result = {
            "checked": 0,
            "updated": 0,
            "paid": 0,
            "failed": 0,
            "processing": 0,
            "skipped": 0,
            "errors": [{"error": str(exc)[:300]}],
            "finished_at": _now_text(),
        }
    with _lock:
        _account_reconcile_last_result = dict(result)


def _loop() -> None:
    repo = BaxiGptCdkRepository()
    client = BaxiGptClient()
    while not _stop_event.is_set():
        now = time.time()
        due = _take_due_targets(now)
        if not due:
            # Account-level reconciliation is intentionally not automatic. A
            # stopped local Idea task must not keep querying upstream orders in
            # the background; explicit/manual poll requests still use targets.
            _stop_event.wait(0.5)
            continue
        for target in due:
            if _stop_event.is_set():
                break
            try:
                done = _poll_once(repo, client, target)
            except Exception as exc:
                target.last_error = str(exc)
                _safe_log(target, f"[Idea] 状态轮询异常: record_id={target.record_id} - {exc}")
                done = time.time() >= target.deadline_at
            if done:
                _remove_target(target.record_id)
            else:
                _reschedule_target(target)


def _poll_once(repo: BaxiGptCdkRepository, client: BaxiGptClient, target: BaxiGptStatusPollTarget) -> bool:
    record = repo.get_by_id(target.record_id)
    if record is None:
        target.last_error = "卡密记录不存在"
        _safe_log(target, f"[Idea] 状态轮询停止: 卡密记录不存在 record_id={target.record_id}")
        return True
    if not record.order_id:
        target.last_error = "没有 order_id"
        _safe_log(target, f"[Idea] 状态轮询停止: {record.code_masked} 没有 order_id")
        return True
    if _record_polling_disabled(record):
        target.last_error = "本地任务已停止轮询"
        _safe_log(target, f"[Idea] 状态轮询停止: 本地任务已停止 {record.display_id or record.order_id}")
        return True
    if str(record.status or "").strip().lower() not in POLLABLE_STATUSES:
        if record.status in REPOSITORY_TERMINAL_STATUSES:
            if record.status == STATUS_PAID:
                _refresh_bound_account_after_paid(record, target, repo=repo)
            else:
                repo.persist_bound_account_extra(record)
        target.last_error = f"状态不可轮询: {record.status}"
        _safe_log(target, f"[Idea] 状态轮询停止: {record.code_masked} status={record.status} 不在 submitted/processing")
        return True
    if time.time() > target.deadline_at:
        repo.persist_bound_account_extra(record)
        target.last_error = "状态轮询超时"
        _safe_log(target, f"[Idea] 状态轮询超时: {record.bound_account_email or record.bound_account_id or '-'} {record.display_id or record.order_id} last_status={record.upstream_status or record.status}")
        return True

    try:
        response = client.status(record.order_id)
    except Exception as exc:
        target.last_error = str(exc)
        raise
    updated = repo.mark_status_response(record.id, response)
    if updated is None:
        target.last_error = "状态响应未写入"
        return False
    if updated.status != STATUS_PAID:
        repo.persist_bound_account_extra(updated)
    status = str(updated.upstream_status or updated.status or response.get("status") or "").strip().lower()
    display_id = updated.display_id or updated.order_id
    email = updated.bound_account_email or updated.remote_email or str(updated.bound_account_id or "-")
    if status != target.last_status:
        _safe_log(target, f"[Idea] 状态轮询: {email} {display_id} status={status or '-'}")
        target.last_status = status
    target.last_error = ""
    if updated.status in TERMINAL_STATUSES:
        if updated.status == STATUS_PAID:
            _safe_log(target, f"[OK] Idea 提交成功: {email} {display_id} status=paid")
            try:
                _refresh_bound_account_after_paid(updated, target, repo=repo)
            except Exception as exc:
                target.last_error = str(exc)
                _safe_log(target, f"[Idea][WARN] paid 后本地状态刷新失败: {email} {display_id} - {exc}")
        elif updated.status == STATUS_FAILED:
            _safe_log(target, f"[FAIL] Idea 提交失败: {email} {display_id} status={status or updated.status} {updated.last_error_message or ''}".rstrip())
        return True
    return False
