import asyncio
import contextlib
import json
import sys
import threading
import types
import unittest
from unittest import mock

from services.chatgpt_core.chatgpt_client import ChatGPTClient
from services.chatgpt_core.sentinel_browser import (
    BrowserAccountCreateResult,
    _sentinel_token_field_state,
    export_session_cookies_for_playwright,
    merge_playwright_cookies_into_session,
    run_sync_playwright_safely,
    create_account_via_browser,
)
from services.chatgpt_core.utils import generate_browser_fingerprint


class SentinelBrowserRuntimeTests(unittest.TestCase):
    def test_sync_playwright_helper_runs_directly_without_async_loop(self):
        current_thread = threading.get_ident()

        result = run_sync_playwright_safely(lambda: threading.get_ident())

        self.assertEqual(result, current_thread)

    def test_sync_playwright_helper_uses_clean_thread_inside_async_loop(self):
        logs: list[str] = []

        async def _run() -> tuple[int, int]:
            loop_thread = threading.get_ident()
            worker_thread = run_sync_playwright_safely(
                lambda: threading.get_ident(),
                logger=logs.append,
                label="Sentinel Browser",
            )
            return loop_thread, worker_thread

        loop_thread, worker_thread = asyncio.run(_run())

        self.assertNotEqual(worker_thread, loop_thread)
        self.assertTrue(any("asyncio loop" in item and "隔离线程" in item for item in logs))

    def test_browser_token_requires_all_three_sentinel_signals(self):
        self.assertEqual(
            _sentinel_token_field_state('{"p":"pow","t":"telemetry","c":"challenge"}'),
            {"p": True, "t": True, "c": True},
        )
        self.assertEqual(
            _sentinel_token_field_state('{"p":"pow","t":"","c":"challenge"}'),
            {"p": True, "t": False, "c": True},
        )
        self.assertIsNone(_sentinel_token_field_state("not-json"))

    def test_registration_sentinel_does_not_fall_back_to_http_pow(self):
        client = ChatGPTClient(verbose=False)
        client.session.cookies.set(
            "login_session",
            "session-demo",
            domain="auth.openai.com",
            path="/",
        )

        with mock.patch(
            "services.chatgpt_core.chatgpt_client.get_sentinel_token_via_browser",
            return_value=None,
        ) as browser_token, mock.patch(
            "services.chatgpt_core.chatgpt_client.build_sentinel_token"
        ) as http_token:
            token = client._get_sentinel_token(
                "oauth_create_account",
                page_url="https://auth.openai.com/about-you",
            )

        self.assertIsNone(token)
        http_token.assert_not_called()
        kwargs = browser_token.call_args.kwargs
        self.assertTrue(kwargs["require_complete_signals"])
        self.assertIn("login_session=session-demo", kwargs["cookie_header"])

    def test_registration_stops_before_post_when_browser_token_is_missing(self):
        client = ChatGPTClient(verbose=False)
        client.session.post = mock.Mock()

        with mock.patch(
            "services.chatgpt_core.chatgpt_client.create_account_via_browser",
            return_value=None,
        ):
            ok, error = client.create_account(
                "Alice",
                "Smith",
                "1990-01-01",
            )

        self.assertFalse(ok)
        self.assertIn("auth_browser_finalize_unavailable", error)
        client.session.post.assert_not_called()

    def test_browser_create_account_preserves_cookie_scope_and_never_uses_curl_post(self):
        client = ChatGPTClient(verbose=False)
        client.session.post = mock.Mock()
        client.session.cookies.set(
            "login_session",
            "session-demo",
            domain="auth.openai.com",
            path="/",
            secure=True,
        )
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
            cookie_names=("cf_clearance", "oai-sc"),
            cf_clearance_present=True,
            oai_sc_present=True,
        )

        with mock.patch(
            "services.chatgpt_core.chatgpt_client.create_account_via_browser",
            return_value=result,
        ) as browser_create:
            ok, state = client.create_account(
                "Alice",
                "Smith",
                "1990-01-01",
                return_state=True,
            )

        self.assertTrue(ok)
        self.assertEqual(state.page_type, "external_url")
        client.session.post.assert_not_called()
        exported = browser_create.call_args.kwargs["cookies"]
        login_cookie = next(item for item in exported if item["name"] == "login_session")
        self.assertEqual(login_cookie["domain"], "auth.openai.com")
        stored_domains = {
            cookie.domain
            for cookie in client.session.cookies.jar
            if cookie.name == "oai-sc"
        }
        self.assertEqual(stored_domains, {".openai.com"})

    def test_browser_create_account_keeps_registration_disallowed_error_code(self):
        client = ChatGPTClient(verbose=False)
        result = BrowserAccountCreateResult(
            status_code=400,
            response_text=(
                '{"error":{"code":"registration_disallowed",'
                '"message":"Sorry, we cannot create your account."}}'
            ),
            response_json={
                "error": {
                    "code": "registration_disallowed",
                    "message": "Sorry, we cannot create your account.",
                }
            },
        )

        with mock.patch(
            "services.chatgpt_core.chatgpt_client.create_account_via_browser",
            return_value=result,
        ):
            ok, error = client.create_account("Alice", "Smith", "1990-01-01")

        self.assertFalse(ok)
        self.assertEqual(error, "HTTP 400: registration_disallowed")

    def test_cookie_bridge_preserves_parent_domain_without_duplicates(self):
        client = ChatGPTClient(verbose=False)
        client.session.cookies.set(
            "oaicom-stable-id",
            "stable-demo",
            domain=".openai.com",
            path="/",
            secure=True,
        )

        exported = export_session_cookies_for_playwright(client.session)
        stable = next(item for item in exported if item["name"] == "oaicom-stable-id")
        self.assertEqual(stable["domain"], ".openai.com")

        merged = merge_playwright_cookies_into_session(
            client.session,
            [
                {
                    "name": "oai-sc",
                    "value": "oai-sc-demo",
                    "domain": ".openai.com",
                    "path": "/",
                    "secure": True,
                }
            ],
        )
        self.assertEqual(merged, 1)
        domains = [
            cookie.domain
            for cookie in client.session.cookies.jar
            if cookie.name == "oai-sc"
        ]
        self.assertEqual(domains, [".openai.com"])

    def test_browser_finalize_uses_one_context_for_auth_sentinel_and_create(self):
        sentinel_frame = type(
            "FakeFrame",
            (),
            {"url": "https://sentinel.openai.com/backend-api/sentinel/frame.html"},
        )()

        class FakeContext:
            def __init__(self):
                self.imported = []
                self.page = None

            def add_cookies(self, cookies):
                self.imported.extend(cookies)

            def new_page(self):
                self.page = FakePage(self)
                return self.page

            def cookies(self):
                generated = [
                    {
                        "name": "cf_clearance",
                        "value": "clearance-demo",
                        "domain": "auth.openai.com",
                        "path": "/",
                        "secure": True,
                    },
                    {
                        "name": "oai-sc",
                        "value": "oai-sc-demo",
                        "domain": ".openai.com",
                        "path": "/",
                        "secure": True,
                    },
                ]
                return [*self.imported, *generated]

        class FakePage:
            def __init__(self, context):
                self.context = context
                self.url = "https://auth.openai.com/about-you"
                self.frames = [sentinel_frame]
                self.evaluate_calls = []

            def set_default_timeout(self, _timeout):
                return None

            def set_default_navigation_timeout(self, _timeout):
                return None

            def on(self, _event, _callback):
                return None

            def goto(self, url, **_kwargs):
                self.url = url
                return None

            def wait_for_timeout(self, _timeout):
                return None

            def evaluate(self, script, payload):
                self.evaluate_calls.append((script, payload))
                return {
                    "status": 200,
                    "url": "https://auth.openai.com/api/accounts/create_account",
                    "text": json.dumps(
                        {
                            "page": {"type": "external_url"},
                            "continue_url": "https://chatgpt.com/api/auth/callback/openai?code=demo",
                        }
                    ),
                }

        context = FakeContext()

        class FakeBrowser:
            def new_context(self, **_kwargs):
                return context

            def close(self):
                return None

        class FakeChromium:
            def launch(self, **_kwargs):
                return FakeBrowser()

        fake_playwright = type("FakePlaywright", (), {"chromium": FakeChromium()})()
        fake_sync_manager = mock.MagicMock()
        fake_sync_manager.__enter__.return_value = fake_playwright
        fake_sync_manager.__exit__.return_value = False
        fake_playwright_module = types.ModuleType("playwright")
        fake_sync_api_module = types.ModuleType("playwright.sync_api")
        fake_sync_api_module.sync_playwright = lambda: fake_sync_manager
        fake_playwright_module.sync_api = fake_sync_api_module
        token = json.dumps({"p": "pow", "t": "telemetry", "c": "challenge"})

        with (
            mock.patch.dict(
                sys.modules,
                {
                    "playwright": fake_playwright_module,
                    "playwright.sync_api": fake_sync_api_module,
                },
            ),
            mock.patch(
                "services.chatgpt_core.sentinel_browser.playwright_proxy_context",
                return_value=contextlib.nullcontext(None),
            ),
            mock.patch(
                "services.chatgpt_core.sentinel_browser._evaluate_complete_sentinel_token",
                return_value=token,
            ),
        ):
            result = create_account_via_browser(
                name="Alice Smith",
                birthdate="1990-01-01",
                device_id="device-demo",
                cookies=[
                    {
                        "name": "login_session",
                        "value": "session-demo",
                        "domain": "auth.openai.com",
                        "path": "/",
                        "secure": True,
                    },
                    {
                        "name": "oaicom-stable-id",
                        "value": "stable-demo",
                        "domain": ".openai.com",
                        "path": "/",
                        "secure": True,
                    },
                ],
                trace_headers={"x-datadog-origin": "rum"},
            )

        self.assertTrue(result.ok)
        self.assertTrue(result.cf_clearance_present)
        self.assertTrue(result.oai_sc_present)
        self.assertEqual(result.sentinel_field_lengths, {"p": 3, "t": 9, "c": 9})
        imported_domains = {
            item.get("domain")
            for item in context.imported
            if item.get("name") == "oaicom-stable-id"
        }
        self.assertEqual(imported_domains, {".openai.com"})
        script, payload = context.page.evaluate_calls[-1]
        self.assertIn("fetch('/api/accounts/create_account'", script)
        self.assertIn("x-access-flow-invocation-id", script)
        self.assertNotIn("oai-device-id", script)
        self.assertEqual(payload["traceHeaders"], {"x-datadog-origin": "rum"})

    def test_generated_fingerprint_matches_pinned_playwright_chromium(self):
        for _ in range(10):
            fingerprint = generate_browser_fingerprint()
            self.assertEqual(fingerprint.chrome_major, 145)
            self.assertEqual(fingerprint.chrome_full_version, "145.0.7632.6")
            self.assertEqual(fingerprint.impersonate, "chrome145")


if __name__ == "__main__":
    unittest.main()
