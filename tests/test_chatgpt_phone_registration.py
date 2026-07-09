import unittest
from unittest import mock

from core.base_platform import AccountStatus, RegisterConfig
from services.chatgpt_core.phone_registration_engine import PhoneRegistrationEngine
from services.chatgpt_core.plugin import ChatGPTPlatform


class FakeEntry:
    country_slug = "uploaded"
    phone = "+573234567890"
    api_url = "https://sms.example/api?id=1"
    detail_url = api_url
    raw_line = "+573234567890----https://sms.example/api?id=1"
    line_no = 1


class FakePhoneService:
    enabled = True
    max_attempts = 1
    max_resend_attempts = 0
    resend_interval_seconds = 0
    last_expired_date = "2026-12-31"
    last_code_time = "2026-06-18 12:00:00"
    last_code_was_extracted = False
    validate_delay_seconds = 0

    def __init__(self):
        self.entry = FakeEntry()
        self.sms_sent = []
        self.completed = []
        self.cancelled = []

    def acquire_phone(self, **kwargs):
        return self.entry

    def prefix_hint(self, phone):
        return phone[:7]

    def mark_sms_sent(self, entry):
        self.sms_sent.append(entry.phone)

    def wait_for_code(self, entry, timeout=None):
        return "123456"

    def request_next_code(self, entry):
        return True

    def complete(self, entry):
        self.completed.append(entry.phone)

    def cancel(self, entry, reason=""):
        self.cancelled.append((entry.phone, reason))

    def mark_blacklisted(self, phone, reason=""):
        self.cancelled.append((phone, reason or "blacklisted"))


class NoCodeThenRetryPhoneService(FakePhoneService):
    max_resend_attempts = 1
    resend_interval_seconds = 60

    def __init__(self, retry_code="123456"):
        super().__init__()
        self.retry_code = retry_code
        self.wait_timeouts = []
        self.next_code_requests = 0

    def wait_for_code(self, entry, timeout=None):
        self.wait_timeouts.append(timeout)
        if len(self.wait_timeouts) == 1:
            return None
        return self.retry_code

    def request_next_code(self, entry):
        self.next_code_requests += 1
        return True


class FakePhoneSignupClient:
    calls = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        FakePhoneSignupClient.calls.append(("init", kwargs))

    def warm_chatgpt_and_signin(self, phone):
        FakePhoneSignupClient.calls.append(("signin", phone))
        return mock.Mock(final_url="https://auth.openai.com/create-account/password", status=200, redirects=[])

    def ensure_registration_route(self, auth_route, phone):
        FakePhoneSignupClient.calls.append(("ensure_registration_route", phone))

    def register_phone_password(self, phone, password):
        FakePhoneSignupClient.calls.append(("register_phone_password", phone, password))
        return {"page": {"type": "phone_otp_send"}, "continue_url": "https://auth.openai.com/api/accounts/phone-otp/send"}

    def maybe_send_phone_otp(self, continue_url, explicit_send=True):
        FakePhoneSignupClient.calls.append(("maybe_send_phone_otp", continue_url, explicit_send))
        return {"final_url": "https://auth.openai.com/contact-verification"}

    def resend_phone_otp(self):
        FakePhoneSignupClient.calls.append(("resend_phone_otp",))
        return True

    def validate_phone_otp(self, code):
        FakePhoneSignupClient.calls.append(("validate_phone_otp", code))
        return {"page": {"type": "about_you"}}

    def create_account(self, *, full_name, birthdate):
        FakePhoneSignupClient.calls.append(("create_account", full_name, birthdate))
        return {"continue_url": "https://chatgpt.com/api/auth/callback/openai?code=demo"}

    def follow_chatgpt_callback_and_capture(self, callback_url, phone):
        FakePhoneSignupClient.calls.append(("follow_callback", callback_url, phone))
        return {
            "access_token": "at-phone",
            "session_token": "session-phone",
            "cookies": "__Secure-next-auth.session-token=demo",
            "account_id": "acct-phone",
            "user_id": "user-phone",
            "phone_number": phone,
            "email": None,
            "country": "CO",
        }


class PhoneRegistrationEngineTests(unittest.TestCase):
    def setUp(self):
        FakePhoneSignupClient.calls = []

    def test_phone_signup_uses_phone_service_and_builds_access_token_account(self):
        phone_service = FakePhoneService()
        engine = PhoneRegistrationEngine(
            extra_config={"_current_task_id": "task-phone"},
            proxy_url="http://proxy.example:8080",
            browser_mode="protocol",
            callback_logger=lambda msg, *_: None,
            phone_service=phone_service,
            client_factory=FakePhoneSignupClient,
        )

        result = engine.run()
        account = engine.build_account(result)

        self.assertTrue(result.success)
        self.assertTrue(any("阶段 2/4：OpenAI 已接受手机号并发码" in line for line in engine.logs))
        self.assertTrue(any("阶段 3/4：等待短信验证码" in line for line in engine.logs))
        self.assertTrue(any("结果：成功，phone:+5732***7890，auth=access_token_only" in line for line in engine.logs))
        self.assertEqual(phone_service.sms_sent, ["+573234567890"])
        self.assertEqual(phone_service.completed, ["+573234567890"])
        self.assertEqual(account.email, "phone:+573234567890")
        self.assertEqual(account.token, "at-phone")
        self.assertEqual(account.status, AccountStatus.PENDING_PAYMENT)
        self.assertEqual(account.extra["chatgpt_registration_entry"], "phone_signup")
        self.assertEqual(account.extra["chatgpt_identifier_type"], "phone")
        self.assertEqual(account.extra["chatgpt_phone_binding"]["source"], "phone_signup")
        self.assertEqual(account.extra["chatgpt_phone_signup_result"]["status"], "registered_phone_signup")
        call_names = [item[0] for item in FakePhoneSignupClient.calls]
        self.assertIn("register_phone_password", call_names)
        self.assertIn("validate_phone_otp", call_names)
        self.assertNotIn("send_email_otp", call_names)

    def test_phone_signup_resends_once_and_waits_sixty_seconds_after_no_code(self):
        phone_service = NoCodeThenRetryPhoneService(retry_code="123456")
        logs = []
        engine = PhoneRegistrationEngine(
            extra_config={"_current_task_id": "task-phone"},
            proxy_url="",
            browser_mode="protocol",
            callback_logger=lambda msg, *_: logs.append(str(msg)),
            phone_service=phone_service,
            client_factory=FakePhoneSignupClient,
        )

        result = engine.run()

        self.assertTrue(result.success)
        self.assertEqual(phone_service.wait_timeouts, [None, 60])
        self.assertEqual(phone_service.next_code_requests, 1)
        self.assertIn(("resend_phone_otp",), FakePhoneSignupClient.calls)
        self.assertTrue(any("长时间未收到短信，重发验证码 1/1" in line for line in logs))

    def test_phone_signup_moves_to_next_phone_when_still_no_code_after_resend(self):
        phone_service = NoCodeThenRetryPhoneService(retry_code=None)
        engine = PhoneRegistrationEngine(
            extra_config={"_current_task_id": "task-phone"},
            proxy_url="",
            browser_mode="protocol",
            callback_logger=lambda msg, *_: None,
            phone_service=phone_service,
            client_factory=FakePhoneSignupClient,
        )

        result = engine.run()

        self.assertFalse(result.success)
        self.assertEqual(result.error_message, "手机号 +573234567890 未收到短信验证码")
        self.assertEqual(phone_service.wait_timeouts, [None, 60])
        self.assertEqual(phone_service.cancelled[0][0], "+573234567890")
        self.assertEqual(engine.panel_results[-1]["status"], "api_no_code")

    def test_phone_signup_upload_defaults_to_one_resend_with_sixty_second_wait(self):
        engine = PhoneRegistrationEngine(
            extra_config={
                "chatgpt_phone_signup_phone_lines": "+573234567890----https://sms.example/api?id=1",
            },
            callback_logger=lambda msg, *_: None,
            client_factory=FakePhoneSignupClient,
        )

        service = engine._build_phone_service()

        self.assertEqual(service.max_resend_attempts, 1)
        self.assertEqual(service.resend_interval_seconds, 60)

    def test_phone_signup_accepts_missing_phone_number_from_me_after_otp_success(self):
        class MissingPhoneClient(FakePhoneSignupClient):
            def follow_chatgpt_callback_and_capture(self, callback_url, phone):
                data = super().follow_chatgpt_callback_and_capture(callback_url, phone)
                data["phone_number"] = phone
                data["me_phone_number_missing"] = True
                data["email"] = "generated@example.openai"
                return data

        phone_service = FakePhoneService()
        engine = PhoneRegistrationEngine(
            extra_config={"_current_task_id": "task-phone"},
            proxy_url="",
            browser_mode="protocol",
            callback_logger=lambda msg, *_: None,
            phone_service=phone_service,
            client_factory=MissingPhoneClient,
        )

        result = engine.run()
        account = engine.build_account(result)

        self.assertTrue(result.success)
        self.assertEqual(account.email, "phone:+573234567890")
        self.assertTrue(account.extra["chatgpt_phone_signup"]["me_phone_number_missing"])

    def test_existing_phone_login_uses_same_password_and_saves_access_token_account(self):
        class ExistingPhoneLoginClient(FakePhoneSignupClient):
            def warm_chatgpt_and_signin(self, phone):
                FakePhoneSignupClient.calls.append(("signin_existing", phone))
                return mock.Mock(final_url="https://auth.openai.com/log-in/password", status=200, redirects=[])

            def verify_login_password_for_existing_phone(self, password):
                FakePhoneSignupClient.calls.append(("verify_login_password", password))
                return {"page": {"type": "contact_verification"}, "continue_url": "https://auth.openai.com/contact-verification"}

            def open_contact_verification_page(self, continue_url, referer=""):
                FakePhoneSignupClient.calls.append(("open_contact_verification", continue_url, referer))
                return {"final_url": "https://auth.openai.com/contact-verification"}

            def validate_phone_otp(self, code):
                FakePhoneSignupClient.calls.append(("validate_phone_otp", code))
                return {"page": {"type": "external_url"}, "continue_url": "https://chatgpt.com/api/auth/callback/openai?code=demo"}

            def register_phone_password(self, phone, password):
                raise AssertionError("已注册手机号登录不应该调用 user/register")

            def create_account(self, *, full_name, birthdate):
                raise AssertionError("完整已注册手机号登录不应该调用 create_account")

        phone_service = FakePhoneService()
        engine = PhoneRegistrationEngine(
            extra_config={"_current_task_id": "task-phone", "password": "SamePassword123!"},
            proxy_url="",
            browser_mode="protocol",
            callback_logger=lambda msg, *_: None,
            phone_service=phone_service,
            client_factory=ExistingPhoneLoginClient,
        )

        result = engine.run()
        account = engine.build_account(result)

        self.assertTrue(result.success)
        self.assertEqual(result.flow, "phone_existing_login")
        self.assertEqual(account.password, "SamePassword123!")
        self.assertEqual(account.email, "phone:+573234567890")
        self.assertEqual(account.extra["chatgpt_phone_auth_flow"], "phone_existing_login")
        self.assertEqual(account.extra["chatgpt_token_source"], "phone_existing_login")
        self.assertIn(("verify_login_password", "SamePassword123!"), FakePhoneSignupClient.calls)

    def test_phone_signup_pool_can_limit_selected_prefixes(self):
        calls = []

        class FakePhonePoolRepository:
            def list_available_by_prefixes(self, prefixes):
                calls.append(("list_available_by_prefixes", prefixes))
                return ["record-4438"]

            def to_phone_items(self, records, *, limit_accounts=0, expand_capacity=False):
                calls.append(("to_phone_items", records, limit_accounts, expand_capacity))
                return [
                    {
                        "phone": "+14438041780",
                        "api_url": "https://sms.example/api?id=4438",
                        "raw_line": "+14438041780----https://sms.example/api?id=4438",
                        "line_no": 1,
                        "pool_managed": True,
                    }
                ]

        engine = PhoneRegistrationEngine(
            extra_config={
                "chatgpt_phone_signup_use_pool": True,
                "chatgpt_phone_signup_prefix_bind_enabled": True,
                "chatgpt_phone_signup_selected_prefixes": ["4438", "4438", "bad"],
            },
            callback_logger=lambda msg, *_: None,
            client_factory=FakePhoneSignupClient,
        )

        with mock.patch(
            "services.chatgpt_core.phone_pool_repository.PhonePoolRepository",
            return_value=FakePhonePoolRepository(),
        ):
            service = engine._build_phone_service()

        self.assertTrue(service.enabled)
        self.assertEqual(engine._uploaded_entries[0].phone, "+14438041780")
        self.assertEqual(calls[0], ("list_available_by_prefixes", ["4438"]))
        self.assertEqual(calls[1][0], "to_phone_items")

    def test_phone_signup_prefix_sample_loads_all_sample_candidates(self):
        calls = []
        records = ["record-1226-a", "record-1226-b", "record-1343-a"]

        class FakePhonePoolRepository:
            def sample_testable_by_prefix(self, size):
                calls.append(("sample_testable_by_prefix", size))
                return records

            def restore_prefix_sample_records(self, record_ids):
                calls.append(("restore_prefix_sample_records", record_ids))
                return records

            def to_phone_items(self, sample_records, *, limit_accounts=0, expand_capacity=False):
                calls.append(("to_phone_items", list(sample_records), limit_accounts, expand_capacity))
                return [
                    {
                        "phone": f"+1555000000{index}",
                        "api_url": f"https://sms.example/api?id={index}",
                        "raw_line": f"+1555000000{index}----https://sms.example/api?id={index}",
                        "line_no": index,
                        "pool_managed": True,
                    }
                    for index, _record in enumerate(sample_records, start=1)
                ]

        engine = PhoneRegistrationEngine(
            extra_config={
                "_target_success_count": 1,
                "chatgpt_phone_signup_use_pool": True,
                "chatgpt_phone_signup_prefix_sample_enabled": True,
                "chatgpt_phone_signup_prefix_sample_size": 2,
            },
            callback_logger=lambda msg, *_: None,
            client_factory=FakePhoneSignupClient,
        )

        with mock.patch(
            "services.chatgpt_core.phone_pool_repository.PhonePoolRepository",
            return_value=FakePhonePoolRepository(),
        ):
            service = engine._build_phone_service()

        self.assertTrue(service.enabled)
        self.assertEqual(len(engine._uploaded_entries), 3)
        self.assertEqual(calls[0], ("sample_testable_by_prefix", 2))
        self.assertEqual(calls[-1], ("to_phone_items", records, 0, False))

    def test_phone_signup_browser_error_before_sms_bubbles_for_proxy_failover(self):
        class CsrfBlockedClient(FakePhoneSignupClient):
            def warm_chatgpt_and_signin(self, phone):
                raise RuntimeError("未拿到 CSRF: HTTP 403 <html>")

        phone_service = FakePhoneService()
        logs = []
        engine = PhoneRegistrationEngine(
            extra_config={"_current_task_id": "task-phone"},
            proxy_url="http://proxy.example:8080",
            browser_mode="protocol",
            callback_logger=lambda msg, *_: logs.append(str(msg)),
            phone_service=phone_service,
            client_factory=CsrfBlockedClient,
        )

        with self.assertRaisesRegex(RuntimeError, "HTTP 403"):
            engine.run()

        self.assertEqual(phone_service.sms_sent, [])
        self.assertEqual(phone_service.cancelled[0][0], "+573234567890")
        self.assertTrue(any("交给外层切换代理" in line for line in logs))

    def test_plugin_routes_phone_signup_without_mailbox(self):
        class ExplodingMailbox:
            def get_email(self):
                raise AssertionError("phone signup 不应该创建邮箱")

        fake_result = mock.Mock(success=True, error_message="")
        fake_account = mock.Mock(email="phone:+573234567890", extra={}, token="at-phone")
        fake_engine = mock.Mock()
        fake_engine.run.return_value = fake_result
        fake_engine.build_account.return_value = fake_account

        platform = ChatGPTPlatform(
            config=RegisterConfig(extra={"chatgpt_registration_entry": "phone_signup"}),
            mailbox=ExplodingMailbox(),
        )

        with mock.patch(
            "services.chatgpt_core.phone_registration_engine.PhoneRegistrationEngine",
            return_value=fake_engine,
        ) as engine_cls, mock.patch(
            "services.chatgpt_core.plugin.resolve_default_chatgpt_proxy",
            return_value="",
        ):
            account = platform.register(password="Secret123!A1")

        self.assertIs(account, fake_account)
        engine_cls.assert_called_once()
        kwargs = engine_cls.call_args.kwargs
        self.assertEqual(kwargs["extra_config"]["chatgpt_registration_entry"], "phone_signup")
        self.assertEqual(kwargs["extra_config"]["chatgpt_registration_mode"], "access_token_only")


if __name__ == "__main__":
    unittest.main()
