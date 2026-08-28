"""注册任务运行时控制与状态存储。"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import Enum
import logging
import threading
import time
from typing import Any, Callable


logger = logging.getLogger(__name__)


class TaskInterruption(RuntimeError):
    """任务执行过程中触发的协作式中断。"""


class StopTaskRequested(TaskInterruption):
    """整个任务被手动停止。"""

    def __init__(self, message: str = "任务已手动停止"):
        super().__init__(message)


class SkipCurrentAttemptRequested(TaskInterruption):
    """当前账号被手动跳过。"""

    def __init__(self, message: str = "已手动跳过当前账号"):
        super().__init__(message)


TERMINAL_TASK_STATUSES = frozenset({"done", "failed", "stopped", "partial", "interrupted"})

DEFAULT_ACTIVE_MAX_LOG_ENTRIES = 4000
DEFAULT_ACTIVE_MAX_LOG_BYTES = 4 * 1024 * 1024
DEFAULT_FINISHED_MAX_LOG_ENTRIES = 500
DEFAULT_FINISHED_MAX_LOG_BYTES = 512 * 1024


def _log_entry_bytes(entry: str) -> int:
    return len(str(entry or "").encode("utf-8", errors="replace"))


class AttemptOutcome(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    STOPPED = "stopped"
    NOT_STARTED = "not_started"


@dataclass(slots=True)
class AttemptResult:
    outcome: AttemptOutcome
    message: str = ""
    consumes_target_slot: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def success(cls, *, metadata: dict[str, Any] | None = None) -> "AttemptResult":
        return cls(AttemptOutcome.SUCCESS, metadata=dict(metadata or {}))

    @classmethod
    def failed(
        cls,
        message: str,
        *,
        consumes_target_slot: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> "AttemptResult":
        return cls(
            AttemptOutcome.FAILED,
            message,
            consumes_target_slot=consumes_target_slot,
            metadata=dict(metadata or {}),
        )

    @classmethod
    def skipped(
        cls,
        message: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> "AttemptResult":
        return cls(AttemptOutcome.SKIPPED, message, metadata=dict(metadata or {}))

    @classmethod
    def stopped(cls, message: str) -> "AttemptResult":
        return cls(AttemptOutcome.STOPPED, message)

    @classmethod
    def not_started(cls) -> "AttemptResult":
        """A queued attempt was prevented from starting by graceful stop."""
        return cls(AttemptOutcome.NOT_STARTED)


@dataclass(slots=True)
class VerificationChallenge:
    challenge_id: str
    attempt_id: int | None
    phase: str
    phase_label: str
    email: str
    timeout_seconds: int
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    status: str = "pending"
    code: str = ""
    cancel_reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    actions: list[str] = field(default_factory=list)
    action_request: dict[str, Any] = field(default_factory=dict)
    action_seq: int = 0

    def __post_init__(self) -> None:
        if not self.expires_at:
            self.expires_at = self.created_at + max(int(self.timeout_seconds or 0), 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "challenge_id": self.challenge_id,
            "attempt_id": self.attempt_id,
            "phase": self.phase,
            "phase_label": self.phase_label,
            "email": self.email,
            "timeout_seconds": self.timeout_seconds,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "status": self.status,
            "metadata": dict(self.metadata or {}),
            "actions": list(self.actions or []),
            "action_request": dict(self.action_request or {}),
            "action_seq": int(self.action_seq or 0),
        }


class RegisterTaskControl:
    """协作式任务控制器：支持立即停止、排空当前尝试、跳过和人工验证码等待。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._stop_requested = False
        self._after_current_requested = False
        self._pending_skip_requests = 0
        self._next_attempt_id = 1
        self._next_challenge_id = 1
        self._active_attempt_ids: set[int] = set()
        self._skip_active_attempt_ids: set[int] = set()
        self._pending_verification: VerificationChallenge | None = None

    def _cancel_pending_verification(self, reason: str) -> None:
        challenge = self._pending_verification
        if challenge is None or challenge.status != "pending":
            return
        challenge.status = "cancelled"
        challenge.cancel_reason = str(reason or "cancelled")
        self._condition.notify_all()

    def request_stop(self) -> bool:
        """Request an immediate cooperative interruption.

        ``True`` means this call changed the control state.  The return value
        lets the API keep repeated clicks idempotent without writing duplicate
        control lines to the persisted task history.
        """
        with self._condition:
            changed = not self._stop_requested
            # Immediate stop always wins over an earlier graceful request.
            self._stop_requested = True
            self._after_current_requested = False
            self._cancel_pending_verification("stop_requested")
            self._condition.notify_all()
            return changed

    def request_stop_after_current(self) -> bool:
        """Stop scheduling new attempts while allowing active attempts to finish."""
        with self._condition:
            if self._stop_requested:
                return False
            changed = not self._after_current_requested
            self._after_current_requested = True
            self._condition.notify_all()
            return changed

    def request_skip_current(self) -> None:
        with self._condition:
            if self._active_attempt_ids:
                self._skip_active_attempt_ids.update(self._active_attempt_ids)
            else:
                self._pending_skip_requests += 1
            self._cancel_pending_verification("skip_requested")
            self._condition.notify_all()

    def start_attempt(self) -> int | None:
        with self._condition:
            # This second gate closes the race between dispatcher submission and
            # worker execution: a future accepted before graceful stop cannot
            # begin a new account after the request is received.
            if self._stop_requested or self._after_current_requested:
                return None
            attempt_id = self._next_attempt_id
            self._next_attempt_id += 1
            self._active_attempt_ids.add(attempt_id)
            return attempt_id

    def finish_attempt(self, attempt_id: int | None) -> None:
        if attempt_id is None:
            return
        with self._condition:
            self._active_attempt_ids.discard(attempt_id)
            self._skip_active_attempt_ids.discard(attempt_id)
            challenge = self._pending_verification
            if (
                challenge is not None
                and challenge.attempt_id == attempt_id
                and challenge.status == "pending"
            ):
                self._cancel_pending_verification("attempt_finished")
            self._condition.notify_all()

    def resume_attempt(self, attempt_id: int | None) -> None:
        """Restore an already-claimed account around an internal retry.

        Phone binding may consume several phone candidates for one account.
        Its per-phone cleanup must release skip/verification state, while a
        graceful request must still allow that same account to continue.  This
        method never allocates a new id and therefore deliberately remains
        valid after ``after_current``; immediate stop still interrupts it.
        """
        if attempt_id is None:
            return
        with self._condition:
            if self._stop_requested:
                self._active_attempt_ids.discard(attempt_id)
                self._skip_active_attempt_ids.discard(attempt_id)
                raise StopTaskRequested()
            self._active_attempt_ids.add(attempt_id)
            self._condition.notify_all()

    def checkpoint(
        self,
        *,
        consume_skip: bool = True,
        attempt_id: int | None = None,
    ) -> None:
        with self._lock:
            if self._stop_requested:
                raise StopTaskRequested()
            if consume_skip:
                if (
                    attempt_id is not None
                    and attempt_id in self._skip_active_attempt_ids
                ):
                    self._skip_active_attempt_ids.discard(attempt_id)
                    raise SkipCurrentAttemptRequested()
                if self._pending_skip_requests > 0:
                    self._pending_skip_requests -= 1
                    raise SkipCurrentAttemptRequested()

    def is_stop_requested(self) -> bool:
        with self._lock:
            return self._stop_requested

    def is_stop_after_current_requested(self) -> bool:
        with self._lock:
            return self._after_current_requested

    def should_stop_starting_new_attempts(self) -> bool:
        """Whether a dispatcher must not begin another account attempt.

        This intentionally differs from :meth:`checkpoint`: graceful stop is
        a scheduling boundary, never an interruption signal for an already
        active account (including an OTP wait).
        """
        with self._lock:
            return self._stop_requested or self._after_current_requested

    def current_verification_snapshot(self) -> dict[str, Any] | None:
        with self._lock:
            challenge = self._pending_verification
            if challenge is None:
                return None
            if challenge.status != "pending":
                return None
            return challenge.to_dict()

    def wait_for_verification_code(
        self,
        *,
        attempt_id: int | None,
        phase: str,
        phase_label: str,
        email: str,
        timeout_seconds: int,
        metadata: dict[str, Any] | None = None,
        actions: list[str] | None = None,
        action_handler=None,
    ) -> str:
        timeout_value = max(int(timeout_seconds or 0), 1)
        phase_value = str(phase or "email_otp").strip() or "email_otp"
        phase_label_value = str(phase_label or "邮箱验证码").strip() or "邮箱验证码"
        email_value = str(email or "").strip()

        with self._condition:
            self.checkpoint(attempt_id=attempt_id)
            challenge_id = f"verify_{int(time.time() * 1000)}_{self._next_challenge_id}"
            self._next_challenge_id += 1
            challenge = VerificationChallenge(
                challenge_id=challenge_id,
                attempt_id=attempt_id,
                phase=phase_value,
                phase_label=phase_label_value,
                email=email_value,
                timeout_seconds=timeout_value,
                metadata=dict(metadata or {}),
                actions=[str(item or "").strip() for item in (actions or []) if str(item or "").strip()],
            )
            self._pending_verification = challenge
            self._condition.notify_all()
            handled_action_seq = 0

            while True:
                self.checkpoint(attempt_id=attempt_id)
                current = self._pending_verification
                if current is None:
                    raise RuntimeError("验证码等待状态已丢失")
                if current.challenge_id != challenge_id:
                    raise RuntimeError("验证码等待状态已被新的挑战替换")
                if current.status == "submitted":
                    code = str(current.code or "").strip()
                    self._pending_verification = None
                    self._condition.notify_all()
                    if not code:
                        raise RuntimeError("验证码为空")
                    return code
                if current.status == "cancelled":
                    self._pending_verification = None
                    self._condition.notify_all()
                    reason = current.cancel_reason or "cancelled"
                    if reason == "stop_requested":
                        raise StopTaskRequested()
                    if reason == "skip_requested":
                        if (
                            attempt_id is not None
                            and attempt_id in self._skip_active_attempt_ids
                        ):
                            self._skip_active_attempt_ids.discard(attempt_id)
                        raise SkipCurrentAttemptRequested()
                    raise RuntimeError("验证码等待已取消")

                if (
                    callable(action_handler)
                    and current.action_seq > handled_action_seq
                    and isinstance(current.action_request, dict)
                ):
                    action_seq = int(current.action_seq or 0)
                    action_payload = dict(current.action_request or {})
                    current.metadata = {
                        **dict(current.metadata or {}),
                        "action_status": "handling",
                        "action": str(action_payload.get("action") or ""),
                    }
                    self._condition.notify_all()

                    self._condition.release()
                    try:
                        try:
                            action_result = action_handler(
                                str(action_payload.get("action") or ""),
                                dict(action_payload.get("payload") or {}),
                            )
                            action_error = ""
                        except Exception as exc:
                            action_result = {}
                            action_error = str(exc or "验证码动作处理失败")
                    finally:
                        self._condition.acquire()

                    current = self._pending_verification
                    if current is not None and current.challenge_id == challenge_id:
                        result_metadata = (
                            dict(action_result.get("metadata") or {})
                            if isinstance(action_result, dict)
                            else {}
                        )
                        current.metadata = {
                            **dict(current.metadata or {}),
                            **result_metadata,
                            "action_status": "failed" if action_error else "done",
                            "action_error": action_error,
                        }
                        handled_action_seq = action_seq
                        self._condition.notify_all()
                    continue

                now = time.time()
                if now >= current.expires_at:
                    current.status = "expired"
                    self._pending_verification = None
                    self._condition.notify_all()
                    raise TimeoutError(f"等待 {phase_label_value} 超时 ({timeout_value}s)")

                self._condition.wait(timeout=min(0.5, current.expires_at - now))

    def submit_verification(self, *, challenge_id: str, code: str) -> dict[str, Any]:
        challenge_id_value = str(challenge_id or "").strip()
        code_value = str(code or "").strip()
        if not challenge_id_value:
            raise ValueError("challenge_id 不能为空")
        if not code_value:
            raise ValueError("验证码不能为空")

        with self._condition:
            challenge = self._pending_verification
            if challenge is None:
                raise KeyError("当前没有待提交的验证码挑战")
            if challenge.challenge_id != challenge_id_value:
                raise KeyError("验证码挑战不存在或已过期")
            if challenge.status != "pending":
                raise ValueError("验证码挑战已结束，无法重复提交")
            if time.time() >= challenge.expires_at:
                challenge.status = "expired"
                self._pending_verification = None
                self._condition.notify_all()
                raise ValueError("验证码挑战已超时")

            challenge.code = code_value
            challenge.status = "submitted"
            self._condition.notify_all()
            return challenge.to_dict()

    def update_verification_metadata(
        self,
        *,
        challenge_id: str,
        patch: dict[str, Any],
    ) -> dict[str, Any]:
        challenge_id_value = str(challenge_id or "").strip()
        with self._condition:
            challenge = self._pending_verification
            if challenge is None:
                raise KeyError("当前没有待更新的验证码挑战")
            if challenge.challenge_id != challenge_id_value:
                raise KeyError("验证码挑战不存在或已过期")
            if challenge.status != "pending":
                raise ValueError("验证码挑战已结束")
            challenge.metadata = {
                **dict(challenge.metadata or {}),
                **dict(patch or {}),
            }
            self._condition.notify_all()
            return challenge.to_dict()

    def request_verification_action(
        self,
        *,
        challenge_id: str,
        action: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        challenge_id_value = str(challenge_id or "").strip()
        action_value = str(action or "").strip()
        if not challenge_id_value:
            raise ValueError("challenge_id 不能为空")
        if not action_value:
            raise ValueError("action 不能为空")

        with self._condition:
            challenge = self._pending_verification
            if challenge is None:
                raise KeyError("当前没有待处理的验证码挑战")
            if challenge.challenge_id != challenge_id_value:
                raise KeyError("验证码挑战不存在或已过期")
            if challenge.status != "pending":
                raise ValueError("验证码挑战已结束")
            if time.time() >= challenge.expires_at:
                challenge.status = "expired"
                self._pending_verification = None
                self._condition.notify_all()
                raise ValueError("验证码挑战已超时")
            allowed = {str(item or "").strip() for item in (challenge.actions or [])}
            if allowed and action_value not in allowed:
                raise ValueError(f"当前验证码挑战不支持动作: {action_value}")
            challenge.action_seq += 1
            challenge.action_request = {
                "seq": challenge.action_seq,
                "action": action_value,
                "payload": dict(payload or {}),
                "requested_at": time.time(),
            }
            challenge.metadata = {
                **dict(challenge.metadata or {}),
                "action_status": "pending",
                "action": action_value,
                "action_error": "",
            }
            self._condition.notify_all()
            return challenge.to_dict()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "stop_requested": self._stop_requested,
                # Keep both names while clients transition.  The explicit
                # stop_after_current_requested key is the public API name;
                # after_current_requested preserves the initial rollout shape.
                "stop_after_current_requested": self._after_current_requested,
                "after_current_requested": self._after_current_requested,
                "stop_mode": (
                    "immediate"
                    if self._stop_requested
                    else "after_current"
                    if self._after_current_requested
                    else ""
                ),
                "pending_skip_requests": self._pending_skip_requests,
                "active_attempts": len(self._active_attempt_ids),
                "targeted_skip_attempts": len(self._skip_active_attempt_ids),
            }


@dataclass
class RegisterTaskRecord:
    id: str
    platform: str
    source: str
    total: int
    supports_after_current: bool = False
    meta: dict[str, Any] = field(default_factory=dict)
    status: str = "pending"
    progress: str = "0/0"
    logs: deque[str] = field(default_factory=deque)
    logs_truncated: bool = False
    dropped_log_entries: int = 0
    dropped_log_bytes: int = 0
    _retained_log_bytes: int = field(default=0, repr=False)
    success: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    cashier_urls: list[str] = field(default_factory=list)
    error: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    control: RegisterTaskControl = field(
        default_factory=RegisterTaskControl,
        repr=False,
    )

    def to_dict(self) -> dict[str, Any]:
        data = {
            "id": self.id,
            "status": self.status,
            "platform": self.platform,
            "source": self.source,
            "meta": dict(self.meta),
            "progress": self.progress,
            "logs": list(self.logs),
            "logs_truncated": self.logs_truncated,
            "dropped_log_entries": self.dropped_log_entries,
            "dropped_log_bytes": self.dropped_log_bytes,
            "retained_log_bytes": self._retained_log_bytes,
            "log_start_index": self.dropped_log_entries,
            "log_next_index": self.dropped_log_entries + len(self.logs),
            "success": self.success,
            "skipped": self.skipped,
            "errors": list(self.errors),
            "control": self.control.snapshot(),
            "capabilities": {
                "stop_after_current": self.supports_after_current,
                "stop_modes": (
                    ["immediate", "after_current"]
                    if self.supports_after_current
                    else ["immediate"]
                )
            },
        }
        pending_verification = self.control.current_verification_snapshot()
        if pending_verification:
            data["pending_verification"] = pending_verification
        if self.cashier_urls:
            data["cashier_urls"] = list(self.cashier_urls)
        if self.error:
            data["error"] = self.error
        return data


class RegisterTaskStore:
    """线程安全的注册任务存储。"""

    def __init__(
        self,
        *,
        max_finished_tasks: int = 200,
        cleanup_threshold: int = 250,
        active_max_log_entries: int = DEFAULT_ACTIVE_MAX_LOG_ENTRIES,
        active_max_log_bytes: int = DEFAULT_ACTIVE_MAX_LOG_BYTES,
        finished_max_log_entries: int = DEFAULT_FINISHED_MAX_LOG_ENTRIES,
        finished_max_log_bytes: int = DEFAULT_FINISHED_MAX_LOG_BYTES,
        on_terminal: Callable[[str, dict[str, Any]], None] | None = None,
    ):
        self._lock = threading.Lock()
        self._records: dict[str, RegisterTaskRecord] = {}
        self.max_finished_tasks = max_finished_tasks
        self.cleanup_threshold = cleanup_threshold
        self.active_max_log_entries = max(1, int(active_max_log_entries or 1))
        self.active_max_log_bytes = max(1, int(active_max_log_bytes or 1))
        self.finished_max_log_entries = max(1, int(finished_max_log_entries or 1))
        self.finished_max_log_bytes = max(1, int(finished_max_log_bytes or 1))
        self._on_terminal = on_terminal

    @staticmethod
    def _trim_record_logs(
        record: RegisterTaskRecord,
        *,
        max_entries: int,
        max_bytes: int,
    ) -> None:
        while record.logs and (
            len(record.logs) > max_entries
            or record._retained_log_bytes > max_bytes
        ):
            dropped = record.logs.popleft()
            dropped_bytes = _log_entry_bytes(dropped)
            record._retained_log_bytes = max(
                0,
                record._retained_log_bytes - dropped_bytes,
            )
            record.logs_truncated = True
            record.dropped_log_entries += 1
            record.dropped_log_bytes += dropped_bytes

    def set_terminal_callback(
        self,
        callback: Callable[[str, dict[str, Any]], None] | None,
    ) -> None:
        """Install the durable-boundary hook used by the API task store.

        The callback is deliberately invoked after releasing the store lock so
        SQLite I/O cannot block task log append/progress updates or deadlock
        with a snapshot read.  It is a best-effort observer: a history write
        failure must never prevent a runner from becoming terminal.
        """
        with self._lock:
            self._on_terminal = callback

    def create(
        self,
        task_id: str,
        *,
        platform: str,
        total: int,
        source: str,
        meta: dict[str, Any] | None = None,
        supports_after_current: bool = False,
    ) -> RegisterTaskRecord:
        with self._lock:
            record = RegisterTaskRecord(
                id=task_id,
                platform=platform,
                total=total,
                source=source,
                meta=dict(meta or {}),
                progress=f"0/{total}",
                supports_after_current=bool(supports_after_current),
            )
            self._records[task_id] = record
            return record

    def exists(self, task_id: str) -> bool:
        with self._lock:
            return task_id in self._records

    def has_active(
        self,
        *,
        platform: str | None = None,
        source: str | None = None,
    ) -> bool:
        with self._lock:
            for record in self._records.values():
                if record.status not in ("pending", "running"):
                    continue
                if platform and record.platform != platform:
                    continue
                if source and record.source != source:
                    continue
                return True
        return False

    def control_for(self, task_id: str) -> RegisterTaskControl:
        with self._lock:
            return self._records[task_id].control

    def request_stop(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            record = self._records[task_id]
            if record.status in TERMINAL_TASK_STATUSES:
                raise ValueError("任务已结束")
            changed = record.control.request_stop()
            snapshot = record.to_dict()
        return {
            **dict(snapshot.get("control") or {}),
            "changed": changed,
            "task_snapshot": snapshot,
        }

    def request_stop_after_current(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            record = self._records[task_id]
            if record.status in TERMINAL_TASK_STATUSES:
                raise ValueError("任务已结束")
            if not record.supports_after_current:
                raise ValueError("当前任务不支持完成当前后停止")
            changed = record.control.request_stop_after_current()
            snapshot = record.to_dict()
        return {
            **dict(snapshot.get("control") or {}),
            "changed": changed,
            "task_snapshot": snapshot,
        }

    def request_skip_current(self, task_id: str) -> dict[str, Any]:
        control = self.control_for(task_id)
        control.request_skip_current()
        return control.snapshot()

    def append_log(self, task_id: str, entry: str) -> None:
        with self._lock:
            record = self._records.get(task_id)
            if record is None:
                return
            normalized_entry = str(entry or "")
            record.logs.append(normalized_entry)
            record._retained_log_bytes += _log_entry_bytes(normalized_entry)
            self._trim_record_logs(
                record,
                max_entries=self.active_max_log_entries,
                max_bytes=self.active_max_log_bytes,
            )
            record.updated_at = time.time()

    def mark_running(self, task_id: str) -> None:
        with self._lock:
            record = self._records[task_id]
            record.status = "running"
            record.updated_at = time.time()

    def set_progress(self, task_id: str, progress: str) -> None:
        with self._lock:
            record = self._records[task_id]
            record.progress = progress
            record.updated_at = time.time()

    def add_cashier_url(self, task_id: str, cashier_url: str) -> None:
        with self._lock:
            record = self._records[task_id]
            record.cashier_urls.append(cashier_url)
            record.updated_at = time.time()

    def update_meta(self, task_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            record = self._records[task_id]
            record.meta.update(dict(patch or {}))
            record.updated_at = time.time()
            return dict(record.meta)

    def finish(
        self,
        task_id: str,
        *,
        status: str,
        success: int,
        skipped: int,
        errors: list[str],
        error: str = "",
        respect_immediate_stop: bool = False,
    ) -> None:
        terminal_snapshot: dict[str, Any] | None = None
        callback: Callable[[str, dict[str, Any]], None] | None = None
        terminal_record: RegisterTaskRecord | None = None
        with self._lock:
            record = self._records[task_id]
            # A graceful request is only a dispatch gate.  Runners report
            # their usual natural result after draining; normalize that final
            # state here so every TaskLogPanel-backed runner gets the same
            # terminal contract without depending on dozens of hand-written
            # final-status branches.
            final_status = status
            if (
                record.control.is_stop_after_current_requested()
                and status in {"done", "failed"}
            ):
                final_status = "stopped"
            if (
                respect_immediate_stop
                and record.control.is_stop_requested()
                and status in {"done", "failed"}
            ):
                final_status = "stopped"
            record.status = final_status
            record.success = success
            record.skipped = skipped
            record.errors = list(errors)
            record.error = error
            record.updated_at = time.time()
            if final_status in TERMINAL_TASK_STATUSES:
                terminal_snapshot = record.to_dict()
                callback = self._on_terminal
                terminal_record = record

        if callback is not None and terminal_snapshot is not None:
            # SQLite can briefly be locked by an account/state update just
            # ahead of task finalization.  A bounded retry preserves the
            # terminal log without ever rolling the worker state back.
            for retry_index in range(3):
                try:
                    callback(task_id, terminal_snapshot)
                    break
                except Exception:
                    logger.exception(
                        "task terminal snapshot persistence failed task_id=%s retry=%s",
                        task_id,
                        retry_index + 1,
                    )
                    if retry_index < 2:
                        time.sleep(0.05 * (retry_index + 1))

        if terminal_record is not None:
            # The durable callback must observe the larger active-task window.
            # Only its completion attempts may compact the in-memory copy.
            with self._lock:
                current_record = self._records.get(task_id)
                if current_record is terminal_record:
                    self._trim_record_logs(
                        current_record,
                        max_entries=self.finished_max_log_entries,
                        max_bytes=self.finished_max_log_bytes,
                    )

    def snapshot(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            return self._records[task_id].to_dict()

    def list_snapshots(self) -> list[dict[str, Any]]:
        with self._lock:
            return [record.to_dict() for record in self._records.values()]

    def log_state(self, task_id: str) -> tuple[list[str], str]:
        with self._lock:
            record = self._records[task_id]
            return list(record.logs), record.status

    def log_window_state(self, task_id: str) -> tuple[list[str], str, int, int]:
        with self._lock:
            record = self._records[task_id]
            logs = list(record.logs)
            start_index = int(record.dropped_log_entries)
            return (
                logs,
                record.status,
                start_index,
                start_index + len(logs),
            )

    def cleanup(self) -> None:
        with self._lock:
            if len(self._records) <= self.cleanup_threshold:
                return
            finished = [
                (task_id, record)
                for task_id, record in self._records.items()
                if record.status in TERMINAL_TASK_STATUSES
            ]
            if len(finished) <= self.max_finished_tasks:
                return
            finished.sort(key=lambda item: item[1].created_at)
            to_remove = finished[: len(finished) - self.max_finished_tasks]
            for task_id, _ in to_remove:
                self._records.pop(task_id, None)


__all__ = [
    "AttemptOutcome",
    "AttemptResult",
    "RegisterTaskControl",
    "RegisterTaskRecord",
    "RegisterTaskStore",
    "SkipCurrentAttemptRequested",
    "StopTaskRequested",
    "TaskInterruption",
    "TERMINAL_TASK_STATUSES",
    "VerificationChallenge",
]
