import unittest
from unittest import mock

from services.chatgpt_core import local_status_refresh
from services.chatgpt_core.codex_usage import (
    account_has_codex_auth_material,
    build_codex_usage_extra_updates,
    build_codex_usage_progress_from_extra,
    parse_codex_rate_limit_headers,
    parse_codex_usage_body,
    probe_codex_usage_window,
    refresh_codex_usage_for_saved_account,
)
from services.chatgpt_core.local_status_refresh import (
    account_has_local_status_auth_material,
    schedule_chatgpt_local_status_refresh_for_account_id,
)
from services.chatgpt_core.status_probe import ProbeHTTPResult


class DummyAccount:
    def __init__(self, *, access_token="", refresh_token="", extra=None, user_id="acct-123"):
        self.email = "demo@example.com"
        self.token = access_token
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.user_id = user_id
        self.extra = dict(extra or {})


class SavedAccount(DummyAccount):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.id = 123
        self.platform = "chatgpt"
        self.password = "pw"
        self.status = "registered"
        self.updated_at = None
        self.extra_json = "{}"

    def get_extra(self):
        return dict(self.extra)

    def set_extra(self, extra):
        self.extra = dict(extra or {})


class FakeSession:
    def __init__(self):
        self.added = []
        self.commits = 0
        self.refreshes = 0

    def add(self, account):
        self.added.append(account)

    def commit(self):
        self.commits += 1

    def refresh(self, account):
        self.refreshes += 1


def _codex_response(headers):
    return ProbeHTTPResult(
        status_code=200,
        headers=headers,
        body_text="",
        body_json={},
        error_code="",
        message="ok",
    )


class CodexUsageTests(unittest.TestCase):
    def setUp(self):
        self.proxy_patcher = mock.patch(
            "services.chatgpt_core.codex_usage._resolve_effective_probe_proxy",
            return_value=("", "direct"),
        )
        self.proxy_patcher.start()

    def tearDown(self):
        self.proxy_patcher.stop()

    def test_header_parser_normalizes_primary_7d_secondary_5h(self):
        snapshot = parse_codex_rate_limit_headers(
            {
                "x-codex-primary-used-percent": "37.5",
                "x-codex-primary-reset-after-seconds": "604800",
                "x-codex-primary-window-minutes": "10080",
                "x-codex-secondary-used-percent": "10",
                "x-codex-secondary-reset-after-seconds": "3600",
                "x-codex-secondary-window-minutes": "300",
            },
            updated_at="2026-06-14T00:00:00+00:00",
        )
        usage = build_codex_usage_extra_updates(snapshot, "2026-06-14T00:00:00+00:00")

        self.assertEqual(usage["codex_7d_used_percent"], 37.5)
        self.assertEqual(usage["codex_7d_window_minutes"], 10080)
        self.assertEqual(usage["codex_5h_used_percent"], 10.0)
        self.assertEqual(usage["codex_5h_window_minutes"], 300)
        self.assertEqual(usage["codex_7d_remaining_percent"], 62.5)
        self.assertEqual(usage["codex_5h_remaining_percent"], 90.0)
        self.assertIn("codex_7d_reset_at", usage)
        self.assertIn("codex_5h_reset_at", usage)

    def test_header_parser_normalizes_primary_5h_secondary_7d(self):
        snapshot = parse_codex_rate_limit_headers(
            {
                "x-codex-primary-used-percent": "100",
                "x-codex-primary-reset-after-seconds": "1800",
                "x-codex-primary-window-minutes": "300",
                "x-codex-secondary-used-percent": "20",
                "x-codex-secondary-reset-after-seconds": "500000",
                "x-codex-secondary-window-minutes": "10080",
            },
            updated_at="2026-06-14T00:00:00+00:00",
        )
        usage = build_codex_usage_extra_updates(snapshot, "2026-06-14T00:00:00+00:00")

        self.assertEqual(usage["codex_5h_used_percent"], 100.0)
        self.assertEqual(usage["codex_5h_remaining_percent"], 0.0)
        self.assertEqual(usage["codex_7d_used_percent"], 20.0)
        self.assertEqual(usage["codex_7d_remaining_percent"], 80.0)

    def test_header_parser_falls_back_primary_7d_secondary_5h_without_windows(self):
        snapshot = parse_codex_rate_limit_headers(
            {
                "x-codex-primary-used-percent": "12",
                "x-codex-secondary-used-percent": "34",
            },
            updated_at="2026-06-14T00:00:00+00:00",
        )
        usage = build_codex_usage_extra_updates(snapshot, "2026-06-14T00:00:00+00:00")

        self.assertEqual(usage["codex_7d_used_percent"], 12.0)
        self.assertEqual(usage["codex_7d_window_minutes"], 10080)
        self.assertEqual(usage["codex_5h_used_percent"], 34.0)
        self.assertEqual(usage["codex_5h_window_minutes"], 300)

    def test_wham_usage_body_parser_extracts_rate_limit_windows(self):
        snapshot = parse_codex_usage_body(
            {
                "rate_limit": {
                    "allowed": True,
                    "limit_reached": False,
                    "primary_window": {
                        "used_percent": 5,
                        "limit_window_seconds": 2592000,
                        "reset_after_seconds": 2592000,
                        "reset_at": 1784068802,
                    },
                    "secondary_window": None,
                }
            },
            updated_at="2026-06-14T00:00:00+00:00",
        )
        usage = build_codex_usage_extra_updates(snapshot, "2026-06-14T00:00:00+00:00")

        self.assertEqual(usage["codex_7d_used_percent"], 5.0)
        self.assertEqual(usage["codex_7d_remaining_percent"], 95.0)
        self.assertEqual(usage["codex_7d_window_minutes"], 43200)
        self.assertEqual(usage["codex_primary_used_percent"], 5.0)

        progress = build_codex_usage_progress_from_extra(usage)
        self.assertIsNone(progress["five_hour"]["used_percent"])
        self.assertEqual(progress["seven_day"]["used_percent"], 5.0)
        self.assertEqual(progress["seven_day"]["window_minutes"], 43200)

    def test_probe_uses_existing_access_token_without_refresh_token(self):
        account = DummyAccount(extra={"access_token": "cached-access-token"})
        with mock.patch("services.chatgpt_core.codex_usage._probe_codex_usage") as probe:
            probe.return_value = _codex_response(
                {
                    "x-codex-primary-used-percent": "10",
                    "x-codex-primary-window-minutes": "10080",
                    "x-codex-secondary-used-percent": "2",
                    "x-codex-secondary-window-minutes": "300",
                }
            )
            result = probe_codex_usage_window(account)

        self.assertEqual(result["state"], "usable")
        self.assertEqual(result["source"], "access_token")
        probe.assert_called_once()
        self.assertEqual(probe.call_args.args[0], "cached-access-token")
        self.assertEqual(result["usage"]["codex_7d_used_percent"], 10.0)

    def test_probe_uses_wham_usage_body_before_response_headers(self):
        account = DummyAccount(extra={"access_token": "cached-access-token"})
        with mock.patch("services.chatgpt_core.codex_usage._probe_codex_responses") as responses_probe, mock.patch(
            "services.chatgpt_core.codex_usage._probe_codex_usage"
        ) as wham_probe:
            wham_probe.return_value = ProbeHTTPResult(
                status_code=200,
                headers={},
                body_text='{"rate_limit":{"primary_window":{"used_percent":5,"limit_window_seconds":2592000,"reset_after_seconds":2592000}}}',
                body_json={
                    "rate_limit": {
                        "primary_window": {
                            "used_percent": 5,
                            "limit_window_seconds": 2592000,
                            "reset_after_seconds": 2592000,
                        }
                    }
                },
                error_code="",
                message="ok",
            )
            result = probe_codex_usage_window(account)

        self.assertEqual(result["state"], "usable")
        self.assertEqual(result["usage_source"], "wham_usage_body")
        responses_probe.assert_not_called()
        self.assertEqual(result["usage"]["codex_7d_used_percent"], 5.0)
        self.assertEqual(result["usage"]["codex_7d_remaining_percent"], 95.0)

    def test_probe_prefers_refresh_token_and_records_fresh_access_token(self):
        account = DummyAccount(extra={"access_token": "old-access-token", "refresh_token": "rt-token"})
        with mock.patch(
            "services.chatgpt_core.status_probe.TokenRefreshManager.refresh_by_oauth_token",
            return_value=mock.Mock(success=True, access_token="fresh-access-token", refresh_token="rt-token-2", error_message=""),
        ), mock.patch("services.chatgpt_core.codex_usage._probe_codex_usage") as probe:
            probe.return_value = _codex_response({"x-codex-primary-used-percent": "1"})
            result = probe_codex_usage_window(account)

        self.assertEqual(result["state"], "usable")
        self.assertEqual(result["source"], "refresh_token")
        self.assertEqual(probe.call_args.args[0], "fresh-access-token")
        self.assertEqual(result["_token_updates"]["access_token"], "fresh-access-token")
        self.assertEqual(result["_token_updates"]["refresh_token"], "rt-token-2")

    def test_probe_marks_missing_auth_without_rt_or_at(self):
        result = probe_codex_usage_window(DummyAccount(user_id="acct-123"))

        self.assertEqual(result["state"], "missing_auth")
        self.assertEqual(result["message"], "账号缺少 refresh_token 且没有可用 access_token")

    def test_account_has_codex_auth_material_accepts_saved_token_shapes(self):
        self.assertTrue(account_has_codex_auth_material(SavedAccount(extra={"accessToken": "at"})))
        self.assertTrue(account_has_codex_auth_material(SavedAccount(extra={"refreshToken": "rt"})))
        self.assertTrue(account_has_codex_auth_material(SavedAccount(access_token="at")))
        self.assertFalse(account_has_codex_auth_material(SavedAccount()))

    def test_saved_account_refresh_persists_codex_cache_without_status_policy(self):
        account = SavedAccount(extra={"access_token": "at"})
        session = FakeSession()
        probe = {
            "state": "quota_exhausted",
            "checked_at": "2026-06-14T00:00:00+00:00",
            "source": "access_token",
            "http_status": 429,
            "error_code": "rate_limit_exceeded",
            "message": "quota",
            "chatgpt_account_id": "acct-123",
            "usage": {"codex_7d_used_percent": 100.0, "codex_7d_remaining_percent": 0.0},
            "progress": {"updated_at": "2026-06-14T00:00:00+00:00"},
        }
        with mock.patch("services.chatgpt_core.codex_usage.probe_codex_usage_window", return_value=probe):
            result = refresh_codex_usage_for_saved_account(account, session, reason="unit_test")

        self.assertTrue(result["ok"])
        self.assertEqual(account.status, "registered")
        self.assertEqual(session.commits, 1)
        codex = account.get_extra()["chatgpt_local"]["codex"]
        self.assertEqual(codex["state"], "quota_exhausted")
        self.assertEqual(codex["usage"]["codex_7d_remaining_percent"], 0.0)


class LocalStatusRefreshScheduleTests(unittest.TestCase):
    def tearDown(self):
        try:
            from services.chatgpt_core import local_status_refresh

            local_status_refresh._LOCAL_STATUS_REFRESH_IN_FLIGHT.clear()
            local_status_refresh._LOCAL_STATUS_REFRESH_PENDING.clear()
        except Exception:
            pass

    def test_account_has_local_status_auth_material_accepts_saved_token_shapes(self):
        self.assertTrue(account_has_local_status_auth_material(SavedAccount(extra={"accessToken": "at"})))
        self.assertTrue(account_has_local_status_auth_material(SavedAccount(extra={"refreshToken": "rt"})))
        self.assertTrue(account_has_local_status_auth_material(SavedAccount(access_token="at")))
        self.assertFalse(account_has_local_status_auth_material(SavedAccount()))

    def test_schedule_local_status_refresh_coalesces_in_flight_account(self):
        with mock.patch.object(
            local_status_refresh,
            "_enqueue_local_status_refresh_job",
            return_value={"generation": 1, "start": True},
        ), mock.patch("threading.Thread.start") as start:
            first = schedule_chatgpt_local_status_refresh_for_account_id(123, reason="unit_test", delay_seconds=0)
            second = schedule_chatgpt_local_status_refresh_for_account_id(123, reason="auth_capture", delay_seconds=2)

        self.assertTrue(first)
        self.assertTrue(second)
        start.assert_called_once()
        self.assertEqual(local_status_refresh._LOCAL_STATUS_REFRESH_PENDING[123]["reason"], "auth_capture")
        self.assertEqual(local_status_refresh._LOCAL_STATUS_REFRESH_PENDING[123]["delay_seconds"], 2.0)


if __name__ == "__main__":
    unittest.main()
