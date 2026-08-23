import unittest
from unittest import mock

from api.actions import _apply_action_result
from core.base_platform import Account, RegisterConfig
from core.db import AccountModel
from services.chatgpt_core.payment_link_cache import (
    build_payment_link_cache_payload,
    gcash_payment_link_effective_expires_at,
    latest_gcash_payment_link_variant,
    normalize_payment_link_params,
    payment_link_cache_for_params,
    payment_link_cache_matches,
    store_payment_link_variant,
    payment_link_requires_regeneration,
)
from services.chatgpt_core.plugin import ChatGPTPlatform


PROFILE_HASH = "profile-hash-123"
PAYPAL_URL = "https://www.paypal.com/agreements/approve?ba_token=BA-123"
GCASH_URL_OLD = (
    "https://checkoutshopper-live.adyen.com/checkoutshopper/checkoutPaymentRedirect"
    "?redirectData=SIGNED_OLD"
)
GCASH_URL_NEW = (
    "https://checkoutshopper-live.adyen.com/checkoutshopper/checkoutPaymentRedirect"
    "?redirectData=SIGNED_NEW"
)


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

    def test_short_and_long_cache_variants_are_independent(self):
        short_url = "https://chatgpt.com/checkout/openai_llc/cs_live_short_variant"
        long_url = "https://pay.openai.com/c/pay/cs_live_long_variant#fid"
        short_cache = build_payment_link_cache_payload(
            {
                "url": short_url,
                "plan": "plus",
                "country": "US",
                "currency": "USD",
                "payment_source": "chatgpt_hosted",
                "payment_link_format": "short_chatgpt",
            },
            source="chatgpt_short",
        )
        long_cache = build_payment_link_cache_payload(
            {
                "url": long_url,
                "plan": "plus",
                "country": "US",
                "currency": "USD",
                "payment_source": "long_link",
                "payment_link_format": "long_link",
                "profile_hash": PROFILE_HASH,
            },
            source="long_link",
        )
        extra = {}
        store_payment_link_variant(extra, short_cache, make_current=False)
        store_payment_link_variant(extra, long_cache, make_current=False)

        short_params = normalize_payment_link_params(
            {
                "plan": "plus",
                "country": "US",
                "currency": "USD",
                "payment_source": "chatgpt_hosted",
                "payment_link_format": "short_chatgpt",
            }
        )
        long_params = normalize_payment_link_params(
            {
                "plan": "plus",
                "country": "US",
                "currency": "USD",
                "payment_source": "long_link",
                "payment_link_format": "long_link",
                "profile_hash": PROFILE_HASH,
            }
        )

        self.assertEqual(payment_link_cache_for_params(extra, short_params)["url"], short_url)
        self.assertEqual(payment_link_cache_for_params(extra, long_params)["url"], long_url)
        self.assertNotEqual(short_cache["variant_key"], long_cache["variant_key"])

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

    def test_upi_cache_is_classified_from_payment_method_and_uses_qr_expiry(self):
        cached = build_payment_link_cache_payload(
            {
                "url": "https://payments.stripe.com/upi/instructions/upi-cache",
                "plan": "plus",
                "country": "IN",
                "currency": "INR",
                "link_type": "hosted",
                "payment_method_type": "upi",
                "link_expires_at": 1_784_170_000,
                "link_expiry_source": "checkout_session",
                "next_action": {
                    "upi_handle_redirect_or_display_qr_code": {
                        "qr_code": {"expires_at": 1_784_170_300}
                    }
                },
                "payment_link_format": "long_link",
                "payment_source": "long_link",
                "profile_hash": PROFILE_HASH,
            },
            source="long_link",
        )

        self.assertEqual(cached["link_type"], "upi")
        self.assertEqual(cached["link_expires_at"], 1_784_170_300)
        self.assertEqual(cached["link_expiry_source"], "upi_qr_code")
        expected = normalize_payment_link_params(
            {
                "plan": "plus",
                "country": "IN",
                "currency": "INR",
                "payment_link_format": "long_link",
                "payment_source": "long_link",
                "profile_hash": PROFILE_HASH,
            }
        )
        with mock.patch("services.chatgpt_core.payment_link_cache.time.time", return_value=1_784_169_900):
            self.assertTrue(payment_link_cache_matches(cached, expected))
        with mock.patch("services.chatgpt_core.payment_link_cache.time.time", return_value=1_784_170_241):
            self.assertFalse(payment_link_cache_matches(cached, expected))

    def test_upi_url_overrides_generic_hosted_type_and_rejects_checkout_expiry(self):
        cached = build_payment_link_cache_payload(
            {
                "url": "https://payments.stripe.com/upi/instructions/upi-url-only",
                "plan": "plus",
                "country": "IN",
                "currency": "INR",
                "link_type": "hosted",
                "link_expires_at": 1_784_256_400,
                "link_expiry_source": "checkout_session",
                "payment_link_format": "long_link",
                "payment_source": "long_link",
                "profile_hash": PROFILE_HASH,
            },
            source="long_link",
        )

        self.assertEqual(cached["link_type"], "upi")
        self.assertNotIn("link_expires_at", cached)
        self.assertNotIn("link_expiry_source", cached)

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

    def test_gcash_cache_keeps_separate_deadlines_without_inheriting_old_remote_identity(self):
        old = build_payment_link_cache_payload(
            {
                "url": GCASH_URL_OLD,
                "provider_redirect_url": GCASH_URL_OLD,
                "link_type": "gcash",
                "country": "PH",
                "currency": "PHP",
                "payment_link_format": "long_link",
                "payment_source": "long_link",
                "profile_hash": "gcash-profile-old",
                "generated_at": "2026-08-20T00:00:00Z",
                "link_expires_at": 4_102_444_800,
                "link_expiry_source": "gcash_provider_redirect",
                "gcash_qr_payload": "GCashOldPayload_1234",
                "gcash_qr_expires_at": 4_102_444_600,
                "remote_batch_id": "batch-old",
                "remote_job_id": "job-old",
                "remote_request_id": "request-old",
            },
            source="long_link",
        )
        refreshed = build_payment_link_cache_payload(
            {
                "url": GCASH_URL_NEW,
                "provider_redirect_url": GCASH_URL_NEW,
                "link_type": "gcash",
                "country": "PH",
                "currency": "PHP",
                "payment_link_format": "long_link",
                "payment_source": "long_link",
                "profile_hash": "gcash-profile-new",
                "generated_at": "2026-08-21T00:00:00Z",
                "link_expires_at": 4_102_445_800,
                "link_expiry_source": "gcash_provider_redirect",
                "gcash_qr_payload": "GCashNewPayload_5678",
                "gcash_qr_expires_at": 4_102_445_300,
            },
            source="long_link",
            fallback=old,
        )

        self.assertEqual(refreshed["url"], GCASH_URL_NEW)
        self.assertEqual(refreshed["link_expires_at"], 4_102_445_800)
        self.assertEqual(refreshed["gcash_qr_expires_at"], 4_102_445_300)
        self.assertEqual(refreshed["gcash_qr_payload"], "GCashNewPayload_5678")
        self.assertEqual(gcash_payment_link_effective_expires_at(refreshed), 4_102_445_300)
        self.assertNotIn("remote_batch_id", refreshed)
        self.assertNotIn("remote_job_id", refreshed)
        self.assertNotIn("remote_request_id", refreshed)

    def test_latest_gcash_variant_survives_a_newer_current_paypal_link(self):
        old = build_payment_link_cache_payload(
            {
                "url": GCASH_URL_OLD,
                "link_type": "gcash",
                "country": "PH",
                "currency": "PHP",
                "payment_link_format": "long_link",
                "payment_source": "long_link",
                "profile_hash": "gcash-profile-old",
                "generated_at": "2026-08-20T00:00:00Z",
                "link_expires_at": 4_102_444_800,
            },
            source="long_link",
        )
        latest = build_payment_link_cache_payload(
            {
                "url": GCASH_URL_NEW,
                "link_type": "gcash",
                "country": "PH",
                "currency": "PHP",
                "payment_link_format": "long_link",
                "payment_source": "long_link",
                "profile_hash": "gcash-profile-new",
                "generated_at": "2026-08-21T00:00:00Z",
                "link_expires_at": 4_102_445_800,
            },
            source="long_link",
        )
        paypal = build_payment_link_cache_payload(
            {
                "url": PAYPAL_URL,
                "link_type": "paypal",
                "country": "GB",
                "currency": "GBP",
                "payment_link_format": "long_link",
                "payment_source": "long_link",
                "profile_hash": "paypal-profile",
                "generated_at": "2026-08-22T00:00:00Z",
            },
            source="long_link",
        )
        extra = {"cashier_url": "https://unrelated.example/checkout"}
        store_payment_link_variant(extra, old, make_current=False)
        store_payment_link_variant(extra, latest, make_current=False)
        store_payment_link_variant(extra, paypal, make_current=True)

        selected = latest_gcash_payment_link_variant(extra)

        self.assertEqual(selected["url"], GCASH_URL_NEW)
        self.assertEqual(selected["link_type"], "gcash")
        self.assertEqual(extra["chatgpt_last_payment_link"]["link_type"], "paypal")

    def test_gcash_cache_rejects_non_provider_url(self):
        cached = build_payment_link_cache_payload(
            {
                "url": "https://evil.example/gcash?redirectData=fake",
                "link_type": "gcash",
                "country": "PH",
                "currency": "PHP",
                "payment_link_format": "long_link",
                "payment_source": "long_link",
            },
            source="long_link",
        )

        self.assertEqual(cached, {})

    def test_plugin_restores_login_bound_short_link_without_calling_long_link_service(self):
        account = Account(
            platform="chatgpt",
            email="paypal@example.com",
            password="pw",
            token="access-token-secret",
            extra={"cookies": "__Secure-next-auth.session-token=web-session"},
        )
        short_url = "https://chatgpt.com/checkout/openai_llc/cs_live_short"
        platform = ChatGPTPlatform(config=RegisterConfig(extra={}))

        with mock.patch(
            "services.chatgpt_core.long_link_payment_client.LongLinkPaymentClient.from_env",
        ) as long_client, mock.patch(
            "services.chatgpt_core.payment.generate_plus_short_link",
            return_value=short_url,
        ) as short_generator:
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
        self.assertEqual(result["data"]["url"], short_url)
        self.assertEqual(result["data"]["payment_link_format"], "short_chatgpt")
        self.assertEqual(result["data"]["payment_source"], "chatgpt_hosted")
        self.assertEqual(result["data"]["link_type"], "chatgpt")
        self.assertTrue(result["data"]["login_required"])
        self.assertEqual(result["data"]["remote_request_id"], "batch-1:42")
        short_generator.assert_called_once()
        long_client.assert_not_called()

    def test_plugin_rejects_short_link_without_web_session(self):
        account = Account(
            platform="chatgpt",
            email="no-session@example.com",
            password="pw",
            token="access-token-secret",
        )
        platform = ChatGPTPlatform(config=RegisterConfig(extra={}))

        with mock.patch("services.chatgpt_core.payment.generate_plus_short_link") as short_generator:
            result = platform.execute_action(
                "payment_link",
                account,
                {
                    "plan": "plus",
                    "payment_source": "chatgpt_hosted",
                    "payment_link_format": "short_chatgpt",
                },
            )

        self.assertFalse(result["ok"])
        self.assertIn("Web Session", result["error"])
        short_generator.assert_not_called()


if __name__ == "__main__":
    unittest.main()
