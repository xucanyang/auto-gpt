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


if __name__ == "__main__":
    unittest.main()
