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
    WebSessionLeaseNotFound,
    WebSessionLeaseReleaseRequested,
    validate_adyen_gcash_redirect_url,
)


GCASH_URL = (
    "https://checkoutshopper-live.adyen.com/checkoutshopper/"
    "checkoutPaymentRedirect?redirectData=gcash-secret"
)


class _FakeBrowserContext:
    def __init__(self):
        self.added_cookies = []
        self.storage_state_calls = []
        self.pages = []
        self.new_page_thread_ids = []
        self.block_navigation = False
        self.navigation_errors = []
        self.goto_started = threading.Event()
        self.goto_continue = threading.Event()

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
                },
                {
                    "name": "adyen-session",
                    "value": "must-not-persist",
                    "domain": ".adyen.com",
                    "path": "/",
                },
            ],
            "origins": [
                {
                    "origin": "https://chatgpt.com",
                    "localStorage": [{"name": "workspace", "value": "acct-demo"}],
                },
                {
                    "origin": "https://checkoutshopper-live.adyen.com",
                    "localStorage": [{"name": "redirectData", "value": "secret"}],
                },
            ],
        }

    def new_page(self):
        self.new_page_thread_ids.append(threading.get_ident())
        page = _FakePage(context=self, url="about:blank")
        self.pages.append(page)
        return page


class _FakePage:
    def __init__(self, *, context=None, url="https://chatgpt.com/"):
        self.closed = False
        self.context = context
        self.url = url
        self.goto_calls = []
        self.bring_to_front_calls = 0
        self.close_calls = 0

    def is_closed(self):
        return self.closed

    def goto(self, url, *, wait_until, timeout):
        self.goto_calls.append(
            {
                "url": url,
                "wait_until": wait_until,
                "timeout": timeout,
                "thread_id": threading.get_ident(),
            }
        )
        if self.context is not None:
            self.context.goto_started.set()
            if self.context.block_navigation:
                self.context.goto_continue.wait(timeout=2)
            if self.context.navigation_errors:
                raise self.context.navigation_errors.pop(0)
        self.url = url
        return None

    def bring_to_front(self):
        self.bring_to_front_calls += 1

    def close(self):
        self.close_calls += 1
        self.closed = True


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

    def _start_owner(self, lease, context=None, page=None):
        context = context or _FakeBrowserContext()
        page = page or _FakePage(context=context)
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
        return owner, context, page, published, logs

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
        saved_state = json.loads(storage_path.read_text("utf-8"))
        self.assertEqual(
            {item["domain"] for item in saved_state["cookies"]},
            {".chatgpt.com"},
        )
        self.assertNotIn("adyen", json.dumps(saved_state).lower())
        self.assertEqual(
            lease.browser_context_options(),
            {"storage_state": str(storage_path)},
        )

    def test_owner_thread_refreshes_then_holds_until_explicit_release(self):
        lease = self._create(account_id=23)
        owner, _context, _page, published, logs = self._start_owner(lease)
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

    def test_strict_adyen_gcash_redirect_url_contract(self):
        self.assertEqual(validate_adyen_gcash_redirect_url(GCASH_URL), GCASH_URL)
        invalid_urls = [
            "http://checkoutshopper-live.adyen.com/checkoutshopper/checkoutPaymentRedirect?redirectData=x",
            "https://checkoutshopper-live.adyen.com.evil.test/checkoutshopper/checkoutPaymentRedirect?redirectData=x",
            "https://checkoutshopper-live.adyen.com:443/checkoutshopper/checkoutPaymentRedirect?redirectData=x",
            "https://user@checkoutshopper-live.adyen.com/checkoutshopper/checkoutPaymentRedirect?redirectData=x",
            "https://checkoutshopper-live.adyen.com/checkoutshopper/checkoutPaymentRedirect",
            "https://checkoutshopper-live.adyen.com/checkoutshopper/checkoutPaymentRedirect?redirectData=",
            "https://checkoutshopper-live.adyen.com/checkoutshopper/other?redirectData=x",
            f"{GCASH_URL}#fragment",
        ]
        for url in invalid_urls:
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_adyen_gcash_redirect_url(url)

    def test_owner_opens_one_new_gcash_tab_per_request_without_navigating_chatgpt_page(self):
        lease = self._create(account_id=26)
        context = _FakeBrowserContext()
        chatgpt_page = _FakePage(context=context)
        owner, _context, _page, _published, logs = self._start_owner(
            lease,
            context=context,
            page=chatgpt_page,
        )

        first = self.manager.request_open_gcash(
            "task-web-session",
            account_id=26,
            lease_id=lease.lease_id,
            url=GCASH_URL,
            remote_request_id="request-one",
            remote_job_id="job-one",
            timeout_seconds=1,
        )
        duplicate = self.manager.request_open_gcash(
            "task-web-session",
            account_id=26,
            lease_id=lease.lease_id,
            url=GCASH_URL,
            remote_request_id="request-one",
            remote_job_id="job-one",
            timeout_seconds=1,
        )
        second_url = GCASH_URL.replace("gcash-secret", "gcash-new")
        second = self.manager.request_open_gcash(
            "task-web-session",
            account_id=26,
            lease_id=lease.lease_id,
            url=second_url,
            remote_request_id="request-two",
            remote_job_id="job-two",
            timeout_seconds=1,
        )

        self.assertTrue(first["ok"])
        self.assertEqual(first, duplicate)
        self.assertFalse(second["reused_tab"])
        self.assertEqual(chatgpt_page.url, "https://chatgpt.com/")
        self.assertEqual(chatgpt_page.goto_calls, [])
        self.assertEqual(len(context.pages), 2)
        self.assertEqual([len(page.goto_calls) for page in context.pages], [1, 1])
        self.assertEqual([page.bring_to_front_calls for page in context.pages], [1, 1])
        self.assertEqual(context.new_page_thread_ids, [owner.ident, owner.ident])
        self.assertEqual(
            {item["thread_id"] for page in context.pages for item in page.goto_calls},
            {owner.ident},
        )
        self.assertEqual(lease.snapshot()["gcash_tab_state"], "ready")
        self.assertTrue(lease.snapshot()["gcash_link_digest"])
        self.assertTrue(any("GCash 标签页" in line for line in logs))
        context.pages[1].close()
        deadline = time.monotonic() + 3
        while lease.snapshot()["gcash_tab_state"] != "closed" and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(lease.snapshot()["gcash_tab_state"], "closed")
        self.assertFalse(context.pages[0].closed)
        snapshot_text = json.dumps(lease.snapshot())
        self.assertNotIn("gcash-secret", snapshot_text)
        self.assertNotIn("gcash-new", snapshot_text)

        self.manager.request_release("task-web-session", account_id=26)
        owner.join(timeout=2)
        self.assertFalse(owner.is_alive())
        self.assertEqual(lease.snapshot()["gcash_tab_state"], "closed")
        self.assertTrue(all(page.closed for page in context.pages))
        self.assertEqual([page.close_calls for page in context.pages], [1, 1])

    def test_active_account_routes_gcash_from_a_different_task_to_owner_context(self):
        lease = self._create(account_id=34, task_id="task-login-owner")
        owner, context, _page, _published, _logs = self._start_owner(lease)

        result = self.manager.request_open_gcash_for_active_account(
            account_id=34,
            url=GCASH_URL,
            remote_request_id="request-payment-task",
            remote_job_id="job-payment-task",
            link_expires_at=1_900_000_000,
            gcash_qr_expires_at=1_899_999_900,
            timeout_seconds=1,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["lease"]["task_id"], "task-login-owner")
        self.assertEqual(result["lease"]["gcash_state"], "succeeded")
        self.assertEqual(result["lease"]["gcash_tab_state"], "ready")
        self.assertEqual(len(context.pages), 1)
        self.assertEqual(context.pages[0].bring_to_front_calls, 1)

        self.manager.request_release("task-login-owner", account_id=34)
        owner.join(timeout=2)
        self.assertFalse(owner.is_alive())

    def test_gcash_open_requires_exact_task_account_and_lease(self):
        lease = self._create(account_id=27)
        lease.transition("ready_holding")

        for task_id, account_id, lease_id in (
            ("wrong-task", 27, lease.lease_id),
            ("task-web-session", 999, lease.lease_id),
            ("task-web-session", 27, "wsl_wrong"),
        ):
            with self.assertRaises(WebSessionLeaseNotFound):
                self.manager.request_open_gcash(
                    task_id,
                    account_id=account_id,
                    lease_id=lease_id,
                    url=GCASH_URL,
                    remote_request_id="wrong-target",
                    timeout_seconds=0.05,
                )

    def test_invalid_gcash_url_records_tab_failure_without_exposing_url(self):
        lease = self._create(account_id=33)
        lease.transition("ready_holding")
        invalid_url = "https://evil.test/pay?redirectData=must-not-leak"

        with self.assertRaises(ValueError):
            self.manager.request_open_gcash(
                "task-web-session",
                account_id=33,
                lease_id=lease.lease_id,
                url=invalid_url,
                remote_request_id="request-invalid-url",
                remote_job_id="job-invalid-url",
                timeout_seconds=0.05,
            )

        snapshot = lease.snapshot()
        self.assertEqual(snapshot["gcash_tab_state"], "failed")
        self.assertTrue(snapshot["gcash_link_digest"])
        self.assertNotIn("evil.test", json.dumps(snapshot))
        self.assertNotIn("must-not-leak", json.dumps(snapshot))

    def test_gcash_open_timeout_cancels_queued_command_without_url_leak(self):
        lease = self._create(account_id=28)
        lease.transition("ready_holding")

        with self.assertRaises(TimeoutError):
            self.manager.request_open_gcash(
                "task-web-session",
                account_id=28,
                lease_id=lease.lease_id,
                url=GCASH_URL,
                remote_request_id="request-timeout",
                timeout_seconds=0.05,
            )

        command = lease._commands.get_nowait()
        self.assertTrue(command.cancelled.is_set())
        self.assertNotIn("url", command.payload)
        self.assertEqual(lease.snapshot()["gcash_tab_state"], "timed_out")
        self.assertNotIn("gcash-secret", json.dumps(lease.snapshot()))

    def test_release_cancels_gcash_navigation_and_owner_exits(self):
        lease = self._create(account_id=29)
        context = _FakeBrowserContext()
        context.block_navigation = True
        owner, _context, _page, _published, _logs = self._start_owner(
            lease,
            context=context,
            page=_FakePage(context=context),
        )
        open_errors = []

        def open_link():
            try:
                self.manager.request_open_gcash(
                    "task-web-session",
                    account_id=29,
                    lease_id=lease.lease_id,
                    url=GCASH_URL,
                    remote_request_id="request-release",
                    timeout_seconds=1,
                )
            except Exception as exc:
                open_errors.append(exc)

        opener = threading.Thread(target=open_link)
        opener.start()
        self.assertTrue(context.goto_started.wait(timeout=1))
        self.manager.request_release("task-web-session", account_id=29)
        context.goto_continue.set()
        opener.join(timeout=2)
        owner.join(timeout=2)

        self.assertFalse(opener.is_alive())
        self.assertFalse(owner.is_alive())
        self.assertEqual(len(open_errors), 1)
        self.assertIsInstance(open_errors[0], RuntimeError)
        self.assertEqual(lease.snapshot()["gcash_tab_state"], "closed")

    def test_gcash_navigation_failure_does_not_reuse_failed_request_tab(self):
        lease = self._create(account_id=32)
        context = _FakeBrowserContext()
        context.navigation_errors.append(RuntimeError(f"navigation failed: {GCASH_URL}"))
        owner, _context, _page, _published, _logs = self._start_owner(
            lease,
            context=context,
            page=_FakePage(context=context),
        )

        with self.assertRaises(RuntimeError):
            self.manager.request_open_gcash(
                "task-web-session",
                account_id=32,
                lease_id=lease.lease_id,
                url=GCASH_URL,
                remote_request_id="request-failed-navigation",
                timeout_seconds=1,
            )
        failed_snapshot = lease.snapshot()
        self.assertEqual(failed_snapshot["gcash_tab_state"], "failed")
        self.assertNotIn("gcash-secret", json.dumps(failed_snapshot))
        self.assertTrue(context.pages[0].closed)

        retry = self.manager.request_open_gcash(
            "task-web-session",
            account_id=32,
            lease_id=lease.lease_id,
            url=GCASH_URL.replace("gcash-secret", "gcash-retry"),
            remote_request_id="request-navigation-retry",
            timeout_seconds=1,
        )
        self.assertFalse(retry["reused_tab"])
        self.assertEqual(len(context.pages), 2)
        self.assertEqual([len(page.goto_calls) for page in context.pages], [1, 1])

        self.manager.request_release("task-web-session", account_id=32)
        owner.join(timeout=2)
        self.assertFalse(owner.is_alive())

    def test_gcash_status_updates_are_sanitized_and_capacity_is_counted(self):
        first = self._create(account_id=30)
        second = self._create(account_id=31)
        self.assertEqual(self.manager.active_count(), 2)
        self.assertEqual(self.manager.available_capacity(5), 3)

        snapshot = self.manager.update_gcash_status(
            "task-web-session",
            account_id=30,
            lease_id=first.lease_id,
            state="failed",
            error=f"upstream failed at {GCASH_URL}",
            remote_request_id="request-status",
            remote_job_id="job-status",
            link_expires_at=1_900_000_000,
            gcash_qr_expires_at=1_899_999_900,
        )
        self.assertEqual(snapshot["gcash_state"], "failed")
        self.assertEqual(snapshot["gcash_remote_request_id"], "request-status")
        self.assertEqual(snapshot["gcash_link_expires_at"], 1_900_000_000)
        self.assertEqual(snapshot["gcash_qr_expires_at"], 1_899_999_900)
        self.assertIn("[payment URL]", snapshot["gcash_error"])
        self.assertNotIn("gcash-secret", json.dumps(snapshot))

        first.transition("failed")
        self.assertEqual(self.manager.active_count(), 1)
        self.assertEqual(self.manager.available_capacity(1), 0)
        second.transition("released")
        self.assertEqual(self.manager.active_count(), 0)

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
