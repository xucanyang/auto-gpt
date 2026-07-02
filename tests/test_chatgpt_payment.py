import unittest
from unittest import mock

from services.chatgpt_core import payment


class DummyAccount:
    access_token = "at-demo"
    cookies = ""


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

    def test_generate_plus_short_link_uses_custom_checkout_and_returns_hosted_url(self):
        checkout_response = _response(
            {
                "checkout_session_id": "cs_live_short123",
                "processor_entity": "openai_llc",
                "publishable_key": payment.OPENAI_STRIPE_PK,
                "url": None,
            }
        )
        stripe_response = _response(
            {
                "stripe_hosted_url": "https://checkout.stripe.com/c/pay/cs_live_short123#fid_from_stripe",
            }
        )

        with mock.patch.object(payment.cffi_requests, "post", side_effect=[checkout_response, stripe_response]) as post_mock:
            url = payment.generate_plus_short_link(DummyAccount(), country="JP", currency="JPY")

        self.assertEqual(url, "https://pay.openai.com/c/pay/cs_live_short123#fid_from_stripe")
        payload = post_mock.call_args_list[0].kwargs["json"]
        self.assertEqual(payload["entry_point"], "all_plans_pricing_modal")
        self.assertEqual(payload["checkout_ui_mode"], "custom")
        self.assertEqual(payload["billing_details"], {"country": "JP", "currency": "JPY"})
        init_url = post_mock.call_args_list[1].args[0]
        init_payload = post_mock.call_args_list[1].kwargs["data"]
        self.assertIn("/v1/payment_pages/cs_live_short123/init", init_url)
        self.assertEqual(init_payload["elements_session_client[elements_init_source]"], "custom_checkout")
        self.assertEqual(init_payload["elements_session_client[referrer_host]"], "chatgpt.com")
        self.assertEqual(init_payload["elements_options_client[saved_payment_method][enable_save]"], "auto")
        self.assertEqual(init_payload["key"], payment.OPENAI_STRIPE_PK)
        self.assertEqual(init_payload["_stripe_version"], payment.STRIPE_VERSION_FULL)

    def test_generate_plus_short_link_rejects_custom_checkout_without_hosted_url(self):
        checkout_response = _response(
            {
                "checkout_session_id": "cs_live_short_without_hosted",
                "processor_entity": "openai_llc",
                "publishable_key": payment.OPENAI_STRIPE_PK,
                "url": None,
            }
        )
        stripe_response = _response({})

        def _post(url, **kwargs):
            if url == payment.PAYMENT_CHECKOUT_URL:
                return checkout_response
            return stripe_response

        with mock.patch.object(payment.cffi_requests, "post", side_effect=_post):
            with self.assertRaises(payment.CustomCheckoutResolutionError):
                payment.generate_plus_short_link(DummyAccount(), country="JP", currency="JPY")

    def test_normalize_short_format_cache_keeps_hosted_payment_url_openable(self):
        url = payment.normalize_checkout_url_for_link_format(
            "https://pay.openai.com/c/pay/cs_live_short_cached#fid_cached",
            payment.PAYMENT_LINK_FORMAT_SHORT,
        )

        self.assertEqual(url, "https://pay.openai.com/c/pay/cs_live_short_cached#fid_cached")

    def test_normalize_short_format_legacy_chatgpt_url_to_hosted_payment_url(self):
        url = payment.normalize_checkout_url_for_link_format(
            "https://chatgpt.com/checkout/openai_llc/cs_live_short_cached",
            payment.PAYMENT_LINK_FORMAT_SHORT,
        )

        self.assertEqual(
            url,
            "https://pay.openai.com/c/pay/cs_live_short_cached"
            + payment.PAY_OPENAI_CHECKOUT_FRAGMENT,
        )

    def test_generate_team_short_link_uses_custom_checkout_and_returns_hosted_url(self):
        checkout_response = _response(
            {
                "checkout_session_id": "cs_live_team_short123",
                "processor_entity": "openai_llc",
                "publishable_key": payment.OPENAI_STRIPE_PK,
                "url": None,
            }
        )
        stripe_response = _response(
            {
                "stripe_hosted_url": "https://checkout.stripe.com/c/pay/cs_live_team_short123#fid_team",
            }
        )

        with mock.patch.object(payment.cffi_requests, "post", side_effect=[checkout_response, stripe_response]) as post_mock:
            url = payment.generate_team_link(
                DummyAccount(),
                country="US",
                currency="USD",
                link_format=payment.PAYMENT_LINK_FORMAT_SHORT,
            )

        self.assertEqual(url, "https://pay.openai.com/c/pay/cs_live_team_short123#fid_team")
        payload = post_mock.call_args_list[0].kwargs["json"]
        self.assertEqual(payload["checkout_ui_mode"], "custom")
        init_payload = post_mock.call_args_list[1].kwargs["data"]
        self.assertEqual(init_payload["elements_session_client[elements_init_source]"], "custom_checkout")

    def test_generate_plus_short_link_uses_fallback_publishable_key(self):
        checkout_response = _response(
            {
                "checkout_session_id": "cs_live_short_fallback",
                "processor_entity": "openai_llc",
                "url": None,
            }
        )
        stripe_response = _response(
            {
                "stripe_hosted_url": "https://checkout.stripe.com/c/pay/cs_live_short_fallback#fid_fallback",
            }
        )

        with mock.patch.object(payment.cffi_requests, "post", side_effect=[checkout_response, stripe_response]) as post_mock:
            url = payment.generate_plus_short_link(DummyAccount(), country="JP", currency="JPY")

        self.assertEqual(url, "https://pay.openai.com/c/pay/cs_live_short_fallback#fid_fallback")
        init_payload = post_mock.call_args_list[1].kwargs["data"]
        self.assertEqual(init_payload["key"], payment.OPENAI_STRIPE_PK)

    def test_generate_plus_short_link_uses_custom_checkout_and_returns_existing_hosted_url(self):
        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = {
            "checkout_session_id": "cs_live_short123",
            "processor_entity": "openai_llc",
            "url": "https://checkout.stripe.com/c/pay/cs_live_short123#fid_existing",
        }
        response.raise_for_status.return_value = None

        with mock.patch.object(payment.cffi_requests, "post", return_value=response) as post_mock:
            url = payment.generate_plus_short_link(DummyAccount(), country="JP", currency="JPY")

        self.assertEqual(url, "https://pay.openai.com/c/pay/cs_live_short123#fid_existing")
        self.assertEqual(post_mock.call_count, 1)
        payload = post_mock.call_args.kwargs["json"]
        self.assertEqual(payload["entry_point"], "all_plans_pricing_modal")
        self.assertEqual(payload["checkout_ui_mode"], "custom")
        self.assertEqual(payload["billing_details"], {"country": "JP", "currency": "JPY"})

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

    def test_generate_team_link_builds_pay_openai_url_from_checkout_session_id_by_default(self):
        checkout_response = _response({"checkout_session_id": "cs_live_team123"})
        stripe_response = _response(
            {
                "stripe_hosted_url": "https://checkout.stripe.com/c/pay/cs_live_team123#fid_team_hosted",
            }
        )

        with mock.patch.object(payment.cffi_requests, "post", side_effect=[checkout_response, stripe_response]) as post_mock:
            url = payment.generate_team_link(DummyAccount(), country="US", currency="USD")

        self.assertEqual(url, "https://pay.openai.com/c/pay/cs_live_team123#fid_team_hosted")
        payload = post_mock.call_args_list[0].kwargs["json"]
        self.assertEqual(payload["checkout_ui_mode"], "hosted")

    def test_generate_team_link_matches_har_payload_and_returns_hosted_url(self):
        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = {"url": "https://chatgpt.com/checkout/openai_llc/long-team-url"}
        response.raise_for_status.return_value = None

        with mock.patch.object(payment.cffi_requests, "post", return_value=response) as post_mock, \
            mock.patch.object(payment.cffi_requests, "get") as get_mock:
            url = payment.generate_team_link(
                DummyAccount(),
                workspace_name="TeamDemo",
                price_interval="year",
                seat_quantity=3,
                country="JP",
                currency="JPY",
            )

        self.assertEqual(url, "https://chatgpt.com/checkout/openai_llc/long-team-url")
        get_mock.assert_not_called()
        payload = post_mock.call_args.kwargs["json"]
        self.assertEqual(payload["billing_details"], {"country": "JP", "currency": "JPY"})
        self.assertEqual(payload["plan_name"], "chatgptteamplan")
        self.assertEqual(payload["team_plan_data"]["workspace_name"], "TeamDemo")
        self.assertEqual(payload["team_plan_data"]["seat_quantity"], 3)
        self.assertNotIn("existing_workspace_id", payload["team_plan_data"])
        self.assertEqual(payload["checkout_ui_mode"], "hosted")
        headers = post_mock.call_args.kwargs["headers"]
        self.assertNotIn("chatgpt-account-id", headers)
        self.assertEqual(payload["cancel_url"], "https://chatgpt.com/?promoCode=STRIPEATLASGPT4BIZ050126")

    def test_checkout_config_summary_extracts_currency(self):
        summary = payment.summarize_checkout_pricing_config(
            {
                "country_code": "DE",
                "symbol_code": "EUR",
                "symbol": "€",
                "currency_config": {
                    "plus": {"month": {"amount": 23}},
                    "business": {"month": {"amount": 26}},
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
