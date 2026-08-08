import tempfile
import time
import unittest
import threading
from unittest.mock import patch

import api.actions as api_actions
import api.tasks as tasks_api
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy.pool import NullPool
from sqlmodel import Session, SQLModel, create_engine, select
from core.task_runtime import StopTaskRequested
from services.chatgpt_core.registration_route_policy import (
    ExistingAccountLoginRouteBlocked,
    build_existing_account_login_route_event,
)
from api.tasks import (
    BatchPaymentLinkTaskRequest,
    BatchResumeSubscriptionAuthTaskRequest,
    PhoneBindingTestTaskRequest,
    RegisterTaskRequest,
    enqueue_batch_payment_link_task,
    enqueue_batch_resume_subscription_auth_task,
    enqueue_phone_binding_test_task,
    enqueue_register_task,
    _run_batch_payment_links,
    _create_task_record,
    _create_standalone_task_record,
    _build_effective_register_extra,
    _is_fatal_registration_infrastructure_error,
    _prepare_register_request,
    _run_batch_resume_subscription_auth,
    _run_phone_binding_test,
    _run_register,
    _run_resume_subscription_auth,
    _task_store,
)
from core.db import AccountListStateModel, AccountModel, PaymentLinkGenerationModel, TaskLog
from core.base_mailbox import BaseMailbox, MailboxAccount
from core.base_platform import Account, AccountStatus, BasePlatform
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


class _FakeChatGPTBrowserRuntimeProfilePlatform(BasePlatform):
    name = "chatgpt"
    display_name = "ChatGPT"
    seen: list[dict] = []
    runtime_profile = {
        "browser_family": "camoufox",
        "device_id": "camoufox-runtime-device",
        "user_agent": "Mozilla/5.0 Firefox/135.0",
        "requested_executor": "headless",
        "effective_executor": "headless",
        "headless_reason": "requested:true",
    }

    def __init__(self, config=None, mailbox=None):
        super().__init__(config)
        self.mailbox = mailbox

    def register(self, email: str, password: str = None) -> Account:
        extra = dict(getattr(self.config, "extra", None) or {})
        type(self).seen.append(
            {
                "executor_type": getattr(self.config, "executor_type", ""),
                "seed_fingerprint": dict(extra.get("chatgpt_browser_fingerprint") or {}),
                "seed_signature": str(
                    extra.get("chatgpt_browser_fingerprint_signature") or ""
                ),
            }
        )
        runtime_profile = dict(type(self).runtime_profile)
        return Account(
            platform="chatgpt",
            email="browser-runtime@example.com",
            password=password or "pw",
            token="at-browser-runtime",
            extra={
                "chatgpt_browser_runtime_profile": runtime_profile,
                "chatgpt_registration_context": {
                    "requested_executor": "headless",
                    "effective_executor": "headless",
                    "registration_transport": "camoufox_browser",
                    "browser_runtime_profile": dict(runtime_profile),
                },
            },
        )

    def check_valid(self, account: Account) -> bool:
        return True


class _FakeChatGPTAuthPendingPlatform(BasePlatform):
    name = "chatgpt"
    display_name = "ChatGPT"

    def __init__(self, config=None, mailbox=None):
        super().__init__(config)
        self.mailbox = mailbox

    def register(self, email: str, password: str = None) -> Account:
        return Account(
            platform="chatgpt",
            email="pending@example.com",
            password=password or "pw",
            token="",
            status=AccountStatus.PENDING_PAYMENT,
            extra={
                "access_token": "",
                "refresh_token": "",
                "registered_auth_pending": True,
                "needs_auth_capture": True,
                "registration_full_auth_failed": True,
                "registration_full_auth_error": "browser web session missing",
                "requested_executor_type": "headless",
                "effective_executor_type": "headless",
                "chatgpt_registration_transport": "camoufox_browser",
                "chatgpt_mailbox_state": {
                    "provider": "icloud_hme",
                    "email": "pending@example.com",
                    "account": {
                        "account_id": "alias-pending",
                        "extra": {
                            "anonymous_id": "anon-pending",
                            "hme": "pending@example.com",
                        },
                    },
                },
            },
        )

    def check_valid(self, account: Account) -> bool:
        return False


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

    def test_prepare_register_treats_proxy_without_mode_as_specified(self):
        req = RegisterTaskRequest(
            platform="chatgpt",
            count=2,
            proxy="http://proxy.example:8080",
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


class RegisterRequestRuntimeControlTests(unittest.TestCase):
    def _prepare(self, **kwargs):
        config = kwargs.pop("config", {})
        req = RegisterTaskRequest(
            platform="chatgpt",
            count=kwargs.pop("count", 5),
            extra=kwargs.pop("extra", {"mail_provider": "fake"}),
            **kwargs,
        )
        with patch("core.config_store.config_store.get_all", return_value=dict(config)):
            return _prepare_register_request(req)

    def test_chatgpt_protocol_uses_mode_defaults_when_request_omits_controls(self):
        prepared = self._prepare(proxy_mode="dynamic")

        self.assertEqual(prepared.concurrency, 2)
        self.assertEqual(prepared.register_delay_seconds, 15)
        self.assertEqual(prepared.register_delay_max_seconds, 30)
        self.assertTrue(prepared.extra["chatgpt_register_unique_exit_ip_enabled"])
        self.assertEqual(
            prepared._register_control,
            {
                "executor_mode": "protocol",
                "requested_concurrency": None,
                "effective_concurrency": 2,
                "default_concurrency": 2,
                "concurrency_cap": 3,
                "concurrency_reason": "",
                "requested_delay_seconds": None,
                "requested_delay_max_seconds": None,
                "effective_delay_seconds": 15,
                "effective_delay_max_seconds": 30,
                "delay_source": "system_default",
            },
        )
        self.assertEqual(prepared._register_unique_exit_ip["policy"], "auto")
        self.assertEqual(prepared._register_unique_exit_ip["proxy_mode"], "dynamic")

    def test_chatgpt_browser_default_and_cap_are_two(self):
        prepared = self._prepare(
            executor_type="headless",
            concurrency=5,
            proxy_mode="pool",
        )

        self.assertEqual(prepared.concurrency, 2)
        self.assertEqual(prepared._register_control["requested_concurrency"], 5)
        self.assertEqual(prepared._register_control["effective_concurrency"], 2)
        self.assertEqual(prepared._register_control["concurrency_reason"], "browser_cap")
        self.assertFalse(prepared.extra["chatgpt_register_unique_exit_ip_enabled"])

    def test_chatgpt_browser_config_can_raise_task_concurrency_to_ten(self):
        prepared = self._prepare(
            count=10,
            executor_type="headless",
            concurrency=10,
            proxy_mode="pool",
            config={
                "chatgpt_register_browser_default_concurrency": "10",
                "chatgpt_register_browser_max_concurrency": "10",
                "chatgpt_register_delay_seconds": "0",
                "chatgpt_register_delay_max_seconds": "0",
            },
        )

        self.assertEqual(prepared.concurrency, 10)
        self.assertEqual(prepared._register_control["concurrency_cap"], 10)
        self.assertEqual(prepared._register_control["effective_concurrency"], 10)

    def test_explicit_zero_delay_range_remains_disabled(self):
        prepared = self._prepare(
            concurrency=5,
            register_delay_seconds=0,
            register_delay_max_seconds=0,
            proxy_mode="direct",
        )

        self.assertEqual(prepared.concurrency, 3)
        self.assertEqual(prepared.register_delay_seconds, 0)
        self.assertEqual(prepared.register_delay_max_seconds, 0)
        self.assertEqual(prepared._register_control["requested_concurrency"], 5)
        self.assertEqual(prepared._register_control["effective_concurrency"], 3)
        self.assertFalse(prepared.extra["chatgpt_register_unique_exit_ip_enabled"])

    def test_legacy_single_delay_field_keeps_fixed_delay_semantics(self):
        prepared = self._prepare(register_delay_seconds=7, proxy_mode="direct")

        self.assertEqual(prepared.register_delay_seconds, 7)
        self.assertEqual(prepared.register_delay_max_seconds, 7)
        self.assertEqual(prepared._register_control["delay_source"], "request_fixed_legacy")

    def test_explicit_zero_max_keeps_fixed_min_delay_semantics(self):
        prepared = self._prepare(
            register_delay_seconds=7,
            register_delay_max_seconds=0,
            proxy_mode="direct",
        )

        self.assertEqual(prepared._register_control["requested_delay_max_seconds"], 0)
        self.assertEqual(prepared.register_delay_seconds, 7)
        self.assertEqual(prepared.register_delay_max_seconds, 7)
        self.assertEqual(prepared._register_control["delay_source"], "request_fixed")

    def test_delay_range_rejects_inverted_or_non_finite_values(self):
        with self.assertRaises(HTTPException) as inverted:
            self._prepare(
                register_delay_seconds=30,
                register_delay_max_seconds=15,
            )
        self.assertEqual(inverted.exception.status_code, 400)
        self.assertIn("最大启动延时不能小于", str(inverted.exception.detail))

        with self.assertRaises(HTTPException) as non_finite:
            self._prepare(register_delay_seconds=float("inf"))
        self.assertEqual(non_finite.exception.status_code, 400)
        self.assertIn("有限数字", str(non_finite.exception.detail))

    def test_invalid_concurrency_is_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            self._prepare(concurrency=0)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("大于等于 1", str(ctx.exception.detail))

    def test_manual_email_and_phone_signup_remain_serial(self):
        manual = self._prepare(
            count=3,
            concurrency=3,
            email="manual@example.com",
            extra={"mail_provider": "manual_email_otp"},
        )
        phone = self._prepare(
            count=3,
            concurrency=3,
            extra={
                "mail_provider": "fake",
                "chatgpt_registration_entry": "phone_signup",
            },
        )

        self.assertEqual(manual.concurrency, 1)
        self.assertEqual(manual._register_control["concurrency_reason"], "manual_email_otp")
        self.assertEqual(phone.concurrency, 1)
        self.assertEqual(phone._register_control["concurrency_reason"], "phone_signup")

    def test_dynamic_auto_policy_respects_explicit_false_and_direct_is_safe(self):
        dynamic_disabled = self._prepare(
            proxy_mode="dynamic",
            extra={
                "mail_provider": "fake",
                "chatgpt_register_unique_exit_ip_enabled": False,
            },
        )
        direct_default = self._prepare(proxy_mode="direct")

        self.assertFalse(dynamic_disabled.extra["chatgpt_register_unique_exit_ip_enabled"])
        self.assertEqual(dynamic_disabled._register_unique_exit_ip["policy"], "off")
        self.assertFalse(direct_default.extra["chatgpt_register_unique_exit_ip_enabled"])
        self.assertEqual(direct_default._register_unique_exit_ip["policy"], "auto")

    def test_canonical_unique_exit_policy_wins_over_legacy_boolean(self):
        request_conflict = self._prepare(
            proxy_mode="dynamic",
            extra={
                "mail_provider": "fake",
                "chatgpt_register_unique_exit_ip_policy": "auto",
                "chatgpt_register_unique_exit_ip_enabled": False,
            },
        )
        config_conflict = self._prepare(
            proxy_mode="dynamic",
            config={
                "chatgpt_register_unique_exit_ip_policy": "auto",
                "chatgpt_register_unique_exit_ip_enabled": "false",
            },
        )
        request_off = self._prepare(
            proxy_mode="dynamic",
            extra={
                "mail_provider": "fake",
                "chatgpt_register_unique_exit_ip_policy": "off",
                "chatgpt_register_unique_exit_ip_enabled": True,
            },
        )

        self.assertEqual(request_conflict._register_unique_exit_ip["policy"], "auto")
        self.assertEqual(request_conflict._register_unique_exit_ip["source"], "request_policy")
        self.assertTrue(request_conflict.extra["chatgpt_register_unique_exit_ip_enabled"])
        self.assertEqual(config_conflict._register_unique_exit_ip["policy"], "auto")
        self.assertEqual(config_conflict._register_unique_exit_ip["source"], "config_policy")
        self.assertTrue(config_conflict.extra["chatgpt_register_unique_exit_ip_enabled"])
        self.assertEqual(request_off._register_unique_exit_ip["policy"], "off")
        self.assertFalse(request_off.extra["chatgpt_register_unique_exit_ip_enabled"])

    def test_invalid_canonical_unique_exit_policy_is_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            self._prepare(
                proxy_mode="dynamic",
                extra={
                    "mail_provider": "fake",
                    "chatgpt_register_unique_exit_ip_policy": "offf",
                },
            )

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("auto、required 或 off", str(ctx.exception.detail))

    def test_explicit_false_failover_wins_over_global_true(self):
        with self.assertRaises(HTTPException) as ctx:
            self._prepare(
                count=2,
                proxy_mode="specified",
                proxy="http://proxy.example:8080",
                proxy_failover=False,
                config={"task_proxy_failover": "true"},
                extra={
                    "mail_provider": "fake",
                    "chatgpt_register_unique_exit_ip_policy": "required",
                },
            )

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("单个指定代理无法满足多个账号独立出口 IP", str(ctx.exception.detail))

    def test_proxy_aliases_and_global_mode_follow_core_resolver_contract(self):
        for prefix in ("register", "probe"):
            with self.subTest(prefix=prefix):
                prepared = self._prepare(
                    count=2,
                    extra={
                        "mail_provider": "fake",
                        f"{prefix}_proxy": f"http://{prefix}-proxy.example:8080",
                        f"{prefix}_proxy_mode": "specified",
                        f"{prefix}_proxy_failover": True,
                        "chatgpt_register_unique_exit_ip_policy": "required",
                    },
                )

                self.assertEqual(prepared.proxy_mode, "specified")
                self.assertEqual(
                    prepared.proxy,
                    f"http://{prefix}-proxy.example:8080",
                )
                self.assertTrue(prepared.proxy_failover)
                self.assertEqual(prepared._register_unique_exit_ip["proxy_mode"], "specified")

        inherited_global = self._prepare(
            proxy_mode="global",
            config={
                "task_proxy_mode": "dynamic",
                "task_proxy_failover": "true",
                "dynamic_proxy_template": "http://dynamic.example:8080",
                "dynamic_proxy_default_country": "us",
                "chatgpt_register_unique_exit_ip_policy": "auto",
            },
        )
        explicit_proxy = self._prepare(
            count=1,
            proxy_mode="global",
            proxy="http://explicit-proxy.example:8080",
            extra={
                "mail_provider": "fake",
                "chatgpt_register_unique_exit_ip_policy": "required",
            },
        )

        self.assertEqual(inherited_global.proxy_mode, "dynamic")
        self.assertEqual(inherited_global.proxy, "http://dynamic.example:8080")
        self.assertEqual(inherited_global.proxy_country_code, "US")
        self.assertTrue(inherited_global.proxy_failover)
        self.assertTrue(inherited_global.extra["chatgpt_register_unique_exit_ip_enabled"])
        self.assertEqual(explicit_proxy.proxy_mode, "specified")
        self.assertEqual(explicit_proxy._register_unique_exit_ip["proxy_mode"], "specified")

    def test_config_values_are_clamped_to_the_hard_mode_cap(self):
        prepared = self._prepare(
            proxy_mode="direct",
            config={
                "chatgpt_register_protocol_default_concurrency": "4",
                "chatgpt_register_protocol_max_concurrency": "4",
                "chatgpt_register_delay_seconds": "5",
                "chatgpt_register_delay_max_seconds": "9",
            },
        )

        self.assertEqual(prepared.concurrency, 3)
        self.assertEqual(prepared.register_delay_seconds, 5)
        self.assertEqual(prepared.register_delay_max_seconds, 9)

    def test_non_chatgpt_request_keeps_legacy_defaults(self):
        request = RegisterTaskRequest(platform="example", count=5)
        with patch("core.config_store.config_store.get_all", return_value={}):
            prepared = _prepare_register_request(request)

        self.assertEqual(prepared.concurrency, 1)
        self.assertEqual(prepared.register_delay_seconds, 0)
        self.assertEqual(prepared.register_delay_max_seconds, 0)
        self.assertNotIn("chatgpt_register_unique_exit_ip_enabled", prepared.extra)

    def test_enqueue_meta_keeps_requested_and_frozen_effective_controls(self):
        request = RegisterTaskRequest(
            platform="chatgpt",
            count=5,
            concurrency=5,
            executor_type="protocol",
            proxy_mode="direct",
            register_delay_seconds=0,
            register_delay_max_seconds=0,
            extra={"mail_provider": "fake"},
        )
        with (
            patch("core.config_store.config_store.get_all", return_value={}),
            patch("api.tasks._save_task_log"),
        ):
            task_id = enqueue_register_task(
                request,
                background_tasks=BackgroundTasks(),
            )

        snapshot = _task_store.snapshot(task_id)
        controls = snapshot["meta"]["registration_control"]
        self.assertEqual(controls["requested_concurrency"], 5)
        self.assertEqual(controls["effective_concurrency"], 3)
        self.assertEqual(controls["requested_delay_seconds"], 0)
        self.assertEqual(controls["effective_delay_seconds"], 0)

    def test_register_task_ids_are_unique_within_same_millisecond(self):
        request = RegisterTaskRequest(
            platform="chatgpt",
            count=1,
            concurrency=1,
            proxy_mode="direct",
            register_delay_seconds=0,
            register_delay_max_seconds=0,
            extra={
                "mail_provider": "fake",
                "chatgpt_register_unique_exit_ip_policy": "off",
            },
        )
        with (
            patch("core.config_store.config_store.get_all", return_value={}),
            patch("api.tasks._save_task_log"),
            patch("api.tasks.time.time", return_value=1_784_000_000.123),
        ):
            first = enqueue_register_task(request, background_tasks=BackgroundTasks())
            second = enqueue_register_task(request, background_tasks=BackgroundTasks())

        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("task_1784000000123_"))
        self.assertTrue(second.startswith("task_1784000000123_"))


class RegisterTaskControlFlowTests(unittest.TestCase):
    def test_register_start_jitter_delays_second_attempt_not_first(self):
        task_id = "task-register-start-jitter"
        request = RegisterTaskRequest(
            platform="chatgpt",
            count=2,
            concurrency=1,
            register_delay_seconds=15,
            register_delay_max_seconds=30,
            proxy_mode="direct",
            extra={"mail_provider": "fake"},
        )
        _create_task_record(task_id, request, "manual", None)
        monotonic_clock = [100.0]
        slept = []

        def fake_sleep(seconds):
            slept.append(float(seconds))
            monotonic_clock[0] += float(seconds)

        with (
            patch("services.chatgpt_core.ChatGPTPlatform", _FakePlatform),
            patch("core.base_mailbox.create_mailbox", return_value=_FakeMailbox()),
            patch("core.db.save_account", side_effect=lambda account: account),
            patch("api.tasks._auto_upload_integrations"),
            patch("api.tasks._save_task_log"),
            patch("api.tasks.time.monotonic", side_effect=lambda: monotonic_clock[0]),
            patch("api.tasks.time.sleep", side_effect=fake_sleep),
            patch("api.tasks.random.uniform", return_value=22.0),
        ):
            _run_register(task_id, request)

        snapshot = _task_store.snapshot(task_id)
        delay_logs = [line for line in snapshot["logs"] if "启动前延迟" in line]
        self.assertEqual(snapshot["success"], 2)
        self.assertEqual(len(delay_logs), 1)
        self.assertIn("22", delay_logs[0])
        self.assertAlmostEqual(sum(slept), 22.0)

    def test_stop_after_current_interrupts_worker_waiting_in_start_jitter(self):
        task_id = "task-register-jitter-after-current"
        first_started = threading.Event()
        release_first = threading.Event()
        calls = []
        slept = []

        class WaitingPlatform(_FakePlatform):
            def register(self, email=None, password=None):
                calls.append(len(calls) + 1)
                first_started.set()
                release_first.wait(timeout=2)
                return Account(
                    platform="chatgpt",
                    email="jitter-first@example.com",
                    password=password or "pw",
                    token="at-jitter-first",
                    extra={},
                )

        request = RegisterTaskRequest(
            platform="chatgpt",
            count=2,
            concurrency=2,
            register_delay_seconds=15,
            register_delay_max_seconds=15,
            proxy_mode="direct",
            extra={"mail_provider": "fake"},
        )
        _create_task_record(task_id, request, "manual", None)
        control = _task_store.control_for(task_id)

        def stop_during_sleep(seconds):
            slept.append(float(seconds))
            if not first_started.wait(timeout=2):
                raise AssertionError("first registration attempt did not start")
            control.request_stop_after_current()
            release_first.set()

        with (
            patch("services.chatgpt_core.ChatGPTPlatform", WaitingPlatform),
            patch("core.base_mailbox.create_mailbox", return_value=_FakeMailbox()),
            patch("core.db.save_account", side_effect=lambda account: account),
            patch("api.tasks._auto_upload_integrations"),
            patch("api.tasks._save_task_log"),
            patch("api.tasks.time.sleep", side_effect=stop_during_sleep),
        ):
            _run_register(task_id, request)

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(calls, [1])
        self.assertEqual(len(slept), 1)
        self.assertLessEqual(sum(slept), 0.25)
        self.assertEqual(snapshot["success"], 1)
        self.assertEqual(snapshot["status"], "stopped")

    def test_runner_uses_frozen_concurrency_cap_after_config_changes(self):
        task_id = "task-register-frozen-cap"
        request = RegisterTaskRequest(
            platform="chatgpt",
            count=2,
            concurrency=3,
            register_delay_seconds=0,
            register_delay_max_seconds=0,
            proxy_mode="direct",
            extra={
                "mail_provider": "fake",
                "chatgpt_register_unique_exit_ip_policy": "off",
            },
        )
        with patch(
            "core.config_store.config_store.get_all",
            return_value={"chatgpt_register_protocol_max_concurrency": "3"},
        ):
            prepared = _prepare_register_request(request)
        _create_task_record(task_id, prepared, "manual", None)
        _FakeChatGPTProxyFingerprintPlatform.seen = []

        with (
            patch(
                "core.config_store.config_store.get_all",
                return_value={
                    "chatgpt_register_protocol_default_concurrency": "1",
                    "chatgpt_register_protocol_max_concurrency": "1",
                },
            ),
            patch("services.chatgpt_core.ChatGPTPlatform", _FakeChatGPTProxyFingerprintPlatform),
            patch("core.base_mailbox.create_mailbox", return_value=_FakeMailbox()),
            patch("core.db.save_account", side_effect=lambda account: account),
            patch("api.tasks._auto_upload_integrations"),
            patch("api.tasks._save_task_log"),
        ):
            _run_register(task_id, prepared)

        snapshot = _task_store.snapshot(task_id)
        controls = snapshot["meta"]["registration_control"]
        self.assertEqual(snapshot["success"], 2)
        self.assertEqual(controls["concurrency_cap"], 3)
        self.assertEqual(controls["effective_concurrency"], 2)

    def test_sentinel_browser_unavailable_is_fatal_for_registration_batch(self):
        self.assertTrue(
            _is_fatal_registration_infrastructure_error(
                "注册流失败: sentinel_browser_unavailable: oauth_create_account"
            )
        )
        self.assertTrue(
            _is_fatal_registration_infrastructure_error(
                "注册流失败: auth_browser_finalize_unavailable: create_account"
            )
        )
        self.assertTrue(
            _is_fatal_registration_infrastructure_error(
                "注册流失败: browser_registration_unavailable: worker crashed"
            )
        )
        self.assertTrue(
            _is_fatal_registration_infrastructure_error(
                "注册流失败: browser_registration_hard_timeout"
            )
        )
        self.assertFalse(
            _is_fatal_registration_infrastructure_error(
                "注册流失败: HTTP 400: registration_disallowed"
            )
        )

    def test_fatal_sentinel_error_stops_scheduling_new_registration_attempts(self):
        calls = []

        class FatalSentinelPlatform(_FakePlatform):
            def register(self, email=None, password=None):
                calls.append((email, password))
                raise RuntimeError(
                    "sentinel_browser_unavailable: oauth_create_account"
                )

        task_id = "task-fatal-sentinel"
        req = RegisterTaskRequest(
            platform="chatgpt",
            count=10,
            concurrency=1,
            proxy_mode="direct",
            extra={"mail_provider": "fake"},
        )
        _create_task_record(task_id, req, "manual", None)

        with (
            patch("services.chatgpt_core.ChatGPTPlatform", FatalSentinelPlatform),
            patch("core.proxy_utils.resolve_task_proxy_candidates", return_value=[("", None, "direct")]),
            patch("core.base_mailbox.create_mailbox", return_value=_FakeMailbox()),
            patch("api.tasks._save_task_log"),
        ):
            _run_register(task_id, req)

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["status"], "failed")
        self.assertEqual(len(calls), 1)
        self.assertIn("sentinel_browser_unavailable", snapshot["error"])

    def test_browser_proxy_failure_does_not_rebuild_identity_on_next_candidate(self):
        calls = []

        class BrowserProxyFailurePlatform(_FakePlatform):
            def register(self, email=None, password=None):
                calls.append(str(getattr(self.config, "proxy", "") or ""))
                if len(calls) == 1:
                    raise RuntimeError("proxy connection timed out")
                return Account(
                    platform="chatgpt",
                    email="replacement-must-not-run@example.com",
                    password=password or "pw",
                    token="at-replacement-must-not-run",
                    extra={},
                )

        task_id = "task-browser-proxy-identity-boundary"
        req = RegisterTaskRequest(
            platform="chatgpt",
            count=1,
            concurrency=1,
            executor_type="headless",
            proxy_mode="dynamic",
            proxy_failover=True,
            extra={"mail_provider": "fake"},
        )
        _create_task_record(task_id, req, "manual", None)
        candidates = [
            ("http://proxy-a.local:8080", None, "dynamic"),
            ("http://proxy-b.local:8080", None, "dynamic"),
        ]

        with (
            patch("services.chatgpt_core.ChatGPTPlatform", BrowserProxyFailurePlatform),
            patch(
                "core.proxy_utils.resolve_task_proxy_candidates",
                return_value=candidates,
            ),
            patch(
                "core.base_mailbox.create_mailbox",
                side_effect=lambda **_kwargs: _FakeMailbox(),
            ) as create_mailbox,
            patch("api.tasks._save_task_log"),
        ):
            _run_register(task_id, req)

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["status"], "failed")
        self.assertEqual(calls, ["http://proxy-a.local:8080"])
        self.assertEqual(create_mailbox.call_count, 1)
        self.assertFalse(
            any(
                "same_attempt_proxy_failover=disabled" in line
                and "proxy connection timed out" in line
                for line in snapshot["logs"]
            )
        )
        self.assertTrue(
            any(
                "[1/1]" in line
                and "[步骤09/09 完成] 失败" in line
                and "占用目标=是" in line
                and "补位=否" in line
                for line in snapshot["logs"]
            )
        )
        self.assertFalse(
            any("保留当前尝试重试下一个代理" in line for line in snapshot["logs"])
        )
        self.assertTrue(
            any(
                "确定性=未知" in line and "占用目标=是" in line
                for line in snapshot["logs"]
            )
        )

    def test_browser_existing_account_skip_does_not_consume_slot_and_backfills(self):
        calls = []

        class BrowserExistingThenSuccessPlatform(_FakePlatform):
            def register(self, email=None, password=None):
                calls.append(len(calls) + 1)
                if len(calls) == 1:
                    route_event = build_existing_account_login_route_event(
                        email="existing@example.com",
                        reason="account already exists",
                        stage="after_email",
                        enabled=False,
                        routed=False,
                        blocked=True,
                        action="skip_save",
                        source="browser_registration",
                        signal="login_password",
                        page_type="login_password",
                        deterministic=True,
                    )
                    raise ExistingAccountLoginRouteBlocked(
                        "existing@example.com",
                        "account already exists",
                        route_event,
                    )
                return Account(
                    platform="chatgpt",
                    email="replacement@example.com",
                    password=password or "pw",
                    token="at-replacement",
                    extra={},
                )

        task_id = "task-browser-existing-account-backfill"
        req = RegisterTaskRequest(
            platform="chatgpt",
            count=1,
            concurrency=1,
            executor_type="headless",
            proxy_mode="direct",
            extra={
                "mail_provider": "fake",
                "chatgpt_existing_account_login_route_enabled": False,
                "register_max_attempts": 2,
            },
        )
        _create_task_record(task_id, req, "manual", None)

        with (
            patch(
                "services.chatgpt_core.ChatGPTPlatform",
                BrowserExistingThenSuccessPlatform,
            ),
            patch("core.base_mailbox.create_mailbox", return_value=_FakeMailbox()),
            patch("core.db.save_account", side_effect=lambda account: account),
            patch("api.tasks.schedule_chatgpt_local_status_refresh_for_account_id"),
            patch("api.tasks._auto_upload_integrations"),
            patch("api.tasks._save_task_log"),
        ):
            _run_register(task_id, req)

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["success"], 1)
        self.assertEqual(snapshot["skipped"], 1)
        self.assertEqual(len(calls), 2)
        self.assertTrue(
            any(
                "[1/1]" in line
                and "[步骤09/09 完成] 跳过" in line
                and "原因码=existing_account" in line
                and "占用目标=否" in line
                and "补位=是" in line
                for line in snapshot["logs"]
            )
        )
        self.assertFalse(
            any(
                "[1/1]" in line and "占用目标=是" in line
                for line in snapshot["logs"]
            )
        )

    def test_browser_uncertain_failure_keeps_remaining_requested_identity_slots(self):
        calls = []

        class BrowserMixedResultPlatform(_FakePlatform):
            def register(self, email=None, password=None):
                calls.append(str(getattr(self.config, "proxy", "") or ""))
                if len(calls) == 1:
                    raise RuntimeError("proxy connection timed out")
                return Account(
                    platform="chatgpt",
                    email=f"browser-success-{len(calls)}@example.com",
                    password=password or "pw",
                    token=f"at-browser-success-{len(calls)}",
                    extra={},
                )

        task_id = "task-browser-uncertain-slot-budget"
        req = RegisterTaskRequest(
            platform="chatgpt",
            count=3,
            concurrency=2,
            executor_type="headless",
            proxy_mode="direct",
            extra={"mail_provider": "fake"},
        )
        _create_task_record(task_id, req, "manual", None)

        with (
            patch("services.chatgpt_core.ChatGPTPlatform", BrowserMixedResultPlatform),
            patch(
                "core.proxy_utils.resolve_task_proxy_candidates",
                return_value=[("", None, "direct")],
            ),
            patch(
                "core.base_mailbox.create_mailbox",
                side_effect=lambda **_kwargs: _FakeMailbox(),
            ) as create_mailbox,
            patch("core.db.save_account", side_effect=lambda account: account),
            patch("api.tasks.schedule_chatgpt_local_status_refresh_for_account_id"),
            patch("api.tasks._auto_upload_integrations"),
            patch("api.tasks._save_task_log"),
        ):
            _run_register(task_id, req)

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["status"], "failed")
        self.assertEqual(snapshot["success"], 2)
        self.assertEqual(len(calls), 3)
        self.assertEqual(create_mailbox.call_count, 3)

    def test_browser_helper_early_failure_does_not_consume_slot_and_backfills(self):
        calls = []

        class BrowserEarlyFailureThenSuccessPlatform(_FakePlatform):
            def register(self, email=None, password=None):
                calls.append(len(calls) + 1)
                if len(calls) == 1:
                    failure = RuntimeError("Page.goto: Timeout 30000ms exceeded")
                    failure.registration_metadata = {
                        "mailbox_finalize_outcome": "early_failure",
                    }
                    raise failure
                return Account(
                    platform="chatgpt",
                    email="replacement-after-early-failure@example.com",
                    password=password or "pw",
                    token="at-replacement",
                    extra={},
                )

        task_id = "task-browser-helper-early-failure-backfill"
        req = RegisterTaskRequest(
            platform="chatgpt",
            count=1,
            concurrency=1,
            executor_type="headless",
            proxy_mode="direct",
            extra={
                "mail_provider": "fake",
                "register_max_attempts": 2,
            },
        )
        _create_task_record(task_id, req, "manual", None)

        with (
            patch(
                "services.chatgpt_core.ChatGPTPlatform",
                BrowserEarlyFailureThenSuccessPlatform,
            ),
            patch(
                "core.proxy_utils.resolve_task_proxy_candidates",
                return_value=[("", None, "direct")],
            ),
            patch(
                "core.base_mailbox.create_mailbox",
                side_effect=lambda **_kwargs: _FakeMailbox(),
            ),
            patch("core.db.save_account", side_effect=lambda account: account),
            patch("api.tasks.schedule_chatgpt_local_status_refresh_for_account_id"),
            patch("api.tasks._auto_upload_integrations"),
            patch("api.tasks._save_task_log"),
        ):
            _run_register(task_id, req)

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["success"], 1)
        self.assertEqual(calls, [1, 2])
        self.assertTrue(
            any(
                "原因码=registration_early_failure" in line
                and "邮箱回写=early_failure" in line
                and "占用目标=否" in line
                and "补位=是" in line
                for line in snapshot["logs"]
            )
        )

    def test_browser_failure_before_register_can_fill_the_same_target_slot(self):
        calls = []

        class BrowserPreRegisterRecoveryPlatform(_FakePlatform):
            def register(self, email=None, password=None):
                calls.append(str(getattr(self.config, "proxy", "") or ""))
                return Account(
                    platform="chatgpt",
                    email="browser-recovered@example.com",
                    password=password or "pw",
                    token="at-browser-recovered",
                    extra={},
                )

        task_id = "task-browser-pre-register-recovery"
        req = RegisterTaskRequest(
            platform="chatgpt",
            count=1,
            concurrency=1,
            executor_type="headed",
            proxy_mode="direct",
            extra={"mail_provider": "fake"},
        )
        _create_task_record(task_id, req, "manual", None)

        with (
            patch(
                "services.chatgpt_core.ChatGPTPlatform",
                BrowserPreRegisterRecoveryPlatform,
            ),
            patch(
                "core.proxy_utils.resolve_task_proxy_candidates",
                return_value=[("", None, "direct")],
            ),
            patch(
                "core.base_mailbox.create_mailbox",
                side_effect=[RuntimeError("mailbox unavailable"), _FakeMailbox()],
            ) as create_mailbox,
            patch("core.db.save_account", side_effect=lambda account: account),
            patch("api.tasks.schedule_chatgpt_local_status_refresh_for_account_id"),
            patch("api.tasks._auto_upload_integrations"),
            patch("api.tasks._save_task_log"),
        ):
            _run_register(task_id, req)

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["success"], 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(create_mailbox.call_count, 2)

    def test_protocol_proxy_failure_can_continue_with_next_candidate(self):
        calls = []

        class ProtocolProxyFailoverPlatform(_FakePlatform):
            def register(self, email=None, password=None):
                proxy = str(getattr(self.config, "proxy", "") or "")
                calls.append(proxy)
                if len(calls) == 1:
                    raise RuntimeError("proxy connection timed out")
                return Account(
                    platform="chatgpt",
                    email="protocol-success@example.com",
                    password=password or "pw",
                    token="at-protocol-success",
                    extra={},
                )

        task_id = "task-protocol-proxy-failover"
        req = RegisterTaskRequest(
            platform="chatgpt",
            count=1,
            concurrency=1,
            executor_type="protocol",
            proxy_mode="dynamic",
            proxy_failover=True,
            extra={"mail_provider": "fake", "register_max_attempts": 1},
        )
        _create_task_record(task_id, req, "manual", None)
        candidates = [
            ("http://proxy-a.local:8080", None, "dynamic"),
            ("http://proxy-b.local:8080", None, "dynamic"),
        ]

        with (
            patch("services.chatgpt_core.ChatGPTPlatform", ProtocolProxyFailoverPlatform),
            patch(
                "core.proxy_utils.resolve_task_proxy_candidates",
                return_value=candidates,
            ),
            patch(
                "core.base_mailbox.create_mailbox",
                side_effect=lambda **_kwargs: _FakeMailbox(),
            ) as create_mailbox,
            patch("core.db.save_account", side_effect=lambda account: account),
            patch("api.tasks.schedule_chatgpt_local_status_refresh_for_account_id"),
            patch("api.tasks._auto_upload_integrations"),
            patch("api.tasks._save_task_log"),
        ):
            _run_register(task_id, req)

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["success"], 1)
        self.assertEqual(
            calls,
            ["http://proxy-a.local:8080", "http://proxy-b.local:8080"],
        )
        self.assertEqual(create_mailbox.call_count, 2)
        self.assertTrue(
            any("保留当前尝试重试下一个代理" in line for line in snapshot["logs"])
        )
        self.assertFalse(
            any("浏览器注册链路启动后失败" in line for line in snapshot["logs"])
        )

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
        headers = [
            line
            for line in logs
            if ("[1/2]" in line or "[2/2]" in line) and "[步骤01/09 准备] 开始" in line
        ]

        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["success"], 2)
        self.assertEqual(len(headers), 2)
        self.assertIn("[1/2]", headers[0])
        self.assertIn("目标=2", headers[0])
        self.assertIn("已成功=0", headers[0])
        self.assertIn("[2/2]", headers[1])
        self.assertIn("目标=2", headers[1])
        self.assertIn("已成功=1", headers[1])
        first_success_index = next(
            index
            for index, line in enumerate(logs)
            if "[1/2]" in line
            and "[步骤09/09 完成] 成功" in line
            and "原因码=success" in line
        )
        separator_index = logs.index("", first_success_index + 1)
        second_header_index = logs.index(headers[1])
        self.assertLess(first_success_index, separator_index)
        self.assertLess(separator_index, second_header_index)
        self.assertEqual(logs[second_header_index - 1], "")
        self.assertFalse(any("开始第 " in line and "目标成功数" in line for line in logs))

    def test_success_slot_does_not_advance_after_failure(self):
        calls = []

        class FailThenSuccessPlatform(_FakePlatform):
            def register(self, email=None, password=None):
                calls.append(len(calls) + 1)
                if len(calls) == 1:
                    raise RuntimeError("first registration failed")
                return Account(
                    platform="chatgpt",
                    email="replacement-success@example.com",
                    password=password or "pw",
                    token="at-success",
                    extra={},
                )

        task_id = "task-register-success-slot-after-failure"
        req = RegisterTaskRequest(
            platform="chatgpt",
            count=1,
            concurrency=1,
            executor_type="protocol",
            proxy_mode="direct",
            extra={"mail_provider": "fake", "register_max_attempts": 2},
        )
        _create_task_record(task_id, req, "manual", None)

        with (
            patch("services.chatgpt_core.ChatGPTPlatform", FailThenSuccessPlatform),
            patch("core.base_mailbox.create_mailbox", return_value=_FakeMailbox()),
            patch("core.db.save_account", side_effect=lambda account: account),
            patch("api.tasks._auto_upload_integrations"),
            patch("api.tasks._save_task_log"),
        ):
            _run_register(task_id, req)

        snapshot = _task_store.snapshot(task_id)
        result_lines = [line for line in snapshot["logs"] if "[步骤09/09 完成]" in line]
        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["success"], 1)
        self.assertEqual(calls, [1, 2])
        self.assertTrue(any("[1/1]" in line and "失败" in line for line in result_lines))
        self.assertTrue(any("[1/1]" in line and "成功" in line and "原因码=success" in line for line in result_lines))
        self.assertFalse(any("[2/1]" in line for line in result_lines))

    def test_concurrent_attempts_keep_startup_success_slot_snapshot(self):
        barrier = threading.Barrier(2)

        class ConcurrentSuccessPlatform(_FakePlatform):
            def register(self, email=None, password=None):
                barrier.wait(timeout=5)
                index = threading.get_ident()
                return Account(
                    platform="chatgpt",
                    email=f"concurrent-{index}@example.com",
                    password=password or "pw",
                    token=f"at-{index}",
                    extra={},
                )

        task_id = "task-register-success-slot-concurrent"
        req = RegisterTaskRequest(
            platform="chatgpt",
            count=2,
            concurrency=2,
            executor_type="protocol",
            proxy_mode="direct",
            extra={"mail_provider": "fake"},
        )
        _create_task_record(task_id, req, "manual", None)

        with (
            patch("services.chatgpt_core.ChatGPTPlatform", ConcurrentSuccessPlatform),
            patch("core.base_mailbox.create_mailbox", return_value=_FakeMailbox()),
            patch("core.db.save_account", side_effect=lambda account: account),
            patch("api.tasks._auto_upload_integrations"),
            patch("api.tasks._save_task_log"),
        ):
            _run_register(task_id, req)

        snapshot = _task_store.snapshot(task_id)
        started = [line for line in snapshot["logs"] if "[步骤01/09 准备] 开始" in line]
        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["success"], 2)
        self.assertEqual(len(snapshot["meta"]["registered_accounts"]), 2)
        # Both workers are claimed before either success is consumed, so the
        # concurrent batch shares the same next-success slot.  A dispatcher
        # may briefly submit one refill while the second completed future is
        # still being drained; that later worker correctly receives slot 2.
        self.assertGreaterEqual(len(started), 2)
        self.assertTrue(all("[1/2]" in line for line in started[:2]))
        self.assertTrue(all("[2/2]" in line for line in started[2:]))

    def test_registration_debug_stream_keeps_http_transactions_only(self):
        class DebugEmitterPlatform(_FakePlatform):
            def register(self, email=None, password=None):
                self._log_fn("状态机细节仅供诊断", "debug")
                self._log_fn(
                    "[HTTP] POST auth.openai.com/api/accounts/user/register -> 200 8ms",
                    "debug",
                )
                return Account(
                    platform="chatgpt",
                    email="debug-emitter@example.com",
                    password=password or "pw",
                    token="at-debug",
                    extra={},
                )

        task_id = "task-register-debug-http-only"
        req = RegisterTaskRequest(
            platform="chatgpt",
            count=1,
            concurrency=1,
            executor_type="protocol",
            proxy_mode="direct",
            extra={"mail_provider": "fake"},
        )
        _create_task_record(task_id, req, "manual", None)

        with (
            patch("services.chatgpt_core.ChatGPTPlatform", DebugEmitterPlatform),
            patch("core.base_mailbox.create_mailbox", return_value=_FakeMailbox()),
            patch("core.db.save_account", side_effect=lambda account: account),
            patch("api.tasks._auto_upload_integrations"),
            patch("api.tasks._save_task_log"),
        ):
            _run_register(task_id, req)

        logs = _task_store.snapshot(task_id)["logs"]
        self.assertFalse(any("状态机细节仅供诊断" in line for line in logs))
        self.assertTrue(any("[DEBUG][1/1]" in line and "[HTTP] POST" in line for line in logs))

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
                "dynamic_proxy_max_attempts": 100,
            },
        )
        _create_task_record(task_id, req, "manual", None)
        _FakeChatGPTProxyFingerprintPlatform.seen = []
        candidate_calls = []

        def fake_candidates(params=None, fallback_proxy=None, default_mode="direct", target="chatgpt"):
            candidate_calls.append(
                (
                    bool(params.get("proxy_failover")),
                    int(params.get("dynamic_proxy_max_attempts") or 0),
                )
            )
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
            patch(
                "services.proxy_scanner.probe_basic",
                side_effect=fake_probe,
            ) as probe_basic,
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
        self.assertGreaterEqual(len(candidate_calls), 3)
        self.assertTrue(all(value == (False, 1) for value in candidate_calls))
        self.assertGreaterEqual(probe_basic.call_count, 1)
        self.assertTrue(
            any("proxy-b" in str(call.args[0]) for call in probe_basic.call_args_list)
        )
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

    def test_register_task_never_reuses_an_ipv6_64_after_lease_release(self):
        task_id = "task-register-ipv6-network-dedup"
        req = RegisterTaskRequest(
            platform="chatgpt",
            count=2,
            concurrency=1,
            register_delay_seconds=0,
            register_delay_max_seconds=0,
            proxy_mode="dynamic",
            proxy_country_code="US",
            extra={
                "mail_provider": "fake",
                "chatgpt_register_unique_exit_ip_policy": "required",
                "chatgpt_register_unique_exit_ip_enabled": True,
                "chatgpt_register_unique_exit_ip_cooldown_seconds": 0,
                "register_max_attempts": 2,
            },
        )
        _create_task_record(task_id, req, "manual", None)
        _FakeChatGPTProxyFingerprintPlatform.seen = []
        candidate_call = [0]

        def fake_candidates(params=None, fallback_proxy=None, default_mode="direct", target="chatgpt"):
            candidate_call[0] += 1
            if candidate_call[0] == 1:
                return [("http://proxy-preflight.local:8080", None, "dynamic preflight")]
            if candidate_call[0] == 2:
                return [("http://proxy-v6-a.local:8080", None, "dynamic first")]
            return [
                ("http://proxy-v6-b.local:8080", None, "dynamic same-network"),
                ("http://proxy-v6-c.local:8080", None, "dynamic next-network"),
            ]

        def fake_probe(proxy_url, timeout_seconds=8):
            if "v6-a" in proxy_url:
                exit_ip = "2001:db8:abcd:12::1"
            elif "v6-b" in proxy_url:
                exit_ip = "2001:db8:abcd:12::ffff"
            elif "v6-c" in proxy_url:
                exit_ip = "2001:db8:abcd:13::1"
            else:
                exit_ip = "2001:db8:abcd:99::1"
            return {"ok": True, "exit_ip": exit_ip, "latency_ms": 1}

        with (
            patch("services.chatgpt_core.ChatGPTPlatform", _FakeChatGPTProxyFingerprintPlatform),
            patch("core.base_mailbox.create_mailbox", return_value=_FakeMailbox()),
            patch("core.proxy_utils.resolve_task_proxy_candidates", side_effect=fake_candidates),
            patch("services.proxy_scanner.probe_basic", side_effect=fake_probe),
            patch("core.db.save_account", side_effect=lambda account: account),
            patch("api.tasks._save_task_log"),
        ):
            _run_register(task_id, req)

        snapshot = _task_store.snapshot(task_id)
        seen_exit_ips = [item["exit_ip"] for item in _FakeChatGPTProxyFingerprintPlatform.seen]
        self.assertEqual(
            seen_exit_ips,
            ["2001:db8:abcd:12::1", "2001:db8:abcd:13::1"],
        )
        self.assertEqual(snapshot["success"], 2)
        unique_meta = dict(snapshot["meta"]["register_unique_exit_ip"])
        self.assertGreaterEqual(int(unique_meta.get("collision_count") or 0), 1)

    def test_browser_executor_uses_attempt_seed_without_persisting_protocol_fingerprint(self):
        task_id = "task-register-browser-runtime-profile"
        req = RegisterTaskRequest(
            platform="chatgpt",
            count=1,
            concurrency=1,
            executor_type="headless",
            proxy_mode="direct",
            extra={"mail_provider": "fake"},
        )
        _create_task_record(task_id, req, "manual", None)
        _FakeChatGPTBrowserRuntimeProfilePlatform.seen = []
        saved_accounts = []

        def fake_save_account(account):
            saved_accounts.append(account)
            return account

        with (
            patch(
                "services.chatgpt_core.ChatGPTPlatform",
                _FakeChatGPTBrowserRuntimeProfilePlatform,
            ),
            patch("core.base_mailbox.create_mailbox", return_value=_FakeMailbox()),
            patch("core.db.save_account", side_effect=fake_save_account),
            patch("api.tasks._auto_upload_integrations"),
            patch("api.tasks._save_task_log"),
        ):
            _run_register(task_id, req)

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["success"], 1)
        self.assertEqual(len(_FakeChatGPTBrowserRuntimeProfilePlatform.seen), 1)
        seen = _FakeChatGPTBrowserRuntimeProfilePlatform.seen[0]
        seed = seen["seed_fingerprint"]
        self.assertEqual(seen["executor_type"], "headless")
        self.assertTrue(seed.get("device_id"))
        self.assertIn("Chrome/", seed.get("user_agent", ""))
        self.assertTrue(str(seed.get("impersonate") or "").startswith("chrome"))
        self.assertTrue(seen["seed_signature"])

        self.assertEqual(len(saved_accounts), 1)
        saved_extra = dict(saved_accounts[0].extra or {})
        self.assertNotEqual(
            seed["device_id"],
            _FakeChatGPTBrowserRuntimeProfilePlatform.runtime_profile["device_id"],
        )
        self.assertNotIn("chatgpt_browser_fingerprint", saved_extra)
        self.assertNotIn("chatgpt_browser_fingerprint_signature", saved_extra)
        self.assertEqual(
            saved_extra["chatgpt_browser_runtime_profile"],
            _FakeChatGPTBrowserRuntimeProfilePlatform.runtime_profile,
        )
        self.assertEqual(
            saved_extra["chatgpt_registration_context"]["browser_runtime_profile"],
            _FakeChatGPTBrowserRuntimeProfilePlatform.runtime_profile,
        )

    def test_registered_auth_pending_is_saved_once_without_probe_or_upload(self):
        task_id = "task-register-auth-pending"
        req = RegisterTaskRequest(
            platform="chatgpt",
            count=1,
            concurrency=1,
            executor_type="headless",
            proxy_mode="direct",
            extra={"mail_provider": "fake"},
        )
        _create_task_record(task_id, req, "manual", None)
        saved_accounts = []

        def fake_save_account(account):
            account.id = 91
            saved_accounts.append(account)
            return account

        with (
            patch("services.chatgpt_core.ChatGPTPlatform", _FakeChatGPTAuthPendingPlatform),
            patch("core.base_mailbox.create_mailbox", return_value=_FakeMailbox()),
            patch("core.db.save_account", side_effect=fake_save_account),
            patch("api.tasks._auto_upload_integrations") as auto_upload,
            patch(
                "api.tasks.schedule_chatgpt_local_status_refresh_for_account_id"
            ) as schedule_refresh,
            patch("core.db.sync_icloud_hme_rerun_result") as sync_hme,
            patch("api.tasks._save_task_log") as save_task_log,
        ):
            _run_register(task_id, req)

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["success"], 1)
        self.assertEqual(len(saved_accounts), 1)
        self.assertEqual(saved_accounts[0].token, "")
        self.assertTrue(saved_accounts[0].extra["registered_auth_pending"])
        self.assertEqual(snapshot["meta"]["auth_pending_count"], 1)
        self.assertEqual(
            snapshot["meta"]["auth_pending_accounts"][0]["email"],
            "pending@example.com",
        )
        self.assertTrue(
            any(
                "[1/1]" in line
                and "[步骤08/09 保存与同步] 待补抓" in line
                and "阶段=auth_capture" in line
                and "结果=待补抓" in line
                for line in snapshot["logs"]
            )
        )
        self.assertTrue(
            any(
                "[1/1]" in line
                and "原因码=registered_auth_pending" in line
                for line in snapshot["logs"]
            )
        )
        auto_upload.assert_not_called()
        schedule_refresh.assert_not_called()
        sync_hme.assert_called_once()
        self.assertFalse(sync_hme.call_args.kwargs["access_token_saved"])
        self.assertEqual(
            sync_hme.call_args.kwargs["result_code"],
            "registered_auth_pending",
        )
        self.assertIn("registered_auth_pending", str(save_task_log.call_args_list))

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

    def test_effective_register_extra_forces_registration_only_for_legacy_rt_config(self):
        req = RegisterTaskRequest(
            platform="chatgpt",
            count=1,
            concurrency=1,
            extra={
                "mail_provider": "fake",
                "chatgpt_registration_mode": "refresh_token",
                "chatgpt_has_refresh_token_solution": True,
                "chatgpt_access_token_only_checkout_amount_check_enabled": True,
            },
        )

        with patch("core.config_store.config_store.get_all", return_value={}):
            extra = _build_effective_register_extra(req)

        self.assertEqual(extra["chatgpt_registration_mode"], "access_token_only")
        self.assertFalse(extra["chatgpt_has_refresh_token_solution"])
        self.assertFalse(extra["chatgpt_access_token_only_checkout_amount_check_enabled"])
        self.assertEqual(extra["chatgpt_registration_requested_mode"], "refresh_token")

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
        info_logs = [line for line in snapshot["logs"] if "[DEBUG]" not in line]
        debug_logs = [line for line in snapshot["logs"] if "[DEBUG]" in line]
        self.assertTrue(any("+15555550123" in line for line in info_logs))
        self.assertFalse(any("+15555550123" in line for line in debug_logs))
        self.assertTrue(any("+1555***0123" in line for line in debug_logs))

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
        info_logs = [line for line in snapshot["logs"] if "[DEBUG]" not in line]
        debug_logs = [line for line in snapshot["logs"] if "[DEBUG]" in line]
        self.assertTrue(any("+13333333333" in line for line in info_logs))
        self.assertFalse(any("+13333333333" in line for line in debug_logs))


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
        web_session: bool = False,
    ) -> int:
        extra = {"chatgpt_last_payment_link": dict(cached)} if isinstance(cached, dict) else {}
        if web_session:
            extra["cookies"] = "__Secure-next-auth.session-token=web-session"
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

    def test_remote_results_commit_before_task_log_writes_on_file_sqlite(self):
        """A task-log checkpoint must never run inside the account write transaction."""

        with tempfile.TemporaryDirectory() as tmpdir:
            file_engine = create_engine(
                f"sqlite:///{tmpdir}/payment-commit-order.db",
                connect_args={"check_same_thread": False, "timeout": 0.1},
                poolclass=NullPool,
            )
            SQLModel.metadata.create_all(file_engine)
            with Session(file_engine) as session:
                accounts = [
                    AccountModel(
                        platform="chatgpt",
                        email=f"commit-order-{index}@example.com",
                        password="pw",
                        token=f"access-token-{index}",
                    )
                    for index in range(20)
                ]
                session.add_all(accounts)
                session.flush()
                account_ids = [int(account.id or 0) for account in accounts]
                session.commit()

            task_id = "task-batch-payment-commit-order"
            _create_standalone_task_record(
                task_id,
                platform="chatgpt",
                source="batch_payment_link",
                total=len(account_ids),
                meta={
                    "account_ids": account_ids,
                    "emails": [f"commit-order-{index}@example.com" for index in range(20)],
                    "params": {},
                    "skip_existing": True,
                    "force_refresh": False,
                    "skipped_items": [],
                    "missing_ids": [],
                },
            )
            client = _FakeLongLinkPaymentClient()
            committed_log_count = 0

            def persist_result_log(log_task_id, message, *_args, **_kwargs):
                nonlocal committed_log_count
                if "[OK]" not in str(message):
                    return
                with Session(file_engine) as reader:
                    histories = reader.exec(
                        select(PaymentLinkGenerationModel).where(
                            PaymentLinkGenerationModel.status == "succeeded"
                        )
                    ).all()
                    states = reader.exec(
                        select(AccountListStateModel).where(
                            AccountListStateModel.account_id.in_(account_ids)
                        )
                    ).all()
                    saved_accounts = [reader.get(AccountModel, account_id) for account_id in account_ids]
                self.assertEqual(len(histories), len(account_ids))
                self.assertEqual(len(states), len(account_ids))
                self.assertTrue(all(bool(state.payment_link_generated) for state in states))
                self.assertTrue(
                    all("chatgpt_last_payment_link" in account.get_extra() for account in saved_accounts)
                )
                tasks_api._save_task_log(
                    "chatgpt",
                    None,
                    "running",
                    detail=tasks_api._build_task_log_detail(
                        log_task_id,
                        {"attempt_outcome": "payment_result_committed"},
                    ),
                )
                committed_log_count += 1

            started_at = time.monotonic()
            with (
                patch("api.tasks.engine", file_engine),
                patch.object(core_db, "engine", file_engine),
                patch(
                    "services.chatgpt_core.long_link_payment_client.LongLinkPaymentClient.from_env",
                    return_value=client,
                ),
                patch("api.tasks._log", side_effect=persist_result_log),
            ):
                _run_batch_payment_links(task_id, account_ids)
            elapsed = time.monotonic() - started_at

            self.assertEqual(committed_log_count, len(account_ids))
            self.assertLess(elapsed, 3.0)
            self.assertEqual(_task_store.snapshot(task_id)["status"], "done")
            with Session(file_engine) as session:
                task_log = session.exec(select(TaskLog).where(TaskLog.task_id == task_id)).one()
            self.assertEqual(task_log.status, "done")
            file_engine.dispose()

    def test_batch_payment_link_generates_local_short_link_and_persists_history(self):
        task_id = "task-batch-payment-local-short"
        account_id = self._add_account(email="short@example.com", web_session=True)
        short_params = {
            "plan": "plus",
            "country": "US",
            "currency": "USD",
            "payment_source": "chatgpt_hosted",
            "payment_link_format": "short_chatgpt",
        }
        self._create_payment_link_task(
            task_id,
            account_id,
            "short@example.com",
            params=short_params,
        )
        short_url = "https://chatgpt.com/checkout/openai_llc/cs_live_batch_short"

        with (
            patch(
                "services.chatgpt_core.payment.generate_plus_short_link",
                return_value=short_url,
            ) as short_generator,
            patch(
                "services.chatgpt_core.long_link_payment_client.LongLinkPaymentClient.from_env",
            ) as long_client,
            patch("api.tasks._save_task_log"),
        ):
            _run_batch_payment_links(task_id, [account_id])

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["success"], 1)
        self.assertEqual(snapshot["meta"]["payment_link_profile"]["payment_link_format"], "short_chatgpt")
        short_generator.assert_called_once()
        long_client.assert_not_called()

        with Session(self.engine) as session:
            account = session.get(AccountModel, account_id)
            generation = session.exec(select(PaymentLinkGenerationModel)).one()
        cache = account.get_extra()["chatgpt_last_payment_link"]
        self.assertEqual(account.cashier_url, short_url)
        self.assertEqual(cache["url"], short_url)
        self.assertEqual(cache["payment_source"], "chatgpt_hosted")
        self.assertEqual(cache["payment_link_format"], "short_chatgpt")
        self.assertTrue(cache["login_required"])
        self.assertEqual(generation.status, "succeeded")
        self.assertEqual(generation.url, short_url)
        self.assertEqual(generation.get_result()["payment_link_format"], "short_chatgpt")

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
