import json
import unittest
from unittest import mock

from fastapi import HTTPException

from api.tasks import (
    BatchCustomEmailRecheckTaskRequest,
    CustomEmailRecheckTaskRequest,
    _build_custom_email_recheck_candidate_proxies,
    _custom_email_account_delay_seconds,
    _custom_email_proxy_settings,
    _normalize_custom_email_batch,
)


class CustomEmailRecheckBatchImportTests(unittest.TestCase):
    def test_normalize_email_list_keeps_existing_behavior(self):
        req = BatchCustomEmailRecheckTaskRequest(
            raw_emails="Alice@example.com bob@example.com invalid-email Alice@example.com",
        )

        emails, skipped, source_summary = _normalize_custom_email_batch(req)

        self.assertEqual(emails, ["alice@example.com", "bob@example.com"])
        self.assertEqual(source_summary["format"], "email_list")
        self.assertEqual(source_summary["email_count"], 2)
        self.assertEqual(len(skipped), 2)
        self.assertEqual(skipped[0]["reason"], "邮箱格式不合法")
        self.assertEqual(skipped[1]["reason"], "重复邮箱")

    def test_normalize_sub2api_json_extracts_emails(self):
        payload = {
            "exported_at": "2026-06-12T05:00:00Z",
            "accounts": [
                {"name": "alpha@example.com", "extra": {"email": "alpha@example.com"}},
                {"extra": {"email": "beta@example.com"}},
                {"email": "bad-email"},
                {"name": "alpha@example.com"},
                {},
                "not-an-object",
            ],
        }
        req = BatchCustomEmailRecheckTaskRequest(
            source_format="sub2api_json",
            source_text=json.dumps(payload, ensure_ascii=False),
            source_filename="sub2api-export.json",
        )

        emails, skipped, source_summary = _normalize_custom_email_batch(req)

        self.assertEqual(emails, ["alpha@example.com", "beta@example.com"])
        self.assertEqual(source_summary["format"], "sub2api_json")
        self.assertEqual(source_summary["account_count"], 6)
        self.assertEqual(source_summary["source_filename"], "sub2api-export.json")
        self.assertEqual(source_summary["email_count"], 2)
        reasons = [item["reason"] for item in skipped]
        self.assertIn("邮箱格式不合法", reasons)
        self.assertIn("重复邮箱", reasons)
        self.assertIn("未找到邮箱字段", reasons)
        self.assertIn("条目不是对象", reasons)

    def test_normalize_sub2api_json_rejects_invalid_json(self):
        req = BatchCustomEmailRecheckTaskRequest(
            source_format="sub2api_json",
            source_text="{not-json}",
        )

        with self.assertRaises(HTTPException) as ctx:
            _normalize_custom_email_batch(req)

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("合法 JSON", str(ctx.exception.detail))

    def test_custom_email_proxy_defaults_keep_legacy_direct_api_behavior(self):
        settings = _custom_email_proxy_settings(
            CustomEmailRecheckTaskRequest(email="alive@example.com"),
        )

        self.assertEqual(settings["proxy_mode"], "direct")
        self.assertEqual(
            _build_custom_email_recheck_candidate_proxies(settings),
            [("", None, "direct")],
        )

    def test_custom_email_proxy_specified_failover_appends_pool_candidates(self):
        settings = _custom_email_proxy_settings(
            CustomEmailRecheckTaskRequest(
                email="alive@example.com",
                proxy="http://manual-proxy:18080",
                proxy_mode="specified",
                proxy_failover=True,
                proxy_country_code="us",
                proxy_max_candidates=2,
                proxy_min_score=70,
            ),
        )

        with mock.patch(
            "core.proxy_pool.proxy_pool.get_candidate_records",
            return_value=[
                {
                    "url": "http://pool-proxy:18080",
                    "exit_country_code": "US",
                    "health_score": 91,
                    "latency_ms": 120,
                }
            ],
        ) as get_candidates:
            candidates = _build_custom_email_recheck_candidate_proxies(settings)

        get_candidates.assert_called_once_with(
            target="chatgpt",
            country_code="US",
            limit=2,
            min_score=70,
        )
        self.assertEqual(candidates[0], ("http://manual-proxy:18080", None, "specified"))
        self.assertEqual(candidates[1][0], "http://pool-proxy:18080")
        self.assertIn("pool country=US", candidates[1][2])

    def test_custom_email_account_delay_seconds_is_clamped(self):
        self.assertEqual(_custom_email_account_delay_seconds(-1), 0)
        self.assertEqual(_custom_email_account_delay_seconds(""), 0)
        self.assertEqual(_custom_email_account_delay_seconds("1.5"), 1.5)
        self.assertEqual(_custom_email_account_delay_seconds(999), 600)


if __name__ == "__main__":
    unittest.main()
