import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy.pool import NullPool
from sqlmodel import Session, SQLModel, create_engine, select

from api import tasks
from core.db import AccountModel, TaskLog
from core.task_runtime import RegisterTaskStore
from services.account_filters import (
    refresh_account_list_state,
    refresh_stale_account_list_state,
)


class TaskLogCheckpointLockingTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(tasks._wait_for_task_log_checkpoints(5.0))
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "checkpoint.db"
        self.engine = create_engine(
            f"sqlite:///{self.db_path}",
            connect_args={"check_same_thread": False, "timeout": 0.1},
            poolclass=NullPool,
        )
        SQLModel.metadata.create_all(self.engine)
        self.store = RegisterTaskStore()
        self.store.set_terminal_callback(tasks._persist_terminal_task_snapshot)
        self.engine_patch = patch.object(tasks, "engine", self.engine)
        self.store_patch = patch.object(tasks, "_task_store", self.store)
        self.engine_patch.start()
        self.store_patch.start()
        with tasks._TASK_LOG_CHECKPOINT_LOCK:
            tasks._TASK_LOG_CHECKPOINT_STATE.clear()
        with tasks._TASK_LOG_CHECKPOINT_WRITE_LOCK:
            tasks._TASK_LOG_CHECKPOINT_PENDING.clear()

    def tearDown(self):
        self.assertTrue(tasks._wait_for_task_log_checkpoints(5.0))
        self.store_patch.stop()
        self.engine_patch.stop()
        self.engine.dispose()
        self._tmpdir.cleanup()

    def _create_running_task(self, task_id: str) -> None:
        self.store.create(
            task_id,
            platform="chatgpt",
            total=1,
            source="checkpoint_locking_test",
        )
        self.store.mark_running(task_id)

    def test_log_returns_immediately_while_another_connection_holds_write_lock(self):
        task_id = "task_checkpoint_busy_writer"
        self._create_running_task(task_id)
        persist_started = threading.Event()
        original_persist = tasks._persist_task_snapshot

        def observed_persist(*args, **kwargs):
            persist_started.set()
            return original_persist(*args, **kwargs)

        blocker = sqlite3.connect(self.db_path, timeout=0.1)
        blocker.execute("BEGIN IMMEDIATE")
        try:
            with (
                patch.object(tasks, "_TASK_LOG_CHECKPOINT_EVERY_ENTRIES", 1),
                patch.object(tasks, "_persist_task_snapshot", side_effect=observed_persist),
            ):
                started_at = time.monotonic()
                tasks._log(task_id, "checkpoint must not wait for SQLite")
                log_elapsed = time.monotonic() - started_at
                self.assertLess(log_elapsed, 0.5)
                self.assertTrue(persist_started.wait(1.0))

                # Keep the lock beyond the checkpoint connection's 100 ms
                # timeout so the retry path is exercised deterministically.
                time.sleep(0.15)
                blocker.commit()
                self.assertTrue(tasks._wait_for_task_log_checkpoints(3.0))
        finally:
            try:
                blocker.rollback()
            finally:
                blocker.close()

        with Session(self.engine) as session:
            row = session.exec(select(TaskLog).where(TaskLog.task_id == task_id)).one()
            detail = json.loads(row.detail_json)
        self.assertEqual(row.status, "running")
        self.assertEqual(detail["attempt_outcome"], "task_running_checkpoint")
        self.assertTrue(any("checkpoint must not wait" in line for line in detail["logs"]))

    def test_writer_coalesces_multiple_pending_snapshots_for_one_task(self):
        first_started = threading.Event()
        release_first = threading.Event()
        persisted_sequences: list[int] = []

        def controlled_persist(_task_id, *, snapshot, **_kwargs):
            persisted_sequences.append(int(snapshot["sequence"]))
            if int(snapshot["sequence"]) == 1:
                first_started.set()
                self.assertTrue(release_first.wait(2.0))

        with patch.object(tasks, "_persist_task_snapshot", side_effect=controlled_persist):
            tasks._queue_task_log_checkpoint("task_checkpoint_coalesce", {"sequence": 1})
            self.assertTrue(first_started.wait(1.0))
            tasks._queue_task_log_checkpoint("task_checkpoint_coalesce", {"sequence": 2})
            tasks._queue_task_log_checkpoint("task_checkpoint_coalesce", {"sequence": 3})
            release_first.set()
            self.assertTrue(tasks._wait_for_task_log_checkpoints(3.0))

        self.assertEqual(persisted_sequences, [1, 3])

    def test_late_running_checkpoint_cannot_overwrite_terminal_snapshot(self):
        task_id = "task_checkpoint_terminal_race"
        self._create_running_task(task_id)
        self.store.append_log(task_id, "running snapshot")
        running_snapshot = self.store.snapshot(task_id)
        checkpoint_started = threading.Event()
        release_checkpoint = threading.Event()
        original_persist = tasks._persist_task_snapshot

        def delayed_checkpoint(*args, **kwargs):
            if kwargs.get("attempt_outcome") == "task_running_checkpoint":
                checkpoint_started.set()
                self.assertTrue(release_checkpoint.wait(2.0))
            return original_persist(*args, **kwargs)

        with patch.object(tasks, "_persist_task_snapshot", side_effect=delayed_checkpoint):
            tasks._queue_task_log_checkpoint(task_id, running_snapshot)
            self.assertTrue(checkpoint_started.wait(1.0))
            self.store.append_log(task_id, "terminal snapshot")
            self.store.finish(
                task_id,
                status="done",
                success=1,
                skipped=0,
                errors=[],
            )
            release_checkpoint.set()
            self.assertTrue(tasks._wait_for_task_log_checkpoints(3.0))

        with Session(self.engine) as session:
            row = session.exec(select(TaskLog).where(TaskLog.task_id == task_id)).one()
            detail = json.loads(row.detail_json)
        self.assertEqual(row.status, "done")
        self.assertEqual(detail["status_snapshot"], "done")
        self.assertEqual(detail["attempt_outcome"], "task_done")
        self.assertEqual(detail["success"], 1)
        self.assertTrue(any("terminal snapshot" in line for line in detail["logs"]))

    def test_fresh_list_state_refresh_stays_read_only_under_other_writer(self):
        with Session(self.engine) as session:
            account = AccountModel(
                platform="chatgpt",
                email="fresh-list-state@example.com",
                password="pw",
            )
            session.add(account)
            session.commit()
            refresh_account_list_state(session, cleanup_orphans=False)

        blocker = sqlite3.connect(self.db_path, timeout=0.1)
        blocker.execute("BEGIN IMMEDIATE")
        started_at = time.monotonic()
        try:
            with Session(self.engine) as session:
                refreshed = refresh_stale_account_list_state(session)
        finally:
            blocker.rollback()
            blocker.close()
        self.assertEqual(refreshed, 0)
        self.assertLess(time.monotonic() - started_at, 0.5)


if __name__ == "__main__":
    unittest.main()
