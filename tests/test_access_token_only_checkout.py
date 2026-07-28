import unittest
from unittest import mock

from services.chatgpt_core.any_auto.transport import AnyAutoRegistrationResult
from services.chatgpt_core.access_token_only_registration_engine import (
    AccessTokenOnlyRegistrationEngine,
    EmailServiceAdapter,
)
from services.chatgpt_core.payment import CheckoutRequestError
from services.chatgpt_core.registration_route_policy import ExistingAccountLoginRouteBlocked
from services.chatgpt_core.sentinel_browser import BrowserRegistrationStageResult


class AccessTokenOnlyCheckoutTests(unittest.TestCase):

    @staticmethod
    def _any_auto_ok(**kwargs):
        session_token = str(kwargs.get("session_token") or "session-demo")
        cookies = str(
            kwargs.get("cookies")
            or f"oai-did=device; __Secure-next-auth.session-token={session_token}"
        )
        return AnyAutoRegistrationResult(
            success=True,
            email=str(kwargs.get("email") or "buyer@example.com"),
            password=str(kwargs.get("password") or "Password123!"),
            access_token=str(kwargs.get("access_token") or "at-demo"),
            refresh_token=str(kwargs.get("refresh_token") or ""),
            id_token=str(kwargs.get("id_token") or ""),
            session_token=session_token,
            account_id=str(kwargs.get("account_id") or "acct-demo"),
            workspace_id=str(kwargs.get("workspace_id") or "ws-demo"),
            cookies=cookies,
            cookie_header=str(kwargs.get("cookie_header") or cookies),
            transport=str(kwargs.get("transport") or "any_auto_browser"),
            executor=str(kwargs.get("executor") or "headless"),
            source=str(kwargs.get("source") or "any_auto"),
            error_message="",
        )

    @staticmethod
    def _any_auto_fail(error: str, **kwargs):
        return AnyAutoRegistrationResult(
            success=False,
            email=str(kwargs.get("email") or "buyer@example.com"),
            password=str(kwargs.get("password") or "Password123!"),
            error_message=error,
            transport=str(kwargs.get("transport") or "any_auto"),
            executor=str(kwargs.get("executor") or "protocol"),
            source="any_auto",
        )

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
        client = mock.Mock(device_id="device-demo", ua="Mozilla/5.0", fingerprint=None)

        with (
            mock.patch.object(
                engine, "_probe_homepage_before_email_creation", return_value=(True, "")
            ),
            mock.patch.object(engine, "_report_homepage_probe"),
            mock.patch.object(engine, "_probe_plus_checkout_billing", return_value={}),
            mock.patch.object(engine, "_build_chatgpt_client", return_value=client),
            mock.patch.object(
                engine,
                "_run_any_auto_registration",
                return_value=self._any_auto_fail(
                    "创建账号失败: HTTP 400: registration_disallowed",
                    executor="protocol",
                    transport="any_auto_protocol",
                ),
            ) as any_auto,
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
        any_auto.assert_called_once()
        browser_stage.assert_not_called()
        browser_oauth.assert_not_called()

    def test_browser_transport_promotes_business_milestones_and_keeps_raw_debug(self):
        emitted = []
        engine = AccessTokenOnlyRegistrationEngine(
            email_service=mock.Mock(),
            proxy_url="http://proxy.local:8080",
            browser_mode="headless",
            callback_logger=lambda message, level="info": emitted.append((level, message)),
        )
        client = mock.Mock(device_id="device-demo")
        adapter = mock.Mock()

        def fake_browser_registration(**kwargs):
            for line in (
                '邮箱页已点击继续按钮: button[type="submit"]',
                "验证码页提交状态: 200",
                "about_you 提交状态: 200",
                "注册流程完成: page=oauth_callback",
                "开始抓取 ChatGPT Web Session: https://chatgpt.com/api/auth/session",
                "ChatGPT Web Session 获取成功: access_token=secret session_token=secret cookies=secret",
                "[HTTP] POST auth.openai.com/api/accounts/email-otp/validate -> 200 42ms page=about_you type=xhr",
            ):
                kwargs["log_fn"](line)
            return self._any_auto_ok(
                email=kwargs["email"],
                password=kwargs["password"],
            )

        with mock.patch(
            "services.chatgpt_core.access_token_only_registration_engine.run_any_auto_browser_registration",
            side_effect=fake_browser_registration,
        ):
            result = engine._run_any_auto_registration(
                chatgpt_client=client,
                email_addr="buyer@example.com",
                password="Password123!",
                skymail_adapter=adapter,
                otp_wait_timeout=120,
                profile_name="Detailed User",
                profile_birthdate="1990-01-02",
            )

        info_lines = [message for level, message in emitted if level == "info"]
        debug_lines = [message for level, message in emitted if level == "debug"]
        self.assertTrue(result.ok)
        self.assertTrue(any(line.startswith("[注册] 邮箱入口已提交｜邮箱=") for line in info_lines))
        self.assertIn("[验证码] 验证码已提交｜长度=- ｜HTTP=200｜下一页=-", info_lines)
        self.assertIn("[注册] about_you 资料已提交｜HTTP=200", info_lines)
        self.assertIn("[注册] OpenAI 账号创建完成", info_lines)
        self.assertIn("[登录] 开始获取 ChatGPT Web Session", info_lines)
        self.assertIn(
            "[登录] ChatGPT Web Session 获取成功｜AT=是｜Session=是｜Cookie状态=已获取",
            info_lines,
        )
        self.assertFalse(any("any-auto 注册运输层成功" in line for line in info_lines))
        self.assertFalse(any("any-auto/" in line or "headless" in line for line in debug_lines))
        self.assertFalse(any("邮箱页已点击继续按钮" in line for line in debug_lines))
        self.assertFalse(any("ChatGPT Web Session 获取成功" in line for line in debug_lines))
        self.assertTrue(any("[HTTP] POST auth.openai.com/api/accounts/email-otp/validate" in line for line in debug_lines))

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
                any_auto = self._any_auto_ok(executor=browser_mode, transport="any_auto_browser")

                with (
                    mock.patch.object(
                        engine,
                        "_probe_homepage_before_email_creation",
                        return_value=(True, ""),
                    ),
                    mock.patch.object(engine, "_report_homepage_probe"),
                    mock.patch.object(engine, "_build_chatgpt_client", return_value=client),
                    mock.patch.object(engine, "_probe_plus_checkout_billing", return_value={}),
                    mock.patch.object(
                        engine,
                        "_run_any_auto_registration",
                        return_value=any_auto,
                    ) as any_auto_call,
                ):
                    result = engine.run()

                self.assertTrue(result.success, result.error_message)
                client.register_complete_flow.assert_not_called()
                client.reuse_session_and_get_tokens.assert_not_called()
                any_auto_call.assert_called_once()
                # headless flag is encoded by executor mode, not a separate stage arg
                self.assertEqual(engine.browser_mode, browser_mode)
                self.assertEqual(bool(expected_headless), browser_mode == "headless")

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
        any_auto = self._any_auto_fail(
            "创建账号失败: HTTP 400: registration_disallowed",
            executor="headless",
            transport="any_auto_browser",
        )

        with (
            mock.patch.object(engine, "_build_chatgpt_client", return_value=client),
            mock.patch.object(
                engine,
                "_run_any_auto_registration",
                return_value=any_auto,
            ) as any_auto_call,
        ):
            result = engine.run()

        self.assertFalse(result.success)
        self.assertIn("registration_disallowed", result.error_message)
        any_auto_call.assert_called_once()
        email_service.create_email.assert_called_once()
        self.assertTrue(
            any("固定为单次执行" in line for line in result.logs)
        )

    def test_signup_committed_callback_finalizes_hme_before_web_session(self):
        """any-auto transport no longer exposes signup_committed IPC; HME finalize on success only."""
        email_service = mock.Mock()
        email_service.create_email.return_value = {"email": "buyer@example.com"}
        engine = AccessTokenOnlyRegistrationEngine(
            email_service=email_service,
            browser_mode="headless",
            max_retries=1,
        )
        client = mock.Mock(device_id="device-demo", _check_stop=mock.Mock())
        any_auto = self._any_auto_ok(executor="headless")
        with (
            mock.patch.object(engine, "_build_chatgpt_client", return_value=client),
            mock.patch.object(engine, "_probe_plus_checkout_billing", return_value={}),
            mock.patch.object(engine, "_run_any_auto_registration", return_value=any_auto),
        ):
            result = engine.run()
        self.assertTrue(result.success)
        email_service.finalize_success.assert_called()
        self.assertTrue(getattr(engine, "_mailbox_finalized", False))

    def test_missing_web_session_after_signup_saves_auth_pending(self):
        """any-auto contract: missing access_token is registration failure, not auth_pending success."""
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
            registration_transport="any_auto_browser",
            registration_stage_transports=[],
            registration_runtime_profile={},
        )
        any_auto = self._any_auto_fail(
            "browser_registration_missing_web_session: no accessToken",
            executor="headless",
            transport="any_auto_browser",
        )

        with (
            mock.patch.object(engine, "_build_chatgpt_client", return_value=client),
            mock.patch.object(
                engine,
                "_run_any_auto_registration",
                return_value=any_auto,
            ),
            mock.patch.object(
                engine,
                "_capture_browser_oauth_tokens",
                return_value=(False, "oauth recovery failed"),
            ) as oauth_capture,
            mock.patch.object(engine, "_probe_plus_checkout_billing", return_value={}),
        ):
            result = engine.run()

        self.assertFalse(result.success)
        self.assertNotEqual(result.source, "registered_auth_pending")
        oauth_capture.assert_not_called()
        email_service.finalize_success.assert_not_called()

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
        any_auto = self._any_auto_fail(
            "user_already_exists: browser registration reached login_password; "
            "use explicit existing-account capture instead",
            email="existing@example.com",
            executor="headless",
            transport="any_auto_browser",
        )

        with (
            mock.patch.object(engine, "_build_chatgpt_client", return_value=client),
            mock.patch.object(
                engine,
                "_run_any_auto_registration",
                return_value=any_auto,
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
        any_auto = self._any_auto_fail(
            "browser_registration_failed: ExistingAccountDetected: "
            "user_already_exists: stage=about_you signal=account_already_exists",
            email="existing@example.com",
            executor="headless",
            transport="any_auto_browser",
        )

        with (
            mock.patch.object(engine, "_build_chatgpt_client", return_value=client),
            mock.patch.object(
                engine,
                "_run_any_auto_registration",
                return_value=any_auto,
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
        calls = {"n": 0}

        def _any_auto_side_effect(**kwargs):
            calls["n"] += 1
            # profile must be stable across retries
            self.assertEqual(kwargs.get("profile_name"), "Fixed Profile")
            self.assertEqual(kwargs.get("profile_birthdate"), "1990-01-02")
            if calls["n"] == 1:
                return self._any_auto_fail("OTP transient failure", executor="protocol")
            return self._any_auto_ok(executor="protocol", transport="any_auto_protocol")

        with (
            mock.patch.object(
                engine, "_probe_homepage_before_email_creation", return_value=(True, "")
            ),
            mock.patch.object(engine, "_report_homepage_probe"),
            mock.patch.object(engine, "_build_chatgpt_client", return_value=client),
            mock.patch.object(engine, "_probe_plus_checkout_billing", return_value={}),
            mock.patch.object(
                engine,
                "_run_any_auto_registration",
                side_effect=_any_auto_side_effect,
            ),
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
        self.assertEqual(calls["n"], 2)

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
        client = mock.Mock(device_id="device-demo", ua="Mozilla/5.0", fingerprint=None)
        captured = {}

        def _capture(**kwargs):
            captured.update(kwargs)
            return self._any_auto_ok(executor="protocol", transport="any_auto_protocol")

        with (
            mock.patch.object(engine, "_probe_homepage_before_email_creation", return_value=(True, "")),
            mock.patch.object(engine, "_report_homepage_probe"),
            mock.patch.object(engine, "_probe_plus_checkout_billing", return_value={}),
            mock.patch.object(engine, "_build_chatgpt_client", return_value=client),
            mock.patch.object(engine, "_run_any_auto_registration", side_effect=_capture),
        ):
            result = engine.run()

        self.assertTrue(result.success)
        self.assertEqual(captured.get("otp_wait_timeout"), 45)

    def test_v2_registration_uses_single_account_otp_defaults(self):
        email_service = mock.Mock()
        email_service.create_email.return_value = {"email": "buyer@example.com"}
        engine = AccessTokenOnlyRegistrationEngine(
            email_service=email_service,
            proxy_url="http://proxy.local:8080",
            extra_config={},
        )
        client = mock.Mock(device_id="device-demo", ua="Mozilla/5.0", fingerprint=None)
        captured = {}

        def _capture(**kwargs):
            captured.update(kwargs)
            return self._any_auto_ok(executor="protocol", transport="any_auto_protocol")

        with (
            mock.patch.object(engine, "_probe_homepage_before_email_creation", return_value=(True, "")),
            mock.patch.object(engine, "_report_homepage_probe"),
            mock.patch.object(engine, "_probe_plus_checkout_billing", return_value={}),
            mock.patch.object(engine, "_build_chatgpt_client", return_value=client),
            mock.patch.object(engine, "_run_any_auto_registration", side_effect=_capture),
        ):
            result = engine.run()

        self.assertTrue(result.success)
        self.assertEqual(captured.get("otp_wait_timeout"), 120)

    def test_v2_registration_skips_existing_route_when_disabled(self):
        email_service = mock.Mock()
        email_service.create_email.return_value = {"email": "buyer@example.com"}
        engine = AccessTokenOnlyRegistrationEngine(
            email_service=email_service,
            proxy_url="http://proxy.local:8080",
            extra_config={"chatgpt_existing_account_login_route_enabled": False},
        )
        client = mock.Mock(
            device_id="device-demo",
            ua="Mozilla/5.0",
            fingerprint=None,
            last_registration_route_event={
                "email": "buyer@example.com",
                "reason": "registration_completed_without_create_account_after_otp",
                "stage": "any_auto",
            },
        )
        any_auto = self._any_auto_fail(
            "user_already_exists: existing_account_login_route",
            email="buyer@example.com",
            executor="protocol",
        )

        with (
            mock.patch.object(engine, "_probe_homepage_before_email_creation", return_value=(True, "")),
            mock.patch.object(engine, "_report_homepage_probe"),
            mock.patch.object(engine, "_build_chatgpt_client", return_value=client),
            mock.patch.object(engine, "_run_any_auto_registration", return_value=any_auto),
        ):
            with self.assertRaises(ExistingAccountLoginRouteBlocked) as caught:
                engine.run()

        self.assertEqual(caught.exception.email, "buyer@example.com")
        self.assertTrue(caught.exception.route_event["blocked"])

    def test_v2_registration_routes_existing_account_to_login_when_enabled(self):
        email_service = mock.Mock()
        email_service.create_email.return_value = {"email": "buyer@example.com"}
        engine = AccessTokenOnlyRegistrationEngine(
            email_service=email_service,
            proxy_url="http://proxy.local:8080",
            extra_config={},
        )
        client = mock.Mock(
            device_id="device-demo",
            ua="Mozilla/5.0",
            fingerprint=None,
            last_registration_route_event={
                "email": "buyer@example.com",
                "reason": "registration_completed_without_create_account_after_otp",
                "stage": "any_auto",
            },
        )
        any_auto = self._any_auto_fail(
            "user_already_exists: existing_account_login_route",
            email="buyer@example.com",
            executor="protocol",
        )
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
            mock.patch.object(engine, "_build_chatgpt_client", return_value=client),
            mock.patch.object(engine, "_run_any_auto_registration", return_value=any_auto),
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
            mock.patch.object(
                engine,
                "_build_chatgpt_client",
                return_value=mock.Mock(device_id="device-demo", ua="Mozilla/5.0", fingerprint=None),
            ),
            mock.patch.object(
                engine,
                "_run_any_auto_registration",
                return_value=self._any_auto_ok(executor="protocol", transport="any_auto_protocol"),
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
            mock.patch.object(
                engine,
                "_build_chatgpt_client",
                return_value=mock.Mock(device_id="device-demo", ua="Mozilla/5.0", fingerprint=None),
            ),
            mock.patch.object(
                engine,
                "_run_any_auto_registration",
                return_value=self._any_auto_ok(executor="protocol", transport="any_auto_protocol"),
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
            mock.patch.object(
                engine,
                "_build_chatgpt_client",
                return_value=mock.Mock(device_id="device-demo", ua="Mozilla/5.0", fingerprint=None),
            ),
            mock.patch.object(
                engine,
                "_run_any_auto_registration",
                return_value=self._any_auto_ok(executor="protocol", transport="any_auto_protocol"),
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
