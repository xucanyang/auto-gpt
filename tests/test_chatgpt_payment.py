import unittest
from unittest import mock

from services.chatgpt_core import payment


class DummyAccount:
    access_token = "at-demo"
    cookies = ""


class LoggedInDummyAccount(DummyAccount):
    cookies = "oai-did=device-demo; __Secure-next-auth.session-token=web-session-demo"


def _response(payload: dict, *, status: int = 200, text: str = ""):
    response = mock.Mock()
    response.status_code = status
    response.text = text
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


class ChatGPTPaymentTests(unittest.TestCase):
    def setUp(self):
        payment._checkout_countries_cache["value"] = None
        payment._checkout_countries_cache["expires_at"] = 0
        payment._checkout_pricing_config_cache.clear()

    def test_generate_plus_link_uses_passed_currency_without_fetching_config(self):
        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = {"url": "https://chatgpt.com/checkout/openai_llc/long-plus-url"}
        response.raise_for_status.return_value = None

        with mock.patch.object(payment.cffi_requests, "post", return_value=response) as post_mock, \
            mock.patch.object(payment.cffi_requests, "get") as get_mock:
            url = payment.generate_plus_link(DummyAccount(), country="ID", currency="IDR")

        self.assertEqual(url, "https://chatgpt.com/checkout/openai_llc/long-plus-url")
        get_mock.assert_not_called()
        payload = post_mock.call_args.kwargs["json"]
        self.assertEqual(payload["billing_details"], {"country": "ID", "currency": "IDR"})
        self.assertEqual(payload["plan_name"], "chatgptplusplan")
        self.assertEqual(payload["checkout_ui_mode"], "hosted")
        self.assertEqual(payload["promo_campaign"]["promo_campaign_id"], "plus-1-month-free")
        self.assertNotIn("promo_code", payload)

    def test_generate_plus_link_normalizes_chatgpt_checkout_to_pay_openai(self):
        checkout_response = _response(
            {
                "url": "https://chatgpt.com/checkout/openai_llc/cs_live_demo123",
                "publishable_key": payment.OPENAI_STRIPE_PK,
            }
        )
        stripe_response = _response(
            {
                "stripe_hosted_url": "https://checkout.stripe.com/c/pay/cs_live_demo123#fid_from_stripe",
            }
        )

        with mock.patch.object(payment.cffi_requests, "post", side_effect=[checkout_response, stripe_response]) as post_mock:
            url = payment.generate_plus_link(DummyAccount(), country="US", currency="USD")

        self.assertEqual(url, "https://pay.openai.com/c/pay/cs_live_demo123#fid_from_stripe")
        payload = post_mock.call_args_list[0].kwargs["json"]
        self.assertEqual(payload["checkout_ui_mode"], "hosted")
        init_url = post_mock.call_args_list[1].args[0]
        init_payload = post_mock.call_args_list[1].kwargs["data"]
        self.assertIn("/v1/payment_pages/cs_live_demo123/init", init_url)
        self.assertEqual(init_payload["key"], payment.OPENAI_STRIPE_PK)

    def test_generate_plus_link_preserves_existing_hosted_fragment_without_stripe_init(self):
        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = {
            "url": "https://chatgpt.com/checkout/openai_llc/cs_live_demo123",
        }
        response.raise_for_status.return_value = None

        response.json.return_value = {
            "url": "https://checkout.stripe.com/c/pay/cs_live_demo123#fid_existing",
        }

        with mock.patch.object(payment.cffi_requests, "post", return_value=response) as post_mock:
            url = payment.generate_plus_link(DummyAccount(), country="US", currency="USD")

        self.assertEqual(url, "https://pay.openai.com/c/pay/cs_live_demo123#fid_existing")
        self.assertEqual(post_mock.call_count, 1)

    def test_generate_plus_link_builds_pay_openai_url_from_checkout_session_id(self):
        checkout_response = _response({"checkout_session_id": "cs_live_demo456"})
        stripe_response = _response(
            {
                "stripe_hosted_url": "https://checkout.stripe.com/c/pay/cs_live_demo456#fid_from_session",
            }
        )

        with mock.patch.object(payment.cffi_requests, "post", side_effect=[checkout_response, stripe_response]) as post_mock:
            url = payment.generate_plus_link(DummyAccount(), country="US", currency="USD")

        self.assertEqual(url, "https://pay.openai.com/c/pay/cs_live_demo456#fid_from_session")
        init_payload = post_mock.call_args_list[1].kwargs["data"]
        self.assertEqual(init_payload["key"], payment.OPENAI_STRIPE_PK)

    def test_generate_plus_short_link_uses_custom_checkout_and_returns_chatgpt_url(self):
        checkout_response = _response(
            {
                "checkout_session_id": "cs_live_short123",
                "processor_entity": "openai_llc",
                "publishable_key": payment.OPENAI_STRIPE_PK,
                "url": None,
            }
        )

        with mock.patch.object(payment.cffi_requests, "post", return_value=checkout_response) as post_mock:
            url = payment.generate_plus_short_link(LoggedInDummyAccount(), country="JP", currency="JPY")

        self.assertEqual(url, "https://chatgpt.com/checkout/openai_llc/cs_live_short123")
        self.assertEqual(post_mock.call_count, 1)
        payload = post_mock.call_args.kwargs["json"]
        self.assertEqual(payload["entry_point"], "all_plans_pricing_modal")
        self.assertEqual(payload["checkout_ui_mode"], "custom")
        self.assertEqual(payload["billing_details"], {"country": "JP", "currency": "JPY"})

    def test_generate_plus_short_link_needs_only_checkout_session_id(self):
        checkout_response = _response(
            {
                "checkout_session_id": "cs_live_short_without_hosted",
                "processor_entity": "openai_llc",
                "publishable_key": payment.OPENAI_STRIPE_PK,
                "url": None,
            }
        )
        with mock.patch.object(payment.cffi_requests, "post", return_value=checkout_response) as post_mock:
            url = payment.generate_plus_short_link(LoggedInDummyAccount(), country="JP", currency="JPY")

        self.assertEqual(url, "https://chatgpt.com/checkout/openai_llc/cs_live_short_without_hosted")
        self.assertEqual(post_mock.call_count, 1)

    def test_normalize_short_format_cache_keeps_hosted_payment_url_openable(self):
        url = payment.normalize_checkout_url_for_link_format(
            "https://pay.openai.com/c/pay/cs_live_short_cached#fid_cached",
            payment.PAYMENT_LINK_FORMAT_SHORT,
        )

        self.assertEqual(url, "https://chatgpt.com/checkout/openai_llc/cs_live_short_cached")

    def test_normalize_short_format_legacy_chatgpt_url_to_hosted_payment_url(self):
        url = payment.normalize_checkout_url_for_link_format(
            "https://chatgpt.com/checkout/openai_llc/cs_live_short_cached",
            payment.PAYMENT_LINK_FORMAT_SHORT,
        )

        self.assertEqual(
            url,
            "https://chatgpt.com/checkout/openai_llc/cs_live_short_cached",
        )

    def test_generate_plus_short_link_requires_saved_web_session(self):
        with mock.patch.object(payment.cffi_requests, "post") as post_mock:
            with self.assertRaises(payment.ShortCheckoutWebSessionRequiredError):
                payment.generate_plus_short_link(DummyAccount(), country="JP", currency="JPY")

        post_mock.assert_not_called()

    def test_generate_plus_short_link_uses_custom_checkout_and_returns_existing_hosted_url(self):
        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = {
            "checkout_session_id": "cs_live_short123",
            "processor_entity": "openai_payments_uk_ltd",
            "url": "https://checkout.stripe.com/c/pay/cs_live_short123#fid_existing",
        }
        response.raise_for_status.return_value = None

        with mock.patch.object(payment.cffi_requests, "post", return_value=response) as post_mock:
            url = payment.generate_plus_short_link(LoggedInDummyAccount(), country="JP", currency="JPY")

        self.assertEqual(url, "https://chatgpt.com/checkout/openai_payments_uk_ltd/cs_live_short123")
        self.assertEqual(post_mock.call_count, 1)
        payload = post_mock.call_args.kwargs["json"]
        self.assertEqual(payload["entry_point"], "all_plans_pricing_modal")
        self.assertEqual(payload["checkout_ui_mode"], "custom")
        self.assertEqual(payload["billing_details"], {"country": "JP", "currency": "JPY"})

    def test_web_session_detection_accepts_complete_chunked_cookie(self):
        account = DummyAccount()
        account.cookies = (
            "__Secure-authjs.session-token.1=part-b; "
            "__Secure-authjs.session-token.0=part-a"
        )

        self.assertEqual(payment.extract_chatgpt_web_session_token(account), "part-apart-b")
        self.assertTrue(payment.has_chatgpt_web_session(account))

    def test_web_session_detection_rejects_incomplete_chunked_cookie(self):
        account = DummyAccount()
        account.cookies = "__Secure-next-auth.session-token.1=part-b"

        self.assertEqual(payment.extract_chatgpt_web_session_token(account), "")
        self.assertFalse(payment.has_chatgpt_web_session(account))

    def test_normalize_hosted_checkout_url_preserves_existing_fragment(self):
        url = payment.normalize_hosted_checkout_url(
            "https://checkout.stripe.com/c/pay/cs_live_demo789#fid_existing"
        )

        self.assertEqual(url, "https://pay.openai.com/c/pay/cs_live_demo789#fid_existing")

    def test_normalize_hosted_checkout_url_adds_missing_fragment(self):
        url = payment.normalize_hosted_checkout_url("https://pay.openai.com/c/pay/cs_live_demo999")

        self.assertEqual(
            url,
            "https://pay.openai.com/c/pay/cs_live_demo999"
            + payment.PAY_OPENAI_CHECKOUT_FRAGMENT,
        )

    def test_checkout_config_summary_extracts_currency(self):
        summary = payment.summarize_checkout_pricing_config(
            {
                "country_code": "DE",
                "symbol_code": "EUR",
                "symbol": "€",
                "currency_config": {
                    "plus": {"month": {"amount": 23}},
                },
            }
        )

        self.assertEqual(summary["country_code"], "DE")
        self.assertEqual(summary["symbol_code"], "EUR")
        self.assertEqual(summary["plus"]["month"]["amount"], 23)

    def test_checkout_countries_are_cached_without_proxy(self):
        response = mock.Mock()
        response.json.return_value = {"countries": ["ID", "JP"]}
        response.raise_for_status.return_value = None

        with mock.patch.object(payment.cffi_requests, "get", return_value=response) as get_mock:
            self.assertEqual(payment.fetch_checkout_countries(), ["ID", "JP"])
            self.assertEqual(payment.fetch_checkout_countries(), ["ID", "JP"])

        get_mock.assert_called_once()

    def test_checkout_config_is_cached_without_proxy(self):
        response = mock.Mock()
        response.json.return_value = {"country_code": "ID", "symbol_code": "IDR"}
        response.raise_for_status.return_value = None

        with mock.patch.object(payment.cffi_requests, "get", return_value=response) as get_mock:
            self.assertEqual(payment.fetch_checkout_pricing_config("ID")["symbol_code"], "IDR")
            self.assertEqual(payment.fetch_checkout_pricing_config("ID")["symbol_code"], "IDR")

        get_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
