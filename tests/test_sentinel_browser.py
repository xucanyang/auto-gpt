import asyncio
import threading
import unittest
from unittest import mock

from services.chatgpt_core.chatgpt_client import ChatGPTClient
from services.chatgpt_core.sentinel_browser import (
    _sentinel_token_field_state,
    run_sync_playwright_safely,
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
        client._get_sentinel_token = mock.Mock(return_value=None)

        ok, error = client.create_account(
            "Alice",
            "Smith",
            "1990-01-01",
        )

        self.assertFalse(ok)
        self.assertIn("sentinel_browser_unavailable", error)
        client.session.post.assert_not_called()

    def test_generated_fingerprint_matches_pinned_playwright_chromium(self):
        for _ in range(10):
            fingerprint = generate_browser_fingerprint()
            self.assertEqual(fingerprint.chrome_major, 145)
            self.assertEqual(fingerprint.chrome_full_version, "145.0.7632.6")
            self.assertEqual(fingerprint.impersonate, "chrome145")


if __name__ == "__main__":
    unittest.main()
