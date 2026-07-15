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


if __name__ == "__main__":
    unittest.main()
