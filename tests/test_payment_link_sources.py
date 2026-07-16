import unittest
from unittest import mock

from api.actions import _apply_action_result
from core.base_platform import Account, RegisterConfig
from core.db import AccountModel
from services.chatgpt_core.payment_link_cache import (
    build_payment_link_cache_payload,
    normalize_payment_link_params,
    payment_link_cache_matches,
    payment_link_requires_regeneration,
)
from services.chatgpt_core.plugin import ChatGPTPlatform


PROFILE_HASH = "profile-hash-123"
PAYPAL_URL = "https://www.paypal.com/agreements/approve?ba_token=BA-123"


class PaymentLinkSourceTests(unittest.TestCase):
    def test_action_persistence_mirrors_paypal_cache(self):
        account = AccountModel(
            platform="chatgpt",
            email="paypal@example.com",
            password="pw",
            status="registered",
        )
        session = mock.Mock()
        result = {
            "ok": True,
            "data": {
                "url": PAYPAL_URL,
                "paypal_url": PAYPAL_URL,
                "provider_redirect_url": PAYPAL_URL,
                "plan": "plus",
                "country": "GB",
                "currency": "GBP",
                "payment_link_format": "paypal_url",
                "payment_source": "long_link_paypal",
                "profile_hash": PROFILE_HASH,
                "cache_source": "long_link_paypal",
            },
        }

        _apply_action_result("chatgpt", "payment_link", account, result, session)

        extra = account.get_extra()
        self.assertEqual(account.cashier_url, PAYPAL_URL)
        self.assertEqual(extra["chatgpt_last_payment_link"]["paypal_url"], PAYPAL_URL)
        self.assertEqual(extra["chatgpt_paypal_url"]["url"], PAYPAL_URL)
        self.assertEqual(extra["chatgpt_paypal_url"]["profile_hash"], PROFILE_HASH)
        self.assertEqual(account.status, "pending_payment")

    def test_paypal_cache_requires_matching_payment_source_and_profile(self):
        cached = build_payment_link_cache_payload(
            {
                "url": PAYPAL_URL,
                "paypal_url": PAYPAL_URL,
                "provider_redirect_url": PAYPAL_URL,
                "plan": "plus",
                "country": "GB",
                "currency": "GBP",
                "payment_link_format": "paypal_url",
                "payment_source": "long_link_paypal",
                "profile_hash": PROFILE_HASH,
                "cs_id": "cs_live_123",
            },
            source="long_link_paypal",
        )
        expected = normalize_payment_link_params(
            {
                "plan": "plus",
                "country": "GB",
                "currency": "GBP",
                "payment_source": "long_link_paypal",
                "profile_hash": PROFILE_HASH,
            }
        )

        self.assertTrue(payment_link_cache_matches(cached, expected))
        self.assertEqual(cached["paypal_url"], PAYPAL_URL)
        self.assertEqual(cached["provider_redirect_url"], PAYPAL_URL)
        self.assertEqual(cached["cs_id"], "cs_live_123")
        self.assertFalse(payment_link_cache_matches(cached, {**expected, "profile_hash": "changed"}))
        self.assertFalse(
            payment_link_cache_matches(
                cached,
                {"plan": "plus", "country": "GB", "currency": "GBP", "payment_source": "chatgpt_hosted"},
            )
        )

    def test_paypal_cache_does_not_inherit_hosted_proxy(self):
        cached = build_payment_link_cache_payload(
            {
                "url": PAYPAL_URL,
                "plan": "plus",
                "country": "GB",
                "currency": "GBP",
                "payment_link_format": "paypal_url",
                "payment_source": "long_link_paypal",
                "profile_hash": PROFILE_HASH,
                "proxy": "",
            },
            source="long_link_paypal",
            fallback={
                "url": "https://pay.openai.com/c/pay/cs_live_old#fid_real",
                "payment_link_format": "long_hosted",
                "payment_source": "chatgpt_hosted",
                "proxy": "http://old-proxy.example:8080",
            },
        )

        self.assertEqual(cached["proxy"], "")

    def test_legacy_hosted_cache_still_matches(self):
        cached = {
            "url": "https://pay.openai.com/c/pay/cs_live_123#fid_real",
            "plan": "plus",
            "country": "ID",
            "currency": "IDR",
            "proxy": "",
            "payment_link_format": "long_hosted",
        }
        self.assertTrue(
            payment_link_cache_matches(
                cached,
                {"plan": "plus", "country": "ID", "currency": "IDR", "payment_link_format": "long_hosted"},
            )
        )

    def test_pix_cache_preserves_provider_expiry_and_rejects_imminent_expiry(self):
        cached = build_payment_link_cache_payload(
            {
                "url": "https://payments.stripe.com/qr/instructions/pix-cache",
                "plan": "plus",
                "country": "BR",
                "currency": "BRL",
                "link_type": "pix",
                "link_expires_at": 1_784_170_800,
                "payment_link_format": "long_link",
                "payment_source": "long_link",
                "profile_hash": PROFILE_HASH,
            },
            source="long_link",
        )
        expected = normalize_payment_link_params(
            {
                "plan": "plus",
                "country": "BR",
                "currency": "BRL",
                "payment_link_format": "long_link",
                "payment_source": "long_link",
                "profile_hash": PROFILE_HASH,
            }
        )

        self.assertEqual(cached["link_expires_at"], 1_784_170_800)
        with mock.patch("services.chatgpt_core.payment_link_cache.time.time", return_value=1_784_170_000):
            self.assertTrue(payment_link_cache_matches(cached, expected))
            self.assertFalse(payment_link_requires_regeneration(cached))
        with mock.patch("services.chatgpt_core.payment_link_cache.time.time", return_value=1_784_170_741):
            self.assertFalse(payment_link_cache_matches(cached, expected))
            self.assertTrue(payment_link_requires_regeneration(cached))

    def test_pix_link_already_submitted_to_management_is_not_reused(self):
        cached = build_payment_link_cache_payload(
            {
                "url": "https://payments.stripe.com/qr/instructions/pix-submitted",
                "plan": "plus",
                "country": "BR",
                "currency": "BRL",
                "link_type": "pix",
                "link_status": "pix_submitted",
                "payment_link_format": "long_link",
                "payment_source": "long_link",
                "profile_hash": PROFILE_HASH,
            },
            source="long_link",
        )
        expected = normalize_payment_link_params(
            {
                "plan": "plus",
                "country": "BR",
                "currency": "BRL",
                "payment_link_format": "long_link",
                "payment_source": "long_link",
                "profile_hash": PROFILE_HASH,
            }
        )

        self.assertTrue(payment_link_requires_regeneration(cached))
        self.assertFalse(payment_link_cache_matches(cached, expected))
        refreshed = build_payment_link_cache_payload(
            {
                "url": "https://payments.stripe.com/qr/instructions/pix-fresh",
                "plan": "plus",
                "country": "BR",
                "currency": "BRL",
                "link_type": "pix",
                "payment_link_format": "long_link",
                "payment_source": "long_link",
                "profile_hash": PROFILE_HASH,
            },
            source="long_link",
            fallback=cached,
        )
        self.assertNotIn("link_status", refreshed)

    def test_plugin_uses_active_long_link_profile_without_calling_hosted_generator(self):
        account = Account(
            platform="chatgpt",
            email="paypal@example.com",
            password="pw",
            token="access-token-secret",
        )
        client = mock.Mock()
        client.get_profile.return_value = {
            "profile_hash": PROFILE_HASH,
            "link_type": "paypal",
            "country": "GB",
            "currency": "GBP",
            "profile": {},
        }
        client.submit_batch.return_value = {
            "batch_id": "batch_" + "a" * 32,
            "items": [
                {
                    "batch_id": "batch_" + "a" * 32,
                    "job_id": "job-1",
                    "request_id": "batch-1:42",
                    "profile_hash": PROFILE_HASH,
                    "status": "done",
                    "completed_at": 1_720_000_000,
                    "result": {
                        "provider_redirect_url": PAYPAL_URL,
                        "link_type": "paypal",
                        "currency": "GBP",
                        "cs_id": "cs_live_123",
                    },
                }
            ],
        }
        platform = ChatGPTPlatform(config=RegisterConfig(extra={}))

        with mock.patch(
            "services.chatgpt_core.long_link_payment_client.LongLinkPaymentClient.from_env",
            return_value=client,
        ), mock.patch("services.chatgpt_core.payment.generate_plus_link") as hosted_plus:
            result = platform.execute_action(
                "payment_link",
                account,
                {
                    "plan": "plus",
                    "payment_source": "chatgpt_hosted",
                    "payment_link_format": "short_chatgpt",
                    "country": "ID",
                    "request_id": "batch-1:42",
                },
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["url"], PAYPAL_URL)
        self.assertEqual(result["data"]["payment_link_format"], "long_link")
        self.assertEqual(result["data"]["payment_source"], "long_link")
        self.assertEqual(result["data"]["link_type"], "paypal")
        self.assertEqual(result["data"]["paypal_url"], PAYPAL_URL)
        self.assertEqual(result["data"]["profile_hash"], PROFILE_HASH)
        client.get_profile.assert_called_once_with()
        client.submit_batch.assert_called_once_with(
            items=[{"access_token": "access-token-secret", "request_id": "batch-1:42"}],
            expected_profile_hash=PROFILE_HASH,
        )
        client.get_batch.assert_not_called()
        hosted_plus.assert_not_called()


if __name__ == "__main__":
    unittest.main()
