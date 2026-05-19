from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from api.tasks import RegisterTaskRequest, enqueue_register_task, get_task, has_active_register_task
from sqlmodel import Session, select

from core.db import AccountModel, TaskLog, engine

from .config import PipelineConfigStore
from .logs import PipelineLogBus
from .models import PipelineAccountItem, PipelineTask
from .state import PipelineStateStore


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RegisterRefillScheduler:
    """Register refill scheduler for maintaining the pending payment pool."""

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
        active_register_task_id = str(getattr(pipeline_task, "active_register_task_id", "") or "").strip()

        if active_register_task_id:
            snapshot = self.poll_active_task(pipeline_task)
            return {"action": "poll", "task_id": active_register_task_id, "snapshot": snapshot}

        pending_count = len(self.state_store.list_pending_payment_items(int(pipeline_task.id or 0)))
        threshold = int(config.payment_pool_threshold or 0)
        if pending_count >= threshold:
            return {"action": "noop", "reason": "pending_payment_pool_sufficient", "pending_count": pending_count}

        if has_active_register_task(platform="chatgpt", source="pipeline"):
            self.log_bus.publish("检测到已有活跃的 pipeline 注册任务，跳过本次补货")
            return {"action": "noop", "reason": "existing_pipeline_register_task"}

        target = int(config.payment_pool_target or 0)
        refill_count = max(target - pending_count, 0)
        if refill_count <= 0:
            return {"action": "noop", "reason": "no_refill_needed", "pending_count": pending_count}

        task_id = self.start_refill_task(pipeline_task, refill_count=refill_count)
        return {"action": "start", "task_id": task_id, "refill_count": refill_count}

    def start_refill_task(self, pipeline_task: PipelineTask, *, refill_count: int) -> str:
        config = self.config_store.load()
        req = RegisterTaskRequest(
            platform="chatgpt",
            count=max(1, int(refill_count or 1)),
            concurrency=1,
            proxy=config.proxy,
            executor_type=str(config.executor_type or "protocol"),
            captcha_solver=str(config.captcha_solver or "yescaptcha"),
            extra={
                **dict(config.register_extra or {}),
                "mail_provider": str(config.mail_provider or ""),
                # Pipeline registration should only produce a payable account.
                # Use the access-token-only registration flow so registration
                # ends after session/access-token landing instead of workspace capture.
                "chatgpt_registration_mode": "access_token_only",
                # Disable no-RT checkout amount probing for pipeline refill.
                # The pipeline should save the account and generate payment links later.
                "chatgpt_access_token_only_checkout_amount_check_enabled": False,
                # Pipeline registration only needs a payable account.
                # Free workspace auth capture should happen after payment succeeds.
                "chatgpt_capture_free_workspace": False,
            },
        )
        meta = {
            "pipeline_task_id": int(pipeline_task.id or 0),
            "pipeline_key": str(pipeline_task.task_key or ""),
            "started_at": _utcnow().isoformat(),
        }
        task_id = enqueue_register_task(req, source="pipeline", meta=meta)
        pipeline_task.active_register_task_id = str(task_id or "")
        pipeline_task.updated_at = _utcnow()
        self.state_store.save_task(pipeline_task)
        self.log_bus.publish(
            f"启动注册补货任务: task_id={task_id} refill_count={max(1, int(refill_count or 1))}"
        )
        return task_id

    def poll_active_task(self, pipeline_task: PipelineTask) -> dict[str, Any] | None:
        task_id = str(getattr(pipeline_task, "active_register_task_id", "") or "").strip()
        if not task_id:
            return None
        try:
            snapshot = get_task(task_id)
        except Exception as exc:
            message = f"注册任务状态丢失: {exc}"
            self._record_register_failure(
                pipeline_task,
                task_id,
                {
                    "error": message,
                    "errors": [message],
                },
            )
            pipeline_task.active_register_task_id = ""
            pipeline_task.updated_at = _utcnow()
            self.state_store.save_task(pipeline_task)
            self.log_bus.publish(f"注册补货任务引用已失效，已清理: task_id={task_id}")
            return {
                "task_id": task_id,
                "status": "failed",
                "error": message,
            }
        self._relay_task_logs(task_id, snapshot)
        status = str((snapshot or {}).get("status") or "").strip().lower()
        if status in {"done", "failed", "stopped"}:
            if status == "done":
                self._collect_registered_accounts(pipeline_task, task_id)
            else:
                self._record_register_failure(pipeline_task, task_id, snapshot)
            pipeline_task.active_register_task_id = ""
            pipeline_task.updated_at = _utcnow()
            self.state_store.save_task(pipeline_task)
            self.log_bus.publish(f"注册补货任务结束: task_id={task_id} status={status}")
            self._task_log_offsets.pop(task_id, None)
        return snapshot

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
                self.log_bus.publish(f"[注册任务] {text}")
        self._task_log_offsets[task_id] = len(logs)

    def _collect_registered_accounts(self, pipeline_task: PipelineTask, task_id: str) -> int:
        pipeline_task_id = int(pipeline_task.id or 0)
        created = 0
        with Session(engine) as session:
            logs = list(
                session.exec(
                    select(TaskLog)
                    .where(TaskLog.platform == "chatgpt")
                    .where(TaskLog.status == "success")
                    .order_by(TaskLog.id.asc())
                ).all()
            )
            for log in logs:
                try:
                    detail = json.loads(log.detail_json or "{}")
                except Exception:
                    detail = {}
                if not isinstance(detail, dict):
                    continue
                if str(detail.get("task_id") or "").strip() != task_id:
                    continue
                meta = detail.get("meta") if isinstance(detail.get("meta"), dict) else {}
                if int(meta.get("pipeline_task_id") or 0) != pipeline_task_id:
                    continue

                email = str(detail.get("email") or log.email or "").strip()
                if not email:
                    continue
                account = session.exec(
                    select(AccountModel)
                    .where(AccountModel.platform == "chatgpt")
                    .where(AccountModel.email == email)
                    .order_by(AccountModel.created_at.desc(), AccountModel.id.desc())
                ).first()
                if account is None or int(account.id or 0) <= 0:
                    continue
                if self.state_store.get_account_item_by_account_id(pipeline_task_id, int(account.id or 0)):
                    continue

                item = PipelineAccountItem(
                    pipeline_task_id=pipeline_task_id,
                    account_id=int(account.id or 0),
                    email=account.email,
                    source="pipeline_register",
                    source_register_task_id=task_id,
                    pipeline_status="pending_payment",
                    register_stage="success",
                    payment_stage="pending",
                    auth_stage="disabled",
                    account_primary_status=str(account.status or "registered"),
                    register_started_at=account.created_at,
                    register_completed_at=account.updated_at,
                    success_summary="注册成功，已加入待支付池",
                )
                self.state_store.create_account_item(item)
                created += 1

        if created > 0:
            self.log_bus.publish(f"注册结果回收完成: task_id={task_id} new_accounts={created}")
        else:
            self.log_bus.publish(f"注册结果回收未发现新账号: task_id={task_id}")
        return created

    def _record_register_failure(self, pipeline_task: PipelineTask, task_id: str, snapshot: dict[str, Any] | None) -> None:
        detail = snapshot or {}
        errors = detail.get("errors") if isinstance(detail.get("errors"), list) else []
        first_error = ""
        for error in errors:
            text = str(error or "").strip()
            if text:
                first_error = text
                break
        message = str(first_error or detail.get("error") or f"注册任务失败: {task_id}").strip()
        item = PipelineAccountItem(
            pipeline_task_id=int(pipeline_task.id or 0),
            source="pipeline_register",
            source_register_task_id=task_id,
            pipeline_status="failed",
            register_stage="failed",
            payment_stage="pending",
            auth_stage="disabled",
            register_error_code="registration_failed",
            register_error_reason=message,
            register_error_detail=message,
            success_summary="",
        )
        self.state_store.create_account_item(item)
