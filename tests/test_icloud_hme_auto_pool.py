"""Tests for iCloud HME 自动补池退避节奏。"""

import unittest
from unittest import mock

import services.icloud_hme_auto_pool as auto_pool
from services.icloud_hme_auto_pool import IcloudHmeAutoPoolConfig


def _make_config(**overrides):
    base = dict(
        enabled=True,
        stock_limit=10,
        interval_min_minutes=1,
        interval_max_minutes=1,
        rate_limit_backoff_minutes=60,
        error_backoff_minutes=1,
        icloud_cookie="ck",
        icloud_domain_base="icloud.com",
        forward_to="b@cccy.me",
        forward_mailbox_id="",
        tempmail_api_url="http://mail.local",
        tempmail_api_key="key",
        tempmail_api_key_header="Authorization",
    )
    base.update(overrides)
    return IcloudHmeAutoPoolConfig(**base)


class IcloudHmeAutoPoolBackoffTests(unittest.TestCase):
    def setUp(self):
        auto_pool._rate_limit_until = 0.0
        auto_pool._error_backoff_until = 0.0
        auto_pool._consecutive_error_count = 0
        auto_pool._next_run_at = 0.0
        auto_pool._last_error = ""
        auto_pool._last_backoff_reason = ""
        auto_pool._stop_event.clear()

    def test_create_error_uses_short_backoff(self):
        with (
            mock.patch.object(auto_pool, "get_icloud_hme_auto_pool_config", return_value=_make_config()),
            mock.patch.object(auto_pool, "_ready_count", return_value=0),
            mock.patch.object(auto_pool, "_create_one_alias", side_effect=TimeoutError("temporary network error")),
            mock.patch.object(auto_pool, "_now", return_value=1000.0),
        ):
            result = auto_pool.run_once(force=True)

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["reason"], "error")
        self.assertEqual(auto_pool._error_backoff_until, 1060.0)
        self.assertEqual(auto_pool._next_run_at, 1060.0)
        self.assertEqual(auto_pool._consecutive_error_count, 1)

    def test_create_success_clears_short_backoff(self):
        auto_pool._error_backoff_until = 1060.0
        auto_pool._consecutive_error_count = 2
        auto_pool._last_backoff_reason = "普通错误短退避"

        with (
            mock.patch.object(auto_pool, "get_icloud_hme_auto_pool_config", return_value=_make_config()),
            mock.patch.object(auto_pool, "_ready_count", side_effect=[0, 1]),
            mock.patch.object(auto_pool, "_create_one_alias", return_value={"hme": "a@icloud.com", "anonymous_id": "a"}),
            mock.patch.object(auto_pool, "_now", return_value=1000.0),
        ):
            result = auto_pool.run_once(force=True)

        self.assertTrue(result["ok"], result)
        self.assertEqual(auto_pool._error_backoff_until, 0.0)
        self.assertEqual(auto_pool._consecutive_error_count, 0)
        self.assertEqual(auto_pool._last_backoff_reason, "")

    def test_rate_limit_uses_long_backoff(self):
        from core.base_mailbox import ICloudAliasLimitError

        with (
            mock.patch.object(auto_pool, "get_icloud_hme_auto_pool_config", return_value=_make_config(rate_limit_backoff_minutes=1)),
            mock.patch.object(auto_pool, "_ready_count", return_value=0),
            mock.patch.object(auto_pool, "_create_one_alias", side_effect=ICloudAliasLimitError("rate limit", retry_after=120)),
            mock.patch.object(auto_pool, "_now", return_value=1000.0),
        ):
            result = auto_pool.run_once(force=True)

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["reason"], "rate_limited")
        self.assertEqual(auto_pool._rate_limit_until, 1120.0)
        self.assertEqual(auto_pool._next_run_at, 1120.0)


if __name__ == "__main__":
    unittest.main()
