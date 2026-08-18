import json
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
)


class _FakeLocator:
    def __init__(self, *, input_value="", count=1, text="", on_click=None):
        self._value = input_value
        self._count = count
        self._text = text
        self._on_click = on_click
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
        if callable(self._on_click):
            self._on_click()
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
    def __init__(self, status, data=None, text="", url=""):
        self.status = status
        self.url = url or "https://auth.openai.com/api/accounts/email-otp/validate"
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
        self.locator_submit_clicks = 0

    def wait_for_load_state(self, **_kwargs):
        return None

    def wait_for_timeout(self, _value):
        return None

    def locator(self, selector):
        if selector.startswith("input[inputmode"):
            return _FakeLocator(count=0)
        if selector.startswith("text=") or "error" in selector.lower() or "alert" in selector.lower():
            return _FakeLocator(count=0)
        return _FakeLocator(
            on_click=self._emit_submit if selector.startswith(("button", "form")) else None
        )

    def _emit_submit(self):
        self.locator_submit_clicks += 1
        if self.request is not None:
            self.emit("request", self.request)
        if self.response is not None:
            self.emit("response", self.response)

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

    def press(self, key, **_kwargs):
        if str(key).lower() in {"control+a", "meta+a"}:
            self.value = ""

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


class _AboutYouDateSegmentLocator(_AboutYouInputLocator):
    def __init__(self, page, part, value):
        super().__init__()
        self.page = page
        self.part = part
        self.value = value

    def type(self, value, **_kwargs):
        self.value += str(value)
        self.page.sync_birthday()


class _AboutYouHiddenBirthdayLocator(_AboutYouInputLocator):
    def __init__(self, page):
        super().__init__(visible=False)
        self.page = page

    def count(self):
        return 1

    def input_value(self):
        return self.page.birthday_value


class _JapaneseBirthdayPage:
    url = "https://auth.openai.com/about-you"

    def __init__(self):
        self.name_input = _AboutYouInputLocator()
        self.empty = _AboutYouInputLocator(visible=False)
        self.visible_inputs = _AboutYouInputCollection([self.name_input])
        self.year_segment = _AboutYouDateSegmentLocator(self, "year", "2026")
        self.month_segment = _AboutYouDateSegmentLocator(self, "month", "08")
        self.day_segment = _AboutYouDateSegmentLocator(self, "day", "07")
        self.birthday_value = "2026-08-07"
        self.hidden_birthday = _AboutYouHiddenBirthdayLocator(self)
        self.listeners = {}

    @staticmethod
    def _pattern_text(value):
        return str(getattr(value, "pattern", value) or "")

    def sync_birthday(self):
        self.birthday_value = (
            f"{self.year_segment.value}-{self.month_segment.value}-{self.day_segment.value}"
        )

    def locator(self, selector):
        if selector == "input:visible:not([type='hidden']):not([disabled]):not([readonly])":
            return self.visible_inputs
        if selector == 'div[data-type="year"], input[data-type="year"]':
            return self.year_segment
        if selector == 'div[data-type="month"], input[data-type="month"]':
            return self.month_segment
        if selector == 'div[data-type="day"], input[data-type="day"]':
            return self.day_segment
        if selector == 'input[name="birthday"]':
            return self.hidden_birthday
        if selector in {'input[name="name"]', 'input[autocomplete="name"]'}:
            return self.name_input
        return self.empty

    def get_by_label(self, pattern):
        text = self._pattern_text(pattern)
        if "氏名" in text:
            return self.name_input
        # Reproduce the live accessible-name false match that used to
        # overwrite the name input with a numeric age.
        if "年齢" in text:
            return self.name_input
        return self.empty

    def get_by_role(self, _role, **kwargs):
        return self.get_by_label(kwargs.get("name"))

    def get_by_placeholder(self, _pattern):
        return self.empty

    def evaluate(self, _script):
        return {
            "labels": ["氏名", "生年月日"],
            "placeholders": ["氏名"],
            "headings": ["年齢を確認します"],
            "hasAge": True,
            "hasBirthday": True,
        }

    def on(self, event, listener):
        self.listeners.setdefault(event, []).append(listener)

    def remove_listener(self, event, listener):
        listeners = self.listeners.get(event, [])
        if listener in listeners:
            listeners.remove(listener)


class BrowserRegistrationFlowTests(unittest.TestCase):
    def test_email_otp_resend_api_fallback_uses_post_resend_contract(self):
        page = mock.Mock()
        fetch_result = {"ok": True, "status": 204, "text": ""}

        with (
            mock.patch.object(br, "_browser_pause"),
            mock.patch.object(
                br,
                "_browser_fetch",
                return_value=fetch_result,
            ) as fetch,
            mock.patch.object(br, "_build_browser_sentinel_token") as sentinel,
        ):
            result = br._send_browser_email_otp(
                page,
                device_id="device-fixed",
                user_agent="Mozilla/5.0",
                referer="https://auth.openai.com/email-verification",
                resend=True,
            )

        self.assertEqual(result, fetch_result)
        request = fetch.call_args
        self.assertEqual(
            request.args[1],
            "https://auth.openai.com/api/accounts/email-otp/resend",
        )
        self.assertEqual(request.kwargs["method"], "POST")
        self.assertEqual(request.kwargs["headers"]["accept"], "*/*")
        self.assertEqual(
            request.kwargs["headers"]["content-type"],
            "application/json",
        )
        self.assertNotIn("body", request.kwargs)
        sentinel.assert_not_called()

    def test_browser_oauth_email_otp_fallback_posts_resend(self):
        page = mock.Mock()
        logs: list[str] = []

        with (
            mock.patch.object(
                br,
                "_browser_fetch",
                side_effect=[
                    {"ok": False, "status": 500, "text": "failed"},
                    {"ok": True, "status": 204, "text": ""},
                ],
            ) as fetch,
            mock.patch.object(
                br,
                "_build_browser_sentinel_token",
                return_value="sentinel",
            ) as sentinel,
        ):
            ok, sent_at = br._send_browser_oauth_email_otp(
                page,
                device_id="device-fixed",
                user_agent="Mozilla/5.0",
                referer="https://auth.openai.com/email-verification",
                log=lambda message: logs.append(str(message)),
            )

        self.assertTrue(ok)
        self.assertIsNotNone(sent_at)
        self.assertEqual(fetch.call_count, 2)
        resend_request = fetch.call_args_list[1]
        self.assertEqual(
            resend_request.args[1],
            "https://auth.openai.com/api/accounts/email-otp/resend",
        )
        self.assertEqual(resend_request.kwargs["method"], "POST")
        self.assertNotIn("body", resend_request.kwargs)
        self.assertEqual(
            resend_request.kwargs["headers"]["content-type"],
            "application/json",
        )
        self.assertNotIn(
            "openai-sentinel-token",
            resend_request.kwargs["headers"],
        )
        sentinel.assert_called_once()

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
            mock.patch.object(br, "_click_passwordless_login_if_available") as passwordless,
        ):
            result = br._wait_for_signup_entry_transition(
                page,
                lambda _message: None,
                timeout=1,
            )

        self.assertEqual(result, login_state)
        passwordless.assert_not_called()

    def test_signup_transition_prefers_authorize_continue_response_page_type(self):
        page = mock.Mock()
        response = _FakeResponse(
            200,
            data={
                "page": {
                    "type": "login_password",
                    "payload": {"url": "/log-in/password"},
                }
            },
            url="https://auth.openai.com/api/accounts/authorize/continue",
        )
        observer = types.SimpleNamespace(business_responses=[response])

        result = br._wait_for_signup_entry_transition(
            page,
            lambda _message: None,
            timeout=1,
            response_observer=observer,
        )

        self.assertEqual(result["page_type"], "login_password")
        self.assertEqual(result["_route_source"], "authorize_continue_response")
        self.assertEqual(result["_route_response_status"], 200)

    def test_browser_registration_login_password_is_structured_existing_account(self):
        page = mock.Mock()
        page.url = "https://auth.openai.com/log-in/password"
        page.evaluate.return_value = "Mozilla/5.0 Camoufox"
        page.context.cookies.return_value = []
        login_state = {
            "page_type": "login_password",
            "current_url": page.url,
            "_route_source": "authorize_continue_response",
        }

        with (
            mock.patch.object(br, "_seed_browser_device_id"),
            mock.patch.object(
                br,
                "_start_browser_signup_via_page",
                return_value=login_state,
            ),
        ):
            with self.assertRaises(br.ExistingAccountDetected) as caught:
                br._browser_registration_flow(
                    page,
                    "existing@example.com",
                    "OpenAI9_policy!",
                    lambda *_args, **_kwargs: "123456",
                    None,
                    lambda _message: None,
                )

        event = caught.exception.route_event
        self.assertEqual(event["stage"], "after_email")
        self.assertEqual(event["signal"], "login_password")
        self.assertEqual(event["page_type"], "login_password")
        self.assertTrue(event["deterministic"])

    def test_browser_registration_about_you_existing_is_structured_late_signal(self):
        page = mock.Mock()
        page.url = "https://auth.openai.com/about-you"
        page.evaluate.return_value = "Mozilla/5.0 Camoufox"
        page.context.cookies.return_value = []
        about_state = {
            "page_type": "about_you",
            "current_url": page.url,
        }

        with (
            mock.patch.object(br, "_seed_browser_device_id"),
            mock.patch.object(
                br,
                "_start_browser_signup_via_page",
                return_value=about_state,
            ),
            mock.patch.object(br, "_ensure_about_you_page"),
            mock.patch.object(
                br,
                "_submit_about_you_via_page",
                return_value={
                    "ok": False,
                    "status": 400,
                    "url": page.url,
                    "text": "An account already exists for this email address.",
                },
            ),
        ):
            with self.assertRaises(br.ExistingAccountDetected) as caught:
                br._browser_registration_flow(
                    page,
                    "existing@example.com",
                    "OpenAI9_policy!",
                    lambda *_args, **_kwargs: "123456",
                    None,
                    lambda _message: None,
                )

        event = caught.exception.route_event
        self.assertEqual(event["stage"], "about_you")
        self.assertEqual(event["signal"], "account_already_exists")
        self.assertEqual(event["page_type"], "about_you")
        self.assertTrue(event["deterministic"])

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

    def test_otp_submit_skips_hidden_first_control(self):
        response = _FakeResponse(
            200,
            {"page": {"type": "about_you", "payload": {"url": "https://auth.openai.com/about-you"}}},
        )
        page = _FakePage(response=response)
        hidden = mock.Mock()
        hidden.is_visible.return_value = False
        visible = _FakeLocator()
        code_controls = mock.Mock()
        code_controls.count.return_value = 2
        code_controls.nth.side_effect = [hidden, visible]

        def locator(selector):
            if selector == br.OTP_DIGIT_INPUT_SELECTOR:
                return _FakeLocator(count=0)
            if selector == "input[name*='code' i]":
                return code_controls
            if selector.startswith("button"):
                return _FakeLocator(on_click=page._emit_submit)
            return _FakeLocator(count=0)

        page.locator = locator
        page.get_by_label = lambda _pattern: _FakeLocator(count=0)
        page.get_by_role = lambda _role, **_kwargs: _FakeLocator(count=0)

        with mock.patch.object(br, "_browser_pause"):
            result = br._submit_otp_via_page(
                page,
                "123456",
                lambda _message: None,
                allow_api_fallback=False,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["page"]["type"], "about_you")
        hidden.click.assert_not_called()
        self.assertEqual(visible.input_value(), "123456")

    def test_otp_target_wait_handles_delayed_dom_render(self):
        page = mock.Mock()
        page.url = "https://auth.openai.com/email-verification"
        empty = _FakeLocator(count=0)
        calls = {"code": 0}

        def locator(selector):
            if selector == "input[name*='code' i]":
                calls["code"] += 1
                return _FakeLocator(count=1 if calls["code"] >= 3 else 0)
            return empty

        page.locator.side_effect = locator
        page.get_by_label.return_value = empty
        page.get_by_role.return_value = empty

        with mock.patch.object(br.time, "sleep"):
            targets = br._wait_for_visible_otp_targets(page, 6, timeout=1)

        self.assertIsNotNone(targets)
        self.assertEqual(targets[0], "single")
        self.assertGreaterEqual(calls["code"], 3)

    def test_click_first_uses_visible_button_when_hidden_match_comes_first(self):
        page = mock.Mock()
        hidden = mock.Mock()
        hidden.is_visible.return_value = False
        visible = mock.Mock()
        visible.is_visible.return_value = True
        buttons = mock.Mock()
        buttons.count.return_value = 2
        buttons.nth.side_effect = [hidden, visible, hidden, visible]
        page.locator.return_value = buttons

        selected = br._click_first(
            page,
            ['button[type="submit"]'],
            timeout=1,
        )

        self.assertEqual(selected, 'button[type="submit"]')
        hidden.click.assert_not_called()
        visible.click.assert_called_once_with(timeout=1000)

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

    def test_codex_oauth_retries_passwordless_after_current_password_rejection(self):
        page = mock.Mock()
        page.url = "https://auth.openai.com/log-in/password"
        page.evaluate.return_value = "Mozilla/5.0"
        oauth_start = types.SimpleNamespace(
            auth_url="https://auth.openai.com/oauth/authorize?state=state-fixed",
            state="state-fixed",
        )
        login_state = {
            "page_type": "login_password",
            "current_url": "https://auth.openai.com/log-in/password",
            "continue_url": "",
        }
        callback_state = {
            "page_type": "oauth_callback",
            "current_url": "http://localhost:1455/auth/callback?code=code-fixed&state=state-fixed",
            "continue_url": "",
        }
        otp_state = {
            "page_type": "email_otp_verification",
            "current_url": "https://auth.openai.com/email-verification",
            "_otp_sent_at": 100.0,
        }

        def navigate(url, **_kwargs):
            if "email-verification" in str(url):
                page.url = callback_state["current_url"]
            else:
                page.url = str(url)

        page.goto.side_effect = navigate
        with (
            mock.patch(
                "services.chatgpt_core.oauth.generate_oauth_url",
                return_value=oauth_start,
            ),
            mock.patch.object(
                br,
                "_derive_oauth_state_from_page",
                side_effect=[login_state, callback_state],
            ),
            mock.patch.object(
                br,
                "_switch_login_password_to_otp",
                side_effect=[None, otp_state],
            ) as switch_passwordless,
            mock.patch.object(
                br,
                "_submit_oauth_password_direct",
                return_value={
                    "ok": False,
                    "status": 400,
                    "text": "Incorrect email address or password",
                },
            ) as submit_password,
            mock.patch.object(
                br,
                "_submit_callback_result",
                return_value={"access_token": "at", "refresh_token": "rt"},
            ),
        ):
            result = br._do_codex_oauth(
                page,
                {"oai-did": "device-fixed"},
                "buyer@example.com",
                "stale-password",
                lambda: "123456",
                None,
                None,
                lambda _message: None,
                strict_browser=True,
            )

        self.assertEqual(result["refresh_token"], "rt")
        self.assertEqual(switch_passwordless.call_count, 2)
        submit_password.assert_called_once()

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
        # Fallback grace is OTP_SENT_AT_FALLBACK_GRACE_SECONDS (60), not the old 8s.
        self.assertEqual(callback.call_args.args[0]["otp_sent_at"], 40.0)

    def test_password_submit_preserves_otp_sent_at_even_when_page_is_email_otp_send(self):
        """Regression: first OTP can land in TempMail while password SPA is still settling.

        Previously otp_sent_at was only kept when otp_triggered was true
        (email_otp_verification). email_otp_send responses dropped the early
        cutoff and fell back to now-8s, so already-delivered codes were ignored.
        """
        page = mock.Mock()
        page.url = "https://auth.openai.com/create-account/password"
        page.evaluate.return_value = "Mozilla/5.0 Camoufox"
        page.context.cookies.return_value = []
        callback = mock.Mock(return_value="654321")
        start_state = {
            "page_type": "create_account_password",
            "current_url": "https://auth.openai.com/create-account/password",
        }
        password_resp = {
            "ok": True,
            "status": 200,
            "url": "https://auth.openai.com/api/accounts/email-otp/send",
            "data": {
                "page": {
                    "type": "email_otp_send",
                    "payload": {
                        "url": "https://auth.openai.com/api/accounts/email-otp/send",
                    },
                }
            },
            "text": "",
            # Even when callers historically treated only email_otp_verification as
            # otp_triggered, the absolute send timestamp must still be kept.
            "otp_triggered": False,
            "otp_sent_at": 55.0,
            "register_committed": True,
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
            mock.patch.object(br, "_submit_password_via_page", return_value=password_resp),
            mock.patch.object(br, "_wait_for_auth_page_settle"),
            mock.patch.object(
                br,
                "_derive_registration_state_from_page",
                return_value={
                    "page_type": "email_otp_verification",
                    "current_url": "https://auth.openai.com/email-verification",
                },
            ),
            mock.patch.object(br, "_find_first_visible_selector", return_value='input[name="code"]'),
            mock.patch.object(br, "_send_browser_email_otp") as send_otp,
            mock.patch.object(br, "_submit_otp_via_page", return_value=otp_result),
            mock.patch.object(br, "_handle_post_signup_onboarding"),
            mock.patch.object(br.time, "time", return_value=100.0),
        ):
            br._browser_registration_flow(
                page,
                "buyer@example.com",
                "OpenAI9_policy!",
                callback,
                None,
                lambda _message: None,
            )

        send_otp.assert_not_called()
        self.assertEqual(callback.call_args.args[0]["otp_sent_at"], 55.0)

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
        self.assertEqual(page.locator_submit_clicks, 1)
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

    def test_japanese_segmented_birthday_does_not_overwrite_name_with_age(self):
        page = _JapaneseBirthdayPage()
        logs = []
        visible_inputs = [
            {"visibleIndex": 0, "labels": ["氏名"], "name": "name"},
        ]

        with (
            mock.patch.object(br, "_collect_visible_text_inputs", return_value=visible_inputs),
            mock.patch.object(br, "_browser_pause"),
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

        self.assertTrue(result["ok"])
        self.assertEqual(page.name_input.value, "Demo User")
        self.assertEqual(page.year_segment.value, "1990")
        self.assertEqual(page.month_segment.value, "01")
        self.assertEqual(page.day_segment.value, "02")
        self.assertEqual(page.birthday_value, "1990-01-02")
        self.assertTrue(any("页面模式: birthday" in line for line in logs))
        self.assertTrue(any("segmented_birthday=True" in line for line in logs))

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

    def test_about_you_first_2xx_wins_over_later_duplicate_409(self):
        page = _JapaneseAgePage()
        request = types.SimpleNamespace(
            url="https://auth.openai.com/api/accounts/create_account"
        )
        committed = mock.Mock()
        committed.url = request.url
        committed.status = 200
        committed.json.return_value = {}
        committed.text.return_value = ""
        duplicate = mock.Mock()
        duplicate.url = request.url
        duplicate.status = 409
        duplicate.json.return_value = {
            "error": {
                "code": "invalid_auth_step",
                "message": "request is not allowed in this auth step",
            }
        }
        duplicate.text.return_value = json.dumps(duplicate.json.return_value)
        visible_inputs = [
            {"visibleIndex": 0, "labels": ["Full name"]},
            {"visibleIndex": 1, "labels": ["Age"]},
        ]
        clock = [0.0]
        logs: list[str] = []

        def fake_time():
            clock[0] += 2.0
            return clock[0]

        def click_and_emit(*_args, **_kwargs):
            page.emit("request", request)
            page.emit("response", committed)
            page.emit("response", duplicate)
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
                logs.append,
                profile_name="Demo User",
                profile_birthdate="1990-01-02",
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["signup_committed"])
        self.assertTrue(result["transition_pending"])
        self.assertEqual(result["post_commit_response_status"], 409)
        self.assertEqual(result["post_commit_response_code"], "invalid_auth_step")
        self.assertTrue(any("忽略随后重复提交响应" in line for line in logs))
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

    def test_email_adapter_release_code_allows_reuse_after_non_advancing_submit(self):
        logs: list[str] = []
        service = mock.Mock()
        service.get_verification_code.side_effect = ["111111", "111111"]
        adapter = EmailServiceAdapter(
            service, "buyer@example.com", lambda message: logs.append(str(message))
        )

        first = adapter.wait_for_verification_code(
            "buyer@example.com",
            timeout=30,
            phase="browser_register_email_otp",
        )
        self.assertEqual(first, "111111")
        self.assertIn("111111", adapter.used_codes_for_phases("browser_register_email_otp"))

        adapter.release_code("111111", "browser_register_email_otp")
        self.assertNotIn("111111", adapter.used_codes_for_phases("browser_register_email_otp"))
        self.assertTrue(any("释放可复用验证码" in item for item in logs))

        second = adapter.wait_for_verification_code(
            "buyer@example.com",
            timeout=30,
            phase="browser_register_email_otp",
        )
        self.assertEqual(second, "111111")
        # After release, the same digits are not force-excluded on the next wait.
        second_exclude = service.get_verification_code.call_args_list[1].kwargs["exclude_codes"]
        self.assertNotIn("111111", second_exclude)

    def test_web_session_bridge_uses_csrf_cookie_when_api_body_empty(self):
        """Live failure mode: /api/auth/csrf returns 200 with empty JSON body."""
        page = mock.Mock()
        page.url = "https://chatgpt.com/"
        page.context.cookies.return_value = [
            {
                "name": "__Host-next-auth.csrf-token",
                "value": "csrf-token-half|signature-half",
                "domain": "chatgpt.com",
                "path": "/",
            }
        ]
        page.context.request = None
        logs: list[str] = []

        with (
            mock.patch.object(
                br,
                "_browser_fetch",
                return_value={"ok": True, "status": 200, "data": {}, "text": "{}"},
            ),
            mock.patch.object(br, "_wait_for_auth_page_settle"),
            mock.patch.object(br, "_seed_browser_device_id"),
            mock.patch.object(
                br,
                "_start_browser_signin",
                return_value="https://auth.openai.com/api/accounts/authorize?client_id=x",
            ) as signin,
            mock.patch.object(
                br,
                "_fetch_chatgpt_session_payload",
                return_value={
                    "status": 200,
                    "data": {"accessToken": "at-from-bridge", "user": {"email": "buyer@example.com"}},
                },
            ),
        ):
            session = br._browser_chatgpt_openai_signin_bridge(
                page,
                lambda message: logs.append(str(message)),
                email="buyer@example.com",
                device_id="device-demo",
            )

        self.assertIsInstance(session, dict)
        self.assertEqual(session.get("accessToken"), "at-from-bridge")
        signin.assert_called()
        self.assertEqual(
            signin.call_args.args[3],
            "csrf-token-half",
        )
        self.assertTrue(any("csrf cookie" in item for item in logs))
        page.goto.assert_any_call(
            "https://auth.openai.com/api/accounts/authorize?client_id=x",
            wait_until="commit",
            timeout=20000,
        )

    def test_ensure_about_you_page_tolerates_ns_binding_aborted(self):
        page = mock.Mock()
        page.url = "https://auth.openai.com/email-verification"
        logs: list[str] = []

        def fake_goto(url, **_kwargs):
            raise RuntimeError("Page.goto: NS_BINDING_ABORTED")

        page.goto.side_effect = fake_goto

        with (
            mock.patch.object(
                br,
                "_derive_registration_state_from_page",
                side_effect=[
                    {"page_type": "email_otp_verification"},
                    {"page_type": "about_you", "current_url": "https://auth.openai.com/about-you"},
                ],
            ),
            mock.patch.object(br, "_wait_for_auth_page_settle"),
        ):
            br._ensure_about_you_page(
                page,
                "https://auth.openai.com/about-you",
                lambda message: logs.append(str(message)),
            )

        page.goto.assert_called_once()
        self.assertTrue(any("导航被中断" in item for item in logs))

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
