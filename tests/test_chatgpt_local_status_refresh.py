import unittest
from types import SimpleNamespace
from unittest import mock

from services.chatgpt_core import local_status_refresh


def _probe(*, plan: str, active_until: str = "", auth_state: str = "refresh_token_valid") -> dict:
    return {
        "auth": {"state": auth_state},
        "subscription": {
            "plan": plan,
            "subscription_active_until": active_until,
        },
    }


class ChatGPTLocalStatusRefreshTests(unittest.TestCase):
    def _refresh(self, probes: list[dict]):
        with mock.patch.object(local_status_refresh, "probe_local_chatgpt_status", side_effect=probes) as probe_mock, mock.patch.object(
            local_status_refresh.time,
            "sleep",
        ) as sleep_mock:
            result = local_status_refresh._probe_local_status_with_subscription_retry(
                SimpleNamespace(),
                proxy=None,
                use_default_proxy=True,
            )
        return result, probe_mock, sleep_mock

    def test_unknown_plan_retries_and_persists_resolved_plus_subscription(self):
        result, probe_mock, sleep_mock = self._refresh(
            [
                _probe(plan="unknown"),
                _probe(plan="plus", active_until="2026-08-23T00:00:00Z"),
            ]
        )

        self.assertEqual(probe_mock.call_count, 2)
        sleep_mock.assert_called_once_with(local_status_refresh._SUBSCRIPTION_RETRY_DELAY_SECONDS)
        self.assertEqual(result["subscription"]["plan"], "plus")
        self.assertEqual(result["subscription"]["refresh_attempts"], 2)
        self.assertEqual(result["subscription"]["retry_reason"], "subscription_plan_unknown")
        self.assertEqual(result["subscription"]["retry_outcome"], "resolved")

    def test_paid_subscription_without_expiry_retries_once(self):
        result, probe_mock, sleep_mock = self._refresh(
            [
                _probe(plan="plus"),
                _probe(plan="plus", active_until="2026-08-23T00:00:00Z"),
            ]
        )

        self.assertEqual(probe_mock.call_count, 2)
        sleep_mock.assert_called_once_with(local_status_refresh._SUBSCRIPTION_RETRY_DELAY_SECONDS)
        self.assertEqual(result["subscription"]["subscription_active_until"], "2026-08-23T00:00:00Z")
        self.assertEqual(result["subscription"]["retry_reason"], "subscription_expiry_missing")
        self.assertEqual(result["subscription"]["retry_outcome"], "resolved")

    def test_unknown_plan_stays_unknown_after_one_bounded_retry(self):
        result, probe_mock, sleep_mock = self._refresh([_probe(plan="unknown"), _probe(plan="unknown")])

        self.assertEqual(probe_mock.call_count, 2)
        sleep_mock.assert_called_once_with(local_status_refresh._SUBSCRIPTION_RETRY_DELAY_SECONDS)
        self.assertEqual(result["subscription"]["plan"], "unknown")
        self.assertEqual(result["subscription"]["refresh_attempts"], 2)
        self.assertEqual(result["subscription"]["retry_outcome"], "still_incomplete")

    def test_confirmed_or_invalid_probe_does_not_retry(self):
        confirmed, confirmed_calls, confirmed_sleep = self._refresh(
            [_probe(plan="plus", active_until="2026-08-23T00:00:00Z")]
        )
        self.assertEqual(confirmed["subscription"]["plan"], "plus")
        confirmed_calls.assert_called_once()
        confirmed_sleep.assert_not_called()

        invalid, invalid_calls, invalid_sleep = self._refresh(
            [_probe(plan="unknown", auth_state="refresh_token_invalidated")]
        )
        self.assertEqual(invalid["subscription"]["plan"], "unknown")
        invalid_calls.assert_called_once()
        invalid_sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
