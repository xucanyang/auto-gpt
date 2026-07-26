import unittest
from unittest.mock import Mock, patch

from core.base_mailbox import IcloudHmeMailbox, MailboxAccount
from services.chatgpt_core.refresh_token_registration_engine import (
    RegistrationResult,
    RefreshTokenRegistrationEngine,
)


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

    def test_helper_ready_export_is_minimal_and_has_no_icloud_cookie(self):
        mailbox = IcloudHmeMailbox(
            mail_provider_name="hme_ready_api",
            icloud_hme_mode="helper_ready_api",
            icloud_cookie="apple-cookie-must-not-be-exported",
            icloud_forward_to="global@example.com",
            icloud_forward_mailbox_id="global-mailbox",
            tempmail_api_url="http://tempmail-api-1:8080",
            tempmail_api_key="tempmail-key",
            tempmail_api_key_header="X-TempMail-Key",
            icloud_hme_helper_api_url="http://helper-api",
            icloud_hme_helper_internal_key="helper-key",
            icloud_hme_helper_api_key_header="X-Internal-Key",
            icloud_hme_helper_consumer="auto-gpt/test",
        )

        exported = mailbox.export_state_config()

        self.assertEqual(exported["icloud_hme_mode"], "helper_ready_api")
        self.assertEqual(exported["icloud_hme_helper_api_url"], "http://helper-api")
        self.assertEqual(exported["tempmail_api_url"], "http://tempmail-api-1:8080")
        self.assertNotIn("icloud_cookie", exported)
        self.assertNotIn("chatgpt_gopay_batch_tasks", exported)

    def test_helper_ready_prepare_sends_parent_task_id_separately_from_attempt_key(self):
        mailbox = IcloudHmeMailbox(
            mail_provider_name="hme_ready_api",
            icloud_hme_mode="helper_ready_api",
            icloud_cookie="",
            icloud_forward_to="global@example.com",
            tempmail_api_url="http://tempmail-api-1:8080",
            tempmail_api_key="tempmail-key",
            icloud_hme_helper_api_url="http://helper-api",
            icloud_hme_helper_internal_key="helper-key",
            icloud_hme_helper_consumer="auto-gpt/test",
        )
        mailbox._task_attempt_token = "attempt-uuid-1"
        mailbox._registration_task_id = "task-parent-1"
        mailbox._helper_client.prepare = Mock(
            return_value={
                "auto_gpt": {
                    "email": "alias+gpt1@icloud.com",
                    "account_id": "ck-1",
                    "extra": {
                        "lease_id": "ck-1",
                        "checkout_id": "ck-1",
                        "forward_to": "specific@example.com",
                    },
                },
                "mailbox": {
                    "email": "alias+gpt1@icloud.com",
                    "forward_to": "specific@example.com",
                    "forward_mailbox_id": "",
                },
                "lease": {"id": "ck-1"},
            }
        )

        account = mailbox.get_email()

        mailbox._helper_client.prepare.assert_called_once_with(
            forward_to="*",
            platform="chatgpt",
            request_id="attempt-uuid-1",
            task_id="task-parent-1",
            consumer="auto-gpt/test",
            ttl_ms=None,
            max_cache_age_ms=86400000,
        )
        self.assertEqual(account.email, "alias+gpt1@icloud.com")
        self.assertEqual(account.extra["forward_to"], "specific@example.com")
        self.assertNotIn("forward_mailbox_id", account.extra)

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

    def test_helper_ready_reads_tagged_hme_otp_from_tempmail_return_path(self):
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
            email="reviser.smiths_2f+gpt1@icloud.com",
            account_id="lease-tag-1",
            extra={
                "provider": "hme_ready_api",
                "mode": "helper_ready_api",
                "lease_id": "lease-tag-1",
                "forward_to": "specific@example.com",
                "forward_mailbox_id": "specific-mbox",
            },
        )

        mailbox._helper_client.wait_code = Mock(side_effect=AssertionError("helper wait-code must not be called"))
        mailbox._tempmail_mailbox._list_emails = Mock(
            return_value=[{"id": "tagged-msg-1", "subject": "OpenAI verification"}]
        )
        mailbox._tempmail_mailbox._get_email_detail = Mock(
            return_value={
                # Apple-visible recipient metadata is deliberately physical.
                "received_for": ["reviser.smiths_2f@icloud.com"],
                "subject": "OpenAI verification",
                "body_text": "Your verification code is 123456.",
                "raw_message": (
                    "Return-Path: <bounce+abc-reviser.smiths_2f+gpt1=icloud.com_at_tm1_openai_com_abc@icloud.com>\r\n"
                    "To: Hide My Email <reviser.smiths_2f@icloud.com>\r\n"
                    "X-ICLOUD-HME: p=reviser.smiths_2f@icloud.com; f=specific@example.com;\r\n\r\n"
                    "Your verification code is 123456."
                ),
            }
        )

        code = mailbox.wait_for_code(account, timeout=1)

        self.assertEqual(code, "123456")
        mailbox._helper_client.wait_code.assert_not_called()
        self.assertEqual(mailbox._last_verification_result["alias_match_source"], "tagged_hme_transport_header")

    def test_tagged_hme_transport_match_never_falls_back_or_prefix_matches(self):
        expected = "reviser.smiths_2f+gpt1@icloud.com"

        self.assertTrue(
            IcloudHmeMailbox._tagged_hme_headers_match_alias(
                "Return-Path: <bounce+abc-reviser.smiths_2f+gpt1=icloud.com_at_tm1_openai_com_abc@icloud.com>\r\n",
                expected,
            )
        )
        self.assertFalse(
            IcloudHmeMailbox._tagged_hme_headers_match_alias(
                "Return-Path: <bounce+abc-reviser.smiths_2f+gpt2=icloud.com_at_tm1_openai_com_abc@icloud.com>\r\n",
                expected,
            )
        )
        random_alias = "reviser.smiths_2f+f8k2mq@icloud.com"
        self.assertTrue(
            IcloudHmeMailbox._tagged_hme_headers_match_alias(
                "Return-Path: <bounce+abc-reviser.smiths_2f+f8k2mq=icloud.com_at_tm1_openai_com_abc@icloud.com>\r\n",
                random_alias,
            )
        )
        self.assertFalse(
            IcloudHmeMailbox._tagged_hme_headers_match_alias(
                "Return-Path: <bounce+abc-reviser.smiths_2f+8f2k6m=icloud.com_at_tm1_openai_com_abc@icloud.com>\r\n",
                random_alias,
            )
        )
        self.assertFalse(
            IcloudHmeMailbox._tagged_hme_headers_match_alias(
                "Return-Path: <bounce+abc-reviser.smiths_2f+gpt10=icloud.com_at_tm1_openai_com_abc@icloud.com>\r\n",
                expected,
            )
        )
        self.assertFalse(
            IcloudHmeMailbox._tagged_hme_headers_match_alias(
                "X-ICLOUD-HME: p=reviser.smiths_2f@icloud.com;\r\n\r\n"
                "quoted body reviser.smiths_2f+gpt1=icloud.com_at_tm1_openai_com",
                expected,
            )
        )

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

        self.assertEqual(
            ids,
            {
                "mbox-global@example.com:mbox-global@example.com",
                "mbox-second@example.com:mbox-second@example.com",
            },
        )
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
            outcome = mailbox.finalize_failure(
                account,
                error_message="访问首页失败: timeout",
                task_id="task-2",
            )

        self.assertEqual(outcome, "early_failure")
        release_alias.assert_called_once()
        mark_failure.assert_not_called()
        self.assertEqual(release_alias.call_args.args[0], "anon-2")

    def test_classify_helper_failure_outcome_buckets(self):
        classify = IcloudHmeMailbox._classify_helper_failure_outcome
        self.assertEqual(
            classify("InvalidIP: Failed to get IP address: ipecho.net SSL"),
            "early_failure",
        )
        self.assertEqual(
            classify(
                "user_already_exists: browser registration reached login_password; "
                "use explicit existing-account capture"
            ),
            "keep",
        )
        self.assertEqual(
            classify("浏览器注册检测到该邮箱已存在；请显式启用已有账号抓取"),
            "keep",
        )
        self.assertEqual(
            classify("任务已手动停止: registration interrupted after mailbox lease"),
            "late_failure",
        )
        self.assertEqual(classify("未获取到验证码"), "late_failure")
        self.assertEqual(classify("获取 CSRF token 失败"), "early_failure")

    def test_helper_user_already_exists_finalizes_as_keep(self):
        mailbox = IcloudHmeMailbox(
            mail_provider_name="hme_ready_api",
            icloud_hme_mode="helper_ready_api",
            icloud_cookie="",
            icloud_forward_to="global@example.com",
            tempmail_api_url="http://tempmail-api-1:8080",
            tempmail_api_key="tempmail-key",
            icloud_hme_helper_api_url="http://helper-api",
            icloud_hme_helper_internal_key="helper-key",
        )
        account = MailboxAccount(
            email="dirty@icloud.com",
            account_id="lease-dirty",
            extra={
                "provider": "hme_ready_api",
                "lease_id": "lease-dirty",
                "registration_id": "reg-dirty",
            },
        )
        mailbox._helper_client.finalize = Mock(return_value={})

        outcome = mailbox.finalize_failure(
            account,
            error_message="浏览器注册检测到该邮箱已存在；当前注册执行器不会自动切换登录恢复",
            task_id="task-dirty",
        )

        self.assertEqual(outcome, "keep")
        mailbox._helper_client.finalize.assert_called_once()
        kwargs = mailbox._helper_client.finalize.call_args.kwargs
        self.assertEqual(kwargs["outcome"], "keep")
        self.assertEqual(kwargs["task_id"], "task-dirty")

    def test_helper_invalidip_finalizes_as_early_failure(self):
        mailbox = IcloudHmeMailbox(
            mail_provider_name="hme_ready_api",
            icloud_hme_mode="helper_ready_api",
            icloud_cookie="",
            icloud_forward_to="global@example.com",
            tempmail_api_url="http://tempmail-api-1:8080",
            tempmail_api_key="tempmail-key",
            icloud_hme_helper_api_url="http://helper-api",
            icloud_hme_helper_internal_key="helper-key",
        )
        account = MailboxAccount(
            email="fresh@icloud.com",
            account_id="lease-fresh",
            extra={"provider": "hme_ready_api", "lease_id": "lease-fresh"},
        )
        mailbox._helper_client.finalize = Mock(return_value={})

        outcome = mailbox.finalize_failure(
            account,
            error_message=(
                "browser_registration_failed: InvalidIP: Failed to get IP address: "
                "HTTPSConnectionPool(host='ipecho.net')"
            ),
            task_id="task-geoip",
        )

        self.assertEqual(outcome, "early_failure")
        kwargs = mailbox._helper_client.finalize.call_args.kwargs
        self.assertEqual(kwargs["outcome"], "early_failure")

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

    def test_helper_prepare_persists_platform_registration_identity(self):
        mailbox = IcloudHmeMailbox(
            mail_provider_name="hme_ready_api",
            icloud_hme_mode="helper_ready_api",
            icloud_cookie="",
            icloud_forward_to="global@example.com",
            tempmail_api_url="http://tempmail-api-1:8080",
            tempmail_api_key="tempmail-key",
            icloud_hme_helper_api_url="http://helper-api",
            icloud_hme_helper_internal_key="helper-key",
        )
        mailbox._helper_client.prepare = Mock(
            return_value={
                "platform": "ChatGPT",
                "registration_id": "reg-1",
                "logical_address_id": "logical-1",
                "physical_alias_id": "physical-1",
                "lease_id": "lease-1",
                "lease_state": "checked_out",
                "email": "base+f8k2mq@icloud.com",
                "physical_hme": "base@icloud.com",
                "logical_type": "tag",
                "tag": "f8k2mq",
                "tag_namespace": "random_tag",
                "tag_slot": 1,
                "forward_to": "global@example.com",
            }
        )

        account = mailbox.get_email()

        self.assertEqual(account.account_id, "lease-1")
        self.assertEqual(account.extra["platform"], "chatgpt")
        self.assertEqual(account.extra["registration_id"], "reg-1")
        self.assertEqual(account.extra["logical_address_id"], "logical-1")
        self.assertEqual(account.extra["physical_alias_id"], "physical-1")
        self.assertEqual(account.extra["physical_hme"], "base@icloud.com")
        self.assertEqual(account.extra["tag"], "f8k2mq")
        self.assertEqual(account.extra["tag_namespace"], "random_tag")
        self.assertEqual(account.extra["tag_slot"], 1)

    def test_invalid_prepare_email_early_finalizes_known_lease(self):
        mailbox = IcloudHmeMailbox(
            icloud_hme_mode="helper_ready_api",
            icloud_cookie="",
            icloud_forward_to="global@example.com",
            tempmail_api_url="http://tempmail-api-1:8080",
            tempmail_api_key="tempmail-key",
            icloud_hme_helper_api_url="http://helper-api",
            icloud_hme_helper_internal_key="helper-key",
        )
        mailbox._registration_task_id = "task-1"
        mailbox._helper_client.prepare = Mock(
            return_value={
                "email": "not-an-email",
                "lease_id": "lease-invalid",
                "registration_id": "reg-invalid",
                "logical_address_id": "logical-invalid",
                "physical_alias_id": "physical-invalid",
            }
        )
        mailbox._helper_client.finalize = Mock(return_value={})

        with self.assertRaises(RuntimeError):
            mailbox.get_email()

        mailbox._helper_client.finalize.assert_called_once_with(
            "lease-invalid",
            outcome="early_failure",
            reason="invalid_prepare_email",
            registration_id="reg-invalid",
            logical_address_id="logical-invalid",
            physical_alias_id="physical-invalid",
            platform="chatgpt",
            task_id="task-1",
        )

    def test_helper_finalize_uses_registration_and_lease_and_merges_state(self):
        mailbox = self._build_mailbox()
        mailbox._icloud_hme_mode = "helper_ready_api"
        account = MailboxAccount(
            email="base+f8k2mq@icloud.com",
            account_id="lease-1",
            extra={
                "provider": "hme_ready_api",
                "mode": "helper_ready_api",
                "lease_id": "lease-1",
                "registration_id": "reg-1",
                "logical_address_id": "logical-1",
                "physical_alias_id": "physical-1",
                "tag": "f8k2mq",
                "tag_namespace": "random_tag",
            },
        )
        mailbox._helper_client.finalize = Mock(
            return_value={
                "registration_id": "reg-1",
                "logical_address_id": "logical-1",
                "physical_alias_id": "physical-1",
                "lease_id": "lease-1",
                "platform": "chatgpt",
                "lease_state": "committed",
            }
        )

        mailbox.finalize_success(account, registered_email="account@example.com", task_id="task-1")

        kwargs = mailbox._helper_client.finalize.call_args.kwargs
        self.assertEqual(kwargs["registration_id"], "reg-1")
        self.assertEqual(kwargs["logical_address_id"], "logical-1")
        self.assertEqual(kwargs["platform"], "chatgpt")
        self.assertEqual(kwargs["bound_account_email"], "account@example.com")
        self.assertEqual(account.extra["lease_state"], "committed")

    def test_base_body_address_is_not_a_routing_match(self):
        self.assertFalse(
            IcloudHmeMailbox._base_hme_headers_match_alias(
                "Subject: quoted alias@example.com\r\n\r\nbody alias@example.com",
                "alias@example.com",
                [],
            )
        )
        self.assertTrue(
            IcloudHmeMailbox._base_hme_headers_match_alias(
                "Delivered-To: alias@example.com\r\n\r\nbody",
                "alias@example.com",
                [],
            )
        )

    def test_multi_forward_same_provider_message_id_keeps_mailbox_namespace(self):
        mailbox = IcloudHmeMailbox(
            icloud_hme_mode="helper_ready_api",
            icloud_cookie="",
            icloud_forward_to="one@example.com, two@example.com",
            tempmail_api_url="http://tempmail-api-1:8080",
            tempmail_api_key="test-key",
            icloud_hme_helper_api_url="http://helper-api",
            icloud_hme_helper_internal_key="helper-key",
        )
        account = MailboxAccount(
            email="base+f8k2mq@icloud.com",
            account_id="lease-1",
            extra={
                "provider": "hme_ready_api",
                "mode": "helper_ready_api",
                "lease_id": "lease-1",
            },
        )
        mailbox._tempmail_mailbox.ensure_mailbox_by_email = Mock(
            side_effect=lambda email, force_lookup=False: MailboxAccount(
                email=email,
                account_id=f"mb-{email.split('@', 1)[0]}",
            )
        )
        mailbox._tempmail_mailbox._list_emails = Mock(
            side_effect=lambda mailbox_id: [{"id": "same-message", "subject": "OpenAI verification"}]
        )

        def detail(mailbox_id, _message_id):
            expected = mailbox_id == "mb-two"
            token = "base+f8k2mq" if expected else "base+other1"
            return {
                "received_for": ["base@icloud.com"],
                "subject": "OpenAI verification",
                "body_text": "Your verification code is 123456.",
                "raw_message": (
                    f"Return-Path: <bounce+{token}=icloud.com_at_tm_openai_com@icloud.com>\r\n\r\n"
                    "Your verification code is 123456."
                ),
            }

        mailbox._tempmail_mailbox._get_email_detail = Mock(side_effect=detail)

        code = mailbox.wait_for_code(account, timeout=1)

        self.assertEqual(code, "123456")
        self.assertEqual(mailbox._last_verification_result["message_id"], "mb-two:same-message")
        self.assertEqual(mailbox._last_verification_result["message_id_namespace"], "mb-two")

    def test_finalize_success_reexports_authoritative_mailbox_state(self):
        class _Service:
            def __init__(self):
                self.state = {"lease_state": "checked_out"}
                self.finalized = False

            def finalize_success(self, **kwargs):
                self.finalized = True
                self.state = {"lease_state": "committed", "registration_id": "reg-1"}

            def export_state(self):
                return dict(self.state)

        service = _Service()
        engine = RefreshTokenRegistrationEngine.__new__(RefreshTokenRegistrationEngine)
        engine.email_service = service
        engine.task_uuid = "task-1"
        engine._log = lambda *args, **kwargs: None
        result = RegistrationResult(
            success=True,
            email="account@example.com",
            metadata={"mailbox_state": {"lease_state": "checked_out"}},
        )

        engine._finalize_email_service_success(result)

        self.assertTrue(service.finalized)
        self.assertEqual(result.metadata["mailbox_state"]["lease_state"], "committed")
        self.assertEqual(result.metadata["mailbox_state"]["registration_id"], "reg-1")


if __name__ == "__main__":
    unittest.main()
