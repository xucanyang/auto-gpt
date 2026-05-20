import unittest
import types
from unittest import mock

from services.chatgpt_core.chatgpt_registration_mode_adapter import (
    CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY,
    CHATGPT_REGISTRATION_MODE_REFRESH_TOKEN,
    ChatGPTRegistrationContext,
    build_chatgpt_registration_mode_adapter,
    resolve_chatgpt_registration_mode,
)


class ChatGPTRegistrationModeAdapterTests(unittest.TestCase):
    def test_resolve_defaults_to_refresh_token_mode(self):
        self.assertEqual(
            resolve_chatgpt_registration_mode({}),
            CHATGPT_REGISTRATION_MODE_REFRESH_TOKEN,
        )

    def test_resolve_supports_boolean_no_rt_flag(self):
        self.assertEqual(
            resolve_chatgpt_registration_mode(
                {"chatgpt_has_refresh_token_solution": False}
            ),
            CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY,
        )

    def test_build_account_marks_selected_mode(self):
        adapter = build_chatgpt_registration_mode_adapter(
            {"chatgpt_registration_mode": "access_token_only"}
        )
        result = type(
            "Result",
            (),
            {
                "email": "demo@example.com",
                "password": "pw",
                "account_id": "acct-demo",
                "access_token": "at-demo",
                "refresh_token": "",
                "id_token": "id-demo",
                "session_token": "session-demo",
                "workspace_id": "ws-demo",
                "source": "register",
            },
        )()

        account = adapter.build_account(result, fallback_password="fallback")

        self.assertEqual(account.email, "demo@example.com")
        self.assertEqual(account.password, "pw")
        self.assertEqual(
            account.extra["chatgpt_registration_mode"],
            CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY,
        )
        self.assertFalse(account.extra["chatgpt_has_refresh_token_solution"])

    def test_build_account_propagates_checkout_skip_save_metadata(self):
        adapter = build_chatgpt_registration_mode_adapter(
            {"chatgpt_registration_mode": "access_token_only"}
        )
        result = type(
            "Result",
            (),
            {
                "email": "demo@example.com",
                "password": "pw",
                "account_id": "acct-demo",
                "access_token": "at-demo",
                "refresh_token": "",
                "id_token": "",
                "session_token": "session-demo",
                "workspace_id": "ws-demo",
                "source": "register",
                "metadata": {
                    "chatgpt_checkout_url": "https://chatgpt.com/checkout/openai_llc/cs_live_123",
                    "chatgpt_checkout_amount": "34900000",
                    "chatgpt_checkout_amount_is_zero": False,
                    "chatgpt_skip_save_account": True,
                    "chatgpt_skip_save_reason": "Plus checkout amount != 0",
                },
            },
        )()

        account = adapter.build_account(result, fallback_password="fallback")

        self.assertTrue(account.extra["chatgpt_skip_save_account"])
        self.assertEqual(account.extra["chatgpt_checkout_amount"], "34900000")
        self.assertEqual(
            account.extra["cashier_url"],
            "https://chatgpt.com/checkout/openai_llc/cs_live_123",
        )

    def test_build_account_marks_already_paid_checkout_as_subscribed(self):
        adapter = build_chatgpt_registration_mode_adapter(
            {"chatgpt_registration_mode": "access_token_only"}
        )
        result = type(
            "Result",
            (),
            {
                "email": "paid@example.com",
                "password": "pw",
                "account_id": "acct-demo",
                "access_token": "at-demo",
                "refresh_token": "",
                "id_token": "",
                "session_token": "session-demo",
                "workspace_id": "ws-demo",
                "source": "register",
                "metadata": {
                    "chatgpt_checkout_error_code": "already_paid",
                    "chatgpt_account_unavailable": True,
                    "chatgpt_payment_already_paid": True,
                    "chatgpt_skip_save_account": True,
                    "chatgpt_skip_save_reason": "Plus checkout 已付费响应: you have paid",
                },
            },
        )()

        account = adapter.build_account(result, fallback_password="fallback")

        self.assertEqual(account.status.value, "subscribed")
        self.assertTrue(account.extra["chatgpt_payment_already_paid"])
        self.assertTrue(account.extra["chatgpt_skip_save_account"])

    def test_build_account_marks_registration_access_token_partial_auth(self):
        adapter = build_chatgpt_registration_mode_adapter(
            {"chatgpt_registration_mode": "refresh_token"}
        )
        result = type(
            "Result",
            (),
            {
                "email": "demo@example.com",
                "password": "pw",
                "account_id": "acct-demo",
                "access_token": "at-demo",
                "refresh_token": "",
                "id_token": "",
                "session_token": "session-demo",
                "workspace_id": "acct-demo",
                "source": "registration_session",
                "metadata": {"registration_access_token_saved": True},
                "workspace_artifacts": [
                    {
                        "scope": "free",
                        "label": "registration_at",
                        "account_id": "acct-demo",
                        "workspace_id": "acct-demo",
                        "access_token": "at-demo",
                        "session_token": "session-demo",
                        "source": "registration_session",
                        "variant_key": "registration_at:acct-demo",
                        "auth_level": "access_token_only",
                        "partial_auth": True,
                    }
                ],
            },
        )()

        account = adapter.build_account(result, fallback_password="fallback")

        self.assertEqual(account.token, "at-demo")
        self.assertEqual(account.status.value, "pending_payment")
        self.assertEqual(account.extra["chatgpt_token_source"], "registration_session")

    def test_access_token_only_adapter_passes_runtime_context_to_engine(self):
        created = {}

        class FakeEngine:
            def __init__(self, **kwargs):
                created["kwargs"] = kwargs
                self.email = None
                self.password = None

            def run(self):
                created["email"] = self.email
                created["password"] = self.password
                return type("Result", (), {"success": True})()

        adapter = build_chatgpt_registration_mode_adapter(
            {"chatgpt_registration_mode": "access_token_only"}
        )
        context = ChatGPTRegistrationContext(
            email_service=object(),
            proxy_url="http://127.0.0.1:7890",
            callback_logger=lambda _msg: None,
            email="demo@example.com",
            password="pw-demo",
            browser_mode="headed",
            max_retries=5,
            extra_config={"register_max_retries": 5},
        )

        fake_module = types.ModuleType("services.chatgpt_core.access_token_only_registration_engine")
        fake_module.AccessTokenOnlyRegistrationEngine = FakeEngine
        with mock.patch.dict(
            "sys.modules",
            {"services.chatgpt_core.access_token_only_registration_engine": fake_module},
        ):
            adapter.run(context)

        self.assertEqual(created["email"], "demo@example.com")
        self.assertEqual(created["password"], "pw-demo")
        self.assertEqual(created["kwargs"]["browser_mode"], "headed")
        self.assertEqual(created["kwargs"]["max_retries"], 5)


if __name__ == "__main__":
    unittest.main()
