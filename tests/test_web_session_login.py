import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from sqlmodel import Session, SQLModel, create_engine, select

from api.tasks import (
    BatchWebSessionLoginTaskRequest,
    WebSessionLoginTaskRequest,
    _create_standalone_task_record,
    _handle_web_session_lease_change,
    _invalid_recheck_proxy_error,
    _resolve_batch_web_session_login_accounts,
    _run_batch_web_session_login,
    _run_ready_web_session_gcash,
    _run_web_session_login,
    _task_store,
    _web_session_login_proxy_error,
    enqueue_batch_web_session_login_task,
    enqueue_web_session_login_task,
    get_web_session_leases,
    release_all_web_session_leases,
    release_web_session_lease,
)
from core import db as core_db
from core.db import AccountModel
from services.chatgpt_core import web_session_lease, web_session_login
from services.chatgpt_core.account_fingerprint import build_browser_fingerprint_payload
from services.chatgpt_core.browser_identity import generate_browser_fingerprint


class WebSessionLoginTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "web_session_login.db"
        self.engine = create_engine(f"sqlite:///{db_path}")
        self.core_engine_patch = mock.patch.object(core_db, "engine", self.engine)
        self.tasks_engine_patch = mock.patch("api.tasks.engine", self.engine)
        self.core_engine_patch.start()
        self.tasks_engine_patch.start()
        SQLModel.metadata.create_all(self.engine)
        core_db.init_db()

    def tearDown(self):
        self.tasks_engine_patch.stop()
        self.core_engine_patch.stop()
        self._tmpdir.cleanup()

    def test_ns_binding_aborted_is_a_retryable_network_error(self):
        self.assertEqual(
            web_session_login._classify_login_error(
                "Page.goto: NS_BINDING_ABORTED; maybe frame was detached?"
            ),
            ("network_failed", True),
        )

    def _add_account(
        self,
        *,
        email: str,
        status: str = "registered",
        password: str = "pw",
        account_identity: str = "acct-original",
        mailbox_state: bool = True,
    ) -> int:
        extra = {
            "access_token": "at-old",
            "session_token": "session-old",
            "cookies": "oai-did=device-old; __Secure-next-auth.session-token=session-old",
            "cookie_header": "oai-did=device-old; __Secure-next-auth.session-token=session-old",
            "account_id": account_identity,
            "workspace_id": "workspace-original",
            "refresh_token": "rt-preserved",
            "auth_level": "refresh_token",
            "manually_used": True,
            "chatgpt_phone_binding": {"status": "bound", "phone": "+12025550123"},
            "chatgpt_local": {"subscription": {"plan": "plus", "checked_at": "2026-08-01T00:00:00Z"}},
            "chatgpt_browser_fingerprint": {
                "device_id": "device-old",
                "accept_language": "en-US,en;q=0.9",
                "impersonate": "chrome136",
                "chrome_major": 136,
                "chrome_full_version": "136.0.7103.114",
                "user_agent": "Mozilla/5.0 Chrome/136.0.7103.114 Safari/537.36",
                "sec_ch_ua": '"Chromium";v="136"',
                "platform_version": "15.0.0",
                "viewport_width": 1440,
                "viewport_height": 900,
            },
        }
        if mailbox_state:
            extra["chatgpt_mailbox_state"] = {"provider": "dummy", "email": email}
        with Session(self.engine) as session:
            row = AccountModel(
                platform="chatgpt",
                email=email,
                password=password,
                user_id=account_identity,
                token="at-old",
                status=status,
                extra_json=json.dumps(extra),
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return int(row.id or 0)

    @staticmethod
    def _captured_session(
        account_id: str = "acct-original",
        *,
        browser_family: str = "chrome",
    ) -> dict:
        if browser_family == "firefox":
            browser_fingerprint = {
                "device_id": "device-new",
                "accept_language": "en-US,en",
                "impersonate": "firefox147",
                "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:147.0) Gecko/20100101 Firefox/147.0",
                "browser_family": "firefox",
                "viewport_width": 1366,
                "viewport_height": 768,
            }
        else:
            browser_fingerprint = {
                "device_id": "device-new",
                "accept_language": "en-US,en",
                "impersonate": "chrome146",
                "chrome_major": 148,
                "chrome_full_version": "148.0.7778.96",
                "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
                "sec_ch_ua": '"Chromium";v="148", "Google Chrome";v="148"',
                "platform_version": "15.7.0",
                "browser_family": "chrome",
                "viewport_width": 1366,
                "viewport_height": 768,
            }
        return {
            "access_token": "at-new",
            "session_token": "session-new",
            "cookies": "oai-did=device-new; __Secure-next-auth.session-token=session-new; csrf=present",
            "cookie_header": "oai-did=device-new; __Secure-next-auth.session-token=session-new; csrf=present",
            "account_id": account_id,
            "workspace_id": "workspace-new",
            "refresh_token": "",
            "browser_fingerprint": browser_fingerprint,
        }

    def test_success_replaces_web_session_without_changing_business_state(self):
        account_id = self._add_account(
            email="plus@example.com",
            status="subscribed",
            password="",
        )
        exported_state = {
            "provider": "dummy",
            "email": "plus@example.com",
            "before_ids": ["new-message-id"],
        }

        with (
            mock.patch.object(web_session_login.config_store, "get_all", return_value={}),
            mock.patch.object(
                web_session_login,
                "capture_web_session_without_refresh_token",
                return_value=(self._captured_session(), exported_state),
            ) as capture_session,
            mock.patch.object(
                web_session_login,
                "schedule_chatgpt_local_status_refresh_for_account_id",
            ) as schedule_refresh,
        ):
            result = web_session_login.execute_chatgpt_web_session_login(
                account_id,
                retry_delays_seconds=[],
                task_id="task-web-session-success",
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["data"]["web_session_complete"])
        self.assertEqual(capture_session.call_args.kwargs["password"], "")
        planned = capture_session.call_args.kwargs.get("browser_fingerprint") or {}
        self.assertEqual(planned.get("browser_family"), "chrome")
        self.assertEqual(planned.get("browser_backend"), "patchright_chromium")
        schedule_refresh.assert_called_once_with(
            account_id,
            reason="web_session_login:success",
            proxy=None,
            use_default_proxy=True,
            delay_seconds=2.0,
        )
        with Session(self.engine) as session:
            account = session.get(AccountModel, account_id)
            extra = account.get_extra()

        self.assertEqual(account.status, "subscribed")
        self.assertEqual(account.token, "at-new")
        self.assertEqual(account.user_id, "acct-original")
        self.assertEqual(extra["access_token"], "at-new")
        self.assertEqual(extra["session_token"], "session-new")
        self.assertIn("csrf=present", extra["cookie_header"])
        self.assertEqual(extra["workspace_id"], "workspace-new")
        self.assertEqual(extra["refresh_token"], "rt-preserved")
        self.assertEqual(extra["auth_level"], "refresh_token")
        self.assertTrue(extra["manually_used"])
        self.assertEqual(extra["chatgpt_phone_binding"]["status"], "bound")
        self.assertNotIn("chatgpt_local", extra)
        self.assertEqual(extra["chatgpt_last_confirmed_subscription"]["plan"], "plus")
        self.assertEqual(extra["chatgpt_mailbox_state"]["before_ids"], ["new-message-id"])
        self.assertEqual(extra["chatgpt_web_session_login"]["status"], "success")
        self.assertTrue(extra["chatgpt_web_session_login"]["web_session_complete"])
        self.assertEqual(extra["chatgpt_web_session_browser_fingerprint"]["device_id"], "device-new")
        self.assertEqual(extra["chatgpt_browser_fingerprint"]["device_id"], "device-new")
        self.assertIn("Chrome/136", extra["chatgpt_browser_fingerprint"]["user_agent"])

    def test_explicit_browser_switch_replaces_profile_only_after_success(self):
        account_id = self._add_account(email="switch-browser@example.com")
        requested_profile = build_browser_fingerprint_payload(
            generate_browser_fingerprint(
                browser_family="firefox",
                deep_context=True,
                timezone="Asia/Jakarta",
            )
        )
        captured = self._captured_session(browser_family="firefox")
        captured["browser_fingerprint"] = {
            **requested_profile,
            "device_id": "device-new",
        }

        with (
            mock.patch.object(web_session_login.config_store, "get_all", return_value={}),
            mock.patch.object(
                web_session_login,
                "capture_web_session_without_refresh_token",
                return_value=(
                    captured,
                    {"provider": "dummy", "email": "switch-browser@example.com"},
                ),
            ),
            mock.patch.object(
                web_session_login,
                "schedule_chatgpt_local_status_refresh_for_account_id",
            ),
        ):
            result = web_session_login.execute_chatgpt_web_session_login(
                account_id,
                retry_delays_seconds=[],
                browser_fingerprint_override=requested_profile,
                replace_browser_fingerprint=True,
            )

        self.assertTrue(result["ok"])
        with Session(self.engine) as session:
            account = session.get(AccountModel, account_id)
            extra = account.get_extra()
        stored = extra["chatgpt_browser_fingerprint"]
        self.assertEqual(stored["browser_family"], "firefox")
        self.assertEqual(stored["browser_backend"], "camoufox_firefox")
        self.assertEqual(stored["operating_system"], "macos")
        self.assertEqual(stored["device_id"], "device-new")
        self.assertEqual(extra["refresh_token"], "rt-preserved")
        self.assertTrue(extra["manually_used"])

    def test_existing_account_without_fingerprint_uses_and_persists_firefox_profile(self):
        account_id = self._add_account(email="legacy-no-fingerprint@example.com")
        with Session(self.engine) as session:
            account = session.get(AccountModel, account_id)
            extra = account.get_extra()
            extra.pop("chatgpt_browser_fingerprint", None)
            account.set_extra(extra)
            session.add(account)
            session.commit()

        with (
            mock.patch.object(web_session_login.config_store, "get_all", return_value={}),
            mock.patch.object(
                web_session_login,
                "capture_web_session_without_refresh_token",
                return_value=(
                    self._captured_session(browser_family="firefox"),
                    {
                        "provider": "dummy",
                        "email": "legacy-no-fingerprint@example.com",
                    },
                ),
            ) as capture_session,
            mock.patch.object(
                web_session_login,
                "schedule_chatgpt_local_status_refresh_for_account_id",
            ),
        ):
            result = web_session_login.execute_chatgpt_web_session_login(
                account_id,
                retry_delays_seconds=[],
                task_id="task-web-session-no-fingerprint",
            )

        self.assertTrue(result["ok"])
        planned = capture_session.call_args.kwargs.get("browser_fingerprint") or {}
        self.assertEqual(planned.get("browser_family"), "firefox")
        self.assertEqual(planned.get("browser_backend"), "camoufox_firefox")
        with Session(self.engine) as session:
            account = session.get(AccountModel, account_id)
            extra = account.get_extra()
        self.assertEqual(
            extra["chatgpt_browser_fingerprint"]["browser_family"],
            "firefox",
        )
        self.assertEqual(
            extra["chatgpt_web_session_browser_fingerprint"]["device_id"],
            "device-new",
        )

    def test_identity_mismatch_never_overwrites_existing_credentials(self):
        account_id = self._add_account(email="identity@example.com", status="invalid")

        with (
            mock.patch.object(web_session_login.config_store, "get_all", return_value={}),
            mock.patch.object(
                web_session_login,
                "capture_web_session_without_refresh_token",
                return_value=(
                    self._captured_session(account_id="acct-other"),
                    {"provider": "dummy", "email": "identity@example.com"},
                ),
            ),
            mock.patch.object(
                web_session_login,
                "schedule_chatgpt_local_status_refresh_for_account_id",
            ) as schedule_refresh,
        ):
            result = web_session_login.execute_chatgpt_web_session_login(
                account_id,
                retry_delays_seconds=[],
                task_id="task-web-session-mismatch",
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["data"]["error_code"], "account_identity_mismatch")
        schedule_refresh.assert_not_called()
        with Session(self.engine) as session:
            account = session.get(AccountModel, account_id)
            extra = account.get_extra()
        self.assertEqual(account.status, "invalid")
        self.assertEqual(account.token, "at-old")
        self.assertEqual(account.user_id, "acct-original")
        self.assertEqual(extra["access_token"], "at-old")
        self.assertEqual(extra["session_token"], "session-old")
        self.assertEqual(extra["refresh_token"], "rt-preserved")
        self.assertEqual(extra["chatgpt_web_session_login"]["status"], "failed")
        self.assertEqual(extra["chatgpt_web_session_login"]["error_code"], "account_identity_mismatch")

    def test_local_status_schedule_failure_does_not_reverse_committed_login(self):
        account_id = self._add_account(email="refresh-schedule@example.com")
        with (
            mock.patch.object(web_session_login.config_store, "get_all", return_value={}),
            mock.patch.object(
                web_session_login,
                "capture_web_session_without_refresh_token",
                return_value=(
                    self._captured_session(),
                    {"provider": "dummy", "email": "refresh-schedule@example.com"},
                ),
            ),
            mock.patch.object(
                web_session_login,
                "schedule_chatgpt_local_status_refresh_for_account_id",
                side_effect=RuntimeError("scheduler unavailable"),
            ),
        ):
            result = web_session_login.execute_chatgpt_web_session_login(
                account_id,
                retry_delays_seconds=[],
            )

        self.assertTrue(result["ok"])
        self.assertFalse(result["data"]["local_status_refresh_scheduled"])
        self.assertIn("不影响登录态成功", "\n".join(result["data"]["logs"]))
        with Session(self.engine) as session:
            account = session.get(AccountModel, account_id)
        self.assertEqual(account.token, "at-new")

    def test_held_browser_persists_credentials_before_operator_release(self):
        account_id = self._add_account(email="held@example.com")
        manager = web_session_lease.WebSessionLeaseManager(runtime_dir=self._tmpdir.name)

        def fake_capture(**kwargs):
            tokens = self._captured_session()
            mailbox = {"provider": "dummy", "email": "held@example.com"}
            kwargs["session_ready_callback"](tokens, mailbox, "login")
            lease = kwargs["session_lease"]
            lease.transition("ready_holding")
            lease.request_release()
            lease.transition("released")
            return tokens, mailbox

        with (
            mock.patch.object(web_session_login.config_store, "get_all", return_value={}),
            mock.patch.object(
                web_session_login,
                "capture_web_session_without_refresh_token",
                side_effect=fake_capture,
            ),
            mock.patch.object(
                web_session_login,
                "schedule_chatgpt_local_status_refresh_for_account_id",
            ),
            mock.patch.object(web_session_lease, "web_session_lease_manager", manager),
        ):
            result = web_session_login.execute_chatgpt_web_session_login(
                account_id,
                retry_delays_seconds=[],
                task_id="task-held-login",
                hold_browser=True,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["browser_lease"]["status"], "released")
        with Session(self.engine) as session:
            account = session.get(AccountModel, account_id)
            extra = account.get_extra()
        self.assertEqual(account.token, "at-new")
        self.assertEqual(extra["session_token"], "session-new")
        self.assertEqual(extra["chatgpt_web_session_login"]["status"], "success")

    def test_browser_crash_after_ready_preserves_committed_credentials(self):
        account_id = self._add_account(email="held-crash@example.com")
        manager = web_session_lease.WebSessionLeaseManager(runtime_dir=self._tmpdir.name)

        def fake_capture(**kwargs):
            tokens = self._captured_session()
            mailbox = {"provider": "dummy", "email": "held-crash@example.com"}
            kwargs["session_ready_callback"](tokens, mailbox, "login")
            lease = kwargs["session_lease"]
            lease.transition("ready_holding")
            lease.transition("interrupted", error="browser closed")
            raise RuntimeError("browser closed")

        with (
            mock.patch.object(web_session_login.config_store, "get_all", return_value={}),
            mock.patch.object(
                web_session_login,
                "capture_web_session_without_refresh_token",
                side_effect=fake_capture,
            ),
            mock.patch.object(
                web_session_login,
                "schedule_chatgpt_local_status_refresh_for_account_id",
            ),
            mock.patch.object(web_session_lease, "web_session_lease_manager", manager),
        ):
            result = web_session_login.execute_chatgpt_web_session_login(
                account_id,
                retry_delays_seconds=[],
                task_id="task-held-crash",
                hold_browser=True,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["data"]["error_code"], "browser_lease_interrupted")
        self.assertTrue(result["data"]["credentials_preserved"])
        with Session(self.engine) as session:
            account = session.get(AccountModel, account_id)
            extra = account.get_extra()
        self.assertEqual(account.token, "at-new")
        self.assertEqual(extra["session_token"], "session-new")
        self.assertEqual(extra["chatgpt_web_session_login"]["status"], "success")

    def test_release_before_ready_does_not_write_failure_or_replace_credentials(self):
        account_id = self._add_account(email="release-before-ready@example.com")
        manager = web_session_lease.WebSessionLeaseManager(runtime_dir=self._tmpdir.name)

        def fake_capture(**kwargs):
            lease = kwargs["session_lease"]
            lease.request_release()
            lease.transition("released")
            raise web_session_lease.WebSessionLeaseReleaseRequested("operator release")

        with (
            mock.patch.object(web_session_login.config_store, "get_all", return_value={}),
            mock.patch.object(
                web_session_login,
                "capture_web_session_without_refresh_token",
                side_effect=fake_capture,
            ),
            mock.patch.object(web_session_lease, "web_session_lease_manager", manager),
        ):
            result = web_session_login.execute_chatgpt_web_session_login(
                account_id,
                retry_delays_seconds=[],
                task_id="task-release-before-ready",
                hold_browser=True,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["data"]["error_code"], "browser_lease_released")
        with Session(self.engine) as session:
            account = session.get(AccountModel, account_id)
            extra = account.get_extra()
        self.assertEqual(account.token, "at-old")
        self.assertEqual(extra["session_token"], "session-old")
        self.assertNotIn("chatgpt_web_session_login", extra)

    def test_batch_resolver_accepts_empty_password_and_skips_missing_mailbox(self):
        registered_id = self._add_account(email="registered@example.com", status="registered")
        invalid_id = self._add_account(email="invalid@example.com", status="invalid")
        missing_password_id = self._add_account(email="no-password@example.com", password="")
        missing_mailbox_id = self._add_account(email="no-mailbox@example.com", mailbox_state=False)
        req = BatchWebSessionLoginTaskRequest(
            account_ids=[registered_id, invalid_id, missing_password_id, missing_mailbox_id, 999999]
        )

        eligible, missing_ids, skipped, matched = _resolve_batch_web_session_login_accounts(req)

        self.assertEqual(
            [item["account_id"] for item in eligible],
            [registered_id, invalid_id, missing_password_id],
        )
        self.assertEqual(missing_ids, [999999])
        self.assertEqual(matched, [])
        self.assertEqual({item["account_id"] for item in skipped}, {missing_mailbox_id})

    def test_browser_lease_terminal_errors_never_trigger_proxy_failover(self):
        interrupted = {"data": {"error_code": "browser_lease_interrupted"}}
        released = {"data": {"error_code": "browser_lease_released"}}

        self.assertFalse(_web_session_login_proxy_error(interrupted, "proxy timeout"))
        self.assertFalse(_web_session_login_proxy_error(released, "proxy timeout"))
        self.assertTrue(_invalid_recheck_proxy_error(interrupted, "proxy timeout"))

    def test_batch_lease_api_releases_one_or_all_and_stops_new_browser_scheduling(self):
        first_id = self._add_account(email="lease-api-one@example.com")
        second_id = self._add_account(email="lease-api-two@example.com")
        task_id = "task-web-session-lease-api"
        _create_standalone_task_record(
            task_id,
            platform="chatgpt",
            source="batch_web_session_login",
            total=2,
            meta={"emails": ["lease-api-one@example.com", "lease-api-two@example.com"]},
        )
        manager = web_session_lease.WebSessionLeaseManager(runtime_dir=self._tmpdir.name)

        with mock.patch.object(web_session_lease, "web_session_lease_manager", manager):
            first = manager.create(
                task_id=task_id,
                account_id=first_id,
                email="lease-api-one@example.com",
            )
            second = manager.create(
                task_id=task_id,
                account_id=second_id,
                email="lease-api-two@example.com",
            )
            first.transition("ready_holding")
            second.transition("ready_holding")

            snapshot = get_web_session_leases(task_id)
            self.assertEqual(snapshot["web_session_lease_counts"]["active"], 2)
            released_one = release_web_session_lease(task_id, first_id)
            self.assertEqual(released_one["leases"][0]["status"], "releasing")
            released_all = release_all_web_session_leases(task_id)

        self.assertEqual(
            {item["account_id"] for item in released_all["leases"]},
            {first_id, second_id},
        )
        self.assertTrue(_task_store.control_for(task_id).is_stop_after_current_requested())
        self.assertTrue(first.release_requested)
        self.assertTrue(second.release_requested)

    def test_single_and_batch_enqueue_keep_independent_task_contracts(self):
        account_ids = [
            self._add_account(email=f"batch-{index}@example.com", status="invalid" if index % 2 else "registered")
            for index in range(1, 4)
        ]

        class _BackgroundTasks:
            def __init__(self):
                self.calls = []

            def add_task(self, *args, **kwargs):
                self.calls.append((args, kwargs))

        single_background = _BackgroundTasks()
        batch_background = _BackgroundTasks()
        with (
            mock.patch("api.tasks._save_task_log"),
            mock.patch("api.tasks.time.time", return_value=1234.5),
            mock.patch(
                "services.chatgpt_core.sentinel_browser.persistent_browser_session_limit",
                return_value=3,
            ),
        ):
            single_task_id = enqueue_web_session_login_task(
                WebSessionLoginTaskRequest(
                    account_id=account_ids[0],
                    proxy_mode="direct",
                    browser_family="chrome",
                ),
                background_tasks=single_background,
            )
            batch_response = enqueue_batch_web_session_login_task(
                BatchWebSessionLoginTaskRequest(
                    account_ids=account_ids,
                    params={
                        "concurrency": 3,
                        "proxy_mode": "direct",
                        "browser_family": "firefox",
                    },
                ),
                background_tasks=batch_background,
            )

        self.assertTrue(single_task_id)
        self.assertNotEqual(single_task_id, batch_response["task_id"])
        self.assertEqual(_task_store.snapshot(single_task_id)["source"], "web_session_login")
        self.assertFalse(_task_store.snapshot(single_task_id)["capabilities"]["stop_after_current"])
        self.assertEqual(
            _task_store.snapshot(single_task_id)["meta"]["browser_profile"]["selection"],
            "chrome",
        )
        self.assertEqual(batch_response["eligible"], 3)
        self.assertEqual(batch_response["effective_concurrency"], 3)
        batch_snapshot = _task_store.snapshot(batch_response["task_id"])
        self.assertEqual(batch_snapshot["source"], "batch_web_session_login")
        self.assertEqual(batch_snapshot["meta"]["effective_concurrency"], 3)
        self.assertEqual(batch_snapshot["meta"]["browser_profile"]["selection"], "firefox")
        self.assertEqual(batch_background.calls[0][0][3]["concurrency"], 3)
        self.assertEqual(batch_background.calls[0][0][3]["browser_family"], "firefox")

    def test_ready_holding_timeline_distinguishes_login_only_from_gcash(self):
        task_sources = {
            "task-web-session-copy-login": "batch_web_session_login",
            "task-web-session-copy-gcash": "batch_web_session_gcash_link",
        }
        timeline_calls: list[dict] = []
        for task_id, source in task_sources.items():
            _create_standalone_task_record(
                task_id,
                platform="chatgpt",
                source=source,
                total=1,
                meta={},
            )

        with (
            mock.patch(
                "api.tasks._task_timeline_log",
                side_effect=lambda *_args, **kwargs: timeline_calls.append(kwargs),
            ),
            mock.patch("api.tasks._persist_task_snapshot"),
        ):
            for task_id in task_sources:
                _handle_web_session_lease_change(
                    task_id,
                    {
                        "status": "ready_holding",
                        "account_id": 1,
                        "email": "copy@example.com",
                    },
                )

        self.assertEqual(timeline_calls[0]["task"], "执行登录态")
        self.assertNotIn("GCash", timeline_calls[0]["next_step"])
        self.assertEqual(timeline_calls[1]["task"], "登录态 + GCash")
        self.assertIn("GCash", timeline_calls[1]["next_step"])
        self.assertIn("新标签页", timeline_calls[1]["next_step"])

    def test_batch_runner_records_partial_failure_and_continues(self):
        first_id = self._add_account(email="runner-one@example.com")
        second_id = self._add_account(email="runner-two@example.com", status="invalid")
        task_id = "task-web-session-partial"
        _create_standalone_task_record(
            task_id,
            platform="chatgpt",
            source="batch_web_session_login",
            total=2,
            meta={
                "emails": ["runner-one@example.com", "runner-two@example.com"],
                "missing_ids": [],
                "skipped_items": [],
                "requested_concurrency": 2,
                "effective_concurrency": 2,
            },
        )

        def fake_execute(*, account_id, **_kwargs):
            if account_id == first_id:
                return {"ok": True, "data": {"web_session_complete": True}}, "runner-one@example.com"
            return {
                "ok": False,
                "error": "password_invalid",
                "data": {"error_code": "password_invalid", "message": "password_invalid"},
            }, "runner-two@example.com"

        with (
            mock.patch("api.tasks._execute_web_session_login_with_proxy_candidates", side_effect=fake_execute),
            mock.patch("api.tasks._save_task_log"),
        ):
            _run_batch_web_session_login(
                task_id,
                [first_id, second_id],
                {"requested_concurrency": 2, "concurrency": 2, "proxy_mode": "direct"},
            )

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["success"], 1)
        self.assertEqual(len(snapshot["errors"]), 1)
        self.assertEqual(len(snapshot["meta"]["results"]), 2)
        self.assertEqual(
            {item["status"] for item in snapshot["meta"]["results"]},
            {"success", "failed"},
        )

    def test_single_runner_reports_local_status_schedule_failure_without_reversing_success(self):
        account_id = self._add_account(email="single-runner@example.com")
        task_id = "task-web-session-single-runner"
        _create_standalone_task_record(
            task_id,
            platform="chatgpt",
            source="web_session_login",
            total=1,
            meta={"email": "single-runner@example.com"},
            supports_after_current=False,
        )

        timeline_calls = []

        def capture_timeline(*args, **kwargs):
            timeline_calls.append((args, kwargs))

        with (
            mock.patch(
                "api.tasks._execute_web_session_login_with_proxy_candidates",
                return_value=(
                    {
                        "ok": True,
                        "data": {
                            "web_session_complete": True,
                            "local_status_refresh_scheduled": False,
                        },
                    },
                    "single-runner@example.com",
                ),
            ),
            mock.patch("api.tasks._task_timeline_log", side_effect=capture_timeline),
            mock.patch("api.tasks._save_task_log") as save_log,
        ):
            _run_web_session_login(task_id, account_id, {"proxy_mode": "direct"})

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["success"], 1)
        done_timeline = next(kwargs for _args, kwargs in timeline_calls if kwargs.get("phase") == "released")
        self.assertIn("调度失败但不影响登录态成功", done_timeline["message"])
        self.assertEqual(done_timeline["next_step"], "可从账号行手动执行刷新状态")
        success_call = next(call for call in save_log.call_args_list if call.args[2] == "success")
        success_detail = success_call.kwargs["detail"]
        self.assertFalse(success_detail["local_status_refresh_scheduled"])

    def test_single_runner_treats_release_before_ready_as_stopped_not_failed(self):
        account_id = self._add_account(email="single-release@example.com")
        task_id = "task-web-session-single-release"
        _create_standalone_task_record(
            task_id,
            platform="chatgpt",
            source="web_session_login",
            total=1,
            meta={"email": "single-release@example.com"},
            supports_after_current=False,
        )

        with (
            mock.patch(
                "api.tasks._execute_web_session_login_with_proxy_candidates",
                return_value=(
                    {
                        "ok": False,
                        "error": "已人工请求保存并释放浏览器",
                        "data": {"error_code": "browser_lease_released"},
                    },
                    "single-release@example.com",
                ),
            ),
            mock.patch("api.tasks._save_task_log") as save_log,
        ):
            _run_web_session_login(task_id, account_id, {"proxy_mode": "direct"})

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["status"], "stopped")
        self.assertEqual(snapshot["success"], 0)
        self.assertEqual(snapshot["skipped"], 1)
        stopped_call = next(call for call in save_log.call_args_list if call.args[2] == "stopped")
        self.assertEqual(
            stopped_call.kwargs["detail"]["attempt_outcome"],
            "web_session_login_released_before_ready",
        )

    def test_batch_runner_counts_release_before_ready_as_skipped(self):
        account_id = self._add_account(email="batch-release@example.com")
        task_id = "task-web-session-batch-release"
        _create_standalone_task_record(
            task_id,
            platform="chatgpt",
            source="batch_web_session_login",
            total=1,
            meta={
                "emails": ["batch-release@example.com"],
                "missing_ids": [],
                "skipped_items": [],
                "requested_concurrency": 1,
                "effective_concurrency": 1,
            },
        )

        with (
            mock.patch(
                "api.tasks._execute_web_session_login_with_proxy_candidates",
                return_value=(
                    {
                        "ok": False,
                        "error": "已人工请求保存并释放浏览器",
                        "data": {"error_code": "browser_lease_released"},
                    },
                    "batch-release@example.com",
                ),
            ),
            mock.patch("api.tasks._save_task_log"),
        ):
            _run_batch_web_session_login(
                task_id,
                [account_id],
                {"requested_concurrency": 1, "concurrency": 1, "proxy_mode": "direct"},
            )

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["skipped"], 1)
        self.assertEqual(snapshot["errors"], [])
        self.assertEqual(snapshot["meta"]["results"][0]["status"], "released")

    def test_batch_runner_honors_immediate_stop_before_starting_browser(self):
        account_id = self._add_account(email="stop-before-start@example.com")
        task_id = "task-web-session-stop-before-start"
        _create_standalone_task_record(
            task_id,
            platform="chatgpt",
            source="batch_web_session_login",
            total=1,
            meta={
                "emails": ["stop-before-start@example.com"],
                "missing_ids": [],
                "skipped_items": [],
                "requested_concurrency": 1,
                "effective_concurrency": 1,
            },
        )
        _task_store.request_stop(task_id)

        with (
            mock.patch("api.tasks._execute_web_session_login_with_proxy_candidates") as execute,
            mock.patch("api.tasks._save_task_log"),
        ):
            _run_batch_web_session_login(
                task_id,
                [account_id],
                {"requested_concurrency": 1, "concurrency": 1, "proxy_mode": "direct"},
            )

        execute.assert_not_called()
        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["status"], "stopped")
        self.assertEqual(snapshot["success"], 0)

    def test_batch_runner_finishes_current_account_then_stops_scheduling(self):
        account_ids = [
            self._add_account(email=f"graceful-stop-{index}@example.com")
            for index in range(1, 4)
        ]
        task_id = "task-web-session-graceful-stop"
        _create_standalone_task_record(
            task_id,
            platform="chatgpt",
            source="batch_web_session_login",
            total=3,
            meta={
                "emails": [f"graceful-stop-{index}@example.com" for index in range(1, 4)],
                "missing_ids": [],
                "skipped_items": [],
                "requested_concurrency": 1,
                "effective_concurrency": 1,
            },
        )
        executed_ids = []

        def fake_execute(*, account_id, control, **_kwargs):
            executed_ids.append(account_id)
            control.request_stop_after_current()
            return {
                "ok": True,
                "data": {
                    "web_session_complete": True,
                    "local_status_refresh_scheduled": True,
                },
            }, "graceful-stop-1@example.com"

        with (
            mock.patch("api.tasks._execute_web_session_login_with_proxy_candidates", side_effect=fake_execute),
            mock.patch("api.tasks._save_task_log"),
        ):
            _run_batch_web_session_login(
                task_id,
                account_ids,
                {"requested_concurrency": 1, "concurrency": 1, "proxy_mode": "direct"},
            )

        self.assertEqual(executed_ids, [account_ids[0]])
        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["status"], "stopped")
        self.assertEqual(snapshot["success"], 1)
        self.assertEqual(snapshot["meta"]["results"][0]["local_status_refresh_scheduled"], True)

    def test_batch_runner_releases_auth_slots_at_ready_while_all_owners_keep_holding(self):
        account_ids = [
            self._add_account(email=f"ready-hold-{index:02d}@example.com")
            for index in range(20)
        ]
        task_id = "task-web-session-ready-hold-concurrency"
        _create_standalone_task_record(
            task_id,
            platform="chatgpt",
            source="batch_web_session_login",
            total=20,
            meta={
                "emails": [f"ready-hold-{index:02d}@example.com" for index in range(20)],
                "missing_ids": [],
                "skipped_items": [],
                "requested_concurrency": 5,
                "effective_concurrency": 5,
                "hold_capacity": 20,
            },
        )
        release_owners = threading.Event()
        all_ready = threading.Event()
        state_lock = threading.Lock()
        authenticating = 0
        max_authenticating = 0
        ready_ids: set[int] = set()

        def fake_execute(*, account_id, lease_change_callback, **_kwargs):
            nonlocal authenticating, max_authenticating
            with state_lock:
                authenticating += 1
                max_authenticating = max(max_authenticating, authenticating)
            time.sleep(0.02)
            with state_lock:
                authenticating -= 1
            lease_change_callback(
                {
                    "status": "ready_holding",
                    "lease_id": f"lease-{account_id}",
                    "account_id": account_id,
                }
            )
            with state_lock:
                ready_ids.add(account_id)
                if len(ready_ids) == len(account_ids):
                    all_ready.set()
            self.assertTrue(release_owners.wait(timeout=10))
            return {
                "ok": True,
                "data": {
                    "web_session_complete": True,
                    "local_status_refresh_scheduled": True,
                },
            }, f"ready-hold-{account_ids.index(account_id):02d}@example.com"

        runner = threading.Thread(
            target=_run_batch_web_session_login,
            args=(
                task_id,
                account_ids,
                {
                    "requested_concurrency": 5,
                    "concurrency": 5,
                    "hold_capacity": 20,
                    "proxy_mode": "direct",
                },
            ),
            daemon=True,
        )
        with (
            mock.patch(
                "api.tasks._execute_web_session_login_with_proxy_candidates",
                side_effect=fake_execute,
            ),
            mock.patch("api.tasks._save_task_log"),
        ):
            runner.start()
            self.assertTrue(all_ready.wait(timeout=10), "20 个账号没有在保持浏览器时完成补位")
            holding_snapshot = _task_store.snapshot(task_id)
            self.assertEqual(holding_snapshot["status"], "running")
            self.assertEqual(holding_snapshot["meta"]["runtime_login_success"], 20)
            self.assertEqual(holding_snapshot["control"]["active_attempts"], 0)
            self.assertEqual(max_authenticating, 5)
            self.assertTrue(runner.is_alive(), "owner 浏览器保持期间父任务不应提前收口")
            release_owners.set()
            runner.join(timeout=10)

        self.assertFalse(runner.is_alive())
        final_snapshot = _task_store.snapshot(task_id)
        self.assertEqual(final_snapshot["status"], "done")
        self.assertEqual(final_snapshot["success"], 20)
        self.assertEqual(len(final_snapshot["meta"]["results"]), 20)

    def test_combined_runner_keeps_owners_when_one_gcash_generation_fails(self):
        account_ids = [
            self._add_account(email=f"gcash-hold-{index}@example.com")
            for index in range(2)
        ]
        task_id = "task-web-session-gcash-hold"
        _create_standalone_task_record(
            task_id,
            platform="chatgpt",
            source="batch_web_session_gcash_link",
            total=2,
            meta={
                "emails": [f"gcash-hold-{index}@example.com" for index in range(2)],
                "missing_ids": [],
                "skipped_items": [],
                "requested_concurrency": 2,
                "effective_concurrency": 2,
                "hold_capacity": 2,
                "gcash_enabled": True,
            },
        )
        release_owners = threading.Event()
        gcash_finished = threading.Event()
        gcash_lock = threading.Lock()
        gcash_calls: list[int] = []
        fake_profile_client = mock.Mock()
        fake_profile_client.get_profile.return_value = {
            "profile_hash": "profile-gcash",
            "link_type": "gcash",
            "country": "PH",
            "currency": "PHP",
        }

        def fake_execute(*, account_id, lease_change_callback, **_kwargs):
            lease_change_callback(
                {
                    "status": "ready_holding",
                    "lease_id": f"gcash-lease-{account_id}",
                    "account_id": account_id,
                }
            )
            self.assertTrue(release_owners.wait(timeout=10))
            return {"ok": True, "data": {"web_session_complete": True}}, f"gcash-{account_id}@example.com"

        def fake_gcash(*, account_id, **_kwargs):
            with gcash_lock:
                gcash_calls.append(account_id)
                if len(gcash_calls) == 2:
                    gcash_finished.set()
            if account_id == account_ids[0]:
                return {
                    "status": "success",
                    "gcash_state": "succeeded",
                    "url": "https://checkoutshopper-live.adyen.com/checkoutshopper/checkoutPaymentRedirect?redirectData=success123",
                    "browser_tab_state": "ready",
                }
            return {
                "status": "failed",
                "gcash_state": "failed",
                "error": "GCash upstream response: payment method unavailable",
            }

        runner = threading.Thread(
            target=_run_batch_web_session_login,
            args=(
                task_id,
                account_ids,
                {
                    "requested_concurrency": 2,
                    "concurrency": 2,
                    "hold_capacity": 2,
                    "gcash_enabled": True,
                    "proxy_mode": "direct",
                },
            ),
            daemon=True,
        )
        with (
            mock.patch(
                "api.tasks._execute_web_session_login_with_proxy_candidates",
                side_effect=fake_execute,
            ),
            mock.patch("api.tasks._run_ready_web_session_gcash", side_effect=fake_gcash),
            mock.patch(
                "services.chatgpt_core.long_link_payment_client.LongLinkPaymentClient.from_env",
                return_value=fake_profile_client,
            ),
            mock.patch("api.tasks._save_task_log"),
        ):
            runner.start()
            self.assertTrue(gcash_finished.wait(timeout=10))
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                holding_snapshot = _task_store.snapshot(task_id)
                meta = holding_snapshot["meta"]
                if meta.get("gcash_success") == 1 and meta.get("gcash_failed") == 1:
                    break
                time.sleep(0.02)
            self.assertEqual(holding_snapshot["status"], "running")
            self.assertEqual(meta["runtime_login_success"], 2)
            self.assertEqual(meta["gcash_success"], 1)
            self.assertEqual(meta["gcash_failed"], 1)
            self.assertEqual(holding_snapshot["control"]["active_attempts"], 0)
            self.assertTrue(runner.is_alive())
            release_owners.set()
            runner.join(timeout=10)

        self.assertFalse(runner.is_alive())
        final_snapshot = _task_store.snapshot(task_id)
        self.assertEqual(final_snapshot["status"], "done")
        self.assertEqual(final_snapshot["success"], 1)
        self.assertEqual(len(final_snapshot["errors"]), 1)
        self.assertIn("payment method unavailable", final_snapshot["errors"][0])

    def test_ready_gcash_uses_latest_account_at_persists_link_and_opens_target_lease(self):
        account_id = self._add_account(email="gcash-ready@example.com")
        task_id = "task-web-session-gcash-ready"
        _create_standalone_task_record(
            task_id,
            platform="chatgpt",
            source="web_session_gcash_link",
            total=1,
            meta={"email": "gcash-ready@example.com", "gcash_enabled": True},
            supports_after_current=False,
        )
        gcash_url = (
            "https://checkoutshopper-live.adyen.com/checkoutshopper/"
            "checkoutPaymentRedirect?redirectData=ready123"
        )

        class _FakeLongLinkClient:
            def __init__(self):
                self.submissions = []

            def get_profile(self, *, force_refresh=False):
                self.force_refresh = force_refresh
                return {
                    "profile_hash": "profile-gcash",
                    "link_type": "gcash",
                    "country": "PH",
                    "currency": "PHP",
                    "plan": "plus",
                }

            def submit_batch(self, *, items, expected_profile_hash):
                self.submissions.append((items, expected_profile_hash))
                request_id = items[0]["request_id"]
                return {
                    "batch_id": "batch-gcash",
                    "items": [
                        {
                            "status": "done",
                            "batch_id": "batch-gcash",
                            "job_id": "job-gcash",
                            "request_id": request_id,
                            "profile_hash": "profile-gcash",
                            "completed_at": 1_787_500_000,
                            "result": {
                                "url": gcash_url,
                                "provider_redirect_url": gcash_url,
                                "link_type": "gcash",
                                "payment_method_type": "gcash",
                                "billing_country": "PH",
                                "currency": "PHP",
                                "link_expires_at": 2_000_000_300,
                                "link_expiry_source": "gcash_provider_redirect",
                                "gcash_qr_payload": "ready_qr_payload",
                                "gcash_qr_expires_at": 2_000_000_200,
                            },
                        }
                    ],
                }

        class _FakeLeaseManager:
            def __init__(self):
                self.status_updates = []
                self.open_calls = []

            def update_gcash_status(self, task_id_value, **kwargs):
                self.status_updates.append((task_id_value, dict(kwargs)))
                return dict(kwargs)

            def request_open_gcash(self, task_id_value, **kwargs):
                self.open_calls.append((task_id_value, dict(kwargs)))
                return {"ok": True, "lease": {"gcash_tab_state": "ready"}}

        fake_client = _FakeLongLinkClient()
        fake_manager = _FakeLeaseManager()
        with (
            mock.patch(
                "services.chatgpt_core.long_link_payment_client.LongLinkPaymentClient.from_env",
                return_value=fake_client,
            ),
            mock.patch.object(web_session_lease, "web_session_lease_manager", fake_manager),
        ):
            result = _run_ready_web_session_gcash(
                task_id=task_id,
                account_id=account_id,
                email="gcash-ready@example.com",
                lease_id="lease-gcash-ready",
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["browser_tab_state"], "ready")
        self.assertEqual(fake_client.submissions[0][0][0]["access_token"], "at-old")
        self.assertEqual(fake_client.submissions[0][1], "profile-gcash")
        self.assertEqual(fake_manager.open_calls[0][0], task_id)
        self.assertEqual(fake_manager.open_calls[0][1]["account_id"], account_id)
        self.assertEqual(fake_manager.open_calls[0][1]["lease_id"], "lease-gcash-ready")
        self.assertEqual(fake_manager.open_calls[0][1]["url"], gcash_url)
        with Session(self.engine) as session:
            account = session.get(AccountModel, account_id)
            history = session.exec(
                select(core_db.PaymentLinkGenerationModel).where(
                    core_db.PaymentLinkGenerationModel.account_id == account_id
                )
            ).one()
        extra = account.get_extra()
        variants = extra["chatgpt_payment_link_variants"]
        gcash_variant = next(item for item in variants.values() if item.get("link_type") == "gcash")
        self.assertEqual(account.cashier_url, gcash_url)
        self.assertEqual(gcash_variant["gcash_qr_expires_at"], 2_000_000_200)
        self.assertEqual(gcash_variant["link_expires_at"], 2_000_000_300)
        self.assertEqual(gcash_variant["browser_tab_state"], "ready")
        self.assertEqual(history.status, "succeeded")
        self.assertEqual(history.get_result()["gcash_qr_payload"], "ready_qr_payload")


if __name__ == "__main__":
    unittest.main()
