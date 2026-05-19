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

from services.chatgpt_core.oauth_client import OAuthClient
from services.chatgpt_core.refresh_token_registration_engine import (
    RefreshTokenRegistrationEngine,
    RegistrationResult,
)
from services.chatgpt_core.utils import FlowState


class DummyEmailService:
    service_type = type("ST", (), {"value": "dummy"})()

    def create_email(self):
        return {"email": "user@example.com", "service_id": "svc-1"}

    def get_verification_code(self, **kwargs):
        return "123456"


class RefreshTokenRegistrationEngineTests(unittest.TestCase):
    def _make_engine(self, **kwargs):
        return RefreshTokenRegistrationEngine(
            email_service=DummyEmailService(),
            proxy_url="http://127.0.0.1:7890",
            callback_logger=lambda msg: None,
            max_retries=1,
            **kwargs,
        )

    @mock.patch("services.chatgpt_core.refresh_token_registration_engine.OAuthManager")
    @mock.patch("services.chatgpt_core.refresh_token_registration_engine.OAuthClient")
    @mock.patch("services.chatgpt_core.refresh_token_registration_engine.ChatGPTClient")
    def test_run_finishes_registration_then_enters_business_and_captures_artifacts(
        self,
        mock_chatgpt_client_cls,
        mock_oauth_client_cls,
        mock_oauth_manager_cls,
    ):
        register_client = mock.Mock()
        register_client.device_id = "device-fixed"
        register_client.ua = "UA"
        register_client.sec_ch_ua = '"Chromium";v="136"'
        register_client.impersonate = "chrome136"
        register_client.fingerprint = {"device_id": "device-fixed"}
        register_client.register_complete_flow.return_value = (True, "注册成功")
        register_client.reuse_session_and_get_tokens.return_value = (
            True,
            {"account_id": "acct-session", "workspace_id": "ws-session"},
        )
        mock_chatgpt_client_cls.return_value = register_client

        mock_oauth_client_cls.return_value = mock.Mock()
        mock_oauth_manager_cls.return_value = mock.Mock()

        engine = self._make_engine(extra_config={"register_max_retries": 1, "chatgpt_enable_team_invite": True})

        def fake_capture(*, result, **kwargs):
            result.success = True
            result.email = "user@example.com"
            result.password = "Secret123!"
            result.account_id = "acct-biz"
            result.workspace_id = "ws-biz"
            result.access_token = "at-biz"
            result.refresh_token = "rt-biz"
            result.session_token = "session-biz"
            result.source = "business_recovery"
            result.workspace_artifacts = [
                {"scope": "business", "label": "business", "account_id": "acct-biz", "workspace_id": "ws-biz", "access_token": "at-biz", "refresh_token": "rt-biz", "session_token": "session-biz", "source": "business_recovery", "variant_key": "business:ws-biz"},
                {"scope": "free", "label": "free", "account_id": "acct-free", "workspace_id": "ws-free", "access_token": "at-free", "refresh_token": "rt-free", "session_token": "session-free", "source": "workspace_capture_free", "variant_key": "free:ws-free"},
            ]
            result.metadata = {"selected_workspace_scopes": ["business", "free"]}
            return True

        with mock.patch.object(
            engine,
            "_enter_business_before_workspace_capture",
            return_value={"team_id": 7, "workspace_id": "ws-biz", "joined": True},
        ) as mock_enter_business, mock.patch.object(
            engine,
            "_capture_workspace_artifacts_after_business_join",
            side_effect=fake_capture,
        ) as mock_capture:
            result = engine.run()

        self.assertTrue(result.success)
        self.assertEqual(result.workspace_id, "ws-biz")
        self.assertEqual(result.refresh_token, "rt-biz")
        self.assertEqual(result.source, "business_recovery")
        self.assertEqual(len(result.workspace_artifacts or []), 2)
        self.assertEqual(result.workspace_artifacts[0]["scope"], "business")
        register_client.register_complete_flow.assert_called_once()
        register_kwargs = register_client.register_complete_flow.call_args.kwargs
        self.assertFalse(register_kwargs["stop_before_about_you_submission"])
        register_client.reuse_session_and_get_tokens.assert_called_once()
        mock_enter_business.assert_called_once()
        mock_capture.assert_called_once()

    def test_capture_workspace_artifacts_after_business_join_keeps_business_when_free_capture_fails(self):
        engine = self._make_engine(
            extra_config={
                "chatgpt_enable_team_invite": True,
                "chatgpt_capture_business_workspace": True,
                "chatgpt_capture_free_workspace": True,
            }
        )
        result = RegistrationResult(
            success=False,
            email="user@example.com",
            password="Secret123!",
            logs=[],
            metadata={},
        )
        register_client = mock.Mock(
            device_id="device-fixed",
            ua="UA",
            sec_ch_ua='"Chromium";v="136"',
            impersonate="chrome136",
            fingerprint={"device_id": "device-fixed"},
        )
        business_artifact = {
            "scope": "business",
            "label": "business",
            "account_id": "acct-biz",
            "workspace_id": "ws-biz",
            "access_token": "at-biz",
            "refresh_token": "rt-biz",
            "id_token": "id-biz",
            "session_token": "session-biz",
            "source": "business_recovery",
            "variant_key": "business:ws-biz",
        }

        with mock.patch.object(engine, "_build_workspace_artifact", return_value=business_artifact), mock.patch.object(
            engine,
            "_capture_workspace_artifact_via_fresh_login",
            return_value=None,
        ) as mock_capture:
            ok = engine._capture_workspace_artifacts_after_business_join(
                result=result,
                register_client=register_client,
                email_adapter=mock.Mock(),
                first_name="Ivy",
                last_name="Stone",
                birthdate="1990-01-02",
                business_join_result={
                    "team_id": 7,
                    "workspace_id": "ws-biz",
                    "joined": True,
                    "tokens": {
                        "access_token": "at-biz",
                        "refresh_token": "rt-biz",
                        "id_token": "id-biz",
                        "account_id": "acct-biz",
                    },
                    "oauth_client": mock.Mock(),
                    "source": "business_recovery",
                },
            )

        self.assertTrue(ok)
        self.assertTrue(result.success)
        self.assertEqual(result.workspace_id, "ws-biz")
        self.assertEqual(result.refresh_token, "rt-biz")
        self.assertEqual(result.source, "business_recovery")
        self.assertEqual(result.error_message, "")
        self.assertEqual([item["scope"] for item in (result.workspace_artifacts or [])], ["business"])

    @mock.patch("services.chatgpt_core.refresh_token_registration_engine.OAuthManager")
    @mock.patch("services.chatgpt_core.refresh_token_registration_engine.OAuthClient")
    @mock.patch("services.chatgpt_core.refresh_token_registration_engine.ChatGPTClient")
    def test_run_saves_registration_access_token_when_workspace_capture_fails(
        self,
        mock_chatgpt_client_cls,
        mock_oauth_client_cls,
        mock_oauth_manager_cls,
    ):
        register_client = mock.Mock()
        register_client.device_id = "device-fixed"
        register_client.ua = "UA"
        register_client.sec_ch_ua = '"Chromium";v="136"'
        register_client.impersonate = "chrome136"
        register_client.fingerprint = {"device_id": "device-fixed"}
        register_client.register_complete_flow.return_value = (True, "注册成功")
        register_client.reuse_session_and_get_tokens.return_value = (
            True,
            {
                "access_token": "at-registration",
                "session_token": "session-registration",
                "account_id": "acct-registration",
                "workspace_id": "acct-registration",
            },
        )
        mock_chatgpt_client_cls.return_value = register_client
        mock_oauth_client_cls.return_value = mock.Mock()
        mock_oauth_manager_cls.return_value = mock.Mock()

        engine = self._make_engine(
            extra_config={
                "register_max_retries": 1,
                "chatgpt_save_registration_access_token_account": True,
                "chatgpt_capture_free_workspace": True,
                "chatgpt_enable_team_invite": False,
            }
        )

        with mock.patch.object(engine, "_finalize_workspace_artifacts", return_value=False) as finalize:
            result = engine.run()

        self.assertTrue(result.success)
        self.assertEqual(result.source, "registration_session")
        self.assertEqual(result.access_token, "at-registration")
        self.assertEqual(result.session_token, "session-registration")
        self.assertEqual(result.workspace_artifacts[0]["variant_key"], "registration_at:acct-registration")
        self.assertTrue(result.metadata["registration_access_token_saved"])
        finalize.assert_called_once()

    @mock.patch("services.chatgpt_core.refresh_token_registration_engine.OAuthManager")
    @mock.patch("services.chatgpt_core.refresh_token_registration_engine.OAuthClient")
    @mock.patch("services.chatgpt_core.refresh_token_registration_engine.ChatGPTClient")
    def test_run_switches_to_login_when_register_flow_reports_existing_account(
        self,
        mock_chatgpt_client_cls,
        mock_oauth_client_cls,
        mock_oauth_manager_cls,
    ):
        register_client = mock.Mock()
        register_client.device_id = "device-fixed"
        register_client.ua = "UA"
        register_client.sec_ch_ua = '"Chromium";v="136"'
        register_client.impersonate = "chrome136"
        register_client.register_complete_flow.return_value = (
            False,
            "创建账号失败: HTTP 400: user_already_exists",
        )
        mock_chatgpt_client_cls.return_value = register_client

        oauth_client = mock.Mock()
        oauth_client.login_and_get_tokens.return_value = {
            "access_token": "at",
            "refresh_token": "rt",
            "id_token": "id-token",
        }
        oauth_client.last_workspace_id = "ws-1"
        oauth_client._decode_oauth_session_cookie.return_value = {
            "workspaces": [{"id": "ws-1"}]
        }
        oauth_client._get_cookie_value.return_value = ""
        mock_oauth_client_cls.return_value = oauth_client

        oauth_manager = mock.Mock()
        oauth_manager.extract_account_info.return_value = {
            "email": "user@example.com",
            "account_id": "acct-existing",
        }
        mock_oauth_manager_cls.return_value = oauth_manager

        engine = self._make_engine()
        result = engine.run()

        self.assertTrue(result.success)
        self.assertEqual(result.source, "login")
        self.assertEqual(result.account_id, "acct-existing")
        login_kwargs = oauth_client.login_and_get_tokens.call_args.kwargs
        self.assertEqual(login_kwargs["login_source"], "existing_account_recovery")

    @mock.patch("services.chatgpt_core.refresh_token_registration_engine.OAuthManager")
    @mock.patch("services.chatgpt_core.refresh_token_registration_engine.OAuthClient")
    @mock.patch("services.chatgpt_core.refresh_token_registration_engine.ChatGPTClient")
    def test_run_keeps_first_created_email_when_global_retry_disabled(
        self,
        mock_chatgpt_client_cls,
        mock_oauth_client_cls,
        mock_oauth_manager_cls,
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

            def get_verification_code(self, **kwargs):
                return "123456"

        register_client = mock.Mock()
        register_client.device_id = "device-fixed"
        register_client.ua = "UA"
        register_client.sec_ch_ua = '"Chromium";v="136"'
        register_client.impersonate = "chrome136"
        register_client.register_complete_flow.return_value = (False, "network timeout")
        mock_chatgpt_client_cls.return_value = register_client

        mock_oauth_client_cls.return_value = mock.Mock()
        mock_oauth_manager_cls.return_value = mock.Mock()

        engine = RefreshTokenRegistrationEngine(
            email_service=RotatingEmailService(),
            proxy_url="http://127.0.0.1:7890",
            callback_logger=lambda msg: None,
            max_retries=2,
        )
        result = engine.run()

        self.assertFalse(result.success)
        self.assertIn("注册状态机失败", result.error_message)
        call_args = register_client.register_complete_flow.call_args_list
        self.assertEqual(len(call_args), 1)
        self.assertEqual(call_args[0].args[0], "user1@example.com")

    @mock.patch.object(RefreshTokenRegistrationEngine, "_recover_workspace_with_business")
    @mock.patch("services.chatgpt_core.refresh_token_registration_engine.OAuthManager")
    @mock.patch("services.chatgpt_core.refresh_token_registration_engine.OAuthClient")
    @mock.patch("services.chatgpt_core.refresh_token_registration_engine.ChatGPTClient")
    def test_run_switches_into_business_recovery_when_workspace_missing(
        self,
        mock_chatgpt_client_cls,
        mock_oauth_client_cls,
        mock_oauth_manager_cls,
        mock_recover,
    ):
        register_client = mock.Mock()
        register_client.device_id = "device-fixed"
        register_client.ua = "UA"
        register_client.sec_ch_ua = '"Chromium";v="136"'
        register_client.impersonate = "chrome136"
        register_client.fingerprint = {"device_id": "device-fixed"}
        register_client.register_complete_flow.return_value = (
            False,
            "创建账号失败: HTTP 400: user_already_exists",
        )
        mock_chatgpt_client_cls.return_value = register_client

        oauth_client = mock.Mock()
        oauth_client.login_and_get_tokens.return_value = None
        oauth_client.last_error = "未获取到 workspace / callback"
        oauth_client.last_state = FlowState(
            page_type="add_phone",
            continue_url="https://auth.openai.com/add-phone",
            current_url="https://auth.openai.com/add-phone",
        )
        mock_oauth_client_cls.return_value = oauth_client

        recovered_oauth_client = mock.Mock()
        recovered_oauth_client.last_workspace_id = "ws-recovered"
        recovered_oauth_client._decode_oauth_session_cookie.return_value = {
            "workspaces": [{"id": "ws-recovered"}]
        }
        recovered_oauth_client._get_cookie_value.return_value = "session-recovered"
        mock_recover.return_value = {
            "tokens": {
                "access_token": "at-recovered",
                "refresh_token": "rt-recovered",
                "id_token": "id-recovered",
                "account_id": "acct-recovered",
            },
            "oauth_client": recovered_oauth_client,
            "team_id": 7,
            "joined": True,
        }

        oauth_manager = mock.Mock()
        oauth_manager.extract_account_info.return_value = {
            "email": "user@example.com",
            "account_id": "acct-recovered",
        }
        mock_oauth_manager_cls.return_value = oauth_manager

        engine = self._make_engine(
            extra_config={
                "chatgpt_enable_team_invite": True,
            }
        )
        result = engine.run()

        self.assertTrue(result.success)
        self.assertEqual(result.source, "business_recovery")
        self.assertEqual(result.workspace_id, "ws-recovered")
        self.assertEqual(result.metadata["business_recovery_team_id"], 7)
        self.assertTrue(result.metadata["business_recovery_joined"])
        mock_recover.assert_called_once()

    def test_workspace_capture_defaults_to_free_when_team_invite_disabled(self):
        engine = self._make_engine(extra_config={})
        self.assertFalse(engine._is_team_invite_enabled())
        self.assertEqual(engine._resolve_workspace_capture_scopes(current_scope=""), ["free"])
        self.assertEqual(engine._resolve_workspace_capture_scopes(current_scope="free"), ["free"])

    def test_workspace_capture_can_be_explicitly_disabled_when_team_invite_disabled(self):
        engine = self._make_engine(
            extra_config={
                "chatgpt_capture_free_workspace": False,
            }
        )
        self.assertFalse(engine._is_team_invite_enabled())
        self.assertEqual(engine._resolve_workspace_capture_scopes(current_scope=""), [])
        self.assertEqual(engine._resolve_workspace_capture_scopes(current_scope="free"), [])

    @mock.patch.object(RefreshTokenRegistrationEngine, "_recover_workspace_with_business")
    @mock.patch("services.chatgpt_core.refresh_token_registration_engine.OAuthManager")
    @mock.patch("services.chatgpt_core.refresh_token_registration_engine.OAuthClient")
    @mock.patch("services.chatgpt_core.refresh_token_registration_engine.ChatGPTClient")
    def test_run_skips_business_recovery_when_team_invite_disabled(
        self,
        mock_chatgpt_client_cls,
        mock_oauth_client_cls,
        mock_oauth_manager_cls,
        mock_recover,
    ):
        register_client = mock.Mock()
        register_client.device_id = "device-fixed"
        register_client.ua = "UA"
        register_client.sec_ch_ua = '"Chromium";v="136"'
        register_client.impersonate = "chrome136"
        register_client.register_complete_flow.return_value = (
            False,
            "创建账号失败: HTTP 400: user_already_exists",
        )
        mock_chatgpt_client_cls.return_value = register_client

        oauth_client = mock.Mock()
        oauth_client.login_and_get_tokens.return_value = None
        oauth_client.last_error = "OAuth 登录状态机失败"
        oauth_client.last_state = FlowState(
            page_type="add_phone",
            continue_url="https://auth.openai.com/add-phone",
            current_url="https://auth.openai.com/add-phone",
        )
        mock_oauth_client_cls.return_value = oauth_client
        mock_oauth_manager_cls.return_value = mock.Mock()

        engine = self._make_engine(extra_config={"chatgpt_enable_team_invite": False})
        result = engine.run()

        self.assertFalse(result.success)
        self.assertIn("OAuth 登录状态机失败", result.error_message)
        mock_recover.assert_not_called()


class OAuthClientPasswordlessTests(unittest.TestCase):
    def _make_client(self):
        return OAuthClient({}, proxy="http://127.0.0.1:7890", verbose=False)

    def test_login_and_get_tokens_prefers_passwordless_over_password_verify(self):
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

        with mock.patch.object(client, "_bootstrap_oauth_session", return_value="https://auth.openai.com/log-in"), \
            mock.patch.object(client, "_submit_authorize_continue", return_value=login_password_state) as submit_continue, \
            mock.patch.object(client, "_send_passwordless_login_otp", return_value=email_otp_state) as send_passwordless, \
            mock.patch.object(client, "_handle_otp_verification", return_value=consent_state), \
            mock.patch.object(client, "_oauth_submit_workspace_and_org", return_value=("auth-code", None)), \
            mock.patch.object(client, "_exchange_code_for_tokens", return_value={"access_token": "at"}), \
            mock.patch.object(client, "_submit_password_verify") as submit_password:
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
        submit_continue.assert_called_once()
        self.assertEqual(submit_continue.call_args.kwargs["screen_hint"], "login")
        send_passwordless.assert_called_once()
        submit_password.assert_not_called()

    def test_login_and_get_tokens_visits_add_phone_continue_url_before_phone_branch(self):
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

        with mock.patch.object(client, "_bootstrap_oauth_session", return_value="https://auth.openai.com/log-in"), \
            mock.patch.object(client, "_submit_authorize_continue", return_value=add_phone_state), \
            mock.patch.object(client, "_follow_flow_state", return_value=(None, consent_state)) as follow_state, \
            mock.patch.object(client, "_oauth_submit_workspace_and_org", return_value=("auth-code", None)), \
            mock.patch.object(client, "_exchange_code_for_tokens", return_value={"access_token": "at"}), \
            mock.patch.object(client, "_handle_add_phone_verification") as handle_phone:
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

    def test_login_and_get_tokens_uses_canonical_consent_url_when_state_is_add_phone(self):
        client = self._make_client()
        add_phone_state = FlowState(
            page_type="add_phone",
            continue_url="https://auth.openai.com/add-phone",
            current_url="https://auth.openai.com/add-phone",
        )

        with mock.patch.object(client, "_bootstrap_oauth_session", return_value="https://auth.openai.com/log-in"), \
            mock.patch.object(client, "_submit_authorize_continue", return_value=add_phone_state), \
            mock.patch.object(client, "_state_supports_workspace_resolution", return_value=True), \
            mock.patch.object(client, "_state_requires_navigation", return_value=False), \
            mock.patch.object(client, "_oauth_submit_workspace_and_org", return_value=("auth-code", None)) as submit_workspace, \
            mock.patch.object(client, "_exchange_code_for_tokens", return_value={"access_token": "at"}):
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

    def test_login_and_get_tokens_retries_once_when_add_phone_has_no_workspace(self):
        client = self._make_client()
        add_phone_state = FlowState(
            page_type="add_phone",
            continue_url="https://auth.openai.com/add-phone",
            current_url="https://auth.openai.com/add-phone",
        )

        with mock.patch.object(client, "_bootstrap_oauth_session", return_value="https://auth.openai.com/log-in") as bootstrap, \
            mock.patch.object(client, "_submit_authorize_continue", return_value=add_phone_state) as submit_continue, \
            mock.patch.object(client, "_state_supports_workspace_resolution", return_value=False), \
            mock.patch.object(client, "_state_requires_navigation", return_value=False):
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
        self.assertIn("未获取到 workspace / callback", client.last_error)

    def test_send_passwordless_login_otp_does_not_send_email_field(self):
        client = self._make_client()
        response = mock.Mock()
        response.status_code = 200
        response.url = "https://auth.openai.com/api/accounts/passwordless/send-otp"
        response.json.return_value = {"page": {"type": "email_otp_verification"}}
        client.session.post = mock.Mock(return_value=response)

        expected_state = FlowState(
            page_type="email_otp_verification",
            continue_url="https://auth.openai.com/email-verification",
            current_url="https://auth.openai.com/email-verification",
        )
        with mock.patch.object(
            client,
            "_state_from_payload",
            return_value=expected_state,
        ):
            state = client._send_passwordless_login_otp(
                "user@example.com",
                "device-fixed",
            )

        self.assertEqual(state, expected_state)
        kwargs = client.session.post.call_args.kwargs
        self.assertNotIn("json", kwargs)
        self.assertNotIn("data", kwargs)

    def test_login_and_get_tokens_submits_about_you_when_configured(self):
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

        with mock.patch.object(client, "_bootstrap_oauth_session", return_value="https://auth.openai.com/log-in"), \
            mock.patch.object(client, "_submit_authorize_continue", return_value=about_you_state), \
            mock.patch.object(client, "_submit_about_you_create_account", return_value=consent_state) as submit_about_you, \
            mock.patch.object(client, "_oauth_submit_workspace_and_org", return_value=("auth-code", None)), \
            mock.patch.object(client, "_exchange_code_for_tokens", return_value={"access_token": "at"}):
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
        self.assertEqual(submit_about_you.call_args.args[0], "Ivy")
        self.assertEqual(submit_about_you.call_args.args[1], "Stone")
        self.assertEqual(submit_about_you.call_args.args[2], "1990-01-02")

    def test_login_and_get_tokens_retries_after_about_you_for_workspace_capture_free(self):
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

        with mock.patch.object(client, "_bootstrap_oauth_session", side_effect=[
            "https://auth.openai.com/log-in",
            "https://auth.openai.com/log-in",
        ]), \
            mock.patch.object(client, "_submit_authorize_continue", side_effect=[login_password_state, login_password_state]), \
            mock.patch.object(client, "_send_passwordless_login_otp", side_effect=[email_otp_state, email_otp_state]), \
            mock.patch.object(client, "_handle_otp_verification", side_effect=[about_you_state, consent_state]) as handle_otp, \
            mock.patch.object(client, "_submit_about_you_create_account") as submit_about_you, \
            mock.patch.object(client, "_oauth_submit_workspace_and_org", return_value=("auth-code", None)) as submit_workspace, \
            mock.patch.object(client, "_exchange_code_for_tokens", return_value={"access_token": "at"}):
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
                login_source="workspace_capture_free",
                workspace_scope_preference="free",
            )

        self.assertEqual(tokens["access_token"], "at")
        self.assertEqual(handle_otp.call_count, 2)
        submit_about_you.assert_not_called()
        self.assertEqual(
            submit_workspace.call_args.args[0],
            "https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
        )


if __name__ == "__main__":
    unittest.main()
