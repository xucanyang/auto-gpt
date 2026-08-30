import unittest
from unittest import mock

from core.task_runtime import (
    RegisterTaskControl,
    RegisterTaskStore,
    SkipCurrentAttemptRequested,
    StopTaskRequested,
)


class RegisterTaskControlTests(unittest.TestCase):
    def test_skip_request_is_consumed_only_once(self):
        control = RegisterTaskControl()

        control.request_skip_current()

        with self.assertRaises(SkipCurrentAttemptRequested):
            control.checkpoint()

        control.checkpoint()

    def test_stop_request_is_sticky(self):
        control = RegisterTaskControl()

        control.request_stop()

        with self.assertRaises(StopTaskRequested):
            control.checkpoint()

    def test_after_current_keeps_active_attempt_running_but_closes_start_gate(self):
        control = RegisterTaskControl()
        active_attempt = control.start_attempt()

        self.assertIsNotNone(active_attempt)
        self.assertTrue(control.request_stop_after_current())
        self.assertFalse(control.request_stop_after_current())

        # Graceful stop is a scheduling boundary, not a checkpoint interrupt.
        control.checkpoint(attempt_id=active_attempt)
        self.assertIsNone(control.start_attempt())
        snapshot = control.snapshot()
        self.assertTrue(snapshot["stop_after_current_requested"])
        self.assertFalse(snapshot["stop_requested"])
        self.assertEqual(snapshot["stop_mode"], "after_current")
        self.assertEqual(snapshot["active_attempts"], 1)

        control.finish_attempt(active_attempt)

    def test_immediate_stop_overrides_after_current_and_remains_sticky(self):
        control = RegisterTaskControl()
        attempt = control.start_attempt()
        self.assertTrue(control.request_stop_after_current())
        self.assertTrue(control.request_stop())
        self.assertFalse(control.request_stop())
        self.assertFalse(control.request_stop_after_current())

        snapshot = control.snapshot()
        self.assertTrue(snapshot["stop_requested"])
        self.assertFalse(snapshot["stop_after_current_requested"])
        self.assertEqual(snapshot["stop_mode"], "immediate")
        with self.assertRaises(StopTaskRequested):
            control.checkpoint(attempt_id=attempt)
        control.finish_attempt(attempt)
        with self.assertRaises(StopTaskRequested):
            control.checkpoint()

    def test_completion_stop_interrupts_attempts_without_becoming_user_stop(self):
        control = RegisterTaskControl()
        attempt = control.start_attempt()
        self.assertIsNotNone(attempt)

        self.assertTrue(control.request_completion_stop())
        self.assertFalse(control.request_completion_stop())

        snapshot = control.snapshot()
        self.assertFalse(snapshot["stop_requested"])
        self.assertTrue(snapshot["completion_stop_requested"])
        self.assertTrue(control.should_stop_starting_new_attempts())
        self.assertIsNone(control.start_attempt())
        with self.assertRaises(StopTaskRequested):
            control.checkpoint(attempt_id=attempt)

        # A later explicit stop remains observable by post-processing code.
        self.assertTrue(control.request_stop())
        self.assertTrue(control.is_stop_requested())
        control.finish_attempt(attempt)

    def test_resumed_account_attempt_can_finish_internal_retries_after_graceful_stop(self):
        control = RegisterTaskControl()
        attempt = control.start_attempt()
        self.assertIsNotNone(attempt)
        self.assertTrue(control.request_stop_after_current())

        # Phone binding releases per-phone state between retries.  Re-entering
        # the same claimed account must remain legal after graceful stop.
        control.finish_attempt(attempt)
        control.resume_attempt(attempt)
        control.checkpoint(attempt_id=attempt)
        self.assertEqual(control.snapshot()["active_attempts"], 1)
        self.assertIsNone(control.start_attempt())
        control.finish_attempt(attempt)

    def test_controlled_task_sleep_checks_immediate_stop_between_slices(self):
        from api.tasks import _sleep_with_task_control

        control = RegisterTaskControl()
        attempt = control.start_attempt()
        self.assertIsNotNone(attempt)

        def request_stop_after_first_slice(_seconds: float) -> None:
            control.request_stop()

        with mock.patch("api.tasks.time.sleep", side_effect=request_stop_after_first_slice) as sleep:
            with self.assertRaises(StopTaskRequested):
                _sleep_with_task_control(control, 10, attempt_id=attempt, interval_seconds=0.5)

        sleep.assert_called_once_with(0.5)

    def test_skip_current_targets_only_active_attempts_in_multithread_mode(self):
        control = RegisterTaskControl()
        attempt_a = control.start_attempt()
        attempt_b = control.start_attempt()

        control.request_skip_current()

        with self.assertRaises(SkipCurrentAttemptRequested):
            control.checkpoint(attempt_id=attempt_a)
        with self.assertRaises(SkipCurrentAttemptRequested):
            control.checkpoint(attempt_id=attempt_b)

        control.finish_attempt(attempt_a)
        control.finish_attempt(attempt_b)

        attempt_c = control.start_attempt()
        control.checkpoint(attempt_id=attempt_c)
        control.finish_attempt(attempt_c)


class RegisterTaskStoreTests(unittest.TestCase):
    def test_finish_only_honors_immediate_stop_when_explicitly_requested(self):
        store = RegisterTaskStore()
        for task_id, respect_immediate_stop, expected_status in (
            ("task-runtime-default-finish", False, "done"),
            ("task-runtime-stop-aware-finish", True, "stopped"),
        ):
            store.create(task_id, platform="chatgpt", total=1, source="unit")
            store.request_stop(task_id)
            store.finish(
                task_id,
                status="done",
                success=1,
                skipped=0,
                errors=[],
                respect_immediate_stop=respect_immediate_stop,
            )
            self.assertEqual(store.snapshot(task_id)["status"], expected_status)

    def test_snapshot_contains_control_and_skip_fields(self):
        store = RegisterTaskStore()
        task_id = "task-runtime-snapshot"

        store.create(
            task_id,
            platform="chatgpt",
            total=2,
            source="manual",
            meta={"scope": "unit"},
        )
        store.request_skip_current(task_id)
        store.finish(
            task_id,
            status="done",
            success=1,
            skipped=1,
            errors=["error-a"],
        )

        snapshot = store.snapshot(task_id)

        self.assertEqual(snapshot["success"], 1)
        self.assertEqual(snapshot["skipped"], 1)
        self.assertEqual(snapshot["errors"], ["error-a"])
        self.assertEqual(
            snapshot["control"]["pending_skip_requests"],
            1,
        )

    def test_after_current_capability_is_explicit_and_rejects_other_tasks(self):
        store = RegisterTaskStore()
        store.create(
            "task-runtime-graceful",
            platform="chatgpt",
            total=2,
            source="manual",
            supports_after_current=True,
        )
        response = store.request_stop_after_current("task-runtime-graceful")
        self.assertTrue(response["changed"])
        self.assertTrue(store.snapshot("task-runtime-graceful")["capabilities"]["stop_after_current"])

        store.create(
            "task-runtime-immediate-only",
            platform="chatgpt",
            total=1,
            source="batch",
        )
        with self.assertRaises(ValueError):
            store.request_stop_after_current("task-runtime-immediate-only")

    def test_active_task_logs_are_bounded_by_entry_count_and_utf8_bytes(self):
        store = RegisterTaskStore(
            active_max_log_entries=3,
            active_max_log_bytes=8,
            finished_max_log_entries=3,
            finished_max_log_bytes=8,
        )
        task_id = "task-runtime-log-bounds"
        store.create(task_id, platform="chatgpt", total=1, source="unit")

        for entry in ("old", "汉", "middle", "new"):
            store.append_log(task_id, entry)

        snapshot = store.snapshot(task_id)
        self.assertEqual(snapshot["logs"], ["new"])
        self.assertTrue(snapshot["logs_truncated"])
        self.assertEqual(snapshot["dropped_log_entries"], 3)
        self.assertEqual(snapshot["dropped_log_bytes"], 12)
        self.assertEqual(snapshot["retained_log_bytes"], 3)
        self.assertEqual(snapshot["log_start_index"], 3)
        self.assertEqual(snapshot["log_next_index"], 4)

        logs, status, start_index, next_index = store.log_window_state(task_id)
        self.assertEqual(logs, ["new"])
        self.assertEqual(status, "pending")
        self.assertEqual((start_index, next_index), (3, 4))

    def test_terminal_callback_observes_active_window_before_memory_compaction(self):
        persisted: list[dict] = []
        store = RegisterTaskStore(
            active_max_log_entries=10,
            active_max_log_bytes=1024,
            finished_max_log_entries=2,
            finished_max_log_bytes=1024,
            on_terminal=lambda _task_id, snapshot: persisted.append(snapshot),
        )
        task_id = "task-runtime-terminal-compaction"
        store.create(task_id, platform="chatgpt", total=1, source="unit")
        for entry in ("line-1", "line-2", "line-3", "line-4"):
            store.append_log(task_id, entry)

        store.finish(
            task_id,
            status="done",
            success=1,
            skipped=0,
            errors=[],
        )

        self.assertEqual(persisted[0]["logs"], ["line-1", "line-2", "line-3", "line-4"])
        in_memory = store.snapshot(task_id)
        self.assertEqual(in_memory["logs"], ["line-3", "line-4"])
        self.assertTrue(in_memory["logs_truncated"])
        self.assertEqual(in_memory["dropped_log_entries"], 2)


if __name__ == "__main__":
    unittest.main()
