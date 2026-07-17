import unittest
import threading
from unittest.mock import patch

import api.actions as api_actions
from fastapi import HTTPException
from sqlmodel import Session, SQLModel, create_engine, select
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
    _prepare_register_request,
    _run_batch_resume_subscription_auth,
    _run_phone_binding_test,
    _run_register,
    _run_resume_subscription_auth,
    _task_store,
)
from core.db import AccountModel, PaymentLinkGenerationModel
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


class _FakeChatGPTProxyFingerprintPlatform(BasePlatform):
    name = "chatgpt"
    display_name = "ChatGPT"
    seen: list[dict] = []
    _lock = threading.Lock()

    def __init__(self, config=None, mailbox=None):
        super().__init__(config)
        self.mailbox = mailbox

    def register(self, email: str, password: str = None) -> Account:
        extra = dict(getattr(self.config, "extra", None) or {})
        with self._lock:
            index = len(type(self).seen) + 1
            type(self).seen.append(
                {
                    "proxy": getattr(self.config, "proxy", ""),
                    "exit_ip": extra.get("chatgpt_register_exit_ip"),
                    "fingerprint": extra.get("chatgpt_browser_fingerprint"),
                    "fingerprint_signature": extra.get("chatgpt_browser_fingerprint_signature"),
                }
            )
        return Account(
            platform="chatgpt",
            email=f"unique-{index}@example.com",
            password=password or "pw",
            token=f"at-{index}",
            extra={},
        )

    def check_valid(self, account: Account) -> bool:
        return True


class EmailApiRegisterRequestTests(unittest.TestCase):
    def test_prepare_email_api_gmail_line_uses_all_identities(self):
        req = RegisterTaskRequest(
            platform="chatgpt",
            count=1,
            concurrency=5,
            extra={
                "mail_provider": "email_api",
                "email_api_lines": "name@gmail.com----api.example.com/code?id=1",
            },
        )

        prepared = _prepare_register_request(req)

        self.assertEqual(prepared.count, 2)
        self.assertEqual(prepared.concurrency, 2)
        self.assertEqual(prepared.extra["mail_provider"], "email_api")
        self.assertEqual(prepared.extra["email_api_candidate_count"], 2)

    def test_prepare_register_rejects_unique_exit_ip_with_direct_proxy_mode(self):
        req = RegisterTaskRequest(
            platform="chatgpt",
            count=2,
            concurrency=2,
            proxy_mode="direct",
            extra={
                "mail_provider": "fake",
                "chatgpt_register_unique_exit_ip_enabled": True,
            },
        )

        with self.assertRaises(HTTPException) as ctx:
            _prepare_register_request(req)

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("不能使用直连模式", str(ctx.exception.detail))

    def test_prepare_register_rejects_unique_exit_ip_single_specified_proxy_for_batch(self):
        req = RegisterTaskRequest(
            platform="chatgpt",
            count=2,
            concurrency=2,
            proxy_mode="specified",
            proxy="http://proxy.example:8080",
            proxy_failover=False,
            extra={
                "mail_provider": "fake",
                "chatgpt_register_unique_exit_ip_enabled": True,
            },
        )

        with self.assertRaises(HTTPException) as ctx:
            _prepare_register_request(req)

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("单个指定代理无法满足多个账号独立出口 IP", str(ctx.exception.detail))

    def test_prepare_email_api_non_gmail_line_uses_one_identity(self):
        req = RegisterTaskRequest(
            platform="chatgpt",
            count=10,
            concurrency=5,
            extra={
                "mail_provider": "email_api",
                "email_api_lines": "user@example.com----api.example.com/code?id=1",
            },
        )

        prepared = _prepare_register_request(req)

        self.assertEqual(prepared.count, 1)
        self.assertEqual(prepared.concurrency, 1)

    def test_prepare_email_api_rejects_bad_line(self):
        req = RegisterTaskRequest(
            platform="chatgpt",
            count=1,
            concurrency=1,
            extra={
                "mail_provider": "email_api",
                "email_api_lines": "name@gmail.com api.example.com/code",
            },
        )

        with self.assertRaises(Exception) as ctx:
            _prepare_register_request(req)

        self.assertIn("----", str(ctx.exception))


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

    def test_serial_register_logs_current_success_and_blank_separator(self):
        task_id = "task-control-log-current-success"
        req = RegisterTaskRequest(
            platform="chatgpt",
            count=2,
            concurrency=1,
            proxy="http://proxy.local:8080",
            extra={"mail_provider": "fake"},
        )
        _create_task_record(task_id, req, "manual", None)

        with (
            patch("services.chatgpt_core.ChatGPTPlatform", _FakePlatform),
            patch("core.base_mailbox.create_mailbox", return_value=_FakeMailbox()),
            patch("core.db.save_account", side_effect=lambda account: account),
            patch("api.tasks._save_task_log"),
        ):
            _run_register(task_id, req)

        snapshot = _task_store.snapshot(task_id)
        logs = snapshot["logs"]
        headers = [line for line in logs if "[账号] -------- 尝试 " in line]

        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["success"], 2)
        self.assertEqual(len(headers), 2)
        self.assertIn("尝试 1 / 目标成功 2 / 当前成功数 0", headers[0])
        self.assertIn("尝试 2 / 目标成功 2 / 当前成功数 1", headers[1])
        first_success_index = next(index for index, line in enumerate(logs) if "注册成功" in line)
        separator_index = logs.index("", first_success_index + 1)
        second_header_index = logs.index(headers[1])
        self.assertLess(first_success_index, separator_index)
        self.assertLess(separator_index, second_header_index)
        self.assertEqual(logs[second_header_index - 1], "")
        self.assertFalse(any("开始第 " in line and "目标成功数" in line for line in logs))

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

    def test_register_unique_exit_ip_skips_duplicate_candidate_and_passes_isolated_fingerprint(self):
        task_id = "task-register-unique-exit-ip"
        req = RegisterTaskRequest(
            platform="chatgpt",
            count=2,
            concurrency=2,
            proxy_mode="dynamic",
            proxy_country_code="US",
            extra={
                "mail_provider": "fake",
                "chatgpt_register_unique_exit_ip_enabled": True,
            },
        )
        _create_task_record(task_id, req, "manual", None)
        _FakeChatGPTProxyFingerprintPlatform.seen = []

        def fake_candidates(params=None, fallback_proxy=None, default_mode="direct", target="chatgpt"):
            self.assertTrue(params.get("proxy_failover"))
            self.assertGreaterEqual(int(params.get("dynamic_proxy_max_attempts") or 0), 8)
            return [
                ("http://proxy-a.local:8080", None, "dynamic country=US actual=US exit_ip=198.51.100.10 provider=test sid=refreshed probe=ok"),
                ("http://proxy-b.local:8080", None, "dynamic country=US actual=US exit_ip=198.51.100.11 provider=test sid=refreshed probe=ok"),
            ]

        def fake_probe(proxy_url, timeout_seconds=8):
            if "proxy-a" in proxy_url:
                return {"ok": True, "exit_ip": "198.51.100.10", "latency_ms": 1}
            return {"ok": True, "exit_ip": "198.51.100.11", "latency_ms": 1}

        saved_accounts = []
        def fake_save_account(account):
            saved_accounts.append(account)
            return account

        with (
            patch("services.chatgpt_core.ChatGPTPlatform", _FakeChatGPTProxyFingerprintPlatform),
            patch("core.base_mailbox.create_mailbox", return_value=_FakeMailbox()),
            patch("core.proxy_utils.resolve_task_proxy_candidates", side_effect=fake_candidates),
            patch("services.proxy_scanner.probe_basic", side_effect=fake_probe),
            patch("core.db.save_account", side_effect=fake_save_account),
            patch("api.tasks._save_task_log"),
        ):
            _run_register(task_id, req)

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["success"], 2)
        seen = list(_FakeChatGPTProxyFingerprintPlatform.seen)
        self.assertEqual(len(seen), 2)
        self.assertEqual({item["exit_ip"] for item in seen}, {"198.51.100.10", "198.51.100.11"})
        fingerprints = [item["fingerprint"] for item in seen]
        self.assertTrue(all(isinstance(item, dict) and item.get("device_id") for item in fingerprints))
        self.assertEqual(len({item["fingerprint_signature"] for item in seen}), 2)
        self.assertEqual(len(saved_accounts), 2)
        self.assertTrue(
            all(
                isinstance((account.extra or {}).get("chatgpt_browser_fingerprint"), dict)
                and (account.extra or {}).get("chatgpt_browser_fingerprint", {}).get("device_id")
                for account in saved_accounts
            )
        )
        unique_meta = dict((snapshot.get("meta") or {}).get("register_unique_exit_ip") or {})
        self.assertEqual(unique_meta.get("assigned_count"), 2)
        self.assertGreaterEqual(int(unique_meta.get("collision_count") or 0), 1)

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
            patch("core.proxy_utils.resolve_task_proxy_candidates", return_value=[("", None, "direct")]),
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
            patch("core.proxy_utils.resolve_task_proxy_candidates", return_value=[("", None, "direct")]),
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
            patch("core.proxy_utils.resolve_task_proxy_candidates", return_value=[("", None, "direct")]),
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


class _FakeLongLinkPaymentClient:
    batch_id = "batch_" + "a" * 32

    def __init__(
        self,
        *,
        profile: dict | None = None,
        submitted_status: str = "queued",
        polled_status: str = "done",
        after_submit=None,
    ):
        self.profile = dict(
            profile
            or {
                "profile_hash": "profile-hash-brl",
                "link_type": "pix",
                "country": "BR",
                "currency": "BRL",
                "effective_concurrency": 4,
                "profile": {},
            }
        )
        self.submitted_status = submitted_status
        self.polled_status = polled_status
        self.after_submit = after_submit
        self.events: list[tuple] = []
        self.submissions: list[dict] = []
        self._items: list[dict] = []

    def get_profile(self, *, force_refresh: bool = False):
        self.events.append(("profile", force_refresh))
        return dict(self.profile)

    def _remote_item(self, item: dict, index: int, status: str) -> dict:
        terminal = status not in {"queued", "running"}
        remote = {
            "batch_id": self.batch_id,
            "job_id": f"job-{index}",
            "request_id": str(item["request_id"]),
            "profile_hash": str(self.profile["profile_hash"]),
            "status": status,
            "created_at": 1_720_000_000,
            "started_at": 1_720_000_001 if status != "queued" else None,
            "completed_at": 1_720_000_002 if terminal else None,
        }
        if status == "done":
            remote["result"] = {
                "url": f"https://pay.example.test/{index}",
                "link_type": str(self.profile["link_type"]),
                "billing_country": str(self.profile["country"]),
                "currency": str(self.profile["currency"]),
            }
        elif terminal:
            remote["error"] = "remote task interrupted" if status == "interrupted" else "remote task failed"
        return remote

    def submit_batch(self, *, items: list[dict], expected_profile_hash: str):
        self.events.append(("submit", len(items)))
        self.submissions.append(
            {
                "items": [dict(item) for item in items],
                "expected_profile_hash": expected_profile_hash,
            }
        )
        self._items = [self._remote_item(item, index, self.submitted_status) for index, item in enumerate(items, start=1)]
        if self.after_submit is not None:
            self.after_submit()
        return {
            "batch_id": self.batch_id,
            "batch_ids": [self.batch_id],
            "items": [dict(item) for item in self._items],
            "summary": {"total": len(self._items), "status": self.submitted_status},
        }

    def get_batch(self, batch_id: str):
        self.events.append(("poll", batch_id))
        assert batch_id == self.batch_id
        items = [
            self._remote_item(
                {"request_id": item["request_id"]},
                index,
                self.polled_status,
            )
            for index, item in enumerate(self._items, start=1)
        ]
        return {
            "batch_id": self.batch_id,
            "status": self.polled_status,
            "summary": {"total": len(items), "status": self.polled_status},
            "items": items,
        }


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
        token: str | None = None,
        cached: dict | None = None,
    ) -> int:
        extra = {"chatgpt_last_payment_link": dict(cached)} if isinstance(cached, dict) else {}
        with Session(self.engine) as session:
            row = AccountModel(
                platform="chatgpt",
                email=email,
                password="pw",
                status=status,
                token=token if token is not None else f"access-token-{email}",
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

    def test_batch_payment_link_submits_all_accounts_before_bulk_poll_and_persists_results(self):
        task_id = "task-batch-payment-submit-then-poll"
        first_id = self._add_account(email="first@example.com")
        second_id = self._add_account(email="second@example.com")
        _create_standalone_task_record(
            task_id,
            platform="chatgpt",
            source="batch_payment_link",
            total=2,
            meta={
                "account_ids": [first_id, second_id],
                "emails": ["first@example.com", "second@example.com"],
                "params": {},
                "skip_existing": True,
                "force_refresh": False,
                "skipped_items": [],
                "missing_ids": [],
            },
        )
        client = _FakeLongLinkPaymentClient()

        with (
            patch(
                "services.chatgpt_core.long_link_payment_client.LongLinkPaymentClient.from_env",
                return_value=client,
            ),
            patch("api.tasks._save_task_log"),
        ):
            _run_batch_payment_links(task_id, [first_id, second_id])

        self.assertEqual(client.events, [("profile", True), ("submit", 2), ("poll", client.batch_id)])
        self.assertEqual(len(client.submissions), 1)
        submission = client.submissions[0]
        self.assertEqual(submission["expected_profile_hash"], "profile-hash-brl")
        self.assertEqual(
            submission["items"],
            [
                {"account_id": first_id, "email": "first@example.com", "request_id": f"auto-gpt:{task_id}:{first_id}", "access_token": "access-token-first@example.com"},
                {"account_id": second_id, "email": "second@example.com", "request_id": f"auto-gpt:{task_id}:{second_id}", "access_token": "access-token-second@example.com"},
            ],
        )
        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["success"], 2)
        self.assertEqual(snapshot["meta"]["payment_link_batch_ids"], [client.batch_id])

        with Session(self.engine) as session:
            generations = session.exec(select(PaymentLinkGenerationModel).order_by(PaymentLinkGenerationModel.account_id)).all()
            first = session.get(AccountModel, first_id)
            second = session.get(AccountModel, second_id)
        self.assertEqual(len(generations), 2)
        self.assertTrue(all(row.status == "succeeded" for row in generations))
        self.assertTrue(all(row.remote_batch_id == client.batch_id for row in generations))
        self.assertTrue(all(row.generated_at for row in generations))
        for account in (first, second):
            cache = account.get_extra()["chatgpt_last_payment_link"]
            self.assertEqual(cache["payment_source"], "long_link")
            self.assertEqual(cache["payment_link_format"], "long_link")
            self.assertEqual(cache["link_type"], "pix")
            self.assertEqual(cache["profile_hash"], "profile-hash-brl")
            self.assertTrue(cache["generated_at"])

    def test_batch_payment_link_does_not_write_old_result_to_reused_account_id(self):
        task_id = "task-batch-payment-account-id-reuse"
        account_id = self._add_account(email="original@example.com")
        self._create_payment_link_task(task_id, account_id, "original@example.com")

        def replace_account() -> None:
            with Session(self.engine) as session:
                original = session.get(AccountModel, account_id)
                self.assertIsNotNone(original)
                session.delete(original)
                session.commit()
                replacement = AccountModel(
                    id=account_id,
                    platform="chatgpt",
                    email="replacement@example.com",
                    password="pw",
                    token="replacement-access-token",
                )
                replacement.set_extra({})
                session.add(replacement)
                session.commit()

        client = _FakeLongLinkPaymentClient(after_submit=replace_account)
        with (
            patch(
                "services.chatgpt_core.long_link_payment_client.LongLinkPaymentClient.from_env",
                return_value=client,
            ),
            patch("api.tasks._save_task_log"),
        ):
            _run_batch_payment_links(task_id, [account_id])

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["status"], "failed")
        self.assertEqual(snapshot["success"], 0)
        self.assertTrue(any("账号已被删除或替换" in line for line in snapshot["logs"]))
        with Session(self.engine) as session:
            replacement = session.get(AccountModel, account_id)
            history = session.exec(
                select(PaymentLinkGenerationModel).where(
                    PaymentLinkGenerationModel.account_id == account_id
                )
            ).all()
        self.assertEqual(replacement.email, "replacement@example.com")
        self.assertNotIn("chatgpt_last_payment_link", replacement.get_extra())
        self.assertEqual(history, [])

    def test_batch_payment_link_syncs_already_paid_instead_of_generating(self):
        task_id = "task-batch-payment-already-paid"
        account_id = self._add_account(
            email="already-paid-link@example.com",
            cached={
                "url": "https://pay.example.test/already-paid",
                "plan": "plus",
                "country": "BR",
                "currency": "BRL",
                "payment_link_format": "long_link",
                "payment_source": "long_link",
                "profile_hash": "profile-hash-brl",
                "link_status": "already_paid",
            },
        )
        self._create_payment_link_task(task_id, account_id, "already-paid-link@example.com")
        client = _FakeLongLinkPaymentClient()

        with (
            patch(
                "services.chatgpt_core.long_link_payment_client.LongLinkPaymentClient.from_env",
                return_value=client,
            ),
            patch("api.tasks._sync_payment_link_account_status", return_value={"status": "subscribed"}) as sync_status,
            patch("api.tasks._save_task_log"),
        ):
            _run_batch_payment_links(task_id, [account_id])

        self.assertEqual(client.submissions, [])
        sync_status.assert_called_once()
        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["success"], 0)
        self.assertEqual(snapshot["skipped"], 1)
        self.assertTrue(any("已经支付过，开始同步账号状态" in line for line in snapshot["logs"]))

    def test_batch_payment_link_reuses_legacy_hosted_cache_unless_force_refresh(self):
        task_id = "task-batch-payment-reuse-legacy-cache"
        account_id = self._add_account(
            email="legacy-cache@example.com",
            cached={
                "url": "https://chatgpt.com/checkout/openai_llc/cs_live_cached123",
                "plan": "plus",
                "country": "US",
                "currency": "USD",
                "payment_link_format": "long_hosted",
                "payment_source": "chatgpt_hosted",
            },
        )
        self._create_payment_link_task(task_id, account_id, "legacy-cache@example.com")
        client = _FakeLongLinkPaymentClient()

        with (
            patch(
                "services.chatgpt_core.long_link_payment_client.LongLinkPaymentClient.from_env",
                return_value=client,
            ),
            patch("api.tasks._save_task_log"),
        ):
            _run_batch_payment_links(task_id, [account_id])

        self.assertEqual(client.submissions, [])
        self.assertEqual(_task_store.snapshot(task_id)["skipped"], 1)

        force_task_id = "task-batch-payment-force-legacy-cache"
        self._create_payment_link_task(
            force_task_id,
            account_id,
            "legacy-cache@example.com",
            force_refresh=True,
        )
        force_client = _FakeLongLinkPaymentClient()
        with (
            patch(
                "services.chatgpt_core.long_link_payment_client.LongLinkPaymentClient.from_env",
                return_value=force_client,
            ),
            patch("api.tasks._save_task_log"),
        ):
            _run_batch_payment_links(force_task_id, [account_id])

        self.assertEqual(len(force_client.submissions), 1)
        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["success"], 0)
        self.assertEqual(snapshot["skipped"], 1)
        force_snapshot = _task_store.snapshot(force_task_id)
        self.assertEqual(force_snapshot["success"], 1)
        self.assertEqual(force_snapshot["skipped"], 0)

        with Session(self.engine) as session:
            row = session.get(AccountModel, account_id)
        cache = row.get_extra()["chatgpt_last_payment_link"]
        self.assertEqual(cache["payment_source"], "long_link")
        self.assertEqual(cache["payment_link_format"], "long_link")
        self.assertEqual(cache["country"], "BR")
        self.assertEqual(cache["currency"], "BRL")

    def test_batch_payment_link_reuses_matching_profile_cache_unless_force_refresh(self):
        account_id = self._add_account(
            email="reuse-profile@example.com",
            cached={
                "url": "https://pay.example.test/cached-profile",
                "plan": "plus",
                "country": "BR",
                "currency": "BRL",
                "payment_link_format": "long_link",
                "payment_source": "long_link",
                "profile_hash": "profile-hash-brl",
                "link_type": "pix",
            },
        )
        cached_task_id = "task-batch-payment-reuse-profile"
        self._create_payment_link_task(cached_task_id, account_id, "reuse-profile@example.com")
        cached_client = _FakeLongLinkPaymentClient()

        with (
            patch(
                "services.chatgpt_core.long_link_payment_client.LongLinkPaymentClient.from_env",
                return_value=cached_client,
            ),
            patch("api.tasks._save_task_log"),
        ):
            _run_batch_payment_links(cached_task_id, [account_id])

        self.assertEqual(cached_client.events, [("profile", True)])
        cached_snapshot = _task_store.snapshot(cached_task_id)
        self.assertEqual(cached_snapshot["success"], 0)
        self.assertEqual(cached_snapshot["skipped"], 1)

        fresh_task_id = "task-batch-payment-force-refresh"
        self._create_payment_link_task(
            fresh_task_id,
            account_id,
            "reuse-profile@example.com",
            force_refresh=True,
        )
        fresh_client = _FakeLongLinkPaymentClient()
        with (
            patch(
                "services.chatgpt_core.long_link_payment_client.LongLinkPaymentClient.from_env",
                return_value=fresh_client,
            ),
            patch("api.tasks._save_task_log"),
        ):
            _run_batch_payment_links(fresh_task_id, [account_id])

        self.assertEqual(len(fresh_client.submissions), 1)
        self.assertEqual(_task_store.snapshot(fresh_task_id)["success"], 1)

    def test_batch_payment_link_mirrors_paypal_result_and_marks_remote_interruption(self):
        paypal_profile = {
            "profile_hash": "profile-hash-paypal",
            "link_type": "paypal",
            "country": "GB",
            "currency": "GBP",
            "effective_concurrency": 2,
            "profile": {},
        }
        paypal_task_id = "task-batch-payment-paypal-result"
        paypal_account_id = self._add_account(email="paypal-profile@example.com")
        self._create_payment_link_task(paypal_task_id, paypal_account_id, "paypal-profile@example.com")
        paypal_client = _FakeLongLinkPaymentClient(profile=paypal_profile)

        with (
            patch(
                "services.chatgpt_core.long_link_payment_client.LongLinkPaymentClient.from_env",
                return_value=paypal_client,
            ),
            patch("api.tasks._save_task_log"),
        ):
            _run_batch_payment_links(paypal_task_id, [paypal_account_id])

        with Session(self.engine) as session:
            paypal_account = session.get(AccountModel, paypal_account_id)
            paypal_history = session.exec(
                select(PaymentLinkGenerationModel).where(PaymentLinkGenerationModel.account_id == paypal_account_id)
            ).one()
        paypal_extra = paypal_account.get_extra()
        self.assertEqual(paypal_extra["chatgpt_last_payment_link"]["link_type"], "paypal")
        self.assertEqual(paypal_extra["chatgpt_paypal_url"]["paypal_url"], "https://pay.example.test/1")
        self.assertEqual(paypal_history.status, "succeeded")
        self.assertEqual(paypal_history.link_type, "paypal")

        interrupted_task_id = "task-batch-payment-interrupted"
        interrupted_account_id = self._add_account(email="interrupted@example.com")
        self._create_payment_link_task(interrupted_task_id, interrupted_account_id, "interrupted@example.com")
        interrupted_client = _FakeLongLinkPaymentClient(submitted_status="interrupted")
        with (
            patch(
                "services.chatgpt_core.long_link_payment_client.LongLinkPaymentClient.from_env",
                return_value=interrupted_client,
            ),
            patch("api.tasks._save_task_log"),
        ):
            _run_batch_payment_links(interrupted_task_id, [interrupted_account_id])

        interrupted_snapshot = _task_store.snapshot(interrupted_task_id)
        self.assertEqual(interrupted_client.events, [("profile", True), ("submit", 1)])
        self.assertEqual(interrupted_snapshot["status"], "interrupted")
        self.assertEqual(interrupted_snapshot["meta"]["payment_link_aggregate_status"], "remote_interrupted")
        with Session(self.engine) as session:
            interrupted_history = session.exec(
                select(PaymentLinkGenerationModel).where(PaymentLinkGenerationModel.account_id == interrupted_account_id)
            ).one()
        self.assertEqual(interrupted_history.status, "interrupted")

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
        self.assertIn("不能生成支付链接", result["skipped_items"][0]["reason"])
        thread_cls.assert_called_once()

    def test_enqueue_batch_payment_link_task_ids_are_unique_within_same_millisecond(self):
        account_id = self._add_account(email="same-millisecond@example.com")
        req = BatchPaymentLinkTaskRequest(account_ids=[account_id])

        with (
            patch("api.tasks.time.time", return_value=1_784_000_000.123),
            patch("api.tasks.threading.Thread") as thread_cls,
        ):
            first = enqueue_batch_payment_link_task(req)
            second = enqueue_batch_payment_link_task(req)

        self.assertNotEqual(first["task_id"], second["task_id"])
        self.assertTrue(first["task_id"].startswith("task_1784000000123_"))
        self.assertTrue(second["task_id"].startswith("task_1784000000123_"))
        self.assertEqual(thread_cls.call_count, 2)


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

if __name__ == "__main__":
    unittest.main()
