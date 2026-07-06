import unittest
from unittest import mock

from services.chatgpt_core.access_token_only_registration_engine import (
    AccessTokenOnlyRegistrationEngine,
    EmailServiceAdapter,
)
from services.chatgpt_core.payment import CheckoutRequestError


class AccessTokenOnlyCheckoutTests(unittest.TestCase):
    class _FakeChatGPTClient:
        device_id = "device-demo"

        def __init__(self, *args, **kwargs):
            self._log = None

        def register_complete_flow(self, *args, **kwargs):
            return True, "ok"

        def reuse_session_and_get_tokens(self):
            return True, {
                "access_token": "at-demo",
                "session_token": "session-demo",
                "account_id": "acct-demo",
                "workspace_id": "ws-demo",
                "cookies": "oai-did=device",
            }

    class _TrackingChatGPTClient(_FakeChatGPTClient):
        last_register_kwargs = {}

        def register_complete_flow(self, *args, **kwargs):
            type(self).last_register_kwargs = dict(kwargs)
            return True, "ok"

    def test_v2_email_adapter_returns_none_on_mailbox_timeout_for_resend_path(self):
        email_service = mock.Mock()
        email_service.get_verification_code.side_effect = TimeoutError("等待 Email API 验证码超时 (30s)")
        logs = []
        adapter = EmailServiceAdapter(email_service, "buyer@example.com", logs.append)

        code = adapter.wait_for_verification_code(
            "buyer@example.com",
            timeout=30,
            phase="register_email_otp",
            phase_label="注册阶段邮箱验证码",
        )

        self.assertIsNone(code)
        self.assertEqual(email_service.get_verification_code.call_count, 1)
        self.assertTrue(any("等待超时" in line for line in logs))

    def test_v2_registration_passes_configured_email_otp_timeouts(self):
        email_service = mock.Mock()
        email_service.create_email.return_value = {"email": "buyer@example.com"}
        engine = AccessTokenOnlyRegistrationEngine(
            email_service=email_service,
            proxy_url="http://proxy.local:8080",
            extra_config={
                "chatgpt_register_otp_wait_seconds": 45,
                "chatgpt_register_otp_resend_wait_seconds": 35,
            },
        )
        self._TrackingChatGPTClient.last_register_kwargs = {}

        with (
            mock.patch.object(engine, "_probe_homepage_before_email_creation", return_value=(True, "")),
            mock.patch.object(engine, "_report_homepage_probe"),
            mock.patch.object(engine, "_probe_plus_checkout_billing", return_value={}),
            mock.patch.object(engine, "_capture_k12_workspace_artifacts", return_value=(True, "")),
            mock.patch(
                "services.chatgpt_core.access_token_only_registration_engine.ChatGPTClient",
                self._TrackingChatGPTClient,
            ),
        ):
            result = engine.run()

        self.assertTrue(result.success)
        self.assertEqual(self._TrackingChatGPTClient.last_register_kwargs["otp_wait_timeout"], 45)
        self.assertEqual(self._TrackingChatGPTClient.last_register_kwargs["otp_resend_wait_timeout"], 35)
        self.assertEqual(self._TrackingChatGPTClient.last_register_kwargs["otp_account_budget_timeout"], 80)

    def test_v2_registration_uses_single_account_otp_defaults(self):
        email_service = mock.Mock()
        email_service.create_email.return_value = {"email": "buyer@example.com"}
        engine = AccessTokenOnlyRegistrationEngine(
            email_service=email_service,
            proxy_url="http://proxy.local:8080",
            extra_config={},
        )
        self._TrackingChatGPTClient.last_register_kwargs = {}

        with (
            mock.patch.object(engine, "_probe_homepage_before_email_creation", return_value=(True, "")),
            mock.patch.object(engine, "_report_homepage_probe"),
            mock.patch.object(engine, "_probe_plus_checkout_billing", return_value={}),
            mock.patch.object(engine, "_capture_k12_workspace_artifacts", return_value=(True, "")),
            mock.patch(
                "services.chatgpt_core.access_token_only_registration_engine.ChatGPTClient",
                self._TrackingChatGPTClient,
            ),
        ):
            result = engine.run()

        self.assertTrue(result.success)
        self.assertEqual(self._TrackingChatGPTClient.last_register_kwargs["otp_wait_timeout"], 120)
        self.assertEqual(self._TrackingChatGPTClient.last_register_kwargs["otp_resend_wait_timeout"], 90)
        self.assertEqual(self._TrackingChatGPTClient.last_register_kwargs["otp_account_budget_timeout"], 210)

    def test_already_paid_metadata_fails_registration_without_saving(self):
        email_service = mock.Mock()
        email_service.create_email.return_value = {"email": "buyer@example.com"}
        engine = AccessTokenOnlyRegistrationEngine(
            email_service=email_service,
            proxy_url="http://proxy.local:8080",
            extra_config={
                "chatgpt_access_token_only_checkout_country": "US",
                "chatgpt_access_token_only_checkout_currency": "USD",
            },
        )

        with (
            mock.patch.object(engine, "_probe_homepage_before_email_creation", return_value=(True, "")),
            mock.patch.object(engine, "_report_homepage_probe"),
            mock.patch(
                "services.chatgpt_core.access_token_only_registration_engine.ChatGPTClient",
                self._FakeChatGPTClient,
            ),
            mock.patch(
                "core.proxy_utils.iter_enabled_runtime_proxies",
                return_value=["http://proxy.local:8080"],
            ),
            mock.patch(
                "services.chatgpt_core.payment.generate_plus_link",
                side_effect=CheckoutRequestError(400, '{"detail":"User is already paid"}'),
            ),
        ):
            result = engine.run()

        self.assertFalse(result.success)
        self.assertIn("already paid", result.error_message.lower())
        self.assertTrue(result.metadata["chatgpt_payment_already_paid"])
        email_service.finalize_success.assert_not_called()
        email_service.finalize_failure.assert_called_once()

    def test_checkout_currency_follows_country_when_currency_is_blank(self):
        engine = AccessTokenOnlyRegistrationEngine(
            email_service=mock.Mock(),
            proxy_url="http://proxy.local:8080",
            extra_config={
                "chatgpt_checkout_country": "US",
                "chatgpt_checkout_currency": "",
            },
        )

        with (
            mock.patch(
                "core.proxy_utils.iter_enabled_runtime_proxies",
                return_value=["http://proxy.local:8080"],
            ),
            mock.patch(
                "services.chatgpt_core.payment.generate_plus_link",
                return_value="https://chatgpt.com/checkout/openai_llc/cs_live_123",
            ) as generate_link,
            mock.patch(
                "services.chatgpt_core.gopay_flow.probe_chatgpt_checkout_amount",
                return_value={"amount_text": "0", "amount": 0, "currency": "usd", "amount_is_zero": True},
            ) as probe_amount,
        ):
            metadata = engine._probe_plus_checkout_billing(
                {
                    "access_token": "at-demo",
                    "session_token": "session-demo",
                    "account_id": "acct-demo",
                },
                "buyer@example.com",
            )

        self.assertEqual(metadata["chatgpt_checkout_country"], "US")
        self.assertEqual(metadata["chatgpt_checkout_currency"], "USD")
        self.assertEqual(generate_link.call_args.kwargs["currency"], "USD")
        self.assertEqual(probe_amount.call_args.kwargs["currency"], "USD")

    def test_access_token_only_specific_currency_overrides_legacy_generic_currency(self):
        engine = AccessTokenOnlyRegistrationEngine(
            email_service=mock.Mock(),
            proxy_url="http://proxy.local:8080",
            extra_config={
                "currency": "IDR",
                "country": "ID",
                "chatgpt_access_token_only_checkout_country": "US",
                "chatgpt_access_token_only_checkout_currency": "USD",
            },
        )

        with (
            mock.patch(
                "core.proxy_utils.iter_enabled_runtime_proxies",
                return_value=["http://proxy.local:8080"],
            ),
            mock.patch(
                "services.chatgpt_core.payment.generate_plus_link",
                return_value="https://chatgpt.com/checkout/openai_llc/cs_live_123",
            ) as generate_link,
            mock.patch(
                "services.chatgpt_core.gopay_flow.probe_chatgpt_checkout_amount",
                return_value={"amount_text": "0", "amount": 0, "currency": "usd", "amount_is_zero": True},
            ) as probe_amount,
        ):
            metadata = engine._probe_plus_checkout_billing(
                {
                    "access_token": "at-demo",
                    "cookies": "oai-did=device",
                    "session_token": "session-demo",
                    "account_id": "acct-demo",
                },
                "buyer@example.com",
            )

        self.assertEqual(metadata["chatgpt_checkout_country"], "US")
        self.assertEqual(metadata["chatgpt_checkout_currency"], "USD")
        self.assertEqual(generate_link.call_args.kwargs["country"], "US")
        self.assertEqual(generate_link.call_args.kwargs["currency"], "USD")
        self.assertEqual(probe_amount.call_args.kwargs["country"], "US")
        self.assertEqual(probe_amount.call_args.kwargs["currency"], "USD")

    def test_checkout_already_paid_response_is_classified_as_invalid_failure(self):
        engine = AccessTokenOnlyRegistrationEngine(
            email_service=mock.Mock(),
            proxy_url="http://proxy.local:8080",
            extra_config={
                "chatgpt_access_token_only_checkout_country": "US",
                "chatgpt_access_token_only_checkout_currency": "USD",
            },
        )

        with (
            mock.patch(
                "core.proxy_utils.iter_enabled_runtime_proxies",
                return_value=["http://proxy.local:8080"],
            ),
            mock.patch(
                "services.chatgpt_core.payment.generate_plus_link",
                side_effect=CheckoutRequestError(400, '{"detail":"User is already paid"}'),
            ),
        ):
            metadata = engine._probe_plus_checkout_billing(
                {
                    "access_token": "at-demo",
                    "cookies": "oai-did=device",
                    "session_token": "session-demo",
                    "account_id": "acct-demo",
                },
                "buyer@example.com",
            )

        self.assertTrue(metadata["chatgpt_skip_save_account"])
        self.assertTrue(metadata["chatgpt_account_unavailable"])
        self.assertTrue(metadata["chatgpt_payment_already_paid"])
        self.assertTrue(metadata["chatgpt_invalid_registration_failure"])
        self.assertEqual(metadata["chatgpt_checkout_error_code"], "already_paid")
        self.assertIn("User is already paid", metadata["chatgpt_checkout_error_body"])

    def test_nonzero_checkout_amount_fails_registration_without_mailbox_success(self):
        email_service = mock.Mock()
        email_service.create_email.return_value = {"email": "buyer@example.com"}
        engine = AccessTokenOnlyRegistrationEngine(
            email_service=email_service,
            proxy_url="http://proxy.local:8080",
            extra_config={
                "chatgpt_access_token_only_checkout_country": "US",
                "chatgpt_access_token_only_checkout_currency": "USD",
            },
        )

        with (
            mock.patch.object(engine, "_probe_homepage_before_email_creation", return_value=(True, "")),
            mock.patch.object(engine, "_report_homepage_probe"),
            mock.patch(
                "services.chatgpt_core.access_token_only_registration_engine.ChatGPTClient",
                self._FakeChatGPTClient,
            ),
            mock.patch(
                "core.proxy_utils.iter_enabled_runtime_proxies",
                return_value=["http://proxy.local:8080"],
            ),
            mock.patch(
                "services.chatgpt_core.payment.generate_plus_link",
                return_value="https://chatgpt.com/checkout/openai_llc/cs_live_123",
            ),
            mock.patch(
                "services.chatgpt_core.gopay_flow.probe_chatgpt_checkout_amount",
                return_value={"amount_text": "20.00", "amount": 2000, "currency": "usd", "amount_is_zero": False},
            ),
        ):
            result = engine.run()

        self.assertFalse(result.success)
        self.assertIn("amount != 0", result.error_message)
        self.assertTrue(result.metadata["chatgpt_nonzero_checkout_amount_failure"])
        email_service.finalize_success.assert_not_called()
        email_service.finalize_failure.assert_called_once()

    def test_gopay_provider_link_is_captured_after_zero_amount_checkout(self):
        engine = AccessTokenOnlyRegistrationEngine(
            email_service=mock.Mock(),
            proxy_url="http://proxy.local:8080",
            extra_config={
                "chatgpt_access_token_only_checkout_country": "ID",
                "chatgpt_access_token_only_checkout_currency": "IDR",
                "chatgpt_access_token_only_gopay_provider_link_enabled": True,
                "chatgpt_gopay_defaults": '{"billing_country":"US","billing_name":"Michael Anderson"}',
            },
        )

        provider_snapshot = {
            "phase": "provider_link_ready",
            "checkout_url": "https://pay.openai.com/c/pay/cs_live_123#fid",
            "cs_id": "cs_live_123",
            "snap_token": "11111111-1111-1111-1111-111111111111",
            "payment_platform_url": "https://app.midtrans.com/snap/v4/redirection/11111111-1111-1111-1111-111111111111",
            "midtrans_redirect_url": "https://app.midtrans.com/snap/v4/redirection/11111111-1111-1111-1111-111111111111",
            "stripe_redirect_url": "https://pm-redirects.stripe.com/authorize/acct/test",
            "result": {"payment_method_types": ["gopay"]},
        }
        with (
            mock.patch(
                "core.proxy_utils.iter_enabled_runtime_proxies",
                return_value=["http://proxy.local:8080"],
            ),
            mock.patch(
                "services.chatgpt_core.payment.generate_plus_link",
                return_value="https://pay.openai.com/c/pay/cs_live_123#fid",
            ) as generate_link,
            mock.patch(
                "services.chatgpt_core.gopay_flow.probe_chatgpt_checkout_amount",
                return_value={"amount_text": "0", "amount": 0, "currency": "idr", "amount_is_zero": True},
            ),
            mock.patch(
                "services.chatgpt_core.gopay_flow.create_gopay_provider_link",
                return_value=provider_snapshot,
            ) as create_provider_link,
        ):
            metadata = engine._probe_plus_checkout_billing(
                {
                    "access_token": "at-demo",
                    "cookies": "oai-did=device",
                    "session_token": "session-demo",
                    "account_id": "acct-demo",
                },
                "buyer@example.com",
            )

        self.assertTrue(metadata["chatgpt_gopay_provider_link_enabled"])
        self.assertTrue(metadata["chatgpt_gopay_provider_link_ready"])
        self.assertEqual(metadata["chatgpt_gopay_provider_link"], provider_snapshot["payment_platform_url"])
        self.assertEqual(metadata["chatgpt_gopay_provider_link_cs_id"], "cs_live_123")
        self.assertNotIn("link_format", generate_link.call_args.kwargs)
        self.assertEqual(generate_link.call_args.kwargs["billing"]["country"], "ID")
        create_provider_link.assert_called_once()
        self.assertEqual(create_provider_link.call_args.kwargs["checkout_url"], "https://pay.openai.com/c/pay/cs_live_123#fid")
        self.assertEqual(create_provider_link.call_args.kwargs["billing"]["country"], "ID")
        checkout_account = create_provider_link.call_args.args[0]
        self.assertEqual(checkout_account.session_token, "session-demo")
        self.assertEqual(checkout_account.extra["session_token"], "session-demo")

    def test_gopay_provider_link_failure_does_not_fail_registration(self):
        email_service = mock.Mock()
        email_service.create_email.return_value = {"email": "buyer@example.com"}
        engine = AccessTokenOnlyRegistrationEngine(
            email_service=email_service,
            proxy_url="http://proxy.local:8080",
            extra_config={
                "chatgpt_access_token_only_checkout_country": "ID",
                "chatgpt_access_token_only_checkout_currency": "IDR",
                "chatgpt_access_token_only_gopay_provider_link_enabled": True,
            },
        )

        with (
            mock.patch.object(engine, "_probe_homepage_before_email_creation", return_value=(True, "")),
            mock.patch.object(engine, "_report_homepage_probe"),
            mock.patch(
                "services.chatgpt_core.access_token_only_registration_engine.ChatGPTClient",
                self._FakeChatGPTClient,
            ),
            mock.patch(
                "core.proxy_utils.iter_enabled_runtime_proxies",
                return_value=["http://proxy.local:8080"],
            ),
            mock.patch(
                "services.chatgpt_core.payment.generate_plus_link",
                return_value="https://pay.openai.com/c/pay/cs_live_123#fid",
            ),
            mock.patch(
                "services.chatgpt_core.gopay_flow.probe_chatgpt_checkout_amount",
                return_value={"amount_text": "0", "amount": 0, "currency": "idr", "amount_is_zero": True},
            ),
            mock.patch(
                "services.chatgpt_core.gopay_flow.create_gopay_provider_link",
                side_effect=RuntimeError("midtrans unavailable"),
            ),
        ):
            result = engine.run()

        self.assertTrue(result.success)
        self.assertFalse(result.metadata["chatgpt_gopay_provider_link_ready"])
        self.assertIn("midtrans unavailable", result.metadata["chatgpt_gopay_provider_link_error"])
        email_service.finalize_success.assert_called_once()
        email_service.finalize_failure.assert_not_called()


if __name__ == "__main__":
    unittest.main()
