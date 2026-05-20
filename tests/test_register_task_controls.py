import unittest
from unittest.mock import patch

import api.actions as api_actions
from sqlmodel import Session, SQLModel, create_engine
from api.tasks import (
    BatchResumeSubscriptionAuthTaskRequest,
    RegisterTaskRequest,
    enqueue_batch_resume_subscription_auth_task,
    _create_task_record,
    _create_standalone_task_record,
    _build_effective_register_extra,
    _run_batch_resume_subscription_auth,
    _run_register,
    _run_resume_subscription_auth,
    _task_store,
)
from core.db import AccountModel, PendingBusinessInviteModel
from core.base_mailbox import BaseMailbox, MailboxAccount
from core.base_platform import Account, BasePlatform
from core import db as core_db


class _FakeMailbox(BaseMailbox):
    def get_email(self) -> MailboxAccount:
        return MailboxAccount(email="demo@example.com")

    def get_current_ids(self, account: MailboxAccount) -> set:
        return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        def poll_once():
            return None

        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=0.01,
            poll_once=poll_once,
        )


class _FakePlatform(BasePlatform):
    name = "fake"
    display_name = "Fake"

    def __init__(self, config=None, mailbox=None):
        super().__init__(config)
        self.mailbox = mailbox

    def register(self, email: str, password: str = None) -> Account:
        account = self.mailbox.get_email()
        self.mailbox.wait_for_code(account, timeout=1)
        return Account(
            platform="fake",
            email=account.email,
            password=password or "pw",
        )

    def check_valid(self, account: Account) -> bool:
        return True


class _FakeChatGPTSkipSavePlatform(BasePlatform):
    name = "chatgpt"
    display_name = "ChatGPT"

    def __init__(self, config=None, mailbox=None):
        super().__init__(config)
        self.mailbox = mailbox

    def register(self, email: str, password: str = None) -> Account:
        return Account(
            platform="chatgpt",
            email="skip-save@example.com",
            password=password or "pw",
            token="at-demo",
            extra={
                "chatgpt_checkout_amount": "34900000",
                "chatgpt_checkout_amount_is_zero": False,
                "chatgpt_checkout_url": "https://chatgpt.com/checkout/openai_llc/cs_live_123",
                "cashier_url": "https://chatgpt.com/checkout/openai_llc/cs_live_123",
                "chatgpt_skip_save_account": True,
                "chatgpt_skip_save_reason": "Plus checkout amount != 0: amount=34900000 currency=idr",
            },
        )

    def check_valid(self, account: Account) -> bool:
        return True


class _FakeChatGPTAlreadyPaidPlatform(BasePlatform):
    name = "chatgpt"
    display_name = "ChatGPT"

    def __init__(self, config=None, mailbox=None):
        super().__init__(config)
        self.mailbox = mailbox

    def register(self, email: str, password: str = None) -> Account:
        return Account(
            platform="chatgpt",
            email="already-paid@example.com",
            password=password or "pw",
            token="at-demo",
            extra={
                "chatgpt_payment_already_paid": True,
                "chatgpt_account_unavailable": True,
                "chatgpt_checkout_error_code": "already_paid",
                "chatgpt_skip_save_account": True,
                "chatgpt_skip_save_reason": "Plus checkout 已付费响应: you have paid",
            },
        )

    def check_valid(self, account: Account) -> bool:
        return True


class RegisterTaskControlFlowTests(unittest.TestCase):
    def _build_request(self):
        return RegisterTaskRequest(
            platform="fake",
            count=1,
            concurrency=1,
            proxy="http://proxy.local:8080",
            extra={"mail_provider": "fake"},
        )

    def _run_with_control(self, task_id: str, *, stop: bool = False, skip: bool = False):
        req = self._build_request()
        _create_task_record(task_id, req, "manual", None)
        if stop:
            _task_store.request_stop(task_id)
        if skip:
            _task_store.request_skip_current(task_id)

        with (
            patch("api.tasks.ChatGPTPlatform", _FakePlatform),
            patch("core.base_mailbox.create_mailbox", return_value=_FakeMailbox()),
            patch("core.db.save_account", side_effect=lambda account: account),
            patch("api.tasks._save_task_log"),
        ):
            _run_register(task_id, req)

        return _task_store.snapshot(task_id)

    def test_skip_current_marks_attempt_as_skipped(self):
        snapshot = self._run_with_control("task-control-skip", skip=True)

        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["success"], 0)
        self.assertEqual(snapshot["skipped"], 1)
        self.assertEqual(snapshot["errors"], [])

    def test_stop_marks_task_as_stopped(self):
        snapshot = self._run_with_control("task-control-stop", stop=True)

        self.assertEqual(snapshot["status"], "stopped")
        self.assertEqual(snapshot["success"], 0)
        self.assertEqual(snapshot["skipped"], 0)
        self.assertEqual(snapshot["errors"], [])

    def test_chatgpt_nonzero_checkout_amount_counts_success_without_saving(self):
        task_id = "task-chatgpt-skip-save"
        req = RegisterTaskRequest(
            platform="chatgpt",
            count=1,
            concurrency=1,
            proxy="http://proxy.local:8080",
            extra={"mail_provider": "fake"},
        )
        _create_task_record(task_id, req, "manual", None)

        with (
            patch("services.chatgpt_core.ChatGPTPlatform", _FakeChatGPTSkipSavePlatform),
            patch("core.base_mailbox.create_mailbox", return_value=_FakeMailbox()),
            patch("core.db.save_account") as save_account,
            patch("api.tasks._save_task_log") as save_log,
        ):
            _run_register(task_id, req)

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["success"], 1)
        save_account.assert_not_called()
        self.assertTrue(any("amount!=0: 1" in line for line in snapshot["logs"]))
        self.assertTrue(any("success_skip_save" in str(call) for call in save_log.call_args_list))

    def test_chatgpt_already_paid_skip_save_counts_as_failure_without_saving(self):
        task_id = "task-chatgpt-already-paid-skip-save"
        req = RegisterTaskRequest(
            platform="chatgpt",
            count=1,
            concurrency=1,
            proxy="http://proxy.local:8080",
            extra={"mail_provider": "fake"},
        )
        _create_task_record(task_id, req, "manual", None)

        with (
            patch("services.chatgpt_core.ChatGPTPlatform", _FakeChatGPTAlreadyPaidPlatform),
            patch("core.base_mailbox.create_mailbox", return_value=_FakeMailbox()),
            patch("core.db.save_account") as save_account,
            patch("api.tasks._save_task_log") as save_log,
        ):
            _run_register(task_id, req)

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["status"], "failed")
        self.assertEqual(snapshot["success"], 0)
        self.assertTrue(snapshot["errors"])
        save_account.assert_not_called()
        self.assertTrue(any("注册未计成功且不保存账号" in line for line in snapshot["logs"]))
        self.assertTrue(any("failed_skip_save" in str(call) for call in save_log.call_args_list))

    def test_effective_register_extra_uses_access_token_checkout_defaults(self):
        req = RegisterTaskRequest(
            platform="chatgpt",
            count=1,
            concurrency=1,
            extra={"mail_provider": "fake"},
        )

        with patch("core.config_store.config_store.get_all", return_value={
            "chatgpt_access_token_only_checkout_country": "US",
            "chatgpt_access_token_only_checkout_currency": "USD",
        }):
            extra = _build_effective_register_extra(req)

        self.assertEqual(extra["chatgpt_checkout_country"], "US")
        self.assertEqual(extra["chatgpt_checkout_currency"], "USD")

    def test_effective_register_extra_uses_usd_when_config_defaults_are_not_persisted(self):
        req = RegisterTaskRequest(
            platform="chatgpt",
            count=1,
            concurrency=1,
            extra={"mail_provider": "fake"},
        )

        with patch("core.config_store.config_store.get_all", return_value={}):
            extra = _build_effective_register_extra(req)

        self.assertEqual(extra["chatgpt_checkout_country"], "US")
        self.assertEqual(extra["chatgpt_checkout_currency"], "USD")

    def test_effective_register_extra_allows_request_checkout_override(self):
        req = RegisterTaskRequest(
            platform="chatgpt",
            count=1,
            concurrency=1,
            extra={
                "mail_provider": "fake",
                "chatgpt_access_token_only_checkout_country": "DE",
                "chatgpt_access_token_only_checkout_currency": "EUR",
            },
        )

        with patch("core.config_store.config_store.get_all", return_value={
            "chatgpt_access_token_only_checkout_country": "US",
            "chatgpt_access_token_only_checkout_currency": "USD",
        }):
            extra = _build_effective_register_extra(req)

        self.assertEqual(extra["chatgpt_checkout_country"], "DE")
        self.assertEqual(extra["chatgpt_checkout_currency"], "EUR")

    def test_resume_auth_task_persists_logs_and_marks_success(self):
        task_id = "task-resume-auth-success"
        req = RegisterTaskRequest(platform="chatgpt", count=1, concurrency=1)
        _create_task_record(task_id, req, "resume_subscription_auth", {"account_id": 123})

        class _FakeSession:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def get(self, _model, _account_id):
                return type(
                    "AccountModel",
                    (),
                    {"platform": "chatgpt", "email": "resume@example.com"},
                )()

            def refresh(self, _account):
                return None

        with (
            patch("api.tasks.Session", return_value=_FakeSession()),
            patch.object(
                api_actions,
                "_execute_chatgpt_resume_subscription_auth",
                side_effect=lambda _account, log_fn=None: (
                    log_fn("[补抓] fake runtime log") if callable(log_fn) else None,
                    {"ok": True, "data": {"message": "done"}},
                )[1],
            ),
            patch.object(api_actions, "_apply_chatgpt_resume_auth_result"),
            patch("api.tasks._save_task_log") as save_log,
        ):
            _run_resume_subscription_auth(task_id, 123)

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["success"], 1)
        self.assertTrue(any("fake runtime log" in line for line in snapshot["logs"]))
        self.assertTrue(any(call.kwargs.get("status") == "success" or (len(call.args) >= 3 and call.args[2] == "success") for call in save_log.call_args_list))

    def test_batch_resume_auth_task_persists_logs_and_aggregates_results(self):
        task_id = "task-batch-resume-auth-success"
        _create_standalone_task_record(
            task_id,
            platform="chatgpt",
            source="batch_resume_subscription_auth",
            total=2,
            meta={"account_ids": [123, 456], "eligible": 2, "skipped_items": [], "missing_ids": []},
        )

        class _FakeSession:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def get(self, _model, account_id):
                account_id_value = int(account_id or 0)
                return type(
                    "AccountModel",
                    (),
                    {"platform": "chatgpt", "email": f"resume{account_id_value}@example.com"},
                )()

            def refresh(self, _account):
                return None

        def _fake_execute(account, log_fn=None):
            if callable(log_fn):
                log_fn(f"[补抓] fake runtime log {account.email}")
            if str(account.email).startswith("resume456"):
                return {"ok": False, "error": "workspace missing", "data": {"message": "workspace missing"}}
            return {"ok": True, "data": {"message": "done"}}

        with (
            patch("api.tasks.Session", return_value=_FakeSession()),
            patch.object(api_actions, "_execute_chatgpt_resume_subscription_auth", side_effect=_fake_execute),
            patch.object(api_actions, "_apply_chatgpt_resume_auth_result"),
            patch("api.tasks._save_task_log") as save_log,
        ):
            _run_batch_resume_subscription_auth(task_id, [123, 456])

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["success"], 1)
        self.assertEqual(snapshot["skipped"], 0)
        self.assertTrue(any("resume123@example.com" in line for line in snapshot["logs"]))
        self.assertTrue(any("resume456@example.com" in line for line in snapshot["logs"]))
        self.assertTrue(any("workspace missing" in error for error in snapshot["errors"]))
        self.assertTrue(any(
            call.kwargs.get("status") == "success" or (len(call.args) >= 3 and call.args[2] == "success")
            for call in save_log.call_args_list
        ))


class BatchResumeAuthTaskCreationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        self.core_engine_patch = patch.object(core_db, "engine", self.engine)
        self.tasks_engine_patch = patch("api.tasks.engine", self.engine)
        self.core_engine_patch.start()
        self.tasks_engine_patch.start()
        SQLModel.metadata.create_all(self.engine)

    def tearDown(self):
        self.tasks_engine_patch.stop()
        self.core_engine_patch.stop()

    def _add_account(self, *, email: str, status: str = "registered", extra: dict | None = None) -> int:
        with Session(self.engine) as session:
            row = AccountModel(
                platform="chatgpt",
                email=email,
                password="pw",
                status=status,
            )
            if extra is not None:
                row.set_extra(extra)
            session.add(row)
            session.commit()
            session.refresh(row)
            return int(row.id or 0)

    def _add_pending(self, *, account_id: int, email: str, status: str) -> int:
        with Session(self.engine) as session:
            row = PendingBusinessInviteModel(
                account_id=account_id,
                email=email,
                status=status,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return int(row.id or 0)

    def test_enqueue_batch_resume_auth_creates_task_for_eligible_accounts_only(self):
        eligible_id = self._add_account(
            email="eligible@example.com",
            status="registered",
            extra={"chatgpt_capabilities": {"auth_level": "access_token_only", "upload_gate": "blocked_missing_rt"}},
        )
        skipped_id = self._add_account(
            email="skipped@example.com",
            status="registered",
            extra={"chatgpt_capabilities": {"auth_level": "refresh_token", "upload_gate": "ready"}},
        )

        req = BatchResumeSubscriptionAuthTaskRequest(account_ids=[eligible_id, skipped_id, 999999])
        with patch("api.tasks.threading.Thread") as thread_cls:
            result = enqueue_batch_resume_subscription_auth_task(req)

        self.assertTrue(str(result["task_id"]).startswith("task_"))
        self.assertEqual(result["eligible"], 1)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["missing"], 1)
        self.assertEqual([item["account_id"] for item in result["items"]], [eligible_id])
        self.assertEqual([item["account_id"] for item in result["skipped_items"]], [skipped_id])
        thread_cls.assert_called_once()

    def test_enqueue_batch_resume_auth_returns_without_task_when_nothing_is_eligible(self):
        skipped_id = self._add_account(
            email="ok@example.com",
            status="registered",
            extra={"chatgpt_capabilities": {"auth_level": "refresh_token", "upload_gate": "ready"}},
        )

        req = BatchResumeSubscriptionAuthTaskRequest(account_ids=[skipped_id])
        with patch("api.tasks.threading.Thread") as thread_cls:
            result = enqueue_batch_resume_subscription_auth_task(req)

        self.assertEqual(result["task_id"], "")
        self.assertEqual(result["eligible"], 0)
        self.assertEqual(result["skipped"], 1)
        thread_cls.assert_not_called()

    def test_enqueue_batch_resume_auth_marks_pending_subscription_rows_as_eligible(self):
        account_id = self._add_account(
            email="pending@example.com",
            status="registered",
            extra={"chatgpt_capabilities": {"auth_level": "refresh_token", "upload_gate": "ready"}},
        )
        self._add_pending(account_id=account_id, email="pending@example.com", status="subscription_pending_auth")

        req = BatchResumeSubscriptionAuthTaskRequest(account_ids=[account_id])
        with patch("api.tasks.threading.Thread") as thread_cls:
            result = enqueue_batch_resume_subscription_auth_task(req)

        self.assertEqual(result["eligible"], 1)
        self.assertEqual(result["items"][0]["pending_status"], "subscription_pending_auth")
        thread_cls.assert_called_once()


if __name__ == "__main__":
    unittest.main()
