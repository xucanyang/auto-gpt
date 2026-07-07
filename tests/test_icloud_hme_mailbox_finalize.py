import unittest
from unittest.mock import Mock, patch

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

    def test_forward_to_list_is_preserved_for_legacy_forward_scan(self):
        mailbox = IcloudHmeMailbox(
            icloud_hme_mode="import_pool",
            icloud_cookie="",
            icloud_forward_to="b@example.com, apple@example.com; ops@example.com",
            tempmail_api_url="http://tempmail-api-1:8080",
            tempmail_api_key="test-key",
        )

        self.assertEqual(mailbox._icloud_forward_to, "b@example.com")
        self.assertEqual(
            mailbox._icloud_forward_tos,
            ["b@example.com", "apple@example.com", "ops@example.com"],
        )

    def test_helper_lease_id_does_not_treat_legacy_anonymous_id_as_checkout(self):
        legacy = MailboxAccount(
            email="legacy@icloud.com",
            account_id="m5tbftxrk28215",
            extra={"provider": "icloud_hme"},
        )
        helper_with_explicit_lease = MailboxAccount(
            email="helper@icloud.com",
            account_id="m5tbftxrk28215",
            extra={"provider": "icloud_hme", "lease_id": "lease-1"},
        )
        helper_provider_state = MailboxAccount(
            email="helper-provider@icloud.com",
            account_id="lease-2",
            extra={"provider": "hme_ready_api"},
        )

        self.assertEqual(IcloudHmeMailbox._helper_lease_id(legacy), "")
        self.assertEqual(IcloudHmeMailbox._helper_lease_id(helper_with_explicit_lease), "lease-1")
        self.assertEqual(IcloudHmeMailbox._helper_lease_id(helper_provider_state), "lease-2")

    def test_helper_ready_waits_code_from_tempmail_forward_mailbox(self):
        mailbox = IcloudHmeMailbox(
            icloud_hme_mode="helper_ready_api",
            icloud_cookie="",
            icloud_forward_to="global@example.com",
            tempmail_api_url="http://tempmail-api-1:8080",
            tempmail_api_key="test-key",
            icloud_hme_helper_api_url="http://helper-api",
            icloud_hme_helper_internal_key="helper-key",
        )
        account = MailboxAccount(
            email="alias@icloud.com",
            account_id="lease-1",
            extra={
                "provider": "icloud_hme",
                "mode": "helper_ready_api",
                "lease_id": "lease-1",
                "forward_to": "specific@example.com",
                "forward_mailbox_id": "specific-mbox",
            },
        )

        mailbox._helper_client.wait_code = Mock(side_effect=AssertionError("helper wait-code must not be called"))
        mailbox._tempmail_mailbox.ensure_mailbox_by_email = Mock(
            side_effect=lambda email, force_lookup=False: MailboxAccount(
                email=email,
                account_id=f"mbox-{email}",
            )
        )
        mailbox._tempmail_mailbox._list_emails = Mock(
            side_effect=lambda mailbox_id: [{"id": "msg-1", "subject": "OpenAI verification"}]
            if mailbox_id == "specific-mbox"
            else []
        )
        mailbox._tempmail_mailbox._get_email_detail = Mock(
            return_value={
                "received_for": ["alias@icloud.com"],
                "subject": "OpenAI verification",
                "body_text": "Your verification code is 123456.",
                "raw_message": "Delivered-To: alias@icloud.com",
            }
        )

        code = mailbox.wait_for_code(account, timeout=1)

        self.assertEqual(code, "123456")
        mailbox._helper_client.wait_code.assert_not_called()
        mailbox._tempmail_mailbox._list_emails.assert_any_call("specific-mbox")
        self.assertEqual(mailbox._last_verification_result["provider"], "IcloudHmeTempMailForwardMailbox")
        self.assertEqual(mailbox._last_verification_result["lease_id"], "lease-1")
        self.assertEqual(mailbox._last_verification_result["matched_mailbox_id"], "specific-mbox")

    def test_helper_ready_current_ids_reads_tempmail_not_helper(self):
        mailbox = IcloudHmeMailbox(
            icloud_hme_mode="helper_ready_api",
            icloud_cookie="",
            icloud_forward_to="global@example.com",
            tempmail_api_url="http://tempmail-api-1:8080",
            tempmail_api_key="test-key",
            icloud_hme_helper_api_url="http://helper-api",
            icloud_hme_helper_internal_key="helper-key",
        )
        account = MailboxAccount(
            email="alias@icloud.com",
            account_id="lease-1",
            extra={
                "provider": "icloud_hme",
                "mode": "helper_ready_api",
                "lease_id": "lease-1",
                "forward_to": "specific@example.com",
                "forward_mailbox_id": "specific-mbox",
            },
        )

        mailbox._helper_client.list_emails = Mock(side_effect=AssertionError("helper list-emails must not be called"))
        mailbox._tempmail_mailbox.ensure_mailbox_by_email = Mock(
            side_effect=lambda email, force_lookup=False: MailboxAccount(
                email=email,
                account_id=f"mbox-{email}",
            )
        )
        mailbox._tempmail_mailbox._list_emails = Mock(
            side_effect=lambda mailbox_id: [{"id": "old-1"}] if mailbox_id == "specific-mbox" else []
        )

        ids = mailbox.get_current_ids(account)

        self.assertIn("old-1", ids)
        mailbox._helper_client.list_emails.assert_not_called()
        self.assertEqual(
            [call.args[0] for call in mailbox._tempmail_mailbox._list_emails.call_args_list],
            ["specific-mbox"],
        )
        mailbox._tempmail_mailbox.ensure_mailbox_by_email.assert_not_called()

    def test_helper_ready_forward_to_without_mailbox_id_scans_only_that_forward(self):
        mailbox = IcloudHmeMailbox(
            icloud_hme_mode="helper_ready_api",
            icloud_cookie="",
            icloud_forward_to="global@example.com, second@example.com",
            tempmail_api_url="http://tempmail-api-1:8080",
            tempmail_api_key="test-key",
            icloud_hme_helper_api_url="http://helper-api",
            icloud_hme_helper_internal_key="helper-key",
        )
        account = MailboxAccount(
            email="alias@icloud.com",
            account_id="lease-1",
            extra={
                "provider": "icloud_hme",
                "mode": "helper_ready_api",
                "lease_id": "lease-1",
                "forward_to": "specific@example.com",
            },
        )

        mailbox._tempmail_mailbox.ensure_mailbox_by_email = Mock(
            side_effect=lambda email, force_lookup=False: MailboxAccount(
                email=email,
                account_id=f"mbox-{email}",
            )
        )
        mailbox._tempmail_mailbox._list_emails = Mock(return_value=[])

        ids = mailbox.get_current_ids(account)

        self.assertEqual(ids, set())
        self.assertEqual(
            [call.args[0] for call in mailbox._tempmail_mailbox.ensure_mailbox_by_email.call_args_list],
            ["specific@example.com"],
        )
        self.assertEqual(
            [call.args[0] for call in mailbox._tempmail_mailbox._list_emails.call_args_list],
            ["mbox-specific@example.com"],
        )

    def test_helper_ready_without_explicit_forward_target_scans_configured_forwards(self):
        mailbox = IcloudHmeMailbox(
            icloud_hme_mode="helper_ready_api",
            icloud_cookie="",
            icloud_forward_to="global@example.com, second@example.com",
            tempmail_api_url="http://tempmail-api-1:8080",
            tempmail_api_key="test-key",
            icloud_hme_helper_api_url="http://helper-api",
            icloud_hme_helper_internal_key="helper-key",
        )
        account = MailboxAccount(
            email="alias@icloud.com",
            account_id="lease-1",
            extra={
                "provider": "icloud_hme",
                "mode": "helper_ready_api",
                "lease_id": "lease-1",
            },
        )

        mailbox._tempmail_mailbox.ensure_mailbox_by_email = Mock(
            side_effect=lambda email, force_lookup=False: MailboxAccount(
                email=email,
                account_id=f"mbox-{email}",
            )
        )
        mailbox._tempmail_mailbox._list_emails = Mock(
            side_effect=lambda mailbox_id: [{"id": mailbox_id}]
        )

        ids = mailbox.get_current_ids(account)

        self.assertEqual(ids, {"mbox-global@example.com", "mbox-second@example.com"})
        self.assertEqual(
            [call.args[0] for call in mailbox._tempmail_mailbox.ensure_mailbox_by_email.call_args_list],
            ["global@example.com", "second@example.com"],
        )
        self.assertEqual(
            [call.args[0] for call in mailbox._tempmail_mailbox._list_emails.call_args_list],
            ["mbox-global@example.com", "mbox-second@example.com"],
        )

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
