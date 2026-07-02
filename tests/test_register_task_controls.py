import unittest
from unittest.mock import patch

import api.actions as api_actions
from sqlmodel import Session, SQLModel, create_engine
from core.task_runtime import StopTaskRequested
from api.tasks import (
    BatchPaymentLinkTaskRequest,
    BatchResumeSubscriptionAuthTaskRequest,
    PhoneBindingTestTaskRequest,
    RegisterTaskRequest,
    enqueue_batch_payment_link_task,
    enqueue_batch_resume_subscription_auth_task,
    enqueue_phone_binding_test_task,
    _run_batch_payment_links,
    _create_task_record,
    _create_standalone_task_record,
    _build_effective_register_extra,
    _run_batch_resume_subscription_auth,
    _run_phone_binding_test,
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
        self._checkpoint()
        return "123456"


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
    calls = 0

    def __init__(self, config=None, mailbox=None):
        super().__init__(config)
        self.mailbox = mailbox

    def register(self, email: str, password: str = None) -> Account:
        type(self).calls += 1
        if type(self).calls > 1:
            return Account(
                platform="chatgpt",
                email="success-after-nonzero@example.com",
                password=password or "pw",
                token="at-demo-success",
                extra={},
            )
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
    calls = 0

    def __init__(self, config=None, mailbox=None):
        super().__init__(config)
        self.mailbox = mailbox

    def register(self, email: str, password: str = None) -> Account:
        type(self).calls += 1
        if type(self).calls > 1:
            return Account(
                platform="chatgpt",
                email="success-after-already-paid@example.com",
                password=password or "pw",
                token="at-demo-success",
                extra={},
            )
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


class _FakeChatGPTAlwaysFailPlatform(BasePlatform):
    name = "chatgpt"
    display_name = "ChatGPT"
    calls = 0

    def __init__(self, config=None, mailbox=None):
        super().__init__(config)
        self.mailbox = mailbox

    def register(self, email: str, password: str = None) -> Account:
        type(self).calls += 1
        raise RuntimeError("fake phone signup failure")

    def check_valid(self, account: Account) -> bool:
        return True


class RegisterTaskControlFlowTests(unittest.TestCase):
    def _build_request(self):
        return RegisterTaskRequest(
            platform="chatgpt",
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
            patch("services.chatgpt_core.ChatGPTPlatform", _FakePlatform),
            patch("core.base_mailbox.create_mailbox", return_value=_FakeMailbox()),
            patch("core.db.save_account", side_effect=lambda account: account),
            patch("api.tasks._save_task_log"),
        ):
            _run_register(task_id, req)

        return _task_store.snapshot(task_id)

    def test_skip_current_marks_attempt_as_skipped(self):
        snapshot = self._run_with_control("task-control-skip", skip=True)

        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["success"], 1)
        self.assertEqual(snapshot["skipped"], 1)
        self.assertEqual(snapshot["errors"], [])

    def test_stop_marks_task_as_stopped(self):
        snapshot = self._run_with_control("task-control-stop", stop=True)

        self.assertEqual(snapshot["status"], "stopped")
        self.assertEqual(snapshot["success"], 0)
        self.assertEqual(snapshot["skipped"], 0)
        self.assertEqual(snapshot["errors"], [])

    def test_chatgpt_nonzero_checkout_amount_counts_failure_without_saving_and_continues(self):
        task_id = "task-chatgpt-skip-save"
        req = RegisterTaskRequest(
            platform="chatgpt",
            count=1,
            concurrency=1,
            proxy="http://proxy.local:8080",
            extra={"mail_provider": "fake"},
        )
        _create_task_record(task_id, req, "manual", None)
        _FakeChatGPTSkipSavePlatform.calls = 0

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
        self.assertEqual(_FakeChatGPTSkipSavePlatform.calls, 2)
        self.assertTrue(snapshot["errors"])
        self.assertTrue(any("Plus checkout amount != 0" in error for error in snapshot["errors"]))
        self.assertEqual(save_account.call_count, 1)
        self.assertTrue(any("amount!=0: 1" in line for line in snapshot["logs"]))
        self.assertTrue(any("注册未计成功且不保存账号" in line for line in snapshot["logs"]))
        self.assertTrue(any("failed_skip_save" in str(call) for call in save_log.call_args_list))

    def test_register_max_attempts_caps_failure_retry_loop(self):
        task_id = "task-chatgpt-skip-save-capped"
        req = RegisterTaskRequest(
            platform="chatgpt",
            count=1,
            concurrency=1,
            proxy="http://proxy.local:8080",
            extra={"mail_provider": "fake", "register_max_attempts": 1},
        )
        _create_task_record(task_id, req, "manual", None)
        _FakeChatGPTSkipSavePlatform.calls = 0

        with (
            patch("services.chatgpt_core.ChatGPTPlatform", _FakeChatGPTSkipSavePlatform),
            patch("core.base_mailbox.create_mailbox", return_value=_FakeMailbox()),
            patch("core.db.save_account") as save_account,
            patch("api.tasks._save_task_log"),
        ):
            _run_register(task_id, req)

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["status"], "failed")
        self.assertEqual(snapshot["success"], 0)
        self.assertEqual(_FakeChatGPTSkipSavePlatform.calls, 1)
        self.assertEqual(save_account.call_count, 0)
        self.assertTrue(any("已达到注册最大尝试次数 1" in error for error in snapshot["errors"]))

    def test_phone_signup_manual_lines_cap_failure_retry_loop(self):
        task_id = "task-phone-signup-lines-capped"
        req = RegisterTaskRequest(
            platform="chatgpt",
            count=1,
            concurrency=1,
            proxy="http://proxy.local:8080",
            extra={
                "chatgpt_registration_entry": "phone_signup",
                "chatgpt_phone_signup_phone_lines": "+573234567890----https://sms.example/1",
            },
        )
        _create_task_record(task_id, req, "manual", None)
        _FakeChatGPTAlwaysFailPlatform.calls = 0

        with (
            patch("services.chatgpt_core.ChatGPTPlatform", _FakeChatGPTAlwaysFailPlatform),
            patch("core.db.save_account") as save_account,
            patch("api.tasks._save_task_log"),
        ):
            _run_register(task_id, req)

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["status"], "failed")
        self.assertEqual(snapshot["success"], 0)
        self.assertEqual(_FakeChatGPTAlwaysFailPlatform.calls, 1)
        self.assertEqual(save_account.call_count, 0)
        self.assertTrue(any("已达到注册最大尝试次数 1" in error for error in snapshot["errors"]))

    def test_chatgpt_already_paid_skip_save_counts_as_failure_without_saving_and_continues(self):
        task_id = "task-chatgpt-already-paid-skip-save"
        req = RegisterTaskRequest(
            platform="chatgpt",
            count=1,
            concurrency=1,
            proxy="http://proxy.local:8080",
            extra={"mail_provider": "fake"},
        )
        _create_task_record(task_id, req, "manual", None)
        _FakeChatGPTAlreadyPaidPlatform.calls = 0

        with (
            patch("services.chatgpt_core.ChatGPTPlatform", _FakeChatGPTAlreadyPaidPlatform),
            patch("core.base_mailbox.create_mailbox", return_value=_FakeMailbox()),
            patch("core.db.save_account") as save_account,
            patch("api.tasks._save_task_log") as save_log,
        ):
            _run_register(task_id, req)

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["success"], 1)
        self.assertEqual(_FakeChatGPTAlreadyPaidPlatform.calls, 2)
        self.assertTrue(snapshot["errors"])
        self.assertEqual(save_account.call_count, 1)
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
                side_effect=lambda _account, allow_phone_verification=False, log_fn=None, shared_phone_service=None, stop_checker=None: (
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

    def test_resume_auth_task_stops_inside_current_account(self):
        task_id = "task-resume-auth-stop-current"
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

        def _fake_execute(_account, allow_phone_verification=False, log_fn=None, shared_phone_service=None, stop_checker=None):
            self.assertTrue(callable(stop_checker))
            if callable(log_fn):
                log_fn("[补抓] fake runtime log before stop")
            _task_store.request_stop(task_id)
            stop_checker()
            return {"ok": True, "data": {"message": "should not reach"}}

        with (
            patch("api.tasks.Session", return_value=_FakeSession()),
            patch.object(api_actions, "_execute_chatgpt_resume_subscription_auth", side_effect=_fake_execute),
            patch.object(api_actions, "_apply_chatgpt_resume_auth_result"),
            patch("api.tasks._save_task_log") as save_log,
        ):
            _run_resume_subscription_auth(task_id, 123)

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["status"], "stopped")
        self.assertEqual(snapshot["success"], 0)
        self.assertTrue(any("[STOP]" in line for line in snapshot["logs"]))
        self.assertTrue(any(
            call.kwargs.get("status") == "stopped" or (len(call.args) >= 3 and call.args[2] == "stopped")
            for call in save_log.call_args_list
        ))

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

        def _fake_execute(account, allow_phone_verification=False, log_fn=None, shared_phone_service=None, stop_checker=None):
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
            call.kwargs.get("status") == "failed" or (len(call.args) >= 3 and call.args[2] == "failed")
            for call in save_log.call_args_list
        ))

    def test_batch_resume_auth_uses_gateway_global_serial_lane(self):
        task_id = "task-batch-resume-auth-shared-phone"
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

        shared_services = []

        def _fake_execute(_account, allow_phone_verification=False, log_fn=None, shared_phone_service=None, stop_checker=None):
            shared_services.append(shared_phone_service)
            return {"ok": True, "data": {"message": "done"}}

        class _FakeBasePhoneService:
            enabled = True
            max_attempts = 3
            max_resend_attempts = 20
            resend_interval_seconds = 30

            def complete(self, _entry):
                return None

        base_phone_service = _FakeBasePhoneService()

        with (
            patch("api.tasks.Session", return_value=_FakeSession()),
            patch("core.config_store.config_store.get_all", return_value={"chatgpt_phone_verification_provider": "local_gateway"}),
            patch("services.chatgpt_core.phone_service.create_phone_service", return_value=base_phone_service) as create_service,
            patch.object(api_actions, "_execute_chatgpt_resume_subscription_auth", side_effect=_fake_execute),
            patch.object(api_actions, "_apply_chatgpt_resume_auth_result"),
            patch("api.tasks._save_task_log"),
        ):
            _run_batch_resume_subscription_auth(task_id, [123, 456], allow_phone_verification=True)

        self.assertEqual(create_service.call_count, 1)
        self.assertEqual(len(shared_services), 2)
        self.assertIsNotNone(shared_services[0])
        self.assertIs(shared_services[0], shared_services[1])
        snapshot = _task_store.snapshot(task_id)
        self.assertTrue(any("全局串行通道" in line for line in snapshot["logs"]))

    def test_batch_resume_auth_stops_inside_current_account(self):
        task_id = "task-batch-resume-auth-stop-current"
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

        calls = []

        def _fake_execute(account, allow_phone_verification=False, log_fn=None, shared_phone_service=None, stop_checker=None):
            calls.append(str(account.email))
            if callable(log_fn):
                log_fn(f"[补抓] fake runtime log {account.email}")
            _task_store.request_stop(task_id)
            if callable(stop_checker):
                stop_checker()
            raise StopTaskRequested()

        with (
            patch("api.tasks.Session", return_value=_FakeSession()),
            patch.object(api_actions, "_execute_chatgpt_resume_subscription_auth", side_effect=_fake_execute),
            patch.object(api_actions, "_apply_chatgpt_resume_auth_result"),
            patch("api.tasks._save_task_log") as save_log,
        ):
            _run_batch_resume_subscription_auth(task_id, [123, 456])

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["status"], "stopped")
        self.assertEqual(calls, ["resume123@example.com"])
        self.assertTrue(any("[STOP]" in line for line in snapshot["logs"]))
        self.assertTrue(any(
            call.kwargs.get("status") == "stopped" or (len(call.args) >= 3 and call.args[2] == "stopped")
            for call in save_log.call_args_list
        ))

    def test_phone_binding_test_accepts_configured_account_interval(self):
        class _BackgroundTasks:
            def __init__(self):
                self.calls = []

            def add_task(self, *args, **kwargs):
                self.calls.append((args, kwargs))

        created_meta = {}

        def _fake_create_task_record(_task_id, *, platform, source, total, meta):
            created_meta.update(meta)

        req = PhoneBindingTestTaskRequest(
            account_ids=[123],
            phone_lines="+13434832954----https://example.com/api/record?token=demo",
            account_interval_seconds=30,
            reuse_phone_until_unusable=True,
        )
        background_tasks = _BackgroundTasks()

        with (
            patch(
                "api.tasks._resolve_phone_binding_test_accounts",
                return_value=(
                    [{"account_id": 123, "email": "phone-test@example.com", "status": "pending_payment"}],
                    [],
                    [],
                    [],
                ),
            ),
            patch("api.tasks._create_standalone_task_record", side_effect=_fake_create_task_record),
            patch("api.tasks._save_task_log"),
        ):
            result = enqueue_phone_binding_test_task(req, background_tasks=background_tasks)

        self.assertTrue(result["task_id"])
        self.assertEqual(created_meta["settings"]["account_interval_seconds"], 30)
        self.assertTrue(created_meta["settings"]["reuse_phone_until_unusable"])
        self.assertEqual(len(background_tasks.calls), 1)
        queued_settings = background_tasks.calls[0][0][4]
        self.assertEqual(queued_settings["account_interval_seconds"], 30)
        self.assertTrue(queued_settings["reuse_phone_until_unusable"])

    def test_phone_binding_test_pairs_one_account_to_one_phone(self):
        task_id = "task-phone-binding-pairwise"
        _create_standalone_task_record(
            task_id,
            platform="chatgpt",
            source="phone_binding_test",
            total=2,
            meta={"missing_ids": [], "parse_errors": []},
        )
        phone_items = [
            {
                "line_no": 1,
                "phone": "+11111111111",
                "api_url": "https://example.com/api/one",
                "raw_line": "+11111111111----https://example.com/api/one",
            },
            {
                "line_no": 2,
                "phone": "+12222222222",
                "api_url": "https://example.com/api/two",
                "raw_line": "+12222222222----https://example.com/api/two",
            },
        ]

        class _FakeAccount:
            def __init__(self, account_id: int):
                self.id = account_id
                self.platform = "chatgpt"
                self.email = f"phone{account_id}@example.com"
                self.extra = {}

            def get_extra(self):
                return dict(self.extra)

            def set_extra(self, extra):
                self.extra = dict(extra or {})

        accounts_by_id = {}

        class _FakeSession:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def get(self, _model, account_id):
                account_id_value = int(account_id or 0)
                if account_id_value not in accounts_by_id:
                    accounts_by_id[account_id_value] = _FakeAccount(account_id_value)
                return accounts_by_id[account_id_value]

            def refresh(self, _account):
                return None

            def add(self, _account):
                return None

            def commit(self):
                return None

        attempts = []

        def _fake_execute(account, allow_phone_verification=False, log_fn=None, shared_phone_service=None, stop_checker=None, retry_delays_seconds=None):
            entry = shared_phone_service.current_entry
            attempts.append((int(account.id or 0), entry.phone))
            shared_phone_service.last_expired_date = "2026-07-06 00:00:00"
            if int(account.id or 0) == 456:
                shared_phone_service.last_code_time = "2026-06-02 20:48:02"
                shared_phone_service.last_code_was_extracted = True
                shared_phone_service.complete(entry)
            return {"ok": True, "data": {"message": "done"}}

        with (
            patch("api.tasks.Session", return_value=_FakeSession()),
            patch.object(api_actions, "_execute_chatgpt_resume_subscription_auth", side_effect=_fake_execute),
            patch.object(api_actions, "_apply_chatgpt_resume_auth_result"),
            patch("api.tasks._save_task_log"),
            patch("api.tasks.time.monotonic", side_effect=[100.0, 200.0, 300.0]),
        ):
            _run_phone_binding_test(
                task_id,
                [123, 456],
                phone_items,
                {
                    "timeout_seconds": 180,
                    "poll_interval_seconds": 5,
                    "max_resend_attempts": 0,
                    "resend_interval_seconds": 0,
                    "account_interval_seconds": 60,
                },
            )

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(attempts, [(123, "+11111111111"), (456, "+11111111111")])
        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["success"], 1)
        self.assertEqual(snapshot["skipped"], 1)
        runtime_results = snapshot["meta"]["runtime_results"]
        self.assertEqual([item["phone"] for item in runtime_results], ["+11111111111", "+11111111111"])
        self.assertEqual(runtime_results[0]["status"], "account_phone_bound")
        self.assertEqual(runtime_results[1]["status"], "bound")
        self.assertEqual(runtime_results[1]["api_expired_date"], "2026-07-06 00:00:00")
        self.assertEqual(runtime_results[1]["code_time"], "2026-06-02 20:48:02")
        self.assertTrue(runtime_results[1]["code_extracted"])
        saved_binding = accounts_by_id[456].extra["chatgpt_phone_binding"]
        self.assertEqual(saved_binding["phone"], "+11111111111")
        self.assertEqual(saved_binding["api_url"], "https://example.com/api/one")
        self.assertEqual(saved_binding["raw_line"], "+11111111111----https://example.com/api/one")
        self.assertEqual(accounts_by_id[456].extra["chatgpt_phone_binding_history"][-1]["task_id"], task_id)
        self.assertTrue(any("结果表已生成" in line for line in snapshot["logs"]))
        self.assertFalse(any("===== 手机号" in line for line in snapshot["logs"]))

    def test_phone_binding_test_reuses_phone_until_terminal_failure(self):
        task_id = "task-phone-binding-reuse"
        _create_standalone_task_record(
            task_id,
            platform="chatgpt",
            source="phone_binding_test",
            total=1,
            meta={"missing_ids": [], "parse_errors": []},
        )
        phone_items = [
            {
                "line_no": 1,
                "phone": "+15555550123",
                "api_url": "https://example.com/api/reuse",
                "raw_line": "+15555550123----https://example.com/api/reuse",
            },
        ]

        class _FakeAccount:
            def __init__(self, account_id: int):
                self.id = account_id
                self.platform = "chatgpt"
                self.email = f"reuse{account_id}@example.com"
                self.extra = {}

            def get_extra(self):
                return dict(self.extra)

            def set_extra(self, extra):
                self.extra = dict(extra or {})

        accounts_by_id = {}

        class _FakeSession:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def get(self, _model, account_id):
                account_id_value = int(account_id or 0)
                if account_id_value not in accounts_by_id:
                    accounts_by_id[account_id_value] = _FakeAccount(account_id_value)
                return accounts_by_id[account_id_value]

            def refresh(self, _account):
                return None

            def add(self, _account):
                return None

            def commit(self):
                return None

        attempts = []

        def _fake_execute(account, allow_phone_verification=False, log_fn=None, shared_phone_service=None, stop_checker=None, retry_delays_seconds=None):
            entry = shared_phone_service.current_entry
            account_id = int(account.id or 0)
            attempts.append((account_id, entry.phone))
            shared_phone_service.mark_sms_sent(entry)
            shared_phone_service.last_expired_date = "2026-07-06 00:00:00"
            if account_id in {101, 102}:
                shared_phone_service.last_code_time = f"2026-06-02 20:48:0{account_id - 100}"
                shared_phone_service.complete(entry)
                return {"ok": True, "data": {"message": "done"}}
            return {
                "ok": False,
                "error": "add_phone 阶段失败: add-phone/send 失败: This phone number is already linked to the maximum number of accounts.",
                "data": {"message": "This phone number is already linked to the maximum number of accounts."},
            }

        with (
            patch("api.tasks.Session", return_value=_FakeSession()),
            patch.object(api_actions, "_execute_chatgpt_resume_subscription_auth", side_effect=_fake_execute),
            patch.object(api_actions, "_apply_chatgpt_resume_auth_result"),
            patch("api.tasks._save_task_log"),
            patch("api.tasks.time.monotonic", side_effect=[100.0, 200.0, 300.0, 400.0, 500.0]),
        ):
            _run_phone_binding_test(
                task_id,
                [101, 102, 103],
                phone_items,
                {
                    "timeout_seconds": 180,
                    "poll_interval_seconds": 5,
                    "max_resend_attempts": 0,
                    "resend_interval_seconds": 0,
                    "account_interval_seconds": 30,
                    "reuse_phone_until_unusable": True,
                },
            )

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(attempts, [
            (101, "+15555550123"),
            (102, "+15555550123"),
            (103, "+15555550123"),
        ])
        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["success"], 2)
        self.assertEqual(snapshot["skipped"], 1)
        runtime_results = snapshot["meta"]["runtime_results"]
        self.assertEqual([item["status"] for item in runtime_results], ["bound", "bound", "openai_phone_limit"])
        self.assertEqual(snapshot["meta"]["bound_phone_lines"], [
            "+1555***0123----https://example.com/api/reuse",
            "+1555***0123----https://example.com/api/reuse",
        ])
        self.assertTrue(any("同号连续绑定已开启" in line for line in snapshot["logs"]))
        self.assertTrue(any("同号连续绑定继续" in line for line in snapshot["logs"]))
        self.assertFalse(any("===== 成功手机号" in line for line in snapshot["logs"]))
        self.assertNotIn("+15555550123", "\n".join(snapshot["logs"]))

    def test_phone_binding_test_does_not_consume_phone_on_account_preflight_failure(self):
        task_id = "task-phone-binding-preflight-failure"
        _create_standalone_task_record(
            task_id,
            platform="chatgpt",
            source="phone_binding_test",
            total=1,
            meta={"missing_ids": [], "parse_errors": []},
        )
        phone_items = [
            {
                "line_no": 1,
                "phone": "+13333333333",
                "api_url": "https://example.com/api/three",
                "raw_line": "+13333333333----https://example.com/api/three",
            },
        ]

        class _FakeAccount:
            def __init__(self, account_id: int):
                self.id = account_id
                self.platform = "chatgpt"
                self.email = f"phone{account_id}@example.com"
                self.extra = {}

            def get_extra(self):
                return dict(self.extra)

            def set_extra(self, extra):
                self.extra = dict(extra or {})

        accounts_by_id = {}

        class _FakeSession:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def get(self, _model, account_id):
                account_id_value = int(account_id or 0)
                if account_id_value not in accounts_by_id:
                    accounts_by_id[account_id_value] = _FakeAccount(account_id_value)
                return accounts_by_id[account_id_value]

            def refresh(self, _account):
                return None

            def add(self, _account):
                return None

            def commit(self):
                return None

        attempts = []

        def _fake_execute(account, allow_phone_verification=False, log_fn=None, shared_phone_service=None, stop_checker=None, retry_delays_seconds=None):
            entry = shared_phone_service.current_entry
            attempts.append((int(account.id or 0), entry.phone))
            if int(account.id or 0) == 123:
                return {
                    "ok": False,
                    "error": "提交邮箱失败: 409 - invalid_request_error: Your sign-in session is no longer valid. Please start over to continue.",
                    "data": {"message": "提交邮箱失败: 409 - invalid_request_error: Your sign-in session is no longer valid. Please start over to continue."},
                }
            shared_phone_service.last_expired_date = "2026-07-06 00:00:00"
            shared_phone_service.last_code_time = "2026-06-02 20:48:02"
            shared_phone_service.mark_sms_sent(entry)
            shared_phone_service.complete(entry)
            return {"ok": True, "data": {"message": "done"}}

        with (
            patch("api.tasks.Session", return_value=_FakeSession()),
            patch.object(api_actions, "_execute_chatgpt_resume_subscription_auth", side_effect=_fake_execute),
            patch.object(api_actions, "_apply_chatgpt_resume_auth_result"),
            patch("api.tasks._save_task_log"),
            patch("api.tasks.time.monotonic", side_effect=[100.0, 200.0, 300.0]),
        ):
            _run_phone_binding_test(
                task_id,
                [123, 456],
                phone_items,
                {
                    "timeout_seconds": 180,
                    "poll_interval_seconds": 5,
                    "max_resend_attempts": 0,
                    "resend_interval_seconds": 0,
                    "account_interval_seconds": 60,
                },
            )

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(attempts, [(123, "+13333333333"), (456, "+13333333333")])
        runtime_results = snapshot["meta"]["runtime_results"]
        self.assertEqual(len(runtime_results), 1)
        self.assertEqual(runtime_results[0]["status"], "bound")
        self.assertEqual(runtime_results[0]["phone"], "+13333333333")
        self.assertEqual(snapshot["success"], 1)
        self.assertEqual(snapshot["skipped"], 0)
        self.assertTrue(any("账号前置失败" in line and "手机号未被触碰" in line for line in snapshot["logs"]))
        self.assertFalse(any("===== 手机号" in line for line in snapshot["logs"]))
        self.assertNotIn("+13333333333", "\n".join(snapshot["logs"]))


class BatchPaymentLinkTaskTests(unittest.TestCase):
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

    def _add_account(
        self,
        *,
        email: str,
        status: str = "registered",
        link_status: str = "",
        cached_url: str = "https://pay.example.test/cached",
        country: str = "US",
        currency: str = "USD",
    ) -> int:
        extra = {
            "chatgpt_last_payment_link": {
                "url": cached_url,
                "plan": "plus",
                "country": country,
                "currency": currency,
                "proxy": "",
                "link_status": link_status,
            }
        }
        with Session(self.engine) as session:
            row = AccountModel(
                platform="chatgpt",
                email=email,
                password="pw",
                status=status,
            )
            row.set_extra(extra)
            session.add(row)
            session.commit()
            session.refresh(row)
            return int(row.id or 0)

    def _create_payment_link_task(
        self,
        task_id: str,
        account_id: int,
        email: str,
        *,
        force_refresh: bool = False,
        params: dict | None = None,
    ) -> None:
        _create_standalone_task_record(
            task_id,
            platform="chatgpt",
            source="batch_payment_link",
            total=1,
            meta={
                "account_ids": [account_id],
                "emails": [email],
                "params": dict(params or {}),
                "skip_existing": True,
                "force_refresh": bool(force_refresh),
                "skipped_items": [],
                "missing_ids": [],
            },
        )

    def test_batch_payment_link_regenerates_invalid_cached_link(self):
        task_id = "task-batch-payment-invalid-cache"
        account_id = self._add_account(email="invalid-cache@example.com", link_status="invalid")
        self._create_payment_link_task(task_id, account_id, "invalid-cache@example.com")
        calls = []

        def _fake_execute(_instance, _platform, _account, _action, params, _session):
            calls.append(dict(params))
            return {
                "ok": True,
                "data": {
                    "url": "https://pay.example.test/new",
                    "plan": "plus",
                    "country": "US",
                    "currency": "USD",
                    "cache_reused": False,
                },
            }

        class _FakeChatGPTPlatform:
            def __init__(self, config=None):
                self.config = config

        with (
            patch("services.chatgpt_core.ChatGPTPlatform", _FakeChatGPTPlatform),
            patch("core.config_store.config_store.get_all", return_value={}),
            patch.object(api_actions, "_execute_platform_action", side_effect=_fake_execute),
            patch("api.tasks._save_task_log"),
        ):
            _run_batch_payment_links(task_id, [account_id])

        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0]["reuse_cached_link"], False)
        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["success"], 1)
        self.assertTrue(any("缓存订阅链接无效，重新生成" in line for line in snapshot["logs"]))

    def test_batch_payment_link_syncs_already_paid_instead_of_generating(self):
        task_id = "task-batch-payment-already-paid"
        account_id = self._add_account(email="already-paid-link@example.com", link_status="already_paid")
        self._create_payment_link_task(task_id, account_id, "already-paid-link@example.com")

        with (
            patch("services.chatgpt_core.ChatGPTPlatform"),
            patch("core.config_store.config_store.get_all", return_value={}),
            patch.object(api_actions, "_execute_platform_action") as execute_action,
            patch("api.tasks._sync_payment_link_account_status", return_value={"status": "subscribed"}) as sync_status,
            patch("api.tasks._save_task_log"),
        ):
            _run_batch_payment_links(task_id, [account_id])

        execute_action.assert_not_called()
        sync_status.assert_called_once()
        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["success"], 0)
        self.assertEqual(snapshot["skipped"], 1)
        self.assertTrue(any("已经支付过，开始同步账号状态" in line for line in snapshot["logs"]))

    def test_batch_payment_link_regenerates_legacy_fixed_fragment_cache(self):
        task_id = "task-batch-payment-regenerate-legacy-cache"
        account_id = self._add_account(
            email="legacy-cache@example.com",
            cached_url="https://chatgpt.com/checkout/openai_llc/cs_live_cached123",
            country="US",
            currency="USD",
        )
        _create_standalone_task_record(
            task_id,
            platform="chatgpt",
            source="batch_payment_link",
            total=1,
            meta={
                "account_ids": [account_id],
                "emails": ["legacy-cache@example.com"],
                "params": {"country": "US", "currency": "USD"},
                "skip_existing": True,
                "force_refresh": False,
                "skipped_items": [],
                "missing_ids": [],
            },
        )
        calls = []

        def _fake_execute(_instance, _platform, _account, _action, params, _session):
            calls.append(dict(params))
            url = "https://pay.openai.com/c/pay/cs_live_regenerated#fid_real"
            extra = _account.get_extra()
            extra["chatgpt_last_payment_link"] = {
                "url": url,
                "plan": "plus",
                "country": "US",
                "currency": "USD",
                "payment_link_format": "long_hosted",
            }
            _account.cashier_url = url
            _account.set_extra(extra)
            _session.add(_account)
            return {
                "ok": True,
                "data": {
                    "url": url,
                    "plan": "plus",
                    "country": "US",
                    "currency": "USD",
                    "payment_link_format": "long_hosted",
                    "cache_reused": False,
                },
            }

        with (
            patch("services.chatgpt_core.ChatGPTPlatform"),
            patch("core.config_store.config_store.get_all", return_value={}),
            patch("core.config_store.config_store.get", return_value=""),
            patch.object(api_actions, "_execute_platform_action", side_effect=_fake_execute),
            patch("api.tasks._save_task_log"),
        ):
            _run_batch_payment_links(task_id, [account_id])

        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0]["reuse_cached_link"], True)
        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["success"], 1)
        self.assertEqual(snapshot["skipped"], 0)
        self.assertEqual(snapshot["cashier_urls"], ["https://pay.openai.com/c/pay/cs_live_regenerated#fid_real"])

        with Session(self.engine) as session:
            row = session.get(AccountModel, account_id)
        self.assertEqual(row.cashier_url, "https://pay.openai.com/c/pay/cs_live_regenerated#fid_real")
        self.assertEqual(row.get_extra()["chatgpt_last_payment_link"]["url"], "https://pay.openai.com/c/pay/cs_live_regenerated#fid_real")

    def test_batch_payment_link_short_format_does_not_reuse_long_cached_link(self):
        task_id = "task-batch-payment-short-format"
        account_id = self._add_account(
            email="short-format@example.com",
            cached_url="https://pay.openai.com/c/pay/cs_live_cached_long",
            country="US",
            currency="USD",
        )
        self._create_payment_link_task(
            task_id,
            account_id,
            "short-format@example.com",
            params={"country": "US", "currency": "USD", "payment_link_format": "short_chatgpt"},
        )
        calls = []

        def _fake_execute(_instance, _platform, _account, _action, params, _session):
            calls.append(dict(params))
            return {
                "ok": True,
                "data": {
                    "url": "https://chatgpt.com/checkout/openai_llc/cs_live_short_new",
                    "plan": "plus",
                    "country": "US",
                    "currency": "USD",
                    "payment_link_format": "short_chatgpt",
                    "cache_reused": False,
                },
            }

        class _FakeChatGPTPlatform:
            def __init__(self, config=None):
                self.config = config

        with (
            patch("services.chatgpt_core.ChatGPTPlatform", _FakeChatGPTPlatform),
            patch("core.config_store.config_store.get_all", return_value={}),
            patch("core.config_store.config_store.get", return_value=""),
            patch.object(api_actions, "_execute_platform_action", side_effect=_fake_execute),
            patch("api.tasks._save_task_log"),
        ):
            _run_batch_payment_links(task_id, [account_id])

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["payment_link_format"], "short_chatgpt")
        self.assertIs(calls[0]["reuse_cached_link"], True)
        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["success"], 1)
        self.assertEqual(snapshot["skipped"], 0)

    def test_batch_payment_link_force_refresh_regenerates_existing_link(self):
        task_id = "task-batch-payment-force-refresh"
        account_id = self._add_account(email="force-refresh@example.com")
        self._create_payment_link_task(
            task_id,
            account_id,
            "force-refresh@example.com",
            force_refresh=True,
        )
        calls = []

        def _fake_execute(_instance, _platform, _account, _action, params, _session):
            calls.append(dict(params))
            return {
                "ok": True,
                "data": {
                    "url": "https://pay.example.test/fresh",
                    "plan": "plus",
                    "country": "US",
                    "currency": "USD",
                    "cache_reused": False,
                },
            }

        class _FakeChatGPTPlatform:
            def __init__(self, config=None):
                self.config = config

        with (
            patch("services.chatgpt_core.ChatGPTPlatform", _FakeChatGPTPlatform),
            patch("core.config_store.config_store.get_all", return_value={}),
            patch.object(api_actions, "_execute_platform_action", side_effect=_fake_execute),
            patch("api.tasks._save_task_log"),
        ):
            _run_batch_payment_links(task_id, [account_id])

        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0]["reuse_cached_link"], False)
        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["success"], 1)
        self.assertEqual(snapshot["skipped"], 0)

    def test_enqueue_batch_payment_link_force_refresh_still_skips_invalid_accounts(self):
        valid_id = self._add_account(email="force-valid@example.com", status="registered")
        invalid_id = self._add_account(email="force-invalid@example.com", status="invalid")

        req = BatchPaymentLinkTaskRequest(
            account_ids=[valid_id, invalid_id],
            force_refresh=True,
            skip_existing=True,
        )
        with patch("api.tasks.threading.Thread") as thread_cls:
            result = enqueue_batch_payment_link_task(req)

        self.assertTrue(str(result["task_id"]).startswith("task_"))
        self.assertEqual(result["eligible"], 1)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual([item["account_id"] for item in result["items"]], [valid_id])
        self.assertEqual([item["account_id"] for item in result["skipped_items"]], [invalid_id])
        self.assertIn("不能生成订阅链接", result["skipped_items"][0]["reason"])
        thread_cls.assert_called_once()


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

    def test_enqueue_batch_resume_auth_uses_global_phone_default_when_omitted(self):
        eligible_id = self._add_account(
            email="eligible-global@example.com",
            status="registered",
            extra={"chatgpt_capabilities": {"auth_level": "access_token_only", "upload_gate": "blocked_missing_rt"}},
        )

        req = BatchResumeSubscriptionAuthTaskRequest(account_ids=[eligible_id])
        with patch("core.config_store.config_store.get", return_value="true"), patch("api.tasks.threading.Thread") as thread_cls:
            result = enqueue_batch_resume_subscription_auth_task(req)

        self.assertTrue(result["allow_phone_verification"])
        self.assertTrue(thread_cls.call_args.kwargs["args"][2])

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

    def test_enqueue_batch_resume_auth_ignores_pending_subscription_rows(self):
        account_id = self._add_account(
            email="pending@example.com",
            status="registered",
            extra={"chatgpt_capabilities": {"auth_level": "refresh_token", "upload_gate": "ready"}},
        )
        self._add_pending(account_id=account_id, email="pending@example.com", status="subscription_pending_auth")

        req = BatchResumeSubscriptionAuthTaskRequest(account_ids=[account_id])
        with patch("api.tasks.threading.Thread") as thread_cls:
            result = enqueue_batch_resume_subscription_auth_task(req)

        self.assertEqual(result["eligible"], 0)
        self.assertEqual(result["skipped"], 1)
        thread_cls.assert_not_called()


if __name__ == "__main__":
    unittest.main()
