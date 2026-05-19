import unittest
from unittest import mock

from services.chatgpt_core import payment


class DummyAccount:
    access_token = "at-demo"
    cookies = ""


class ChatGPTPaymentTests(unittest.TestCase):
    def setUp(self):
        payment._checkout_countries_cache["value"] = None
        payment._checkout_countries_cache["expires_at"] = 0
        payment._checkout_pricing_config_cache.clear()

    def test_generate_plus_link_uses_passed_currency_without_fetching_config(self):
        response = mock.Mock()
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

    def test_generate_team_link_matches_har_payload_and_returns_hosted_url(self):
        response = mock.Mock()
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
        self.assertEqual(payload["checkout_ui_mode"], "custom")
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
