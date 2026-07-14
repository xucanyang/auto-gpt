import unittest
from unittest import mock

from services.chatgpt_core.status_probe import ProbeHTTPResult, probe_local_chatgpt_status


class DummyAccount:
    def __init__(self, *, token="", access_token="", refresh_token="", session_token="", extra=None, user_id=""):
        self.email = "demo@example.com"
        self.token = token
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.session_token = session_token
        self.extra = dict(extra or {})
        self.user_id = user_id


def _empty_accounts_check_result():
    return ProbeHTTPResult(
        status_code=200,
        headers={},
        body_text="{}",
        body_json={},
        error_code="",
        message="ok",
    )


class ChatGPTStatusProbeTests(unittest.TestCase):
    def setUp(self):
        self.accounts_check_patcher = mock.patch(
            "services.chatgpt_core.status_probe._probe_accounts_check",
            return_value=_empty_accounts_check_result(),
        )
        self.accounts_check_patcher.start()
        self.proxy_patcher = mock.patch(
            "services.chatgpt_core.status_probe._resolve_effective_probe_proxy",
            return_value=("", "direct"),
        )
        self.proxy_patcher.start()

    def tearDown(self):
        self.proxy_patcher.stop()
        self.accounts_check_patcher.stop()

    def test_probe_marks_missing_refresh_token(self):
        account = DummyAccount()

        result = probe_local_chatgpt_status(account)

        self.assertEqual(result["auth"]["state"], "missing_refresh_token")
        self.assertEqual(result["codex"]["state"], "skipped_auth_invalid")

    def test_probe_marks_invalidated_token(self):
        account = DummyAccount(refresh_token="rt-token")

        with mock.patch(
            "services.chatgpt_core.status_probe.TokenRefreshManager.refresh_by_oauth_token",
            return_value=mock.Mock(success=False, access_token="", refresh_token="", error_message="OAuth token 刷新失败: HTTP 401"),
        ):
            result = probe_local_chatgpt_status(account)

        self.assertEqual(result["auth"]["state"], "refresh_token_invalidated")
        self.assertEqual(result["codex"]["state"], "skipped_auth_invalid")

    def test_probe_reads_duck_typed_refresh_token_attributes(self):
        account = DummyAccount(refresh_token="rt-token")

        with mock.patch(
            "services.chatgpt_core.status_probe.TokenRefreshManager.refresh_by_oauth_token",
            return_value=mock.Mock(success=False, access_token="", refresh_token="", error_message="OAuth token 刷新失败: HTTP 401"),
        ):
            result = probe_local_chatgpt_status(account)

        self.assertEqual(result["auth"]["state"], "refresh_token_invalidated")
        self.assertTrue(result["auth"]["refresh_available"])

    def test_probe_marks_account_deactivated(self):
        account = DummyAccount(refresh_token="rt-token")

        with mock.patch(
            "services.chatgpt_core.status_probe.TokenRefreshManager.refresh_by_oauth_token",
            return_value=mock.Mock(success=True, access_token="fresh-access-token", refresh_token="rt-token-2", error_message=""),
        ), mock.patch(
            "services.chatgpt_core.status_probe._probe_backend_me",
            return_value=ProbeHTTPResult(
                status_code=403,
                headers={},
                body_text='{"error":{"code":"account_deactivated","message":"You do not have an account because it has been deleted or deactivated."}}',
                body_json={"error": {"code": "account_deactivated", "message": "You do not have an account because it has been deleted or deactivated."}},
                error_code="account_deactivated",
                message="You do not have an account because it has been deleted or deactivated.",
            ),
        ):
            result = probe_local_chatgpt_status(account)

        self.assertEqual(result["auth"]["state"], "account_deactivated")
        self.assertEqual(result["codex"]["state"], "skipped_auth_invalid")

    def test_probe_falls_back_to_existing_access_token_when_refresh_fails(self):
        account = DummyAccount(
            access_token="cached-access-token",
            refresh_token="rt-token",
            user_id="acct-123",
        )

        with mock.patch(
            "services.chatgpt_core.status_probe.TokenRefreshManager.refresh_by_oauth_token",
            return_value=mock.Mock(success=False, access_token="", refresh_token="", error_message="OAuth token 刷新失败: HTTP 401"),
        ), mock.patch(
            "services.chatgpt_core.status_probe._probe_backend_me",
            return_value=ProbeHTTPResult(
                status_code=200,
                headers={},
                body_text='{"plan_type":"chatgptplusplan"}',
                body_json={"plan_type": "chatgptplusplan"},
                error_code="",
                message="ok",
            ),
        ), mock.patch(
            "services.chatgpt_core.status_probe._probe_codex_usage",
            return_value=ProbeHTTPResult(
                status_code=200,
                headers={},
                body_text='{"ok":true}',
                body_json={"ok": True},
                error_code="",
                message="ok",
            ),
        ):
            result = probe_local_chatgpt_status(account)

        self.assertEqual(result["auth"]["state"], "access_token_valid")
        self.assertEqual(result["auth"]["source"], "access_token")
        self.assertEqual(result["subscription"]["plan"], "plus")
        self.assertEqual(result["codex"]["state"], "usable")

    def test_probe_uses_access_token_when_refresh_token_missing(self):
        account = DummyAccount(
            access_token="cached-access-token",
            user_id="acct-123",
        )

        with mock.patch(
            "services.chatgpt_core.status_probe._probe_backend_me",
            return_value=ProbeHTTPResult(
                status_code=200,
                headers={},
                body_text='{"plan_type":"chatgptfreeplan"}',
                body_json={"plan_type": "chatgptfreeplan"},
                error_code="",
                message="ok",
            ),
        ), mock.patch(
            "services.chatgpt_core.status_probe._probe_codex_usage",
            return_value=ProbeHTTPResult(
                status_code=200,
                headers={},
                body_text='{"ok":true}',
                body_json={"ok": True},
                error_code="",
                message="ok",
            ),
        ):
            result = probe_local_chatgpt_status(account)

        self.assertEqual(result["auth"]["state"], "access_token_valid")
        self.assertEqual(result["auth"]["source"], "access_token")
        self.assertEqual(result["subscription"]["plan"], "free")

    def test_probe_backend_me_timeout_returns_structured_probe_failure(self):
        account = DummyAccount(
            access_token="cached-access-token",
            user_id="acct-123",
        )

        with mock.patch(
            "services.chatgpt_core.status_probe._probe_backend_me",
            side_effect=TimeoutError("backend timeout"),
        ):
            result = probe_local_chatgpt_status(account)

        self.assertEqual(result["auth"]["state"], "probe_failed")
        self.assertEqual(result["auth"]["http_status"], 0)
        self.assertIn("backend timeout", result["auth"]["message"])
        self.assertEqual(result["codex"]["state"], "not_checked")

    def test_probe_refresh_token_timeout_does_not_mark_missing_refresh_token(self):
        account = DummyAccount(refresh_token="rt-token")

        with mock.patch(
            "services.chatgpt_core.status_probe.TokenRefreshManager.refresh_by_oauth_token",
            return_value=mock.Mock(success=False, access_token="", refresh_token="", error_message="OAuth token 刷新异常: timeout"),
        ):
            result = probe_local_chatgpt_status(account)

        self.assertEqual(result["auth"]["state"], "probe_failed")
        self.assertEqual(result["auth"]["http_status"], 0)
        self.assertEqual(result["codex"]["state"], "not_checked")

    def test_probe_accounts_check_timeout_still_probes_codex(self):
        account = DummyAccount(
            access_token="cached-access-token",
            user_id="acct-123",
        )

        with mock.patch(
            "services.chatgpt_core.status_probe._probe_backend_me",
            return_value=ProbeHTTPResult(
                status_code=200,
                headers={},
                body_text="{}",
                body_json={},
                error_code="",
                message="ok",
            ),
        ), mock.patch(
            "services.chatgpt_core.status_probe._probe_accounts_check",
            side_effect=TimeoutError("accounts check timeout"),
        ), mock.patch(
            "services.chatgpt_core.status_probe._probe_codex_usage",
            return_value=ProbeHTTPResult(
                status_code=200,
                headers={},
                body_text='{"ok":true}',
                body_json={"ok": True},
                error_code="",
                message="ok",
            ),
        ):
            result = probe_local_chatgpt_status(account)

        self.assertEqual(result["auth"]["state"], "access_token_valid")
        self.assertEqual(result["subscription"]["plan"], "unknown")
        self.assertEqual(result["codex"]["state"], "usable")

    def test_probe_codex_timeout_returns_structured_codex_failure(self):
        account = DummyAccount(
            access_token="cached-access-token",
            user_id="acct-123",
        )

        with mock.patch(
            "services.chatgpt_core.status_probe._probe_backend_me",
            return_value=ProbeHTTPResult(
                status_code=200,
                headers={},
                body_text='{"plan_type":"chatgptplusplan"}',
                body_json={"plan_type": "chatgptplusplan"},
                error_code="",
                message="ok",
            ),
        ), mock.patch(
            "services.chatgpt_core.status_probe._probe_codex_usage",
            side_effect=TimeoutError("codex timeout"),
        ):
            result = probe_local_chatgpt_status(account)

        self.assertEqual(result["auth"]["state"], "access_token_valid")
        self.assertEqual(result["codex"]["state"], "probe_failed")
        self.assertEqual(result["codex"]["http_status"], 0)
        self.assertIn("codex timeout", result["codex"]["message"])

    def test_probe_treats_deactivated_workspace_as_deactivated(self):
        account = DummyAccount(access_token="cached-access-token")

        with mock.patch(
            "services.chatgpt_core.status_probe._probe_backend_me",
            return_value=ProbeHTTPResult(
                status_code=402,
                headers={},
                body_text='{"detail":{"code":"deactivated_workspace"}}',
                body_json={"detail": {"code": "deactivated_workspace"}},
                error_code="deactivated_workspace",
                message='{"detail":{"code":"deactivated_workspace"}}',
            ),
        ):
            result = probe_local_chatgpt_status(account)

        self.assertEqual(result["auth"]["state"], "account_deactivated")
        self.assertEqual(result["codex"]["state"], "skipped_auth_invalid")

    def test_probe_extracts_plan_and_quota_state(self):
        account = DummyAccount(refresh_token="rt-token", user_id="acct-123")

        with mock.patch(
            "services.chatgpt_core.status_probe.TokenRefreshManager.refresh_by_oauth_token",
            return_value=mock.Mock(success=True, access_token="fresh-access-token", refresh_token="rt-token-2", error_message=""),
        ), mock.patch(
            "services.chatgpt_core.status_probe._probe_backend_me",
            return_value=ProbeHTTPResult(
                status_code=200,
                headers={},
                body_text='{"plan_type":"chatgptplusplan"}',
                body_json={"plan_type": "chatgptplusplan"},
                error_code="",
                message="ok",
            ),
        ):
            with mock.patch(
                "services.chatgpt_core.status_probe._probe_codex_usage",
                return_value=ProbeHTTPResult(
                    status_code=429,
                    headers={},
                    body_text='{"error":{"type":"usage_limit_reached"}}',
                    body_json={"error": {"type": "usage_limit_reached"}},
                    error_code="",
                    message="usage_limit_reached",
                ),
            ):
                result = probe_local_chatgpt_status(account)

        self.assertEqual(result["auth"]["state"], "refresh_token_valid")
        self.assertEqual(result["subscription"]["plan"], "plus")
        self.assertEqual(result["codex"]["state"], "quota_exhausted")

    def test_probe_prefers_refresh_token_for_subscription_and_codex(self):
        account = DummyAccount(refresh_token="rt-token", user_id="acct-rt")

        with mock.patch(
            "services.chatgpt_core.status_probe.TokenRefreshManager.refresh_by_oauth_token",
            return_value=mock.Mock(success=True, access_token="fresh-access-token", refresh_token="rt-token-2", error_message=""),
        ), mock.patch(
            "services.chatgpt_core.status_probe._probe_backend_me",
            return_value=ProbeHTTPResult(
                status_code=200,
                headers={},
                body_text='{"plan_type":"chatgptplusplan"}',
                body_json={"plan_type": "chatgptplusplan"},
                error_code="",
                message="ok",
            ),
        ), mock.patch(
            "services.chatgpt_core.status_probe._probe_codex_usage",
            return_value=ProbeHTTPResult(
                status_code=200,
                headers={},
                body_text='{"ok":true}',
                body_json={"ok": True},
                error_code="",
                message="ok",
            ),
        ):
            result = probe_local_chatgpt_status(account)

        self.assertEqual(result["auth"]["state"], "refresh_token_valid")
        self.assertEqual(result["subscription"]["plan"], "plus")
        self.assertEqual(result["codex"]["state"], "usable")
        self.assertEqual(result["auth"]["source"], "refresh_token")

    def test_probe_marks_codex_invalidated_as_refresh_token_invalidated(self):
        account = DummyAccount(refresh_token="rt-token", user_id="acct-rt")

        with mock.patch(
            "services.chatgpt_core.status_probe.TokenRefreshManager.refresh_by_oauth_token",
            return_value=mock.Mock(success=True, access_token="fresh-access-token", refresh_token="rt-token-2", error_message=""),
        ), mock.patch(
            "services.chatgpt_core.status_probe._probe_backend_me",
            return_value=ProbeHTTPResult(
                status_code=200,
                headers={},
                body_text='{"plan_type":"chatgptplusplan"}',
                body_json={"plan_type": "chatgptplusplan"},
                error_code="",
                message="ok",
            ),
        ), mock.patch(
            "services.chatgpt_core.status_probe._probe_codex_usage",
            return_value=ProbeHTTPResult(
                status_code=401,
                headers={},
                body_text='{"error":{"code":"token_invalidated"}}',
                body_json={"error": {"code": "token_invalidated"}},
                error_code="token_invalidated",
                message="invalidated",
            ),
        ):
            result = probe_local_chatgpt_status(account)

        self.assertEqual(result["codex"]["state"], "refresh_token_invalidated")
        self.assertEqual(result["codex"]["source"], "refresh_token")

    def test_probe_falls_back_to_accounts_check_for_plus_subscription(self):
        account = DummyAccount(
            refresh_token="rt-token",
            user_id="acct-personal",
            extra={"workspace_id": "ws-personal"},
        )

        with mock.patch(
            "services.chatgpt_core.status_probe.TokenRefreshManager.refresh_by_oauth_token",
            return_value=mock.Mock(
                success=True,
                access_token="fresh-access-token",
                refresh_token="rt-token-2",
                error_message="",
            ),
        ), mock.patch(
            "services.chatgpt_core.status_probe._probe_backend_me",
            return_value=ProbeHTTPResult(
                status_code=200,
                headers={},
                body_text="{}",
                body_json={},
                error_code="",
                message="ok",
            ),
        ), mock.patch(
            "services.chatgpt_core.status_probe._probe_accounts_check",
            return_value=ProbeHTTPResult(
                status_code=200,
                headers={},
                body_text="{}",
                body_json={
                    "accounts": {
                        "ws-personal": {
                            "account": {
                                "plan_type": "chatgptplusplan",
                                "is_default": True,
                            },
                            "entitlement": {
                                "subscription_plan": "chatgptplusplan",
                                "expires_at": "2026-08-01T00:00:00+00:00",
                            },
                        }
                    }
                },
                error_code="",
                message="ok",
            ),
        ), mock.patch(
            "services.chatgpt_core.status_probe._probe_codex_usage",
            return_value=ProbeHTTPResult(
                status_code=200,
                headers={},
                body_text='{"ok":true}',
                body_json={"ok": True},
                error_code="",
                message="ok",
            ),
        ):
            result = probe_local_chatgpt_status(account)

        self.assertEqual(result["auth"]["state"], "refresh_token_valid")
        self.assertEqual(result["subscription"]["plan"], "plus")
        self.assertEqual(result["subscription"]["source"], "accounts_check")
        self.assertEqual(
            result["subscription"]["subscription_active_until"],
            "2026-08-01T00:00:00+00:00",
        )

    def test_probe_prefers_current_workspace_id_over_default_account_entry(self):
        account = DummyAccount(
            access_token="cached-access-token",
            user_id="acct-personal",
            extra={"workspace_id": "ws-personal"},
        )

        with mock.patch(
            "services.chatgpt_core.status_probe._probe_backend_me",
            return_value=ProbeHTTPResult(
                status_code=200,
                headers={},
                body_text="{}",
                body_json={},
                error_code="",
                message="ok",
            ),
        ), mock.patch(
            "services.chatgpt_core.status_probe._probe_accounts_check",
            return_value=ProbeHTTPResult(
                status_code=200,
                headers={},
                body_text="{}",
                body_json={
                    "accounts": {
                        "acct-personal": {
                            "account": {"plan_type": "free", "is_default": True},
                            "entitlement": {
                                "subscription_plan": "chatgptfreeplan"
                            },
                        },
                        "ws-personal": {
                            "account": {"plan_type": "chatgptplusplan"},
                            "entitlement": {
                                "subscription_plan": "chatgptplusplan",
                                "expires_at": "2026-09-01T00:00:00+00:00",
                            },
                        },
                    }
                },
                error_code="",
                message="ok",
            ),
        ), mock.patch(
            "services.chatgpt_core.status_probe._probe_codex_usage",
            return_value=ProbeHTTPResult(
                status_code=200,
                headers={},
                body_text='{"ok":true}',
                body_json={"ok": True},
                error_code="",
                message="ok",
            ),
        ):
            result = probe_local_chatgpt_status(account)

        self.assertEqual(result["subscription"]["plan"], "plus")
        self.assertEqual(result["subscription"]["source"], "accounts_check")
        self.assertEqual(
            result["subscription"]["subscription_active_until"],
            "2026-09-01T00:00:00+00:00",
        )

    def test_probe_backfills_subscription_expiry_when_me_has_paid_plan_without_expiry(self):
        account = DummyAccount(
            access_token="cached-access-token",
            user_id="acct-123",
        )

        with mock.patch(
            "services.chatgpt_core.status_probe._probe_backend_me",
            return_value=ProbeHTTPResult(
                status_code=200,
                headers={},
                body_text='{"plan_type":"chatgptplusplan"}',
                body_json={"plan_type": "chatgptplusplan"},
                error_code="",
                message="ok",
            ),
        ), mock.patch(
            "services.chatgpt_core.status_probe._probe_accounts_check",
            return_value=ProbeHTTPResult(
                status_code=200,
                headers={},
                body_text='{}',
                body_json={
                    "accounts": {
                        "acct-123": {
                            "account": {"plan_type": "chatgptplusplan", "is_default": True},
                            "entitlement": {
                                "subscription_plan": "chatgptplusplan",
                                "expires_at": 1781089634,
                            },
                        }
                    }
                },
                error_code="",
                message="ok",
            ),
        ) as accounts_check, mock.patch(
            "services.chatgpt_core.status_probe._probe_codex_usage",
            return_value=ProbeHTTPResult(
                status_code=200,
                headers={},
                body_text='{"ok":true}',
                body_json={"ok": True},
                error_code="",
                message="ok",
            ),
        ):
            result = probe_local_chatgpt_status(account)

        accounts_check.assert_called_once()
        self.assertEqual(result["subscription"]["plan"], "plus")
        self.assertEqual(result["subscription"]["source"], "accounts_check")
        self.assertEqual(result["subscription"]["subscription_active_until"], "1781089634")

if __name__ == "__main__":
    unittest.main()
