import unittest

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
        self.assertTrue(session.calls[0][1].endswith("/api/internal/payment-links/profile"))
        self.assertTrue(session.calls[1][1].endswith("/api/internal/payment-links/batches"))
        self.assertTrue(session.calls[2][1].endswith(f"/api/internal/payment-links/batches/{batch_id}"))
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
        self.assertEqual(session.calls[1][2]["headers"]["X-Internal-API-Key"], "internal-key")

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


if __name__ == "__main__":
    unittest.main()
