"""Quality-gated scheduler for rotating TempMail registration domains."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import logging
import math
import threading
import time
from typing import Any, Callable

from sqlmodel import Session, select

from core import db as core_db
from core.db import RegistrationDomainRotationGroupModel
from services.chatgpt_core.task_logging import sanitize_error_message


logger = logging.getLogger(__name__)

ROTATION_MODE = "rotating"
_LIVE_TASK_ITEM_STATES = frozenset({"starting", "active", "draining"})
_SLOT_ITEM_STATES = frozenset({*_LIVE_TASK_ITEM_STATES, "retry_wait"})
_ACTIVE_GROUP_STATES = frozenset({"running", "stopping", "failing"})
_TERMINAL_GROUP_STATES = frozenset({"completed", "stopped", "failed", "interrupted"})
_TECHNICAL_RETRY_LIMIT = 2
_TECHNICAL_RETRY_BACKOFF_SECONDS = (5.0, 15.0)
_TECHNICAL_FAILURE_CIRCUIT_DISTINCT_DOMAINS = 2
_TECHNICAL_FAILURE_CIRCUIT_WINDOW_SECONDS = 30.0
_TASK_FAILURE_CATEGORIES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "dynamic_proxy_unavailable",
        "动态代理不可用",
        (
            "dynamic proxy",
            "proxy pool unavailable",
            "动态代理没有可用候选",
            "代理预检失败",
            "没有可用候选",
        ),
    ),
    (
        "proxy_unavailable",
        "代理连接异常",
        (
            "proxy",
            "socks",
            "代理",
        ),
    ),
    (
        "tls_failure",
        "TLS/SSL 连接异常",
        (
            "ssl",
            "tls",
            "wrong_version_number",
            "certificate verify",
        ),
    ),
    (
        "network_failure",
        "网络连接异常",
        (
            "connection",
            "econn",
            "network",
            "socket",
            "broken pipe",
            "dns",
            "连接",
            "网络",
        ),
    ),
    (
        "upstream_unavailable",
        "上游服务异常",
        (
            "429",
            "502",
            "503",
            "504",
            "cloudflare",
            "gateway",
            "internal server error",
            "rate limit",
            "service unavailable",
            "too many requests",
            "upstream",
            "上游",
            "限流",
            "服务不可用",
        ),
    ),
    (
        "task_timeout",
        "任务执行超时",
        (
            "timeout",
            "timed out",
            "hard timeout",
            "超时",
        ),
    ),
    (
        "browser_dependency_unavailable",
        "浏览器依赖不可用",
        (
            "browser closed",
            "browser_crashed",
            "sentinel_browser",
            "targetclosederror",
            "浏览器容量",
        ),
    ),
    (
        "storage_temporarily_unavailable",
        "状态存储暂时不可用",
        (
            "database is locked",
            "disk i/o",
            "persistence failed",
            "write failed",
            "数据库被锁定",
            "写入失败",
        ),
    ),
)
_TECHNICAL_LINK_REASON_CODES = frozenset(
    {
        "account_identity_changed",
        "account_identity_changed_before_submit",
        "account_not_found",
        "invalid_account_id",
        "invalid_frozen_profile",
        "payment_link_persist_failed",
        "payment_link_queue_rejected",
        "postprocessor_unavailable",
        "task_exception",
    }
)
_TECHNICAL_LINK_MESSAGE_MARKERS = (
    "429",
    "502",
    "503",
    "504",
    "broken pipe",
    "cloudflare",
    "connection",
    "dns",
    "econn",
    "fetch failed",
    "gateway",
    "internal server error",
    "network",
    "proxy",
    "rate limit",
    "socket",
    "ssl",
    "service unavailable",
    "tls",
    "timeout",
    "timed out",
    "too many requests",
    "upstream",
    "上游",
    "连接",
    "代理",
    "限流",
    "服务不可用",
    "网络",
    "超时",
    "配置不可用",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _nonnegative_int(value: Any) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _positive_int(value: Any, default: int = 1) -> int:
    parsed = _nonnegative_int(value)
    try:
        fallback = max(int(default), 0)
    except (TypeError, ValueError, OverflowError):
        fallback = 1
    return parsed if parsed > 0 else fallback


def _float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return float(default)
    return parsed if math.isfinite(parsed) else float(default)


def _default_schedule_retry(
    delay_seconds: float,
    callback: Callable[[], None],
) -> threading.Timer:
    timer = threading.Timer(max(float(delay_seconds or 0), 0.0), callback)
    timer.daemon = True
    timer.start()
    return timer


def _public_group_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    items = snapshot.get("items") if isinstance(snapshot.get("items"), list) else []
    public_items: list[dict[str, Any]] = []
    tasks: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    created_domains = 0
    seen_task_ids: set[str] = set()
    for raw_item in items:
        if not isinstance(raw_item, dict):
            continue
        item = dict(raw_item)
        state = str(item.get("state") or "pending").strip().lower() or "pending"
        counts[state] += 1
        quality = item.get("quality") if isinstance(item.get("quality"), dict) else {}
        trigger = item.get("trigger") if isinstance(item.get("trigger"), dict) else {}
        public_item = {
            "domain": str(item.get("domain") or ""),
            "position": _positive_int(item.get("position")),
            "state": state,
            "task_id": str(item.get("task_id") or ""),
            "quality": deepcopy(quality),
            "trigger": deepcopy(trigger),
            "error": str(item.get("error") or ""),
            "attempt_count": _nonnegative_int(item.get("attempt_count")),
            "retry_count": _nonnegative_int(item.get("retry_count")),
            "retry_limit": _nonnegative_int(
                item.get("retry_limit")
                if item.get("retry_limit") is not None
                else _TECHNICAL_RETRY_LIMIT
            ),
            "next_retry_at": str(item.get("next_retry_at") or ""),
            "technical_failure": deepcopy(
                item.get("technical_failure")
                if isinstance(item.get("technical_failure"), dict)
                else {}
            ),
            "started_at": str(item.get("started_at") or ""),
            "terminal_at": str(item.get("terminal_at") or ""),
        }
        public_items.append(public_item)
        raw_attempts = item.get("attempts") if isinstance(item.get("attempts"), list) else []
        attempt_tasks: list[dict[str, Any]] = []
        for attempt_index, raw_attempt in enumerate(raw_attempts, start=1):
            if not isinstance(raw_attempt, dict):
                continue
            attempt_task_id = str(raw_attempt.get("task_id") or "").strip()
            if not attempt_task_id or attempt_task_id in seen_task_ids:
                continue
            seen_task_ids.add(attempt_task_id)
            attempt_tasks.append(
                {
                    **dict(public_item),
                    "task_id": attempt_task_id,
                    "state": str(raw_attempt.get("state") or "active").strip().lower()
                    or "active",
                    "error": str(raw_attempt.get("error") or ""),
                    "attempt": _positive_int(raw_attempt.get("attempt"), attempt_index),
                    "is_current": attempt_task_id == public_item["task_id"],
                    "started_at": str(raw_attempt.get("started_at") or ""),
                    "terminal_at": str(raw_attempt.get("terminal_at") or ""),
                }
            )
        if not attempt_tasks and public_item["task_id"]:
            attempt_tasks.append(
                {
                    **dict(public_item),
                    "attempt": _positive_int(item.get("attempt_count")),
                    "is_current": True,
                }
            )
            seen_task_ids.add(public_item["task_id"])
        if attempt_tasks:
            created_domains += 1
            tasks.extend(attempt_tasks)
        if state in {"start_failed", "failed", "technical_failed"}:
            technical_failure = public_item.get("technical_failure") or {}
            errors.append(
                {
                    "domain": public_item["domain"],
                    "position": public_item["position"],
                    "state": state,
                    "message": str(
                        item.get("error")
                        or technical_failure.get("message")
                        or "域名任务异常结束"
                    ),
                    "retry_count": public_item["retry_count"],
                    "retry_limit": public_item["retry_limit"],
                }
            )

    requested_count = _positive_int(snapshot.get("requested_domain_count"), len(public_items) or 1)
    return {
        "task_group_id": str(snapshot.get("task_group_id") or ""),
        "mode": ROTATION_MODE,
        "state": str(snapshot.get("state") or "interrupted"),
        "requested_domain_count": requested_count,
        "created_count": created_domains,
        "task_attempt_count": len(tasks),
        "failed_count": len(errors),
        "requested_count_per_task": _positive_int(snapshot.get("requested_count_per_task")),
        "requested_concurrency_per_task": _positive_int(
            snapshot.get("requested_concurrency_per_task")
        ),
        "active_domain_slots": _positive_int(snapshot.get("active_domain_slots")),
        "policy": deepcopy(snapshot.get("policy") or {}),
        "counts": dict(counts),
        "tasks": tasks,
        "domains": public_items,
        "errors": errors,
        "failure": deepcopy(
            snapshot.get("failure") if isinstance(snapshot.get("failure"), dict) else {}
        ),
        "technical_failures": deepcopy(
            snapshot.get("technical_failures")
            if isinstance(snapshot.get("technical_failures"), list)
            else []
        ),
        "stop_reason": str(snapshot.get("stop_reason") or ""),
        "created_at": str(snapshot.get("created_at") or ""),
        "updated_at": str(snapshot.get("updated_at") or ""),
        "finished_at": str(snapshot.get("finished_at") or ""),
    }


def persist_rotation_group_snapshot(snapshot: dict[str, Any]) -> None:
    group_id = str(snapshot.get("task_group_id") or "").strip()
    if not group_id:
        raise ValueError("rotation group id is required")
    with Session(core_db.engine) as session:
        row = session.get(RegistrationDomainRotationGroupModel, group_id)
        if row is None:
            row = RegistrationDomainRotationGroupModel(group_id=group_id)
        row.state = str(snapshot.get("state") or "running")[:64]
        row.stop_reason = str(snapshot.get("stop_reason") or "")[:1000]
        row.finished_at = str(snapshot.get("finished_at") or "")[:64]
        row.updated_at = datetime.now(timezone.utc)
        row.set_snapshot(snapshot)
        session.add(row)
        session.commit()


def load_rotation_group_snapshot(group_id: str) -> dict[str, Any] | None:
    normalized = str(group_id or "").strip()
    if not normalized:
        return None
    with Session(core_db.engine) as session:
        row = session.get(RegistrationDomainRotationGroupModel, normalized)
        if row is None:
            return None
        snapshot = row.get_snapshot()
        return snapshot if snapshot else None


def mark_stale_rotation_groups_interrupted() -> int:
    """Terminate pre-restart runtime groups without resuming frozen work."""

    interrupted = 0
    now = _now_iso()
    with Session(core_db.engine) as session:
        rows = session.exec(
            select(RegistrationDomainRotationGroupModel).where(
                RegistrationDomainRotationGroupModel.state.in_(tuple(_ACTIVE_GROUP_STATES))
            )
        ).all()
        for row in rows:
            snapshot = row.get_snapshot()
            if not snapshot:
                snapshot = {
                    "task_group_id": row.group_id,
                    "mode": ROTATION_MODE,
                    "items": [],
                    "created_at": row.created_at.isoformat(),
                }
            snapshot["state"] = "interrupted"
            snapshot["stop_reason"] = "服务重启，轮换任务组已中断且不会自动恢复"
            snapshot["updated_at"] = now
            snapshot["finished_at"] = now
            items = snapshot.get("items") if isinstance(snapshot.get("items"), list) else []
            for item in items:
                if isinstance(item, dict) and str(item.get("state") or "") in _SLOT_ITEM_STATES:
                    item["state"] = "interrupted"
                    item["terminal_at"] = now
            row.state = "interrupted"
            row.stop_reason = snapshot["stop_reason"]
            row.finished_at = now
            row.updated_at = datetime.now(timezone.utc)
            row.set_snapshot(snapshot)
            session.add(row)
            interrupted += 1
        session.commit()
    return interrupted


@dataclass
class _RuntimeGroup:
    snapshot: dict[str, Any]
    template: Any
    account_ledgers: dict[int, dict[int, dict[str, Any]]] = field(
        default_factory=dict
    )
    retry_handles: dict[int, Any] = field(default_factory=dict)
    technical_failure_events: list[tuple[float, str, int]] = field(
        default_factory=list
    )


StartTask = Callable[
    [str, str, int, int, Any, dict[str, Any], Callable[[str], None]],
    str,
]
StopTask = Callable[[str, str, str], None]
UpdateTaskMeta = Callable[[str, dict[str, Any]], None]
LogTask = Callable[[str, str, str], None]
PersistSnapshot = Callable[[dict[str, Any]], None]
LoadSnapshot = Callable[[str], dict[str, Any] | None]
ScheduleRetry = Callable[[float, Callable[[], None]], Any]


class RegistrationDomainRotationManager:
    """Keep a bounded number of domain tasks active and replace poor domains."""

    def __init__(
        self,
        *,
        start_task: StartTask,
        stop_task: StopTask,
        update_task_meta: UpdateTaskMeta,
        log_task: LogTask,
        persist_snapshot: PersistSnapshot = persist_rotation_group_snapshot,
        load_snapshot: LoadSnapshot = load_rotation_group_snapshot,
        schedule_retry: ScheduleRetry = _default_schedule_retry,
    ) -> None:
        self._start_task = start_task
        self._stop_task = stop_task
        self._update_task_meta = update_task_meta
        self._log_task = log_task
        self._persist_snapshot = persist_snapshot
        self._load_snapshot = load_snapshot
        self._schedule_retry = schedule_retry
        self._lock = threading.RLock()
        self._groups: dict[str, _RuntimeGroup] = {}
        self._task_to_group: dict[str, tuple[str, int]] = {}

    @staticmethod
    def _new_quality() -> dict[str, Any]:
        return {
            "registration_decisions": 0,
            "registration_accepted": 0,
            "registration_disallowed": 0,
            "registration_rejection_rate_percent": 0.0,
            "registered_accounts": 0,
            "link_success": 0,
            "link_quality_miss": 0,
            "link_technical_neutral": 0,
            "link_pending": 0,
            "link_current_miss_streak": 0,
        }

    def create_group(
        self,
        *,
        group_id: str,
        domains: list[str],
        template: Any,
        requested_count_per_task: int,
        requested_concurrency_per_task: int,
        active_domain_slots: int,
        rejection_rate_threshold_percent: float,
        rejection_rate_min_samples: int,
        no_link_streak_threshold: int,
    ) -> dict[str, Any]:
        normalized_group_id = str(group_id or "").strip()
        normalized_domains: list[str] = []
        seen_domains: set[str] = set()
        for raw_domain in domains:
            domain = str(raw_domain or "").strip().lower().lstrip("@.")
            if not domain or domain in seen_domains:
                continue
            seen_domains.add(domain)
            normalized_domains.append(domain)
        if not normalized_group_id or not normalized_domains:
            raise ValueError("rotation group id and domains are required")
        slots = min(_positive_int(active_domain_slots), len(normalized_domains))
        rejection_threshold = min(
            max(_float(rejection_rate_threshold_percent, 50.0), 0.0),
            100.0,
        )
        now = _now_iso()
        snapshot = {
            "task_group_id": normalized_group_id,
            "mode": ROTATION_MODE,
            "state": "running",
            "requested_domain_count": len(normalized_domains),
            "requested_count_per_task": _positive_int(requested_count_per_task),
            "requested_concurrency_per_task": _positive_int(
                requested_concurrency_per_task
            ),
            "active_domain_slots": slots,
            "policy": {
                "rejection_rate_threshold_percent": rejection_threshold,
                "rejection_rate_operator": ">",
                "rejection_rate_min_samples": _positive_int(
                    rejection_rate_min_samples,
                    10,
                ),
                "no_link_streak_threshold": _positive_int(
                    no_link_streak_threshold,
                    10,
                ),
                "technical_retry_limit": _TECHNICAL_RETRY_LIMIT,
                "technical_retry_backoff_seconds": list(
                    _TECHNICAL_RETRY_BACKOFF_SECONDS
                ),
                "technical_failure_circuit_distinct_domains": (
                    _TECHNICAL_FAILURE_CIRCUIT_DISTINCT_DOMAINS
                ),
                "technical_failure_circuit_window_seconds": (
                    _TECHNICAL_FAILURE_CIRCUIT_WINDOW_SECONDS
                ),
                "stop_mode": "after_current",
            },
            "items": [
                {
                    "domain": domain,
                    "position": position,
                    "state": "pending",
                    "task_id": "",
                    "quality": self._new_quality(),
                    "trigger": {},
                    "error": "",
                    "attempts": [],
                    "attempt_count": 0,
                    "retry_count": 0,
                    "retry_limit": _TECHNICAL_RETRY_LIMIT,
                    "next_retry_at": "",
                    "technical_failure": {},
                    "started_at": "",
                    "terminal_at": "",
                }
                for position, domain in enumerate(normalized_domains, start=1)
            ],
            "failure": {},
            "technical_failures": [],
            "stop_reason": "",
            "created_at": now,
            "updated_at": now,
            "finished_at": "",
        }
        with self._lock:
            if normalized_group_id in self._groups:
                raise ValueError("rotation group already exists")
            runtime = _RuntimeGroup(snapshot=snapshot, template=template)
            self._groups[normalized_group_id] = runtime
            if not self._persist_locked(runtime):
                self._groups.pop(normalized_group_id, None)
                raise RuntimeError("轮换任务组状态持久化失败，未启动任何域名任务")
            self._fill_slots_locked(runtime)
            self._finalize_if_idle_locked(runtime)
            return _public_group_snapshot(runtime.snapshot)

    def _persist_locked(self, runtime: _RuntimeGroup) -> bool:
        runtime.snapshot["updated_at"] = _now_iso()
        for retry_index in range(3):
            try:
                self._persist_snapshot(deepcopy(runtime.snapshot))
                return True
            except Exception:
                logger.warning(
                    "registration domain rotation persistence failed group_id=%s retry=%s",
                    runtime.snapshot.get("task_group_id"),
                    retry_index + 1,
                    exc_info=True,
                )
                if retry_index < 2:
                    time.sleep(0.05 * (retry_index + 1))
        return False

    @staticmethod
    def _items(runtime: _RuntimeGroup) -> list[dict[str, Any]]:
        raw = runtime.snapshot.get("items")
        return raw if isinstance(raw, list) else []

    def _item_for_position_locked(
        self,
        runtime: _RuntimeGroup,
        position: int,
    ) -> dict[str, Any] | None:
        return next(
            (
                item
                for item in self._items(runtime)
                if isinstance(item, dict) and _positive_int(item.get("position")) == position
            ),
            None,
        )

    def _bind_task_locked(
        self,
        runtime: _RuntimeGroup,
        item: dict[str, Any],
        task_id: str,
    ) -> None:
        normalized_task_id = str(task_id or "").strip()
        if not normalized_task_id:
            return
        attempts = item.setdefault("attempts", [])
        if not isinstance(attempts, list):
            attempts = []
            item["attempts"] = attempts
        attempt = next(
            (
                raw_attempt
                for raw_attempt in attempts
                if isinstance(raw_attempt, dict)
                and str(raw_attempt.get("task_id") or "").strip()
                == normalized_task_id
            ),
            None,
        )
        now = _now_iso()
        if attempt is None:
            attempt = {
                "task_id": normalized_task_id,
                "attempt": len(attempts) + 1,
                "state": "active",
                "error": "",
                "started_at": now,
                "terminal_at": "",
            }
            attempts.append(attempt)
        else:
            attempt["state"] = "active"
            attempt["started_at"] = str(attempt.get("started_at") or now)
        item["task_id"] = normalized_task_id
        item["state"] = "active"
        item["attempt_count"] = len(attempts)
        item["error"] = ""
        item["next_retry_at"] = ""
        item["terminal_at"] = ""
        item["started_at"] = item.get("started_at") or now
        group_id = str(runtime.snapshot.get("task_group_id") or "")
        self._task_to_group[normalized_task_id] = (
            group_id,
            _positive_int(item.get("position")),
        )
        self._persist_locked(runtime)

    def _child_group_meta_locked(
        self,
        runtime: _RuntimeGroup,
        item: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "id": str(runtime.snapshot.get("task_group_id") or ""),
            "mode": ROTATION_MODE,
            "domain": str(item.get("domain") or ""),
            "position": _positive_int(item.get("position")),
            "domain_count": _positive_int(runtime.snapshot.get("requested_domain_count")),
            "requested_count_per_task": _positive_int(
                runtime.snapshot.get("requested_count_per_task")
            ),
            "requested_concurrency_per_task": _positive_int(
                runtime.snapshot.get("requested_concurrency_per_task")
            ),
            "active_domain_slots": _positive_int(
                runtime.snapshot.get("active_domain_slots")
            ),
            "attempt": _nonnegative_int(item.get("attempt_count")) + 1,
            "technical_retry_count": _nonnegative_int(item.get("retry_count")),
            "rotation_policy": deepcopy(runtime.snapshot.get("policy") or {}),
        }

    def _fill_slots_locked(self, runtime: _RuntimeGroup) -> None:
        if str(runtime.snapshot.get("state") or "") != "running":
            return
        slots = _positive_int(runtime.snapshot.get("active_domain_slots"))
        while True:
            active = sum(
                1
                for item in self._items(runtime)
                if isinstance(item, dict) and str(item.get("state") or "") in _SLOT_ITEM_STATES
            )
            if active >= slots:
                return
            item = next(
                (
                    candidate
                    for candidate in self._items(runtime)
                    if isinstance(candidate, dict)
                    and str(candidate.get("state") or "") == "pending"
                ),
                None,
            )
            if item is None:
                return
            item["state"] = "starting"
            item["task_id"] = ""
            item["next_retry_at"] = ""
            item["terminal_at"] = ""
            item["started_at"] = item.get("started_at") or _now_iso()
            self._persist_locked(runtime)
            group_id = str(runtime.snapshot.get("task_group_id") or "")
            position = _positive_int(item.get("position"))

            def bind_task(task_id: str, *, _runtime=runtime, _item=item) -> None:
                with self._lock:
                    self._bind_task_locked(_runtime, _item, task_id)

            try:
                task_id = self._start_task(
                    group_id,
                    str(item.get("domain") or ""),
                    position,
                    _positive_int(runtime.snapshot.get("requested_domain_count")),
                    runtime.template,
                    self._child_group_meta_locked(runtime, item),
                    bind_task,
                )
                if not str(item.get("task_id") or "").strip():
                    self._bind_task_locked(runtime, item, task_id)
            except Exception as exc:
                item["state"] = "start_failed"
                item["error"] = sanitize_error_message(
                    exc or "域名任务创建失败"
                )[:1000]
                item["terminal_at"] = _now_iso()
                self._begin_group_failure_locked(
                    runtime,
                    failed_item=item,
                    code="domain_task_start_failed",
                    reason=(
                        f"域名 {item.get('domain') or '-'} 的注册任务创建失败；"
                        "已停止补位并收口活动任务"
                    ),
                )
                self._persist_locked(runtime)
                return

    def _runtime_item_for_task_locked(
        self,
        task_id: str,
    ) -> tuple[_RuntimeGroup, dict[str, Any]] | None:
        mapping = self._task_to_group.get(str(task_id or "").strip())
        if mapping is None:
            return None
        group_id, position = mapping
        runtime = self._groups.get(group_id)
        if runtime is None:
            return None
        item = self._item_for_position_locked(runtime, position)
        return (runtime, item) if item is not None else None

    @staticmethod
    def _attempt_for_task_locked(
        item: dict[str, Any],
        task_id: str,
    ) -> dict[str, Any] | None:
        attempts = item.get("attempts") if isinstance(item.get("attempts"), list) else []
        normalized_task_id = str(task_id or "").strip()
        return next(
            (
                attempt
                for attempt in attempts
                if isinstance(attempt, dict)
                and str(attempt.get("task_id") or "").strip() == normalized_task_id
            ),
            None,
        )

    @staticmethod
    def _set_attempt_terminal_locked(
        item: dict[str, Any],
        task_id: str,
        *,
        state: str,
        error: str = "",
    ) -> None:
        attempt = RegistrationDomainRotationManager._attempt_for_task_locked(
            item,
            task_id,
        )
        if attempt is None:
            return
        attempt["state"] = str(state or "failed")
        attempt["error"] = str(error or "")[:1000]
        attempt["terminal_at"] = _now_iso()

    @staticmethod
    def _terminal_error(task_snapshot: dict[str, Any], terminal_status: str) -> str:
        candidates: list[Any] = [task_snapshot.get("error")]
        errors = task_snapshot.get("errors")
        if isinstance(errors, list):
            candidates.extend(reversed(errors))
        for candidate in candidates:
            value = sanitize_error_message(candidate or "").strip()
            if value:
                return value[:1000]
        if terminal_status == "interrupted":
            return "注册任务意外中断"
        return "注册任务异常结束"

    @staticmethod
    def _retryable_task_failure(
        terminal_status: str,
        error: str,
    ) -> tuple[str, str] | None:
        if terminal_status == "interrupted":
            return ("task_interrupted", "注册任务意外中断")
        text = str(error or "").strip().lower()
        if not text:
            return None
        for code, label, markers in _TASK_FAILURE_CATEGORIES:
            if any(marker in text for marker in markers):
                return (code, label)
        return None

    @staticmethod
    def _task_has_business_progress(
        item: dict[str, Any],
        task_snapshot: dict[str, Any],
    ) -> bool:
        quality = item.get("quality") if isinstance(item.get("quality"), dict) else {}
        if any(
            _nonnegative_int(quality.get(key)) > 0
            for key in ("registration_decisions", "registered_accounts")
        ):
            return True
        if any(
            _nonnegative_int(task_snapshot.get(key)) > 0
            for key in ("success", "skipped")
        ):
            return True
        progress = str(task_snapshot.get("progress") or "").strip()
        if "/" in progress and _nonnegative_int(progress.split("/", 1)[0]) > 0:
            return True
        return False

    @staticmethod
    def _retry_delay_seconds(retry_count: int) -> float:
        index = min(
            max(_positive_int(retry_count) - 1, 0),
            len(_TECHNICAL_RETRY_BACKOFF_SECONDS) - 1,
        )
        return _TECHNICAL_RETRY_BACKOFF_SECONDS[index]

    @staticmethod
    def _cancel_retry_handle_locked(
        runtime: _RuntimeGroup,
        position: int,
    ) -> None:
        handle = runtime.retry_handles.pop(_positive_int(position), None)
        cancel = getattr(handle, "cancel", None)
        if callable(cancel):
            try:
                cancel()
            except Exception:
                logger.debug("registration domain retry cancellation failed", exc_info=True)

    def _cancel_all_retry_handles_locked(self, runtime: _RuntimeGroup) -> None:
        for position in list(runtime.retry_handles):
            self._cancel_retry_handle_locked(runtime, position)

    def _record_technical_failure_locked(
        self,
        runtime: _RuntimeGroup,
        item: dict[str, Any],
        *,
        task_id: str,
        code: str,
        label: str,
        error: str,
    ) -> int:
        policy = runtime.snapshot.get("policy") or {}
        window_seconds = max(
            _float(
                policy.get("technical_failure_circuit_window_seconds"),
                _TECHNICAL_FAILURE_CIRCUIT_WINDOW_SECONDS,
            ),
            1.0,
        )
        now_monotonic = time.monotonic()
        runtime.technical_failure_events = [
            event
            for event in runtime.technical_failure_events
            if now_monotonic - event[0] <= window_seconds
        ]
        position = _positive_int(item.get("position"))
        runtime.technical_failure_events.append((now_monotonic, code, position))
        occurred_at = _now_iso()
        event = {
            "domain": str(item.get("domain") or ""),
            "position": position,
            "task_id": str(task_id or ""),
            "attempt": _positive_int(item.get("attempt_count")),
            "code": str(code or "task_technical_failure"),
            "label": str(label or "技术故障"),
            "message": str(error or label or "技术故障")[:1000],
            "occurred_at": occurred_at,
        }
        failures = runtime.snapshot.setdefault("technical_failures", [])
        if not isinstance(failures, list):
            failures = []
            runtime.snapshot["technical_failures"] = failures
        failures.append(event)
        del failures[:-20]
        return len(
            {
                event_position
                for _, event_code, event_position in runtime.technical_failure_events
                if event_code == code
            }
        )

    @staticmethod
    def _mark_technical_recovery_locked(
        runtime: _RuntimeGroup,
        item: dict[str, Any],
    ) -> None:
        position = _positive_int(item.get("position"))
        runtime.technical_failure_events = [
            event
            for event in runtime.technical_failure_events
            if event[2] != position
        ]
        technical_failure = (
            item.get("technical_failure")
            if isinstance(item.get("technical_failure"), dict)
            else {}
        )
        if technical_failure and not technical_failure.get("recovered_at"):
            technical_failure["recovered_at"] = _now_iso()
            item["technical_failure"] = technical_failure

    def _schedule_technical_retry_locked(
        self,
        runtime: _RuntimeGroup,
        item: dict[str, Any],
        *,
        task_id: str,
        code: str,
        label: str,
        error: str,
    ) -> None:
        policy = runtime.snapshot.get("policy") or {}
        retry_limit = _nonnegative_int(
            policy.get("technical_retry_limit")
            if policy.get("technical_retry_limit") is not None
            else _TECHNICAL_RETRY_LIMIT
        )
        item["retry_limit"] = retry_limit
        distinct_domains = self._record_technical_failure_locked(
            runtime,
            item,
            task_id=task_id,
            code=code,
            label=label,
            error=error,
        )
        circuit_threshold = _positive_int(
            policy.get("technical_failure_circuit_distinct_domains"),
            _TECHNICAL_FAILURE_CIRCUIT_DISTINCT_DOMAINS,
        )
        circuit_window = max(
            _float(
                policy.get("technical_failure_circuit_window_seconds"),
                _TECHNICAL_FAILURE_CIRCUIT_WINDOW_SECONDS,
            ),
            1.0,
        )
        if distinct_domains >= circuit_threshold:
            item["state"] = "technical_failed"
            item["error"] = error
            item["technical_failure"] = {
                "code": code,
                "label": label,
                "message": error,
                "retry_count": _nonnegative_int(item.get("retry_count")),
                "retry_limit": retry_limit,
                "occurred_at": _now_iso(),
            }
            self._begin_group_failure_locked(
                runtime,
                failed_item=item,
                code="technical_failure_circuit_open",
                reason=(
                    f"{circuit_window:g} 秒内已有 {distinct_domains} 个域名出现同类{label}；"
                    "已停止补位并收口活动任务，避免公共依赖故障扩散"
                ),
            )
            return

        current_retry_count = _nonnegative_int(item.get("retry_count"))
        if current_retry_count >= retry_limit:
            item["state"] = "technical_failed"
            item["error"] = error
            item["technical_failure"] = {
                "code": code,
                "label": label,
                "message": error,
                "retry_count": current_retry_count,
                "retry_limit": retry_limit,
                "occurred_at": _now_iso(),
            }
            self._begin_group_failure_locked(
                runtime,
                failed_item=item,
                code="technical_retry_exhausted",
                reason=(
                    f"域名 {item.get('domain') or '-'} 遇到{label}，"
                    f"同域自动重试 {current_retry_count} 次后仍失败；"
                    "已停止补位并收口活动任务"
                ),
            )
            return

        retry_count = current_retry_count + 1
        delay_seconds = self._retry_delay_seconds(retry_count)
        retry_at = (
            datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
        ).isoformat()
        item["state"] = "retry_wait"
        item["retry_count"] = retry_count
        item["error"] = error
        item["terminal_at"] = ""
        item["next_retry_at"] = retry_at
        item["technical_failure"] = {
            "code": code,
            "label": label,
            "message": error,
            "retry_count": retry_count,
            "retry_limit": retry_limit,
            "retry_at": retry_at,
            "occurred_at": _now_iso(),
        }
        self._persist_locked(runtime)
        self._publish_task_meta_locked(runtime, item)

        group_id = str(runtime.snapshot.get("task_group_id") or "")
        position = _positive_int(item.get("position"))

        def retry_callback() -> None:
            with self._lock:
                live_runtime = self._groups.get(group_id)
                if live_runtime is not runtime:
                    return
                self._cancel_retry_handle_locked(runtime, position)
                live_item = self._item_for_position_locked(runtime, position)
                if live_item is None:
                    return
                if str(runtime.snapshot.get("state") or "") != "running":
                    return
                if str(live_item.get("state") or "") != "retry_wait":
                    return
                if _nonnegative_int(live_item.get("retry_count")) != retry_count:
                    return
                live_item["state"] = "pending"
                live_item["task_id"] = ""
                live_item["next_retry_at"] = ""
                self._persist_locked(runtime)
                self._fill_slots_locked(runtime)
                new_task_id = str(live_item.get("task_id") or "").strip()
                if new_task_id:
                    try:
                        self._log_task(
                            new_task_id,
                            (
                                f"[域名轮换] 上一任务遇到{label}；"
                                f"正在执行同域技术重试 {retry_count}/{retry_limit}"
                            ),
                            "warning",
                        )
                    except Exception:
                        logger.debug(
                            "registration domain retry log skipped task_id=%s",
                            new_task_id,
                            exc_info=True,
                        )
                self._persist_locked(runtime)
                self._publish_task_meta_locked(runtime, live_item)
                self._finalize_if_idle_locked(runtime)

        try:
            runtime.retry_handles[position] = self._schedule_retry(
                delay_seconds,
                retry_callback,
            )
        except Exception as exc:
            item["state"] = "technical_failed"
            item["error"] = sanitize_error_message(exc or error)[:1000]
            item["next_retry_at"] = ""
            self._begin_group_failure_locked(
                runtime,
                failed_item=item,
                code="technical_retry_schedule_failed",
                reason=(
                    f"域名 {item.get('domain') or '-'} 的技术重试无法调度；"
                    "已停止补位并收口活动任务"
                ),
            )

    def _publish_task_meta_locked(
        self,
        runtime: _RuntimeGroup,
        item: dict[str, Any],
    ) -> None:
        task_id = str(item.get("task_id") or "").strip()
        if not task_id:
            return
        public = _public_group_snapshot(runtime.snapshot)
        try:
            self._update_task_meta(
                task_id,
                {
                    "registration_domain_rotation": {
                        "group_id": public["task_group_id"],
                        "group_state": public["state"],
                        "domain_state": str(item.get("state") or ""),
                        "quality": deepcopy(item.get("quality") or {}),
                        "trigger": deepcopy(item.get("trigger") or {}),
                        "attempt_count": _nonnegative_int(item.get("attempt_count")),
                        "retry_count": _nonnegative_int(item.get("retry_count")),
                        "retry_limit": _nonnegative_int(item.get("retry_limit")),
                        "next_retry_at": str(item.get("next_retry_at") or ""),
                        "technical_failure": deepcopy(
                            item.get("technical_failure") or {}
                        ),
                        "policy": deepcopy(public.get("policy") or {}),
                        "counts": deepcopy(public.get("counts") or {}),
                    }
                },
            )
        except Exception:
            logger.debug(
                "registration domain rotation task meta update skipped task_id=%s",
                task_id,
                exc_info=True,
            )

    def _trigger_quality_stop_locked(
        self,
        runtime: _RuntimeGroup,
        item: dict[str, Any],
        *,
        code: str,
        message: str,
    ) -> None:
        if str(item.get("state") or "") != "active":
            return
        task_id = str(item.get("task_id") or "").strip()
        if not task_id:
            return
        item["state"] = "draining"
        item["trigger"] = {
            "code": str(code or "domain_quality_threshold"),
            "message": str(message or "域名质量阈值已触发")[:1000],
            "triggered_at": _now_iso(),
        }
        self._persist_locked(runtime)
        self._publish_task_meta_locked(runtime, item)
        try:
            self._log_task(task_id, f"[域名轮换] {message}", "warning")
            self._log_task(
                task_id,
                "[域名轮换] 已请求完成当前账号后停止；停止收口后将启动下一个等待域名",
                "warning",
            )
            self._stop_task(task_id, "after_current", code)
        except Exception:
            logger.warning(
                "registration domain rotation graceful stop failed task_id=%s",
                task_id,
                exc_info=True,
            )

    def record_registration_result(
        self,
        task_id: str,
        *,
        decision: str,
    ) -> None:
        normalized_decision = str(decision or "").strip().lower()
        if normalized_decision not in {"accepted", "registration_disallowed"}:
            return
        with self._lock:
            resolved = self._runtime_item_for_task_locked(task_id)
            if resolved is None:
                return
            runtime, item = resolved
            self._mark_technical_recovery_locked(runtime, item)
            quality = item.setdefault("quality", self._new_quality())
            quality["registration_decisions"] = _nonnegative_int(
                quality.get("registration_decisions")
            ) + 1
            key = (
                "registration_disallowed"
                if normalized_decision == "registration_disallowed"
                else "registration_accepted"
            )
            quality[key] = _nonnegative_int(quality.get(key)) + 1
            decisions = _nonnegative_int(quality.get("registration_decisions"))
            disallowed = _nonnegative_int(quality.get("registration_disallowed"))
            rejection_rate = (100.0 * disallowed / decisions) if decisions else 0.0
            quality["registration_rejection_rate_percent"] = round(rejection_rate, 1)
            self._persist_locked(runtime)
            self._publish_task_meta_locked(runtime, item)
            policy = runtime.snapshot.get("policy") or {}
            minimum = _positive_int(policy.get("rejection_rate_min_samples"), 10)
            threshold = _float(policy.get("rejection_rate_threshold_percent"), 50.0)
            if decisions >= minimum and rejection_rate > threshold:
                self._trigger_quality_stop_locked(
                    runtime,
                    item,
                    code="registration_rejection_rate_exceeded",
                    message=(
                        "开户拒绝率触发阈值："
                        f"{disallowed}/{decisions}={rejection_rate:.1f}% > {threshold:g}%"
                        f"（最小样本 {minimum}）"
                    ),
                )

    def record_registered_account(
        self,
        task_id: str,
        *,
        account_id: int,
        attempt_order: int,
    ) -> None:
        normalized_account_id = _positive_int(account_id, 0)
        if normalized_account_id <= 0:
            return
        with self._lock:
            resolved = self._runtime_item_for_task_locked(task_id)
            if resolved is None:
                return
            runtime, item = resolved
            self._mark_technical_recovery_locked(runtime, item)
            accounts = self._accounts_for_item_locked(runtime, item)
            accounts.setdefault(
                normalized_account_id,
                {
                    "account_id": normalized_account_id,
                    "attempt_order": _positive_int(attempt_order),
                    "state": "pending",
                    "reason_code": "registration_succeeded",
                    "updated_at": _now_iso(),
                },
            )
            self._recompute_link_quality_locked(runtime, item)
            self._persist_locked(runtime)
            self._publish_task_meta_locked(runtime, item)

    def _accounts_for_item_locked(
        self,
        runtime: _RuntimeGroup,
        item: dict[str, Any],
    ) -> dict[int, dict[str, Any]]:
        position = _positive_int(item.get("position"))
        return runtime.account_ledgers.setdefault(position, {})

    def _account_for_result_locked(
        self,
        runtime: _RuntimeGroup,
        item: dict[str, Any],
        account_id: int,
    ) -> dict[str, Any] | None:
        return self._accounts_for_item_locked(runtime, item).get(account_id)

    @staticmethod
    def _set_account_outcome(
        account: dict[str, Any],
        *,
        state: str,
        reason_code: str,
    ) -> None:
        account["state"] = str(state or "pending")
        account["reason_code"] = str(reason_code or "")[:128]
        account["updated_at"] = _now_iso()

    def record_eligibility_result(self, task_id: str, result: dict[str, Any]) -> None:
        account_id = _positive_int(result.get("account_id"), 0)
        if account_id <= 0:
            return
        state = str(result.get("state") or "probe_failed").strip().lower()
        reason_code = str(result.get("reason_code") or state).strip().lower()
        with self._lock:
            resolved = self._runtime_item_for_task_locked(task_id)
            if resolved is None:
                return
            runtime, item = resolved
            account = self._account_for_result_locked(runtime, item, account_id)
            if account is None:
                return
            current_state = str(account.get("state") or "pending")
            if state == "eligible":
                if current_state in {"pending", "awaiting_link"}:
                    self._set_account_outcome(
                        account,
                        state="awaiting_link",
                        reason_code=reason_code,
                    )
            elif state == "ineligible":
                if current_state != "success":
                    self._set_account_outcome(
                        account,
                        state="miss",
                        reason_code=reason_code,
                    )
            elif state in {"probe_failed", "skipped"}:
                if current_state not in {"success", "miss"}:
                    self._set_account_outcome(
                        account,
                        state="neutral",
                        reason_code=reason_code,
                    )
            elif current_state not in {"success", "miss", "neutral"}:
                self._set_account_outcome(account, state="pending", reason_code=reason_code)
            self._after_link_outcome_locked(runtime, item)

    @staticmethod
    def _technical_link_failure(reason_code: str, message: str) -> bool:
        reason = str(reason_code or "").strip().lower()
        if reason in _TECHNICAL_LINK_REASON_CODES:
            return True
        text = str(message or "").strip().lower()
        return any(marker in text for marker in _TECHNICAL_LINK_MESSAGE_MARKERS)

    def record_link_result(self, task_id: str, result: dict[str, Any]) -> None:
        account_id = _positive_int(result.get("account_id"), 0)
        if account_id <= 0:
            return
        state = str(result.get("state") or "").strip().lower()
        reason_code = str(result.get("reason_code") or state).strip().lower()
        message = str(result.get("message") or result.get("error") or "")
        with self._lock:
            resolved = self._runtime_item_for_task_locked(task_id)
            if resolved is None:
                return
            runtime, item = resolved
            account = self._account_for_result_locked(runtime, item, account_id)
            if account is None:
                return
            current_state = str(account.get("state") or "pending")
            if state in {"link_succeeded", "submitted", "submit_failed"}:
                self._set_account_outcome(account, state="success", reason_code=reason_code)
            elif state == "extract_failed" and current_state != "success":
                outcome = (
                    "neutral"
                    if self._technical_link_failure(reason_code, message)
                    else "miss"
                )
                self._set_account_outcome(account, state=outcome, reason_code=reason_code)
            elif state == "pending_auth" and current_state not in {
                "success",
                "miss",
                "neutral",
            }:
                self._set_account_outcome(account, state="pending", reason_code=reason_code)
            elif current_state not in {"success", "miss"}:
                self._set_account_outcome(account, state="neutral", reason_code=reason_code)
            self._after_link_outcome_locked(runtime, item)

    def _recompute_link_quality_locked(
        self,
        runtime: _RuntimeGroup,
        item: dict[str, Any],
    ) -> None:
        accounts = list(self._accounts_for_item_locked(runtime, item).values())
        accounts.sort(
            key=lambda account: (
                _positive_int(account.get("attempt_order")),
                _positive_int(account.get("account_id")),
            )
        )
        states = Counter(str(account.get("state") or "pending") for account in accounts)
        terminal = [
            str(account.get("state") or "")
            for account in accounts
            if str(account.get("state") or "") in {"success", "miss"}
        ]
        streak = 0
        for state in reversed(terminal):
            if state == "success":
                break
            streak += 1
        quality = item.setdefault("quality", self._new_quality())
        quality["registered_accounts"] = len(accounts)
        quality["link_success"] = states.get("success", 0)
        quality["link_quality_miss"] = states.get("miss", 0)
        quality["link_technical_neutral"] = states.get("neutral", 0)
        quality["link_pending"] = states.get("pending", 0) + states.get(
            "awaiting_link",
            0,
        )
        quality["link_current_miss_streak"] = streak

    def _after_link_outcome_locked(
        self,
        runtime: _RuntimeGroup,
        item: dict[str, Any],
    ) -> None:
        self._recompute_link_quality_locked(runtime, item)
        self._persist_locked(runtime)
        self._publish_task_meta_locked(runtime, item)
        quality = item.get("quality") or {}
        streak = _nonnegative_int(quality.get("link_current_miss_streak"))
        threshold = _positive_int(
            (runtime.snapshot.get("policy") or {}).get("no_link_streak_threshold"),
            10,
        )
        if streak >= threshold:
            self._trigger_quality_stop_locked(
                runtime,
                item,
                code="no_payment_link_streak_reached",
                message=f"连续 {streak} 个业务终态未提链成功，达到阈值 {threshold}",
            )

    def handle_task_terminal(self, task_id: str, task_snapshot: dict[str, Any]) -> None:
        with self._lock:
            resolved = self._runtime_item_for_task_locked(task_id)
            if resolved is None:
                return
            runtime, item = resolved
            self._task_to_group.pop(str(task_id or "").strip(), None)
            previous_state = str(item.get("state") or "")
            terminal_status = str(task_snapshot.get("status") or "stopped").strip().lower()
            terminal_error = self._terminal_error(task_snapshot, terminal_status)
            group_state = str(runtime.snapshot.get("state") or "")
            retryable_failure = self._retryable_task_failure(
                terminal_status,
                terminal_error,
            )
            has_business_progress = self._task_has_business_progress(
                item,
                task_snapshot,
            )
            abnormal_terminal = terminal_status in {"failed", "interrupted"}

            if (
                abnormal_terminal
                and not item.get("trigger")
                and group_state == "running"
                and retryable_failure is not None
                and not has_business_progress
            ):
                code, label = retryable_failure
                self._set_attempt_terminal_locked(
                    item,
                    task_id,
                    state="technical_failed",
                    error=terminal_error,
                )
                item["terminal_at"] = _now_iso()
                self._schedule_technical_retry_locked(
                    runtime,
                    item,
                    task_id=task_id,
                    code=code,
                    label=label,
                    error=terminal_error,
                )
                if str(item.get("state") or "") != "retry_wait":
                    runtime.account_ledgers.pop(
                        _positive_int(item.get("position")),
                        None,
                    )
                self._persist_locked(runtime)
                self._publish_task_meta_locked(runtime, item)
                self._fill_slots_locked(runtime)
                self._finalize_if_idle_locked(runtime)
                return

            if item.get("trigger"):
                item["state"] = "quality_rejected"
            elif group_state == "stopping":
                item["state"] = "stopped"
            elif group_state == "failing":
                if terminal_status == "done":
                    item["state"] = "completed"
                elif terminal_status in {"failed", "interrupted"}:
                    item["state"] = terminal_status
                else:
                    item["state"] = "stopped"
            elif terminal_status == "done":
                item["state"] = "completed"
                self._mark_technical_recovery_locked(runtime, item)
            elif terminal_status == "failed":
                item["state"] = "failed"
                item["error"] = terminal_error
            elif terminal_status == "interrupted":
                item["state"] = "interrupted"
                item["error"] = terminal_error
            else:
                item["state"] = "stopped"
            item["terminal_at"] = _now_iso()
            item["next_retry_at"] = ""
            self._set_attempt_terminal_locked(
                item,
                task_id,
                state=str(item.get("state") or terminal_status),
                error=(terminal_error if abnormal_terminal else ""),
            )
            runtime.account_ledgers.pop(_positive_int(item.get("position")), None)

            if (
                abnormal_terminal
                and not item.get("trigger")
                and group_state == "running"
            ):
                if retryable_failure is not None and has_business_progress:
                    _, label = retryable_failure
                    failure_code = "technical_failure_after_business_progress"
                    failure_reason = (
                        f"域名 {item.get('domain') or '-'} 在已有业务进度后遇到{label}；"
                        "为避免整任务重放导致目标超发，已停止补位并收口活动任务"
                    )
                else:
                    failure_code = (
                        "domain_task_interrupted"
                        if terminal_status == "interrupted"
                        else "domain_task_failed"
                    )
                    failure_reason = (
                        f"域名 {item.get('domain') or '-'} 的注册任务出现不可恢复异常；"
                        "已停止补位并收口活动任务"
                    )
                self._begin_group_failure_locked(
                    runtime,
                    failed_item=item,
                    code=failure_code,
                    reason=failure_reason,
                )
            self._persist_locked(runtime)
            self._publish_task_meta_locked(runtime, item)
            if previous_state == "draining" and item.get("trigger"):
                trigger = item.get("trigger") or {}
                try:
                    self._log_task(
                        task_id,
                        "[域名轮换] 当前域名已完成停止收口，正在释放轮换槽位",
                        "warning",
                    )
                except Exception:
                    logger.warning(
                        "registration domain terminal log failed task_id=%s",
                        task_id,
                        exc_info=True,
                    )
                logger.info(
                    "registration domain rejected task_id=%s code=%s",
                    task_id,
                    trigger.get("code"),
                )
            self._fill_slots_locked(runtime)
            self._finalize_if_idle_locked(runtime)

    @staticmethod
    def _cancel_pending_locked(runtime: _RuntimeGroup) -> None:
        now = _now_iso()
        for item in RegistrationDomainRotationManager._items(runtime):
            if not isinstance(item, dict):
                continue
            if str(item.get("state") or "") != "pending":
                continue
            item["state"] = "cancelled"
            item["terminal_at"] = now

    def _begin_group_failure_locked(
        self,
        runtime: _RuntimeGroup,
        *,
        failed_item: dict[str, Any],
        code: str,
        reason: str,
    ) -> None:
        domain = str(failed_item.get("domain") or "-")
        runtime.snapshot["state"] = "failing"
        safe_reason = sanitize_error_message(
            reason
            or (
                f"域名 {domain} 的注册任务异常结束；"
                "轮换已停止补位并收口活动任务"
            )
        )[:1000]
        runtime.snapshot["stop_reason"] = safe_reason
        runtime.snapshot["failure"] = {
            "code": str(code or "rotation_group_task_failed")[:128],
            "domain": domain,
            "message": safe_reason,
            "error": str(failed_item.get("error") or "")[:1000],
            "occurred_at": _now_iso(),
        }
        self._cancel_pending_locked(runtime)
        self._cancel_all_retry_handles_locked(runtime)
        now = _now_iso()
        for item in self._items(runtime):
            if not isinstance(item, dict) or item is failed_item:
                continue
            state = str(item.get("state") or "")
            if state == "retry_wait":
                item["state"] = "technical_failed"
                item["next_retry_at"] = ""
                item["terminal_at"] = now
                item["error"] = str(
                    item.get("error") or "同组基础设施熔断，已取消技术重试"
                )[:1000]
                continue
            if state not in _LIVE_TASK_ITEM_STATES:
                continue
            child_task_id = str(item.get("task_id") or "").strip()
            if not child_task_id:
                item["state"] = "stopped"
                item["terminal_at"] = now
                continue
            item["state"] = "draining"
            try:
                self._log_task(
                    child_task_id,
                    f"[域名轮换] {safe_reason}；当前任务将在完成在途账号后停止",
                    "warning",
                )
                self._stop_task(
                    child_task_id,
                    "after_current",
                    "rotation_group_task_failed",
                )
            except Exception:
                logger.warning(
                    "registration domain failure drain failed task_id=%s",
                    child_task_id,
                    exc_info=True,
                )

    def _finalize_if_idle_locked(self, runtime: _RuntimeGroup) -> None:
        items = self._items(runtime)
        active = any(
            isinstance(item, dict) and str(item.get("state") or "") in _SLOT_ITEM_STATES
            for item in items
        )
        pending = any(
            isinstance(item, dict) and str(item.get("state") or "") == "pending"
            for item in items
        )
        if active or pending:
            return
        current_state = str(runtime.snapshot.get("state") or "")
        if current_state == "stopping":
            final_state = "stopped"
        elif current_state == "failing":
            final_state = "failed"
        elif current_state in _TERMINAL_GROUP_STATES:
            final_state = current_state
        elif any(str(item.get("task_id") or "") for item in items if isinstance(item, dict)):
            final_state = "completed"
        else:
            final_state = "failed"
        runtime.snapshot["state"] = final_state
        runtime.snapshot["finished_at"] = runtime.snapshot.get("finished_at") or _now_iso()
        persisted = self._persist_locked(runtime)
        self._retire_runtime_locked(runtime, remove=bool(persisted))

    def _retire_runtime_locked(
        self,
        runtime: _RuntimeGroup,
        *,
        remove: bool,
    ) -> None:
        self._cancel_all_retry_handles_locked(runtime)
        runtime.template = None
        runtime.account_ledgers.clear()
        runtime.technical_failure_events.clear()
        group_id = str(runtime.snapshot.get("task_group_id") or "")
        self._task_to_group = {
            task_id: mapping
            for task_id, mapping in self._task_to_group.items()
            if mapping[0] != group_id
        }
        if remove and self._groups.get(group_id) is runtime:
            self._groups.pop(group_id, None)

    def stop_group(
        self,
        group_id: str,
        *,
        mode: str = "after_current",
        reason: str = "用户停止域名轮换任务组",
    ) -> dict[str, Any]:
        normalized_group_id = str(group_id or "").strip()
        normalized_mode = "immediate" if str(mode or "") == "immediate" else "after_current"
        with self._lock:
            runtime = self._groups.get(normalized_group_id)
            if runtime is None:
                snapshot = self._load_snapshot(normalized_group_id)
                if snapshot is None:
                    raise KeyError(normalized_group_id)
                return _public_group_snapshot(snapshot)
            if str(runtime.snapshot.get("state") or "") in _TERMINAL_GROUP_STATES:
                return _public_group_snapshot(runtime.snapshot)
            runtime.snapshot["state"] = "stopping"
            runtime.snapshot["stop_reason"] = str(reason or "用户停止域名轮换任务组")[:1000]
            self._cancel_all_retry_handles_locked(runtime)
            for item in self._items(runtime):
                if not isinstance(item, dict):
                    continue
                state = str(item.get("state") or "")
                if state == "pending":
                    item["state"] = "cancelled"
                    item["terminal_at"] = _now_iso()
                    continue
                if state == "retry_wait":
                    item["state"] = "stopped"
                    item["next_retry_at"] = ""
                    item["terminal_at"] = _now_iso()
                    continue
                if state not in _LIVE_TASK_ITEM_STATES:
                    continue
                task_id = str(item.get("task_id") or "").strip()
                if not task_id:
                    item["state"] = "stopped"
                    item["terminal_at"] = _now_iso()
                    continue
                item["state"] = "draining"
                try:
                    self._log_task(
                        task_id,
                        f"[域名轮换] 任务组停止｜模式={normalized_mode}",
                        "warning",
                    )
                    self._stop_task(task_id, normalized_mode, "rotation_group_stop")
                except Exception:
                    logger.warning(
                        "registration domain rotation group stop failed task_id=%s",
                        task_id,
                        exc_info=True,
                    )
            self._persist_locked(runtime)
            self._finalize_if_idle_locked(runtime)
            return _public_group_snapshot(runtime.snapshot)

    def get_group(self, group_id: str) -> dict[str, Any] | None:
        normalized_group_id = str(group_id or "").strip()
        with self._lock:
            runtime = self._groups.get(normalized_group_id)
            if runtime is not None:
                public = _public_group_snapshot(runtime.snapshot)
                if str(runtime.snapshot.get("state") or "") in _TERMINAL_GROUP_STATES:
                    if self._persist_locked(runtime):
                        self._retire_runtime_locked(runtime, remove=True)
                return public
        snapshot = self._load_snapshot(normalized_group_id)
        return _public_group_snapshot(snapshot) if snapshot else None

    def reset_runtime_for_tests(self) -> None:
        with self._lock:
            for runtime in self._groups.values():
                self._cancel_all_retry_handles_locked(runtime)
            self._groups.clear()
            self._task_to_group.clear()
