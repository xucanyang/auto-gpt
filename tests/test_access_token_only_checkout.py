import unittest
from unittest import mock

from services.chatgpt_core.access_token_only_registration_engine import AccessTokenOnlyRegistrationEngine
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

    def test_already_paid_metadata_skips_mailbox_writeback(self):
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
                side_effect=CheckoutRequestError(400, '{"detail":"you have paid"}'),
            ),
        ):
            result = engine.run()

        self.assertTrue(result.success)
        self.assertTrue(result.metadata["chatgpt_payment_already_paid"])
        email_service.finalize_success.assert_not_called()
        email_service.finalize_failure.assert_not_called()

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
                    "cookies": "oai-did=device",
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

    def test_checkout_already_paid_response_is_classified_as_skip_save(self):
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
                side_effect=CheckoutRequestError(400, '{"detail":"you have paid"}'),
            ),
        ):
            metadata = engine._probe_plus_checkout_billing(
                {
                    "access_token": "at-demo",
                    "cookies": "oai-did=device",
                    "account_id": "acct-demo",
                },
                "buyer@example.com",
            )

        self.assertTrue(metadata["chatgpt_skip_save_account"])
        self.assertTrue(metadata["chatgpt_account_unavailable"])
        self.assertTrue(metadata["chatgpt_payment_already_paid"])
        self.assertEqual(metadata["chatgpt_checkout_error_code"], "already_paid")
        self.assertIn("you have paid", metadata["chatgpt_checkout_error_body"])


if __name__ == "__main__":
    unittest.main()
