import unittest
from unittest import mock

try:
    import camoufox.sync_api  # noqa: F401
except ModuleNotFoundError:
    _CAMOUFOX_AVAILABLE = False
else:
    _CAMOUFOX_AVAILABLE = True

from core.task_runtime import SkipCurrentAttemptRequested
from services.chatgpt_core.any_auto import register as any_auto_register
from services.chatgpt_core.any_auto import transport

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
            mock.patch.object(browser_register, "Camoufox") as camoufox,
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
            camoufox.return_value.__enter__.return_value = browser
            worker = ChatGPTBrowserRegister(
                headless=True,
                proxy=None,
                otp_callback=lambda: "123456",
                profile_name="Fixed Profile",
                profile_birthdate="1990-01-02",
                stop_check=stop_check,
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
        self.assertIs(wait_session.call_args.kwargs["stop_check"], stop_check)
        self.assertEqual(normalize_session.call_args.args[1], final_cookies)
        page.goto.assert_any_call(
            "https://chatgpt.com/auth/callback/openai?code=demo",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        retry_oauth.assert_not_called()

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
