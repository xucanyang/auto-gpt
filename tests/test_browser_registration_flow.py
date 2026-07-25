import unittest
import types
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

    def nth(self, _index):
        return self

    def click(self, **_kwargs):
        return None

    def press(self, _key, **_kwargs):
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

    def __init__(self, response=None, request=None):
        self.response = response
        self.request = request
        self.listeners = {}

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
        if self.request is not None:
            self.emit("request", self.request)
        if self.response is not None:
            self.emit("response", self.response)

    def on(self, event, listener):
        self.listeners.setdefault(event, []).append(listener)

    def emit(self, event, value):
        for listener in list(self.listeners.get(event, [])):
            listener(value)

    def remove_listener(self, event, listener):
        listeners = self.listeners.get(event, [])
        if listener in listeners:
            listeners.remove(listener)


class _AboutYouInputLocator:
    def __init__(self, *, visible=True):
        self.visible = visible
        self.value = ""
        self.first = self

    def count(self):
        return 1 if self.visible else 0

    def wait_for(self, **_kwargs):
        if not self.visible:
            raise TimeoutError("not visible")

    def click(self, **_kwargs):
        if not self.visible:
            raise TimeoutError("not visible")

    def evaluate(self, _script, value):
        self.value = str(value)
        return True

    def dispatch_event(self, _event):
        return None

    def fill(self, value):
        self.value = str(value)

    def type(self, value, **_kwargs):
        self.value += str(value)

    def input_value(self):
        return self.value


class _AboutYouInputCollection:
    def __init__(self, inputs):
        self.inputs = list(inputs)
        self.first = self.inputs[0] if self.inputs else _AboutYouInputLocator(visible=False)

    def count(self):
        return len(self.inputs)

    def nth(self, index):
        return self.inputs[index]


class _JapaneseAgePage:
    url = "https://auth.openai.com/about-you"

    def __init__(self):
        self.name_input = _AboutYouInputLocator()
        self.age_input = _AboutYouInputLocator()
        self.empty = _AboutYouInputLocator(visible=False)
        self.visible_inputs = _AboutYouInputCollection([self.name_input, self.age_input])
        self.keyboard = mock.Mock()
        self.listeners = {}

    @staticmethod
    def _pattern_text(value):
        return str(getattr(value, "pattern", value) or "")

    def locator(self, selector):
        if selector == "input:visible:not([type='hidden']):not([disabled]):not([readonly])":
            return self.visible_inputs
        if selector == "input[inputmode='numeric']":
            return self.age_input
        return self.empty

    def get_by_label(self, pattern):
        return self.age_input if "年齢" in self._pattern_text(pattern) else self.empty

    def get_by_role(self, _role, **kwargs):
        return self.age_input if "年齢" in self._pattern_text(kwargs.get("name")) else self.empty

    def get_by_placeholder(self, pattern):
        return self.age_input if "年齢" in self._pattern_text(pattern) else self.empty

    def evaluate(self, script):
        source = str(script or "")
        recognizes_japanese_age = "年齢" in source or "\\u5e74\\u9f62" in source.lower()
        return {
            "labels": ["full name", "年齢"],
            "placeholders": [],
            "headings": [],
            "hasAge": recognizes_japanese_age,
            "hasBirthday": False,
        }

    def on(self, event, listener):
        self.listeners.setdefault(event, []).append(listener)

    def emit(self, event, value):
        for listener in list(self.listeners.get(event, [])):
            listener(value)

    def remove_listener(self, event, listener):
        listeners = self.listeners.get(event, [])
        if listener in listeners:
            listeners.remove(listener)


class BrowserRegistrationFlowTests(unittest.TestCase):
    def test_password_validation_message_is_reported(self):
        page = mock.Mock()
        page.locator.return_value.first.evaluate.return_value = (
            "Please include at least one special character."
        )

        message = br._extract_input_validation_message(page, 'input[type="password"]')

        self.assertEqual(
            message,
            "Please include at least one special character.",
        )

    def test_browser_registration_prefers_openai_page_entry(self):
        page = mock.Mock()
        page.url = "https://chatgpt.com/"
        page.evaluate.return_value = "Mozilla/5.0 Camoufox"
        page.context.cookies.return_value = []
        terminal_state = {
            "page_type": "chatgpt_home",
            "current_url": "https://chatgpt.com/",
        }

        with (
            mock.patch.object(br, "_seed_browser_device_id"),
            mock.patch.object(
                br,
                "_start_browser_signup_via_page",
                return_value=terminal_state,
            ) as page_entry,
            mock.patch.object(br, "_start_browser_signup_via_authorize") as authorize_entry,
            mock.patch.object(br, "_handle_post_signup_onboarding"),
            mock.patch.object(br, "_extract_flow_state", return_value=terminal_state),
        ):
            result = br._browser_registration_flow(
                page,
                "buyer@example.com",
                "OpenAI9_policy!",
                lambda *_args, **_kwargs: "123456",
                None,
                lambda _message: None,
            )

        self.assertEqual(result, terminal_state)
        page_entry.assert_called_once_with(page, "buyer@example.com", mock.ANY)
        authorize_entry.assert_not_called()

    def test_browser_registration_falls_back_to_authorize_entry(self):
        page = mock.Mock()
        page.url = "https://chatgpt.com/"
        page.evaluate.return_value = "Mozilla/5.0 Camoufox"
        page.context.cookies.return_value = []
        terminal_state = {
            "page_type": "chatgpt_home",
            "current_url": "https://chatgpt.com/",
        }

        with (
            mock.patch.object(br, "_seed_browser_device_id"),
            mock.patch.object(
                br,
                "_start_browser_signup_via_page",
                side_effect=br._BrowserSignupEntryUnavailable("page entry unavailable"),
            ) as page_entry,
            mock.patch.object(
                br,
                "_start_browser_signup_via_authorize",
                return_value=terminal_state,
            ) as authorize_entry,
            mock.patch.object(br, "_handle_post_signup_onboarding"),
            mock.patch.object(br, "_extract_flow_state", return_value=terminal_state),
        ):
            result = br._browser_registration_flow(
                page,
                "buyer@example.com",
                "OpenAI9_policy!",
                lambda *_args, **_kwargs: "123456",
                None,
                lambda _message: None,
                device_id="device-demo",
            )

        self.assertEqual(result, terminal_state)
        page_entry.assert_called_once_with(page, "buyer@example.com", mock.ANY)
        authorize_entry.assert_called_once_with(
            page,
            "buyer@example.com",
            "device-demo",
            mock.ANY,
        )

    def test_browser_registration_does_not_fallback_after_page_submission(self):
        page = mock.Mock()
        page.url = "https://auth.openai.com/log-in"
        page.evaluate.return_value = "Mozilla/5.0 Camoufox"

        with (
            mock.patch.object(br, "_seed_browser_device_id"),
            mock.patch.object(
                br,
                "_start_browser_signup_via_page",
                side_effect=RuntimeError("邮箱页提交后未进入密码/验证码页面"),
            ),
            mock.patch.object(br, "_start_browser_signup_via_authorize") as authorize_entry,
        ):
            with self.assertRaisesRegex(RuntimeError, "邮箱页提交后未进入"):
                br._browser_registration_flow(
                    page,
                    "buyer@example.com",
                    "OpenAI9_policy!",
                    lambda *_args, **_kwargs: "123456",
                    None,
                    lambda _message: None,
                )

        authorize_entry.assert_not_called()

    def test_signup_transition_clicks_passwordless_only_once(self):
        page = mock.Mock()
        transition_state = {"page_type": "", "current_url": str(page.url or "")}
        otp_state = {
            "page_type": "email_otp_verification",
            "current_url": "https://auth.openai.com/email-verification",
        }

        with (
            mock.patch.object(
                br,
                "_click_passwordless_login_if_available",
                return_value=True,
            ) as click_passwordless,
            mock.patch.object(
                br,
                "_derive_registration_state_from_page",
                side_effect=[transition_state, otp_state],
            ),
            mock.patch.object(br.time, "time", return_value=100.0),
            mock.patch.object(br.time, "sleep"),
        ):
            result = br._wait_for_signup_entry_transition(
                page,
                lambda _message: None,
                timeout=1,
            )

        self.assertEqual(result["page_type"], "email_otp_verification")
        self.assertTrue(result["_page_otp_triggered"])
        self.assertEqual(result["_otp_sent_at"], 92.0)
        click_passwordless.assert_called_once_with(
            page,
            mock.ANY,
            context="邮箱页提交后",
        )

    def test_signup_transition_never_clicks_passwordless_on_login_password(self):
        page = mock.Mock()
        login_state = {
            "page_type": "login_password",
            "current_url": "https://auth.openai.com/log-in/password",
        }

        with (
            mock.patch.object(
                br,
                "_derive_registration_state_from_page",
                return_value=login_state,
            ),
            mock.patch.object(br, "_recover_signup_password_page", return_value=False),
            mock.patch.object(br, "_click_passwordless_login_if_available") as passwordless,
        ):
            result = br._wait_for_signup_entry_transition(
                page,
                lambda _message: None,
                timeout=1,
            )

        self.assertEqual(result, login_state)
        passwordless.assert_not_called()

    def test_hidden_password_input_does_not_override_visible_otp(self):
        page = mock.Mock()
        page.url = "https://auth.openai.com/create-account/password"
        page.evaluate.return_value = False

        hidden = mock.Mock()
        hidden.count.return_value = 1
        hidden.nth.return_value.is_visible.return_value = False
        visible = mock.Mock()
        visible.count.return_value = 1
        visible.nth.return_value.is_visible.return_value = True
        empty = mock.Mock()
        empty.count.return_value = 0

        def locator(selector):
            if selector in br.PASSWORD_INPUT_SELECTORS:
                return hidden
            if selector in br.OTP_INPUT_SELECTORS:
                return visible
            return empty

        page.locator.side_effect = locator

        state = br._derive_registration_state_from_page(page)

        self.assertEqual(state["page_type"], "email_otp_verification")

    def test_password_submit_uses_success_response_when_url_does_not_change(self):
        page = mock.Mock()
        page.url = "https://auth.openai.com/create-account/password"
        response = mock.Mock()
        response.url = "https://auth.openai.com/api/accounts/user/register"
        response.status = 200
        response.json.return_value = {
            "page": {
                "type": "email_otp_verification",
                "payload": {"url": "https://auth.openai.com/email-verification"},
            }
        }
        response.text.return_value = ""
        observer = types.SimpleNamespace(
            business_requests=[],
            business_responses=[response],
            business_failures=[],
            sentinel_requests=[],
            sentinel_responses=[],
            sentinel_failures=[],
            has_business_request=True,
            sentinel_pending=False,
        )
        submission = mock.Mock()
        submission.observer = observer
        submission.started_at = 100.0

        with (
            mock.patch.object(br, "_recover_signup_password_page", return_value=False),
            mock.patch.object(br, "_wait_for_any_selector", return_value='input[type="password"]'),
            mock.patch.object(br, "_fill_input_like_user", return_value=True),
            mock.patch.object(br, "_browser_pause"),
            mock.patch.object(br, "_PasswordFormSubmission", return_value=submission),
        ):
            result = br._submit_password_via_page(
                page,
                "OpenAI9_policy!",
                lambda _message: None,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["data"]["page"]["type"], "email_otp_verification")
        self.assertEqual(result["otp_sent_at"], 92.0)
        submission.start.assert_called_once_with()
        submission.close.assert_called_once_with()

    def test_password_submit_2xx_waits_for_stale_dom_without_resubmitting(self):
        page = mock.Mock()
        page.url = "https://auth.openai.com/create-account/password"
        response = mock.Mock()
        response.url = "https://auth.openai.com/api/accounts/user/register"
        response.status = 200
        response.json.return_value = {"ok": True}
        response.text.return_value = ""
        observer = types.SimpleNamespace(
            business_requests=[types.SimpleNamespace(url=response.url)],
            business_responses=[response],
            business_failures=[],
            sentinel_requests=[],
            sentinel_responses=[],
            sentinel_failures=[],
            has_business_request=True,
            sentinel_pending=False,
        )
        submission = mock.Mock()
        submission.observer = observer
        submission.started_at = 100.0
        clock = [0.0]

        def fake_time():
            clock[0] += 1.0
            return clock[0]

        with (
            mock.patch.object(br, "_recover_signup_password_page", return_value=False),
            mock.patch.object(br, "_wait_for_any_selector", return_value='input[type="password"]'),
            mock.patch.object(br, "_fill_input_like_user", return_value=True),
            mock.patch.object(br, "_browser_pause"),
            mock.patch.object(br, "_PasswordFormSubmission", return_value=submission),
            mock.patch.object(
                br,
                "_derive_registration_state_from_page",
                side_effect=[
                    {"page_type": "create_account_password"},
                    {
                        "page_type": "email_otp_verification",
                        "current_url": "https://auth.openai.com/email-verification",
                    },
                ],
            ),
            mock.patch.object(br.time, "time", side_effect=fake_time),
            mock.patch.object(br.time, "sleep"),
        ):
            result = br._submit_password_via_page(
                page,
                "OpenAI9_policy!",
                lambda _message: None,
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["register_committed"])
        self.assertTrue(result["otp_triggered"])
        self.assertEqual(result["data"]["page"]["type"], "email_otp_verification")
        submission.start.assert_called_once_with()
        submission.advance_if_idle.assert_not_called()
        submission.close.assert_called_once_with()

    def test_password_submit_reports_server_rejection(self):
        page = mock.Mock()
        page.url = "https://auth.openai.com/create-account/password"
        response = mock.Mock()
        response.url = "https://auth.openai.com/api/accounts/user/register"
        response.status = 400
        response.json.return_value = {
            "error": {"message": "Password does not meet requirements."}
        }
        response.text.return_value = ""
        observer = types.SimpleNamespace(
            business_requests=[],
            business_responses=[response],
            business_failures=[],
            sentinel_requests=[],
            sentinel_responses=[],
            sentinel_failures=[],
            has_business_request=True,
            sentinel_pending=False,
        )
        submission = mock.Mock()
        submission.observer = observer
        submission.started_at = 100.0

        with (
            mock.patch.object(br, "_recover_signup_password_page", return_value=False),
            mock.patch.object(br, "_wait_for_any_selector", return_value='input[type="password"]'),
            mock.patch.object(br, "_fill_input_like_user", return_value=True),
            mock.patch.object(br, "_browser_pause"),
            mock.patch.object(br, "_PasswordFormSubmission", return_value=submission),
        ):
            result = br._submit_password_via_page(
                page,
                "OpenAI9_policy!",
                lambda _message: None,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 400)
        self.assertEqual(result["text"], "Password does not meet requirements.")
        submission.start.assert_called_once_with()
        submission.close.assert_called_once_with()

    def test_password_submission_fallbacks_are_single_shot(self):
        submission = object.__new__(br._PasswordFormSubmission)
        submission.observer = types.SimpleNamespace(
            has_business_request=False,
            sentinel_pending=False,
        )
        submission.started_at = 100.0
        submission.request_submit_at = None
        submission.enter_at = None
        submission.input = mock.Mock()
        submission.log = mock.Mock()
        submission.context = "密码页"

        with mock.patch.object(submission, "_request_submit", return_value=True) as request_submit:
            submission.advance_if_idle(now=109.9)
            submission.advance_if_idle(now=110.0)
            submission.advance_if_idle(now=119.9)
            submission.advance_if_idle(now=120.0)
            submission.advance_if_idle(now=140.0)

        request_submit.assert_called_once_with()
        submission.input.press.assert_called_once_with("Enter", timeout=5000)
        self.assertEqual(submission.request_submit_at, 110.0)
        self.assertEqual(submission.enter_at, 120.0)

    def test_password_submission_unavailable_request_submit_is_not_retried(self):
        submission = object.__new__(br._PasswordFormSubmission)
        submission.observer = types.SimpleNamespace(
            has_business_request=False,
            sentinel_pending=False,
        )
        submission.started_at = 100.0
        submission.request_submit_at = None
        submission.enter_at = None
        submission.input = mock.Mock()
        submission.log = mock.Mock()
        submission.context = "密码页"

        with mock.patch.object(submission, "_request_submit", return_value=False) as request_submit:
            submission.advance_if_idle(now=110.0)
            submission.advance_if_idle(now=110.5)
            submission.advance_if_idle(now=120.0)

        request_submit.assert_called_once_with()
        submission.input.press.assert_called_once_with("Enter", timeout=5000)

    def test_password_submission_stops_fallbacks_after_business_request(self):
        submission = object.__new__(br._PasswordFormSubmission)
        submission.observer = types.SimpleNamespace(
            has_business_request=True,
            sentinel_pending=False,
        )
        submission.started_at = 100.0
        submission.request_submit_at = None
        submission.enter_at = None
        submission.input = mock.Mock()
        submission.log = mock.Mock()
        submission.context = "密码页"

        with mock.patch.object(submission, "_request_submit") as request_submit:
            submission.advance_if_idle(now=200.0)

        request_submit.assert_not_called()
        submission.input.press.assert_not_called()

    def test_password_submission_click_error_waits_before_one_request_submit(self):
        submission = object.__new__(br._PasswordFormSubmission)
        submission.page = mock.Mock()
        submission.input = mock.Mock()
        submission.submit_button = mock.Mock()
        submission.submit_button.click.side_effect = RuntimeError("detached")
        submission.observer = types.SimpleNamespace(
            has_business_request=False,
            sentinel_pending=False,
            close=mock.Mock(),
        )
        submission.started_at = 0.0
        submission.request_submit_at = None
        submission.enter_at = None
        submission.log = mock.Mock()
        submission.context = "密码页"

        with (
            mock.patch.object(submission, "_validity", return_value=(True, "")),
            mock.patch.object(submission, "_request_submit", return_value=True) as request_submit,
            mock.patch.object(br.time, "time", return_value=100.0),
        ):
            submission.start()
            request_submit.assert_not_called()
            submission.advance_if_idle(now=109.9)
            submission.advance_if_idle(now=110.0)

        request_submit.assert_called_once_with()
        self.assertEqual(submission.started_at, 100.0)
        self.assertEqual(submission.request_submit_at, 110.0)

    def test_password_submission_click_error_still_falls_back_to_one_enter(self):
        submission = object.__new__(br._PasswordFormSubmission)
        submission.page = mock.Mock()
        submission.input = mock.Mock()
        submission.submit_button = mock.Mock()
        submission.submit_button.click.side_effect = RuntimeError("detached")
        submission.observer = types.SimpleNamespace(
            has_business_request=False,
            sentinel_pending=False,
            close=mock.Mock(),
        )
        submission.started_at = 0.0
        submission.request_submit_at = None
        submission.enter_at = None
        submission.initial_click_error = ""
        submission.log = mock.Mock()
        submission.context = "密码页"

        with (
            mock.patch.object(submission, "_validity", return_value=(True, "")),
            mock.patch.object(submission, "_request_submit", return_value=False) as request_submit,
            mock.patch.object(br.time, "time", return_value=100.0),
        ):
            submission.start()
            request_submit.assert_not_called()
            submission.advance_if_idle(now=110.0)
            submission.advance_if_idle(now=110.1)
            submission.advance_if_idle(now=120.0)

        request_submit.assert_called_once_with()
        self.assertEqual(
            submission.input.press.call_args_list,
            [
                mock.call("Tab", timeout=3000),
                mock.call("Enter", timeout=5000),
            ],
        )
        self.assertEqual(submission.initial_click_error, "detached")
        self.assertEqual(submission.request_submit_at, 110.0)
        self.assertEqual(submission.enter_at, 120.0)
        submission.observer.close.assert_not_called()

    def test_oauth_email_passwordless_is_single_shot_and_preserves_cutoff(self):
        page = mock.Mock()
        page.url = "https://auth.openai.com/log-in"
        otp_state = {
            "page_type": "email_otp_verification",
            "current_url": "https://auth.openai.com/email-verification",
        }

        with (
            mock.patch.object(br, "_wait_for_any_selector", return_value='input[type="email"]'),
            mock.patch.object(br, "_fill_input_like_user", return_value=True),
            mock.patch.object(br, "_browser_pause"),
            mock.patch.object(br, "_click_first", return_value='button[type="submit"]'),
            mock.patch.object(
                br,
                "_click_passwordless_login_if_available",
                return_value=True,
            ) as passwordless,
            mock.patch.object(br, "_derive_oauth_state_from_page", return_value=otp_state),
            mock.patch.object(br.time, "time", return_value=100.0),
            mock.patch.object(br.time, "sleep"),
        ):
            result = br._submit_login_email_via_page(
                page,
                "buyer@example.com",
                lambda _message: None,
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["otp_triggered"])
        self.assertEqual(result["otp_sent_at"], 92.0)
        passwordless.assert_called_once_with(
            page,
            mock.ANY,
            context="OAuth 邮箱页提交后",
        )

    def test_oauth_password_2xx_waits_without_replaying_transaction(self):
        page = mock.Mock()
        page.url = "https://auth.openai.com/log-in/password"
        response = mock.Mock()
        response.url = "https://auth.openai.com/api/accounts/password/verify"
        response.status = 204
        response.json.side_effect = ValueError("empty")
        response.text.return_value = ""
        observer = types.SimpleNamespace(
            business_requests=[types.SimpleNamespace(url=response.url)],
            business_responses=[response],
            business_failures=[],
            sentinel_failures=[],
            sentinel_pending=False,
        )
        submission = mock.Mock()
        submission.observer = observer
        clock = [0.0]

        def fake_time():
            clock[0] += 10.0
            return clock[0]

        with (
            mock.patch.object(br, "_wait_for_any_selector", return_value='input[type="password"]'),
            mock.patch.object(br, "_fill_input_like_user", return_value=True),
            mock.patch.object(br, "_browser_pause"),
            mock.patch.object(br, "_PasswordFormSubmission", return_value=submission),
            mock.patch.object(
                br,
                "_derive_registration_state_from_page",
                return_value={"page_type": "login_password"},
            ),
            mock.patch.object(br.time, "time", side_effect=fake_time),
            mock.patch.object(br.time, "sleep"),
        ):
            result = br._submit_oauth_password_direct(
                page,
                "OpenAI9_policy!",
                lambda _message: None,
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["password_verified"])
        self.assertTrue(result["transition_pending"])
        submission.start.assert_called_once_with()
        submission.close.assert_called_once_with()

    def test_oauth_state_machine_does_not_resubmit_verified_password(self):
        page = mock.Mock()
        page.url = "https://auth.openai.com/log-in/password"
        page.evaluate.return_value = "Mozilla/5.0 Camoufox"
        oauth_start = types.SimpleNamespace(
            auth_url="https://auth.openai.com/oauth/authorize?state=state-demo",
            state="state-demo",
            code_verifier="verifier-demo",
            redirect_uri="http://localhost:1455/auth/callback",
            client_id="client-demo",
        )
        password_state = {
            "page_type": "login_password",
            "continue_url": "",
            "current_url": page.url,
        }
        consent_state = {
            "page_type": "consent",
            "continue_url": "",
            "current_url": "https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
        }
        token_result = {"access_token": "at-demo"}

        with (
            mock.patch(
                "services.chatgpt_core.oauth.generate_oauth_url",
                return_value=oauth_start,
            ),
            mock.patch.object(
                br,
                "_derive_oauth_state_from_page",
                side_effect=[password_state, password_state, consent_state],
            ),
            mock.patch.object(br, "_get_page_oauth_url", return_value=""),
            mock.patch.object(
                br,
                "_submit_oauth_password_direct",
                return_value={
                    "ok": True,
                    "status": 204,
                    "password_verified": True,
                    "transition_pending": True,
                },
            ) as submit_password,
            mock.patch.object(br, "_complete_oauth_in_browser", return_value=token_result),
            mock.patch.object(br.time, "sleep"),
        ):
            result = br._do_codex_oauth(
                page,
                {"oai-did": "device-demo"},
                "buyer@example.com",
                "OpenAI9_policy!",
                lambda *_args, **_kwargs: "123456",
                None,
                None,
                lambda _message: None,
                strict_browser=True,
            )

        self.assertEqual(result, token_result)
        submit_password.assert_called_once_with(
            page,
            "OpenAI9_policy!",
            mock.ANY,
        )

    def test_japanese_about_you_dom_overrides_stale_email_verification_url(self):
        page = mock.Mock()
        page.url = "https://auth.openai.com/email-verification"
        page.evaluate.return_value = True
        empty = mock.Mock()
        empty.count.return_value = 0
        page.locator.return_value = empty

        state = br._derive_registration_state_from_page(page)

        self.assertEqual(state["page_type"], "about_you")

    def test_registration_complete_requires_committed_add_phone_provenance(self):
        self.assertFalse(br._is_registration_complete({"page_type": "add_phone"}))
        self.assertTrue(
            br._is_registration_complete(
                {"page_type": "add_phone", "signup_committed": True}
            )
        )

    def test_page_triggered_signup_otp_is_not_sent_twice(self):
        page = mock.Mock()
        page.url = "https://chatgpt.com/"
        page.evaluate.return_value = "Mozilla/5.0 Camoufox"
        page.context.cookies.return_value = []
        callback = mock.Mock(return_value="123456")
        start_state = {
            "page_type": "email_otp_verification",
            "current_url": "https://auth.openai.com/email-verification",
            "_page_otp_triggered": True,
            "_otp_sent_at": 92.0,
        }
        otp_result = {
            "ok": True,
            "status": 200,
            "url": "https://chatgpt.com/",
            "data": None,
            "text": "",
        }

        with (
            mock.patch.object(br, "_seed_browser_device_id"),
            mock.patch.object(br, "_start_browser_signup_via_page", return_value=start_state),
            mock.patch.object(br, "_send_browser_email_otp") as send_otp,
            mock.patch.object(br, "_submit_otp_via_page", return_value=otp_result),
            mock.patch.object(br, "_handle_post_signup_onboarding"),
        ):
            result = br._browser_registration_flow(
                page,
                "buyer@example.com",
                "OpenAI9_policy!",
                callback,
                None,
                lambda _message: None,
            )

        self.assertEqual(result["page_type"], "chatgpt_home")
        send_otp.assert_not_called()
        self.assertEqual(callback.call_args.args[0]["otp_sent_at"], 92.0)

    def test_active_signup_otp_samples_cutoff_before_send(self):
        page = mock.Mock()
        page.url = "https://chatgpt.com/"
        page.evaluate.return_value = "Mozilla/5.0 Camoufox"
        page.context.cookies.return_value = []
        callback = mock.Mock(return_value="123456")
        start_state = {
            "page_type": "email_otp_verification",
            "current_url": "https://auth.openai.com/email-verification",
        }
        otp_result = {
            "ok": True,
            "status": 200,
            "url": "https://chatgpt.com/",
            "data": None,
            "text": "",
        }
        clock = [100.0]

        def send_otp(*_args, **_kwargs):
            clock[0] = 200.0
            return {"ok": True, "status": 200, "data": None, "text": ""}

        with (
            mock.patch.object(br, "_seed_browser_device_id"),
            mock.patch.object(br, "_start_browser_signup_via_page", return_value=start_state),
            mock.patch.object(br, "_send_browser_email_otp", side_effect=send_otp) as send,
            mock.patch.object(br, "_submit_otp_via_page", return_value=otp_result),
            mock.patch.object(br, "_handle_post_signup_onboarding"),
            mock.patch.object(br.time, "time", side_effect=lambda: clock[0]),
        ):
            br._browser_registration_flow(
                page,
                "buyer@example.com",
                "OpenAI9_policy!",
                callback,
                None,
                lambda _message: None,
            )

        send.assert_called_once()
        self.assertEqual(callback.call_args.args[0]["otp_sent_at"], 92.0)

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

    def test_otp_strict_mode_times_out_committed_success_without_next_state(self):
        page = _FakePage()
        api_result = {
            "ok": True,
            "status": 200,
            "url": page.url,
            "data": {},
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
            result = br._submit_otp_via_page(
                page,
                "123456",
                lambda _message: None,
                assume_success_without_state=False,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 200)
        self.assertTrue(result["otp_committed"])
        self.assertIn("未离开", result["text"])

    def test_otp_strict_mode_waits_for_delayed_state_after_2xx(self):
        response = _FakeResponse(204)
        page = _FakePage(response=response)
        original_click = page.click
        page.click = mock.Mock(side_effect=original_click)
        clock = [0.0]

        def fake_time():
            clock[0] += 1.0
            return clock[0]

        with (
            mock.patch.object(br, "_browser_pause"),
            mock.patch.object(
                br,
                "_derive_registration_state_from_page",
                side_effect=[
                    {"page_type": "email_otp_verification"},
                    {"page_type": "email_otp_verification"},
                    {
                        "page_type": "about_you",
                        "current_url": "https://auth.openai.com/about-you",
                    },
                ],
            ),
            mock.patch.object(br, "_validate_browser_email_otp") as validate,
            mock.patch.object(br.time, "time", side_effect=fake_time),
            mock.patch.object(br.time, "sleep"),
        ):
            result = br._submit_otp_via_page(
                page,
                "123456",
                lambda _message: None,
                assume_success_without_state=False,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], 204)
        self.assertTrue(result["otp_committed"])
        page.click.assert_called_once()
        validate.assert_not_called()

    def test_otp_auto_submit_request_failure_is_not_clicked_or_replayed(self):
        page = _FakePage()
        request = types.SimpleNamespace(
            url="https://auth.openai.com/api/accounts/email-otp/validate"
        )
        observer = types.SimpleNamespace(
            business_requests=[request],
            business_responses=[],
            business_failures=["connection closed"],
            has_business_request=True,
            close=mock.Mock(),
        )
        clock = [0.0]

        def fake_time():
            clock[0] += 5.0
            return clock[0]

        with (
            mock.patch.object(br, "_NetworkActivityObserver", return_value=observer),
            mock.patch.object(br, "_browser_pause"),
            mock.patch.object(br, "_click_first") as click_submit,
            mock.patch.object(
                br,
                "_derive_registration_state_from_page",
                return_value={"page_type": "email_otp_verification"},
            ),
            mock.patch.object(br, "_validate_browser_email_otp") as validate,
            mock.patch.object(br.time, "time", side_effect=fake_time),
            mock.patch.object(br.time, "sleep"),
        ):
            result = br._submit_otp_via_page(
                page,
                "123456",
                lambda _message: None,
            )

        self.assertFalse(result["ok"])
        self.assertIn("结果不确定", result["text"])
        click_submit.assert_not_called()
        validate.assert_not_called()
        observer.close.assert_called_once_with()

    def test_otp_api_fallback_waits_for_inflight_ui_request(self):
        request = types.SimpleNamespace(
            url="https://auth.openai.com/api/accounts/email-otp/validate"
        )
        page = _FakePage(request=request)
        clock = [0.0]

        def fake_time():
            clock[0] += 5.0
            return clock[0]

        with (
            mock.patch.object(br, "_browser_pause"),
            mock.patch.object(br, "_validate_browser_email_otp") as validate,
            mock.patch.object(br.time, "time", side_effect=fake_time),
            mock.patch.object(br.time, "sleep"),
        ):
            result = br._submit_otp_via_page(
                page,
                "123456",
                lambda _message: None,
            )

        self.assertFalse(result["ok"])
        validate.assert_not_called()

    def test_invoke_otp_callback_supports_contextual_and_legacy_forms(self):
        contextual = mock.Mock(return_value="123456")
        self.assertEqual(
            br._invoke_otp_callback(contextual, {"phase": "oauth_email_otp"}),
            "123456",
        )
        contextual.assert_called_once_with({"phase": "oauth_email_otp"})

        keyword_only = mock.Mock(side_effect=lambda **kwargs: kwargs["phase"])
        self.assertEqual(
            br._invoke_otp_callback(keyword_only, {"phase": "oauth_email_otp"}),
            "oauth_email_otp",
        )

    def test_strict_browser_oauth_never_uses_cookie_to_curl_fallback(self):
        page = mock.Mock()
        page.url = "https://auth.openai.com/sign-in-with-chatgpt/codex/consent"
        page.evaluate.return_value = "Mozilla/5.0 Camoufox"
        oauth_start = types.SimpleNamespace(
            auth_url="https://auth.openai.com/oauth/authorize?state=state-demo",
            state="state-demo",
            code_verifier="verifier-demo",
            redirect_uri="http://localhost:1455/auth/callback",
            client_id="client-demo",
        )

        with (
            mock.patch(
                "services.chatgpt_core.oauth.generate_oauth_url",
                return_value=oauth_start,
            ),
            mock.patch.object(
                br,
                "_derive_oauth_state_from_page",
                return_value={
                    "page_type": "consent",
                    "continue_url": page.url,
                    "current_url": page.url,
                },
            ),
            mock.patch.object(br, "_complete_oauth_in_browser", return_value=None),
            mock.patch.object(
                br,
                "_complete_oauth_with_session",
                side_effect=AssertionError("strict browser OAuth used curl fallback"),
            ) as curl_fallback,
        ):
            result = br._do_codex_oauth(
                page,
                {"oai-did": "device-demo"},
                "buyer@example.com",
                "Password123!",
                lambda *_args, **_kwargs: "123456",
                None,
                None,
                lambda _message: None,
                strict_browser=True,
            )

        self.assertIsNone(result)
        curl_fallback.assert_not_called()

    def test_japanese_age_label_is_classified_as_age(self):
        entries = [
            {"visibleIndex": 0, "labels": ["Full name"]},
            {"visibleIndex": 1, "labels": ["年齢"]},
            {"visibleIndex": 2, "labels": ["紹介コード"]},
        ]

        selected = br._pick_best_about_you_input(entries, "age")

        self.assertIs(selected, entries[1])

    def test_japanese_age_input_receives_age_instead_of_birthdate(self):
        page = _JapaneseAgePage()
        logs = []
        visible_inputs = [
            {"visibleIndex": 0, "labels": ["Full name"]},
            {"visibleIndex": 1, "labels": ["年齢"]},
        ]

        with (
            mock.patch.object(br, "_collect_visible_text_inputs", return_value=visible_inputs),
            mock.patch.object(br, "_browser_pause"),
            mock.patch.object(br, "_sync_hidden_birthday_input", return_value=True),
            mock.patch.object(br, "_click_first", return_value='button[type="submit"]'),
            mock.patch.object(
                br,
                "_derive_registration_state_from_page",
                return_value={"page_type": "oauth_callback"},
            ),
        ):
            result = br._submit_about_you_via_page(
                page,
                logs.append,
                profile_name="Demo User",
                profile_birthdate="1990-01-02",
            )

        expected_age = str(max(25, min(40, int(br.time.strftime("%Y")) - 1990)))
        self.assertTrue(result["ok"])
        self.assertEqual(page.age_input.value, expected_age)
        self.assertNotIn("1990", page.age_input.value)
        self.assertTrue(any("页面模式: age" in line for line in logs))

    def test_about_you_api_fallback_waits_for_inflight_ui_request(self):
        page = _JapaneseAgePage()
        request = types.SimpleNamespace(
            url="https://auth.openai.com/api/accounts/create_account"
        )
        visible_inputs = [
            {"visibleIndex": 0, "labels": ["Full name"]},
            {"visibleIndex": 1, "labels": ["年齢"]},
        ]
        clock = [0.0]

        def fake_time():
            clock[0] += 5.0
            return clock[0]

        def click_and_emit(*_args, **_kwargs):
            page.emit("request", request)
            return 'button[type="submit"]'

        with (
            mock.patch.object(br, "_collect_visible_text_inputs", return_value=visible_inputs),
            mock.patch.object(br, "_browser_pause"),
            mock.patch.object(br, "_sync_hidden_birthday_input", return_value=True),
            mock.patch.object(br, "_click_first", side_effect=click_and_emit),
            mock.patch.object(
                br,
                "_derive_registration_state_from_page",
                return_value={"page_type": "about_you"},
            ),
            mock.patch.object(br, "_submit_browser_about_you") as api_fallback,
            mock.patch.object(br.time, "time", side_effect=fake_time),
            mock.patch.object(br.time, "sleep"),
        ):
            result = br._submit_about_you_via_page(
                page,
                lambda _message: None,
                profile_name="Demo User",
                profile_birthdate="1990-01-02",
            )

        self.assertFalse(result["ok"])
        api_fallback.assert_not_called()
        self.assertTrue(all(not listeners for listeners in page.listeners.values()))

    def test_about_you_request_failure_is_not_replayed_with_new_invocation(self):
        page = _JapaneseAgePage()
        request = types.SimpleNamespace(
            url="https://auth.openai.com/api/accounts/create_account",
            failure="connection closed",
        )
        visible_inputs = [
            {"visibleIndex": 0, "labels": ["Full name"]},
            {"visibleIndex": 1, "labels": ["年齢"]},
        ]
        clock = [0.0]

        def fake_time():
            clock[0] += 5.0
            return clock[0]

        def click_and_fail(*_args, **_kwargs):
            page.emit("request", request)
            page.emit("requestfailed", request)
            return 'button[type="submit"]'

        with (
            mock.patch.object(br, "_collect_visible_text_inputs", return_value=visible_inputs),
            mock.patch.object(br, "_browser_pause"),
            mock.patch.object(br, "_sync_hidden_birthday_input", return_value=True),
            mock.patch.object(br, "_click_first", side_effect=click_and_fail),
            mock.patch.object(
                br,
                "_derive_registration_state_from_page",
                return_value={"page_type": "about_you"},
            ),
            mock.patch.object(br, "_submit_browser_about_you") as api_fallback,
            mock.patch.object(br.time, "time", side_effect=fake_time),
            mock.patch.object(br.time, "sleep"),
        ):
            result = br._submit_about_you_via_page(
                page,
                lambda _message: None,
                profile_name="Demo User",
                profile_birthdate="1990-01-02",
            )

        self.assertFalse(result["ok"])
        self.assertIn("connection closed", result["text"])
        api_fallback.assert_not_called()
        self.assertTrue(all(not listeners for listeners in page.listeners.values()))

    def test_about_you_2xx_waits_for_stale_dom_without_resubmitting(self):
        page = _JapaneseAgePage()
        request = types.SimpleNamespace(
            url="https://auth.openai.com/api/accounts/create_account"
        )
        response = mock.Mock()
        response.url = request.url
        response.status = 204
        response.json.side_effect = ValueError("empty")
        response.text.return_value = ""
        visible_inputs = [
            {"visibleIndex": 0, "labels": ["Full name"]},
            {"visibleIndex": 1, "labels": ["年齢"]},
        ]
        clock = [0.0]

        def fake_time():
            clock[0] += 2.0
            return clock[0]

        def click_and_emit(*_args, **_kwargs):
            page.emit("request", request)
            page.emit("response", response)
            return 'button[type="submit"]'

        with (
            mock.patch.object(br, "_collect_visible_text_inputs", return_value=visible_inputs),
            mock.patch.object(br, "_browser_pause"),
            mock.patch.object(br, "_sync_hidden_birthday_input", return_value=True),
            mock.patch.object(br, "_click_first", side_effect=click_and_emit) as submit,
            mock.patch.object(
                br,
                "_derive_registration_state_from_page",
                return_value={"page_type": "about_you"},
            ),
            mock.patch.object(br, "_submit_browser_about_you") as api_fallback,
            mock.patch.object(br.time, "time", side_effect=fake_time),
            mock.patch.object(br.time, "sleep"),
        ):
            result = br._submit_about_you_via_page(
                page,
                lambda _message: None,
                profile_name="Demo User",
                profile_birthdate="1990-01-02",
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["signup_committed"])
        self.assertTrue(result["transition_pending"])
        self.assertEqual(result["data"]["continue_url"], "https://chatgpt.com/")
        submit.assert_called_once()
        api_fallback.assert_not_called()
        self.assertTrue(all(not listeners for listeners in page.listeners.values()))

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

    def test_email_adapter_can_reserve_independent_oauth_wait_budget(self):
        service = mock.Mock()
        service.get_verification_code.return_value = "654321"
        budget = mock.Mock()
        budget.plan_wait.return_value = type(
            "Plan",
            (),
            {
                "exhausted": True,
                "timeout_seconds": 0,
                "requested_seconds": 120,
                "remaining_seconds": 0,
                "clamped": True,
            },
        )()
        adapter = EmailServiceAdapter(
            service,
            "buyer@example.com",
            lambda _message: None,
            otp_budget=budget,
        )

        code = adapter.wait_for_verification_code(
            "buyer@example.com",
            timeout=120,
            phase="browser_oauth_email_otp",
            ignore_budget=True,
        )

        self.assertEqual(code, "654321")
        budget.plan_wait.assert_not_called()
        self.assertEqual(service.get_verification_code.call_args.kwargs["timeout"], 120)

    def test_browser_direct_starts_without_protocol_state_or_cookies(self):
        email_service = mock.Mock()
        email_service.get_verification_code.return_value = "654321"
        adapter = EmailServiceAdapter(email_service, "buyer@example.com", lambda _message: None)
        adapter._used_codes_by_phase["register_email_otp"] = {"123456"}
        client = mock.Mock()
        client.device_id = "device-demo"
        client._check_stop = mock.Mock()
        stage_result = BrowserRegistrationStageResult(
            final_state={
                "page_type": "oauth_callback",
                "current_url": "https://chatgpt.com/api/auth/callback/openai?code=demo",
            },
            page_url="https://chatgpt.com/api/auth/callback/openai?code=demo",
            cookies=[{"name": "login_session", "value": "demo", "domain": "auth.openai.com", "path": "/"}],
            device_id="device-demo",
            user_agent="Mozilla/5.0 Camoufox",
            requested_executor="headless",
            effective_executor="headless",
            web_session={"access_token": "at-demo"},
        )
        engine = AccessTokenOnlyRegistrationEngine(
            email_service,
            browser_mode="headless",
            max_retries=1,
        )

        with mock.patch(
            "services.chatgpt_core.access_token_only_registration_engine.run_browser_registration_stage",
            return_value=stage_result,
        ) as run_stage:
            result = engine._run_browser_registration(
                chatgpt_client=client,
                email_addr="buyer@example.com",
                password="Password123!",
                skymail_adapter=adapter,
                otp_wait_timeout=30,
                otp_account_budget_timeout=60,
                profile_name="Buyer Example",
                profile_birthdate="1990-01-01",
            )

        self.assertTrue(result.ok)
        kwargs = run_stage.call_args.kwargs
        self.assertNotIn("page_type", kwargs["initial_state"])
        self.assertEqual(
            kwargs["initial_state"]["profile"],
            {"name": "Buyer Example", "birthdate": "1990-01-01"},
        )
        self.assertEqual(kwargs["cookies"], [])
        callback_result = kwargs["otp_callback"]({"otp_sent_at": 123.0})
        self.assertEqual(callback_result["code"], "654321")
        self.assertEqual(callback_result["otp_sent_at"], 123.0)
        self.assertIn("123456", email_service.get_verification_code.call_args.kwargs["exclude_codes"])
        self.assertEqual(client.registration_transport, "camoufox_browser")
        self.assertEqual(
            client.registration_runtime_profile["user_agent"],
            "Mozilla/5.0 Camoufox",
        )

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
        client.registration_stage_transports = []
        client.registration_runtime_profile = {}
        client.registration_transport = "camoufox_browser"
        browser_tokens = BrowserOAuthTokenRecoveryResult(
            tokens={
                "access_token": "at-demo",
                "refresh_token": "rt-demo",
                "id_token": "id-demo",
            }
        )
        engine = AccessTokenOnlyRegistrationEngine(
            email_service,
            browser_mode="headless",
            max_retries=1,
        )

        with (
            mock.patch(
                "services.chatgpt_core.oauth_client.OAuthClient",
                side_effect=AssertionError("browser executor must not use HTTP OAuthClient"),
            ) as oauth_class,
            mock.patch(
                "services.chatgpt_core.access_token_only_registration_engine.run_browser_oauth_token_recovery",
                return_value=browser_tokens,
            ) as browser_recovery,
        ):
            ok, result = engine._capture_browser_oauth_tokens(
                chatgpt_client=client,
                email_addr="buyer@example.com",
                password="Password123!",
                skymail_adapter=adapter,
            )

        self.assertTrue(ok)
        oauth_class.assert_not_called()
        self.assertEqual(result["access_token"], "at-demo")
        browser_kwargs = browser_recovery.call_args.kwargs
        self.assertEqual(browser_kwargs["device_id"], "device-demo")
        callback_result = browser_kwargs["otp_callback"]({"otp_sent_at": 123.0})
        self.assertEqual(callback_result, "654321")
        self.assertIn(
            "123456",
            email_service.get_verification_code.call_args.kwargs["exclude_codes"],
        )

    def test_protocol_executor_cannot_call_browser_registration_helper(self):
        email_service = mock.Mock()
        adapter = EmailServiceAdapter(email_service, "buyer@example.com", lambda _message: None)
        client = mock.Mock()
        client.device_id = "device-demo"
        client._check_stop = mock.Mock()
        engine = AccessTokenOnlyRegistrationEngine(
            email_service,
            browser_mode="protocol",
            max_retries=1,
        )

        with mock.patch(
            "services.chatgpt_core.access_token_only_registration_engine.run_browser_registration_stage",
        ) as browser_stage:
            result = engine._run_browser_registration(
                chatgpt_client=client,
                email_addr="buyer@example.com",
                password="Password123!",
                skymail_adapter=adapter,
                otp_wait_timeout=30,
                otp_account_budget_timeout=60,
            )

        self.assertFalse(result.ok)
        self.assertIn("forbidden", result.error)
        browser_stage.assert_not_called()

    def test_email_otp_send_is_not_page_navigation(self):
        state = {
            "method": "GET",
            "page_type": "email_otp_send",
            "continue_url": "https://auth.openai.com/api/accounts/email-otp/send",
            "current_url": "https://auth.openai.com/create-account/password",
        }
        self.assertTrue(br._is_email_otp(state))
        self.assertFalse(br._requires_registration_navigation(state))

    def test_api_continue_url_never_navigates(self):
        state = {
            "method": "GET",
            "page_type": "",
            "continue_url": "https://auth.openai.com/api/accounts/create_account",
            "current_url": "https://auth.openai.com/about-you",
        }
        self.assertFalse(br._requires_registration_navigation(state))

    def test_chatgpt_oauth_callback_external_url_must_navigate(self):
        """about_you 成功后常见 continue=chatgpt.com/api/auth/callback/openai，不可当内部 API 跳过。"""
        state = {
            "method": "GET",
            "page_type": "external_url",
            "continue_url": "https://chatgpt.com/api/auth/callback/openai?code=demo",
            "current_url": "https://auth.openai.com/about-you",
        }
        self.assertTrue(br._is_oauth_browser_callback_url(state["continue_url"]))
        self.assertFalse(br._is_internal_auth_api_continue_url(state["continue_url"]))
        self.assertTrue(br._requires_registration_navigation(state))

    def test_platform_auth_callback_external_url_must_navigate(self):
        state = {
            "method": "GET",
            "page_type": "external_url",
            "continue_url": "https://platform.openai.com/auth/callback",
            "current_url": "https://auth.openai.com/about-you",
        }
        self.assertTrue(br._requires_registration_navigation(state))

    def test_external_url_internal_accounts_api_still_blocked(self):
        state = {
            "method": "GET",
            "page_type": "external_url",
            "continue_url": "https://auth.openai.com/api/accounts/email-otp/send",
            "current_url": "https://auth.openai.com/create-account/password",
        }
        self.assertFalse(br._requires_registration_navigation(state))


if __name__ == "__main__":
    unittest.main()
