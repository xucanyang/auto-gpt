"""Quality-gated scheduler for rotating TempMail registration domains."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
_ACTIVE_ITEM_STATES = frozenset({"starting", "active", "draining"})
_ACTIVE_GROUP_STATES = frozenset({"running", "stopping", "failing"})
_TERMINAL_GROUP_STATES = frozenset({"completed", "stopped", "failed", "interrupted"})
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


def _public_group_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    items = snapshot.get("items") if isinstance(snapshot.get("items"), list) else []
    public_items: list[dict[str, Any]] = []
    tasks: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
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
            "started_at": str(item.get("started_at") or ""),
            "terminal_at": str(item.get("terminal_at") or ""),
        }
        public_items.append(public_item)
        if public_item["task_id"]:
            tasks.append(dict(public_item))
        if state == "start_failed":
            errors.append(
                {
                    "domain": public_item["domain"],
                    "position": public_item["position"],
                    "message": str(item.get("error") or "域名任务创建失败"),
                }
            )

    requested_count = _positive_int(snapshot.get("requested_domain_count"), len(public_items) or 1)
    return {
        "task_group_id": str(snapshot.get("task_group_id") or ""),
        "mode": ROTATION_MODE,
        "state": str(snapshot.get("state") or "interrupted"),
        "requested_domain_count": requested_count,
        "created_count": len(tasks),
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
                if isinstance(item, dict) and str(item.get("state") or "") in _ACTIVE_ITEM_STATES:
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


StartTask = Callable[
    [str, str, int, int, Any, dict[str, Any], Callable[[str], None]],
    str,
]
StopTask = Callable[[str, str, str], None]
UpdateTaskMeta = Callable[[str, dict[str, Any]], None]
LogTask = Callable[[str, str, str], None]
PersistSnapshot = Callable[[dict[str, Any]], None]
LoadSnapshot = Callable[[str], dict[str, Any] | None]


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
    ) -> None:
        self._start_task = start_task
        self._stop_task = stop_task
        self._update_task_meta = update_task_meta
        self._log_task = log_task
        self._persist_snapshot = persist_snapshot
        self._load_snapshot = load_snapshot
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
                    "started_at": "",
                    "terminal_at": "",
                }
                for position, domain in enumerate(normalized_domains, start=1)
            ],
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
        item["task_id"] = normalized_task_id
        item["state"] = "active"
        item["started_at"] = item.get("started_at") or _now_iso()
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
                if isinstance(item, dict) and str(item.get("state") or "") in _ACTIVE_ITEM_STATES
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
            item["started_at"] = _now_iso()
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
                self._persist_locked(runtime)

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
            if item.get("trigger"):
                item["state"] = "quality_rejected"
            elif str(runtime.snapshot.get("state") or "") == "stopping":
                item["state"] = "stopped"
            elif str(runtime.snapshot.get("state") or "") == "failing":
                if terminal_status == "done":
                    item["state"] = "completed"
                elif terminal_status in {"failed", "interrupted"}:
                    item["state"] = terminal_status
                else:
                    item["state"] = "stopped"
            elif terminal_status == "done":
                item["state"] = "completed"
            elif terminal_status == "failed":
                item["state"] = "failed"
                item["error"] = sanitize_error_message(
                    task_snapshot.get("error") or ""
                )[:1000]
            elif terminal_status == "interrupted":
                item["state"] = "interrupted"
            else:
                item["state"] = "stopped"
            item["terminal_at"] = _now_iso()
            runtime.account_ledgers.pop(_positive_int(item.get("position")), None)

            if (
                terminal_status == "failed"
                and not item.get("trigger")
                and str(runtime.snapshot.get("state") or "") == "running"
            ):
                self._begin_group_failure_locked(runtime, failed_item=item)
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
    ) -> None:
        domain = str(failed_item.get("domain") or "-")
        runtime.snapshot["state"] = "failing"
        runtime.snapshot["stop_reason"] = (
            f"域名 {domain} 的注册任务异常结束；轮换已停止补位，"
            "避免把代理或公共依赖故障误判为域名质量"
        )[:1000]
        self._cancel_pending_locked(runtime)
        for item in self._items(runtime):
            if not isinstance(item, dict) or item is failed_item:
                continue
            if str(item.get("state") or "") not in _ACTIVE_ITEM_STATES:
                continue
            child_task_id = str(item.get("task_id") or "").strip()
            if not child_task_id:
                continue
            item["state"] = "draining"
            try:
                self._log_task(
                    child_task_id,
                    "[域名轮换] 同组任务异常结束，当前任务将在完成在途账号后停止",
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
            isinstance(item, dict) and str(item.get("state") or "") in _ACTIVE_ITEM_STATES
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
        runtime.template = None
        runtime.account_ledgers.clear()
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
            for item in self._items(runtime):
                if not isinstance(item, dict):
                    continue
                state = str(item.get("state") or "")
                if state == "pending":
                    item["state"] = "cancelled"
                    item["terminal_at"] = _now_iso()
                    continue
                if state not in _ACTIVE_ITEM_STATES:
                    continue
                task_id = str(item.get("task_id") or "").strip()
                if not task_id:
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
            self._groups.clear()
            self._task_to_group.clear()
