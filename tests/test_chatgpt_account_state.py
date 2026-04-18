import unittest

from services.chatgpt_account_state import (
    apply_chatgpt_status_policy,
    classify_local_probe_state,
    classify_remote_sync_state,
)


class DummyAccount:
    def __init__(self, status="registered"):
        self.status = status


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
        self.assertEqual(account.status, "registered")

    def test_payment_and_quota_do_not_mark_invalid(self):
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
