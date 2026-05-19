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

    def _add_log(self, *, log_id: int | None = None, status: str, task_id: str, email: str = "demo@example.com"):
        with Session(self.engine) as session:
            row = TaskLog(
                id=log_id,
                platform="chatgpt",
                email=email,
                status=status,
                error="",
                detail_json=json.dumps({"task_id": task_id}, ensure_ascii=False),
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

    def test_running_log_without_runtime_task_is_normalized_to_stopped(self):
        self._add_log(status="running", task_id="task_missing")

        result = tasks_api.get_logs(platform="chatgpt", page=1, page_size=50)

        self.assertEqual(result["items"][0]["status"], "stopped")

    def test_batch_delete_logs_removes_whole_task_group(self):
        first = self._add_log(status="running", task_id="task_group")
        self._add_log(status="success", task_id="task_group")

        result = tasks_api.batch_delete_logs(tasks_api.TaskLogBatchDeleteRequest(ids=[int(first.id or 0)]))

        self.assertEqual(result["deleted"], 1)
        self.assertEqual(result["deleted_records"], 2)
        with Session(self.engine) as session:
            rows = session.exec(select(TaskLog)).all()
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
