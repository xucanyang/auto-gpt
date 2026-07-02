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

from sqlmodel import Session, select

from core.db import AccountModel, engine
from services.chatgpt_core.baxigpt_cdk_repository import (
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


def _refresh_bound_account_after_paid(record: Any, target: BaxiGptStatusPollTarget) -> dict[str, Any] | None:
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
            _safe_log(target, f"[Pix][WARN] paid 后本地状态刷新跳过: 未找到绑定账号 account_id={account_id or '-'} email={email or '-'}")
            return None
        _safe_log(target, f"[Pix] paid 已确认，开始刷新本地账号状态: {account.email or account.id}")
        refresh_result = sync_chatgpt_account_local_status(session, account)
        summary = summarize_status_refresh(refresh_result, trigger="pix_cdk_paid")

        try:
            extra = account.get_extra()
        except Exception:
            extra = {}
        if not isinstance(extra, dict):
            extra = {}
        cdk_payload = extra.get("baxigpt_cdk") if isinstance(extra.get("baxigpt_cdk"), dict) else {}
        cdk_payload = dict(cdk_payload)
        cdk_payload["local_status_refresh"] = summary
        extra["baxigpt_cdk"] = cdk_payload
        account.set_extra(extra)
        session.add(account)
        session.commit()

        _safe_log(
            target,
            "[Pix] 本地状态刷新完成: "
            f"{account.email or account.id} "
            f"status={summary.get('status') or '-'} "
            f"plan={summary.get('subscription_plan') or '-'} "
            f"auth={summary.get('auth_state') or '-'} "
            f"gate={summary.get('upload_gate') or '-'}",
        )
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
    if record is None or not record.order_id or str(record.status or "").strip().lower() not in POLLABLE_STATUSES:
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


def _reschedule_target(target: BaxiGptStatusPollTarget) -> None:
    with _lock:
        current = _targets.get(target.record_id)
        if current is None:
            return
        current.next_due_at = time.time() + max(int(current.interval_seconds or DEFAULT_INTERVAL_SECONDS), 1)
        current.last_status = target.last_status
        current.last_error = target.last_error


def _loop() -> None:
    repo = BaxiGptCdkRepository()
    client = BaxiGptClient()
    while not _stop_event.is_set():
        due = _take_due_targets(time.time())
        if not due:
            _stop_event.wait(0.5)
            continue
        for target in due:
            if _stop_event.is_set():
                break
            try:
                done = _poll_once(repo, client, target)
            except Exception as exc:
                target.last_error = str(exc)
                _safe_log(target, f"[Pix] 状态轮询异常: record_id={target.record_id} - {exc}")
                done = time.time() >= target.deadline_at
            if done:
                _remove_target(target.record_id)
            else:
                _reschedule_target(target)


def _poll_once(repo: BaxiGptCdkRepository, client: BaxiGptClient, target: BaxiGptStatusPollTarget) -> bool:
    record = repo.get_by_id(target.record_id)
    if record is None:
        target.last_error = "卡密记录不存在"
        _safe_log(target, f"[Pix] 状态轮询停止: 卡密记录不存在 record_id={target.record_id}")
        return True
    if not record.order_id:
        target.last_error = "没有 order_id"
        _safe_log(target, f"[Pix] 状态轮询停止: {record.code_masked} 没有 order_id")
        return True
    if str(record.status or "").strip().lower() not in POLLABLE_STATUSES:
        if record.status in REPOSITORY_TERMINAL_STATUSES:
            repo.persist_bound_account_extra(record)
        target.last_error = f"状态不可轮询: {record.status}"
        _safe_log(target, f"[Pix] 状态轮询停止: {record.code_masked} status={record.status} 不在 submitted/processing")
        return True
    if time.time() > target.deadline_at:
        repo.persist_bound_account_extra(record)
        target.last_error = "状态轮询超时"
        _safe_log(target, f"[Pix] 状态轮询超时: {record.bound_account_email or record.bound_account_id or '-'} {record.display_id or record.order_id} last_status={record.upstream_status or record.status}")
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
    repo.persist_bound_account_extra(updated)
    status = str(updated.upstream_status or updated.status or response.get("status") or "").strip().lower()
    display_id = updated.display_id or updated.order_id
    email = updated.bound_account_email or updated.remote_email or str(updated.bound_account_id or "-")
    if status != target.last_status:
        _safe_log(target, f"[Pix] 状态轮询: {email} {display_id} status={status or '-'}")
        target.last_status = status
    target.last_error = ""
    if updated.status in TERMINAL_STATUSES:
        if updated.status == STATUS_PAID:
            _safe_log(target, f"[OK] Pix 开通成功: {email} {display_id} status=paid")
            try:
                _refresh_bound_account_after_paid(updated, target)
            except Exception as exc:
                target.last_error = str(exc)
                _safe_log(target, f"[Pix][WARN] paid 后本地状态刷新失败: {email} {display_id} - {exc}")
        elif updated.status == STATUS_FAILED:
            _safe_log(target, f"[FAIL] Pix 开通失败: {email} {display_id} status={status or updated.status} {updated.last_error_message or ''}".rstrip())
        return True
    return False
