import asyncio
import contextlib
import json
import os
import sys
import threading
import time
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from core.browser_runtime import resolve_browser_headless
from core.task_runtime import StopTaskRequested
from services.chatgpt_core.chatgpt_client import ChatGPTClient
from services.chatgpt_core import sentinel_browser as sentinel_browser_module
from services.chatgpt_core.sentinel_browser import (
    BrowserAccountCreateResult,
    BrowserOAuthTokenRecoveryResult,
    BrowserRegistrationStageResult,
    _BrowserWorkerOutcome,
    _create_account_via_browser_sync,
    _evaluate_complete_sentinel_token,
    _run_isolated_browser_transaction,
    _sentinel_token_field_state,
    create_account_via_browser,
    export_session_cookies_for_playwright,
    get_sentinel_token_via_browser,
    merge_playwright_cookies_into_session,
    run_any_auto_browser_registration_isolated,
    run_browser_registration_stage,
    run_browser_oauth_token_recovery,
    run_sync_playwright_safely,
    run_with_browser_capacity,
    run_with_persistent_browser_capacity,
)
from services.chatgpt_core.utils import generate_browser_fingerprint


_INLINE_WORKER_PREAMBLE = r"""
import json
import os
import subprocess
import sys
import time

protocol_fd = int(sys.argv[1])
os.set_inheritable(protocol_fd, False)
request = json.load(sys.stdin)

def emit(message):
    payload = (json.dumps(message, separators=(",", ":")) + "\n").encode()
    view = memoryview(payload)
    while view:
        written = os.write(protocol_fd, view)
        view = view[written:]
"""


def _inline_worker_command(script: str):
    return lambda protocol_fd: [sys.executable, "-c", script, str(protocol_fd)]


def _pid_is_running(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as handle:
            fields = handle.read().split()
    except FileNotFoundError:
        return False
    return len(fields) > 2 and fields[2] != "Z"


class SentinelBrowserRuntimeTests(unittest.TestCase):
    def setUp(self):
        with sentinel_browser_module._BROWSER_SLOT_STATE_LOCK:
            sentinel_browser_module._BROWSER_WAIT_QUEUE.clear()
            sentinel_browser_module._BROWSER_QUEUE_SEQUENCE = 0
            sentinel_browser_module._BROWSER_ACTIVE_COUNT = 0
            sentinel_browser_module._BROWSER_ACTIVE_LANE_COUNTS["registration"] = 0
            sentinel_browser_module._BROWSER_ACTIVE_LANE_COUNTS["recheck"] = 0
            sentinel_browser_module._BROWSER_REGISTRATION_WAITERS = 0
        with sentinel_browser_module._BROWSER_LAUNCH_STATE_LOCK:
            sentinel_browser_module._BROWSER_NEXT_LAUNCH_AT = 0.0
        with sentinel_browser_module._PID_PRESSURE_LOCK:
            sentinel_browser_module._PID_PRESSURE_BLOCKED = False
        with sentinel_browser_module._CPU_PRESSURE_LOCK:
            sentinel_browser_module._CPU_PRESSURE_BLOCKED = False

    def tearDown(self):
        with sentinel_browser_module._BROWSER_SLOT_STATE_LOCK:
            queue_depth = len(sentinel_browser_module._BROWSER_WAIT_QUEUE)
            registration_waiters = (
                sentinel_browser_module._BROWSER_REGISTRATION_WAITERS
            )
            sentinel_browser_module._BROWSER_WAIT_QUEUE.clear()
            sentinel_browser_module._BROWSER_ACTIVE_COUNT = 0
            sentinel_browser_module._BROWSER_ACTIVE_LANE_COUNTS["registration"] = 0
            sentinel_browser_module._BROWSER_ACTIVE_LANE_COUNTS["recheck"] = 0
            sentinel_browser_module._BROWSER_REGISTRATION_WAITERS = 0
        with sentinel_browser_module._BROWSER_LAUNCH_STATE_LOCK:
            sentinel_browser_module._BROWSER_NEXT_LAUNCH_AT = 0.0
        self.assertEqual(queue_depth, 0)
        self.assertEqual(registration_waiters, 0)

    def _wait_for_queue_depth(self, expected: int, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with sentinel_browser_module._BROWSER_SLOT_STATE_LOCK:
                if len(sentinel_browser_module._BROWSER_WAIT_QUEUE) == expected:
                    return
            time.sleep(0.01)
        with sentinel_browser_module._BROWSER_SLOT_STATE_LOCK:
            actual = len(sentinel_browser_module._BROWSER_WAIT_QUEUE)
        self.fail(f"browser queue depth did not reach {expected}; actual={actual}")

    def test_runtime_browser_limit_accepts_thirty(self):
        with mock.patch(
            "services.chatgpt_core.sentinel_browser._runtime_capacity_config",
            return_value={"max_concurrency": "30"},
        ):
            self.assertEqual(
                sentinel_browser_module._auth_browser_concurrency_limit(),
                30,
            )

    def test_dynamic_counter_can_lease_thirty_auth_browser_slots(self):
        with (
            mock.patch(
                "services.chatgpt_core.sentinel_browser._auth_browser_concurrency_limit",
                return_value=30,
            ),
            mock.patch(
                "services.chatgpt_core.sentinel_browser._auth_browser_capacity_mode",
                return_value="fixed",
            ),
            mock.patch(
                "services.chatgpt_core.sentinel_browser._auth_browser_launch_interval_seconds",
                return_value=0.0,
            ),
        ):
            with contextlib.ExitStack() as stack:
                for index in range(30):
                    stack.enter_context(
                        sentinel_browser_module.browser_capacity_slot(
                            f"thirty-slot-{index}",
                            priority="registration",
                        )
                    )
                self.assertEqual(sentinel_browser_module._BROWSER_ACTIVE_COUNT, 30)

        self.assertEqual(sentinel_browser_module._BROWSER_ACTIVE_COUNT, 0)

    def test_persistent_browser_limit_is_independent_and_bounded(self):
        with mock.patch(
            "services.chatgpt_core.sentinel_browser._runtime_capacity_config",
            return_value={"persistent_max_sessions": "4"},
        ):
            self.assertEqual(
                sentinel_browser_module.persistent_browser_session_limit(),
                4,
            )

    def test_persistent_browser_capacity_waits_without_consuming_auth_slots(self):
        first_started = threading.Event()
        second_started = threading.Event()
        release_first = threading.Event()
        logs: list[str] = []

        def hold_first():
            first_started.set()
            release_first.wait(timeout=3)
            return "first"

        def hold_second():
            second_started.set()
            return "second"

        with (
            mock.patch(
                "services.chatgpt_core.sentinel_browser._PERSISTENT_BROWSER_SEMAPHORE",
                threading.BoundedSemaphore(2),
            ),
            mock.patch(
                "services.chatgpt_core.sentinel_browser._PERSISTENT_BROWSER_ACTIVE_COUNT",
                0,
            ),
            mock.patch(
                "services.chatgpt_core.sentinel_browser._BROWSER_ACTIVE_COUNT",
                0,
            ),
            mock.patch(
                "services.chatgpt_core.sentinel_browser.persistent_browser_session_limit",
                return_value=1,
            ),
            mock.patch(
                "services.chatgpt_core.sentinel_browser._auth_browser_capacity_mode",
                return_value="fixed",
            ),
            mock.patch(
                "services.chatgpt_core.sentinel_browser._auth_browser_launch_interval_seconds",
                return_value=0,
            ),
        ):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(
                    run_with_persistent_browser_capacity,
                    "hold-first",
                    hold_first,
                    logger=logs.append,
                )
                self.assertTrue(first_started.wait(timeout=1))
                second = pool.submit(
                    run_with_persistent_browser_capacity,
                    "hold-second",
                    hold_second,
                    logger=logs.append,
                )
                time.sleep(0.15)
                self.assertFalse(second_started.is_set())
                self.assertEqual(sentinel_browser_module._BROWSER_ACTIVE_COUNT, 0)
                release_first.set()
                self.assertEqual(first.result(timeout=2), "first")
                self.assertEqual(second.result(timeout=2), "second")

        self.assertTrue(
            any(
                "persistent_browser_slot=waiting" in line
                and "reason=capacity" in line
                for line in logs
            )
        )

    def test_explicit_headed_mode_wins_over_container_headless_default(self):
        with mock.patch.dict(os.environ, {"PLAYWRIGHT_HEADLESS": "1"}):
            headless, reason = resolve_browser_headless(False)

        self.assertFalse(headless)
        self.assertEqual(reason, "requested:false")

    def test_strict_headless_mode_ignores_container_override(self):
        with mock.patch.dict(os.environ, {"PLAYWRIGHT_HEADLESS": "0"}):
            headless, reason = resolve_browser_headless(
                True,
                override_env_names=(),
            )

        self.assertTrue(headless)
        self.assertEqual(reason, "requested:true")

    def test_sync_playwright_helper_runs_directly_without_async_loop(self):
        current_thread = threading.get_ident()

        result = run_sync_playwright_safely(lambda: threading.get_ident())

        self.assertEqual(result, current_thread)

    def test_sync_playwright_helper_uses_clean_thread_inside_async_loop(self):
        logs: list[str] = []

        async def _run() -> tuple[int, int]:
            loop_thread = threading.get_ident()
            worker_thread = run_sync_playwright_safely(
                lambda: threading.get_ident(),
                logger=logs.append,
                label="Sentinel Browser",
            )
            return loop_thread, worker_thread

        loop_thread, worker_thread = asyncio.run(_run())

        self.assertNotEqual(worker_thread, loop_thread)
        self.assertTrue(any("asyncio loop" in item and "隔离线程" in item for item in logs))

    def test_browser_worker_streams_logs_and_normal_auth_result(self):
        script = _INLINE_WORKER_PREAMBLE + r"""
emit({"type": "log", "message": "worker-log-line"})
emit({"type": "result", "value": {"status_code": 200, "cookie_names": ["oai-sc"]}})
"""
        logs: list[str] = []
        with mock.patch(
            "services.chatgpt_core.sentinel_browser._browser_worker_command",
            side_effect=_inline_worker_command(script),
        ):
            result = create_account_via_browser(
                name="Worker Result",
                birthdate="1990-01-01",
                hard_timeout_seconds=2,
                log_fn=logs.append,
            )

        self.assertTrue(result and result.ok)
        self.assertEqual(result.cookie_names, ("oai-sc",))
        self.assertIn("worker-log-line", logs)

    def test_browser_worker_round_trips_otp_callback(self):
        script = r"""
import json
import os
import sys

protocol_fd = int(sys.argv[1])
os.set_inheritable(protocol_fd, False)
request = json.loads(sys.stdin.readline())

def emit(message):
    payload = (json.dumps(message, separators=(",", ":")) + "\n").encode()
    os.write(protocol_fd, payload)

emit({"type": "callback_request", "id": "otp-1", "name": "otp", "payload": {}})
response = json.loads(sys.stdin.readline())
emit({"type": "result", "value": {"request": request, "otp": response.get("value")}})
"""
        with mock.patch(
            "services.chatgpt_core.sentinel_browser._browser_worker_command",
            side_effect=_inline_worker_command(script),
        ), mock.patch(
            "services.chatgpt_core.shared_camoufox.shared_camoufox_preallocated_context_lease",
            return_value=contextlib.nullcontext(
                types.SimpleNamespace(
                    endpoint="ws://127.0.0.1:12345/test",
                    token="test-context-token",
                )
            ),
        ):
            outcome = _run_isolated_browser_transaction(
                "browser_registration",
                {"email": "buyer@example.com"},
                hard_timeout_seconds=2,
                logger=lambda _message: None,
                callbacks={"otp": lambda _payload: "123456"},
            )

        self.assertEqual(outcome.status, "ok")
        self.assertEqual(outcome.value["otp"], "123456")
        self.assertEqual(
            outcome.value["request"]["operation"], "browser_registration"
        )

    def test_any_auto_worker_without_capture_starts_without_parent_preallocation(self):
        script = _INLINE_WORKER_PREAMBLE + r"""
emit({"type": "result", "value": {"success": False, "error_message": "demo"}})
"""
        with (
            mock.patch(
                "services.chatgpt_core.sentinel_browser._browser_worker_command",
                side_effect=_inline_worker_command(script),
            ),
            mock.patch(
                "services.chatgpt_core.shared_camoufox.shared_camoufox_preallocated_context_lease",
            ) as preallocate,
        ):
            outcome = _run_isolated_browser_transaction(
                "any_auto_browser_registration",
                {"email": "buyer@example.com", "headless": True},
                hard_timeout_seconds=2,
                logger=lambda _message: None,
            )

        self.assertEqual(outcome.status, "ok")
        preallocate.assert_not_called()

    def test_any_auto_worker_capture_preallocates_exact_parent_context(self):
        script = _INLINE_WORKER_PREAMBLE + r"""
emit({"type": "result", "value": {
    "success": False,
    "error_message": "demo",
    "endpoint": os.environ.get("AUTO_GPT_SHARED_CAMOUFOX_WS_ENDPOINT"),
    "token": os.environ.get("AUTO_GPT_SHARED_CAMOUFOX_CONTEXT_TOKEN"),
}})
"""
        allocation = types.SimpleNamespace(
            endpoint="ws://127.0.0.1:12345/diagnostic-context",
            token="diagnostic-context-token",
            browser_fingerprint={"profile_id": "profile-demo"},
        )
        with (
            mock.patch(
                "services.chatgpt_core.sentinel_browser._browser_worker_command",
                side_effect=_inline_worker_command(script),
            ),
            mock.patch(
                "services.chatgpt_core.shared_camoufox.shared_camoufox_context_options",
                return_value=contextlib.nullcontext({"proxy": {"server": "http://127.0.0.1:19000"}}),
            ) as proxy_options,
            mock.patch(
                "services.chatgpt_core.shared_camoufox.shared_camoufox_preallocated_context_lease",
                return_value=contextlib.nullcontext(allocation),
            ) as preallocate,
        ):
            outcome = _run_isolated_browser_transaction(
                "any_auto_browser_registration",
                {
                    "email": "buyer@example.com",
                    "headless": True,
                    "proxy_url": "socks5h://user:pass@proxy.local:1080",
                    "diagnostic_capture_spec": {
                        "enabled": True,
                        "video_capture_enabled": False,
                    },
                },
                hard_timeout_seconds=2,
                logger=lambda _message: None,
            )

        self.assertEqual(outcome.status, "ok")
        self.assertEqual(outcome.value["endpoint"], allocation.endpoint)
        self.assertEqual(outcome.value["token"], allocation.token)
        proxy_options.assert_called_once_with(
            "socks5h://user:pass@proxy.local:1080",
            logger=mock.ANY,
        )
        preallocate.assert_called_once_with(
            True,
            context_options={"proxy": {"server": "http://127.0.0.1:19000"}},
            browser_fingerprint=None,
            logger=mock.ANY,
        )

    def test_any_auto_video_capture_stays_worker_owned(self):
        script = _INLINE_WORKER_PREAMBLE + r"""
emit({"type": "result", "value": {"success": False, "error_message": "demo"}})
"""
        with (
            mock.patch(
                "services.chatgpt_core.sentinel_browser._browser_worker_command",
                side_effect=_inline_worker_command(script),
            ),
            mock.patch(
                "services.chatgpt_core.shared_camoufox.shared_camoufox_preallocated_context_lease",
            ) as preallocate,
        ):
            outcome = _run_isolated_browser_transaction(
                "any_auto_browser_registration",
                {
                    "email": "buyer@example.com",
                    "headless": True,
                    "diagnostic_capture_spec": {
                        "enabled": True,
                        "video_capture_enabled": True,
                    },
                },
                hard_timeout_seconds=2,
                logger=lambda _message: None,
            )

        self.assertEqual(outcome.status, "ok")
        preallocate.assert_not_called()

    def test_browser_worker_propagates_stop_from_otp_callback(self):
        script = r"""
import json
import os
import sys

protocol_fd = int(sys.argv[1])
os.set_inheritable(protocol_fd, False)
json.loads(sys.stdin.readline())
payload = json.dumps(
    {"type": "callback_request", "id": "otp-1", "name": "otp", "payload": {}},
    separators=(",", ":"),
).encode() + b"\n"
os.write(protocol_fd, payload)
sys.stdin.readline()
"""

        def stop_callback(_payload):
            raise StopTaskRequested()

        with mock.patch(
            "services.chatgpt_core.sentinel_browser._browser_worker_command",
            side_effect=_inline_worker_command(script),
        ), mock.patch(
            "services.chatgpt_core.shared_camoufox.shared_camoufox_preallocated_context_lease",
            return_value=contextlib.nullcontext(
                types.SimpleNamespace(
                    endpoint="ws://127.0.0.1:12345/test",
                    token="test-context-token",
                )
            ),
        ):
            with self.assertRaises(StopTaskRequested):
                _run_isolated_browser_transaction(
                    "browser_registration",
                    {},
                    hard_timeout_seconds=2,
                    logger=lambda _message: None,
                    callbacks={"otp": stop_callback},
                )

    def test_browser_registration_stage_uses_shared_worker_gate(self):
        worker_outcome = _BrowserWorkerOutcome(
            status="ok",
            value={
                "final_state": {
                    "page_type": "oauth_callback",
                    "current_url": "https://chatgpt.com/api/auth/callback/openai?code=demo",
                },
                "page_url": "https://chatgpt.com/api/auth/callback/openai?code=demo",
                "cookies": [{"name": "login_session", "value": "demo"}],
                "cookie_names": ["login_session"],
                "device_id": "device-demo",
                "user_agent": "Mozilla/5.0",
            },
        )
        with mock.patch(
            "services.chatgpt_core.sentinel_browser._run_with_browser_slot",
            return_value=worker_outcome,
        ) as worker:
            result = run_browser_registration_stage(
                email="buyer@example.com",
                password="Password123!",
                otp_callback=lambda: "123456",
                proxy="http://proxy.local:8080",
                device_id="device-demo",
                hard_timeout_seconds=300,
            )

        self.assertIsInstance(result, BrowserRegistrationStageResult)
        self.assertTrue(result.ok)
        self.assertEqual(result.cookie_names, ("login_session",))
        self.assertIn("otp", worker.call_args.kwargs["callbacks"])

    def test_any_auto_full_registration_uses_killable_worker_gate(self):
        worker_outcome = _BrowserWorkerOutcome(
            status="ok",
            value={
                "success": True,
                "email": "buyer@example.com",
                "password": "Password123!",
                "access_token": "at-demo",
                "session_token": "session-demo",
                "cookies": "__Secure-next-auth.session-token=session-demo",
                "cookie_header": "__Secure-next-auth.session-token=session-demo",
                "transport": "any_auto_browser",
                "executor": "headless",
            },
        )
        otp_requests: list[dict] = []

        def otp_callback(payload):
            otp_requests.append(dict(payload or {}))
            return "123456"

        with mock.patch(
            "services.chatgpt_core.sentinel_browser._run_with_browser_slot",
            return_value=worker_outcome,
        ) as worker:
            result = run_any_auto_browser_registration_isolated(
                email="buyer@example.com",
                password="Password123!",
                proxy_url="http://proxy.local:8080",
                headless=True,
                otp_callback=otp_callback,
                profile_name="Example User",
                profile_birthdate="1990-01-02",
                hard_timeout_seconds=420,
            )

        self.assertTrue(result.ok)
        self.assertEqual(worker.call_args.args[0], "any_auto_browser_registration")
        self.assertEqual(worker.call_args.kwargs["priority"], "registration")
        self.assertEqual(worker.call_args.kwargs["hard_timeout_seconds"], 420)
        payload = worker.call_args.args[1]
        self.assertEqual(payload["proxy_url"], "http://proxy.local:8080")
        self.assertTrue(payload["headless"])
        self.assertFalse(payload["phone_callback_enabled"])
        self.assertEqual(
            worker.call_args.kwargs["callbacks"]["otp"]({"otp_sent_at": 123.0}),
            "123456",
        )
        self.assertEqual(otp_requests, [{"otp_sent_at": 123.0}])

    def test_any_auto_full_registration_maps_worker_timeout(self):
        with mock.patch(
            "services.chatgpt_core.sentinel_browser._run_with_browser_slot",
            return_value=_BrowserWorkerOutcome(
                status="timeout",
                error="any_auto_browser_registration hard timeout after 420.0s",
            ),
        ):
            result = run_any_auto_browser_registration_isolated(
                email="buyer@example.com",
                password="Password123!",
                proxy_url=None,
                headless=False,
                otp_callback=lambda: "123456",
                hard_timeout_seconds=420,
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.executor, "headed")
        self.assertIn("browser_registration_hard_timeout", result.error_message)

    def test_browser_oauth_recovery_uses_shared_worker_gate(self):
        worker_outcome = _BrowserWorkerOutcome(
            status="ok",
            value={
                "access_token": "at-demo",
                "refresh_token": "rt-demo",
                "id_token": "id-demo",
            },
        )
        with mock.patch(
            "services.chatgpt_core.sentinel_browser._run_with_browser_slot",
            return_value=worker_outcome,
        ) as worker:
            result = run_browser_oauth_token_recovery(
                email="buyer@example.com",
                password="Password123!",
                otp_callback=lambda: "123456",
                proxy="http://proxy.local:8080",
                device_id="device-demo",
                hard_timeout_seconds=300,
            )

        self.assertIsInstance(result, BrowserOAuthTokenRecoveryResult)
        self.assertTrue(result.ok)
        self.assertEqual(result.tokens["refresh_token"], "rt-demo")
        self.assertEqual(
            worker.call_args.args[0], "browser_oauth_token_recovery"
        )
        self.assertIn("otp", worker.call_args.kwargs["callbacks"])

    def test_browser_worker_hard_timeout_kills_entire_process_group(self):
        script = _INLINE_WORKER_PREAMBLE + r"""
child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(30)"],
    start_new_session=True,
)
emit({"type": "log", "message": "CHILD_PID=" + str(child.pid)})
time.sleep(30)
"""
        logs: list[str] = []
        with (
            mock.patch(
                "services.chatgpt_core.sentinel_browser._browser_worker_command",
                side_effect=_inline_worker_command(script),
            ),
            mock.patch(
                "services.chatgpt_core.sentinel_browser._BROWSER_WORKER_TERM_GRACE_SECONDS",
                0.1,
            ),
        ):
            outcome = _run_isolated_browser_transaction(
                "create_account",
                {},
                hard_timeout_seconds=0.3,
                logger=logs.append,
            )

        self.assertEqual(outcome.status, "timeout")
        child_pid = int(
            next(line.split("=", 1)[1] for line in logs if line.startswith("CHILD_PID="))
        )
        deadline = time.monotonic() + 2
        while _pid_is_running(child_pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(_pid_is_running(child_pid))

    def test_browser_worker_stop_check_interrupts_and_cleans_process_group(self):
        script = _INLINE_WORKER_PREAMBLE + r"""
child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(30)"],
    start_new_session=True,
)
emit({"type": "log", "message": "STOP_CHILD_PID=" + str(child.pid)})
time.sleep(30)
"""
        logs: list[str] = []

        class StopRequested(RuntimeError):
            pass

        def stop_check():
            if any(line.startswith("STOP_CHILD_PID=") for line in logs):
                raise StopRequested("stop now")

        with (
            mock.patch(
                "services.chatgpt_core.sentinel_browser._browser_worker_command",
                side_effect=_inline_worker_command(script),
            ),
            mock.patch(
                "services.chatgpt_core.sentinel_browser._BROWSER_WORKER_TERM_GRACE_SECONDS",
                0.1,
            ),
        ):
            with self.assertRaises(StopRequested):
                _run_isolated_browser_transaction(
                    "create_account",
                    {},
                    hard_timeout_seconds=5,
                    logger=logs.append,
                    stop_check=stop_check,
                )

        child_pid = int(
            next(
                line.split("=", 1)[1]
                for line in logs
                if line.startswith("STOP_CHILD_PID=")
            )
        )
        deadline = time.monotonic() + 2
        while _pid_is_running(child_pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(_pid_is_running(child_pid))

    def test_browser_timeout_releases_slot_for_next_transaction(self):
        hanging_script = _INLINE_WORKER_PREAMBLE + "\ntime.sleep(30)\n"
        success_script = _INLINE_WORKER_PREAMBLE + r"""
emit({"type": "result", "value": {"status_code": 200}})
"""
        commands = [
            _inline_worker_command(hanging_script),
            _inline_worker_command(success_script),
        ]

        def next_command(protocol_fd):
            return commands.pop(0)(protocol_fd)

        with (
            mock.patch(
                "services.chatgpt_core.sentinel_browser._browser_worker_command",
                side_effect=next_command,
            ),
            mock.patch(
                "services.chatgpt_core.sentinel_browser._BROWSER_WORKER_TERM_GRACE_SECONDS",
                0.1,
            ),
        ):
            timed_out = create_account_via_browser(
                name="Timeout",
                birthdate="1990-01-01",
                hard_timeout_seconds=0.2,
            )
            next_result = create_account_via_browser(
                name="Next",
                birthdate="1990-01-01",
                hard_timeout_seconds=2,
            )

        self.assertIn("auth_browser_hard_timeout", timed_out.error)
        self.assertTrue(next_result and next_result.ok)

    def test_sentinel_and_auth_share_process_wide_browser_concurrency_limit(self):
        active = 0
        peak = 0
        lock = threading.Lock()

        def fake_transaction(operation, _payload, **_kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            if operation == "sentinel_token":
                return _BrowserWorkerOutcome(status="ok", value="sentinel-token")
            return _BrowserWorkerOutcome(
                status="ok",
                value={"status_code": 200},
            )

        with (
            mock.patch(
                "services.chatgpt_core.sentinel_browser._auth_browser_concurrency_limit",
                return_value=1,
            ),
            mock.patch(
                "services.chatgpt_core.sentinel_browser._run_isolated_browser_transaction",
                side_effect=fake_transaction,
            ),
        ):
            with ThreadPoolExecutor(max_workers=2) as pool:
                auth_future = pool.submit(
                    create_account_via_browser,
                    name="User 1",
                    birthdate="1990-01-01",
                )
                sentinel_future = pool.submit(
                    get_sentinel_token_via_browser,
                    flow="password_verify",
                )
                auth_result = auth_future.result()
                sentinel_result = sentinel_future.result()

        self.assertEqual(peak, 1)
        self.assertTrue(auth_result and auth_result.ok)
        self.assertEqual(sentinel_result, "sentinel-token")

    def test_generic_browser_work_and_sentinel_share_capacity_limit(self):
        active = 0
        peak = 0
        lock = threading.Lock()

        def tracked(value):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            return value

        def fake_transaction(_operation, _payload, **_kwargs):
            tracked(None)
            return _BrowserWorkerOutcome(status="ok", value="sentinel-token")

        with (
            mock.patch(
                "services.chatgpt_core.sentinel_browser._auth_browser_concurrency_limit",
                return_value=1,
            ),
            mock.patch(
                "services.chatgpt_core.sentinel_browser._BROWSER_ACTIVE_COUNT",
                0,
            ),
            mock.patch(
                "services.chatgpt_core.sentinel_browser._auth_browser_launch_interval_seconds",
                return_value=0.0,
            ),
            mock.patch(
                "services.chatgpt_core.sentinel_browser._run_isolated_browser_transaction",
                side_effect=fake_transaction,
            ),
        ):
            with ThreadPoolExecutor(max_workers=2) as pool:
                registration = pool.submit(
                    run_with_browser_capacity,
                    "any_auto_browser_registration",
                    lambda: tracked("registration-result"),
                    logger=lambda _message: None,
                )
                sentinel = pool.submit(
                    get_sentinel_token_via_browser,
                    flow="password_verify",
                )
                registration_result = registration.result(timeout=2)
                sentinel_result = sentinel.result(timeout=2)

        self.assertEqual(peak, 1)
        self.assertEqual(registration_result, "registration-result")
        self.assertEqual(sentinel_result, "sentinel-token")

    def test_second_browser_slot_waits_until_cgroup_memory_reserve_is_available(self):
        active = 0
        peak = 0
        lock = threading.Lock()
        first_started = threading.Event()
        second_started = threading.Event()
        allow_second = threading.Event()
        release_workers = threading.Event()
        logs: list[str] = []

        def fake_transaction(_operation, _payload, **_kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                if active == 1:
                    first_started.set()
                if active == 2:
                    second_started.set()
            release_workers.wait(timeout=3)
            with lock:
                active -= 1
            return _BrowserWorkerOutcome(
                status="ok",
                value={"status_code": 200},
            )

        def memory_state():
            return (
                allow_second.is_set(),
                2_000_000_000,
                2_684_354_560,
                1_342_177_280,
            )

        with (
            mock.patch(
                "services.chatgpt_core.sentinel_browser._auth_browser_concurrency_limit",
                return_value=2,
            ),
            mock.patch(
                "services.chatgpt_core.sentinel_browser._BROWSER_ACTIVE_COUNT",
                0,
            ),
            mock.patch(
                "services.chatgpt_core.sentinel_browser._auth_browser_launch_interval_seconds",
                return_value=0.0,
            ),
            mock.patch(
                "services.chatgpt_core.sentinel_browser._browser_memory_allows_second_slot",
                side_effect=memory_state,
            ),
            mock.patch(
                "services.chatgpt_core.sentinel_browser._run_isolated_browser_transaction",
                side_effect=fake_transaction,
            ),
        ):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(
                    create_account_via_browser,
                    name="Memory One",
                    birthdate="1990-01-01",
                    log_fn=logs.append,
                )
                self.assertTrue(first_started.wait(timeout=1))
                second = pool.submit(
                    create_account_via_browser,
                    name="Memory Two",
                    birthdate="1990-01-01",
                    log_fn=logs.append,
                )
                time.sleep(0.15)
                self.assertFalse(second_started.is_set())
                allow_second.set()
                self.assertTrue(second_started.wait(timeout=2))
                release_workers.set()
                first_result = first.result(timeout=2)
                second_result = second.result(timeout=2)

        self.assertEqual(peak, 2)
        self.assertTrue(first_result and first_result.ok)
        self.assertTrue(second_result and second_result.ok)
        self.assertTrue(
            any(
                "browser_slot=waiting" in line and "reason=memory" in line
                for line in logs
            )
        )

    def test_browser_slot_waits_until_cgroup_pid_reserve_is_available(self):
        first_started = threading.Event()
        second_started = threading.Event()
        allow_second = threading.Event()
        release_workers = threading.Event()
        logs: list[str] = []

        def tracked(value):
            if not first_started.is_set():
                first_started.set()
            else:
                second_started.set()
            release_workers.wait(timeout=3)
            return value

        def pid_state():
            if not first_started.is_set() or allow_second.is_set():
                return True, 1000, 1536, 220
            return False, 1400, 1536, 220

        with (
            mock.patch(
                "services.chatgpt_core.sentinel_browser._auth_browser_concurrency_limit",
                return_value=2,
            ),
            mock.patch(
                "services.chatgpt_core.sentinel_browser._BROWSER_ACTIVE_COUNT",
                0,
            ),
            mock.patch(
                "services.chatgpt_core.sentinel_browser._browser_memory_allows_second_slot",
                return_value=(True, 0, 0, 0),
            ),
            mock.patch(
                "services.chatgpt_core.sentinel_browser._browser_pid_headroom_allows_slot",
                side_effect=pid_state,
            ),
            mock.patch(
                "services.chatgpt_core.sentinel_browser._auth_browser_launch_interval_seconds",
                return_value=0.0,
            ),
        ):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(
                    run_with_browser_capacity,
                    "pid-one",
                    lambda: tracked("first"),
                    logger=logs.append,
                )
                self.assertTrue(first_started.wait(timeout=1))
                second = pool.submit(
                    run_with_browser_capacity,
                    "pid-two",
                    lambda: tracked("second"),
                    logger=logs.append,
                )
                time.sleep(0.15)
                self.assertFalse(second_started.is_set())
                allow_second.set()
                self.assertTrue(second_started.wait(timeout=2))
                release_workers.set()
                self.assertEqual(first.result(timeout=2), "first")
                self.assertEqual(second.result(timeout=2), "second")

        self.assertTrue(
            any(
                "browser_slot=waiting" in line
                and "reason=pids" in line
                and "current=1400" in line
                and "limit=1536" in line
                and "reserve=220" in line
                for line in logs
            )
        )

    def test_browser_launch_interval_spaces_starts_without_bypassing_capacity(self):
        active = 0
        peak = 0
        lock = threading.Lock()
        launch_times: list[float] = []

        def tracked(value):
            nonlocal active, peak
            with lock:
                launch_times.append(time.monotonic())
                active += 1
                peak = max(peak, active)
            time.sleep(0.11)
            with lock:
                active -= 1
            return value

        with (
            mock.patch(
                "services.chatgpt_core.sentinel_browser._auth_browser_concurrency_limit",
                return_value=2,
            ),
            mock.patch(
                "services.chatgpt_core.sentinel_browser._BROWSER_ACTIVE_COUNT",
                0,
            ),
            mock.patch(
                "services.chatgpt_core.sentinel_browser._BROWSER_NEXT_LAUNCH_AT",
                0.0,
            ),
            mock.patch(
                "services.chatgpt_core.sentinel_browser._browser_memory_allows_second_slot",
                return_value=(True, 0, 0, 0),
            ),
            mock.patch(
                "services.chatgpt_core.sentinel_browser._browser_pid_headroom_allows_slot",
                return_value=(True, 0, 0, 0),
            ),
            mock.patch(
                "services.chatgpt_core.sentinel_browser._auth_browser_launch_interval_seconds",
                return_value=0.04,
            ),
        ):
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = [
                    pool.submit(
                        run_with_browser_capacity,
                        f"launch-{index}",
                        lambda index=index: tracked(index),
                    )
                    for index in range(3)
                ]
                self.assertEqual(
                    {future.result(timeout=2) for future in futures},
                    {0, 1, 2},
                )

        ordered = sorted(launch_times)
        self.assertEqual(len(ordered), 3)
        self.assertGreaterEqual(
            min(b - a for a, b in zip(ordered, ordered[1:])),
            0.03,
        )
        self.assertEqual(peak, 2)

    def test_launch_rechecks_pid_headroom_after_capacity_is_reserved(self):
        logs: list[str] = []
        pid_states = [
            (True, 100, 3072, 220),
            (False, 2600, 3072, 220),
            (True, 2000, 3072, 220),
        ]
        with (
            mock.patch(
                "services.chatgpt_core.sentinel_browser._BROWSER_ACTIVE_COUNT",
                0,
            ),
            mock.patch(
                "services.chatgpt_core.sentinel_browser._auth_browser_concurrency_limit",
                return_value=1,
            ),
            mock.patch(
                "services.chatgpt_core.sentinel_browser._auth_browser_capacity_mode",
                return_value="adaptive",
            ),
            mock.patch(
                "services.chatgpt_core.sentinel_browser._auth_browser_launch_interval_seconds",
                return_value=0,
            ),
            mock.patch(
                "services.chatgpt_core.sentinel_browser._browser_pid_headroom_allows_slot",
                side_effect=pid_states,
            ),
            mock.patch(
                "services.chatgpt_core.sentinel_browser._browser_memory_allows_second_slot",
                return_value=(True, 0, 0, 0),
            ),
            mock.patch(
                "services.chatgpt_core.sentinel_browser._browser_host_memory_headroom_allows_slot",
                return_value=(True, 0, 0, 0),
            ),
            mock.patch(
                "services.chatgpt_core.sentinel_browser._browser_cpu_pressure_allows_slot",
                return_value=(True, 0, 0),
            ),
            mock.patch("services.chatgpt_core.sentinel_browser.time.sleep"),
        ):
            result = run_with_browser_capacity(
                "launch-recheck",
                lambda: "started",
                logger=logs.append,
            )

        self.assertEqual(result, "started")
        self.assertTrue(
            any(
                "browser_launch=waiting" in line and "reason=pids" in line
                for line in logs
            )
        )

    def test_fifo_queue_does_not_allow_later_registration_to_overtake_auth(self):
        first_started = threading.Event()
        release_first = threading.Event()
        order: list[str] = []
        order_lock = threading.Lock()
        logs: list[str] = []

        def first_work():
            with order_lock:
                order.append("first")
            first_started.set()
            release_first.wait(timeout=3)
            return "first"

        def mark(label: str):
            with order_lock:
                order.append(label)
            return label

        with (
            mock.patch(
                "services.chatgpt_core.sentinel_browser._BROWSER_ACTIVE_COUNT",
                0,
            ),
            mock.patch(
                "services.chatgpt_core.sentinel_browser._auth_browser_concurrency_limit",
                return_value=1,
            ),
            mock.patch(
                "services.chatgpt_core.sentinel_browser._auth_browser_capacity_mode",
                return_value="fixed",
            ),
            mock.patch(
                "services.chatgpt_core.sentinel_browser._auth_browser_launch_interval_seconds",
                return_value=0,
            ),
        ):
            with ThreadPoolExecutor(max_workers=3) as pool:
                first = pool.submit(
                    run_with_browser_capacity,
                    "first-registration",
                    first_work,
                    priority="registration",
                    logger=logs.append,
                )
                self.assertTrue(first_started.wait(timeout=1))

                auth = pool.submit(
                    run_with_browser_capacity,
                    "earlier-auth",
                    lambda: mark("auth"),
                    logger=logs.append,
                )
                self._wait_for_queue_depth(1)
                registration = pool.submit(
                    run_with_browser_capacity,
                    "later-registration",
                    lambda: mark("registration"),
                    priority="registration",
                    logger=logs.append,
                )
                self._wait_for_queue_depth(2)

                release_first.set()
                self.assertEqual(first.result(timeout=2), "first")
                self.assertEqual(auth.result(timeout=2), "auth")
                self.assertEqual(registration.result(timeout=2), "registration")

        self.assertEqual(order, ["first", "auth", "registration"])
        self.assertTrue(
            any(
                "browser_slot=waiting reason=fifo_queue" in line
                and "operation=later-registration" in line
                for line in logs
            )
        )

    def test_lane_reserve_keeps_recheck_slot_available_behind_registration_fifo(self):
        started: dict[str, threading.Event] = {
            name: threading.Event()
            for name in ("registration-1", "registration-2", "registration-3", "registration-4", "recheck")
        }
        release_first = threading.Event()
        release_other = threading.Event()
        release_recheck = threading.Event()
        order: list[str] = []
        order_lock = threading.Lock()

        def work(label: str):
            started[label].set()
            with order_lock:
                order.append(label)
            if label == "registration-1":
                release_first.wait(timeout=3)
            elif label == "recheck":
                release_recheck.wait(timeout=3)
            else:
                release_other.wait(timeout=3)
            return label

        runtime_config = {
            "max_concurrency": 3,
            "registration_reserve": 2,
            "recheck_reserve": 1,
        }
        with (
            mock.patch(
                "services.chatgpt_core.sentinel_browser._BROWSER_ACTIVE_COUNT",
                0,
            ),
            mock.patch(
                "services.chatgpt_core.sentinel_browser._runtime_capacity_config",
                return_value=runtime_config,
            ),
            mock.patch(
                "services.chatgpt_core.sentinel_browser._auth_browser_capacity_mode",
                return_value="fixed",
            ),
            mock.patch(
                "services.chatgpt_core.sentinel_browser._auth_browser_launch_interval_seconds",
                return_value=0.0,
            ),
        ):
            with ThreadPoolExecutor(max_workers=5) as pool:
                active_futures = [
                    pool.submit(
                        run_with_browser_capacity,
                        f"registration-{index}",
                        lambda index=index: work(f"registration-{index}"),
                        priority="registration",
                    )
                    for index in (1, 2, 3)
                ]
                for label in ("registration-1", "registration-2", "registration-3"):
                    self.assertTrue(started[label].wait(timeout=1))

                queued_registration = pool.submit(
                    run_with_browser_capacity,
                    "registration-4",
                    lambda: work("registration-4"),
                    priority="registration",
                )
                self._wait_for_queue_depth(1)
                queued_recheck = pool.submit(
                    run_with_browser_capacity,
                    "recheck",
                    lambda: work("recheck"),
                    priority="recheck",
                )
                self._wait_for_queue_depth(2)

                # One registration slot is released, but two registrations
                # remain active and satisfy their reserve. The waiting
                # recheck must therefore consume the reserved recheck slot
                # before the older registration waiter.
                release_first.set()
                self.assertTrue(started["recheck"].wait(timeout=2))
                self.assertFalse(started["registration-4"].is_set())

                release_recheck.set()
                release_other.set()
                for index, future in zip((1, 2, 3), active_futures):
                    self.assertEqual(
                        future.result(timeout=2),
                        f"registration-{index}",
                    )
                self.assertEqual(queued_recheck.result(timeout=2), "recheck")
                self.assertEqual(queued_registration.result(timeout=2), "registration-4")

        self.assertLess(order.index("recheck"), order.index("registration-4"))

    def test_idle_lane_slot_can_be_borrowed_by_registration(self):
        first_started = threading.Event()
        second_started = threading.Event()
        release_workers = threading.Event()
        runtime_config = {
            "max_concurrency": 2,
            "registration_reserve": 1,
            "recheck_reserve": 1,
        }

        def work(which: str):
            (first_started if which == "first" else second_started).set()
            release_workers.wait(timeout=3)
            return which

        with (
            mock.patch(
                "services.chatgpt_core.sentinel_browser._BROWSER_ACTIVE_COUNT",
                0,
            ),
            mock.patch(
                "services.chatgpt_core.sentinel_browser._runtime_capacity_config",
                return_value=runtime_config,
            ),
            mock.patch(
                "services.chatgpt_core.sentinel_browser._auth_browser_capacity_mode",
                return_value="fixed",
            ),
            mock.patch(
                "services.chatgpt_core.sentinel_browser._auth_browser_launch_interval_seconds",
                return_value=0.0,
            ),
        ):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(
                    run_with_browser_capacity,
                    "registration-first",
                    lambda: work("first"),
                    priority="registration",
                )
                self.assertTrue(first_started.wait(timeout=1))
                second = pool.submit(
                    run_with_browser_capacity,
                    "registration-borrowed",
                    lambda: work("second"),
                    priority="registration",
                )
                self.assertTrue(second_started.wait(timeout=1))
                snapshot = sentinel_browser_module.browser_capacity_snapshot()
                self.assertEqual(snapshot["active_by_lane"]["registration"], 2)
                release_workers.set()
                self.assertEqual(first.result(timeout=2), "first")
                self.assertEqual(second.result(timeout=2), "second")

    def test_lane_reserves_are_balanced_when_legacy_total_is_too_small(self):
        with mock.patch(
            "services.chatgpt_core.sentinel_browser._runtime_capacity_config",
            return_value={
                "max_concurrency": 2,
                "registration_reserve": 4,
                "recheck_reserve": 2,
            },
        ):
            self.assertEqual(
                sentinel_browser_module._browser_lane_reserves(2),
                {"registration": 1, "recheck": 1},
            )

    def test_pid_pressure_hysteresis_requires_extra_headroom_to_resume(self):
        states = [(2900, 3072), (2750, 3072), (2500, 3072)]
        state_index = {"value": -1}

        def read_file(path):
            if path.endswith("pids.current"):
                state_index["value"] += 1
                return states[state_index["value"]][0]
            return states[state_index["value"]][1]

        with (
            mock.patch(
                "services.chatgpt_core.sentinel_browser._runtime_capacity_config",
                return_value={"pid_budget": 128, "pid_emergency_reserve": 256},
            ),
            mock.patch(
                "services.chatgpt_core.sentinel_browser._read_int_file",
                side_effect=read_file,
            ),
        ):
            self.assertFalse(sentinel_browser_module._browser_pid_headroom_allows_slot()[0])
            self.assertFalse(sentinel_browser_module._browser_pid_headroom_allows_slot()[0])
            self.assertTrue(sentinel_browser_module._browser_pid_headroom_allows_slot()[0])

    def test_cpu_pressure_hysteresis_waits_until_below_resume_threshold(self):
        with (
            mock.patch(
                "services.chatgpt_core.sentinel_browser._runtime_capacity_config",
                return_value={"cpu_psi_avg10_limit": 15},
            ),
            mock.patch(
                "services.chatgpt_core.sentinel_browser._cpu_psi_avg10",
                side_effect=[16.0, 14.0, 11.0],
            ),
        ):
            self.assertFalse(sentinel_browser_module._browser_cpu_pressure_allows_slot()[0])
            self.assertFalse(sentinel_browser_module._browser_cpu_pressure_allows_slot()[0])
            self.assertTrue(sentinel_browser_module._browser_cpu_pressure_allows_slot()[0])

    def test_fifo_releases_resource_waiters_one_by_one_with_launch_interval(self):
        resources_available = threading.Event()
        release_workers = threading.Event()
        starts: list[tuple[int, float]] = []
        starts_lock = threading.Lock()
        all_started = threading.Event()
        logs: list[str] = []

        def host_memory_state():
            return (
                resources_available.is_set(),
                4_000_000_000 if resources_available.is_set() else 2_000_000_000,
                1_073_741_824,
                1_342_177_280,
            )

        def tracked(index: int):
            with starts_lock:
                starts.append((index, time.monotonic()))
                if len(starts) == 3:
                    all_started.set()
            release_workers.wait(timeout=3)
            return index

        with (
            mock.patch(
                "services.chatgpt_core.sentinel_browser._BROWSER_ACTIVE_COUNT",
                0,
            ),
            mock.patch(
                "services.chatgpt_core.sentinel_browser._auth_browser_concurrency_limit",
                return_value=3,
            ),
            mock.patch(
                "services.chatgpt_core.sentinel_browser._auth_browser_capacity_mode",
                return_value="adaptive",
            ),
            mock.patch(
                "services.chatgpt_core.sentinel_browser._auth_browser_launch_interval_seconds",
                return_value=0.05,
            ),
            mock.patch(
                "services.chatgpt_core.sentinel_browser._browser_pid_headroom_allows_slot",
                return_value=(True, 100, 3072, 192),
            ),
            mock.patch(
                "services.chatgpt_core.sentinel_browser._browser_memory_allows_second_slot",
                return_value=(True, 0, 0, 0),
            ),
            mock.patch(
                "services.chatgpt_core.sentinel_browser._browser_host_memory_headroom_allows_slot",
                side_effect=host_memory_state,
            ),
            mock.patch(
                "services.chatgpt_core.sentinel_browser._browser_cpu_pressure_allows_slot",
                return_value=(True, 0, 20),
            ),
        ):
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = []
                for index in range(3):
                    futures.append(
                        pool.submit(
                            run_with_browser_capacity,
                            f"fifo-{index}",
                            lambda index=index: tracked(index),
                            logger=logs.append,
                            priority="registration",
                        )
                    )
                    self._wait_for_queue_depth(index + 1)

                resources_available.set()
                self.assertTrue(all_started.wait(timeout=2.5))
                release_workers.set()
                self.assertEqual(
                    [future.result(timeout=2) for future in futures],
                    [0, 1, 2],
                )

        ordered = sorted(starts, key=lambda item: item[1])
        self.assertEqual([index for index, _started_at in ordered], [0, 1, 2])
        gaps = [
            later[1] - earlier[1]
            for earlier, later in zip(ordered, ordered[1:])
        ]
        self.assertGreaterEqual(min(gaps), 0.04)
        acquired_logs = [
            line for line in logs if "browser_slot=acquired after_wait=true" in line
        ]
        self.assertEqual(len(acquired_logs), 3)

    def test_stopped_fifo_waiter_is_removed_without_blocking_next_ticket(self):
        first_started = threading.Event()
        release_first = threading.Event()
        stop_second = threading.Event()
        third_started = threading.Event()

        def first_work():
            first_started.set()
            release_first.wait(timeout=3)
            return "first"

        def second_stop_check():
            if stop_second.is_set():
                raise StopTaskRequested()

        with (
            mock.patch(
                "services.chatgpt_core.sentinel_browser._BROWSER_ACTIVE_COUNT",
                0,
            ),
            mock.patch(
                "services.chatgpt_core.sentinel_browser._auth_browser_concurrency_limit",
                return_value=1,
            ),
            mock.patch(
                "services.chatgpt_core.sentinel_browser._auth_browser_capacity_mode",
                return_value="fixed",
            ),
            mock.patch(
                "services.chatgpt_core.sentinel_browser._auth_browser_launch_interval_seconds",
                return_value=0,
            ),
        ):
            with ThreadPoolExecutor(max_workers=3) as pool:
                first = pool.submit(
                    run_with_browser_capacity,
                    "cancel-first",
                    first_work,
                )
                self.assertTrue(first_started.wait(timeout=1))
                second = pool.submit(
                    run_with_browser_capacity,
                    "cancel-second",
                    lambda: "unexpected",
                    stop_check=second_stop_check,
                )
                self._wait_for_queue_depth(1)
                third = pool.submit(
                    run_with_browser_capacity,
                    "cancel-third",
                    lambda: third_started.set() or "third",
                )
                self._wait_for_queue_depth(2)

                stop_second.set()
                with self.assertRaises(StopTaskRequested):
                    second.result(timeout=2)
                self._wait_for_queue_depth(1)
                release_first.set()
                self.assertEqual(first.result(timeout=2), "first")
                self.assertEqual(third.result(timeout=2), "third")
                self.assertTrue(third_started.is_set())

    def test_browser_token_requires_all_three_sentinel_signals(self):
        self.assertEqual(
            _sentinel_token_field_state('{"p":"pow","t":"telemetry","c":"challenge"}'),
            {"p": True, "t": True, "c": True},
        )
        self.assertEqual(
            _sentinel_token_field_state('{"p":"pow","t":"","c":"challenge"}'),
            {"p": True, "t": False, "c": True},
        )
        self.assertIsNone(_sentinel_token_field_state("not-json"))

    def test_sentinel_token_requires_top_level_page_and_initializes_sdk(self):
        class FakePage:
            def __init__(self):
                self.payload = None
                self.script = ""

            def wait_for_function(self, *_args, **_kwargs):
                return None

            def evaluate(self, script, payload):
                self.script = script
                self.payload = payload
                return {
                    "success": True,
                    "token": json.dumps(
                        {"p": "pow", "t": "telemetry", "c": "challenge"}
                    ),
                }

        page = FakePage()
        token = _evaluate_complete_sentinel_token(
            page,
            flow="oauth_create_account",
            sdk_wait_timeout_ms=1000,
            token_eval_timeout_ms=1000,
            require_complete_signals=True,
            logger=lambda _message: None,
        )

        self.assertTrue(token)
        self.assertIn("window.top !== window", page.script)
        self.assertIn("window.SentinelSDK.init(flow)", page.script)
        self.assertNotIn("initializeSdk", page.payload)

    def test_registration_sentinel_does_not_fall_back_to_http_pow(self):
        client = ChatGPTClient(verbose=False, browser_mode="headless")
        client.session.cookies.set(
            "login_session",
            "session-demo",
            domain="auth.openai.com",
            path="/",
        )

        with mock.patch(
            "services.chatgpt_core.chatgpt_client.get_sentinel_token_via_browser",
            return_value=None,
        ) as browser_token, mock.patch(
            "services.chatgpt_core.chatgpt_client.build_sentinel_token"
        ) as http_token:
            token = client._get_sentinel_token(
                "oauth_create_account",
                page_url="https://auth.openai.com/about-you",
            )

        self.assertIsNone(token)
        http_token.assert_not_called()
        kwargs = browser_token.call_args.kwargs
        self.assertTrue(kwargs["require_complete_signals"])
        self.assertIn("login_session=session-demo", kwargs["cookie_header"])

    def test_protocol_registration_sentinel_never_starts_browser(self):
        client = ChatGPTClient(verbose=False, browser_mode="protocol")
        with mock.patch(
            "services.chatgpt_core.chatgpt_client.build_sentinel_token",
            return_value="protocol-token",
        ) as http_token, mock.patch(
            "services.chatgpt_core.chatgpt_client.get_sentinel_token_via_browser",
            side_effect=AssertionError("protocol executor started browser"),
        ) as browser_token:
            token = client._get_sentinel_token(
                "oauth_create_account",
                page_url="https://auth.openai.com/about-you",
            )

        self.assertEqual(token, "protocol-token")
        http_token.assert_called_once()
        browser_token.assert_not_called()

    def test_protocol_create_account_posts_http_and_never_starts_browser(self):
        client = ChatGPTClient(verbose=False, browser_mode="protocol")
        response = mock.Mock(status_code=200, url="https://auth.openai.com/api/accounts/create_account")
        response.json.return_value = {
            "page": {"type": "external_url"},
            "continue_url": "https://chatgpt.com/api/auth/callback/openai?code=demo",
        }
        dump_response = mock.Mock(status_code=200)
        client.session.post = mock.Mock(return_value=response)
        client.session.get = mock.Mock(return_value=dump_response)

        with mock.patch.object(
            client,
            "_get_sentinel_token",
            return_value="protocol-token",
        ), mock.patch(
            "services.chatgpt_core.chatgpt_client.create_account_via_browser",
            side_effect=AssertionError("protocol executor started browser"),
        ) as browser_create:
            ok, state = client.create_account(
                "Alice",
                "Smith",
                "1990-01-01",
                return_state=True,
            )

        self.assertTrue(ok)
        self.assertEqual(state.page_type, "external_url")
        client.session.post.assert_called_once()
        # any-auto 对齐：create 前先 client_auth_session_dump
        self.assertTrue(client.session.get.called)
        dump_url = str(client.session.get.call_args.args[0])
        self.assertIn("client_auth_session_dump", dump_url)
        browser_create.assert_not_called()

    def test_registration_stops_before_post_when_browser_token_is_missing(self):
        client = ChatGPTClient(verbose=False, browser_mode="headed")
        client.session.post = mock.Mock()

        with mock.patch(
            "services.chatgpt_core.chatgpt_client.create_account_via_browser",
            return_value=None,
        ):
            ok, error = client.create_account(
                "Alice",
                "Smith",
                "1990-01-01",
            )

        self.assertFalse(ok)
        self.assertIn("auth_browser_finalize_unavailable", error)
        client.session.post.assert_not_called()

    def test_browser_create_account_preserves_cookie_scope_and_never_uses_curl_post(self):
        client = ChatGPTClient(verbose=False, browser_mode="headed")
        client.session.post = mock.Mock()
        client.session.cookies.set(
            "login_session",
            "session-demo",
            domain="auth.openai.com",
            path="/",
            secure=True,
        )
        result = BrowserAccountCreateResult(
            status_code=200,
            response_url="https://auth.openai.com/api/accounts/create_account",
            response_json={
                "page": {"type": "external_url"},
                "continue_url": "https://chatgpt.com/api/auth/callback/openai?code=demo",
                "method": "GET",
            },
            cookies=[
                {
                    "name": "oai-sc",
                    "value": "sentinel-cookie",
                    "domain": ".openai.com",
                    "path": "/",
                    "secure": True,
                }
            ],
            cookie_names=("cf_clearance", "oai-sc"),
            cf_clearance_present=True,
            oai_sc_present=True,
        )

        with mock.patch(
            "services.chatgpt_core.chatgpt_client.create_account_via_browser",
            return_value=result,
        ) as browser_create:
            ok, state = client.create_account(
                "Alice",
                "Smith",
                "1990-01-01",
                return_state=True,
            )

        self.assertTrue(ok)
        self.assertEqual(state.page_type, "external_url")
        client.session.post.assert_not_called()
        self.assertFalse(browser_create.call_args.kwargs["headless"])
        exported = browser_create.call_args.kwargs["cookies"]
        login_cookie = next(item for item in exported if item["name"] == "login_session")
        self.assertEqual(login_cookie["domain"], "auth.openai.com")
        stored_domains = {
            cookie.domain
            for cookie in client.session.cookies.jar
            if cookie.name == "oai-sc"
        }
        self.assertEqual(stored_domains, {".openai.com"})

    def test_browser_create_account_keeps_registration_disallowed_error_code(self):
        client = ChatGPTClient(verbose=False, browser_mode="headed")
        result = BrowserAccountCreateResult(
            status_code=400,
            response_text=(
                '{"error":{"code":"registration_disallowed",'
                '"message":"Sorry, we cannot create your account."}}'
            ),
            response_json={
                "error": {
                    "code": "registration_disallowed",
                    "message": "Sorry, we cannot create your account.",
                }
            },
        )

        with mock.patch(
            "services.chatgpt_core.chatgpt_client.create_account_via_browser",
            return_value=result,
        ):
            ok, error = client.create_account("Alice", "Smith", "1990-01-01")

        self.assertFalse(ok)
        self.assertEqual(error, "HTTP 400: registration_disallowed")

    def test_cookie_bridge_preserves_parent_domain_without_duplicates(self):
        client = ChatGPTClient(verbose=False)
        client.session.cookies.set(
            "oaicom-stable-id",
            "stable-demo",
            domain=".openai.com",
            path="/",
            secure=True,
        )

        exported = export_session_cookies_for_playwright(client.session)
        stable = next(item for item in exported if item["name"] == "oaicom-stable-id")
        self.assertEqual(stable["domain"], ".openai.com")

        merged = merge_playwright_cookies_into_session(
            client.session,
            [
                {
                    "name": "oai-sc",
                    "value": "oai-sc-demo",
                    "domain": ".openai.com",
                    "path": "/",
                    "secure": True,
                }
            ],
        )
        self.assertEqual(merged, 1)
        domains = [
            cookie.domain
            for cookie in client.session.cookies.jar
            if cookie.name == "oai-sc"
        ]
        self.assertEqual(domains, [".openai.com"])

    def test_browser_finalize_uses_one_context_for_auth_sentinel_and_create(self):
        class FakeContext:
            def __init__(self):
                self.imported = []
                self.page = None

            def add_cookies(self, cookies):
                self.imported.extend(cookies)

            def new_page(self):
                self.page = FakePage(self)
                return self.page

            def cookies(self):
                generated = [
                    {
                        "name": "cf_clearance",
                        "value": "clearance-demo",
                        "domain": "auth.openai.com",
                        "path": "/",
                        "secure": True,
                    },
                    {
                        "name": "oai-sc",
                        "value": "oai-sc-demo",
                        "domain": ".openai.com",
                        "path": "/",
                        "secure": True,
                    },
                ]
                return [*self.imported, *generated]

        class FakePage:
            def __init__(self, context):
                self.context = context
                self.url = "https://auth.openai.com/about-you"
                self.evaluate_calls = []

            def set_default_timeout(self, _timeout):
                return None

            def set_default_navigation_timeout(self, _timeout):
                return None

            def on(self, _event, _callback):
                return None

            def goto(self, url, **_kwargs):
                self.url = url
                return None

            def wait_for_timeout(self, _timeout):
                return None

            def evaluate(self, script, payload):
                self.evaluate_calls.append((script, payload))
                return {
                    "status": 200,
                    "url": "https://auth.openai.com/api/accounts/create_account",
                    "text": json.dumps(
                        {
                            "page": {"type": "external_url"},
                            "continue_url": "https://chatgpt.com/api/auth/callback/openai?code=demo",
                        }
                    ),
                }

        context = FakeContext()

        class FakeBrowser:
            def new_context(self, **_kwargs):
                return context

            def close(self):
                return None

        class FakeChromium:
            def launch(self, **_kwargs):
                return FakeBrowser()

        fake_playwright = type("FakePlaywright", (), {"chromium": FakeChromium()})()
        fake_sync_manager = mock.MagicMock()
        fake_sync_manager.__enter__.return_value = fake_playwright
        fake_sync_manager.__exit__.return_value = False
        fake_playwright_module = types.ModuleType("playwright")
        fake_sync_api_module = types.ModuleType("playwright.sync_api")
        fake_sync_api_module.sync_playwright = lambda: fake_sync_manager
        fake_playwright_module.sync_api = fake_sync_api_module
        token = json.dumps({"p": "pow", "t": "telemetry", "c": "challenge"})

        with (
            mock.patch.dict(
                sys.modules,
                {
                    "playwright": fake_playwright_module,
                    "playwright.sync_api": fake_sync_api_module,
                },
            ),
            mock.patch(
                "services.chatgpt_core.sentinel_browser.playwright_proxy_context",
                return_value=contextlib.nullcontext(None),
            ),
            mock.patch(
                "services.chatgpt_core.sentinel_browser._evaluate_complete_sentinel_token",
                return_value=token,
            ) as sentinel_token,
        ):
            result = _create_account_via_browser_sync(
                name="Alice Smith",
                birthdate="1990-01-01",
                proxy=None,
                page_url="https://auth.openai.com/about-you",
                timeout_ms=45000,
                headless=True,
                device_id="device-demo",
                user_agent=None,
                sec_ch_ua=None,
                chrome_full_version=None,
                accept_language=None,
                platform_version=None,
                viewport_width=None,
                viewport_height=None,
                cookies=[
                    {
                        "name": "login_session",
                        "value": "session-demo",
                        "domain": "auth.openai.com",
                        "path": "/",
                        "secure": True,
                    },
                    {
                        "name": "oaicom-stable-id",
                        "value": "stable-demo",
                        "domain": ".openai.com",
                        "path": "/",
                        "secure": True,
                    },
                ],
                trace_headers={"x-datadog-origin": "rum"},
                stop_check=None,
                log_fn=lambda _message: None,
            )

        self.assertTrue(result.ok)
        self.assertTrue(result.cf_clearance_present)
        self.assertTrue(result.oai_sc_present)
        self.assertEqual(result.sentinel_field_lengths, {"p": 3, "t": 9, "c": 9})
        self.assertIs(sentinel_token.call_args.args[0], context.page)
        imported_domains = {
            item.get("domain")
            for item in context.imported
            if item.get("name") == "oaicom-stable-id"
        }
        self.assertEqual(imported_domains, {".openai.com"})
        script, payload = context.page.evaluate_calls[-1]
        self.assertIn("fetch('/api/accounts/create_account'", script)
        self.assertIn("x-access-flow-invocation-id", script)
        self.assertNotIn("oai-device-id", script)
        self.assertEqual(payload["traceHeaders"], {"x-datadog-origin": "rum"})

    def test_generated_fingerprint_matches_pinned_playwright_chromium(self):
        for _ in range(10):
            fingerprint = generate_browser_fingerprint()
            self.assertEqual(fingerprint.chrome_major, 146)
            self.assertEqual(fingerprint.chrome_full_version, "146.0.0.0")
            self.assertEqual(fingerprint.impersonate, "chrome146")


if __name__ == "__main__":
    unittest.main()
