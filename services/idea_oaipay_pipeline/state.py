from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlmodel import Session, select

from core.db import engine
from .models import IdeaOaiPayPipelineItem, IdeaOaiPayPipelineTask


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _json_loads_object(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _json_loads_list(raw: str) -> list[Any]:
    try:
        data = json.loads(raw or "[]")
    except Exception:
        return []
    return data if isinstance(data, list) else []


class IdeaOaiPayPipelineStateStore:
    def create_task(
        self,
        task_key: str,
        *,
        status: str,
        source_type: str,
        target_success_count: int,
        config: dict[str, Any],
        runtime_config: dict[str, Any] | None = None,
    ) -> IdeaOaiPayPipelineTask:
        task = IdeaOaiPayPipelineTask(
            task_key=task_key,
            status=status,
            source_type=source_type,
            target_success_count=max(0, int(target_success_count or 0)),
            config_json=json.dumps(config or {}, ensure_ascii=False),
            runtime_config_json=json.dumps(runtime_config or config or {}, ensure_ascii=False),
            started_at=_utcnow() if status == "running" else None,
        )
        with Session(engine) as session:
            session.add(task)
            session.commit()
            session.refresh(task)
            return task

    def save_task(self, task: IdeaOaiPayPipelineTask) -> IdeaOaiPayPipelineTask:
        task.updated_at = _utcnow()
        with Session(engine) as session:
            merged = session.merge(task)
            session.add(merged)
            session.commit()
            session.refresh(merged)
            return merged

    def get_task(self, task_id: int) -> IdeaOaiPayPipelineTask | None:
        with Session(engine) as session:
            return session.get(IdeaOaiPayPipelineTask, int(task_id or 0))

    def get_latest_task(self) -> IdeaOaiPayPipelineTask | None:
        with Session(engine) as session:
            return session.exec(
                select(IdeaOaiPayPipelineTask).order_by(
                    IdeaOaiPayPipelineTask.created_at.desc(),
                    IdeaOaiPayPipelineTask.id.desc(),
                )
            ).first()

    def list_tasks(self, limit: int = 20) -> list[IdeaOaiPayPipelineTask]:
        size = max(1, min(int(limit or 20), 200))
        with Session(engine) as session:
            return list(
                session.exec(
                    select(IdeaOaiPayPipelineTask)
                    .order_by(IdeaOaiPayPipelineTask.created_at.desc(), IdeaOaiPayPipelineTask.id.desc())
                    .limit(size)
                ).all()
            )

    def task_config(self, task: IdeaOaiPayPipelineTask) -> dict[str, Any]:
        return _json_loads_object(task.config_json)

    def task_runtime_config(self, task: IdeaOaiPayPipelineTask) -> dict[str, Any]:
        return _json_loads_object(task.runtime_config_json)

    def append_task_log(self, task_id: int, line: str, *, limit: int = 800) -> None:
        text = str(line or "").strip()
        if not text:
            return
        with Session(engine) as session:
            task = session.get(IdeaOaiPayPipelineTask, int(task_id or 0))
            if task is None:
                return
            logs = _json_loads_list(task.logs_json)
            logs.append(f"[{_utcnow().strftime('%H:%M:%S')}] {text}")
            task.logs_json = json.dumps(logs[-max(1, int(limit or 1)):], ensure_ascii=False)
            task.updated_at = _utcnow()
            session.add(task)
            session.commit()

    def list_task_logs(self, task_id: int) -> list[str]:
        task = self.get_task(int(task_id or 0))
        if task is None:
            return []
        return [str(item or "") for item in _json_loads_list(task.logs_json) if str(item or "").strip()]

    def create_item(self, item: IdeaOaiPayPipelineItem) -> IdeaOaiPayPipelineItem:
        with Session(engine) as session:
            session.add(item)
            session.commit()
            session.refresh(item)
            return item

    def bulk_create_items(self, items: list[IdeaOaiPayPipelineItem]) -> list[IdeaOaiPayPipelineItem]:
        if not items:
            return []
        with Session(engine) as session:
            for item in items:
                session.add(item)
            session.commit()
            for item in items:
                session.refresh(item)
            return items

    def get_item(self, item_id: int) -> IdeaOaiPayPipelineItem | None:
        with Session(engine) as session:
            return session.get(IdeaOaiPayPipelineItem, int(item_id or 0))

    def get_item_by_account_id(self, pipeline_task_id: int, account_id: int) -> IdeaOaiPayPipelineItem | None:
        with Session(engine) as session:
            return session.exec(
                select(IdeaOaiPayPipelineItem)
                .where(IdeaOaiPayPipelineItem.pipeline_task_id == int(pipeline_task_id or 0))
                .where(IdeaOaiPayPipelineItem.account_id == int(account_id or 0))
                .order_by(IdeaOaiPayPipelineItem.id.desc())
            ).first()

    def list_items(self, pipeline_task_id: int, *, limit: int = 1000) -> list[IdeaOaiPayPipelineItem]:
        size = max(1, min(int(limit or 1000), 5000))
        with Session(engine) as session:
            return list(
                session.exec(
                    select(IdeaOaiPayPipelineItem)
                    .where(IdeaOaiPayPipelineItem.pipeline_task_id == int(pipeline_task_id or 0))
                    .order_by(IdeaOaiPayPipelineItem.created_at.asc(), IdeaOaiPayPipelineItem.id.asc())
                    .limit(size)
                ).all()
            )

    def list_items_by_statuses(
        self,
        pipeline_task_id: int,
        *,
        overall_statuses: list[str] | None = None,
        idea_stages: list[str] | None = None,
        check_stages: list[str] | None = None,
        phone_stages: list[str] | None = None,
        oaipay_stages: list[str] | None = None,
        limit: int = 200,
    ) -> list[IdeaOaiPayPipelineItem]:
        with Session(engine) as session:
            stmt = select(IdeaOaiPayPipelineItem).where(IdeaOaiPayPipelineItem.pipeline_task_id == int(pipeline_task_id or 0))
            if overall_statuses:
                stmt = stmt.where(IdeaOaiPayPipelineItem.overall_status.in_(overall_statuses))
            if idea_stages:
                stmt = stmt.where(IdeaOaiPayPipelineItem.idea_stage.in_(idea_stages))
            if check_stages:
                stmt = stmt.where(IdeaOaiPayPipelineItem.check_stage.in_(check_stages))
            if phone_stages:
                stmt = stmt.where(IdeaOaiPayPipelineItem.phone_stage.in_(phone_stages))
            if oaipay_stages:
                stmt = stmt.where(IdeaOaiPayPipelineItem.oaipay_stage.in_(oaipay_stages))
            stmt = stmt.order_by(IdeaOaiPayPipelineItem.updated_at.asc(), IdeaOaiPayPipelineItem.id.asc()).limit(max(1, min(int(limit or 200), 1000)))
            return list(session.exec(stmt).all())

    def update_item(self, item_id: int, **patch: Any) -> IdeaOaiPayPipelineItem | None:
        with Session(engine) as session:
            item = session.get(IdeaOaiPayPipelineItem, int(item_id or 0))
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

    def update_task_summary(self, task_id: int) -> dict[str, Any]:
        items = self.list_items(task_id, limit=5000)
        summary = {
            "total": len(items),
            "done": 0,
            "failed": 0,
            "manual_required": 0,
            "running": 0,
            "registered": 0,
            "idea_paid": 0,
            "check_pass": 0,
            "phone_success": 0,
            "oaipay_success": 0,
        }
        for item in items:
            overall = str(item.overall_status or "").strip()
            if overall in summary:
                summary[overall] += 1
            if item.register_stage == "success":
                summary["registered"] += 1
            if item.idea_stage in {"paid", "skipped", "disabled"}:
                summary["idea_paid"] += 1 if item.idea_stage == "paid" else 0
            if item.gate_stage in {"pass", "skipped"}:
                summary["check_pass"] += 1
            if item.phone_stage == "success":
                summary["phone_success"] += 1
            if item.oaipay_stage in {"uploaded", "exists"}:
                summary["oaipay_success"] += 1
        with Session(engine) as session:
            task = session.get(IdeaOaiPayPipelineTask, int(task_id or 0))
            if task is not None:
                task.summary_json = json.dumps(summary, ensure_ascii=False)
                task.updated_at = _utcnow()
                session.add(task)
                session.commit()
        return summary
