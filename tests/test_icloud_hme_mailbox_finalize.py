import unittest
from unittest.mock import patch

from core.base_mailbox import IcloudHmeMailbox, MailboxAccount


class IcloudHmeMailboxFinalizeTests(unittest.TestCase):
    def _build_mailbox(self):
        return IcloudHmeMailbox(
            icloud_hme_mode="import_pool",
            icloud_cookie="",
            icloud_forward_to="b@example.com",
            tempmail_api_url="http://tempmail-api-1:8080",
            tempmail_api_key="test-key",
        )

    def test_already_paid_failure_does_not_release_alias_to_reserved(self):
        mailbox = self._build_mailbox()
        account = MailboxAccount(email="alias@icloud.com", account_id="anon-1")

        with (
            patch("core.db.release_icloud_hme_alias_after_early_failure") as release_alias,
            patch("core.db.update_icloud_hme_alias_on_failure") as mark_failure,
        ):
            mailbox.finalize_failure(
                account,
                error_message="homepage ok but User is already paid",
                task_id="task-1",
            )

        release_alias.assert_not_called()
        mark_failure.assert_called_once()
        self.assertEqual(mark_failure.call_args.args[0], "anon-1")
        self.assertEqual(mark_failure.call_args.kwargs["task_id"], "task-1")

    def test_finalize_success_only_updates_local_alias_state(self):
        mailbox = self._build_mailbox()
        account = MailboxAccount(
            email="alias@icloud.com",
            account_id="anon-success-1",
            extra={"label": "chatgpt-demo"},
        )

        with patch("core.db.update_icloud_hme_alias_on_success") as mark_success:
            mailbox.finalize_success(
                account,
                registered_email="registered@example.com",
                task_id="task-success-1",
            )

        mark_success.assert_called_once()
        self.assertEqual(mark_success.call_args.args[0], "anon-success-1")
        self.assertEqual(mark_success.call_args.kwargs["bound_account_email"], "registered@example.com")
        self.assertEqual(mark_success.call_args.kwargs["task_id"], "task-success-1")
        self.assertIn("chatgpt:registered@example.com", mark_success.call_args.kwargs["note"])

    def test_forward_sink_mailbox_is_permanent_by_default(self):
        mailbox = self._build_mailbox()

        forward_sink = mailbox._tempmail_mailbox

        self.assertTrue(forward_sink._permanent)
        self.assertGreaterEqual(forward_sink._ttl_minutes, 525600)

    def test_early_homepage_failure_still_releases_alias(self):
        mailbox = self._build_mailbox()
        account = MailboxAccount(email="alias@icloud.com", account_id="anon-2")

        with (
            patch("core.db.release_icloud_hme_alias_after_early_failure") as release_alias,
            patch("core.db.update_icloud_hme_alias_on_failure") as mark_failure,
        ):
            mailbox.finalize_failure(
                account,
                error_message="访问首页失败: timeout",
                task_id="task-2",
            )

        release_alias.assert_called_once()
        mark_failure.assert_not_called()
        self.assertEqual(release_alias.call_args.args[0], "anon-2")

    def test_deactivated_failure_marks_alias_disabled_dead(self):
        mailbox = self._build_mailbox()
        account = MailboxAccount(email="alias@icloud.com", account_id="anon-dead")

        with (
            patch("core.db.release_icloud_hme_alias_after_early_failure") as release_alias,
            patch("core.db.update_icloud_hme_alias_on_failure") as mark_failure,
            patch("core.db.update_icloud_hme_alias_on_account_deactivated") as mark_deactivated,
        ):
            mailbox.finalize_failure(
                account,
                error_message=(
                    "注册流失败: 验证码失败: HTTP 403: account_deactivated: "
                    "You do not have an account because it has been deleted or deactivated."
                ),
                task_id="task-dead",
            )

        release_alias.assert_not_called()
        mark_failure.assert_not_called()
        mark_deactivated.assert_called_once()
        self.assertEqual(mark_deactivated.call_args.args[0], "anon-dead")
        self.assertEqual(mark_deactivated.call_args.kwargs["task_id"], "task-dead")


if __name__ == "__main__":
    unittest.main()
