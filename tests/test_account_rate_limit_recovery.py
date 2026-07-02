from datetime import datetime, timedelta, timezone
import unittest

from core.db import AccountModel
from services.account_rate_limit_recovery import (
    RATE_LIMIT_RECOVERY_SECONDS,
    account_rate_limit_payload,
    mark_account_rate_limited,
    reconcile_account_rate_limit,
)


class AccountRateLimitRecoveryTests(unittest.TestCase):
    def _account(self, *, status: str = "registered") -> AccountModel:
        return AccountModel(
            platform="chatgpt",
            email="rate-limited@example.com",
            password="pw",
            status=status,
        )

    def test_mark_account_rate_limited_sets_one_hour_recover_at(self):
        now = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)
        account = self._account(status="pending_payment")

        mark_account_rate_limited(account, now=now)

        self.assertEqual(account.status, "rate_limited")
        extra = account.get_extra()
        self.assertEqual(extra["rate_limit_started_at"], "2026-06-05T12:00:00Z")
        self.assertEqual(extra["rate_limit_recover_at"], "2026-06-05T13:00:00Z")
        self.assertEqual(extra["rate_limit_previous_status"], "pending_payment")
        payload = account_rate_limit_payload(account, now=now)
        self.assertEqual(payload["seconds_remaining"], RATE_LIMIT_RECOVERY_SECONDS)

    def test_reconcile_recovers_expired_rate_limited_account_to_previous_status(self):
        now = datetime(2026, 6, 5, 14, 30, 0, tzinfo=timezone.utc)
        account = self._account(status="rate_limited")
        account.set_extra(
            {
                "rate_limit_started_at": "2026-06-05T12:00:00Z",
                "rate_limit_recover_at": "2026-06-05T13:00:00Z",
                "rate_limit_previous_status": "subscribed",
            }
        )

        changed = reconcile_account_rate_limit(account, now=now)

        self.assertTrue(changed)
        self.assertEqual(account.status, "subscribed")
        extra = account.get_extra()
        self.assertNotIn("rate_limit_recover_at", extra)
        self.assertEqual(extra["rate_limit_recovered_at"], "2026-06-05T14:30:00Z")
        self.assertEqual(extra["rate_limit_last_previous_status"], "subscribed")

    def test_reconcile_backfills_recover_at_from_updated_at_when_missing(self):
        now = datetime(2026, 6, 5, 12, 30, 0, tzinfo=timezone.utc)
        account = self._account(status="rate_limited")
        account.updated_at = now - timedelta(minutes=10)
        account.set_extra({})

        changed = reconcile_account_rate_limit(account, now=now)

        self.assertTrue(changed)
        self.assertEqual(account.status, "rate_limited")
        extra = account.get_extra()
        self.assertEqual(extra["rate_limit_started_at"], "2026-06-05T12:20:00Z")
        self.assertEqual(extra["rate_limit_recover_at"], "2026-06-05T13:20:00Z")
        self.assertEqual(extra["rate_limit_previous_status"], "registered")


if __name__ == "__main__":
    unittest.main()
