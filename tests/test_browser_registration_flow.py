import unittest
from unittest import mock

try:
    from services.chatgpt_core import browser_registration as br
except ModuleNotFoundError as exc:
    if exc.name == "camoufox":
        raise unittest.SkipTest("camoufox is only installed in the runtime image") from exc
    raise

from services.chatgpt_core.access_token_only_registration_engine import EmailServiceAdapter
from services.chatgpt_core.access_token_only_registration_engine import AccessTokenOnlyRegistrationEngine
from services.chatgpt_core.sentinel_browser import (
    BrowserOAuthTokenRecoveryResult,
    BrowserRegistrationStageResult,
)
from services.chatgpt_core.utils import FlowState


class _FakeLocator:
    def __init__(self, *, input_value="", count=1, text=""):
        self._value = input_value
        self._count = count
        self._text = text
        self.first = self

    def count(self):
        return self._count

    def wait_for(self, **_kwargs):
        return None

    def is_visible(self, **_kwargs):
        return self._count > 0

    def click(self, **_kwargs):
        return None

    def fill(self, value):
        self._value = str(value)

    def type(self, value, **_kwargs):
        self._value += str(value)

    def input_value(self):
        return self._value

    def text_content(self, **_kwargs):
        if not self._text:
            raise RuntimeError("no text")
        return self._text


class _FakeResponse:
    def __init__(self, status, data=None, text=""):
        self.status = status
        self.url = "https://auth.openai.com/api/accounts/email-otp/validate"
        self._data = data
        self._text = text

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data

    def text(self):
        return self._text or ""


class _FakePage:
    url = "https://auth.openai.com/email-verification"

    def __init__(self, response=None):
        self.response = response
        self.listener = None

    def wait_for_load_state(self, **_kwargs):
        return None

    def wait_for_timeout(self, _value):
        return None

    def locator(self, selector):
        if selector.startswith("input[inputmode"):
            return _FakeLocator(count=0)
        if selector.startswith("text=") or "error" in selector.lower() or "alert" in selector.lower():
            return _FakeLocator(count=0)
        return _FakeLocator()

    def get_by_label(self, _pattern):
        return _FakeLocator()

    def get_by_role(self, _role, **_kwargs):
        return _FakeLocator()

    def query_selector(self, selector):
        return object() if selector.startswith("button") else None

    def click(self, _selector):
        if self.listener is not None and self.response is not None:
            self.listener(self.response)

    def on(self, _event, listener):
        self.listener = listener

    def remove_listener(self, _event, listener):
        if self.listener is listener:
            self.listener = None


class BrowserRegistrationFlowTests(unittest.TestCase):
    def test_otp_response_success_is_accepted_without_url_change(self):
        response = _FakeResponse(
            200,
            {"page": {"type": "about_you", "payload": {"url": "https://auth.openai.com/about-you"}}},
        )
        page = _FakePage(response)

        with mock.patch.object(br, "_browser_pause"):
            result = br._submit_otp_via_page(page, "123456", lambda _message: None)

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["data"]["page"]["type"], "about_you")

    def test_otp_api_fallback_handles_success_when_click_emits_no_navigation(self):
        page = _FakePage()
        api_result = {
            "ok": True,
            "status": 200,
            "url": page.url,
            "data": {"page": {"type": "about_you"}},
            "text": "",
        }
        clock = [0.0]

        def fake_time():
            clock[0] += 1.0
            return clock[0]

        with (
            mock.patch.object(br, "_browser_pause"),
            mock.patch.object(br, "_validate_browser_email_otp", return_value=api_result) as validate,
            mock.patch.object(br.time, "time", side_effect=fake_time),
            mock.patch.object(br.time, "sleep"),
        ):
            result = br._submit_otp_via_page(page, "123456", lambda _message: None)

        self.assertTrue(result["ok"])
        validate.assert_called_once()
        self.assertEqual(result["data"]["page"]["type"], "about_you")

    def test_otp_api_error_keeps_status_and_server_message(self):
        page = _FakePage()
        api_result = {
            "ok": False,
            "status": 422,
            "url": page.url,
            "data": {"error": {"message": "The verification code is invalid."}},
            "text": "",
        }
        clock = [0.0]

        def fake_time():
            clock[0] += 1.0
            return clock[0]

        with (
            mock.patch.object(br, "_browser_pause"),
            mock.patch.object(br, "_validate_browser_email_otp", return_value=api_result),
            mock.patch.object(br.time, "time", side_effect=fake_time),
            mock.patch.object(br.time, "sleep"),
        ):
            result = br._submit_otp_via_page(page, "123456", lambda _message: None)

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 422)
        self.assertIn("invalid", result["text"].lower())

    def test_email_adapter_excludes_codes_used_by_protocol_phase(self):
        service = mock.Mock()
        service.get_verification_code.return_value = "654321"
        adapter = EmailServiceAdapter(service, "buyer@example.com", lambda _message: None)
        adapter._used_codes_by_phase["register_email_otp"] = {"123456"}

        code = adapter.wait_for_verification_code(
            "buyer@example.com",
            timeout=30,
            exclude_codes=adapter.used_codes_for_phases(
                "register_email_otp", "browser_register_email_otp"
            ),
            phase="browser_register_email_otp",
        )

        self.assertEqual(code, "654321")
        kwargs = service.get_verification_code.call_args.kwargs
        self.assertIn("123456", kwargs["exclude_codes"])

    def test_browser_fallback_carries_about_you_state_cookies_and_otp_context(self):
        email_service = mock.Mock()
        email_service.get_verification_code.return_value = "654321"
        adapter = EmailServiceAdapter(email_service, "buyer@example.com", lambda _message: None)
        adapter._used_codes_by_phase["register_email_otp"] = {"123456"}
        client = mock.Mock()
        client.device_id = "device-demo"
        client.session = mock.Mock()
        client.last_registration_state = FlowState(
            page_type="about_you",
            current_url="https://auth.openai.com/about-you",
            continue_url="https://auth.openai.com/about-you",
        )
        client._check_stop = mock.Mock()
        stage_result = BrowserRegistrationStageResult(
            final_state={
                "page_type": "oauth_callback",
                "current_url": "https://chatgpt.com/api/auth/callback/openai?code=demo",
            },
            page_url="https://chatgpt.com/api/auth/callback/openai?code=demo",
            cookies=[{"name": "login_session", "value": "demo", "domain": "auth.openai.com", "path": "/"}],
        )
        engine = AccessTokenOnlyRegistrationEngine(email_service, max_retries=1)

        with (
            mock.patch(
                "services.chatgpt_core.access_token_only_registration_engine.export_session_cookies_for_playwright",
                return_value=[{"name": "login_session", "value": "protocol", "domain": "auth.openai.com", "path": "/"}],
            ),
            mock.patch(
                "services.chatgpt_core.access_token_only_registration_engine.run_browser_registration_stage",
                return_value=stage_result,
            ) as run_stage,
            mock.patch(
                "services.chatgpt_core.access_token_only_registration_engine.merge_playwright_cookies_into_session",
                return_value=1,
            ),
        ):
            ok, _message = engine._run_browser_registration_fallback(
                chatgpt_client=client,
                email_addr="buyer@example.com",
                password="Password123!",
                skymail_adapter=adapter,
                otp_wait_timeout=30,
                otp_account_budget_timeout=60,
            )

        self.assertTrue(ok)
        kwargs = run_stage.call_args.kwargs
        self.assertEqual(kwargs["initial_state"]["page_type"], "about_you")
        self.assertEqual(kwargs["cookies"][0]["value"], "protocol")
        callback_result = kwargs["otp_callback"]({"otp_sent_at": 123.0})
        self.assertEqual(callback_result["code"], "654321")
        self.assertEqual(callback_result["otp_sent_at"], 123.0)
        self.assertIn("123456", email_service.get_verification_code.call_args.kwargs["exclude_codes"])

    def test_post_browser_add_phone_uses_isolated_oauth_recovery(self):
        email_service = mock.Mock()
        email_service.get_verification_code.return_value = "654321"
        adapter = EmailServiceAdapter(email_service, "buyer@example.com", lambda _message: None)
        adapter._used_codes_by_phase["register_email_otp"] = {"123456"}
        client = mock.Mock()
        client.device_id = "device-demo"
        client.ua = "Mozilla/5.0"
        client.sec_ch_ua = '"Chromium";v="145"'
        client.impersonate = "chrome145"
        client.fingerprint = None
        client._check_stop = mock.Mock()
        oauth_client = mock.Mock()
        oauth_client.login_and_get_tokens.return_value = None
        oauth_client.last_error = (
            "passwordless 登录后仍停留在 add_phone，未获取到 workspace / callback"
        )
        browser_tokens = BrowserOAuthTokenRecoveryResult(
            tokens={
                "access_token": "at-demo",
                "refresh_token": "rt-demo",
                "id_token": "id-demo",
            }
        )
        engine = AccessTokenOnlyRegistrationEngine(email_service, max_retries=1)

        with (
            mock.patch(
                "services.chatgpt_core.oauth_client.OAuthClient",
                return_value=oauth_client,
            ),
            mock.patch(
                "services.chatgpt_core.access_token_only_registration_engine.run_browser_oauth_token_recovery",
                return_value=browser_tokens,
            ) as browser_recovery,
        ):
            ok, result = engine._recover_tokens_after_browser_registration(
                chatgpt_client=client,
                email_addr="buyer@example.com",
                password="Password123!",
                first_name="Buyer",
                last_name="Example",
                birthdate="1990-01-01",
                skymail_adapter=adapter,
            )

        self.assertTrue(ok)
        self.assertEqual(result["access_token"], "at-demo")
        browser_kwargs = browser_recovery.call_args.kwargs
        self.assertEqual(browser_kwargs["device_id"], "device-demo")
        callback_result = browser_kwargs["otp_callback"]({"otp_sent_at": 123.0})
        self.assertEqual(callback_result, "654321")
        self.assertIn(
            "123456",
            email_service.get_verification_code.call_args.kwargs["exclude_codes"],
        )


if __name__ == "__main__":
    unittest.main()
