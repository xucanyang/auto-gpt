import unittest
from unittest import mock

from core.base_mailbox import MailboxAccount
from core.base_platform import RegisterConfig
from services.chatgpt_core.plugin import (
    ChatGPTPlatform,
    _generate_chatgpt_registration_password,
)


class _BlankMailbox:
    def get_email(self):
        return MailboxAccount(email="", account_id="blank-mailbox")

    def wait_for_code(self, *args, **kwargs):
        return "123456"


class _TrackingMailbox:
    def __init__(self):
        self.account = MailboxAccount(email="demo@example.com", account_id="tracked-mailbox")
        self.wait_call = None
        self.current_ids_calls = []

    def get_email(self):
        return self.account

    def get_current_ids(self, account):
        self.current_ids_calls.append(account)
        return {"mid-1"}

    def wait_for_code(self, *args, **kwargs):
        self.wait_call = (args, kwargs)
        return "123456"


class _ReuseThenCreateFailMailbox:
    def __init__(self):
        self.account = MailboxAccount(email="alive@example.com", account_id="alive-1")
        self.get_email_calls = 0

    def get_email(self):
        self.get_email_calls += 1
        if self.get_email_calls == 1:
            return self.account
        raise RuntimeError("mailbox create failed")

    def get_current_ids(self, account):
        return {"mid-old"} if account and account.email else set()

    def wait_for_code(self, *args, **kwargs):
        return "123456"


class _FakeAdapter:
    def run(self, context):
        context.email_service.create_email()
        raise AssertionError("create_email 应该先报错")


class _VerificationAdapter:
    def __init__(self):
        self.run_called = False

    def run(self, context):
        self.run_called = True
        context.email_service.create_email()
        code = context.email_service.get_verification_code(
            timeout=30,
            otp_sent_at=123.0,
            exclude_codes={"654321"},
        )
        self.last_code = code
        return mock.Mock(success=True)

    def build_account(self, result, fallback_password):
        return {"success": True, "password": fallback_password}


class _RetryCreateAdapter:
    def run(self, context):
        first = context.email_service.create_email()
        second = context.email_service.create_email()
        return mock.Mock(success=True, first=first, second=second)

    def build_account(self, result, fallback_password):
        return {
            "success": True,
            "password": fallback_password,
            "first": getattr(result, "first", {}),
            "second": getattr(result, "second", {}),
        }


class _CaptureContextAdapter:
    def __init__(self):
        self.context = None

    def run(self, context):
        self.context = context
        return mock.Mock(success=True)

    def build_account(self, result, fallback_password):
        return {"success": True, "password": fallback_password}


class _FailureMetadataAdapter:
    def run(self, context):
        return mock.Mock(
            success=False,
            error_message="Page.goto: Timeout 30000ms exceeded",
            metadata={"mailbox_finalize_outcome": "early_failure"},
        )


class _StateCaptureAdapter:
    def run(self, context):
        context.email_service.create_email()
        return mock.Mock(success=True, mailbox_state=context.email_service.export_state())

    def build_account(self, result, fallback_password):
        return {
            "success": True,
            "password": fallback_password,
            "mailbox_state": result.mailbox_state,
        }


class _HmeReadyStateMailbox:
    def __init__(self):
        self.account = MailboxAccount(
            email="alias@icloud.com",
            account_id="lease-123",
            extra={
                "provider": "hme_ready_api",
                "mode": "helper_ready_api",
                "source": "icloud-hide-email-helper",
                "lease_id": "lease-123",
                "checkout_id": "checkout-123",
                "hme": "alias@icloud.com",
                "forward_to": "forward@example.com",
                "forward_mailbox_id": "mailbox-123",
                "unrelated_runtime_dump": "x" * 100_000,
            },
        )

    def get_email(self):
        return self.account

    def get_current_ids(self, account):
        return {f"message-{index:04d}" for index in range(600)}

    def wait_for_code(self, *args, **kwargs):
        return "123456"


class ChatGPTPluginTests(unittest.TestCase):
    def setUp(self):
        self.default_proxy_patcher = mock.patch(
            "services.chatgpt_core.plugin.resolve_default_chatgpt_proxy",
            return_value="",
        )
        self.default_proxy = self.default_proxy_patcher.start()

    def tearDown(self):
        self.default_proxy_patcher.stop()

    def test_generated_registration_password_always_meets_openai_policy(self):
        allowed_specials = set(",._!@#")
        for requested_length in (0, 8, 12, 16, 32):
            for _ in range(50):
                password = _generate_chatgpt_registration_password(requested_length)
                self.assertEqual(len(password), max(requested_length, 12))
                self.assertTrue(any(char.islower() for char in password))
                self.assertTrue(any(char.isupper() for char in password))
                self.assertTrue(any(char.isdigit() for char in password))
                self.assertTrue(any(char in allowed_specials for char in password))

    def test_register_uses_policy_compliant_generated_password(self):
        platform = ChatGPTPlatform(
            config=RegisterConfig(extra={"chatgpt_registration_mode": "refresh_token"}),
            mailbox=_TrackingMailbox(),
        )
        adapter = _CaptureContextAdapter()
        generated = "OpenAI9_policy!"

        with mock.patch(
            "services.chatgpt_core.plugin._generate_chatgpt_registration_password",
            return_value=generated,
        ) as generate_password, mock.patch(
            "services.chatgpt_core.plugin.build_chatgpt_registration_mode_adapter",
            return_value=adapter,
        ):
            result = platform.register()

        self.assertTrue(result["success"])
        generate_password.assert_called_once_with()
        self.assertIsNotNone(adapter.context)
        self.assertEqual(adapter.context.password, generated)

    def test_register_preserves_explicit_password(self):
        platform = ChatGPTPlatform(
            config=RegisterConfig(extra={"chatgpt_registration_mode": "refresh_token"}),
            mailbox=_TrackingMailbox(),
        )
        adapter = _CaptureContextAdapter()

        with mock.patch(
            "services.chatgpt_core.plugin._generate_chatgpt_registration_password",
        ) as generate_password, mock.patch(
            "services.chatgpt_core.plugin.build_chatgpt_registration_mode_adapter",
            return_value=adapter,
        ):
            result = platform.register(password="Explicit9_password!")

        self.assertTrue(result["success"])
        generate_password.assert_not_called()
        self.assertIsNotNone(adapter.context)
        self.assertEqual(adapter.context.password, "Explicit9_password!")

    def test_custom_provider_rejects_blank_email(self):
        platform = ChatGPTPlatform(
            config=RegisterConfig(extra={"chatgpt_registration_mode": "refresh_token"}),
            mailbox=_BlankMailbox(),
        )

        with mock.patch(
            "services.chatgpt_core.plugin.build_chatgpt_registration_mode_adapter",
            return_value=_FakeAdapter(),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                platform.register()

        self.assertIn("custom_provider 返回空邮箱地址", str(ctx.exception))

    def test_custom_provider_uses_mailbox_baseline_for_verification_code(self):
        mailbox = _TrackingMailbox()
        platform = ChatGPTPlatform(
            config=RegisterConfig(extra={"chatgpt_registration_mode": "refresh_token"}),
            mailbox=mailbox,
        )
        adapter = _VerificationAdapter()

        with mock.patch(
            "services.chatgpt_core.plugin.build_chatgpt_registration_mode_adapter",
            return_value=adapter,
        ):
            result = platform.register()

        self.assertTrue(adapter.run_called)
        self.assertEqual(adapter.last_code, "123456")
        self.assertEqual(result["success"], True)
        self.assertEqual(mailbox.current_ids_calls, [mailbox.account])
        self.assertIsNotNone(mailbox.wait_call)
        _, kwargs = mailbox.wait_call
        self.assertEqual(kwargs.get("before_ids"), {"mid-1"})
        self.assertEqual(kwargs.get("otp_sent_at"), 123.0)
        self.assertEqual(kwargs.get("exclude_codes"), {"654321"})

    def test_custom_provider_prefers_configured_mailbox_timeout(self):
        mailbox = _TrackingMailbox()
        platform = ChatGPTPlatform(
            config=RegisterConfig(
                extra={
                    "chatgpt_registration_mode": "refresh_token",
                    "mailbox_otp_timeout_seconds": 90,
                }
            ),
            mailbox=mailbox,
        )
        adapter = _VerificationAdapter()

        with mock.patch(
            "services.chatgpt_core.plugin.build_chatgpt_registration_mode_adapter",
            return_value=adapter,
        ):
            platform.register()

        _, kwargs = mailbox.wait_call
        self.assertEqual(kwargs.get("timeout"), 90)

    def test_email_api_provider_does_not_shrink_state_machine_timeout(self):
        mailbox = _TrackingMailbox()
        platform = ChatGPTPlatform(
            config=RegisterConfig(
                extra={
                    "chatgpt_registration_mode": "refresh_token",
                    "mail_provider": "email_api",
                    "mailbox_otp_timeout_seconds": 20,
                    "email_otp_timeout_seconds": 20,
                }
            ),
            mailbox=mailbox,
        )
        adapter = _VerificationAdapter()

        with mock.patch(
            "services.chatgpt_core.plugin.build_chatgpt_registration_mode_adapter",
            return_value=adapter,
        ):
            platform.register()

        _, kwargs = mailbox.wait_call
        self.assertEqual(kwargs.get("timeout"), 30)

    def test_hme_ready_provider_does_not_shrink_state_machine_timeout(self):
        mailbox = _TrackingMailbox()
        platform = ChatGPTPlatform(
            config=RegisterConfig(
                extra={
                    "chatgpt_registration_mode": "refresh_token",
                    "mail_provider": "hme_ready_api",
                    "icloud_hme_mode": "helper_ready_api",
                    "mailbox_otp_timeout_seconds": 20,
                    "email_otp_timeout_seconds": 20,
                }
            ),
            mailbox=mailbox,
        )
        adapter = _VerificationAdapter()

        with mock.patch(
            "services.chatgpt_core.plugin.build_chatgpt_registration_mode_adapter",
            return_value=adapter,
        ):
            platform.register()

        _, kwargs = mailbox.wait_call
        self.assertEqual(kwargs.get("timeout"), 30)

    def test_hme_ready_state_export_never_copies_global_runtime_config(self):
        mailbox = _HmeReadyStateMailbox()
        platform = ChatGPTPlatform(
            config=RegisterConfig(
                extra={
                    "mail_provider": "hme_ready_api",
                    "icloud_hme_helper_api_url": "http://helper.internal",
                    "icloud_hme_helper_internal_key": "helper-secret",
                    "tempmail_api_url": "http://tempmail.internal",
                    "tempmail_api_key": "tempmail-secret",
                    "chatgpt_gopay_batch_tasks": "g" * 750_000,
                    "chatgpt_gopay_phone_pool": ["+10000000000"] * 10_000,
                    "idea_oaipay_pipeline_state": {"items": ["x"] * 10_000},
                }
            ),
            mailbox=mailbox,
        )

        with mock.patch(
            "services.chatgpt_core.plugin.build_chatgpt_registration_mode_adapter",
            return_value=_StateCaptureAdapter(),
        ):
            result = platform.register()

        state = result["mailbox_state"]
        self.assertEqual(state["schema_version"], 2)
        self.assertEqual(state["provider"], "hme_ready_api")
        self.assertEqual(state["account"]["extra"]["lease_id"], "lease-123")
        self.assertEqual(state["account"]["extra"]["forward_mailbox_id"], "mailbox-123")
        self.assertNotIn("unrelated_runtime_dump", state["account"]["extra"])
        self.assertEqual(state["config"]["icloud_hme_mode"], "helper_ready_api")
        self.assertEqual(state["config"]["icloud_hme_helper_api_url"], "http://helper.internal")
        self.assertNotIn("chatgpt_gopay_batch_tasks", state["config"])
        self.assertNotIn("chatgpt_gopay_phone_pool", state["config"])
        self.assertNotIn("idea_oaipay_pipeline_state", state["config"])
        self.assertLessEqual(len(state["before_ids"]), 128)

    def test_resume_subscription_auth_action_is_exposed(self):
        platform = ChatGPTPlatform(config=RegisterConfig(extra={}))

        actions = platform.get_platform_actions()

        self.assertIn("resume_subscription_auth", {action["id"] for action in actions})

    def test_probe_local_status_uses_global_candidate_proxy(self):
        account = mock.Mock(
            email="demo@example.com",
            token="at-demo",
            user_id="acct-123",
            extra={"access_token": "at-demo"},
        )
        platform = ChatGPTPlatform(config=RegisterConfig(proxy="http://proxy.example:8080", extra={}))

        with mock.patch(
            "core.proxy_utils.resolve_probe_candidate_proxies",
            return_value=[("http://global.proxy:8080", None, "dynamic country=JP")],
        ) as resolve_candidates, mock.patch(
            "services.chatgpt_core.status_probe.probe_local_chatgpt_status",
            return_value={
                "auth": {"state": "access_token_valid"},
                "subscription": {"plan": "unknown"},
                "codex": {"state": "not_checked"},
            },
        ) as probe:
            result = platform.execute_action("probe_local_status", account, {})

        self.assertTrue(result["ok"])
        resolve_candidates.assert_called_once()
        probe.assert_called_once()
        self.assertEqual(probe.call_args.kwargs.get("proxy"), "http://global.proxy:8080")
        self.assertFalse(probe.call_args.kwargs.get("use_default_proxy"))

    def test_custom_provider_reuses_existing_mailbox_on_second_create_call(self):
        mailbox = _ReuseThenCreateFailMailbox()
        platform = ChatGPTPlatform(
            config=RegisterConfig(extra={"chatgpt_registration_mode": "refresh_token"}),
            mailbox=mailbox,
        )

        with mock.patch(
            "services.chatgpt_core.plugin.build_chatgpt_registration_mode_adapter",
            return_value=_RetryCreateAdapter(),
        ):
            result = platform.register()

        self.assertEqual(result.get("first", {}).get("mailbox_action"), "created")
        self.assertEqual(result.get("second", {}).get("mailbox_action"), "reused_existing")
        self.assertEqual(mailbox.get_email_calls, 1)

    def test_custom_provider_fallback_reuses_alive_mailbox_after_create_error(self):
        mailbox = _ReuseThenCreateFailMailbox()
        platform = ChatGPTPlatform(
            config=RegisterConfig(extra={"chatgpt_registration_mode": "refresh_token"}),
            mailbox=mailbox,
        )

        with mock.patch(
            "services.chatgpt_core.plugin.build_chatgpt_registration_mode_adapter",
            return_value=_VerificationAdapter(),
        ):
            platform.register()

        self.assertEqual(mailbox.get_email_calls, 1)

    def test_registration_failure_preserves_internal_metadata_on_exception(self):
        platform = ChatGPTPlatform(
            config=RegisterConfig(extra={"chatgpt_registration_mode": "access_token_only"}),
            mailbox=_TrackingMailbox(),
        )

        with mock.patch(
            "services.chatgpt_core.plugin.build_chatgpt_registration_mode_adapter",
            return_value=_FailureMetadataAdapter(),
        ):
            with self.assertRaisesRegex(RuntimeError, "Page.goto") as raised:
                platform.register()

        self.assertEqual(
            raised.exception.registration_metadata["mailbox_finalize_outcome"],
            "early_failure",
        )

    def test_direct_mode_ignores_stale_explicit_proxy(self):
        mailbox = _TrackingMailbox()
        log_messages = []
        platform = ChatGPTPlatform(
            config=RegisterConfig(
                proxy="http://host.docker.internal:11011",
                extra={
                    "chatgpt_registration_mode": "refresh_token",
                    "__register_proxy_mode": "direct",
                },
            ),
            mailbox=mailbox,
        )
        platform._log_fn = log_messages.append
        adapter = _CaptureContextAdapter()

        with mock.patch(
            "services.chatgpt_core.plugin.build_chatgpt_registration_mode_adapter",
            return_value=adapter,
        ), mock.patch(
            "services.chatgpt_core.plugin.resolve_default_chatgpt_proxy",
            return_value="http://proxy.example:8080",
        ) as resolve_proxy:
            result = platform.register()

        self.assertTrue(result["success"])
        resolve_proxy.assert_not_called()
        self.assertIsNotNone(adapter.context)
        self.assertEqual(adapter.context.proxy_url, "")
        self.assertIn("[代理] 已选择直连模式，忽略显式代理配置", log_messages)
        self.assertIn("[代理] ChatGPT 注册核心链路 proxy=direct", log_messages)


if __name__ == "__main__":
    unittest.main()
