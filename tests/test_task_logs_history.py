import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sqlmodel import Session, SQLModel, create_engine, select

from api import tasks as tasks_api
from core import db as core_db
from core.db import TaskLog


class TaskLogHistoryTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "task_logs.db"
        self.engine = create_engine(f"sqlite:///{db_path}")
        self.core_engine_patch = mock.patch.object(core_db, "engine", self.engine)
        self.tasks_engine_patch = mock.patch.object(tasks_api, "engine", self.engine)
        self.core_engine_patch.start()
        self.tasks_engine_patch.start()
        SQLModel.metadata.create_all(self.engine)

    def tearDown(self):
        self.tasks_engine_patch.stop()
        self.core_engine_patch.stop()
        self._tmpdir.cleanup()

    def _add_log(
        self,
        *,
        log_id: int | None = None,
        status: str,
        task_id: str,
        email: str = "demo@example.com",
        detail: dict | None = None,
    ):
        with Session(self.engine) as session:
            row = TaskLog(
                id=log_id,
                task_id=task_id,
                platform="chatgpt",
                email=email,
                status=status,
                error="",
                detail_json=json.dumps(detail or {"task_id": task_id}, ensure_ascii=False),
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return row

    def test_get_logs_returns_latest_log_per_task(self):
        self._add_log(status="running", task_id="task_a")
        self._add_log(status="success", task_id="task_a")
        self._add_log(status="running", task_id="task_b")

        result = tasks_api.get_logs(platform="chatgpt", page=1, page_size=50)

        self.assertEqual(result["total"], 2)
        by_task = {item["task_id"]: item for item in result["items"]}
        self.assertEqual(by_task["task_a"]["status"], "success")
        self.assertEqual(by_task["task_b"]["status"], "stopped")

    def test_get_logs_source_filter_supports_top_level_and_legacy_meta_source(self):
        self._add_log(
            status="success",
            task_id="task_payment",
            detail={
                "task_id": "task_payment",
                "source": "batch_payment_methods",
                "progress": "1/1",
            },
        )
        self._add_log(
            status="success",
            task_id="task_zero",
            detail={
                "task_id": "task_zero",
                "meta": {"source": "batch_zero_amount_eligibility"},
            },
        )

        result = tasks_api.get_logs(
            platform="chatgpt",
            page=1,
            page_size=50,
            source="batch_payment_methods",
        )

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["task_id"], "task_payment")

    def test_running_log_without_runtime_task_is_normalized_to_stopped(self):
        self._add_log(status="running", task_id="task_missing")

        result = tasks_api.get_logs(platform="chatgpt", page=1, page_size=50)

        self.assertEqual(result["items"][0]["status"], "stopped")

    def test_explicit_legacy_success_is_not_reclassified_when_snapshot_was_running(self):
        with Session(self.engine) as session:
            row = TaskLog(
                task_id="task_legacy_success",
                platform="chatgpt",
                email="demo@example.com",
                status="success",
                detail_json=json.dumps(
                    {
                        "task_id": "task_legacy_success",
                        "status_snapshot": "running",
                        "progress": "7/7",
                        "attempt_outcome": "batch_sub2api_upload_success",
                    },
                    ensure_ascii=False,
                ),
            )
            session.add(row)
            session.commit()

        result = tasks_api.get_logs(platform="chatgpt", page=1, page_size=50)

        self.assertEqual(result["items"][0]["status"], "success")

    def test_batch_delete_logs_removes_whole_task_group(self):
        first = self._add_log(status="running", task_id="task_group")
        self._add_log(status="success", task_id="task_group")

        result = tasks_api.batch_delete_logs(tasks_api.TaskLogBatchDeleteRequest(ids=[int(first.id or 0)]))

        self.assertEqual(result["deleted"], 1)
        self.assertEqual(result["deleted_records"], 2)
        with Session(self.engine) as session:
            rows = session.exec(select(TaskLog)).all()
        self.assertEqual(rows, [])

    def test_detail_read_recovers_logs_from_legacy_duplicate_rows(self):
        task_id = "task_legacy_duplicate_rows"
        with Session(self.engine) as session:
            first = TaskLog(
                task_id=task_id,
                platform="chatgpt",
                email="demo@example.com",
                status="running",
                detail_json=json.dumps(
                    {
                        "task_id": task_id,
                        "status_snapshot": "running",
                        "logs": ["line-1", "line-2"],
                        "log_start_index": 0,
                        "log_next_index": 2,
                    },
                    ensure_ascii=False,
                ),
            )
            second = TaskLog(
                task_id=task_id,
                platform="chatgpt",
                email="demo@example.com",
                status="interrupted",
                detail_json=json.dumps(
                    {
                        "task_id": task_id,
                        "status_snapshot": "interrupted",
                        "logs": ["line-2", "line-3"],
                        "log_start_index": 1,
                        "log_next_index": 3,
                        "errors": ["remote interrupted"],
                    },
                    ensure_ascii=False,
                ),
            )
            session.add(first)
            session.commit()
            session.refresh(first)
            session.add(second)
            session.commit()
            session.refresh(second)
            second_id = int(second.id or 0)

        payload = tasks_api.get_log_detail(second_id)
        assert payload["status"] == "interrupted"
        assert payload["detail"]["logs"] == ["line-1", "line-2", "line-3"]


if __name__ == "__main__":
    unittest.main()
