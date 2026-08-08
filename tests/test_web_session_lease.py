import json
from pathlib import Path
import stat
import tempfile
import threading
import time
import unittest

from services.chatgpt_core.web_session_lease import (
    WebSessionLeaseConflict,
    WebSessionLeaseManager,
    WebSessionLeaseReleaseRequested,
)


class _FakeBrowserContext:
    def __init__(self):
        self.added_cookies = []
        self.storage_state_calls = []

    def add_cookies(self, cookies):
        self.added_cookies.extend(cookies)

    def storage_state(self, *, indexed_db=False):
        self.storage_state_calls.append(indexed_db)
        return {
            "cookies": [
                {
                    "name": "__Secure-next-auth.session-token",
                    "value": "session-current",
                    "domain": ".chatgpt.com",
                    "path": "/",
                }
            ],
            "origins": [
                {
                    "origin": "https://chatgpt.com",
                    "localStorage": [{"name": "workspace", "value": "acct-demo"}],
                }
            ],
        }


class _FakePage:
    def __init__(self):
        self.closed = False

    def is_closed(self):
        return self.closed


class WebSessionLeaseTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.manager = WebSessionLeaseManager(runtime_dir=self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    def _create(self, *, account_id=11, **kwargs):
        return self.manager.create(
            task_id=kwargs.pop("task_id", "task-web-session"),
            account_id=account_id,
            email=kwargs.pop("email", f"account-{account_id}@example.com"),
            **kwargs,
        )

    def test_one_active_lease_per_account_and_terminal_release_allows_reuse(self):
        lease = self._create(account_id=21)

        with self.assertRaises(WebSessionLeaseConflict) as raised:
            self._create(account_id=21, task_id="task-conflict")

        self.assertEqual(raised.exception.snapshot["task_id"], "task-web-session")
        lease.request_release()
        with self.assertRaises(WebSessionLeaseReleaseRequested):
            lease.check_release_requested()
        lease.transition("released")

        replacement = self._create(account_id=21, task_id="task-replacement")
        self.assertNotEqual(replacement.lease_id, lease.lease_id)
        self.assertEqual(
            self.manager.active_for_account(21)["task_id"],
            "task-replacement",
        )

    def test_seeded_credentials_and_profile_are_saved_with_restricted_permissions(self):
        lease = self._create(
            account_id=22,
            cookie_header="cf_clearance=clearance; oai-did=device-cookie; __Host-next-auth.csrf-token=csrf",
            session_token="session-seeded",
            device_id="device-fallback",
        )
        context = _FakeBrowserContext()

        lease.seed_browser_context(context)
        self.assertEqual(
            {item["name"] for item in context.added_cookies},
            {
                "cf_clearance",
                "oai-did",
                "__Host-next-auth.csrf-token",
                "__Secure-next-auth.session-token",
            },
        )
        host_cookie = next(
            item
            for item in context.added_cookies
            if item["name"] == "__Host-next-auth.csrf-token"
        )
        self.assertEqual(host_cookie["url"], "https://chatgpt.com/")
        self.assertNotIn("domain", host_cookie)
        self.assertTrue(lease.checkpoint_profile(context))

        storage_path = Path(lease.snapshot()["profile_path"])
        self.assertTrue(storage_path.is_file())
        self.assertEqual(stat.S_IMODE(storage_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(storage_path.parent.stat().st_mode), 0o700)
        self.assertEqual(context.storage_state_calls, [True])
        self.assertEqual(
            json.loads(storage_path.read_text("utf-8"))["origins"][0]["origin"],
            "https://chatgpt.com",
        )
        self.assertEqual(
            lease.browser_context_options(),
            {"storage_state": str(storage_path)},
        )

    def test_owner_thread_refreshes_then_holds_until_explicit_release(self):
        lease = self._create(account_id=23)
        context = _FakeBrowserContext()
        page = _FakePage()
        published = []
        logs = []

        def publish(payload, reason):
            published.append((reason, dict(payload)))
            return {"reason": reason}

        owner = threading.Thread(
            target=lease.hold_browser,
            kwargs={
                "page": page,
                "context": context,
                "initial_payload": {"access_token": "at-initial"},
                "on_session_material": publish,
                "refresh_payload": lambda: {"access_token": "at-refreshed"},
                "log": logs.append,
            },
        )
        owner.start()
        deadline = time.monotonic() + 2
        while lease.status != "ready_holding" and time.monotonic() < deadline:
            time.sleep(0.01)

        self.assertEqual(lease.status, "ready_holding")
        refreshed = self.manager.request_refresh(
            "task-web-session",
            account_id=23,
            timeout_seconds=1,
        )
        self.assertTrue(refreshed["ok"])
        self.assertEqual(lease.snapshot()["refresh_count"], 1)
        self.assertTrue(owner.is_alive())

        released = self.manager.request_release("task-web-session", account_id=23)
        self.assertEqual(released[0]["status"], "releasing")
        owner.join(timeout=2)
        self.assertFalse(owner.is_alive())
        self.assertEqual(lease.status, "releasing")
        lease.transition("released")
        self.assertEqual([reason for reason, _payload in published], ["login", "refresh"])
        self.assertEqual(lease.snapshot()["held_seconds"], 0)
        self.assertTrue(any("等待人工停止并释放" in line for line in logs))

    def test_refresh_timeout_cancels_queued_command(self):
        lease = self._create(account_id=24)
        lease.transition("ready_holding")

        with self.assertRaises(TimeoutError):
            lease.request_refresh(timeout_seconds=0.1)

        command = lease._commands.get_nowait()
        self.assertTrue(command.cancelled.is_set())

    def test_release_before_browser_ready_is_idempotent_and_never_reactivates(self):
        lease = self._create(account_id=25)
        first = lease.request_release()
        second = lease.request_release()

        self.assertEqual(first["status"], "releasing")
        self.assertEqual(second["status"], "releasing")
        lease.transition("authenticating")
        self.assertEqual(lease.status, "releasing")
        lease.transition("released")
        lease.transition("ready_holding")
        self.assertEqual(lease.status, "released")
        self.assertIsNone(self.manager.active_for_account(25))


if __name__ == "__main__":
    unittest.main()
