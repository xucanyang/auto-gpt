from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlmodel import Session, select

from core.db import engine

from .models import PipelineAccountItem, PipelineTask


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PipelineStateStore:
    """Persistent state store for pipeline tasks and account items."""

    def create_task(
        self,
        task_key: str,
        *,
        status: str = "stopped",
        config_snapshot: dict[str, Any] | None = None,
    ) -> PipelineTask:
        task = PipelineTask(
            task_key=task_key,
            status=status,
            config_snapshot_json=json.dumps(config_snapshot or {}, ensure_ascii=False),
        )
        with Session(engine) as session:
            session.add(task)
            session.commit()
            session.refresh(task)
        return task

    def get_task(self, task_id: int) -> PipelineTask | None:
        with Session(engine) as session:
            return session.get(PipelineTask, int(task_id or 0))

    def get_task_by_key(self, task_key: str) -> PipelineTask | None:
        with Session(engine) as session:
            return session.exec(
                select(PipelineTask).where(PipelineTask.task_key == str(task_key or "").strip())
            ).first()

    def get_latest_task(self) -> PipelineTask | None:
        with Session(engine) as session:
            return session.exec(
                select(PipelineTask).order_by(PipelineTask.created_at.desc(), PipelineTask.id.desc())
            ).first()

    def save_task(self, task: PipelineTask) -> PipelineTask:
        task.updated_at = _utcnow()
        with Session(engine) as session:
            merged = session.merge(task)
            session.add(merged)
            session.commit()
            session.refresh(merged)
            return merged
        return task

    def append_task_log(self, task_id: int, line: str, *, limit: int = 500) -> PipelineTask | None:
        text = str(line or "").strip()
        if not text:
            return self.get_task(int(task_id or 0))
        with Session(engine) as session:
            task = session.get(PipelineTask, int(task_id or 0))
            if task is None:
                return None
            try:
                logs = json.loads(task.logs_json or "[]")
                if not isinstance(logs, list):
                    logs = []
            except Exception:
                logs = []
            logs.append(text)
            task.logs_json = json.dumps(logs[-max(1, int(limit or 1)):], ensure_ascii=False)
            task.updated_at = _utcnow()
            session.add(task)
            session.commit()
            session.refresh(task)
            return task

    def replace_task_logs(self, task_id: int, lines: list[str], *, limit: int = 500) -> PipelineTask | None:
        normalized = [str(line or "").strip() for line in lines if str(line or "").strip()]
        with Session(engine) as session:
            task = session.get(PipelineTask, int(task_id or 0))
            if task is None:
                return None
            task.logs_json = json.dumps(normalized[-max(1, int(limit or 1)):], ensure_ascii=False)
            task.updated_at = _utcnow()
            session.add(task)
            session.commit()
            session.refresh(task)
            return task

    def list_task_logs(self, task_id: int) -> list[str]:
        task = self.get_task(int(task_id or 0))
        if task is None:
            return []
        try:
            logs = json.loads(task.logs_json or "[]")
            if isinstance(logs, list):
                return [str(line or "") for line in logs if str(line or "").strip()]
        except Exception:
            pass
        return []

    def list_tasks(self, limit: int = 20) -> list[PipelineTask]:
        size = max(1, min(int(limit or 20), 200))
        with Session(engine) as session:
            return list(
                session.exec(
                    select(PipelineTask)
                    .order_by(PipelineTask.created_at.desc(), PipelineTask.id.desc())
                    .limit(size)
                ).all()
            )

    def create_account_item(self, item: PipelineAccountItem) -> PipelineAccountItem:
        with Session(engine) as session:
            session.add(item)
            session.commit()
            session.refresh(item)
            return item

    def get_account_item_by_account_id(
        self,
        pipeline_task_id: int,
        account_id: int,
    ) -> PipelineAccountItem | None:
        with Session(engine) as session:
            return session.exec(
                select(PipelineAccountItem)
                .where(PipelineAccountItem.pipeline_task_id == int(pipeline_task_id or 0))
                .where(PipelineAccountItem.account_id == int(account_id or 0))
            ).first()

    def get_account_item(self, item_id: int) -> PipelineAccountItem | None:
        with Session(engine) as session:
            return session.get(PipelineAccountItem, int(item_id or 0))

    def list_account_items(self, pipeline_task_id: int) -> list[PipelineAccountItem]:
        with Session(engine) as session:
            return list(
                session.exec(
                    select(PipelineAccountItem)
                    .where(PipelineAccountItem.pipeline_task_id == int(pipeline_task_id or 0))
                    .order_by(PipelineAccountItem.created_at.asc(), PipelineAccountItem.id.asc())
                ).all()
            )

    def list_account_items_by_batch(self, pipeline_task_id: int, batch_id: str) -> list[PipelineAccountItem]:
        target_batch_id = str(batch_id or "").strip()
        if not target_batch_id:
            return []
        with Session(engine) as session:
            return list(
                session.exec(
                    select(PipelineAccountItem)
                    .where(PipelineAccountItem.pipeline_task_id == int(pipeline_task_id or 0))
                    .where(PipelineAccountItem.payment_batch_task_id == target_batch_id)
                    .order_by(PipelineAccountItem.created_at.asc(), PipelineAccountItem.id.asc())
                ).all()
            )

    def save_account_item(self, item: PipelineAccountItem) -> PipelineAccountItem:
        item.updated_at = _utcnow()
        with Session(engine) as session:
            merged = session.merge(item)
            session.add(merged)
            session.commit()
            session.refresh(merged)
            return merged

    def update_account_item(self, item_id: int, **patch: Any) -> PipelineAccountItem | None:
        with Session(engine) as session:
            item = session.get(PipelineAccountItem, int(item_id or 0))
            if item is None:
                return None
            for key, value in patch.items():
                if hasattr(item, key):
                    setattr(item, key, value)
            item.updated_at = _utcnow()
            session.add(item)
            session.commit()
            session.refresh(item)
            return item

    def reserve_pending_payment_items(
        self,
        pipeline_task_id: int,
        *,
        limit: int,
    ) -> list[PipelineAccountItem]:
        size = max(1, int(limit or 1))
        with Session(engine) as session:
            candidates = list(
                session.exec(
                    select(PipelineAccountItem)
                    .where(PipelineAccountItem.pipeline_task_id == int(pipeline_task_id or 0))
                    .where(PipelineAccountItem.pipeline_status == "pending_payment")
                    .order_by(PipelineAccountItem.created_at.asc(), PipelineAccountItem.id.asc())
                    .limit(size)
                ).all()
            )
            if not candidates:
                return []

            now = _utcnow()
            reserved: list[PipelineAccountItem] = []
            for item in candidates:
                if str(item.pipeline_status or "") != "pending_payment":
                    continue
                item.pipeline_status = "payment_reserved"
                item.payment_stage = "reserved"
                item.updated_at = now
                session.add(item)
                reserved.append(item)
            session.commit()
            for item in reserved:
                session.refresh(item)
            return reserved

    def list_pending_payment_items(self, pipeline_task_id: int) -> list[PipelineAccountItem]:
        return self._list_items_by_status(pipeline_task_id, ["pending_payment"])

    def list_paid_items(self, pipeline_task_id: int) -> list[PipelineAccountItem]:
        return self._list_items_by_status(pipeline_task_id, ["paid"])

    def list_failed_items(self, pipeline_task_id: int) -> list[PipelineAccountItem]:
        return self._list_items_by_status(pipeline_task_id, ["failed", "auth_failed"])

    def list_auth_pending_items(self, pipeline_task_id: int) -> list[PipelineAccountItem]:
        return self._list_items_by_status(pipeline_task_id, ["auth_pending"])

    def list_task_history(self, limit: int = 20) -> list[PipelineTask]:
        return self.list_tasks(limit=limit)

    def _list_items_by_status(self, pipeline_task_id: int, statuses: list[str]) -> list[PipelineAccountItem]:
        normalized = [str(status or "").strip() for status in statuses if str(status or "").strip()]
        if not normalized:
            return []
        with Session(engine) as session:
            return list(
                session.exec(
                    select(PipelineAccountItem)
                    .where(PipelineAccountItem.pipeline_task_id == int(pipeline_task_id or 0))
                    .where(PipelineAccountItem.pipeline_status.in_(normalized))
                    .order_by(PipelineAccountItem.updated_at.desc(), PipelineAccountItem.id.desc())
                ).all()
            )
