import os
import unittest
from unittest import mock

from services.chatgpt_core.long_link_payment_client import (
    LongLinkPaymentClient,
    LongLinkPaymentError,
    clear_profile_cache,
    payment_link_from_remote_job,
)


PROFILE_HASH = "a" * 64
PAYPAL_URL = "https://www.paypal.com/agreements/approve?ba_token=BA-test"


class _Response:
    def __init__(self, status_code, data):
        self.status_code = status_code
        self._data = data

    def json(self):
        return self._data


class _Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


class LongLinkPaymentClientTests(unittest.TestCase):
    def setUp(self):
        clear_profile_cache()

    def test_reads_profile_submits_all_items_and_polls_batch(self):
        token_one = "eyJfirst.payload.signature"
        token_two = "eyJsecond.payload.signature"
        batch_id = "batch_" + "b" * 32
        session = _Session(
            [
                _Response(
                    200,
                    {
                        "ok": True,
                        "link_type": "pix",
                        "profile_hash": PROFILE_HASH,
                        "effective_concurrency": 4,
                        "profile": {"billing_country": "BR", "currency": "BRL"},
                    },
                ),
                _Response(
                    200,
                    {
                        "ok": True,
                        "batch_id": batch_id,
                        "items": [
                            {"request_id": "task:1", "batch_id": batch_id, "job_id": "job-1", "status": "queued"},
                            {"request_id": "task:2", "batch_id": batch_id, "job_id": "job-2", "status": "queued"},
                        ],
                    },
                ),
                _Response(
                    200,
                    {
                        "ok": True,
                        "batch_id": batch_id,
                        "status": "done",
                        "items": [
                            {"request_id": "task:1", "batch_id": batch_id, "job_id": "job-1", "status": "done"},
                            {"request_id": "task:2", "batch_id": batch_id, "job_id": "job-2", "status": "done"},
                        ],
                    },
                ),
            ]
        )
        client = LongLinkPaymentClient(
            base_url="http://long-link.test",
            api_key="internal-key",
            session=session,
            profile_cache_seconds=30,
        )

        profile = client.get_profile()
        submitted = client.submit_batch(
            items=[
                {"access_token": token_one, "request_id": "task:1"},
                {"access_token": token_two, "request_id": "task:2"},
            ],
            expected_profile_hash=PROFILE_HASH,
        )
        polled = client.get_batch(batch_id)

        self.assertEqual(profile["link_type"], "pix")
        self.assertEqual(profile["country"], "BR")
        self.assertEqual(profile["currency"], "BRL")
        self.assertEqual(profile["effective_concurrency"], 4)
        self.assertEqual(submitted["batch_id"], batch_id)
        self.assertEqual(polled["status"], "done")
        self.assertEqual([call[0] for call in session.calls], ["GET", "POST", "GET"])
        self.assertTrue(session.calls[0][1].endswith("/api/v1/payment-links/profile"))
        self.assertTrue(session.calls[1][1].endswith("/api/v1/payment-links/batches"))
        self.assertTrue(session.calls[2][1].endswith(f"/api/v1/payment-links/batches/{batch_id}"))
        self.assertEqual(
            session.calls[1][2]["json"],
            {
                "expectedProfileHash": PROFILE_HASH,
                "items": [
                    {"accessToken": token_one, "requestId": "task:1"},
                    {"accessToken": token_two, "requestId": "task:2"},
                ],
            },
        )
        self.assertEqual(session.calls[1][2]["headers"]["Authorization"], "Bearer internal-key")
        self.assertNotIn("X-Internal-API-Key", session.calls[1][2]["headers"])

    def test_v1_404_falls_back_once_to_legacy_routes(self):
        profile_payload = {
            "ok": True,
            "link_type": "hosted",
            "profile_hash": PROFILE_HASH,
            "effective_concurrency": 2,
            "profile": {"billing_country": "US", "currency": "USD"},
        }
        session = _Session(
            [
                _Response(404, {"detail": "Not Found"}),
                _Response(200, profile_payload),
                _Response(200, profile_payload),
            ]
        )
        client = LongLinkPaymentClient(
            base_url="http://long-link.test",
            api_key="legacy-key",
            session=session,
            profile_cache_seconds=0,
        )

        client.get_profile(force_refresh=True)
        client.get_profile(force_refresh=True)

        self.assertEqual(client.api_version, "legacy")
        self.assertEqual(len(session.calls), 3)
        self.assertTrue(session.calls[0][1].endswith("/api/v1/payment-links/profile"))
        self.assertEqual(session.calls[0][2]["headers"]["Authorization"], "Bearer legacy-key")
        self.assertTrue(session.calls[1][1].endswith("/api/internal/payment-links/profile"))
        self.assertTrue(session.calls[2][1].endswith("/api/internal/payment-links/profile"))
        self.assertEqual(session.calls[1][2]["headers"]["X-Internal-API-Key"], "legacy-key")
        self.assertNotIn("Authorization", session.calls[1][2]["headers"])

    def test_v1_auth_error_does_not_fall_back_or_echo_key(self):
        api_key = "opll_live_do-not-echo"
        session = _Session([_Response(401, {"detail": f"bad credential {api_key}"})])
        client = LongLinkPaymentClient(
            base_url="https://pay.example.test",
            api_key=api_key,
            session=session,
            profile_cache_seconds=0,
        )

        with self.assertRaises(LongLinkPaymentError) as raised:
            client.get_profile(force_refresh=True)

        self.assertEqual(len(session.calls), 1)
        self.assertNotIn(api_key, str(raised.exception))
        self.assertIn("HTTP 401", str(raised.exception))

    def test_runtime_config_precedes_environment_values(self):
        configured = {
            "openai_pay_long_link_base_url": "https://pay.example.test/",
            "openai_pay_long_link_api_key": "opll_live_shared",
        }
        with mock.patch(
            "core.config_store.config_store.get",
            side_effect=lambda key, default="": configured.get(key, default),
        ), mock.patch.dict(
            os.environ,
            {
                "OPENAI_PAY_LONG_LINK_BASE_URL": "http://legacy.internal:8788",
                "OPENAI_PAY_LONG_LINK_API_KEY": "legacy-env-key",
            },
        ):
            client = LongLinkPaymentClient.from_env()

        self.assertEqual(client.base_url, "https://pay.example.test")
        self.assertEqual(client.api_key, "opll_live_shared")

    def test_invalid_service_url_is_rejected_without_echoing_credentials(self):
        with self.assertRaises(LongLinkPaymentError) as raised:
            LongLinkPaymentClient(
                base_url="https://user:password@pay.example.test?token=secret",
                api_key="opll_live_test",
            )

        self.assertNotIn("password", str(raised.exception))
        self.assertNotIn("secret", str(raised.exception))

    def test_rejects_mismatched_batch_handles_without_echoing_access_token(self):
        token = "eyJsecret.payload.signature"
        session = _Session(
            [
                _Response(
                    200,
                    {
                        "ok": True,
                        "items": [
                            {"request_id": "another-request", "batch_id": "batch_" + "c" * 32, "job_id": "job-1"}
                        ],
                    },
                )
            ]
        )
        client = LongLinkPaymentClient(
            base_url="http://long-link.test",
            api_key="internal-key",
            session=session,
        )

        with self.assertRaises(LongLinkPaymentError) as raised:
            client.submit_batch(
                items=[{"access_token": token, "request_id": "expected-request"}],
                expected_profile_hash=PROFILE_HASH,
            )

        self.assertIn("与请求不一致", str(raised.exception))
        self.assertNotIn(token, str(raised.exception))

    def test_paypal_result_remains_generic_link_with_legacy_mirror(self):
        result = payment_link_from_remote_job(
            {
                "batch_id": "batch_" + "d" * 32,
                "job_id": "job-paypal",
                "request_id": "task:paypal",
                "status": "done",
                "profile_hash": PROFILE_HASH,
                "completed_at": 1_720_000_000,
                "result": {
                    "provider_redirect_url": PAYPAL_URL,
                    "link_type": "paypal",
                    "billing_country": "GB",
                    "currency": "GBP",
                },
            },
            profile={"profile_hash": PROFILE_HASH, "link_type": "paypal", "country": "GB", "currency": "GBP"},
        )

        self.assertEqual(result["url"], PAYPAL_URL)
        self.assertEqual(result["paypal_url"], PAYPAL_URL)
        self.assertEqual(result["link_type"], "paypal")
        self.assertEqual(result["payment_source"], "long_link")
        self.assertEqual(result["payment_link_format"], "long_link")
        self.assertEqual(result["country"], "GB")
        self.assertEqual(result["currency"], "GBP")
        self.assertTrue(result["generated_at"].endswith("+00:00"))

    def test_pix_result_preserves_provider_qr_expiry(self):
        result = payment_link_from_remote_job(
            {
                "batch_id": "batch_" + "e" * 32,
                "job_id": "job-pix",
                "request_id": "task:pix",
                "status": "done",
                "profile_hash": PROFILE_HASH,
                "completed_at": 1_720_000_000,
                "result": {
                    "long_url": "https://payments.stripe.com/qr/instructions/pix-test",
                    "link_type": "pix",
                    "billing_country": "BR",
                    "currency": "BRL",
                    "link_expires_at": 1_784_170_800,
                },
            },
            profile={"profile_hash": PROFILE_HASH, "link_type": "pix", "country": "BR", "currency": "BRL"},
        )

        self.assertEqual(result["link_expires_at"], 1_784_170_800)

    def test_upi_result_uses_qr_expiry_and_auto_classifies_payment_method(self):
        result = payment_link_from_remote_job(
            {
                "batch_id": "batch_" + "u" * 32,
                "job_id": "job-upi",
                "request_id": "task:upi",
                "status": "done",
                "profile_hash": PROFILE_HASH,
                "completed_at": 1_720_000_000,
                "result": {
                    "long_url": "https://payments.stripe.com/upi/instructions/upi-test",
                    "link_type": "hosted",
                    "payment_method_type": "upi",
                    "billing_country": "IN",
                    "currency": "INR",
                    "link_expires_at": 1_784_170_000,
                    "link_expiry_source": "checkout_session",
                    "next_action": {
                        "upi_handle_redirect_or_display_qr_code": {
                            "qr_code": {"expires_at": 1_784_170_300}
                        }
                    },
                },
            },
            profile={"profile_hash": PROFILE_HASH, "link_type": "upi", "country": "IN", "currency": "INR"},
        )

        self.assertEqual(result["link_type"], "upi")
        self.assertEqual(result["payment_method_type"], "upi")
        self.assertEqual(result["link_expires_at"], 1_784_170_300)
        self.assertEqual(result["link_expiry_source"], "upi_qr_code")

    def test_upi_url_classification_does_not_accept_checkout_session_expiry(self):
        result = payment_link_from_remote_job(
            {
                "batch_id": "batch_" + "v" * 32,
                "job_id": "job-upi-url",
                "request_id": "task:upi-url",
                "status": "done",
                "profile_hash": PROFILE_HASH,
                "completed_at": 1_720_000_000,
                "result": {
                    "long_url": "https://payments.stripe.com/upi/instructions/upi-url",
                    "link_type": "hosted",
                    "billing_country": "IN",
                    "currency": "INR",
                    "link_expires_at": 1_784_256_400,
                    "link_expiry_source": "checkout_session",
                },
            },
            profile={"profile_hash": PROFILE_HASH, "link_type": "hosted", "country": "IN", "currency": "INR"},
        )

        self.assertEqual(result["link_type"], "upi")
        self.assertNotIn("link_expires_at", result)
        self.assertNotIn("link_expiry_source", result)

    def test_team_profile_and_batch_send_identical_profile_overrides(self):
        token = "eyJteam.payload.signature"
        overrides = {
            "plan": "team",
            "team_plan_data": {
                "workspace_name": "Client Workspace",
                "price_interval": "year",
                "seat_quantity": 7,
            },
            "promo_code": "TEAM50",
        }
        batch_id = "batch_" + "f" * 32
        session = _Session(
            [
                _Response(
                    200,
                    {
                        "ok": True,
                        "link_type": "team",
                        "profile_hash": PROFILE_HASH,
                        "profile": {
                            "link_type": "team",
                            "plan": "team",
                            "generation_kind": "team_checkout",
                            "billing_country": "GB",
                            "currency": "GBP",
                            "team": {
                                "workspace_name": "Client Workspace",
                                "price_interval": "year",
                                "seat_quantity": 7,
                            },
                        },
                    },
                ),
                _Response(
                    200,
                    {
                        "ok": True,
                        "batch_id": batch_id,
                        "items": [
                            {
                                "request_id": "task:team:client",
                                "batch_id": batch_id,
                                "job_id": "job-team-client",
                                "status": "queued",
                            }
                        ],
                    },
                ),
            ]
        )
        client = LongLinkPaymentClient(
            base_url="http://long-link.test",
            api_key="internal-key",
            session=session,
        )

        profile = client.get_profile(overrides=overrides)
        submitted = client.submit_batch(
            items=[{"access_token": token, "request_id": "task:team:client"}],
            expected_profile_hash=PROFILE_HASH,
            profile_overrides=overrides,
        )

        self.assertEqual(profile["plan"], "team")
        self.assertEqual(profile["team"]["workspace_name"], "Client Workspace")
        self.assertEqual(submitted["batch_id"], batch_id)
        self.assertEqual([call[0] for call in session.calls], ["POST", "POST"])
        self.assertEqual(session.calls[0][2]["json"], {"profileOverrides": overrides})
        self.assertEqual(session.calls[1][2]["json"]["profileOverrides"], overrides)


if __name__ == "__main__":
    unittest.main()
