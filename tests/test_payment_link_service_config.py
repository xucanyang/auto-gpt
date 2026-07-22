import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

from api import config as config_api
from core.shared_config import is_shareable_key
from services.chatgpt_core.long_link_payment_client import LongLinkPaymentClient


ROOT = Path(__file__).resolve().parents[1]


class PaymentLinkServiceConfigTests(unittest.TestCase):
    def test_service_credentials_are_registered_and_shareable(self):
        for key in ("openai_pay_long_link_base_url", "openai_pay_long_link_api_key"):
            self.assertIn(key, config_api.CONFIG_KEYS)
            self.assertTrue(is_shareable_key(key))

    def test_config_update_normalizes_url_and_key(self):
        with mock.patch.object(config_api.config_store, "get_all", return_value={}), mock.patch.object(
            config_api.config_store,
            "set_many",
        ) as set_many:
            result = config_api.update_config(
                config_api.ConfigUpdate(
                    data={
                        "openai_pay_long_link_base_url": "https://pay.example.test/",
                        "openai_pay_long_link_api_key": "  opll_live_test  ",
                    }
                )
            )

        self.assertTrue(result["ok"])
        saved = set_many.call_args.args[0]
        self.assertEqual(saved["openai_pay_long_link_base_url"], "https://pay.example.test")
        self.assertEqual(saved["openai_pay_long_link_api_key"], "opll_live_test")

    def test_config_update_rejects_url_credentials(self):
        with mock.patch.object(config_api.config_store, "get_all", return_value={}):
            with self.assertRaises(HTTPException) as raised:
                config_api.update_config(
                    config_api.ConfigUpdate(
                        data={
                            "openai_pay_long_link_base_url": "https://user:pass@pay.example.test",
                        }
                    )
                )

        self.assertEqual(raised.exception.status_code, 400)
        self.assertNotIn("pass", str(raised.exception.detail))

    def test_connection_test_returns_only_redacted_profile_summary(self):
        api_key = "opll_live_test-secret"
        profile = {
            "profile_hash": "a" * 64,
            "link_type": "paypal",
            "country": "GB",
            "currency": "GBP",
            "effective_concurrency": 4,
        }
        with mock.patch.object(LongLinkPaymentClient, "get_profile", return_value=profile):
            result = config_api.test_payment_link_connection(
                config_api.PaymentLinkConnectionTestRequest(
                    base_url="https://pay.example.test",
                    api_key=api_key,
                )
            )

        self.assertEqual(result["api_version"], "v1")
        self.assertEqual(result["link_type"], "paypal")
        self.assertEqual(result["effective_concurrency"], 4)
        self.assertNotIn(api_key, repr(result))
        self.assertEqual(result["profile_hash_prefix"], "a" * 12)

    def test_settings_page_exposes_connection_fields_and_test_action(self):
        source = (ROOT / "frontend" / "src" / "pages" / "Settings.tsx").read_text(encoding="utf-8")
        self.assertIn("title: '支付长链服务'", source)
        self.assertIn("key: 'openai_pay_long_link_base_url'", source)
        self.assertIn("key: 'openai_pay_long_link_api_key'", source)
        self.assertIn("apiFetch('/config/payment-link/test'", source)
        self.assertIn("message={`连接成功", source)


if __name__ == "__main__":
    unittest.main()
