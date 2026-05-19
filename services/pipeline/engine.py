from __future__ import annotations

from datetime import datetime, timezone
import threading
import time
import uuid
from typing import Any

from api.chatgpt import get_active_gopay_batch_payment

from .config import PipelineConfigStore
from .models import PipelineConfig
from .auth_scheduler import AuthCaptureScheduler
from .logs import PipelineLogBus
from .payment_scheduler import PaymentBatchScheduler
from .register_scheduler import RegisterRefillScheduler
from .state import PipelineStateStore


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PipelineEngine:
    """Lifecycle controller with recovery and reconciliation support."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()
        self._thread: threading.Thread | None = None
        self._next_register_tick_at = 0.0
        self._next_payment_tick_at = 0.0
        self._next_auth_tick_at = 0.0
        self.config_store = PipelineConfigStore()
        self.state_store = PipelineStateStore()
        self.log_bus = PipelineLogBus()
        self.log_bus.set_persist_callback(self._persist_live_log)
        self.status = "stopped"
        self._config: PipelineConfig = self.config_store.load()
        self.register_scheduler = RegisterRefillScheduler(
            state_store=self.state_store,
            config_store=self.config_store,
            log_bus=self.log_bus,
        )
        self.payment_scheduler = PaymentBatchScheduler(
            state_store=self.state_store,
            config_store=self.config_store,
            log_bus=self.log_bus,
        )
        self.auth_scheduler = AuthCaptureScheduler(
            state_store=self.state_store,
            config_store=self.config_store,
            log_bus=self.log_bus,
        )

    @property
    def config(self) -> PipelineConfig:
        self._config = self.config_store.load()
        return self._config

    def set_config(self, config: PipelineConfig) -> PipelineConfig:
        self._config = self.config_store.save(config)
        return self._config

    def recover_latest_task(self):
        task = self.state_store.get_latest_task()
        if task is None:
            return None
        reconciled = self.reconcile_task(task)
        self.status = str(reconciled.status or "stopped")
        return reconciled

    def reconcile_task(self, task):
        task = self._reconcile_active_payment_batch(task)
        self._reconcile_register_intermediate_items(task)
        self._reconcile_auth_intermediate_items(task)
        return task

    def should_auto_start(self) -> bool:
        return bool(self.config.auto_start)

    def restore_or_start(self):
        task = self.recover_latest_task()
        if task is not None:
            task_status = str(task.status or "stopped").strip().lower()
            if task_status in {"running", "paused"}:
                self._ensure_worker_thread(paused=task_status == "paused", log_message="自动流水线已恢复")
            elif self.should_auto_start():
                self.start()
            return task
        if self.should_auto_start():
            self.start()
        return None

    def get_status_snapshot(self) -> dict[str, Any]:
        task = self.state_store.get_latest_task()
        if task is None:
            return {
                "task": None,
                "config": self.config.model_dump(),
                "queues": {
                    "pending_payment": [],
                    "paid": [],
                    "failed": [],
                    "auth_pending": [],
                },
                "active_payment_batch": None,
                "task_logs": [],
                "task_list": [],
                "summary": {
                    "pending_payment_count": 0,
                    "paid_count": 0,
                    "failed_count": 0,
                    "auth_pending_count": 0,
                },
            }

        task_id = int(task.id or 0)
        pending_payment = self.state_store.list_pending_payment_items(task_id)
        paid = self.state_store.list_paid_items(task_id)
        failed = self.state_store.list_failed_items(task_id)
        auth_pending = self.state_store.list_auth_pending_items(task_id)
        active_payment_batch = None
        task_logs = self.state_store.list_task_logs(task_id)
        task_list = self.state_store.list_tasks(limit=20)
        active_batch_id = str(task.active_payment_batch_id or "").strip()
        if active_batch_id:
            try:
                active_payment_batch = get_active_gopay_batch_payment().get("task")
                if not active_payment_batch or str(active_payment_batch.get("task_id") or "").strip() != active_batch_id:
                    active_payment_batch = None
            except Exception:
                active_payment_batch = None
        return {
            "task": {
                "id": task.id,
                "task_key": task.task_key,
                "status": task.status,
                "active_register_task_id": task.active_register_task_id,
                "active_payment_batch_id": task.active_payment_batch_id,
                "active_auth_task_id": task.active_auth_task_id,
                "last_error": task.last_error,
                "started_at": task.started_at.isoformat() if task.started_at else "",
                "stopped_at": task.stopped_at.isoformat() if task.stopped_at else "",
                "updated_at": task.updated_at.isoformat() if task.updated_at else "",
            },
            "config": self.config.model_dump(),
            "queues": {
                "pending_payment": [self._item_to_dict(item) for item in pending_payment],
                "paid": [self._item_to_dict(item) for item in paid],
                "failed": [self._item_to_dict(item) for item in failed],
                "auth_pending": [self._item_to_dict(item) for item in auth_pending],
            },
            "active_payment_batch": active_payment_batch,
            "task_logs": task_logs,
            "task_list": [
                {
                    "id": item.id,
                    "task_key": item.task_key,
                    "status": item.status,
                    "started_at": item.started_at.isoformat() if item.started_at else "",
                    "stopped_at": item.stopped_at.isoformat() if item.stopped_at else "",
                    "updated_at": item.updated_at.isoformat() if item.updated_at else "",
                }
                for item in task_list
            ],
            "summary": {
                "pending_payment_count": len(pending_payment),
                "paid_count": len(paid),
                "failed_count": len(failed),
                "auth_pending_count": len(auth_pending),
            },
        }

    def start(self) -> None:
        with self._lock:
            if self.status == "running" and self._thread_is_alive():
                return
            self.status = "running"
            task = self._ensure_runtime_task()
            task.status = "running"
            if task.started_at is None:
                task.started_at = _utcnow()
            task.stopped_at = None
            task.updated_at = _utcnow()
            self.state_store.save_task(task)
            self._ensure_worker_thread(
                paused=False,
                log_message=f"自动流水线已启动: task_key={task.task_key}",
            )

    def stop(self) -> None:
        with self._lock:
            if self.status in {"stopped", "done"}:
                return
            self._stop_event.set()
            self._pause_event.set()
            self.status = "stopped"
            task = self.state_store.get_latest_task()
            if task is not None:
                task.status = "stopped"
                task.stopped_at = _utcnow()
                task.updated_at = _utcnow()
                self.state_store.save_task(task)
            self.log_bus.publish("自动流水线已停止，后续不再启动新的调度动作")

    def pause(self) -> None:
        with self._lock:
            if self.status != "running":
                return
            self._pause_event.clear()
            self.status = "paused"
            task = self.state_store.get_latest_task()
            if task is not None:
                task.status = "paused"
                task.updated_at = _utcnow()
                self.state_store.save_task(task)
            self.log_bus.publish("自动流水线已暂停")

    def resume(self) -> None:
        with self._lock:
            if self.status != "paused":
                return
            self.status = "running"
            task = self.state_store.get_latest_task()
            if task is not None:
                task.status = "running"
                task.updated_at = _utcnow()
                self.state_store.save_task(task)
            self._ensure_worker_thread(paused=False, log_message="自动流水线已恢复")

    def _reconcile_active_payment_batch(self, task):
        active_batch_id = str(getattr(task, "active_payment_batch_id", "") or "").strip()
        active = get_active_gopay_batch_payment()
        active_task = active.get("task") if isinstance(active, dict) else None
        active_task_id = str((active_task or {}).get("task_id") or "").strip()

        if active_task_id:
            task.active_payment_batch_id = active_task_id
            if str(task.status or "") not in {"running", "paused"}:
                task.status = "running"
            task.updated_at = _utcnow()
            self.state_store.save_task(task)
            self.log_bus.publish(f"恢复活跃 GoPay batch: batch_id={active_task_id}")
            return task

        if active_batch_id:
            task.active_payment_batch_id = ""
            task.updated_at = _utcnow()
            self.state_store.save_task(task)
            self.log_bus.publish(f"历史活跃 GoPay batch 已不存在，清理引用: batch_id={active_batch_id}")
        return task

    def _reconcile_register_intermediate_items(self, task) -> None:
        active_register_task_id = str(getattr(task, "active_register_task_id", "") or "").strip()
        items = self.state_store.list_account_items(int(task.id or 0))
        for item in items:
            status = str(item.pipeline_status or "").strip()
            register_stage = str(item.register_stage or "").strip()
            if status == "registering" or register_stage == "running":
                self.state_store.update_account_item(
                    int(item.id or 0),
                    pipeline_status="failed",
                    register_stage="failed",
                    register_error_code="register_state_lost",
                    register_error_reason="服务重启后注册中间态丢失",
                    register_error_detail="服务重启后注册任务内存态丢失，已标记失败待人工检查",
                )
        if active_register_task_id:
            task.active_register_task_id = ""
            task.updated_at = _utcnow()
            self.state_store.save_task(task)
            self.log_bus.publish(
                f"历史活跃注册任务已丢失，清理引用: task_id={active_register_task_id}"
            )

    def _reconcile_auth_intermediate_items(self, task) -> None:
        active_auth_task_id = str(getattr(task, "active_auth_task_id", "") or "").strip()
        items = self.state_store.list_account_items(int(task.id or 0))
        for item in items:
            status = str(item.pipeline_status or "").strip()
            auth_stage = str(item.auth_stage or "").strip()
            if status in {"auth_pending", "auth_running"} or auth_stage in {"pending", "running"}:
                self.state_store.update_account_item(
                    int(item.id or 0),
                    pipeline_status="auth_failed",
                    auth_stage="failed",
                    auth_error_code="auth_state_lost",
                    auth_error_reason="服务重启后 Auth 中间态丢失",
                    auth_error_detail="服务重启后 Auth task 内存态丢失，已标记失败待人工检查",
                )
        if active_auth_task_id:
            task.active_auth_task_id = ""
            task.updated_at = _utcnow()
            self.state_store.save_task(task)
            self.log_bus.publish(
                f"历史活跃 Auth 任务已丢失，清理引用: task_id={active_auth_task_id}"
            )

    def _ensure_runtime_task(self):
        task = self.state_store.get_latest_task()
        if task is not None:
            return task
        task_key = f"pipeline_{uuid.uuid4().hex[:12]}"
        return self.state_store.create_task(task_key=task_key, status="running", config_snapshot=self.config.model_dump())

    def _thread_is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _ensure_worker_thread(self, *, paused: bool, log_message: str = "") -> None:
        self._stop_event.clear()
        if paused:
            self._pause_event.clear()
        else:
            self._pause_event.set()
        self._reset_scheduler_deadlines(immediate=True)
        if self._thread_is_alive():
            if log_message:
                self.log_bus.publish(log_message)
            return
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        if log_message:
            self.log_bus.publish(log_message)

    def _reset_scheduler_deadlines(self, *, immediate: bool) -> None:
        task = self.state_store.get_latest_task()
        self._schedule_next_register_tick(task, immediate=immediate)
        self._schedule_next_payment_tick(task, immediate=immediate)
        self._schedule_next_auth_tick(task, immediate=immediate)

    def _coerce_interval_seconds(self, value: Any) -> float:
        try:
            numeric = float(value or 0)
        except Exception:
            numeric = 0.0
        if numeric <= 0:
            return 0.1
        return min(numeric, 3600.0)

    def _register_interval_seconds(self, config: PipelineConfig) -> float:
        return self._coerce_interval_seconds(config.register_poll_interval_seconds)

    def _payment_interval_seconds(self, task, config: PipelineConfig) -> float:
        active_batch_id = str(getattr(task, "active_payment_batch_id", "") or "").strip() if task is not None else ""
        if active_batch_id:
            return self._coerce_interval_seconds(config.gopay_batch_poll_interval_seconds)
        return self._coerce_interval_seconds(config.payment_batch_interval_seconds)

    def _auth_interval_seconds(self, config: PipelineConfig) -> float:
        return self._coerce_interval_seconds(config.auth_poll_interval_seconds)

    def _schedule_next_register_tick(self, task, *, immediate: bool = False) -> None:
        config = self.config_store.load()
        self._next_register_tick_at = self._next_tick_at(
            self._register_interval_seconds(config),
            immediate=immediate,
        )

    def _schedule_next_payment_tick(self, task, *, immediate: bool = False) -> None:
        config = self.config_store.load()
        self._next_payment_tick_at = self._next_tick_at(
            self._payment_interval_seconds(task, config),
            immediate=immediate,
        )

    def _schedule_next_auth_tick(self, task, *, immediate: bool = False) -> None:
        config = self.config_store.load()
        self._next_auth_tick_at = self._next_tick_at(
            self._auth_interval_seconds(config),
            immediate=immediate,
        )

    def _next_tick_at(self, interval_seconds: float, *, immediate: bool) -> float:
        now = time.monotonic()
        if immediate:
            return now
        return now + max(0.0, float(interval_seconds or 0.0))

    def _next_wait_seconds(self) -> float:
        now = time.monotonic()
        candidates = [
            self._next_register_tick_at,
            self._next_payment_tick_at,
            self._next_auth_tick_at,
        ]
        waits = [candidate - now for candidate in candidates if candidate > now]
        if not waits:
            return 0.1
        return max(0.1, min(waits))

    def _run_loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                self._pause_event.wait()
                if self._stop_event.is_set():
                    break
                task = self.state_store.get_latest_task()
                if task is None:
                    task = self._ensure_runtime_task()
                if str(task.status or "") == "stopped":
                    break

                ran_any = False
                if time.monotonic() >= self._next_register_tick_at:
                    ran_any = True
                    try:
                        self.register_scheduler.tick(task)
                    except Exception as exc:
                        self.log_bus.publish(f"注册补货调度异常: {exc}")
                    finally:
                        task = self.state_store.get_latest_task() or task
                        self._schedule_next_register_tick(task, immediate=False)
                if self._stop_event.is_set():
                    break

                if time.monotonic() >= self._next_payment_tick_at:
                    ran_any = True
                    try:
                        task = self.state_store.get_latest_task() or task
                        self.payment_scheduler.tick(task)
                    except Exception as exc:
                        self.log_bus.publish(f"支付批处理调度异常: {exc}")
                    finally:
                        task = self.state_store.get_latest_task() or task
                        self._schedule_next_payment_tick(task, immediate=False)
                if self._stop_event.is_set():
                    break

                if time.monotonic() >= self._next_auth_tick_at:
                    ran_any = True
                    try:
                        task = self.state_store.get_latest_task() or task
                        self.auth_scheduler.tick(task)
                    except Exception as exc:
                        self.log_bus.publish(f"Auth 调度异常: {exc}")
                    finally:
                        task = self.state_store.get_latest_task() or task
                        self._schedule_next_auth_tick(task, immediate=False)

                if not ran_any:
                    self._wait_with_pause(self._next_wait_seconds())
        finally:
            with self._lock:
                current = threading.current_thread()
                if self._thread is current:
                    self._thread = None

    def _wait_with_pause(self, seconds: int) -> None:
        deadline = time.time() + max(0, int(seconds or 0))
        while time.time() < deadline and not self._stop_event.is_set():
            if not self._pause_event.is_set():
                self._pause_event.wait()
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            time.sleep(min(1.0, remaining))

    def _item_to_dict(self, item) -> dict[str, Any]:
        return {
            "id": item.id,
            "pipeline_task_id": item.pipeline_task_id,
            "account_id": item.account_id,
            "email": item.email,
            "source": item.source,
            "source_register_task_id": item.source_register_task_id,
            "pipeline_status": item.pipeline_status,
            "register_stage": item.register_stage,
            "payment_stage": item.payment_stage,
            "auth_stage": item.auth_stage,
            "account_primary_status": item.account_primary_status,
            "checkout_url": item.checkout_url,
            "payment_batch_task_id": item.payment_batch_task_id,
            "gopay_session_id": item.gopay_session_id,
            "gopay_uid": item.gopay_uid,
            "subscription_plan_expected": item.subscription_plan_expected,
            "subscription_plan_confirmed": item.subscription_plan_confirmed,
            "subscription_refresh_status": item.subscription_refresh_status,
            "subscription_refreshed_at": item.subscription_refreshed_at,
            "register_error_code": item.register_error_code,
            "register_error_reason": item.register_error_reason,
            "register_error_detail": item.register_error_detail,
            "payment_failed_stage": item.payment_failed_stage,
            "payment_error_code": item.payment_error_code,
            "payment_error_reason": item.payment_error_reason,
            "payment_error_detail": item.payment_error_detail,
            "auth_error_code": item.auth_error_code,
            "auth_error_reason": item.auth_error_reason,
            "auth_error_detail": item.auth_error_detail,
            "success_summary": item.success_summary,
            "created_at": item.created_at.isoformat() if item.created_at else "",
            "updated_at": item.updated_at.isoformat() if item.updated_at else "",
        }

    def _persist_live_log(self, line: str) -> None:
        task = self.state_store.get_latest_task()
        if task is None or int(task.id or 0) <= 0:
            return
        self.state_store.append_task_log(int(task.id or 0), line, limit=self.log_bus.limit)


pipeline_engine = PipelineEngine()
