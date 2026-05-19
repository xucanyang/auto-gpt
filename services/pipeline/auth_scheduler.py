from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from api.tasks import enqueue_resume_subscription_auth_task, get_task

from .config import PipelineConfigStore
from .logs import PipelineLogBus
from .models import PipelineTask
from .state import PipelineStateStore


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AuthCaptureScheduler:
    """Optional single-thread auth capture scheduler."""

    def __init__(
        self,
        *,
        state_store: PipelineStateStore,
        config_store: PipelineConfigStore,
        log_bus: PipelineLogBus,
    ) -> None:
        self.state_store = state_store
        self.config_store = config_store
        self.log_bus = log_bus
        self._task_log_offsets: dict[str, int] = {}

    def tick(self, pipeline_task: PipelineTask) -> dict[str, Any]:
        config = self.config_store.load()
        active_auth_task_id = str(getattr(pipeline_task, "active_auth_task_id", "") or "").strip()

        if active_auth_task_id:
            snapshot = self.poll_active_task(pipeline_task)
            return {"action": "poll", "task_id": active_auth_task_id, "snapshot": snapshot}

        paid_items = self.state_store.list_paid_items(int(pipeline_task.id or 0))
        if not paid_items:
            return {"action": "noop", "reason": "no_paid_items"}

        if not bool(config.enable_auth_capture):
            completed = self._mark_paid_items_done_without_auth(paid_items)
            return {"action": "skip_auth", "count": completed}

        target = self._pick_next_auth_item(paid_items)
        if target is None:
            return {"action": "noop", "reason": "no_auth_candidate"}

        task_id = self.start_auth_task(pipeline_task, target)
        return {"action": "start", "task_id": task_id, "account_item_id": int(target.id or 0)}

    def _pick_next_auth_item(self, items: list[Any]) -> Any | None:
        for item in items:
            if str(item.pipeline_status or "").strip() == "paid":
                return item
        return None

    def _mark_paid_items_done_without_auth(self, items: list[Any]) -> int:
        count = 0
        now = _utcnow()
        for item in items:
            if str(item.pipeline_status or "").strip() != "paid":
                continue
            self.state_store.update_account_item(
                int(item.id or 0),
                pipeline_status="done",
                auth_stage="skipped",
                auth_completed_at=now,
                success_summary=str(item.success_summary or "支付成功，已跳过 Auth 补抓"),
            )
            count += 1
        if count > 0:
            self.log_bus.publish(f"Auth 补抓已关闭，直接完成已支付账号: count={count}")
        return count

    def start_auth_task(self, pipeline_task: PipelineTask, item: Any) -> str:
        item_id = int(item.id or 0)
        account_id = int(item.account_id or 0)
        if item_id <= 0 or account_id <= 0:
            raise ValueError("无效的支付成功账号，无法启动 Auth 补抓")

        now = _utcnow()
        self.state_store.update_account_item(
            item_id,
            pipeline_status="auth_pending",
            auth_stage="pending",
        )
        task_id = enqueue_resume_subscription_auth_task(account_id)
        self.state_store.update_account_item(
            item_id,
            pipeline_status="auth_running",
            auth_stage="running",
            auth_started_at=now,
        )
        pipeline_task.active_auth_task_id = str(task_id or "")
        pipeline_task.updated_at = now
        self.state_store.save_task(pipeline_task)
        self.log_bus.publish(
            f"启动 Auth 补抓任务: task_id={task_id} account_id={account_id} item_id={item_id}"
        )
        return task_id

    def poll_active_task(self, pipeline_task: PipelineTask) -> dict[str, Any] | None:
        task_id = str(getattr(pipeline_task, "active_auth_task_id", "") or "").strip()
        if not task_id:
            return None
        try:
            snapshot = get_task(task_id)
        except Exception as exc:
            message = f"Auth 任务状态丢失: {exc}"
            self._apply_auth_result(
                task_id,
                {
                    "status": "failed",
                    "meta": {},
                    "errors": [message],
                    "error": message,
                },
                int(pipeline_task.id or 0),
            )
            pipeline_task.active_auth_task_id = ""
            pipeline_task.updated_at = _utcnow()
            self.state_store.save_task(pipeline_task)
            self.log_bus.publish(f"Auth 补抓任务引用已失效，已清理: task_id={task_id}")
            return {
                "task_id": task_id,
                "status": "failed",
                "error": message,
            }
        self._relay_task_logs(task_id, snapshot)
        status = str((snapshot or {}).get("status") or "").strip().lower()
        if status in {"done", "failed", "stopped"}:
            self._apply_auth_result(task_id, snapshot, int(pipeline_task.id or 0))
            pipeline_task.active_auth_task_id = ""
            pipeline_task.updated_at = _utcnow()
            self.state_store.save_task(pipeline_task)
            self.log_bus.publish(f"Auth 补抓任务结束: task_id={task_id} status={status}")
            self._task_log_offsets.pop(task_id, None)
        return snapshot

    def _apply_auth_result(self, task_id: str, snapshot: dict[str, Any] | None, pipeline_task_id: int) -> None:
        detail = snapshot or {}
        meta = detail.get("meta") if isinstance(detail.get("meta"), dict) else {}
        account_id = int(meta.get("account_id") or 0)
        if account_id <= 0:
            return
        item = self.state_store.get_account_item_by_account_id(pipeline_task_id, account_id)
        if item is None or int(item.id or 0) <= 0:
            return

        status = str(detail.get("status") or "").strip().lower()
        errors = detail.get("errors") if isinstance(detail.get("errors"), list) else []
        first_error = ""
        for error in errors:
            text = str(error or "").strip()
            if text:
                first_error = text
                break
        message = str(first_error or detail.get("error") or "").strip()
        now = _utcnow()
        if status == "done":
            self.state_store.update_account_item(
                int(item.id or 0),
                pipeline_status="done",
                auth_stage="success",
                auth_completed_at=now,
                success_summary=str(item.success_summary or "支付成功，Auth 补抓完成"),
            )
        elif status in {"failed", "stopped"}:
            self.state_store.update_account_item(
                int(item.id or 0),
                pipeline_status="auth_failed",
                auth_stage="failed",
                auth_completed_at=now,
                auth_error_code="auth_capture_failed" if status == "failed" else "auth_capture_stopped",
                auth_error_reason=message or ("Auth 补抓失败" if status == "failed" else "Auth 补抓已停止"),
                auth_error_detail=message or ("Auth 补抓失败" if status == "failed" else "Auth 补抓已停止"),
            )

    def _relay_task_logs(self, task_id: str, snapshot: dict[str, Any] | None) -> None:
        logs = snapshot.get("logs") if isinstance(snapshot, dict) and isinstance(snapshot.get("logs"), list) else []
        if not logs:
            return
        offset = int(self._task_log_offsets.get(task_id, 0) or 0)
        if offset < 0 or offset > len(logs):
            offset = 0
        for line in logs[offset:]:
            text = str(line or "").strip()
            if text:
                self.log_bus.publish(f"[Auth任务] {text}")
        self._task_log_offsets[task_id] = len(logs)
