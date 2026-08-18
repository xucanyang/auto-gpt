import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from api import tasks as tasks_api
from core import db as core_db
from core.db import (
    RegistrationPaypalPaymentEventModel,
    TaskLog,
    TaskLogSummaryModel,
)


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
        with Session(self.engine) as session:
            session.add(
                RegistrationPaypalPaymentEventModel(
                    task_id="task_group",
                    account_id=7,
                    account_email_masked="d***@example.com",
                    stage="payment_submitted",
                    level="info",
                    message="PayPal approval URL 已提交支付队列",
                    idempotency_key="task-group-payment-submitted",
                )
            )
            session.commit()
        tasks_api.get_logs(platform="chatgpt", page=1, page_size=50)

        result = tasks_api.batch_delete_logs(tasks_api.TaskLogBatchDeleteRequest(ids=[int(first.id or 0)]))

        self.assertEqual(result["deleted"], 1)
        self.assertEqual(result["deleted_records"], 2)
        with Session(self.engine) as session:
            rows = session.exec(select(TaskLog)).all()
            summaries = session.exec(select(TaskLogSummaryModel)).all()
            payment_events = session.exec(
                select(RegistrationPaypalPaymentEventModel)
            ).all()
        self.assertEqual(rows, [])
        self.assertEqual(summaries, [])
        self.assertEqual(payment_events, [])

    def test_paypal_events_are_merged_into_all_durable_task_detail_reads(self):
        task_id = "task_paypal_timeline"
        log = self._add_log(
            status="success",
            task_id=task_id,
            detail={
                "task_id": task_id,
                "source": "register",
                "status_snapshot": "done",
                "logs": ["registration finished"],
            },
        )
        with Session(self.engine) as session:
            event = RegistrationPaypalPaymentEventModel(
                task_id=task_id,
                account_id=17,
                account_email_masked="p***@example.com",
                stage="payment_authorized",
                level="info",
                message="支付结果已回读：PayPal 已授权/商户回跳成功",
                idempotency_key="task-paypal-authorized",
            )
            event.set_metadata({"batch_id": "batch123", "item_id": "item123"})
            session.add(event)
            session.commit()

        by_id = tasks_api.get_log_detail(int(log.id or 0))
        by_task = tasks_api.get_log_detail_by_task(task_id)
        expired_snapshot = tasks_api.get_task(task_id)

        for payload in (by_id, by_task):
            events = payload["detail"]["payment_events"]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["stage"], "payment_authorized")
            self.assertEqual(events[0]["metadata"]["batch_id"], "batch123")
        self.assertTrue(expired_snapshot["expired"])
        self.assertEqual(
            expired_snapshot["payment_events"][0]["stage"],
            "payment_authorized",
        )

    def test_cached_history_list_does_not_read_large_detail_rows(self):
        self._add_log(
            status="success",
            task_id="task_large_detail",
            detail={
                "task_id": "task_large_detail",
                "source": "batch_payment_methods",
                "success": 1,
                "logs": ["x" * (1024 * 1024)],
            },
        )
        self.assertEqual(tasks_api.backfill_task_log_summaries(), 1)

        statements: list[str] = []

        def capture_statement(_conn, _cursor, statement, _parameters, _context, _executemany):
            statements.append(str(statement))

        event.listen(self.engine, "before_cursor_execute", capture_statement)
        try:
            result = tasks_api.get_logs(
                platform="chatgpt",
                page=1,
                page_size=50,
                source="batch_payment_methods",
            )
        finally:
            event.remove(self.engine, "before_cursor_execute", capture_statement)

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["success"], 1)
        list_statements = "\n".join(statements).lower()
        self.assertIn("task_log_summaries", list_statements)
        self.assertNotIn("task_logs.detail_json", list_statements)

    def test_save_task_log_keeps_summary_projection_in_sync(self):
        task_id = "task_summary_projection_write"
        tasks_api._save_task_log(
            "chatgpt",
            "demo@example.com",
            "success",
            detail={
                "task_id": task_id,
                "source": "batch_zero_amount_eligibility",
                "progress": "2/2",
                "success": 2,
                "skipped": 0,
                "errors": [],
            },
        )

        with Session(self.engine) as session:
            row = session.exec(
                select(TaskLogSummaryModel).where(
                    TaskLogSummaryModel.task_id == task_id
                )
            ).one()
        cached = json.loads(row.summary_json)
        self.assertEqual(row.source, "batch_zero_amount_eligibility")
        self.assertEqual(cached["success"], 2)
        self.assertEqual(cached["total"], 2)

    def test_account_list_performance_indexes_are_declared(self):
        with self.engine.connect() as connection:
            index_rows = connection.exec_driver_sql(
                "PRAGMA index_list(accounts)"
            ).fetchall()
        index_names = {str(row[1]) for row in index_rows}
        self.assertIn("idx_accounts_status_platform", index_names)
        self.assertIn(
            "idx_accounts_platform_list_state_freshness",
            index_names,
        )

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
