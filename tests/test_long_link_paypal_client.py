import unittest

from services.chatgpt_core.long_link_paypal_client import (
    LongLinkPayPalClient,
    LongLinkPayPalError,
    clear_profile_cache,
)


PROFILE_HASH = "profile-hash-123"
PAYPAL_URL = "https://www.paypal.com/agreements/approve?ba_token=BA-123"


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


class LongLinkPayPalClientTests(unittest.TestCase):
    def setUp(self):
        clear_profile_cache()

    def _profile_response(self):
        return _Response(
            200,
            {
                "ok": True,
                "link_type": "paypal",
                "profile_hash": PROFILE_HASH,
                "profile": {"billing_country": "GB", "currency": "GBP"},
            },
        )

    def test_generate_paypal_link_runs_profile_start_and_poll(self):
        token = "eyJsecret.payload.signature"
        session = _Session(
            [
                self._profile_response(),
                _Response(
                    200,
                    {
                        "job_id": "job-1",
                        "request_id": "request-1",
                        "status": "queued",
                        "profile_hash": PROFILE_HASH,
                    },
                ),
                _Response(200, {"job_id": "job-1", "status": "running", "profile_hash": PROFILE_HASH}),
                _Response(
                    200,
                    {
                        "job_id": "job-1",
                        "request_id": "request-1",
                        "status": "done",
                        "profile_hash": PROFILE_HASH,
                        "result": {
                            "provider_redirect_url": PAYPAL_URL,
                            "long_url": PAYPAL_URL,
                            "cs_id": "cs_live_123",
                            "payment_method_id": "pm_123",
                            "checkout_amount": "0",
                            "checkout_amount_is_zero": True,
                        },
                    },
                ),
            ]
        )
        client = LongLinkPayPalClient(
            base_url="http://long-link.test",
            api_key="internal-key",
            session=session,
            poll_interval=0,
            sleep=lambda _: None,
        )

        result = client.generate_paypal_link(access_token=token, request_id="request-1")

        self.assertEqual(result["url"], PAYPAL_URL)
        self.assertEqual(result["paypal_url"], PAYPAL_URL)
        self.assertEqual(result["payment_source"], "long_link_paypal")
        self.assertEqual(result["profile_hash"], PROFILE_HASH)
        self.assertEqual(result["country"], "GB")
        self.assertEqual(result["currency"], "GBP")
        self.assertEqual(result["cs_id"], "cs_live_123")
        start_payload = session.calls[1][2]["json"]
        self.assertEqual(start_payload["access_token"], token)
        self.assertEqual(start_payload["expected_profile_hash"], PROFILE_HASH)
        self.assertNotIn(token, repr(result))

    def test_upstream_error_redacts_access_token(self):
        token = "eyJsecret.payload.signature"
        session = _Session([_Response(500, {"detail": f"invalid token {token}"})])
        client = LongLinkPayPalClient(
            base_url="http://long-link.test",
            api_key="internal-key",
            session=session,
        )

        with self.assertRaises(LongLinkPayPalError) as raised:
            client.start(access_token=token, request_id="request-1", expected_profile_hash=PROFILE_HASH)

        self.assertNotIn(token, str(raised.exception))
        self.assertIn("***", str(raised.exception))

    def test_profile_cache_avoids_repeated_requests(self):
        session = _Session([self._profile_response()])
        client = LongLinkPayPalClient(
            base_url="http://long-link.test",
            api_key="internal-key",
            session=session,
            profile_cache_seconds=30,
        )

        first = client.get_profile()
        second = client.get_profile()

        self.assertEqual(first, second)
        self.assertEqual(len(session.calls), 1)


if __name__ == "__main__":
    unittest.main()
