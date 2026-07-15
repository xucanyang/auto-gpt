import unittest

from services.chatgpt_account_state import (
    apply_auth_capture_status,
    apply_chatgpt_status_policy,
    apply_payment_snapshot_status,
    classify_chatgpt_capabilities,
    classify_local_probe_state,
    classify_remote_sync_state,
    is_chatgpt_upload_ready,
    mark_payment_failed,
    mark_payment_pending,
    mark_payment_succeeded,
)


class DummyAccount:
    def __init__(self, status="registered", extra=None, token=""):
        self.status = status
        self.extra = dict(extra or {})
        self.token = token
        self.user_id = ""
        self.cashier_url = ""

    def get_extra(self):
        return dict(self.extra)


class ChatGPTAccountStateTests(unittest.TestCase):
    def test_local_401_marks_invalid(self):
        account = DummyAccount()
        reason = apply_chatgpt_status_policy(
            account,
            local_probe={
                "auth": {
                    "state": "refresh_token_invalidated",
                    "http_status": 401,
                    "error_code": "token_invalidated",
                    "message": "invalidated",
                }
            },
        )
        self.assertEqual(reason, "auth_401")
        self.assertEqual(account.status, "invalid")

    def test_remote_401_marks_invalid(self):
        self.assertEqual(
            classify_remote_sync_state(
                {
                    "remote_state": "access_token_invalidated",
                    "last_probe_status_code": 401,
                    "last_probe_error_code": "token_invalidated",
                    "last_probe_message": "invalidated",
                }
            ),
            "remote_401",
        )

    def test_valid_probe_recovers_invalid_account(self):
        account = DummyAccount(status="invalid")
        reason = apply_chatgpt_status_policy(
            account,
            local_probe={
                "auth": {
                    "state": "access_token_valid",
                    "http_status": 200,
                    "error_code": "",
                    "message": "ok",
                },
                "codex": {
                    "state": "usable",
                    "http_status": 200,
                    "error_code": "",
                    "message": "ok",
                },
            },
        )
        self.assertEqual(reason, "")
        self.assertEqual(account.status, "pending_payment")

    def test_access_token_only_is_pending_payment_and_not_upload_ready(self):
        account = DummyAccount(
            status="registered",
            token="at-demo",
            extra={"access_token": "at-demo", "partial_auth": True},
        )

        capabilities = classify_chatgpt_capabilities(
            account,
            local_probe={
                "auth": {"state": "access_token_valid", "http_status": 200},
                "subscription": {"plan": "free"},
                "codex": {"state": "not_checked"},
            },
        )
        ready, message, _ = is_chatgpt_upload_ready(account, local_probe={"auth": {"state": "access_token_valid"}})
        apply_chatgpt_status_policy(account, local_probe={"auth": {"state": "access_token_valid", "http_status": 200}})

        self.assertEqual(capabilities["auth_level"], "access_token_only")
        self.assertEqual(capabilities["upload_gate"], "blocked_missing_rt")
        self.assertFalse(ready)
        self.assertIn("refresh_token", message)
        self.assertEqual(account.status, "pending_payment")

    def test_refresh_token_with_current_account_identity_is_upload_ready(self):
        account = DummyAccount(
            status="pending_payment",
            token="at-demo",
            extra={"access_token": "at-demo", "refresh_token": "rt-demo", "workspace_id": "acct-demo"},
        )
        account.user_id = "acct-demo"

        ready, message, capabilities = is_chatgpt_upload_ready(
            account,
            local_probe={
                "auth": {"state": "refresh_token_valid", "http_status": 200},
                "subscription": {"plan": "plus"},
                "codex": {"state": "usable", "http_status": 200},
            },
        )
        apply_chatgpt_status_policy(
            account,
            local_probe={
                "auth": {"state": "refresh_token_valid", "http_status": 200},
                "subscription": {"plan": "plus"},
            },
        )

        self.assertTrue(ready)
        self.assertEqual(message, "")
        self.assertEqual(capabilities["upload_gate"], "ready")
        self.assertEqual(account.status, "subscribed")

    def test_confirmed_phone_binding_requires_bound_status_and_full_phone(self):
        confirmed = DummyAccount(
            extra={
                "chatgpt_phone_binding": {
                    "status": "bound",
                    "phone": "+16134655704",
                }
            }
        )
        observed_only = DummyAccount(
            extra={
                "chatgpt_bound_phone": {
                    "phone": "+16134655704",
                    "verification_status": "required",
                }
            }
        )
        malformed = DummyAccount(
            extra={
                "chatgpt_phone_binding": {
                    "status": "bound",
                    "phone": "5704",
                }
            }
        )

        confirmed_caps = classify_chatgpt_capabilities(confirmed)
        observed_caps = classify_chatgpt_capabilities(observed_only)
        malformed_caps = classify_chatgpt_capabilities(malformed)

        self.assertTrue(confirmed_caps["has_confirmed_phone_binding"])
        self.assertEqual(confirmed_caps["phone_binding_state"], "confirmed")
        self.assertFalse(observed_caps["has_confirmed_phone_binding"])
        self.assertEqual(observed_caps["phone_binding_state"], "unconfirmed")
        self.assertFalse(malformed_caps["has_confirmed_phone_binding"])
        self.assertEqual(malformed_caps["phone_binding_state"], "unconfirmed")

    def test_payment_pending_helper_preserves_subscribed_and_invalid(self):
        account = DummyAccount(status="registered")
        self.assertEqual(mark_payment_pending(account), "pending_payment")
        self.assertEqual(account.status, "pending_payment")

        subscribed = DummyAccount(status="subscribed")
        self.assertEqual(mark_payment_pending(subscribed), "subscribed")
        self.assertEqual(subscribed.status, "subscribed")

        invalid = DummyAccount(status="invalid")
        self.assertEqual(mark_payment_pending(invalid), "invalid")
        self.assertEqual(invalid.status, "invalid")

    def test_payment_failed_helper_preserves_subscribed(self):
        subscribed = DummyAccount(status="subscribed")
        self.assertEqual(mark_payment_failed(subscribed), "subscribed")
        self.assertEqual(subscribed.status, "subscribed")

        pending = DummyAccount(status="pending_payment")
        self.assertEqual(mark_payment_failed(pending), "payment_failed")
        self.assertEqual(pending.status, "payment_failed")

    def test_payment_snapshot_status_transitions(self):
        active = DummyAccount(status="registered")
        self.assertEqual(apply_payment_snapshot_status(active, {"phase": "waiting_otp", "status": "active"}), "pending_payment")
        self.assertEqual(active.status, "pending_payment")

        succeeded = DummyAccount(status="payment_failed")
        self.assertEqual(apply_payment_snapshot_status(succeeded, {"phase": "succeeded", "status": "done"}), "subscribed")
        self.assertEqual(succeeded.status, "subscribed")

        failed = DummyAccount(status="pending_payment")
        self.assertEqual(apply_payment_snapshot_status(failed, {"phase": "failed", "status": "failed"}), "payment_failed")
        self.assertEqual(failed.status, "payment_failed")

    def test_auth_capture_status_preserves_payment_state(self):
        pending = DummyAccount(
            status="pending_payment",
            extra={"chatgpt_last_payment_link": {"url": "https://checkout.example.test/cs_123"}},
        )
        self.assertEqual(apply_auth_capture_status(pending, "registered"), "pending_payment")
        self.assertEqual(pending.status, "pending_payment")

        failed = DummyAccount(status="payment_failed")
        self.assertEqual(apply_auth_capture_status(failed, "registered"), "payment_failed")
        self.assertEqual(failed.status, "payment_failed")

        subscribed = DummyAccount(status="subscribed")
        self.assertEqual(apply_auth_capture_status(subscribed, "registered"), "subscribed")
        self.assertEqual(subscribed.status, "subscribed")

    def test_auth_capture_status_can_recover_non_payment_pending(self):
        account = DummyAccount(status="pending_payment")
        self.assertEqual(apply_auth_capture_status(account, "registered"), "registered")
        self.assertEqual(account.status, "registered")

    def test_confirmed_free_probe_demotes_buggy_subscribed_to_pending_when_payment_link_exists(self):
        account = DummyAccount(
            status="subscribed",
            token="at-demo",
            extra={
                "access_token": "at-demo",
                "chatgpt_last_payment_link": {"url": "https://checkout.example.test/cs_123"},
            },
        )

        reason = apply_chatgpt_status_policy(
            account,
            local_probe={
                "auth": {"state": "access_token_valid", "http_status": 200},
                "subscription": {"plan": "free"},
            },
        )

        self.assertEqual(reason, "")
        self.assertEqual(account.status, "pending_payment")

    def test_confirmed_free_probe_demotes_buggy_subscribed_to_registered_without_payment_marker(self):
        account = DummyAccount(
            status="subscribed",
            token="at-demo",
            extra={"access_token": "at-demo"},
        )

        reason = apply_chatgpt_status_policy(
            account,
            local_probe={
                "auth": {"state": "access_token_valid", "http_status": 200},
                "subscription": {"plan": "free"},
            },
        )

        self.assertEqual(reason, "")
        self.assertEqual(account.status, "registered")

    def test_successful_payment_marker_preserves_subscribed_when_probe_is_free(self):
        account = DummyAccount(
            status="subscribed",
            token="at-demo",
            extra={
                "access_token": "at-demo",
                "chatgpt_gopay": {"phase": "succeeded", "status": "done"},
            },
        )

        reason = apply_chatgpt_status_policy(
            account,
            local_probe={
                "auth": {"state": "access_token_valid", "http_status": 200},
                "subscription": {"plan": "free"},
            },
        )

        self.assertEqual(reason, "")
        self.assertEqual(account.status, "subscribed")

    def test_probe_failure_unknown_does_not_demote_subscribed(self):
        account = DummyAccount(
            status="subscribed",
            token="at-demo",
            extra={"access_token": "at-demo"},
        )

        reason = apply_chatgpt_status_policy(
            account,
            local_probe={
                "auth": {"state": "probe_failed", "http_status": 0},
                "subscription": {"plan": "unknown"},
            },
        )

        self.assertEqual(reason, "")
        self.assertEqual(account.status, "subscribed")

    def test_unknown_probe_keeps_old_plan_only_as_last_known(self):
        account = DummyAccount(
            status="subscribed",
            token="at-demo",
            extra={
                "access_token": "at-demo",
                "chatgpt_capabilities": {
                    "subscription_plan": "plus",
                    "subscription_checked": True,
                },
            },
        )

        capabilities = classify_chatgpt_capabilities(
            account,
            local_probe={
                "auth": {"state": "probe_failed", "http_status": 0},
                "subscription": {"plan": "unknown"},
            },
        )

        self.assertEqual(capabilities["subscription_plan"], "unknown")
        self.assertEqual(capabilities["last_known_subscription_plan"], "plus")
        self.assertTrue(capabilities["subscription_plan_stale"])
        self.assertEqual(capabilities["subscription_refresh_state"], "refresh_failed")
        self.assertFalse(capabilities["has_paid_subscription"])
        self.assertTrue(capabilities["last_known_has_paid_subscription"])

    def test_mark_payment_succeeded_overrides_stale_invalid(self):
        account = DummyAccount(status="invalid")
        self.assertEqual(mark_payment_succeeded(account), "subscribed")
        self.assertEqual(account.status, "subscribed")

    def test_quota_does_not_mark_invalid(self):
        self.assertEqual(
            classify_local_probe_state(
                {
                    "auth": {"state": "refresh_token_valid", "http_status": 200},
                    "codex": {"state": "payment_required", "http_status": 403, "message": "payment required"},
                }
            ),
            "",
        )
        self.assertEqual(
            classify_remote_sync_state(
                {
                    "remote_state": "quota_exhausted",
                    "last_probe_status_code": 429,
                    "last_probe_error_code": "",
                    "last_probe_message": "usage limit reached",
                }
            ),
            "",
        )

    def test_deactivated_message_marks_invalid(self):
        self.assertEqual(
            classify_local_probe_state(
                {
                    "auth": {
                        "state": "banned_like",
                        "http_status": 403,
                        "error_code": "account_deactivated",
                        "message": "You do not have an account because it has been deleted or deactivated.",
                    }
                }
            ),
            "auth_deactivated",
        )

    def test_refresh_token_invalidated_marks_invalid(self):
        self.assertEqual(
            classify_local_probe_state(
                {
                    "auth": {
                        "state": "refresh_token_invalidated",
                        "http_status": 401,
                        "error_code": "token_invalidated",
                        "message": "invalidated",
                    }
                }
            ),
            "auth_401",
        )

    def test_codex_refresh_token_invalidated_marks_invalid(self):
        self.assertEqual(
            classify_local_probe_state(
                {
                    "auth": {
                        "state": "refresh_token_valid",
                        "http_status": 200,
                    },
                    "codex": {
                        "state": "refresh_token_invalidated",
                        "http_status": 401,
                        "error_code": "token_invalidated",
                        "message": "invalidated",
                    },
                }
            ),
            "codex_401",
        )

    def test_access_token_invalidated_marks_invalid(self):
        self.assertEqual(
            classify_local_probe_state(
                {
                    "auth": {
                        "state": "access_token_invalidated",
                        "http_status": 401,
                        "error_code": "token_invalidated",
                        "message": "invalidated",
                    }
                }
            ),
            "auth_401",
        )

    def test_deactivated_workspace_marks_invalid(self):
        self.assertEqual(
            classify_local_probe_state(
                {
                    "auth": {
                        "state": "account_deactivated",
                        "http_status": 402,
                        "error_code": "deactivated_workspace",
                        "message": '{"detail":{"code":"deactivated_workspace"}}',
                    }
                }
            ),
            "auth_deactivated",
        )


if __name__ == "__main__":
    unittest.main()
