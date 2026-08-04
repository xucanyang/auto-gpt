import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from sqlmodel import Session, SQLModel, create_engine

from api.tasks import (
    BatchInvalidRecheckTaskRequest,
    InvalidRecheckTaskRequest,
    _create_standalone_task_record,
    _recheck_proxy_settings,
    _resolve_batch_invalid_recheck_accounts,
    _run_batch_invalid_recheck,
    _task_store,
    enqueue_batch_invalid_recheck_task,
)
from core import db as core_db
from core.db import AccountListStateModel, AccountModel
from services.chatgpt_core import invalid_account_recheck


class InvalidAccountRecheckTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "invalid_recheck.db"
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

    def _add_account(self, *, email: str = "invalid@example.com", status: str = "invalid", extra: str | None = None) -> int:
        with Session(self.engine) as session:
            row = AccountModel(
                platform="chatgpt",
                email=email,
                password="pw",
                token="old-token",
                status=status,
                extra_json=extra
                or (
                    '{"chatgpt_mailbox_state": {"provider": "dummy", "email": "invalid@example.com"}, '
                    '"refresh_token": "old-rt", "chatgpt_has_refresh_token_solution": true, '
                    '"session_token": "old-session", "cookies": "old-cookie=1", '
                    '"cookie_header": "old-cookie=1", "workspace_id": "old-workspace", '
                    '"chatgpt_local": {"auth": {"state": "account_deactivated"}}}'
                ),
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return int(row.id or 0)

    @staticmethod
    def _complete_web_session():
        return {
            "access_token": "at-new",
            "session_token": "session-new",
            "cookies": "oai-did=device-new; __Secure-next-auth.session-token=session-new",
            "cookie_header": "oai-did=device-new; __Secure-next-auth.session-token=session-new",
            "account_id": "acct-new",
            "workspace_id": "ws-new",
            "refresh_token": "",
        }

    def test_complete_web_session_revives_original_account_without_followup_auth(self):
        account_id = self._add_account()
        mailbox_state = {"provider": "dummy", "email": "invalid@example.com"}
        followup_auth = mock.Mock()
        followup_module = SimpleNamespace(recheck_custom_chatgpt_email=followup_auth)

        with (
            mock.patch.object(invalid_account_recheck.config_store, "get_all", return_value={}),
            mock.patch.object(
                invalid_account_recheck,
                "_capture_web_session_without_refresh_token",
                return_value=(self._complete_web_session(), mailbox_state),
            ) as capture,
            mock.patch.object(
                invalid_account_recheck,
                "schedule_chatgpt_local_status_refresh_for_account_id",
            ) as schedule_refresh,
            mock.patch.dict(
                sys.modules,
                {"services.chatgpt_core.custom_email_recheck": followup_module},
            ),
        ):
            result = invalid_account_recheck.recheck_invalid_chatgpt_account(
                account_id,
                retry_delays_seconds=[],
                task_id="task-recheck",
                proxy_url="http://recheck-proxy.example:18080",
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["data"]["web_session_complete"])
        self.assertEqual(capture.call_count, 1)
        self.assertEqual(capture.call_args.kwargs["proxy_url"], "http://recheck-proxy.example:18080")
        followup_auth.assert_not_called()
        schedule_refresh.assert_called_once_with(
            account_id,
            reason="invalid_account_recheck:recovered",
            delay_seconds=2.0,
        )
        joined_logs = "\n".join(result["data"]["logs"])
        self.assertIn("[失效测活] 手机验证策略：", joined_logs)
        self.assertNotIn("补抓完整 Auth", joined_logs)
        with Session(self.engine) as session:
            account = session.get(AccountModel, account_id)
            extra = account.get_extra()
            list_state = session.get(AccountListStateModel, account_id)
        self.assertEqual(account.status, "registered")
        self.assertEqual(account.token, "at-new")
        self.assertEqual(account.user_id, "acct-new")
        self.assertEqual(extra["access_token"], "at-new")
        self.assertEqual(extra["session_token"], "session-new")
        self.assertIn("__Secure-next-auth.session-token=session-new", extra["cookies"])
        self.assertEqual(extra["cookie_header"], extra["cookies"])
        self.assertEqual(extra["workspace_id"], "ws-new")
        self.assertNotIn("refresh_token", extra)
        self.assertNotIn("chatgpt_local", extra)
        self.assertEqual(extra["chatgpt_invalid_recheck"]["status"], "recovered_access_token")
        self.assertTrue(extra["chatgpt_invalid_recheck"]["has_session_token"])
        self.assertTrue(extra["chatgpt_invalid_recheck"]["has_cookies"])
        self.assertTrue(extra["chatgpt_invalid_recheck"]["web_session_complete"])
        self.assertIn("chatgpt_last_revival", extra)
        self.assertEqual(extra["chatgpt_capabilities"]["auth_level"], "access_token_only")
        self.assertIsNotNone(list_state)
        self.assertEqual(list_state.account_validity, "valid")

    def test_incomplete_web_session_does_not_revive_account(self):
        account_id = self._add_account()
        incomplete = {
            "access_token": "at-new",
            "session_token": "",
            "cookies": "",
        }
        with (
            mock.patch.object(invalid_account_recheck.config_store, "get_all", return_value={}),
            mock.patch.object(
                invalid_account_recheck,
                "_capture_web_session_without_refresh_token",
                return_value=(incomplete, {"provider": "dummy", "email": "invalid@example.com"}),
            ),
            mock.patch.object(
                invalid_account_recheck,
                "schedule_chatgpt_local_status_refresh_for_account_id",
            ) as schedule_refresh,
        ):
            result = invalid_account_recheck.recheck_invalid_chatgpt_account(
                account_id,
                retry_delays_seconds=[],
            )

        self.assertFalse(result["ok"])
        self.assertIn("Web Session 材料不完整", result["error"])
        schedule_refresh.assert_not_called()
        with Session(self.engine) as session:
            account = session.get(AccountModel, account_id)
            extra = account.get_extra()
        self.assertEqual(account.status, "invalid")
        self.assertEqual(account.token, "old-token")
        self.assertEqual(extra["refresh_token"], "old-rt")
        self.assertEqual(extra["session_token"], "old-session")
        self.assertIn("chatgpt_local", extra)
        self.assertFalse(extra["chatgpt_invalid_recheck"]["web_session_complete"])

    def test_deactivated_result_stays_invalid_and_records_reason(self):
        account_id = self._add_account()
        with (
            mock.patch.object(invalid_account_recheck.config_store, "get_all", return_value={}),
            mock.patch.object(
                invalid_account_recheck,
                "_capture_web_session_without_refresh_token",
                side_effect=RuntimeError(
                    "account_deactivated: You do not have an account because it has been deleted or deactivated."
                ),
            ) as capture,
        ):
            result = invalid_account_recheck.recheck_invalid_chatgpt_account(
                account_id,
                retry_delays_seconds=[],
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["data"]["error_code"], "account_deactivated")
        self.assertEqual(capture.call_count, 1)
        with Session(self.engine) as session:
            account = session.get(AccountModel, account_id)
            extra = account.get_extra()
        self.assertEqual(account.status, "invalid")
        self.assertEqual(account.token, "old-token")
        self.assertEqual(extra["chatgpt_invalid_recheck"]["status"], "account_deactivated")
        self.assertEqual(extra["chatgpt_capabilities"]["auth_level"], "invalid")

    def test_web_session_capture_uses_login_only_browser_transport(self):
        transport_result = SimpleNamespace(
            ok=True,
            access_token="at-new",
            session_token="session-new",
            cookies="oai-did=device-new; __Secure-next-auth.session-token=session-new",
            cookie_header="oai-did=device-new; __Secure-next-auth.session-token=session-new",
            account_id="acct-new",
            workspace_id="ws-new",
            error_message="",
        )

        class _FakeEmailService:
            def __init__(self, *, state, log_fn=None, task_control=None, attempt_id=None):
                self.state = dict(state or {})

            def create_email(self):
                return {"email": "invalid@example.com"}

            def get_verification_code(self, **_kwargs):
                return "123456"

            def export_state(self):
                return dict(self.state)

        with (
            mock.patch.object(invalid_account_recheck, "RestoredEmailService", _FakeEmailService),
            mock.patch(
                "services.chatgpt_core.any_auto.transport.run_any_auto_browser_registration",
                return_value=transport_result,
            ) as run_browser,
        ):
            tokens, exported_state = invalid_account_recheck._capture_web_session_without_refresh_token(
                email="invalid@example.com",
                password="pw",
                exported_mailbox_state={"provider": "dummy", "email": "invalid@example.com"},
                browser_mode="protocol",
                log_fn=lambda _message: None,
                proxy_url="http://browser-proxy.example:18080",
            )

        self.assertEqual(tokens["access_token"], "at-new")
        self.assertEqual(tokens["session_token"], "session-new")
        self.assertEqual(exported_state["provider"], "dummy")
        kwargs = run_browser.call_args.kwargs
        self.assertTrue(kwargs["login_only"])
        self.assertTrue(kwargs["headless"])
        self.assertIsNone(kwargs["phone_callback"])
        self.assertEqual(kwargs["proxy_url"], "http://browser-proxy.example:18080")
        self.assertEqual(kwargs["otp_callback"](), "123456")

    def test_invalid_recheck_proxy_settings_keep_legacy_direct_and_accept_task_modes(self):
        self.assertEqual(
            _recheck_proxy_settings(InvalidRecheckTaskRequest(account_id=1))["proxy_mode"],
            "direct",
        )
        settings = _recheck_proxy_settings(
            InvalidRecheckTaskRequest(
                account_id=1,
                proxy_mode="specified",
                proxy="http://manual-proxy.example:18080",
                proxy_failover=True,
                proxy_country_code="us",
                proxy_max_candidates=9,
                proxy_min_score=65,
            )
        )
        self.assertEqual(settings["proxy_mode"], "specified")
        self.assertEqual(settings["proxy"], "http://manual-proxy.example:18080")
        self.assertEqual(settings["proxy_country_code"], "US")
        self.assertTrue(settings["proxy_failover"])
        self.assertEqual(settings["proxy_max_candidates"], 9)

    def test_batch_invalid_recheck_preserves_requested_concurrency_above_five(self):
        account_ids = [
            self._add_account(email=f"parallel-{index}@example.com")
            for index in range(1, 7)
        ]
        created_meta = {}

        class _BackgroundTasks:
            def __init__(self):
                self.calls = []

            def add_task(self, *args, **kwargs):
                self.calls.append((args, kwargs))

        def _fake_create_task(_task_id, *, platform, source, total, meta):
            created_meta.update(meta)

        background_tasks = _BackgroundTasks()
        request = BatchInvalidRecheckTaskRequest(
            account_ids=account_ids,
            params={"concurrency": 6, "proxy_mode": "direct"},
        )
        with (
            mock.patch("api.tasks._create_standalone_task_record", side_effect=_fake_create_task),
            mock.patch("api.tasks._save_task_log"),
        ):
            response = enqueue_batch_invalid_recheck_task(
                request,
                background_tasks=background_tasks,
            )

        self.assertEqual(response["requested_concurrency"], 6)
        self.assertEqual(response["effective_concurrency"], 6)
        self.assertEqual(created_meta["requested_concurrency"], 6)
        self.assertEqual(created_meta["effective_concurrency"], 6)
        runner_args = background_tasks.calls[0][0]
        self.assertEqual(runner_args[3]["concurrency"], 6)
        self.assertEqual(runner_args[3]["proxy_mode"], "direct")

    def test_batch_invalid_recheck_runner_executes_more_than_five_workers(self):
        account_ids = [
            self._add_account(email=f"worker-{index}@example.com")
            for index in range(1, 7)
        ]
        task_id = "task-invalid-recheck-concurrency-above-five"
        _create_standalone_task_record(
            task_id,
            platform="chatgpt",
            source="batch_invalid_recheck",
            total=len(account_ids),
            meta={
                "emails": [f"worker-{index}@example.com" for index in range(1, 7)],
                "missing_ids": [],
                "skipped_items": [],
                "requested_concurrency": 6,
                "effective_concurrency": 6,
            },
        )
        barrier = threading.Barrier(6)
        state_lock = threading.Lock()
        active = 0
        peak = 0

        def _fake_execute(*, account_id, **_kwargs):
            nonlocal active, peak
            with state_lock:
                active += 1
                peak = max(peak, active)
            try:
                barrier.wait(timeout=3)
                return {"ok": True, "data": {"invalid_recheck": {}}}, f"worker-{account_id}@example.com"
            finally:
                with state_lock:
                    active -= 1

        with (
            mock.patch(
                "api.tasks._execute_invalid_recheck_with_proxy_candidates",
                side_effect=_fake_execute,
            ),
            mock.patch("api.actions._apply_chatgpt_invalid_recheck_result"),
            mock.patch("api.tasks._save_task_log"),
        ):
            _run_batch_invalid_recheck(
                task_id,
                account_ids,
                {
                    "requested_concurrency": 6,
                    "concurrency": 6,
                    "proxy_mode": "direct",
                },
            )

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(peak, 6)
        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["success"], 6)
        self.assertEqual(snapshot["meta"]["effective_concurrency"], 6)
        self.assertEqual(len(snapshot["meta"]["results"]), 6)

    def test_batch_resolver_only_allows_invalid_accounts(self):
        invalid_id = self._add_account(email="invalid-one@example.com", status="invalid")
        registered_id = self._add_account(
            email="registered-one@example.com",
            status="registered",
            extra='{"chatgpt_mailbox_state": {"provider": "dummy", "email": "registered-one@example.com"}}',
        )
        req = BatchInvalidRecheckTaskRequest(account_ids=[invalid_id, registered_id, 999999])

        eligible, missing_ids, skipped, matched = _resolve_batch_invalid_recheck_accounts(req)

        self.assertEqual([item["account_id"] for item in eligible], [invalid_id])
        self.assertEqual(missing_ids, [999999])
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["account_id"], registered_id)
        self.assertEqual(matched, [])


if __name__ == "__main__":
    unittest.main()
