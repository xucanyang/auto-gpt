import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sqlmodel import Session, SQLModel, create_engine

from api.tasks import (
    BatchWebSessionLoginTaskRequest,
    WebSessionLoginTaskRequest,
    _create_standalone_task_record,
    _invalid_recheck_proxy_error,
    _resolve_batch_web_session_login_accounts,
    _run_batch_web_session_login,
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
    def _captured_session(account_id: str = "acct-original") -> dict:
        return {
            "access_token": "at-new",
            "session_token": "session-new",
            "cookies": "oai-did=device-new; __Secure-next-auth.session-token=session-new; csrf=present",
            "cookie_header": "oai-did=device-new; __Secure-next-auth.session-token=session-new; csrf=present",
            "account_id": account_id,
            "workspace_id": "workspace-new",
            "refresh_token": "",
            "browser_fingerprint": {
                "device_id": "device-new",
                "accept_language": "en-US,en",
                "user_agent": "Mozilla/5.0 Firefox/141.0",
                "viewport_width": 1366,
                "viewport_height": 768,
            },
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
        self.assertIsNone(
            capture_session.call_args.kwargs.get("browser_fingerprint")
        )
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

    def test_existing_account_without_fingerprint_is_not_upgraded_to_v2(self):
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
                    self._captured_session(),
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
        self.assertIsNone(
            capture_session.call_args.kwargs.get("browser_fingerprint")
        )
        with Session(self.engine) as session:
            account = session.get(AccountModel, account_id)
            extra = account.get_extra()
        self.assertNotIn("chatgpt_browser_fingerprint", extra)
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
                WebSessionLoginTaskRequest(account_id=account_ids[0], proxy_mode="direct"),
                background_tasks=single_background,
            )
            batch_response = enqueue_batch_web_session_login_task(
                BatchWebSessionLoginTaskRequest(
                    account_ids=account_ids,
                    params={"concurrency": 3, "proxy_mode": "direct"},
                ),
                background_tasks=batch_background,
            )

        self.assertTrue(single_task_id)
        self.assertNotEqual(single_task_id, batch_response["task_id"])
        self.assertEqual(_task_store.snapshot(single_task_id)["source"], "web_session_login")
        self.assertFalse(_task_store.snapshot(single_task_id)["capabilities"]["stop_after_current"])
        self.assertEqual(batch_response["eligible"], 3)
        self.assertEqual(batch_response["effective_concurrency"], 3)
        batch_snapshot = _task_store.snapshot(batch_response["task_id"])
        self.assertEqual(batch_snapshot["source"], "batch_web_session_login")
        self.assertEqual(batch_snapshot["meta"]["effective_concurrency"], 3)
        self.assertEqual(batch_background.calls[0][0][3]["concurrency"], 3)

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


if __name__ == "__main__":
    unittest.main()
