import unittest
from unittest import mock

from core.base_mailbox import MailboxAccount
from core.db import AccountModel
from services.chatgpt_core import restored_email_service
from services.chatgpt_core.refresh_token_registration_engine import EmailServiceAdapter


class RestoredEmailServiceTests(unittest.TestCase):
    def test_create_email_reuses_saved_mailbox(self):
        state = {
            "provider": "dummy",
            "email": "restored@example.com",
            "account": {
                "email": "restored@example.com",
                "account_id": "mailbox-1",
                "extra": {},
            },
            "before_ids": ["old-1"],
        }

        class _DummyMailbox:
            def get_current_ids(self, account):
                return {"old-1", "old-2"}

        with mock.patch.object(
            restored_email_service,
            "create_mailbox",
            return_value=_DummyMailbox(),
        ):
            service = restored_email_service.RestoredEmailService(state=state)
            payload = service.create_email()

        self.assertEqual(payload["email"], "restored@example.com")
        self.assertEqual(payload["service_id"], "mailbox-1")
        self.assertEqual(payload["mailbox_action"], "restored_existing")
        self.assertEqual(
            service.export_state()["before_ids"],
            ["old-1", "old-2"],
        )

    def test_processed_otp_message_is_added_to_before_ids(self):
        state = {
            "provider": "dummy",
            "email": "restored@example.com",
            "account": {
                "email": "restored@example.com",
                "account_id": "mailbox-1",
                "extra": {},
            },
            "before_ids": [],
        }

        class _DummyMailbox:
            _last_verification_result = {}

            def get_current_ids(self, account):
                return set()

            def wait_for_code(self, account, **kwargs):
                self._last_verification_result = {
                    "message_id": "otp-message-1",
                    "code": "476433",
                }
                return "476433"

        with mock.patch.object(
            restored_email_service,
            "create_mailbox",
            return_value=_DummyMailbox(),
        ):
            service = restored_email_service.RestoredEmailService(state=state)
            service.create_email()
            adapter = EmailServiceAdapter(
                service,
                "restored@example.com",
                lambda *_args: None,
            )

            code = adapter.wait_for_verification_code(
                "restored@example.com",
                timeout=1,
                phase="oauth_email_otp",
                phase_label="OAuth email verification",
            )
            exported = service.export_state()

        self.assertEqual(code, "476433")
        self.assertIn("otp-message-1", exported["before_ids"])

    def test_email_adapter_excludes_codes_from_related_phases(self):
        service = mock.Mock()
        service.get_verification_code.return_value = "654321"
        adapter = EmailServiceAdapter(
            service,
            "restored@example.com",
            lambda *_args: None,
        )
        adapter._used_codes_by_phase["register_email_otp"] = {"123456"}
        adapter._used_codes_by_phase["browser_register_email_otp"] = {"234567"}

        code = adapter.wait_for_verification_code(
            "restored@example.com",
            timeout=30,
            exclude_codes=adapter.used_codes_for_phases(
                "register_email_otp",
                "browser_register_email_otp",
            ),
            phase="browser_oauth_email_otp",
        )

        self.assertEqual(code, "654321")
        self.assertEqual(
            service.get_verification_code.call_args.kwargs["exclude_codes"],
            {"123456", "234567"},
        )

    def test_email_adapter_can_ignore_registration_otp_budget(self):
        service = mock.Mock()
        service.get_verification_code.return_value = "654321"
        budget = mock.Mock()
        budget.plan_wait.return_value = mock.Mock(exhausted=True)
        adapter = EmailServiceAdapter(
            service,
            "restored@example.com",
            lambda *_args: None,
            otp_budget=budget,
        )

        code = adapter.wait_for_verification_code(
            "restored@example.com",
            timeout=120,
            phase="browser_oauth_email_otp",
            ignore_budget=True,
        )

        self.assertEqual(code, "654321")
        budget.plan_wait.assert_not_called()
        effective_timeout = service.get_verification_code.call_args.kwargs["timeout"]
        self.assertGreater(effective_timeout, 0)
        self.assertLessEqual(effective_timeout, 120)

    def test_expired_tempmail_mailbox_is_recreated_by_exact_email(self):
        state = {
            "provider": "tempmail_local",
            "email": "restored@example.com",
            "account": {
                "email": "restored@example.com",
                "account_id": "old-mailbox",
                "extra": {},
            },
            "before_ids": ["old-1"],
            "config": {
                "tempmail_api_url": "http://tempmail-api-1:8080",
                "tempmail_api_key": "test",
            },
        }

        class _TempMailMailbox:
            def __init__(self):
                self.ensured_email = ""
                self.baseline_account_id = ""

            def ensure_mailbox_by_email(self, email):
                self.ensured_email = email
                return MailboxAccount(
                    email=email,
                    account_id="new-mailbox",
                    extra={"mailbox_action": "created_exact_address"},
                )

            def get_current_ids(self, account):
                self.baseline_account_id = account.account_id
                return {"new-1"}

        mailbox = _TempMailMailbox()
        logs = []
        with mock.patch.object(
            restored_email_service,
            "create_mailbox",
            return_value=mailbox,
        ):
            service = restored_email_service.RestoredEmailService(
                state=state,
                log_fn=lambda message, level="info": logs.append(message),
            )
            payload = service.create_email()
            exported = service.export_state()

        self.assertEqual(mailbox.ensured_email, "restored@example.com")
        self.assertEqual(mailbox.baseline_account_id, "new-mailbox")
        self.assertEqual(payload["service_id"], "new-mailbox")
        self.assertEqual(payload["mailbox_action"], "created_exact_address")
        self.assertEqual(exported["account"]["account_id"], "new-mailbox")
        self.assertTrue(any("recreated expired TempMail address" in line for line in logs))

    def test_manual_mailbox_receives_task_control(self):
        state = {
            "provider": "manual_email_otp",
            "email": "manual@example.com",
            "account": {
                "email": "manual@example.com",
                "account_id": "manual@example.com",
                "extra": {},
            },
            "before_ids": [],
        }

        class _ManualMailbox:
            def get_current_ids(self, account):
                return set()

        mailbox = _ManualMailbox()
        with mock.patch.object(
            restored_email_service,
            "create_mailbox",
            return_value=mailbox,
        ):
            service = restored_email_service.RestoredEmailService(
                state=state,
                task_control="task-control",
                attempt_id=123,
            )
            service.create_email()

        self.assertEqual(getattr(mailbox, "_task_control", None), "task-control")
        self.assertEqual(getattr(mailbox, "_task_attempt_token", None), 123)

    def test_hme_ready_stored_state_keeps_account_route_and_refreshes_runtime_credentials(self):
        account = AccountModel(
            platform="chatgpt",
            email="alias@icloud.com",
            password="pw",
            token="",
            status="registered",
            extra_json=(
                '{"chatgpt_mailbox_state":{'
                '"provider":"icloud_hme",'
                '"email":"alias@icloud.com",'
                '"account":{"email":"alias@icloud.com","account_id":"anon-1",'
                '"extra":{"provider":"icloud_hme","forward_to":"old@example.com",'
                '"forward_mailbox_id":"old-mailbox"}},'
                '"config":{"tempmail_api_url":"http://old-api",'
                '"tempmail_api_key":"old-key","icloud_forward_to":"old@example.com",'
                '"icloud_forward_mailbox_id":"old-mailbox"}}}'
            ),
        )

        with mock.patch.object(
            restored_email_service.config_store,
            "get_all",
            return_value={
                "tempmail_api_url": "http://new-api",
                "tempmail_api_key": "new-key",
                "tempmail_api_key_header": "Authorization",
                "icloud_hme_mode": "import_pool",
                "icloud_forward_to": "current@example.com",
                "icloud_forward_mailbox_id": "new-mailbox",
            },
        ):
            state = restored_email_service.mailbox_state_from_account(account)

        self.assertEqual(state["config"]["tempmail_api_url"], "http://new-api")
        self.assertEqual(state["config"]["tempmail_api_key"], "new-key")
        self.assertEqual(state["config"]["icloud_forward_to"], "current@example.com")
        self.assertEqual(state["config"]["icloud_hme_mode"], "helper_ready_api")
        self.assertNotIn("icloud_forward_mailbox_id", state["config"])
        self.assertEqual(state["provider"], "hme_ready_api")
        self.assertEqual(state["account"]["extra"]["forward_to"], "old@example.com")
        self.assertEqual(state["account"]["extra"]["forward_mailbox_id"], "old-mailbox")

    def test_icloud_stored_state_drops_stale_config_forward_id_when_current_empty(self):
        account = AccountModel(
            platform="chatgpt",
            email="alias@icloud.com",
            password="pw",
            token="",
            status="registered",
            extra_json=(
                '{"chatgpt_mailbox_state":{'
                '"provider":"icloud_hme",'
                '"email":"alias@icloud.com",'
                '"account":{"email":"alias@icloud.com","account_id":"anon-1",'
                '"extra":{"provider":"icloud_hme","forward_to":"old@example.com",'
                '"forward_mailbox_id":"cached-mailbox"}},'
                '"config":{"tempmail_api_url":"http://old-api",'
                '"tempmail_api_key":"old-key","icloud_forward_to":"old@example.com",'
                '"icloud_forward_mailbox_id":"stale-config-mailbox"}}}'
            ),
        )

        with mock.patch.object(
            restored_email_service.config_store,
            "get_all",
            return_value={
                "tempmail_api_url": "http://new-api",
                "tempmail_api_key": "new-key",
                "icloud_hme_mode": "import_pool",
                "icloud_forward_to": "current@example.com",
                "icloud_forward_mailbox_id": "",
            },
        ):
            state = restored_email_service.mailbox_state_from_account(account)

        self.assertEqual(state["config"]["tempmail_api_url"], "http://new-api")
        self.assertNotIn("icloud_forward_mailbox_id", state["config"])
        self.assertEqual(state["provider"], "hme_ready_api")
        self.assertEqual(state["account"]["extra"]["forward_to"], "old@example.com")
        self.assertEqual(state["account"]["extra"]["forward_mailbox_id"], "cached-mailbox")

    def test_icloud_state_with_helper_lease_keeps_helper_mode(self):
        state = {
            "provider": "icloud_hme",
            "email": "helper-hme@icloud.com",
                "account": {
                    "email": "helper-hme@icloud.com",
                    "account_id": "lease-1",
                    "extra": {
                        "provider": "icloud_hme",
                        "lease_id": "lease-1",
                        "registration_id": "reg-1",
                        "logical_address_id": "logical-1",
                        "physical_alias_id": "physical-1",
                        "tag": "f8k2mq",
                        "tag_namespace": "random_tag",
                    },
                },
            "config": {"icloud_hme_mode": "live"},
        }

        with mock.patch.object(
            restored_email_service.config_store,
            "get_all",
            return_value={
                "icloud_hme_mode": "helper_ready_api",
                "icloud_hme_helper_api_url": "http://helper",
                "icloud_hme_helper_internal_key": "secret",
                "icloud_forward_to": "current@example.com",
                "tempmail_api_url": "http://tempmail",
                "tempmail_api_key": "test-key",
            },
        ):
            restored = restored_email_service._with_current_mailbox_config(state)

        self.assertEqual(restored["config"]["icloud_hme_mode"], "helper_ready_api")
        self.assertEqual(restored["account"]["extra"]["lease_id"], "lease-1")
        self.assertEqual(restored["account"]["extra"]["registration_id"], "reg-1")
        self.assertEqual(restored["account"]["extra"]["platform"], "chatgpt")


if __name__ == "__main__":
    unittest.main()
