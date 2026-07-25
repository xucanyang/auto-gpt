import unittest
from unittest import mock

from services.chatgpt_core.access_token_only_registration_engine import (
    AccessTokenOnlyRegistrationEngine,
    EmailServiceAdapter,
)
from services.chatgpt_core.payment import CheckoutRequestError
from services.chatgpt_core.registration_route_policy import ExistingAccountLoginRouteBlocked
from services.chatgpt_core.sentinel_browser import BrowserRegistrationStageResult


class AccessTokenOnlyCheckoutTests(unittest.TestCase):
    class _FakeChatGPTClient:
        device_id = "device-demo"

        def __init__(self, *args, **kwargs):
            self._log = None

        def register_complete_flow(self, *args, **kwargs):
            return True, "ok"

        def reuse_session_and_get_tokens(self):
            return True, {
                "access_token": "at-demo",
                "session_token": "session-demo",
                "account_id": "acct-demo",
                "workspace_id": "ws-demo",
                "cookies": "oai-did=device",
            }

    class _TrackingChatGPTClient(_FakeChatGPTClient):
        last_register_kwargs = {}

        def register_complete_flow(self, *args, **kwargs):
            type(self).last_register_kwargs = dict(kwargs)
            return True, "ok"

    class _ExistingRouteChatGPTClient(_TrackingChatGPTClient):
        def register_complete_flow(self, *args, **kwargs):
            type(self).last_register_kwargs = dict(kwargs)
            self.last_registration_route_event = {
                "email": args[0] if args else "buyer@example.com",
                "reason": "registration_completed_without_create_account_after_otp",
                "stage": "register_complete_flow",
            }
            return False, "user_already_exists: existing_account_login_route"

    def test_browser_infrastructure_failure_does_not_retry_same_email(self):
        engine = AccessTokenOnlyRegistrationEngine(
            email_service=mock.Mock(),
            proxy_url="http://proxy.local:8080",
        )

        self.assertFalse(
            engine._should_retry(
                "创建账号失败: auth_browser_finalize_unavailable: sentinel_browser_unavailable"
            )
        )
        self.assertTrue(
            engine._should_retry("创建账号失败: HTTP 400: registration_disallowed")
        )
        self.assertFalse(
            engine._should_retry("browser_registration_unavailable: worker crashed")
        )

    def test_protocol_registration_never_falls_back_to_browser(self):
        email_service = mock.Mock()
        email_service.create_email.return_value = {"email": "buyer@example.com"}
        engine = AccessTokenOnlyRegistrationEngine(
            email_service=email_service,
            proxy_url="http://proxy.local:8080",
            browser_mode="protocol",
            max_retries=1,
        )

        class ProtocolClient(self._FakeChatGPTClient):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.session = mock.Mock()
                self.registration_transport = "protocol"
                self.last_registration_state = None

            def _check_stop(self):
                return None

            def register_complete_flow(self, *args, **kwargs):
                return False, "创建账号失败: HTTP 400: registration_disallowed"

        with (
            mock.patch.object(
                engine, "_probe_homepage_before_email_creation", return_value=(True, "")
            ),
            mock.patch.object(engine, "_report_homepage_probe"),
            mock.patch.object(engine, "_probe_plus_checkout_billing", return_value={}),
            mock.patch(
                "services.chatgpt_core.access_token_only_registration_engine.ChatGPTClient",
                ProtocolClient,
            ),
            mock.patch(
                "services.chatgpt_core.access_token_only_registration_engine.run_browser_registration_stage",
            ) as browser_stage,
            mock.patch(
                "services.chatgpt_core.access_token_only_registration_engine.run_browser_oauth_token_recovery",
            ) as browser_oauth,
        ):
            result = engine.run()

        self.assertFalse(result.success)
        self.assertIn("registration_disallowed", result.error_message)
        browser_stage.assert_not_called()
        browser_oauth.assert_not_called()

    def test_browser_modes_start_with_browser_registration_and_preserve_headless_flag(self):
        for browser_mode, expected_headless in (("headless", True), ("headed", False)):
            with self.subTest(browser_mode=browser_mode):
                email_service = mock.Mock()
                email_service.create_email.return_value = {"email": "buyer@example.com"}
                engine = AccessTokenOnlyRegistrationEngine(
                    email_service=email_service,
                    proxy_url="http://proxy.local:8080",
                    browser_mode=browser_mode,
                    max_retries=1,
                )
                client = mock.Mock(
                    device_id="device-demo",
                    ua="Mozilla/5.0",
                    sec_ch_ua='"Chromium";v="145"',
                    impersonate="chrome145",
                    fingerprint=None,
                    session=mock.Mock(),
                    last_registration_state=None,
                    registration_transport="protocol",
                )
                client.register_complete_flow.side_effect = AssertionError(
                    "browser mode must not execute protocol registration"
                )
                client.reuse_session_and_get_tokens.side_effect = AssertionError(
                    "browser mode must not reuse curl session"
                )
                stage_result = BrowserRegistrationStageResult(
                    final_state={
                        "page_type": "oauth_callback",
                        "current_url": "https://chatgpt.com/api/auth/callback/openai?code=demo",
                        "method": "GET",
                    },
                    page_url="https://chatgpt.com/api/auth/callback/openai?code=demo",
                    cookies=[
                        {
                            "name": "login_session",
                            "value": "demo",
                            "domain": "auth.openai.com",
                            "path": "/",
                        }
                    ],
                    cookie_names=("login_session",),
                    device_id="device-demo",
                    user_agent="Mozilla/5.0 Camoufox",
                    requested_executor=browser_mode,
                    effective_executor=browser_mode,
                    web_session={
                        "access_token": "at-demo",
                        "session_token": "session-demo",
                        "account_id": "acct-demo",
                        "workspace_id": "ws-demo",
                    },
                )

                with (
                    mock.patch.object(
                        engine,
                        "_probe_homepage_before_email_creation",
                        return_value=(True, ""),
                    ),
                    mock.patch.object(engine, "_report_homepage_probe"),
                    mock.patch.object(engine, "_build_chatgpt_client", return_value=client),
                    mock.patch.object(engine, "_probe_plus_checkout_billing", return_value={}),
                    mock.patch(
                        "services.chatgpt_core.access_token_only_registration_engine.run_browser_registration_stage",
                        return_value=stage_result,
                    ) as browser_stage,
                ):
                    result = engine.run()

                self.assertTrue(result.success, result.error_message)
                client.register_complete_flow.assert_not_called()
                client.reuse_session_and_get_tokens.assert_not_called()
                browser_stage.assert_called_once()
                self.assertIs(
                    browser_stage.call_args.kwargs["headless"],
                    expected_headless,
                )

    def test_browser_signup_full_flow_is_never_replayed(self):
        email_service = mock.Mock()
        email_service.create_email.return_value = {"email": "buyer@example.com"}
        engine = AccessTokenOnlyRegistrationEngine(
            email_service=email_service,
            proxy_url="http://proxy.local:8080",
            browser_mode="headless",
            max_retries=3,
        )
        client = mock.Mock(
            device_id="device-demo",
            ua="Mozilla/5.0",
            sec_ch_ua='"Chromium";v="145"',
            impersonate="chrome145",
            fingerprint=None,
            last_registration_route_event=None,
            registration_transport="camoufox_browser",
            registration_stage_transports=[],
        )
        browser_result = BrowserRegistrationStageResult(
            error="创建账号失败: HTTP 400: registration_disallowed"
        )

        with (
            mock.patch.object(engine, "_build_chatgpt_client", return_value=client),
            mock.patch.object(
                engine,
                "_run_browser_registration",
                return_value=browser_result,
            ) as browser_stage,
        ):
            result = engine.run()

        self.assertFalse(result.success)
        self.assertIn("registration_disallowed", result.error_message)
        browser_stage.assert_called_once()
        email_service.create_email.assert_called_once()
        self.assertTrue(
            any("固定为单次执行" in line for line in result.logs)
        )

    def test_signup_committed_callback_finalizes_hme_before_web_session(self):
        """about_you commit must retire the HME lease before Web Session can hang."""
        email_service = mock.Mock()
        email_service.create_email.return_value = {"email": "buyer@example.com"}
        adapter = EmailServiceAdapter(
            email_service, "buyer@example.com", lambda _message: None
        )
        engine = AccessTokenOnlyRegistrationEngine(
            email_service=email_service,
            browser_mode="headless",
            max_retries=1,
        )
        client = mock.Mock(device_id="device-demo", _check_stop=mock.Mock())
        stage_result = BrowserRegistrationStageResult(
            final_state={"page_type": "oauth_callback"},
            page_url="https://chatgpt.com/",
            web_session={"access_token": "at-demo", "session_token": "st-demo"},
            device_id="device-demo",
            user_agent="Mozilla/5.0",
            effective_executor="headless",
        )

        with mock.patch(
            "services.chatgpt_core.access_token_only_registration_engine.run_browser_registration_stage",
            return_value=stage_result,
        ) as run_stage:
            engine._run_browser_registration(
                chatgpt_client=client,
                email_addr="buyer@example.com",
                password="Password123!",
                skymail_adapter=adapter,
                otp_wait_timeout=30,
                otp_account_budget_timeout=60,
            )
            otp_callback = run_stage.call_args.kwargs["otp_callback"]
            otp_callback(
                {
                    "action": "signup_committed",
                    "email": "buyer@example.com",
                    "page_type": "oauth_callback",
                }
            )

        email_service.finalize_success.assert_called()
        self.assertTrue(getattr(engine, "_mailbox_finalized", False))

    def test_missing_web_session_after_signup_saves_auth_pending(self):
        """Signup finished but AT missing must not hard-fail the attempt.

        Live logs showed many FAIL+late_failure rows with session-token cookies
        already present; those identities should be saved as auth_pending (and
        HME finalize_success) after isolated OAuth also fails to mint AT.
        """
        email_service = mock.Mock()
        email_service.create_email.return_value = {"email": "buyer@example.com"}
        engine = AccessTokenOnlyRegistrationEngine(
            email_service=email_service,
            proxy_url="http://proxy.local:8080",
            browser_mode="headless",
            max_retries=1,
        )
        client = mock.Mock(
            device_id="device-demo",
            ua="Mozilla/5.0",
            sec_ch_ua='"Chromium";v="145"',
            impersonate="chrome145",
            fingerprint=None,
            last_registration_route_event=None,
            registration_transport="camoufox_browser",
            registration_stage_transports=[],
            registration_runtime_profile={},
        )
        browser_stage = BrowserRegistrationStageResult(
            final_state={
                "page_type": "oauth_callback",
                "current_url": "https://chatgpt.com/",
            },
            page_url="https://chatgpt.com/",
            web_session={
                "access_token": "",
                "session_token": "session-token-demo",
                "cookies": "__Secure-next-auth.session-token=session-token-demo",
            },
            error=(
                "browser_registration_missing_web_session: signup finished but "
                "ChatGPT /api/auth/session returned no accessToken"
            ),
            device_id="device-demo",
            user_agent="Mozilla/5.0 Camoufox",
            effective_executor="headless",
        )
        self.assertTrue(browser_stage.signup_complete)
        self.assertFalse(browser_stage.ok)

        with (
            mock.patch.object(engine, "_build_chatgpt_client", return_value=client),
            mock.patch.object(
                engine,
                "_run_browser_registration",
                return_value=browser_stage,
            ),
            mock.patch.object(
                engine,
                "_capture_browser_oauth_tokens",
                return_value=(False, "oauth recovery failed"),
            ) as oauth_capture,
            mock.patch.object(engine, "_probe_plus_checkout_billing", return_value={}),
        ):
            result = engine.run()

        self.assertTrue(result.success)
        self.assertEqual(result.source, "registered_auth_pending")
        self.assertTrue(result.metadata.get("registered_auth_pending"))
        self.assertTrue(result.metadata.get("needs_auth_capture"))
        self.assertEqual(result.session_token, "session-token-demo")
        oauth_capture.assert_called_once()
        email_service.finalize_success.assert_called()

    def test_browser_signup_existing_account_routes_to_browser_login_recovery(self):
        email_service = mock.Mock()
        email_service.create_email.return_value = {"email": "existing@example.com"}
        engine = AccessTokenOnlyRegistrationEngine(
            email_service=email_service,
            proxy_url="http://proxy.local:8080",
            browser_mode="headless",
            max_retries=1,
        )
        client = mock.Mock(
            device_id="device-demo",
            ua="Mozilla/5.0",
            sec_ch_ua='"Chromium";v="145"',
            impersonate="chrome145",
            fingerprint=None,
            last_registration_route_event={
                "email": "existing@example.com",
                "stage": "after_email",
                "signal": "login_password",
                "page_type": "login_password",
                "source": "browser_registration",
                "reason": "login_password",
                "deterministic": True,
            },
            registration_transport="camoufox_browser",
            registration_stage_transports=[],
        )
        browser_stage = BrowserRegistrationStageResult(
            error=(
                "user_already_exists: browser registration reached login_password; "
                "use explicit existing-account capture instead"
            )
        )

        with (
            mock.patch.object(engine, "_build_chatgpt_client", return_value=client),
            mock.patch.object(
                engine,
                "_run_browser_registration",
                return_value=browser_stage,
            ),
            mock.patch.object(
                engine,
                "_capture_browser_oauth_tokens",
                return_value=(
                    True,
                    {
                        "access_token": "at-existing",
                        "session_token": "session-existing",
                        "account_id": "acct-existing",
                        "workspace_id": "ws-existing",
                    },
                ),
            ) as browser_login,
            mock.patch.object(engine, "_probe_plus_checkout_billing", return_value={}),
            mock.patch(
                "services.chatgpt_core.oauth_client.OAuthClient",
                side_effect=AssertionError("browser signup switched to protocol login"),
            ) as protocol_login,
        ):
            result = engine.run()

        self.assertTrue(result.success)
        self.assertEqual(result.account_id, "acct-existing")
        self.assertEqual(
            result.metadata["chatgpt_existing_account_login_route"]["action"],
            "login_recovery",
        )
        self.assertEqual(
            result.metadata["chatgpt_existing_account_login_route"]["stage"],
            "after_email",
        )
        browser_login.assert_called_once()
        protocol_login.assert_not_called()

    def test_browser_signup_existing_account_skips_when_login_route_disabled(self):
        email_service = mock.Mock()
        email_service.create_email.return_value = {"email": "existing@example.com"}
        engine = AccessTokenOnlyRegistrationEngine(
            email_service=email_service,
            proxy_url="http://proxy.local:8080",
            browser_mode="headless",
            max_retries=1,
            extra_config={"chatgpt_existing_account_login_route_enabled": False},
        )
        client = mock.Mock(
            device_id="device-demo",
            ua="Mozilla/5.0",
            sec_ch_ua='"Chromium";v="145"',
            impersonate="chrome145",
            fingerprint=None,
            last_registration_route_event={
                "email": "existing@example.com",
                "stage": "about_you",
                "signal": "account_already_exists",
                "page_type": "about_you",
                "source": "browser_registration",
                "reason": "An account already exists for this email address.",
                "deterministic": True,
            },
            registration_transport="camoufox_browser",
            registration_stage_transports=[],
        )
        browser_stage = BrowserRegistrationStageResult(
            error=(
                "browser_registration_failed: ExistingAccountDetected: "
                "user_already_exists: stage=about_you signal=account_already_exists"
            )
        )

        with (
            mock.patch.object(engine, "_build_chatgpt_client", return_value=client),
            mock.patch.object(
                engine,
                "_run_browser_registration",
                return_value=browser_stage,
            ),
            mock.patch.object(engine, "_capture_browser_oauth_tokens") as browser_login,
        ):
            with self.assertRaises(ExistingAccountLoginRouteBlocked) as caught:
                engine.run()

        self.assertEqual(caught.exception.email, "existing@example.com")
        self.assertTrue(caught.exception.route_event["blocked"])
        self.assertEqual(caught.exception.route_event["action"], "skip_save")
        self.assertEqual(caught.exception.route_event["stage"], "about_you")
        browser_login.assert_not_called()

    def test_explicit_browser_existing_account_capture_uses_browser_oauth_only(self):
        email_service = mock.Mock()
        email_service.create_email.return_value = {"email": "existing@example.com"}
        engine = AccessTokenOnlyRegistrationEngine(
            email_service=email_service,
            proxy_url="http://proxy.local:8080",
            browser_mode="headed",
            max_retries=1,
            extra_config={"chatgpt_existing_account_capture": True},
        )
        client = mock.Mock(
            device_id="device-demo",
            ua="Mozilla/5.0",
            sec_ch_ua='"Chromium";v="145"',
            impersonate="chrome145",
            fingerprint=None,
            registration_transport="camoufox_browser_oauth",
            registration_runtime_profile={
                "browser_family": "camoufox",
                "device_id": "device-demo",
                "user_agent": "Mozilla/5.0 Camoufox",
            },
            registration_stage_transports=[],
        )
        browser_tokens = {
            "access_token": "at-existing",
            "refresh_token": "rt-existing",
            "session_token": "session-existing",
            "account_id": "acct-existing",
            "workspace_id": "ws-existing",
        }

        with (
            mock.patch.object(engine, "_build_chatgpt_client", return_value=client),
            mock.patch.object(
                engine,
                "_capture_browser_oauth_tokens",
                return_value=(True, browser_tokens),
            ) as browser_login,
            mock.patch.object(engine, "_probe_plus_checkout_billing", return_value={}),
            mock.patch(
                "services.chatgpt_core.oauth_client.OAuthClient",
                side_effect=AssertionError("explicit browser capture used protocol OAuth"),
            ) as protocol_login,
        ):
            result = engine.run()

        self.assertTrue(result.success, result.error_message)
        self.assertEqual(result.account_id, "acct-existing")
        browser_login.assert_called_once()
        protocol_login.assert_not_called()

    def test_engine_retry_reuses_one_generated_profile(self):
        email_service = mock.Mock()
        email_service.create_email.return_value = {"email": "buyer@example.com"}
        engine = AccessTokenOnlyRegistrationEngine(
            email_service=email_service,
            proxy_url="http://proxy.local:8080",
            browser_mode="protocol",
            max_retries=2,
        )
        client = mock.Mock(
            device_id="device-demo",
            ua="Mozilla/5.0",
            sec_ch_ua='"Chromium";v="145"',
            impersonate="chrome145",
            fingerprint=None,
            registration_transport="protocol",
        )
        client.register_complete_flow.side_effect = [
            (False, "OTP transient failure"),
            (True, "registration complete"),
        ]
        client.reuse_session_and_get_tokens.return_value = (
            True,
            {
                "access_token": "at-demo",
                "session_token": "session-demo",
                "account_id": "acct-demo",
                "workspace_id": "ws-demo",
            },
        )

        with (
            mock.patch.object(
                engine, "_probe_homepage_before_email_creation", return_value=(True, "")
            ),
            mock.patch.object(engine, "_report_homepage_probe"),
            mock.patch.object(engine, "_build_chatgpt_client", return_value=client),
            mock.patch.object(engine, "_probe_plus_checkout_billing", return_value={}),
            mock.patch(
                "services.chatgpt_core.access_token_only_registration_engine.generate_random_name",
                side_effect=[("Fixed", "Profile"), ("Changed", "Identity")],
            ) as generate_name,
            mock.patch(
                "services.chatgpt_core.access_token_only_registration_engine.generate_random_birthday",
                side_effect=["1990-01-02", "2001-03-04"],
            ) as generate_birthday,
            mock.patch(
                "services.chatgpt_core.access_token_only_registration_engine.time.sleep"
            ),
        ):
            result = engine.run()

        self.assertTrue(result.success, result.error_message)
        self.assertEqual(generate_name.call_count, 1)
        self.assertEqual(generate_birthday.call_count, 1)
        profiles = [call.args[2:5] for call in client.register_complete_flow.call_args_list]
        self.assertEqual(
            profiles,
            [
                ("Fixed", "Profile", "1990-01-02"),
                ("Fixed", "Profile", "1990-01-02"),
            ],
        )

    def test_unknown_executor_is_rejected_instead_of_downgraded(self):
        with self.assertRaisesRegex(ValueError, "unsupported ChatGPT executor"):
            AccessTokenOnlyRegistrationEngine(
                email_service=mock.Mock(),
                browser_mode="automatic",
            )

    def test_v2_email_adapter_returns_none_on_mailbox_timeout_for_resend_path(self):
        email_service = mock.Mock()
        email_service.get_verification_code.side_effect = TimeoutError("等待 Email API 验证码超时 (30s)")
        logs = []
        adapter = EmailServiceAdapter(email_service, "buyer@example.com", logs.append)

        code = adapter.wait_for_verification_code(
            "buyer@example.com",
            timeout=30,
            phase="register_email_otp",
            phase_label="注册阶段邮箱验证码",
        )

        self.assertIsNone(code)
        self.assertEqual(email_service.get_verification_code.call_count, 1)
        self.assertTrue(any("等待超时" in line for line in logs))

    def test_v2_registration_passes_configured_email_otp_timeouts(self):
        email_service = mock.Mock()
        email_service.create_email.return_value = {"email": "buyer@example.com"}
        engine = AccessTokenOnlyRegistrationEngine(
            email_service=email_service,
            proxy_url="http://proxy.local:8080",
            extra_config={
                "chatgpt_register_otp_wait_seconds": 45,
                "chatgpt_register_otp_resend_wait_seconds": 35,
            },
        )
        self._TrackingChatGPTClient.last_register_kwargs = {}

        with (
            mock.patch.object(engine, "_probe_homepage_before_email_creation", return_value=(True, "")),
            mock.patch.object(engine, "_report_homepage_probe"),
            mock.patch.object(engine, "_probe_plus_checkout_billing", return_value={}),
            mock.patch(
                "services.chatgpt_core.access_token_only_registration_engine.ChatGPTClient",
                self._TrackingChatGPTClient,
            ),
        ):
            result = engine.run()

        self.assertTrue(result.success)
        self.assertEqual(self._TrackingChatGPTClient.last_register_kwargs["otp_wait_timeout"], 45)
        self.assertEqual(self._TrackingChatGPTClient.last_register_kwargs["otp_resend_wait_timeout"], 35)
        self.assertEqual(self._TrackingChatGPTClient.last_register_kwargs["otp_account_budget_timeout"], 80)
        self.assertTrue(self._TrackingChatGPTClient.last_register_kwargs["allow_existing_account_login_route"])

    def test_v2_registration_uses_single_account_otp_defaults(self):
        email_service = mock.Mock()
        email_service.create_email.return_value = {"email": "buyer@example.com"}
        engine = AccessTokenOnlyRegistrationEngine(
            email_service=email_service,
            proxy_url="http://proxy.local:8080",
            extra_config={},
        )
        self._TrackingChatGPTClient.last_register_kwargs = {}

        with (
            mock.patch.object(engine, "_probe_homepage_before_email_creation", return_value=(True, "")),
            mock.patch.object(engine, "_report_homepage_probe"),
            mock.patch.object(engine, "_probe_plus_checkout_billing", return_value={}),
            mock.patch(
                "services.chatgpt_core.access_token_only_registration_engine.ChatGPTClient",
                self._TrackingChatGPTClient,
            ),
        ):
            result = engine.run()

        self.assertTrue(result.success)
        self.assertEqual(self._TrackingChatGPTClient.last_register_kwargs["otp_wait_timeout"], 120)
        self.assertEqual(self._TrackingChatGPTClient.last_register_kwargs["otp_resend_wait_timeout"], 90)
        self.assertEqual(self._TrackingChatGPTClient.last_register_kwargs["otp_account_budget_timeout"], 210)
        self.assertTrue(self._TrackingChatGPTClient.last_register_kwargs["allow_existing_account_login_route"])

    def test_v2_registration_skips_existing_route_when_disabled(self):
        email_service = mock.Mock()
        email_service.create_email.return_value = {"email": "buyer@example.com"}
        engine = AccessTokenOnlyRegistrationEngine(
            email_service=email_service,
            proxy_url="http://proxy.local:8080",
            extra_config={"chatgpt_existing_account_login_route_enabled": False},
        )
        self._ExistingRouteChatGPTClient.last_register_kwargs = {}

        with (
            mock.patch.object(engine, "_probe_homepage_before_email_creation", return_value=(True, "")),
            mock.patch.object(engine, "_report_homepage_probe"),
            mock.patch(
                "services.chatgpt_core.access_token_only_registration_engine.ChatGPTClient",
                self._ExistingRouteChatGPTClient,
            ),
        ):
            with self.assertRaises(ExistingAccountLoginRouteBlocked) as caught:
                engine.run()

        self.assertEqual(caught.exception.email, "buyer@example.com")
        self.assertTrue(caught.exception.route_event["blocked"])
        self.assertFalse(self._ExistingRouteChatGPTClient.last_register_kwargs["allow_existing_account_login_route"])

    def test_v2_registration_routes_existing_account_to_login_when_enabled(self):
        email_service = mock.Mock()
        email_service.create_email.return_value = {"email": "buyer@example.com"}
        engine = AccessTokenOnlyRegistrationEngine(
            email_service=email_service,
            proxy_url="http://proxy.local:8080",
            extra_config={},
        )
        self._ExistingRouteChatGPTClient.last_register_kwargs = {}
        oauth_client = mock.Mock()
        oauth_client.login_and_get_tokens.return_value = {
            "access_token": "at-existing",
            "session_token": "session-existing",
            "account_id": "acct-existing",
            "workspace_id": "ws-existing",
        }

        with (
            mock.patch.object(engine, "_probe_homepage_before_email_creation", return_value=(True, "")),
            mock.patch.object(engine, "_report_homepage_probe"),
            mock.patch.object(engine, "_probe_plus_checkout_billing", return_value={}),
            mock.patch(
                "services.chatgpt_core.access_token_only_registration_engine.ChatGPTClient",
                self._ExistingRouteChatGPTClient,
            ),
            mock.patch(
                "services.chatgpt_core.oauth_client.OAuthClient",
                return_value=oauth_client,
            ),
        ):
            result = engine.run()

        self.assertTrue(result.success)
        self.assertEqual(result.account_id, "acct-existing")
        self.assertTrue(result.metadata["existing_account_login_routed"])
        self.assertEqual(result.metadata["chatgpt_existing_account_login_route"]["action"], "login_recovery")
        login_kwargs = oauth_client.login_and_get_tokens.call_args.kwargs
        self.assertEqual(login_kwargs["login_source"], "access_token_only:existing_account_recovery")

    def test_already_paid_metadata_fails_registration_without_saving(self):
        email_service = mock.Mock()
        email_service.create_email.return_value = {"email": "buyer@example.com"}
        engine = AccessTokenOnlyRegistrationEngine(
            email_service=email_service,
            proxy_url="http://proxy.local:8080",
            extra_config={
                "chatgpt_access_token_only_checkout_country": "US",
                "chatgpt_access_token_only_checkout_currency": "USD",
            },
        )

        with (
            mock.patch.object(engine, "_probe_homepage_before_email_creation", return_value=(True, "")),
            mock.patch.object(engine, "_report_homepage_probe"),
            mock.patch(
                "services.chatgpt_core.access_token_only_registration_engine.ChatGPTClient",
                self._FakeChatGPTClient,
            ),
            mock.patch(
                "core.proxy_utils.iter_enabled_runtime_proxies",
                return_value=["http://proxy.local:8080"],
            ),
            mock.patch(
                "services.chatgpt_core.payment.generate_plus_link",
                side_effect=CheckoutRequestError(400, '{"detail":"User is already paid"}'),
            ),
        ):
            result = engine.run()

        self.assertFalse(result.success)
        self.assertIn("already paid", result.error_message.lower())
        self.assertTrue(result.metadata["chatgpt_payment_already_paid"])
        email_service.finalize_success.assert_not_called()
        email_service.finalize_failure.assert_called_once()

    def test_checkout_currency_follows_country_when_currency_is_blank(self):
        engine = AccessTokenOnlyRegistrationEngine(
            email_service=mock.Mock(),
            proxy_url="http://proxy.local:8080",
            extra_config={
                "chatgpt_checkout_country": "US",
                "chatgpt_checkout_currency": "",
            },
        )

        with (
            mock.patch(
                "core.proxy_utils.iter_enabled_runtime_proxies",
                return_value=["http://proxy.local:8080"],
            ),
            mock.patch(
                "services.chatgpt_core.payment.generate_plus_link",
                return_value="https://chatgpt.com/checkout/openai_llc/cs_live_123",
            ) as generate_link,
            mock.patch(
                "services.chatgpt_core.gopay_flow.probe_chatgpt_checkout_amount",
                return_value={"amount_text": "0", "amount": 0, "currency": "usd", "amount_is_zero": True},
            ) as probe_amount,
        ):
            metadata = engine._probe_plus_checkout_billing(
                {
                    "access_token": "at-demo",
                    "session_token": "session-demo",
                    "account_id": "acct-demo",
                },
                "buyer@example.com",
            )

        self.assertEqual(metadata["chatgpt_checkout_country"], "US")
        self.assertEqual(metadata["chatgpt_checkout_currency"], "USD")
        self.assertEqual(generate_link.call_args.kwargs["currency"], "USD")
        self.assertEqual(probe_amount.call_args.kwargs["currency"], "USD")

    def test_access_token_only_specific_currency_overrides_legacy_generic_currency(self):
        engine = AccessTokenOnlyRegistrationEngine(
            email_service=mock.Mock(),
            proxy_url="http://proxy.local:8080",
            extra_config={
                "currency": "IDR",
                "country": "ID",
                "chatgpt_access_token_only_checkout_country": "US",
                "chatgpt_access_token_only_checkout_currency": "USD",
            },
        )

        with (
            mock.patch(
                "core.proxy_utils.iter_enabled_runtime_proxies",
                return_value=["http://proxy.local:8080"],
            ),
            mock.patch(
                "services.chatgpt_core.payment.generate_plus_link",
                return_value="https://chatgpt.com/checkout/openai_llc/cs_live_123",
            ) as generate_link,
            mock.patch(
                "services.chatgpt_core.gopay_flow.probe_chatgpt_checkout_amount",
                return_value={"amount_text": "0", "amount": 0, "currency": "usd", "amount_is_zero": True},
            ) as probe_amount,
        ):
            metadata = engine._probe_plus_checkout_billing(
                {
                    "access_token": "at-demo",
                    "cookies": "oai-did=device",
                    "session_token": "session-demo",
                    "account_id": "acct-demo",
                },
                "buyer@example.com",
            )

        self.assertEqual(metadata["chatgpt_checkout_country"], "US")
        self.assertEqual(metadata["chatgpt_checkout_currency"], "USD")
        self.assertEqual(generate_link.call_args.kwargs["country"], "US")
        self.assertEqual(generate_link.call_args.kwargs["currency"], "USD")
        self.assertEqual(probe_amount.call_args.kwargs["country"], "US")
        self.assertEqual(probe_amount.call_args.kwargs["currency"], "USD")

    def test_checkout_already_paid_response_is_classified_as_invalid_failure(self):
        engine = AccessTokenOnlyRegistrationEngine(
            email_service=mock.Mock(),
            proxy_url="http://proxy.local:8080",
            extra_config={
                "chatgpt_access_token_only_checkout_country": "US",
                "chatgpt_access_token_only_checkout_currency": "USD",
            },
        )

        with (
            mock.patch(
                "core.proxy_utils.iter_enabled_runtime_proxies",
                return_value=["http://proxy.local:8080"],
            ),
            mock.patch(
                "services.chatgpt_core.payment.generate_plus_link",
                side_effect=CheckoutRequestError(400, '{"detail":"User is already paid"}'),
            ),
        ):
            metadata = engine._probe_plus_checkout_billing(
                {
                    "access_token": "at-demo",
                    "cookies": "oai-did=device",
                    "session_token": "session-demo",
                    "account_id": "acct-demo",
                },
                "buyer@example.com",
            )

        self.assertTrue(metadata["chatgpt_skip_save_account"])
        self.assertTrue(metadata["chatgpt_account_unavailable"])
        self.assertTrue(metadata["chatgpt_payment_already_paid"])
        self.assertTrue(metadata["chatgpt_invalid_registration_failure"])
        self.assertEqual(metadata["chatgpt_checkout_error_code"], "already_paid")
        self.assertIn("User is already paid", metadata["chatgpt_checkout_error_body"])

    def test_nonzero_checkout_amount_fails_registration_without_mailbox_success(self):
        email_service = mock.Mock()
        email_service.create_email.return_value = {"email": "buyer@example.com"}
        engine = AccessTokenOnlyRegistrationEngine(
            email_service=email_service,
            proxy_url="http://proxy.local:8080",
            extra_config={
                "chatgpt_access_token_only_checkout_country": "US",
                "chatgpt_access_token_only_checkout_currency": "USD",
            },
        )

        with (
            mock.patch.object(engine, "_probe_homepage_before_email_creation", return_value=(True, "")),
            mock.patch.object(engine, "_report_homepage_probe"),
            mock.patch(
                "services.chatgpt_core.access_token_only_registration_engine.ChatGPTClient",
                self._FakeChatGPTClient,
            ),
            mock.patch(
                "core.proxy_utils.iter_enabled_runtime_proxies",
                return_value=["http://proxy.local:8080"],
            ),
            mock.patch(
                "services.chatgpt_core.payment.generate_plus_link",
                return_value="https://chatgpt.com/checkout/openai_llc/cs_live_123",
            ),
            mock.patch(
                "services.chatgpt_core.gopay_flow.probe_chatgpt_checkout_amount",
                return_value={"amount_text": "20.00", "amount": 2000, "currency": "usd", "amount_is_zero": False},
            ),
        ):
            result = engine.run()

        self.assertFalse(result.success)
        self.assertIn("amount != 0", result.error_message)
        self.assertTrue(result.metadata["chatgpt_nonzero_checkout_amount_failure"])
        email_service.finalize_success.assert_not_called()
        email_service.finalize_failure.assert_called_once()

    def test_gopay_provider_link_is_captured_after_zero_amount_checkout(self):
        engine = AccessTokenOnlyRegistrationEngine(
            email_service=mock.Mock(),
            proxy_url="http://proxy.local:8080",
            extra_config={
                "chatgpt_access_token_only_checkout_country": "ID",
                "chatgpt_access_token_only_checkout_currency": "IDR",
                "chatgpt_access_token_only_gopay_provider_link_enabled": True,
                "chatgpt_gopay_defaults": '{"billing_country":"US","billing_name":"Michael Anderson"}',
            },
        )

        provider_snapshot = {
            "phase": "provider_link_ready",
            "checkout_url": "https://pay.openai.com/c/pay/cs_live_123#fid",
            "cs_id": "cs_live_123",
            "snap_token": "11111111-1111-1111-1111-111111111111",
            "payment_platform_url": "https://app.midtrans.com/snap/v4/redirection/11111111-1111-1111-1111-111111111111",
            "midtrans_redirect_url": "https://app.midtrans.com/snap/v4/redirection/11111111-1111-1111-1111-111111111111",
            "stripe_redirect_url": "https://pm-redirects.stripe.com/authorize/acct/test",
            "result": {"payment_method_types": ["gopay"]},
        }
        with (
            mock.patch(
                "core.proxy_utils.iter_enabled_runtime_proxies",
                return_value=["http://proxy.local:8080"],
            ),
            mock.patch(
                "services.chatgpt_core.payment.generate_plus_link",
                return_value="https://pay.openai.com/c/pay/cs_live_123#fid",
            ) as generate_link,
            mock.patch(
                "services.chatgpt_core.gopay_flow.probe_chatgpt_checkout_amount",
                return_value={"amount_text": "0", "amount": 0, "currency": "idr", "amount_is_zero": True},
            ),
            mock.patch(
                "services.chatgpt_core.gopay_flow.create_gopay_provider_link",
                return_value=provider_snapshot,
            ) as create_provider_link,
        ):
            metadata = engine._probe_plus_checkout_billing(
                {
                    "access_token": "at-demo",
                    "cookies": "oai-did=device",
                    "session_token": "session-demo",
                    "account_id": "acct-demo",
                },
                "buyer@example.com",
            )

        self.assertTrue(metadata["chatgpt_gopay_provider_link_enabled"])
        self.assertTrue(metadata["chatgpt_gopay_provider_link_ready"])
        self.assertEqual(metadata["chatgpt_gopay_provider_link"], provider_snapshot["payment_platform_url"])
        self.assertEqual(metadata["chatgpt_gopay_provider_link_cs_id"], "cs_live_123")
        self.assertNotIn("link_format", generate_link.call_args.kwargs)
        self.assertEqual(generate_link.call_args.kwargs["billing"]["country"], "ID")
        create_provider_link.assert_called_once()
        self.assertEqual(create_provider_link.call_args.kwargs["checkout_url"], "https://pay.openai.com/c/pay/cs_live_123#fid")
        self.assertEqual(create_provider_link.call_args.kwargs["billing"]["country"], "ID")
        checkout_account = create_provider_link.call_args.args[0]
        self.assertEqual(checkout_account.session_token, "session-demo")
        self.assertEqual(checkout_account.extra["session_token"], "session-demo")

    def test_gopay_provider_link_failure_does_not_fail_registration(self):
        email_service = mock.Mock()
        email_service.create_email.return_value = {"email": "buyer@example.com"}
        engine = AccessTokenOnlyRegistrationEngine(
            email_service=email_service,
            proxy_url="http://proxy.local:8080",
            extra_config={
                "chatgpt_access_token_only_checkout_country": "ID",
                "chatgpt_access_token_only_checkout_currency": "IDR",
                "chatgpt_access_token_only_gopay_provider_link_enabled": True,
            },
        )

        with (
            mock.patch.object(engine, "_probe_homepage_before_email_creation", return_value=(True, "")),
            mock.patch.object(engine, "_report_homepage_probe"),
            mock.patch(
                "services.chatgpt_core.access_token_only_registration_engine.ChatGPTClient",
                self._FakeChatGPTClient,
            ),
            mock.patch(
                "core.proxy_utils.iter_enabled_runtime_proxies",
                return_value=["http://proxy.local:8080"],
            ),
            mock.patch(
                "services.chatgpt_core.payment.generate_plus_link",
                return_value="https://pay.openai.com/c/pay/cs_live_123#fid",
            ),
            mock.patch(
                "services.chatgpt_core.gopay_flow.probe_chatgpt_checkout_amount",
                return_value={"amount_text": "0", "amount": 0, "currency": "idr", "amount_is_zero": True},
            ),
            mock.patch(
                "services.chatgpt_core.gopay_flow.create_gopay_provider_link",
                side_effect=RuntimeError("midtrans unavailable"),
            ),
        ):
            result = engine.run()

        self.assertTrue(result.success)
        self.assertFalse(result.metadata["chatgpt_gopay_provider_link_ready"])
        self.assertIn("midtrans unavailable", result.metadata["chatgpt_gopay_provider_link_error"])
        email_service.finalize_success.assert_called_once()
        email_service.finalize_failure.assert_not_called()


if __name__ == "__main__":
    unittest.main()
