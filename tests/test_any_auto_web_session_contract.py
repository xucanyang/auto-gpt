import inspect
import types
import unittest
from unittest import mock

try:
    import camoufox.sync_api  # noqa: F401
except ModuleNotFoundError:
    _CAMOUFOX_AVAILABLE = False
else:
    _CAMOUFOX_AVAILABLE = True

from core.task_runtime import SkipCurrentAttemptRequested
from services.chatgpt_core.access_token_only_registration_engine import (
    AccessTokenOnlyRegistrationEngine,
)
from services.chatgpt_core.any_auto import register as any_auto_register
from services.chatgpt_core.any_auto import transport
from services.chatgpt_core.refresh_token_registration_engine import RegistrationResult

if _CAMOUFOX_AVAILABLE:
    from services.chatgpt_core import browser_registration
    from services.chatgpt_core.any_auto import browser_register
    from services.chatgpt_core.any_auto.browser_register import ChatGPTBrowserRegister
else:
    browser_registration = None
    browser_register = None
    ChatGPTBrowserRegister = None


class AnyAutoWebSessionContractTests(unittest.TestCase):
    @unittest.skipUnless(_CAMOUFOX_AVAILABLE, "camoufox is only installed in the runtime image")
    def test_browser_transport_forwards_login_only_to_worker(self):
        raw_result = {
            "success": True,
            "access_token": "at-demo",
            "session_token": "session-demo",
            "cookies": "__Secure-next-auth.session-token=session-demo",
            "cookie_header": "__Secure-next-auth.session-token=session-demo",
        }
        with mock.patch.object(browser_register, "ChatGPTBrowserRegister") as worker_class:
            worker_class.return_value.run.return_value = raw_result
            result = transport.run_any_auto_browser_registration(
                email="user@example.com",
                password="Password123!",
                proxy_url=None,
                headless=True,
                otp_callback=lambda: "123456",
                login_only=True,
            )

        self.assertTrue(result.ok)
        self.assertTrue(worker_class.call_args.kwargs["login_only"])

    @unittest.skipUnless(_CAMOUFOX_AVAILABLE, "camoufox is only installed in the runtime image")
    def test_session_fetch_uses_absolute_chatgpt_endpoint(self):
        page = mock.Mock()
        with mock.patch.object(
            browser_registration,
            "_page_evaluate_safe",
            return_value={
                "status": 200,
                "ok": True,
                "data": {"accessToken": "at-demo"},
                "text": "{}",
            },
        ) as evaluate:
            payload = browser_registration._fetch_chatgpt_session_payload(page)

        self.assertEqual(payload["data"]["accessToken"], "at-demo")
        script = evaluate.call_args.args[1]
        self.assertIn("fetch('https://chatgpt.com/api/auth/session'", script)
        self.assertIn("credentials: 'include'", script)

    @unittest.skipUnless(_CAMOUFOX_AVAILABLE, "camoufox is only installed in the runtime image")
    def test_encoded_csrf_cookie_bypasses_broken_socks_api_request(self):
        page = mock.Mock()
        page.context.cookies.return_value = [
            {
                "name": "__Host-next-auth.csrf-token",
                "value": "csrf-cookie-half%7Csignature-half",
                "domain": "chatgpt.com",
            }
        ]
        page.context.request.get.side_effect = AssertionError(
            "authenticated SOCKS5 APIRequestContext must not be used when the cookie exists"
        )
        logs: list[str] = []

        with mock.patch.object(
            browser_registration,
            "_browser_fetch",
            return_value={"ok": False, "status": 0, "data": {}, "text": "NetworkError"},
        ):
            token = browser_registration._get_browser_csrf_token(
                page,
                log=lambda message: logs.append(str(message)),
            )

        self.assertEqual(token, "csrf-cookie-half")
        page.context.request.get.assert_not_called()
        self.assertTrue(any("csrf cookie" in item for item in logs))

    @unittest.skipUnless(_CAMOUFOX_AVAILABLE, "camoufox is only installed in the runtime image")
    def test_signin_rejects_next_auth_self_route(self):
        page = mock.Mock()
        logs: list[str] = []
        with mock.patch.object(
            browser_registration,
            "_browser_fetch",
            return_value={
                "ok": True,
                "status": 200,
                "data": {"url": "https://chatgpt.com/api/auth/signin"},
                "text": "{}",
            },
        ):
            authorize_url = browser_registration._start_browser_signin(
                page,
                "user@example.com",
                "device-demo",
                "csrf-demo",
                log=lambda message: logs.append(str(message)),
            )

        self.assertEqual(authorize_url, "")
        page.context.request.post.assert_not_called()
        self.assertTrue(any("拒绝" in item for item in logs))

    @unittest.skipUnless(_CAMOUFOX_AVAILABLE, "camoufox is only installed in the runtime image")
    def test_signup_entry_accepts_committed_page_after_navigation_timeout(self):
        page = mock.Mock()
        page.url = "https://auth.openai.com/create-account/password"
        page.goto.side_effect = RuntimeError("Page.goto: Timeout 30000ms exceeded")
        recovered = {
            "page_type": "create_account_password",
            "current_url": page.url,
        }
        logs: list[str] = []

        with mock.patch.object(
            browser_register,
            "_derive_registration_state_from_page",
            return_value=recovered,
        ):
            result = browser_register._start_browser_signup_via_page(
                page,
                "user@example.com",
                lambda message: logs.append(str(message)),
            )

        self.assertEqual(result, recovered)
        page.goto.assert_called_once_with(
            "https://platform.openai.com/login",
            wait_until="commit",
            timeout=30000,
        )
        self.assertTrue(any("页面已提交并可继续" in item for item in logs))

    @unittest.skipUnless(_CAMOUFOX_AVAILABLE, "camoufox is only installed in the runtime image")
    def test_signup_flow_does_not_authorize_fallback_after_submission_error(self):
        page = mock.Mock()
        page.evaluate.return_value = "Mozilla/5.0 Camoufox"
        with (
            mock.patch.object(browser_register, "_seed_browser_device_id"),
            mock.patch.object(
                browser_register,
                "_start_browser_signup_via_page",
                side_effect=RuntimeError("邮箱页提交后未进入密码页面"),
            ),
            mock.patch.object(
                browser_register,
                "_start_browser_signup_via_authorize",
            ) as authorize_fallback,
        ):
            with self.assertRaisesRegex(RuntimeError, "邮箱页提交后"):
                browser_register._browser_registration_flow(
                    page,
                    "user@example.com",
                    "Password123!",
                    lambda: "123456",
                    None,
                    lambda _message: None,
                )

        authorize_fallback.assert_not_called()

    @unittest.skipUnless(_CAMOUFOX_AVAILABLE, "camoufox is only installed in the runtime image")
    def test_signup_transition_accepts_business_response_before_url_changes(self):
        page = mock.Mock()
        page.url = "https://auth.openai.com/log-in"
        response = mock.Mock()
        response.status = 200
        response.url = "https://auth.openai.com/api/accounts/authorize/continue"
        response.json.return_value = {
            "page": {
                "type": "create_account_password",
                "payload": {
                    "url": "https://auth.openai.com/create-account/password"
                },
            }
        }
        response.text.return_value = ""
        observer = types.SimpleNamespace(
            business_responses=[response],
            business_failures=[],
            has_business_request=True,
        )

        with mock.patch.object(
            browser_register,
            "_derive_registration_state_from_page",
            return_value={},
        ):
            state = browser_register._wait_for_signup_entry_transition(
                page,
                lambda _message: None,
                response_observer=observer,
                input_selector='input[type="email"]',
            )

        self.assertEqual(state["page_type"], "create_account_password")
        self.assertEqual(
            state["_transition_diagnostics"]["source"],
            "business_response",
        )
        self.assertTrue(
            state["_transition_diagnostics"]["submit_business_request_seen"]
        )
        self.assertEqual(
            state["_transition_diagnostics"]["last_business_status"],
            200,
        )

    @unittest.skipUnless(_CAMOUFOX_AVAILABLE, "camoufox is only installed in the runtime image")
    def test_signup_entry_closes_response_observer_when_transition_fails(self):
        page = mock.Mock()
        page.url = "https://platform.openai.com/login"
        observer = mock.Mock()
        shared = mock.Mock()
        shared._NetworkActivityObserver.return_value = observer

        with (
            mock.patch.object(
                browser_register,
                "_shared_browser_registration",
                return_value=shared,
            ),
            mock.patch.object(
                browser_register,
                "_derive_registration_state_from_page",
                return_value={},
            ),
            mock.patch.object(
                browser_register,
                "_wait_for_any_selector",
                return_value='input[type="email"]',
            ),
            mock.patch.object(
                browser_register,
                "_fill_input_like_user",
                return_value=True,
            ),
            mock.patch.object(
                browser_register,
                "_click_first",
                return_value='button[type="submit"]',
            ),
            mock.patch.object(
                browser_register,
                "_wait_for_signup_entry_transition",
                side_effect=RuntimeError("transition failed"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "transition failed"):
                browser_register._start_browser_signup_via_page(
                    page,
                    "user@example.com",
                    lambda _message: None,
                )

        observer.close.assert_called_once_with()

    @unittest.skipUnless(_CAMOUFOX_AVAILABLE, "camoufox is only installed in the runtime image")
    def test_otp_submit_forwards_browser_context_and_marks_commit(self):
        shared = mock.Mock()
        shared._submit_otp_via_page.return_value = {
            "ok": True,
            "status": 200,
            "data": {"page": {"type": "about_you"}},
        }

        with mock.patch.object(
            browser_register,
            "_shared_browser_registration",
            return_value=shared,
        ):
            result = browser_register._submit_otp_via_page(
                mock.Mock(),
                "123456",
                lambda _message: None,
                device_id="did-demo",
                user_agent="ua-demo",
                referer="https://auth.openai.com/email-verification",
                assume_success_without_state=False,
            )

        self.assertTrue(result["otp_committed"])
        kwargs = shared._submit_otp_via_page.call_args.kwargs
        self.assertEqual(kwargs["device_id"], "did-demo")
        self.assertEqual(kwargs["user_agent"], "ua-demo")
        self.assertFalse(kwargs["assume_success_without_state"])

    @unittest.skipUnless(_CAMOUFOX_AVAILABLE, "camoufox is only installed in the runtime image")
    def test_phone_challenge_keeps_ui_otp_separate_from_email_validation(self):
        source = inspect.getsource(browser_register._do_add_phone_attempt)

        self.assertIn(
            "_submit_ui_otp_via_page(page, sms_code, log)",
            source,
        )
        self.assertNotIn(
            "_submit_otp_via_page(page, sms_code, log)",
            source,
        )

    @unittest.skipUnless(_CAMOUFOX_AVAILABLE, "camoufox is only installed in the runtime image")
    def test_committed_password_response_advances_without_resubmitting_password(self):
        page = mock.Mock()
        page.url = "https://auth.openai.com/create-account/password"
        page.evaluate.return_value = "Mozilla/5.0 Camoufox"
        page.context.cookies.return_value = []
        otp_callback = mock.Mock(return_value="123456")
        password_response = {
            "ok": False,
            "status": 200,
            "url": "https://auth.openai.com/api/accounts/user/register",
            "data": {"ok": True},
            "text": "密码注册请求已成功提交，但页面未离开旧密码页面",
            "register_committed": True,
        }
        otp_response = {
            "ok": True,
            "status": 200,
            "url": "https://auth.openai.com/api/accounts/email-otp/validate",
            "data": {
                "page": {
                    "type": "about_you",
                    "payload": {"url": "https://auth.openai.com/about-you"},
                }
            },
            "otp_committed": True,
        }
        about_response = {
            "ok": True,
            "status": 200,
            "url": "https://auth.openai.com/api/accounts/create_account",
            "data": {"continue_url": "https://chatgpt.com/", "method": "GET"},
            "signup_committed": True,
        }

        with (
            mock.patch.object(browser_register, "_seed_browser_device_id"),
            mock.patch.object(
                browser_register,
                "_start_browser_signup_via_page",
                return_value={
                    "page_type": "create_account_password",
                    "current_url": page.url,
                },
            ),
            mock.patch.object(
                browser_register,
                "_submit_password_via_page",
                return_value=password_response,
            ) as submit_password,
            mock.patch.object(
                browser_register,
                "_submit_otp_via_page",
                return_value=otp_response,
            ) as submit_otp,
            mock.patch.object(
                browser_register,
                "_submit_about_you_via_page",
                return_value=about_response,
            ) as submit_about,
            mock.patch.object(
                browser_register,
                "_derive_registration_state_from_page",
                return_value={
                    "page_type": "about_you",
                    "current_url": "https://auth.openai.com/about-you",
                },
            ),
            mock.patch.object(browser_register, "_handle_post_signup_onboarding"),
        ):
            final_state = browser_register._browser_registration_flow(
                page,
                "user@example.com",
                "Password123!",
                otp_callback,
                None,
                lambda _message: None,
                profile_name="Demo User",
                profile_birthdate="1990-01-02",
            )

        submit_password.assert_called_once_with(
            page,
            "Password123!",
            mock.ANY,
        )
        submit_otp.assert_called_once()
        submit_about.assert_called_once()
        otp_callback.assert_called_once_with()
        self.assertTrue(final_state["otp_committed"])
        self.assertTrue(final_state["signup_committed"])

    @unittest.skipUnless(_CAMOUFOX_AVAILABLE, "camoufox is only installed in the runtime image")
    def test_committed_otp_settles_about_you_without_authorize_reentry(self):
        page = mock.Mock()
        page.url = "https://auth.openai.com/email-verification"
        page.evaluate.return_value = "Mozilla/5.0 Camoufox"
        page.context.cookies.return_value = []
        otp_callback = mock.Mock(return_value="123456")
        otp_response = {
            "ok": True,
            "status": 200,
            "url": "https://auth.openai.com/api/accounts/email-otp/validate",
            "data": {
                "page": {
                    "type": "about_you",
                    "payload": {"url": "https://auth.openai.com/about-you"},
                }
            },
            "otp_committed": True,
        }
        about_response = {
            "ok": True,
            "status": 200,
            "url": "https://auth.openai.com/api/accounts/create_account",
            "data": {"continue_url": "https://chatgpt.com/", "method": "GET"},
            "signup_committed": True,
        }

        def settle_about_you(*_args, **_kwargs):
            page.url = "https://auth.openai.com/about-you"

        with (
            mock.patch.object(browser_register, "_seed_browser_device_id"),
            mock.patch.object(
                browser_register,
                "_start_browser_signup_via_page",
                return_value={
                    "page_type": "email_otp_verification",
                    "current_url": page.url,
                },
            ),
            mock.patch.object(
                browser_register,
                "_submit_otp_via_page",
                return_value=otp_response,
            ) as submit_otp,
            mock.patch.object(
                browser_register,
                "_ensure_about_you_page",
                side_effect=settle_about_you,
            ) as ensure_about_you,
            mock.patch.object(
                browser_register,
                "_start_browser_signup_via_authorize",
            ) as reenter,
            mock.patch.object(
                browser_register,
                "_submit_about_you_via_page",
                return_value=about_response,
            ) as submit_about,
            mock.patch.object(browser_register, "_handle_post_signup_onboarding"),
        ):
            final_state = browser_register._browser_registration_flow(
                page,
                "user@example.com",
                "Password123!",
                otp_callback,
                None,
                lambda _message: None,
                profile_name="Demo User",
                profile_birthdate="1990-01-02",
            )

        otp_callback.assert_called_once_with()
        submit_otp.assert_called_once()
        ensure_about_you.assert_called_once_with(
            page,
            "https://auth.openai.com/about-you",
            mock.ANY,
        )
        reenter.assert_not_called()
        submit_about.assert_called_once()
        self.assertTrue(final_state["otp_committed"])
        self.assertTrue(final_state["signup_committed"])

    @unittest.skipUnless(_CAMOUFOX_AVAILABLE, "camoufox is only installed in the runtime image")
    def test_committed_signup_navigation_timeout_returns_recoverable_state(self):
        page = mock.Mock()
        page.url = "https://auth.openai.com/about-you"
        page.evaluate.return_value = "Mozilla/5.0 Camoufox"
        page.context.cookies.return_value = []
        page.goto.side_effect = RuntimeError("Page.goto: Timeout 30000ms exceeded")
        about_response = {
            "ok": True,
            "status": 200,
            "url": "https://auth.openai.com/api/accounts/create_account",
            "data": {
                "continue_url": "https://auth.openai.com/api/oauth/oauth2/auth",
                "method": "GET",
            },
            "signup_committed": True,
        }

        with (
            mock.patch.object(browser_register, "_seed_browser_device_id"),
            mock.patch.object(
                browser_register,
                "_start_browser_signup_via_page",
                return_value={"page_type": "about_you", "current_url": page.url},
            ),
            mock.patch.object(browser_register, "_ensure_about_you_page"),
            mock.patch.object(
                browser_register,
                "_submit_about_you_via_page",
                return_value=about_response,
            ) as submit_about,
        ):
            final_state = browser_register._browser_registration_flow(
                page,
                "user@example.com",
                "Password123!",
                lambda: "123456",
                None,
                lambda _message: None,
                profile_name="Demo User",
                profile_birthdate="1990-01-02",
            )

        submit_about.assert_called_once()
        page.goto.assert_called_once_with(
            "https://auth.openai.com/api/oauth/oauth2/auth",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        self.assertEqual(final_state["page_type"], "post_signup_partial")
        self.assertTrue(final_state["signup_committed"])
        self.assertTrue(final_state["session_capture_pending"])
        self.assertEqual(
            final_state["post_signup_failure_code"],
            "post_signup_navigation_failed",
        )

    @unittest.skipUnless(_CAMOUFOX_AVAILABLE, "camoufox is only installed in the runtime image")
    def test_committed_signup_error_page_returns_auth_api_partial_state(self):
        page = mock.Mock()
        page.url = "https://auth.openai.com/about-you"
        page.evaluate.return_value = "Mozilla/5.0 Camoufox"
        page.context.cookies.return_value = []
        about_response = {
            "ok": True,
            "status": 200,
            "url": "https://auth.openai.com/api/accounts/create_account",
            "data": {
                "continue_url": "https://auth.openai.com/api/oauth/oauth2/auth",
                "method": "GET",
            },
            "signup_committed": True,
        }

        def land_on_error(*_args, **_kwargs):
            page.url = "https://auth.openai.com/error"

        with (
            mock.patch.object(browser_register, "_seed_browser_device_id"),
            mock.patch.object(
                browser_register,
                "_start_browser_signup_via_page",
                return_value={"page_type": "about_you", "current_url": page.url},
            ),
            mock.patch.object(browser_register, "_ensure_about_you_page"),
            mock.patch.object(
                browser_register,
                "_submit_about_you_via_page",
                return_value=about_response,
            ),
        ):
            page.goto.side_effect = land_on_error
            final_state = browser_register._browser_registration_flow(
                page,
                "user@example.com",
                "Password123!",
                lambda: "123456",
                None,
                lambda _message: None,
            )

        self.assertEqual(final_state["page_type"], "post_signup_partial")
        self.assertEqual(
            final_state["post_signup_failure_code"],
            "post_signup_auth_api_failure",
        )

    @unittest.skipUnless(_CAMOUFOX_AVAILABLE, "camoufox is only installed in the runtime image")
    def test_committed_signup_duplicate_response_enters_login_recovery_state(self):
        page = mock.Mock()
        page.url = "https://auth.openai.com/error"
        page.evaluate.return_value = "Mozilla/5.0 Camoufox"
        page.context.cookies.return_value = []
        about_response = {
            "ok": True,
            "status": 200,
            "url": "https://auth.openai.com/api/accounts/create_account",
            "data": {"continue_url": "https://chatgpt.com/", "method": "GET"},
            "signup_committed": True,
            "post_commit_response_status": 409,
            "post_commit_response_code": "invalid_auth_step",
        }

        with (
            mock.patch.object(browser_register, "_seed_browser_device_id"),
            mock.patch.object(
                browser_register,
                "_start_browser_signup_via_page",
                return_value={"page_type": "about_you", "current_url": page.url},
            ),
            mock.patch.object(browser_register, "_ensure_about_you_page"),
            mock.patch.object(
                browser_register,
                "_submit_about_you_via_page",
                return_value=about_response,
            ) as submit_about,
        ):
            final_state = browser_register._browser_registration_flow(
                page,
                "user@example.com",
                "Password123!",
                lambda: "123456",
                None,
                lambda _message: None,
            )

        submit_about.assert_called_once()
        page.goto.assert_not_called()
        self.assertTrue(final_state["signup_committed"])
        self.assertEqual(final_state["page_type"], "post_signup_partial")
        self.assertEqual(
            final_state["post_signup_failure_code"],
            "post_signup_duplicate_submission",
        )

    @unittest.skipUnless(_CAMOUFOX_AVAILABLE, "camoufox is only installed in the runtime image")
    def test_login_only_flow_rejects_registration_only_states(self):
        for page_type, current_url in (
            ("create_account_password", "https://auth.openai.com/create-account/password"),
            ("about_you", "https://auth.openai.com/about-you"),
        ):
            with self.subTest(page_type=page_type):
                page = mock.Mock()
                page.url = current_url
                page.evaluate.return_value = "Mozilla/5.0 Camoufox"
                page.context.cookies.return_value = []
                with (
                    mock.patch.object(browser_register, "_seed_browser_device_id"),
                    mock.patch.object(
                        browser_register,
                        "_start_browser_signup_via_authorize",
                        return_value={"page_type": page_type, "current_url": current_url},
                    ) as login_entry,
                    mock.patch.object(browser_register, "_start_browser_signup_via_page") as signup_entry,
                    mock.patch.object(browser_register, "_submit_password_via_page") as submit_password,
                    mock.patch.object(browser_register, "_submit_about_you_via_page") as submit_about_you,
                ):
                    with self.assertRaisesRegex(RuntimeError, "失效测活拒绝进入"):
                        browser_register._browser_registration_flow(
                            page,
                            "user@example.com",
                            "Password123!",
                            lambda: "123456",
                            None,
                            lambda _message: None,
                            login_only=True,
                        )

                login_entry.assert_called_once_with(
                    page,
                    "user@example.com",
                    mock.ANY,
                    mock.ANY,
                    screen_hint="login",
                )
                signup_entry.assert_not_called()
                submit_password.assert_not_called()
                submit_about_you.assert_not_called()

    @unittest.skipUnless(_CAMOUFOX_AVAILABLE, "camoufox is only installed in the runtime image")
    def test_login_only_prefers_passwordless_otp_before_stored_password(self):
        page = mock.Mock()
        page.url = "https://auth.openai.com/log-in/password"
        page.evaluate.return_value = "Mozilla/5.0 Camoufox"
        page.context.cookies.return_value = []
        password_state = {
            "page_type": "login_password",
            "current_url": page.url,
        }
        otp_state = {
            "page_type": "email_otp_verification",
            "current_url": "https://auth.openai.com/email-verification",
        }
        callback_state = {
            "page_type": "oauth_callback",
            "continue_url": "https://chatgpt.com/api/auth/callback/openai",
        }
        otp_callback = mock.Mock(return_value="123456")

        with (
            mock.patch.object(browser_register, "_seed_browser_device_id"),
            mock.patch.object(
                browser_register,
                "_start_browser_signup_via_authorize",
                return_value=password_state,
            ),
            mock.patch.object(
                browser_register,
                "_switch_login_password_to_otp",
                return_value=otp_state,
            ) as switch_to_otp,
            mock.patch.object(
                browser_register,
                "_submit_oauth_password_direct",
            ) as submit_password,
            mock.patch.object(
                browser_register,
                "_submit_otp_via_page",
                return_value={"ok": True, "status": 200, "otp_committed": True},
            ) as submit_otp,
            mock.patch.object(
                browser_register,
                "_extract_flow_state",
                return_value=callback_state,
            ),
        ):
            result = browser_register._browser_registration_flow(
                page,
                "user@example.com",
                "stale-password",
                otp_callback,
                None,
                lambda _message: None,
                login_only=True,
            )

        self.assertEqual(result["page_type"], "oauth_callback")
        switch_to_otp.assert_called_once_with(
            page,
            mock.ANY,
            context="登录测活密码页",
        )
        submit_password.assert_not_called()
        otp_callback.assert_called_once_with()
        submit_otp.assert_called_once()

    @unittest.skipUnless(_CAMOUFOX_AVAILABLE, "camoufox is only installed in the runtime image")
    def test_passwordless_switch_trusts_successful_send_otp_response_before_spa_transition(self):
        page = mock.Mock()
        page.url = "https://auth.openai.com/log-in/password"
        response = object()
        observer = types.SimpleNamespace(
            business_responses=[response],
            business_failures=[],
            close=mock.Mock(),
        )
        shared = types.SimpleNamespace(
            _NetworkActivityObserver=mock.Mock(return_value=observer),
            _browser_response_details=mock.Mock(
                return_value=(
                    200,
                    "https://auth.openai.com/api/accounts/passwordless/send-otp",
                    {},
                    "",
                )
            ),
            _browser_response_error=mock.Mock(return_value=""),
        )

        with (
            mock.patch.object(browser_register, "_shared_browser_registration", return_value=shared),
            mock.patch.object(
                browser_register,
                "_click_passwordless_login_if_available",
                return_value=True,
            ),
        ):
            state = browser_register._switch_login_password_to_otp(
                page,
                lambda _message: None,
                context="登录测活密码页",
            )

        self.assertEqual(state["page_type"], "email_otp_verification")
        self.assertEqual(state["current_url"], page.url)
        shared._NetworkActivityObserver.assert_called_once_with(
            page,
            ("/api/accounts/passwordless/send-otp",),
        )
        observer.close.assert_called_once_with()

    @unittest.skipUnless(_CAMOUFOX_AVAILABLE, "camoufox is only installed in the runtime image")
    def test_login_only_does_not_submit_password_after_passwordless_send_failure(self):
        page = mock.Mock()
        page.url = "https://auth.openai.com/log-in/password"
        page.evaluate.return_value = "Mozilla/5.0 Camoufox"
        page.context.cookies.return_value = []
        password_state = {
            "page_type": "login_password",
            "current_url": page.url,
        }

        with (
            mock.patch.object(browser_register, "_seed_browser_device_id"),
            mock.patch.object(
                browser_register,
                "_start_browser_signup_via_authorize",
                return_value=password_state,
            ),
            mock.patch.object(
                browser_register,
                "_switch_login_password_to_otp",
                side_effect=RuntimeError(
                    "passwordless_login_send_failed: HTTP 429 Too many attempts"
                ),
            ),
            mock.patch.object(
                browser_register,
                "_submit_oauth_password_direct",
            ) as submit_password,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "passwordless_login_send_failed",
            ):
                browser_register._browser_registration_flow(
                    page,
                    "user@example.com",
                    "not-an-account-password",
                    lambda: "123456",
                    None,
                    lambda _message: None,
                    login_only=True,
                )

        submit_password.assert_not_called()

    @unittest.skipUnless(_CAMOUFOX_AVAILABLE, "camoufox is only installed in the runtime image")
    def test_login_only_retries_passwordless_otp_after_stored_password_rejection(self):
        page = mock.Mock()
        page.url = "https://auth.openai.com/log-in/password"
        page.evaluate.return_value = "Mozilla/5.0 Camoufox"
        page.context.cookies.return_value = []
        password_state = {
            "page_type": "login_password",
            "current_url": page.url,
        }
        otp_state = {
            "page_type": "email_otp_verification",
            "current_url": "https://auth.openai.com/email-verification",
        }
        callback_state = {
            "page_type": "oauth_callback",
            "continue_url": "https://chatgpt.com/api/auth/callback/openai",
        }

        with (
            mock.patch.object(browser_register, "_seed_browser_device_id"),
            mock.patch.object(
                browser_register,
                "_start_browser_signup_via_authorize",
                return_value=password_state,
            ),
            mock.patch.object(
                browser_register,
                "_switch_login_password_to_otp",
                side_effect=[None, otp_state],
            ) as switch_to_otp,
            mock.patch.object(
                browser_register,
                "_submit_oauth_password_direct",
                return_value={
                    "ok": False,
                    "status": 400,
                    "text": "Incorrect email address or password",
                },
            ) as submit_password,
            mock.patch.object(
                browser_register,
                "_submit_otp_via_page",
                return_value={"ok": True, "status": 200, "otp_committed": True},
            ),
            mock.patch.object(
                browser_register,
                "_extract_flow_state",
                return_value=callback_state,
            ),
        ):
            result = browser_register._browser_registration_flow(
                page,
                "user@example.com",
                "stale-password",
                lambda: "123456",
                None,
                lambda _message: None,
                login_only=True,
            )

        self.assertEqual(result["page_type"], "oauth_callback")
        self.assertEqual(switch_to_otp.call_count, 2)
        self.assertEqual(
            [call.kwargs["context"] for call in switch_to_otp.call_args_list],
            ["登录测活密码页", "登录测活密码失败兜底"],
        )
        submit_password.assert_called_once_with(page, "stale-password", mock.ANY)

    @unittest.skipUnless(_CAMOUFOX_AVAILABLE, "camoufox is only installed in the runtime image")
    def test_login_only_add_phone_never_invokes_phone_binding(self):
        page = mock.Mock()
        page.url = "https://auth.openai.com/add-phone"
        page.evaluate.return_value = "Mozilla/5.0 Camoufox"
        page.context.cookies.return_value = []
        phone_callback = mock.Mock(return_value="+12025550123")
        add_phone_state = {
            "page_type": "add_phone",
            "current_url": page.url,
        }

        with (
            mock.patch.object(browser_register, "_seed_browser_device_id"),
            mock.patch.object(
                browser_register,
                "_start_browser_signup_via_authorize",
                return_value=add_phone_state,
            ),
            mock.patch.object(browser_register, "_handle_add_phone_challenge") as bind_phone,
        ):
            result = browser_register._browser_registration_flow(
                page,
                "user@example.com",
                "Password123!",
                lambda: "123456",
                phone_callback,
                lambda _message: None,
                login_only=True,
            )

        self.assertEqual(result, add_phone_state)
        phone_callback.assert_not_called()
        bind_phone.assert_not_called()

    @unittest.skipUnless(_CAMOUFOX_AVAILABLE, "camoufox is only installed in the runtime image")
    def test_browser_transport_reuses_context_cookies_and_skips_codex_oauth(self):
        page = mock.Mock()
        page.url = "https://auth.openai.com/oauth/callback"
        initial_cookies = [
            {"name": "oai-did", "value": "did-before", "domain": "auth.openai.com"}
        ]
        final_cookies = [
            {"name": "oai-did", "value": "did-after", "domain": "chatgpt.com"},
            {
                "name": "__Secure-next-auth.session-token",
                "value": "session-demo",
                "domain": "chatgpt.com",
            },
        ]
        page.context.cookies.side_effect = [initial_cookies, final_cookies]
        browser = mock.Mock()
        browser.new_page.return_value = page
        stop_check = mock.Mock()

        with (
            mock.patch.object(
                browser_register,
                "shared_camoufox_registration_session",
            ) as shared_session,
            mock.patch.object(
                browser_register,
                "run_with_browser_capacity",
                side_effect=lambda _operation, callback, **_kwargs: callback(),
            ) as capacity,
            mock.patch.object(
                browser_register,
                "_browser_registration_flow",
                return_value={
                    "page_type": "oauth_callback",
                    "continue_url": "https://chatgpt.com/auth/callback/openai?code=demo",
                },
            ) as signup_flow,
            mock.patch(
                "services.chatgpt_core.browser_registration._wait_for_web_session",
                return_value={
                    "accessToken": "at-demo",
                    "sessionToken": "session-demo",
                },
            ) as wait_session,
            mock.patch(
                "services.chatgpt_core.browser_registration._normalize_browser_web_session",
                return_value={
                    "access_token": "at-demo",
                    "session_token": "session-demo",
                    "cookie_header": (
                        "oai-did=did-after; "
                        "__Secure-next-auth.session-token=session-demo"
                    ),
                    "account_id": "acct-demo",
                },
            ) as normalize_session,
            mock.patch.object(
                ChatGPTBrowserRegister,
                "_retry_oauth_fresh_browser",
                side_effect=AssertionError("GPT signup transport must not start Codex OAuth"),
            ) as retry_oauth,
        ):
            shared_session.return_value.__enter__.return_value = types.SimpleNamespace(
                browser=browser,
                context=page.context,
                page=page,
                token="test-context",
            )
            worker = ChatGPTBrowserRegister(
                headless=True,
                proxy=None,
                otp_callback=lambda: "123456",
                profile_name="Fixed Profile",
                profile_birthdate="1990-01-02",
                stop_check=stop_check,
                login_only=True,
                log_fn=lambda _message: None,
            )
            result = worker.run("user@example.com", "Password123!")

        self.assertTrue(result["success"])
        self.assertEqual(result["access_token"], "at-demo")
        self.assertEqual(result["session_token"], "session-demo")
        self.assertEqual(result["cookies"], final_cookies)
        signup_kwargs = signup_flow.call_args.kwargs
        self.assertEqual(signup_kwargs["profile_name"], "Fixed Profile")
        self.assertEqual(signup_kwargs["profile_birthdate"], "1990-01-02")
        self.assertIs(signup_kwargs["stop_check"], stop_check)
        self.assertTrue(signup_kwargs["login_only"])
        self.assertIs(wait_session.call_args.kwargs["stop_check"], stop_check)
        self.assertEqual(normalize_session.call_args.args[1], final_cookies)
        self.assertEqual(
            capacity.call_args.args[0],
            "any_auto_browser_registration",
        )
        self.assertIs(capacity.call_args.kwargs["stop_check"], stop_check)
        page.goto.assert_any_call(
            "https://chatgpt.com/auth/callback/openai?code=demo",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        self.assertEqual(
            [item.args[0] for item in page.remove_listener.call_args_list],
            ["request", "response", "requestfailed"],
        )
        retry_oauth.assert_not_called()

    @unittest.skipUnless(_CAMOUFOX_AVAILABLE, "camoufox is only installed in the runtime image")
    def test_committed_signup_recovers_web_session_via_existing_account_login(self):
        page = mock.Mock()
        page.url = "https://chatgpt.com/"
        cookie_items = [
            {"name": "oai-did", "value": "did-demo", "domain": "chatgpt.com"},
            {
                "name": "__Secure-next-auth.session-token",
                "value": "session-demo",
                "domain": "chatgpt.com",
            },
        ]
        page.context.cookies.return_value = cookie_items
        browser = mock.Mock()
        browser.new_page.return_value = page
        signup_state = {
            "page_type": "chatgpt_home",
            "current_url": "https://chatgpt.com/",
            "signup_committed": True,
            "otp_committed": True,
        }
        recovered_state = {
            "page_type": "oauth_callback",
            "continue_url": "https://chatgpt.com/auth/callback/openai?code=recovered",
        }

        with (
            mock.patch.object(
                browser_register,
                "shared_camoufox_registration_session",
            ) as shared_session,
            mock.patch.object(
                browser_register,
                "run_with_browser_capacity",
                side_effect=lambda _operation, callback, **_kwargs: callback(),
            ),
            mock.patch.object(
                browser_register,
                "_browser_registration_flow",
                side_effect=[signup_state, recovered_state],
            ) as registration_flow,
            mock.patch(
                "services.chatgpt_core.browser_registration._wait_for_web_session",
                side_effect=[{}, {"accessToken": "at-demo", "sessionToken": "session-demo"}],
            ) as wait_session,
            mock.patch(
                "services.chatgpt_core.browser_registration._normalize_browser_web_session",
                side_effect=[
                    {"access_token": "", "session_token": "", "cookie_header": ""},
                    {
                        "access_token": "at-demo",
                        "session_token": "session-demo",
                        "cookie_header": "__Secure-next-auth.session-token=session-demo",
                        "account_id": "acct-demo",
                    },
                ],
            ),
        ):
            shared_session.return_value.__enter__.return_value = types.SimpleNamespace(
                browser=browser,
                context=page.context,
                page=page,
                token="test-context",
            )
            worker = ChatGPTBrowserRegister(
                headless=True,
                otp_callback=lambda: "123456",
                log_fn=lambda _message: None,
            )
            result = worker.run("user@example.com", "Password123!")

        self.assertTrue(result["success"])
        self.assertEqual(result["access_token"], "at-demo")
        self.assertEqual(registration_flow.call_count, 2)
        self.assertFalse(registration_flow.call_args_list[0].kwargs["login_only"])
        self.assertTrue(registration_flow.call_args_list[1].kwargs["login_only"])
        self.assertEqual(wait_session.call_count, 2)
        self.assertTrue(result["metadata"]["registration_otp_committed"])
        self.assertTrue(result["metadata"]["registration_signup_committed"])
        self.assertEqual(
            result["metadata"]["registration_signup_recovery"],
            "existing_account_login",
        )
        self.assertEqual(
            [item.args[0] for item in page.remove_listener.call_args_list],
            ["request", "response", "requestfailed"],
        )

    @unittest.skipUnless(_CAMOUFOX_AVAILABLE, "camoufox is only installed in the runtime image")
    def test_committed_signup_failed_login_recovery_returns_persistable_pending(self):
        page = mock.Mock()
        page.url = "https://auth.openai.com/error"
        cookie_items = [
            {"name": "oai-did", "value": "did-demo", "domain": "auth.openai.com"},
        ]
        page.context.cookies.return_value = cookie_items
        browser = mock.Mock()
        browser.new_page.return_value = page
        signup_state = {
            "page_type": "post_signup_partial",
            "current_url": page.url,
            "signup_committed": True,
            "otp_committed": True,
            "session_capture_pending": True,
            "post_signup_failure_code": "post_signup_auth_api_failure",
        }

        with (
            mock.patch.object(
                browser_register,
                "shared_camoufox_registration_session",
            ) as shared_session,
            mock.patch.object(
                browser_register,
                "run_with_browser_capacity",
                side_effect=lambda _operation, callback, **_kwargs: callback(),
            ),
            mock.patch.object(
                browser_register,
                "_browser_registration_flow",
                side_effect=[signup_state, RuntimeError("existing login failed")],
            ) as registration_flow,
            mock.patch(
                "services.chatgpt_core.browser_registration._wait_for_web_session",
                side_effect=RuntimeError("session fetch crashed"),
            ) as wait_session,
            mock.patch(
                "services.chatgpt_core.browser_registration._normalize_browser_web_session",
                return_value={
                    "access_token": "",
                    "session_token": "",
                    "cookie_header": "",
                },
            ),
        ):
            shared_session.return_value.__enter__.return_value = types.SimpleNamespace(
                browser=browser,
                context=page.context,
                page=page,
                token="test-context",
            )
            worker = ChatGPTBrowserRegister(
                headless=True,
                otp_callback=lambda: "123456",
                log_fn=lambda _message: None,
            )
            result = worker.run("user@example.com", "Password123!")

        self.assertTrue(result["success"])
        self.assertEqual(result["source"], "registered_auth_pending")
        self.assertEqual(registration_flow.call_count, 2)
        self.assertEqual(wait_session.call_args.kwargs["timeout"], 10)
        self.assertTrue(result["metadata"]["registration_signup_committed"])
        self.assertTrue(result["metadata"]["registered_auth_pending"])
        self.assertTrue(result["metadata"]["session_capture_pending"])
        self.assertEqual(
            result["metadata"]["session_capture_pending_reason"],
            "post_signup_existing_account_login_failed",
        )
        self.assertEqual(
            result["metadata"]["registration_post_signup_failure_code"],
            "post_signup_auth_api_failure",
        )

        normalized = transport._normalize_result(
            email="user@example.com",
            password="Password123!",
            payload=result,
            executor="headless",
            transport="any_auto_browser",
        )
        self.assertTrue(normalized.ok)

    def test_protocol_transport_explicitly_disables_codex_oauth(self):
        captured = {}

        class FakeEngine:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def run(self):
                return {
                    "success": True,
                    "email": "user@example.com",
                    "password": "Password123!",
                    "access_token": "at-demo",
                    "sessionToken": "session-demo",
                    "metadata": {
                        "cookie_header": [
                            {"name": "oai-did", "value": "did-demo"},
                        ]
                    },
                }

        with mock.patch(
            "services.chatgpt_core.any_auto.register.RegistrationEngine",
            FakeEngine,
        ):
            result = transport.run_any_auto_protocol_registration(
                email="user@example.com",
                password="Password123!",
                proxy_url=None,
                wait_code=lambda **_kwargs: "123456",
            )

        self.assertFalse(captured["capture_codex_oauth"])
        self.assertTrue(result.ok)
        self.assertEqual(result.session_token, "session-demo")
        self.assertIn("oai-did=did-demo", result.cookie_header)

    def test_protocol_transport_does_not_swallow_skip_control_flow(self):
        class FakeEngine:
            def __init__(self, **_kwargs):
                pass

            def run(self):
                raise SkipCurrentAttemptRequested("skip")

        with mock.patch(
            "services.chatgpt_core.any_auto.register.RegistrationEngine",
            FakeEngine,
        ):
            with self.assertRaises(SkipCurrentAttemptRequested):
                transport.run_any_auto_protocol_registration(
                    email="user@example.com",
                    password="Password123!",
                    proxy_url=None,
                    wait_code=lambda **_kwargs: "123456",
                )

    def test_failure_finalize_outcome_is_attached_to_registration_metadata(self):
        class FakeEmailService:
            _registration_failure_outcome = ""

            def finalize_failure(self, **_kwargs):
                self._registration_failure_outcome = "early_failure"

            def export_state(self):
                return {"provider": "icloud_hme"}

        email_service = FakeEmailService()
        engine = AccessTokenOnlyRegistrationEngine(
            email_service,
            browser_mode="headless",
        )
        result = RegistrationResult(
            success=False,
            email="user@example.com",
            error_message="Page.goto: Timeout 30000ms exceeded",
        )

        engine._finalize_email_service_failure(result)

        self.assertEqual(
            result.metadata["mailbox_finalize_outcome"],
            "early_failure",
        )
        self.assertEqual(result.metadata["mailbox_state"]["provider"], "icloud_hme")

    def test_normalization_requires_access_and_session_material(self):
        incomplete = transport._normalize_result(
            email="user@example.com",
            password="Password123!",
            payload={"success": True, "access_token": "at-demo"},
            executor="headless",
            transport="any_auto_browser",
        )
        self.assertFalse(incomplete.ok)
        self.assertIn("incomplete", incomplete.error_message)

        complete = transport._normalize_result(
            email="user@example.com",
            password="Password123!",
            payload={
                "success": True,
                "access_token": "at-demo",
                "sessionToken": "session-demo",
                "metadata": {
                    "cookie_header": [
                        {"name": "oai-did", "value": "did-demo"},
                    ]
                },
            },
            executor="headless",
            transport="any_auto_browser",
        )
        self.assertTrue(complete.ok)
        self.assertEqual(complete.session_token, "session-demo")
        self.assertIn("__Secure-next-auth.session-token=session-demo", complete.cookie_header)

    def test_protocol_cookie_serializer_handles_curl_cffi_cookie_container(self):
        from curl_cffi import requests

        session = requests.Session()
        session.cookies.set("oai-did", "did-demo", domain="chatgpt.com")
        session.cookies.set(
            "__Secure-next-auth.session-token",
            "session-demo",
            domain="chatgpt.com",
        )

        header = any_auto_register._cookie_header_from_session(session)

        self.assertIn("oai-did=did-demo", header)
        self.assertIn("__Secure-next-auth.session-token=session-demo", header)


if __name__ == "__main__":
    unittest.main()
