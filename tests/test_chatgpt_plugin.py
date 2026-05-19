import unittest
from unittest import mock

from core.base_mailbox import MailboxAccount
from core.base_platform import RegisterConfig
from services.chatgpt_core.plugin import ChatGPTPlatform


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


class ChatGPTPluginTests(unittest.TestCase):
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

    def test_resume_subscription_auth_action_is_exposed(self):
        platform = ChatGPTPlatform(config=RegisterConfig(extra={}))

        actions = platform.get_platform_actions()

        self.assertIn("resume_subscription_auth", {action["id"] for action in actions})

    def test_probe_local_status_uses_direct_connection(self):
        account = mock.Mock(
            email="demo@example.com",
            token="at-demo",
            user_id="acct-123",
            extra={"access_token": "at-demo"},
        )
        platform = ChatGPTPlatform(config=RegisterConfig(proxy="http://proxy.example:8080", extra={}))

        with mock.patch(
            "services.chatgpt_core.plugin.resolve_runtime_proxy",
            return_value="http://proxy.example:8080",
        ) as resolve_proxy, mock.patch(
            "services.chatgpt_core.status_probe.probe_local_chatgpt_status",
            return_value={
                "auth": {"state": "access_token_valid"},
                "subscription": {"plan": "unknown"},
                "codex": {"state": "not_checked"},
            },
        ) as probe:
            result = platform.execute_action("probe_local_status", account, {})

        self.assertTrue(result["ok"])
        resolve_proxy.assert_not_called()
        probe.assert_called_once()
        self.assertEqual(probe.call_args.kwargs.get("proxy"), "")

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


if __name__ == "__main__":
    unittest.main()
