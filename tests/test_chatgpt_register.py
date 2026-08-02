import sys
import types
import unittest
from unittest import mock

smstome_tool_stub = types.ModuleType("smstome_tool")
smstome_tool_stub.PhoneEntry = type("PhoneEntry", (), {})
smstome_tool_stub.get_unused_phone = lambda *args, **kwargs: None
smstome_tool_stub.mark_phone_blacklisted = lambda *args, **kwargs: None
smstome_tool_stub.parse_country_slugs = lambda value: []
smstome_tool_stub.update_global_phone_list = lambda *args, **kwargs: 0
smstome_tool_stub.wait_for_otp = lambda *args, **kwargs: None
sys.modules.setdefault("smstome_tool", smstome_tool_stub)

from services.chatgpt_core.chatgpt_client import ChatGPTClient
from services.chatgpt_core.oauth_client import OAuthClient
from services.chatgpt_core.sentinel_browser import BrowserAccountCreateResult
from services.chatgpt_core.refresh_token_registration_engine import (
    EmailServiceAdapter,
    RegistrationResult,
    RefreshTokenRegistrationEngine,
)
from services.chatgpt_core.registration_route_policy import (
    ExistingAccountLoginRouteBlocked,
)
from core.task_runtime import SkipCurrentAttemptRequested
from services.chatgpt_core.utils import FlowState


class DummyEmailService:
    service_type = type("ST", (), {"value": "dummy"})()

    def create_email(self):
        return {"email": "user@example.com", "service_id": "svc-1"}

    def get_verification_code(self, **_kwargs):
        return "123456"


class TimeoutEmailService(DummyEmailService):
    def __init__(self):
        self.calls = []

    def get_verification_code(self, **kwargs):
        self.calls.append(kwargs)
        raise TimeoutError("mailbox timeout")


class _FakeOtpSendResponse:
    status_code = 200
    url = "https://auth.openai.com/email-verification"
    text = '{"page":{"type":"email_otp_verification"}}'

    def json(self):
        return {"page": {"type": "email_otp_verification"}}


class _FakeOtpSession:
    def __init__(self):
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _FakeOtpSendResponse()


class RefreshTokenRegistrationEngineTests(unittest.TestCase):
    def setUp(self):
        self._homepage_probe_patch = mock.patch.object(
            RefreshTokenRegistrationEngine,
            "_probe_homepage_before_email_creation",
            return_value=(True, ""),
        )
        self._homepage_report_patch = mock.patch.object(
            RefreshTokenRegistrationEngine,
            "_report_homepage_probe",
        )
        self._homepage_probe_patch.start()
        self._homepage_report_patch.start()

    def tearDown(self):
        self._homepage_report_patch.stop()
        self._homepage_probe_patch.stop()

    def _make_engine(self, **kwargs):
        return RefreshTokenRegistrationEngine(
            email_service=DummyEmailService(),
            proxy_url="http://127.0.0.1:7890",
            callback_logger=lambda _msg: None,
            max_retries=1,
            **kwargs,
        )

    def test_confirmed_phone_binding_event_is_attached_to_registration_result(self):
        engine = self._make_engine()
        oauth_client = mock.Mock()
        oauth_client._phone_challenge_events = []
        oauth_client._phone_binding_events = [
            {
                "phone": "+16134655704",
                "phone_number": "+16134655704",
                "status": "bound",
                "source": "oauth_add_phone",
                "api_url": "https://phone-api.example.test/read?id=1",
                "source_api_url": "https://supplier.example.test/read?id=1",
                "bound_at": "2026-07-16T00:00:00+00:00",
                "updated_at": "2026-07-16T00:00:00+00:00",
            }
        ]
        result = RegistrationResult(success=True, metadata={})

        engine._remember_oauth_phone_challenge_events(oauth_client)
        engine._apply_phone_challenge_metadata(result)

        self.assertEqual(result.metadata["chatgpt_phone_binding"]["status"], "bound")
        self.assertEqual(result.metadata["chatgpt_phone_binding_history"][-1]["phone"], "+16134655704")
        self.assertEqual(result.metadata["chatgpt_bound_phone"]["verification_status"], "verified")
        self.assertEqual(result.metadata["chatgpt_bound_phone_number"], "+16134655704")

    @staticmethod
    def _registered_client():
        client = mock.Mock(
            device_id="device-fixed",
            ua="UA",
            sec_ch_ua='"Chromium";v="136"',
            impersonate="chrome136",
            fingerprint={"device_id": "device-fixed"},
        )
        client.register_complete_flow.return_value = (True, "registration complete")
        client.reuse_session_and_get_tokens.return_value = (
            True,
            {
                "access_token": "at-registration",
                "session_token": "session-registration",
                "account_id": "acct-registration",
                "workspace_id": "ws-personal",
                "cookies": "oai-did=device",
            },
        )
        return client

    def test_email_adapter_returns_none_on_mailbox_timeout_for_resend_path(self):
        service = TimeoutEmailService()
        logs = []
        adapter = EmailServiceAdapter(
            service,
            "user@example.com",
            lambda message, *_: logs.append(str(message)),
        )

        code = adapter.wait_for_verification_code(
            "user@example.com",
            timeout=60,
            phase="register_email_otp",
            phase_label="registration email OTP",
        )

        self.assertIsNone(code)
        self.assertEqual(len(service.calls), 1)
        self.assertEqual(service.calls[0]["phase"], "register_email_otp")
        self.assertTrue(any("timeout" in line.lower() for line in logs))

    def test_send_email_otp_uses_device_and_trace_headers(self):
        client = ChatGPTClient(verbose=False)
        client.session = _FakeOtpSession()

        with mock.patch(
            "services.chatgpt_core.chatgpt_client.generate_datadog_trace",
            return_value={"x-datadog-trace-id": "trace-1"},
        ):
            ok = client.send_email_otp(
                referer="https://auth.openai.com/email-verification"
            )

        self.assertTrue(ok)
        _, kwargs = client.session.calls[0]
        headers = kwargs["headers"]
        self.assertEqual(headers.get("oai-device-id"), client.device_id)
        self.assertEqual(headers.get("x-datadog-trace-id"), "trace-1")
        self.assertEqual(
            headers.get("Referer"),
            "https://auth.openai.com/email-verification",
        )

    @mock.patch("services.chatgpt_core.refresh_token_registration_engine.OAuthManager")
    @mock.patch("services.chatgpt_core.refresh_token_registration_engine.OAuthClient")
    @mock.patch("services.chatgpt_core.refresh_token_registration_engine.ChatGPTClient")
    def test_run_completes_single_account_refresh_token_capture(
        self,
        mock_chatgpt_client_cls,
        _mock_oauth_client_cls,
        _mock_oauth_manager_cls,
    ):
        register_client = self._registered_client()
        mock_chatgpt_client_cls.return_value = register_client
        engine = self._make_engine()
        full_auth = {
            "account_id": "acct-personal",
            "workspace_id": "ws-personal",
            "access_token": "at-full",
            "refresh_token": "rt-full",
            "id_token": "id-full",
            "session_token": "session-full",
            "source": "auth_capture",
        }

        with mock.patch.object(
            engine,
            "_capture_auth_via_fresh_login",
            return_value=full_auth,
        ) as capture_auth:
            result = engine.run()

        self.assertTrue(result.success)
        self.assertEqual(result.account_id, "acct-personal")
        self.assertEqual(result.workspace_id, "ws-personal")
        self.assertEqual(result.access_token, "at-full")
        self.assertEqual(result.refresh_token, "rt-full")
        self.assertEqual(result.source, "auth_capture")
        self.assertTrue(result.metadata["registration_access_token_checkpoint_created"])
        capture_auth.assert_called_once()

    @mock.patch("services.chatgpt_core.refresh_token_registration_engine.OAuthManager")
    @mock.patch("services.chatgpt_core.refresh_token_registration_engine.OAuthClient")
    @mock.patch("services.chatgpt_core.refresh_token_registration_engine.ChatGPTClient")
    def test_run_keeps_access_token_checkpoint_when_full_auth_fails_even_if_legacy_flag_is_false(
        self,
        mock_chatgpt_client_cls,
        _mock_oauth_client_cls,
        _mock_oauth_manager_cls,
    ):
        mock_chatgpt_client_cls.return_value = self._registered_client()
        engine = self._make_engine(
            extra_config={"chatgpt_save_registration_access_token_account": False}
        )

        def fail_after_checkpoint(*, result, **_kwargs):
            self.assertTrue(
                result.metadata["registration_access_token_checkpoint_created"]
            )
            self.assertEqual(
                result.metadata["registration_access_token_checkpoint_policy"],
                "always_keep_before_full_auth",
            )
            result.error_message = "full auth failed"
            return False

        with mock.patch.object(
            engine,
            "_finalize_auth_capture",
            side_effect=fail_after_checkpoint,
        ):
            result = engine.run()

        self.assertTrue(result.success)
        self.assertEqual(result.source, "registration_session")
        self.assertEqual(result.account_id, "acct-registration")
        self.assertEqual(result.workspace_id, "ws-personal")
        self.assertEqual(result.access_token, "at-registration")
        self.assertEqual(result.refresh_token, "")
        self.assertEqual(result.session_token, "session-registration")
        self.assertTrue(result.metadata["registration_access_token_saved"])
        self.assertFalse(result.metadata["registration_access_token_save_requested"])

    @mock.patch("services.chatgpt_core.refresh_token_registration_engine.OAuthManager")
    @mock.patch("services.chatgpt_core.refresh_token_registration_engine.OAuthClient")
    @mock.patch("services.chatgpt_core.refresh_token_registration_engine.ChatGPTClient")
    def test_run_keeps_access_token_checkpoint_when_full_auth_is_skipped(
        self,
        mock_chatgpt_client_cls,
        _mock_oauth_client_cls,
        _mock_oauth_manager_cls,
    ):
        mock_chatgpt_client_cls.return_value = self._registered_client()
        engine = self._make_engine()

        with mock.patch.object(
            engine,
            "_finalize_auth_capture",
            side_effect=SkipCurrentAttemptRequested("phone verification skipped"),
        ):
            result = engine.run()

        self.assertTrue(result.success)
        self.assertEqual(result.source, "registration_session")
        self.assertEqual(result.access_token, "at-registration")
        self.assertEqual(result.refresh_token, "")
        self.assertTrue(result.metadata["registration_access_token_saved"])

    def test_full_auth_capture_uses_single_account_phone_policy(self):
        engine = self._make_engine(
            extra_config={
                "chatgpt_resume_auth_allow_add_phone_verification": False,
                "chatgpt_resume_auth_allow_existing_phone_verification": True,
            }
        )
        oauth_client = mock.Mock()
        oauth_client._phone_challenge_events = []
        oauth_client.login_and_get_tokens.return_value = {
            "access_token": "at-personal",
            "refresh_token": "rt-personal",
            "id_token": "",
            "account_id": "acct-personal",
        }
        oauth_client.last_workspace_id = "ws-personal"
        oauth_client._get_cookie_value.return_value = "session-personal"

        with mock.patch.object(engine, "_build_oauth_client", return_value=oauth_client):
            payload = engine._capture_auth_via_fresh_login(
                email="user@example.com",
                password="Secret123!",
                device_id="device-fixed",
                user_agent="UA",
                sec_ch_ua='"Chromium";v="136"',
                impersonate="chrome136",
                browser_fingerprint={"device_id": "device-fixed"},
                email_adapter=mock.Mock(),
                first_name="Ada",
                last_name="Lovelace",
                birthdate="1990-01-02",
            )

        self.assertEqual(payload["account_id"], "acct-personal")
        self.assertEqual(payload["workspace_id"], "ws-personal")
        self.assertEqual(payload["refresh_token"], "rt-personal")
        login_kwargs = oauth_client.login_and_get_tokens.call_args.kwargs
        self.assertTrue(login_kwargs["allow_phone_verification"])
        self.assertFalse(login_kwargs["allow_add_phone_verification"])
        self.assertTrue(login_kwargs["allow_existing_phone_verification"])
        self.assertFalse(login_kwargs["allow_add_phone_session_recovery"])
        self.assertEqual(login_kwargs["login_source"], "auth_capture")

    @mock.patch("services.chatgpt_core.refresh_token_registration_engine.OAuthManager")
    @mock.patch("services.chatgpt_core.refresh_token_registration_engine.OAuthClient")
    @mock.patch("services.chatgpt_core.refresh_token_registration_engine.ChatGPTClient")
    def test_run_keeps_first_created_email_when_global_retry_is_disabled(
        self,
        mock_chatgpt_client_cls,
        _mock_oauth_client_cls,
        _mock_oauth_manager_cls,
    ):
        class RotatingEmailService:
            service_type = type("ST", (), {"value": "dummy"})()

            def __init__(self):
                self.index = 0

            def create_email(self):
                self.index += 1
                return {
                    "email": f"user{self.index}@example.com",
                    "service_id": f"svc-{self.index}",
                }

            def get_verification_code(self, **_kwargs):
                return "123456"

        register_client = self._registered_client()
        register_client.register_complete_flow.return_value = (
            False,
            "network timeout",
        )
        mock_chatgpt_client_cls.return_value = register_client
        email_service = RotatingEmailService()
        engine = RefreshTokenRegistrationEngine(
            email_service=email_service,
            proxy_url="http://127.0.0.1:7890",
            callback_logger=lambda _msg: None,
            max_retries=2,
        )

        result = engine.run()

        self.assertFalse(result.success)
        self.assertIn("注册状态机失败", result.error_message)
        self.assertEqual(email_service.index, 1)
        register_client.register_complete_flow.assert_called_once()
        self.assertEqual(
            register_client.register_complete_flow.call_args.args[0],
            "user1@example.com",
        )

    @mock.patch("services.chatgpt_core.refresh_token_registration_engine.OAuthManager")
    @mock.patch("services.chatgpt_core.refresh_token_registration_engine.OAuthClient")
    @mock.patch("services.chatgpt_core.refresh_token_registration_engine.ChatGPTClient")
    def test_existing_account_route_preserves_single_account_oauth_identity(
        self,
        mock_chatgpt_client_cls,
        mock_oauth_client_cls,
        mock_oauth_manager_cls,
    ):
        register_client = mock.Mock(
            device_id="device-fixed",
            ua="UA",
            sec_ch_ua='"Chromium";v="136"',
            impersonate="chrome136",
        )
        register_client.register_complete_flow.return_value = (
            False,
            "HTTP 400: user_already_exists",
        )
        mock_chatgpt_client_cls.return_value = register_client

        oauth_client = mock.Mock()
        oauth_client.login_and_get_tokens.return_value = {
            "access_token": "at-existing",
            "refresh_token": "rt-existing",
            "id_token": "id-existing",
        }
        oauth_client.last_workspace_id = "acct-existing"
        oauth_client._decode_oauth_session_cookie.return_value = {
            "workspaces": [{"id": "acct-existing"}]
        }
        oauth_client._get_cookie_value.return_value = ""
        mock_oauth_client_cls.return_value = oauth_client

        oauth_manager = mock.Mock()
        oauth_manager.extract_account_info.return_value = {
            "email": "user@example.com",
            "account_id": "acct-existing",
        }
        mock_oauth_manager_cls.return_value = oauth_manager

        result = self._make_engine().run()

        self.assertTrue(result.success)
        self.assertEqual(result.source, "login")
        self.assertEqual(result.account_id, "acct-existing")
        self.assertEqual(result.workspace_id, "acct-existing")
        self.assertEqual(result.refresh_token, "rt-existing")
        self.assertTrue(result.metadata["existing_account_login_routed"])
        self.assertEqual(
            oauth_client.login_and_get_tokens.call_args.kwargs["login_source"],
            "existing_account_recovery",
        )

    @mock.patch("services.chatgpt_core.refresh_token_registration_engine.OAuthManager")
    @mock.patch("services.chatgpt_core.refresh_token_registration_engine.OAuthClient")
    @mock.patch("services.chatgpt_core.refresh_token_registration_engine.ChatGPTClient")
    def test_existing_account_route_can_be_disabled(
        self,
        mock_chatgpt_client_cls,
        mock_oauth_client_cls,
        mock_oauth_manager_cls,
    ):
        register_client = mock.Mock(
            device_id="device-fixed",
            ua="UA",
            sec_ch_ua='"Chromium";v="136"',
            impersonate="chrome136",
        )
        register_client.register_complete_flow.return_value = (
            False,
            "HTTP 400: user_already_exists",
        )
        register_client.last_registration_route_event = {
            "email": "user@example.com",
            "reason": "user_already_exists",
            "stage": "register_complete_flow",
        }
        mock_chatgpt_client_cls.return_value = register_client
        engine = self._make_engine(
            extra_config={"chatgpt_existing_account_login_route_enabled": False}
        )

        with self.assertRaises(ExistingAccountLoginRouteBlocked) as caught:
            engine.run()

        self.assertEqual(caught.exception.email, "user@example.com")
        self.assertTrue(caught.exception.route_event["blocked"])
        mock_oauth_client_cls.assert_not_called()
        mock_oauth_manager_cls.assert_not_called()


class ChatGPTClientRegistrationOtpTests(unittest.TestCase):
    def _make_client_at_email_verification(self):
        client = ChatGPTClient(verbose=False)
        client.visit_homepage = mock.Mock(return_value=True)
        client.get_csrf_token = mock.Mock(return_value="csrf-demo")
        client.signin = mock.Mock(
            return_value="https://auth.openai.com/api/accounts/authorize?demo=1"
        )
        client.authorize = mock.Mock(
            return_value="https://auth.openai.com/email-verification"
        )
        client.send_email_otp = mock.Mock(return_value=True)
        client.verify_email_otp = mock.Mock(
            return_value=(
                True,
                FlowState(
                    page_type="callback",
                    current_url="https://chatgpt.com/",
                    continue_url="https://chatgpt.com/",
                ),
            )
        )
        return client

    def test_direct_email_verification_detects_existing_route_before_resend(self):
        client = self._make_client_at_email_verification()
        mailbox = mock.Mock()
        mailbox.wait_for_verification_code.return_value = "123456"

        success, message = client.register_complete_flow(
            "user@example.com",
            "Secret123!",
            "Alice",
            "Smith",
            "1990-01-01",
            mailbox,
            otp_wait_timeout=30,
            otp_resend_wait_timeout=30,
        )

        self.assertFalse(success)
        self.assertIn("user_already_exists", message)
        self.assertEqual(
            client.last_registration_route_event["reason"],
            "registration_completed_without_create_account_after_otp",
        )
        client.send_email_otp.assert_not_called()
        client.verify_email_otp.assert_called_once_with("123456", return_state=True)
        self.assertEqual(mailbox.wait_for_verification_code.call_count, 1)

    def test_direct_email_verification_resends_only_after_first_wait_times_out(self):
        client = self._make_client_at_email_verification()
        mailbox = mock.Mock()
        mailbox.wait_for_verification_code.side_effect = [None, "654321"]

        success, message = client.register_complete_flow(
            "user@example.com",
            "Secret123!",
            "Alice",
            "Smith",
            "1990-01-01",
            mailbox,
            otp_wait_timeout=30,
            otp_resend_wait_timeout=30,
        )

        self.assertFalse(success)
        self.assertIn("user_already_exists", message)
        client.send_email_otp.assert_called_once_with(
            referer="https://auth.openai.com/email-verification"
        )
        client.verify_email_otp.assert_called_once_with("654321", return_state=True)
        self.assertEqual(mailbox.wait_for_verification_code.call_count, 2)

    def test_direct_email_verification_honors_single_account_timeout_budget(self):
        client = self._make_client_at_email_verification()
        mailbox = mock.Mock()
        mailbox.wait_for_verification_code.return_value = None
        mailbox.is_otp_wait_budget_exhausted.return_value = True

        success, message = client.register_complete_flow(
            "user@example.com",
            "Secret123!",
            "Alice",
            "Smith",
            "1990-01-01",
            mailbox,
            otp_wait_timeout=30,
            otp_resend_wait_timeout=30,
            otp_account_budget_timeout=30,
        )

        self.assertFalse(success)
        self.assertIn("超时", message)
        client.send_email_otp.assert_not_called()
        client.verify_email_otp.assert_not_called()


class OAuthClientPasswordlessTests(unittest.TestCase):
    def _make_client(self):
        return OAuthClient({}, proxy="http://127.0.0.1:7890", verbose=False)

    def test_login_and_get_tokens_prefers_passwordless(self):
        client = self._make_client()
        login_password_state = FlowState(
            page_type="login_password",
            continue_url="https://auth.openai.com/log-in/password",
            current_url="https://auth.openai.com/log-in/password",
        )
        email_otp_state = FlowState(
            page_type="email_otp_verification",
            continue_url="https://auth.openai.com/email-verification",
            current_url="https://auth.openai.com/email-verification",
        )
        consent_state = FlowState(
            page_type="consent",
            continue_url="https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
            current_url="https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
        )

        with mock.patch.object(
            client,
            "_bootstrap_oauth_session",
            return_value="https://auth.openai.com/log-in",
        ), mock.patch.object(
            client,
            "_submit_authorize_continue",
            return_value=login_password_state,
        ), mock.patch.object(
            client,
            "_send_passwordless_login_otp",
            return_value=email_otp_state,
        ) as send_passwordless, mock.patch.object(
            client,
            "_handle_otp_verification",
            return_value=consent_state,
        ), mock.patch.object(
            client,
            "_oauth_submit_workspace_and_org",
            return_value=("auth-code", None),
        ), mock.patch.object(
            client,
            "_exchange_code_for_tokens",
            return_value={"access_token": "at"},
        ), mock.patch.object(client, "_submit_password_verify") as submit_password:
            tokens = client.login_and_get_tokens(
                "user@example.com",
                "Secret123!",
                "device-fixed",
                user_agent="UA",
                sec_ch_ua='"Chromium";v="136"',
                impersonate="chrome136",
                skymail_client=mock.Mock(),
                prefer_passwordless_login=True,
                allow_phone_verification=False,
            )

        self.assertEqual(tokens["access_token"], "at")
        send_passwordless.assert_called_once()
        submit_password.assert_not_called()

    def test_login_follows_add_phone_continue_url_before_personal_oauth_resolution(self):
        client = self._make_client()
        add_phone_state = FlowState(
            page_type="add_phone",
            continue_url="https://auth.openai.com/add-phone",
            current_url="https://auth.openai.com/api/accounts/email-otp/validate",
            source="api",
        )
        consent_state = FlowState(
            page_type="consent",
            continue_url="https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
            current_url="https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
        )

        with mock.patch.object(
            client,
            "_bootstrap_oauth_session",
            return_value="https://auth.openai.com/log-in",
        ), mock.patch.object(
            client,
            "_submit_authorize_continue",
            return_value=add_phone_state,
        ), mock.patch.object(
            client,
            "_follow_flow_state",
            return_value=(None, consent_state),
        ) as follow_state, mock.patch.object(
            client,
            "_oauth_submit_workspace_and_org",
            return_value=("auth-code", None),
        ), mock.patch.object(
            client,
            "_exchange_code_for_tokens",
            return_value={"access_token": "at"},
        ), mock.patch.object(client, "_handle_add_phone_verification") as handle_phone:
            tokens = client.login_and_get_tokens(
                "user@example.com",
                "Secret123!",
                "device-fixed",
                prefer_passwordless_login=True,
                allow_phone_verification=False,
            )

        self.assertEqual(tokens["access_token"], "at")
        follow_state.assert_called_once()
        handle_phone.assert_not_called()

    def test_login_handles_add_phone_before_oauth_resolution_when_new_binding_is_enabled(self):
        client = self._make_client()
        add_phone_state = FlowState(
            page_type="add_phone",
            continue_url="https://auth.openai.com/add-phone",
            current_url="https://auth.openai.com/api/accounts/email-otp/validate",
            source="api",
        )
        consent_state = FlowState(
            page_type="consent",
            continue_url="https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
            current_url="https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
        )
        transitions: list[str] = []

        def handle_phone_transition(*_args, **_kwargs):
            transitions.append("handle_phone")
            return consent_state

        def follow_state_transition(*_args, **_kwargs):
            transitions.append("follow_state")
            return None, consent_state

        with mock.patch.object(
            client,
            "_bootstrap_oauth_session",
            return_value="https://auth.openai.com/log-in",
        ), mock.patch.object(
            client,
            "_submit_authorize_continue",
            return_value=add_phone_state,
        ), mock.patch.object(
            client,
            "_handle_add_phone_verification",
            side_effect=handle_phone_transition,
        ) as handle_phone, mock.patch.object(
            client,
            "_follow_flow_state",
            side_effect=follow_state_transition,
        ) as follow_state, mock.patch.object(
            client,
            "_oauth_submit_workspace_and_org",
            return_value=("auth-code", None),
        ), mock.patch.object(
            client,
            "_exchange_code_for_tokens",
            return_value={"access_token": "at"},
        ):
            tokens = client.login_and_get_tokens(
                "user@example.com",
                "Secret123!",
                "device-fixed",
                prefer_passwordless_login=True,
                allow_phone_verification=True,
                allow_add_phone_verification=True,
            )

        self.assertEqual(tokens["access_token"], "at")
        handle_phone.assert_called_once()
        self.assertEqual(transitions[0], "handle_phone")
        if "follow_state" in transitions:
            self.assertGreater(transitions.index("follow_state"), transitions.index("handle_phone"))

    def test_login_uses_canonical_consent_url_for_personal_oauth_resolution(self):
        client = self._make_client()
        add_phone_state = FlowState(
            page_type="add_phone",
            continue_url="https://auth.openai.com/add-phone",
            current_url="https://auth.openai.com/add-phone",
        )

        with mock.patch.object(
            client,
            "_bootstrap_oauth_session",
            return_value="https://auth.openai.com/log-in",
        ), mock.patch.object(
            client,
            "_submit_authorize_continue",
            return_value=add_phone_state,
        ), mock.patch.object(
            client,
            "_state_supports_workspace_resolution",
            return_value=True,
        ), mock.patch.object(
            client,
            "_state_requires_navigation",
            return_value=False,
        ), mock.patch.object(
            client,
            "_oauth_submit_workspace_and_org",
            return_value=("auth-code", None),
        ) as submit_workspace, mock.patch.object(
            client,
            "_exchange_code_for_tokens",
            return_value={"access_token": "at"},
        ):
            tokens = client.login_and_get_tokens(
                "user@example.com",
                "Secret123!",
                "device-fixed",
                prefer_passwordless_login=True,
                allow_phone_verification=False,
                skymail_client=mock.Mock(),
            )

        self.assertEqual(tokens["access_token"], "at")
        self.assertEqual(
            submit_workspace.call_args.args[0],
            "https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
        )

    def test_login_retries_once_when_personal_oauth_account_is_not_resolved(self):
        client = self._make_client()
        add_phone_state = FlowState(
            page_type="add_phone",
            continue_url="https://auth.openai.com/add-phone",
            current_url="https://auth.openai.com/add-phone",
        )

        with mock.patch.object(
            client,
            "_bootstrap_oauth_session",
            return_value="https://auth.openai.com/log-in",
        ) as bootstrap, mock.patch.object(
            client,
            "_submit_authorize_continue",
            return_value=add_phone_state,
        ) as submit_continue, mock.patch.object(
            client,
            "_state_supports_workspace_resolution",
            return_value=False,
        ), mock.patch.object(
            client,
            "_state_requires_navigation",
            return_value=False,
        ):
            tokens = client.login_and_get_tokens(
                "user@example.com",
                "Secret123!",
                "device-fixed",
                prefer_passwordless_login=True,
                allow_phone_verification=False,
                skymail_client=mock.Mock(),
            )

        self.assertIsNone(tokens)
        self.assertEqual(bootstrap.call_count, 2)
        self.assertEqual(submit_continue.call_count, 2)
        self.assertIn("workspace / callback", client.last_error)

    def test_send_passwordless_login_otp_does_not_send_email_field(self):
        client = self._make_client()
        response = mock.Mock(
            status_code=200,
            url="https://auth.openai.com/api/accounts/passwordless/send-otp",
        )
        response.json.return_value = {
            "page": {"type": "email_otp_verification"}
        }
        client.session.post = mock.Mock(return_value=response)

        client._send_passwordless_login_otp(
            "user@example.com",
            "device-fixed",
        )

        kwargs = client.session.post.call_args.kwargs
        self.assertNotIn("json", kwargs)
        self.assertNotIn("data", kwargs)

    def test_password_verify_carries_request_start_into_email_otp_state(self):
        client = self._make_client()
        response = mock.Mock(
            status_code=200,
            url="https://auth.openai.com/api/accounts/password/verify",
            text="",
        )
        response.json.return_value = {
            "page": {"type": "email_otp_verification"},
            "continue_url": "https://auth.openai.com/email-verification",
        }
        client.session.post = mock.Mock(return_value=response)

        with mock.patch(
            "services.chatgpt_core.oauth_client.build_sentinel_token",
            return_value="sentinel",
        ), mock.patch(
            "services.chatgpt_core.oauth_client.time.time",
            return_value=200.0,
        ):
            state = client._submit_password_verify(
                "user@example.com",
                "Secret123!",
                "device-fixed",
            )

        self.assertIsNotNone(state)
        self.assertEqual(state.otp_sent_at, 195.0)

    def test_authorize_continue_carries_request_start_into_existing_account_otp(self):
        client = self._make_client()
        client._has_cookie = mock.Mock(return_value=True)
        response = mock.Mock(
            status_code=200,
            url="https://auth.openai.com/api/accounts/authorize/continue",
            text="",
        )
        response.json.return_value = {
            "page": {"type": "email_otp_verification"},
            "continue_url": "https://auth.openai.com/email-verification",
        }
        client.session.post = mock.Mock(return_value=response)

        with mock.patch(
            "services.chatgpt_core.oauth_client.build_sentinel_token",
            return_value="sentinel",
        ), mock.patch(
            "services.chatgpt_core.oauth_client.time.time",
            return_value=400.0,
        ):
            state = client._submit_authorize_continue(
                "user@example.com",
                "device-fixed",
                "https://auth.openai.com/log-in",
            )

        self.assertIsNotNone(state)
        self.assertEqual(state.otp_sent_at, 395.0)

    def test_passwordless_carries_request_start_into_email_otp_state(self):
        client = self._make_client()
        response = mock.Mock(
            status_code=200,
            url="https://auth.openai.com/api/accounts/passwordless/send-otp",
            text="",
        )
        response.json.return_value = {
            "page": {"type": "email_otp_verification"},
            "continue_url": "https://auth.openai.com/email-verification",
        }
        client.session.post = mock.Mock(return_value=response)

        with mock.patch(
            "services.chatgpt_core.oauth_client.time.time",
            return_value=300.0,
        ):
            state = client._send_passwordless_login_otp(
                "user@example.com",
                "device-fixed",
            )

        self.assertIsNotNone(state)
        self.assertEqual(state.otp_sent_at, 295.0)

    def test_email_otp_wait_uses_trigger_request_cutoff_after_processing_delay(self):
        client = self._make_client()
        state = FlowState(
            page_type="email_otp_verification",
            continue_url="https://auth.openai.com/email-verification",
            current_url="https://auth.openai.com/email-verification",
            otp_sent_at=123.0,
        )
        response = mock.Mock(
            status_code=200,
            url="https://auth.openai.com/api/accounts/email-otp/validate",
            text="",
        )
        response.json.return_value = {
            "page": {"type": "consent"},
            "continue_url": "https://auth.openai.com/consent",
        }
        client.session.post = mock.Mock(return_value=response)
        mailbox = mock.Mock()
        mailbox._used_codes = set()
        mailbox.wait_for_verification_code.return_value = "972138"
        mailbox.get_last_verification_result.return_value = {"message_id": "otp-1"}

        with mock.patch(
            "services.chatgpt_core.oauth_client.build_sentinel_token",
            return_value="sentinel",
        ):
            next_state = client._handle_otp_verification(
                "user@example.com",
                "device-fixed",
                "UA",
                '"Chromium";v="136"',
                "chrome136",
                mailbox,
                state,
            )

        self.assertIsNotNone(next_state)
        self.assertEqual(
            mailbox.wait_for_verification_code.call_args.kwargs["otp_sent_at"],
            123.0,
        )

    def test_successful_email_otp_resend_replaces_cutoff_with_resend_start(self):
        clock = [1000.0]
        client = OAuthClient(
            {
                "chatgpt_oauth_otp_wait_seconds": 120,
                "chatgpt_oauth_otp_resend_wait_seconds": 30,
            },
            proxy="http://127.0.0.1:7890",
            verbose=False,
        )
        state = FlowState(
            page_type="email_otp_verification",
            continue_url="https://auth.openai.com/email-verification",
            current_url="https://auth.openai.com/email-verification",
            otp_sent_at=900.0,
        )
        resend_response = mock.Mock(status_code=200, text="", url="")
        client.session.get = mock.Mock(return_value=resend_response)
        validate_response = mock.Mock(
            status_code=200,
            url="https://auth.openai.com/api/accounts/email-otp/validate",
            text="",
        )
        validate_response.json.return_value = {
            "page": {"type": "consent"},
            "continue_url": "https://auth.openai.com/consent",
        }
        client.session.post = mock.Mock(return_value=validate_response)
        mailbox = mock.Mock()
        mailbox._used_codes = set()
        wait_cutoffs = []

        def wait_for_code(*_args, **kwargs):
            wait_cutoffs.append(kwargs["otp_sent_at"])
            if len(wait_cutoffs) == 1:
                clock[0] = 1031.0
                return None
            return "972138"

        mailbox.wait_for_verification_code.side_effect = wait_for_code
        mailbox.get_last_verification_result.return_value = {"message_id": "otp-new"}

        with mock.patch(
            "services.chatgpt_core.oauth_client.build_sentinel_token",
            return_value="sentinel",
        ), mock.patch(
            "services.chatgpt_core.oauth_client.time.time",
            side_effect=lambda: clock[0],
        ):
            next_state = client._handle_otp_verification(
                "user@example.com",
                "device-fixed",
                "UA",
                '"Chromium";v="136"',
                "chrome136",
                mailbox,
                state,
            )

        self.assertIsNotNone(next_state)
        self.assertEqual(wait_cutoffs, [900.0, 1026.0])
        client.session.get.assert_called_once()

    def test_failed_email_otp_resend_keeps_previous_cutoff(self):
        clock = [1000.0]
        client = OAuthClient(
            {
                "chatgpt_oauth_otp_wait_seconds": 120,
                "chatgpt_oauth_otp_resend_wait_seconds": 30,
            },
            proxy="http://127.0.0.1:7890",
            verbose=False,
        )
        state = FlowState(
            page_type="email_otp_verification",
            continue_url="https://auth.openai.com/email-verification",
            current_url="https://auth.openai.com/email-verification",
            otp_sent_at=900.0,
        )
        resend_response = mock.Mock(status_code=500, text="failed", url="")
        client.session.get = mock.Mock(return_value=resend_response)
        validate_response = mock.Mock(
            status_code=200,
            url="https://auth.openai.com/api/accounts/email-otp/validate",
            text="",
        )
        validate_response.json.return_value = {
            "page": {"type": "consent"},
            "continue_url": "https://auth.openai.com/consent",
        }
        client.session.post = mock.Mock(return_value=validate_response)
        mailbox = mock.Mock()
        mailbox._used_codes = set()
        wait_cutoffs = []

        def wait_for_code(*_args, **kwargs):
            wait_cutoffs.append(kwargs["otp_sent_at"])
            if len(wait_cutoffs) == 1:
                clock[0] = 1031.0
                return None
            return "972138"

        mailbox.wait_for_verification_code.side_effect = wait_for_code
        mailbox.get_last_verification_result.return_value = {"message_id": "otp-new"}

        with mock.patch(
            "services.chatgpt_core.oauth_client.build_sentinel_token",
            return_value="sentinel",
        ), mock.patch(
            "services.chatgpt_core.oauth_client.time.time",
            side_effect=lambda: clock[0],
        ):
            next_state = client._handle_otp_verification(
                "user@example.com",
                "device-fixed",
                "UA",
                '"Chromium";v="136"',
                "chrome136",
                mailbox,
                state,
            )

        self.assertIsNotNone(next_state)
        self.assertEqual(wait_cutoffs, [900.0, 900.0])
        client.session.get.assert_called_once()

    def test_email_otp_account_deactivated_stops_waiting_for_more_codes(self):
        client = self._make_client()
        state = FlowState(
            page_type="email_otp_verification",
            continue_url="https://auth.openai.com/email-verification",
            current_url="https://auth.openai.com/email-verification",
        )
        response = mock.Mock(
            status_code=403,
            text=(
                '{"error":{"code":"account_delete","message":"You do not have '
                'an account because it has been deleted or deactivated."}}'
            ),
        )
        response.json.return_value = {
            "error": {
                "code": "account_delete",
                "message": (
                    "You do not have an account because it has been deleted or "
                    "deactivated."
                ),
            }
        }
        client.session.post = mock.Mock(return_value=response)
        mailbox = mock.Mock()
        mailbox._used_codes = set()
        mailbox.wait_for_verification_code.side_effect = ["972138", "111111"]
        mailbox.get_last_verification_result.return_value = {"message_id": "otp-1"}

        with mock.patch(
            "services.chatgpt_core.oauth_client.build_sentinel_token",
            return_value="sentinel",
        ):
            next_state = client._handle_otp_verification(
                "user@example.com",
                "device-fixed",
                "UA",
                '"Chromium";v="136"',
                "chrome136",
                mailbox,
                state,
            )

        self.assertIsNone(next_state)
        self.assertEqual(mailbox.wait_for_verification_code.call_count, 1)
        self.assertIn("account_deactivated", client.last_error)

    def test_login_and_get_tokens_submits_about_you_when_required(self):
        client = self._make_client()
        about_you_state = FlowState(
            page_type="about_you",
            continue_url="https://auth.openai.com/about-you",
            current_url="https://auth.openai.com/about-you",
        )
        consent_state = FlowState(
            page_type="consent",
            continue_url="https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
            current_url="https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
        )

        with mock.patch.object(
            client,
            "_bootstrap_oauth_session",
            return_value="https://auth.openai.com/log-in",
        ), mock.patch.object(
            client,
            "_submit_authorize_continue",
            return_value=about_you_state,
        ), mock.patch.object(
            client,
            "_submit_about_you_create_account",
            return_value=consent_state,
        ) as submit_about_you, mock.patch.object(
            client,
            "_oauth_submit_workspace_and_org",
            return_value=("auth-code", None),
        ), mock.patch.object(
            client,
            "_exchange_code_for_tokens",
            return_value={"access_token": "at"},
        ):
            tokens = client.login_and_get_tokens(
                "user@example.com",
                "Secret123!",
                "device-fixed",
                prefer_passwordless_login=True,
                allow_phone_verification=False,
                complete_about_you_if_needed=True,
                first_name="Ivy",
                last_name="Stone",
                birthdate="1990-01-02",
                skymail_client=mock.Mock(),
            )

        self.assertEqual(tokens["access_token"], "at")
        submit_about_you.assert_called_once()

    def test_personal_oauth_selection_falls_back_when_no_org_is_available(self):
        client = OAuthClient(
            {"chatgpt_workspace_select_no_org_retry_delays_seconds": "0"},
            proxy="http://127.0.0.1:7890",
            verbose=False,
        )
        response = mock.Mock(status_code=400)
        response.json.return_value = {
            "error": {
                "code": "no_valid_organizations",
                "type": "invalid_request_error",
                "message": "You do not have any valid organizations.",
            }
        }
        client.session.post = mock.Mock(return_value=response)

        with mock.patch.object(
            client,
            "_load_workspace_session_data",
            return_value={"workspaces": [{"id": "ws-personal", "kind": "personal"}]},
        ), mock.patch.object(
            client,
            "_oauth_try_workspace_only_authorization",
            return_value=("auth-code", None),
        ) as fallback:
            code, next_state = client._oauth_submit_workspace_and_org(
                "https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
                "device-fixed",
                "UA",
                "chrome136",
                workspace_scope_preference="free",
                authorize_url="https://auth.openai.com/oauth/authorize",
                authorize_params={"client_id": "app-demo", "state": "state-demo"},
            )

        self.assertEqual(code, "auth-code")
        self.assertIsNone(next_state)
        self.assertEqual(client.last_workspace_id, "ws-personal")
        fallback.assert_called_once()
        self.assertEqual(fallback.call_args.kwargs["workspace_id"], "ws-personal")


class OAuthClientBootstrapTests(unittest.TestCase):
    def _make_client(self, browser_mode="protocol"):
        return OAuthClient(
            {},
            proxy="http://127.0.0.1:7890",
            verbose=False,
            browser_mode=browser_mode,
        )

    def test_bootstrap_invokes_browser_fallback_when_http_is_blocked(self):
        client = self._make_client("headed")
        blocked_response = mock.Mock(
            status_code=403,
            text="<!DOCTYPE html><title>Just a moment...</title>",
            url="https://auth.openai.com/",
            history=[],
        )
        client.session.get = mock.Mock(
            side_effect=[blocked_response, blocked_response]
        )

        with mock.patch.object(
            client,
            "_browser_bootstrap_oauth_session",
            return_value=("https://auth.openai.com/log-in", True),
        ) as browser_bootstrap:
            final_url = client._bootstrap_oauth_session(
                "https://auth.openai.com/oauth/authorize",
                {"client_id": "app-demo", "state": "state-demo"},
                device_id="device-demo",
                user_agent="UA",
                sec_ch_ua='"Chromium";v="136"',
            )

        self.assertEqual(final_url, "https://auth.openai.com/log-in")
        browser_bootstrap.assert_called_once()

    def test_protocol_bootstrap_never_invokes_browser_when_http_is_blocked(self):
        client = self._make_client("protocol")
        blocked_response = mock.Mock(
            status_code=403,
            text="<!DOCTYPE html><title>Just a moment...</title>",
            url="https://auth.openai.com/",
            history=[],
        )
        client.session.get = mock.Mock(
            side_effect=[blocked_response, blocked_response]
        )

        with mock.patch.object(
            client,
            "_browser_bootstrap_oauth_session",
            side_effect=AssertionError("protocol executor started browser"),
        ) as browser_bootstrap:
            final_url = client._bootstrap_oauth_session(
                "https://auth.openai.com/oauth/authorize",
                {"client_id": "app-demo", "state": "state-demo"},
                device_id="device-demo",
                user_agent="UA",
                sec_ch_ua='"Chromium";v="136"',
            )

        self.assertEqual(final_url, "https://auth.openai.com/")
        browser_bootstrap.assert_not_called()

    def test_authorize_continue_stops_without_login_session(self):
        client = self._make_client("headed")

        with mock.patch(
            "services.chatgpt_core.oauth_client.build_sentinel_token"
        ) as build_token:
            state = client._submit_authorize_continue(
                "user@example.com",
                "device-demo",
                "https://auth.openai.com/log-in",
                user_agent="UA",
                sec_ch_ua='"Chromium";v="136"',
                impersonate="chrome136",
            )

        self.assertIsNone(state)
        self.assertIn("login_session", client.last_error)
        build_token.assert_not_called()

    def test_merge_playwright_cookies_backfills_login_session(self):
        client = self._make_client("headed")

        merged = client._merge_playwright_cookies_into_session(
            [
                {
                    "name": "login_session",
                    "value": "demo-session",
                    "domain": ".auth.openai.com",
                    "path": "/",
                    "secure": True,
                }
            ]
        )

        self.assertGreaterEqual(merged, 1)
        self.assertTrue(client._has_cookie("login_session"))

    def test_about_you_requires_browser_owned_finalize_without_http_fallback(self):
        client = self._make_client("headed")
        client.session.post = mock.Mock()

        with mock.patch(
            "services.chatgpt_core.oauth_client.create_account_via_browser",
            return_value=None,
        ) as browser_create, mock.patch(
            "services.chatgpt_core.oauth_client.build_sentinel_token"
        ) as http_token:
            state = client._submit_about_you_create_account(
                "Alice",
                "Smith",
                "1990-01-01",
                "device-demo",
                user_agent="UA",
                sec_ch_ua='"Chromium";v="145"',
                impersonate="chrome145",
            )

        self.assertIsNone(state)
        self.assertIn("auth_browser_finalize_unavailable", client.last_error)
        self.assertEqual(
            browser_create.call_args.kwargs["page_url"],
            "https://auth.openai.com/about-you",
        )
        self.assertFalse(browser_create.call_args.kwargs["headless"])
        http_token.assert_not_called()
        client.session.post.assert_not_called()

    def test_about_you_browser_finalize_merges_cookies_and_returns_state(self):
        client = self._make_client("headed")
        client.session.post = mock.Mock()
        result = BrowserAccountCreateResult(
            status_code=200,
            response_url="https://auth.openai.com/api/accounts/create_account",
            response_json={
                "page": {"type": "external_url"},
                "continue_url": "https://chatgpt.com/api/auth/callback/openai?code=demo",
                "method": "GET",
            },
            cookies=[
                {
                    "name": "oai-sc",
                    "value": "sentinel-cookie",
                    "domain": ".openai.com",
                    "path": "/",
                    "secure": True,
                }
            ],
            cf_clearance_present=True,
            oai_sc_present=True,
        )

        with mock.patch(
            "services.chatgpt_core.oauth_client.create_account_via_browser",
            return_value=result,
        ) as browser_create:
            state = client._submit_about_you_create_account(
                "Alice",
                "Smith",
                "1990-01-01",
                "device-demo",
                user_agent="UA",
                sec_ch_ua='"Chromium";v="145"',
                impersonate="chrome145",
            )

        self.assertEqual(state.page_type, "external_url")
        self.assertEqual(browser_create.call_args.kwargs["device_id"], "device-demo")
        self.assertTrue(client._has_cookie("oai-sc"))
        client.session.post.assert_not_called()

    def test_about_you_protocol_posts_http_without_browser(self):
        client = self._make_client("protocol")
        response = mock.Mock(
            status_code=200,
            text="{}",
            url="https://auth.openai.com/api/accounts/create_account",
        )
        response.json.return_value = {
            "page": {"type": "external_url"},
            "continue_url": "https://chatgpt.com/api/auth/callback/openai?code=demo",
        }
        client.session.post = mock.Mock(return_value=response)

        with mock.patch(
            "services.chatgpt_core.oauth_client.build_sentinel_token",
            return_value="protocol-token",
        ), mock.patch(
            "services.chatgpt_core.oauth_client.create_account_via_browser",
            side_effect=AssertionError("protocol executor started browser"),
        ) as browser_create:
            state = client._submit_about_you_create_account(
                "Alice",
                "Smith",
                "1990-01-01",
                "device-demo",
                user_agent="UA",
                sec_ch_ua='"Chromium";v="145"',
                impersonate="chrome145",
            )

        self.assertEqual(state.page_type, "external_url")
        client.session.post.assert_called_once()
        browser_create.assert_not_called()

    def test_stop_after_login_waits_for_existing_phone_otp_handling(self):
        client = self._make_client()
        email_otp_state = FlowState(
            page_type="email_otp_verification",
            continue_url="https://auth.openai.com/email-verification",
            current_url="https://auth.openai.com/email-verification",
            source="api",
        )
        existing_phone_state = FlowState(
            page_type="phone_otp_select_channel",
            continue_url="https://auth.openai.com/phone-otp/select-channel",
            current_url="https://auth.openai.com/phone-otp/select-channel",
            source="api",
        )
        callback_state = FlowState(
            page_type="external_url",
            continue_url="https://chatgpt.com/api/auth/callback/openai?code=demo",
            current_url="https://chatgpt.com/api/auth/callback/openai?code=demo",
        )

        with mock.patch.object(
            client,
            "_bootstrap_oauth_session",
            return_value="https://auth.openai.com/log-in",
        ), mock.patch.object(
            client,
            "_submit_authorize_continue",
            return_value=email_otp_state,
        ), mock.patch.object(
            client,
            "_handle_otp_verification",
            return_value=existing_phone_state,
        ), mock.patch.object(
            client,
            "_handle_existing_phone_otp_verification",
            return_value=callback_state,
        ) as handle_existing_phone, mock.patch.object(
            client,
            "_exchange_code_for_tokens",
            return_value={"access_token": "at"},
        ):
            tokens = client.login_and_get_tokens(
                "user@example.com",
                "Secret123!",
                "device-fixed",
                prefer_passwordless_login=True,
                allow_phone_verification=True,
                allow_existing_phone_verification=True,
                stop_after_login=True,
                skymail_client=mock.Mock(),
            )

        self.assertEqual(tokens["access_token"], "at")
        handle_existing_phone.assert_called_once()


if __name__ == "__main__":
    unittest.main()
