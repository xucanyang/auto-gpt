import os
import time
import unittest
from unittest import mock
from unittest.mock import AsyncMock

from services.turnstile_solver.api_solver import TurnstileAPIServer


class _FakeBrowser:
    def __init__(self):
        self.closed = False

    def is_connected(self):
        return not self.closed

    async def close(self):
        self.closed = True


class SolverBrowserPoolTests(unittest.IsolatedAsyncioTestCase):
    def _server(self, **environment):
        values = {
            "SOLVER_POOL_MODE": "auto",
            "SOLVER_WARM_BROWSERS": "0",
            "SOLVER_IDLE_TIMEOUT_SECONDS": "300",
            **environment,
        }
        with mock.patch.dict(os.environ, values):
            return TurnstileAPIServer(
                headless=True,
                useragent=None,
                debug=False,
                browser_type="camoufox",
                thread=5,
                proxy_support=False,
            )

    async def test_zero_warm_pool_starts_without_browser_process(self):
        server = self._server()
        server._ensure_browser_runtime = AsyncMock()
        server._launch_browser = AsyncMock()

        await server._initialize_browser()

        server._ensure_browser_runtime.assert_awaited_once_with()
        server._launch_browser.assert_not_awaited()
        self.assertEqual(server._launched_browser_count, 0)
        self.assertEqual(server.browser_pool.qsize(), 0)
        self.assertEqual(server.thread_count, 5)

    async def test_auto_pool_reuses_then_reaps_idle_browser(self):
        server = self._server(SOLVER_IDLE_TIMEOUT_SECONDS="30")
        browser = _FakeBrowser()
        server._launched_browser_count = 1

        await server._release_browser(1, browser, {"browser_name": "camoufox"})

        self.assertFalse(browser.closed)
        self.assertEqual(server.browser_pool.qsize(), 1)
        index, pooled_browser, config, _idle_since = server.browser_pool.get_nowait()
        server.browser_pool.put_nowait(
            (index, pooled_browser, config, time.monotonic() - 31)
        )

        closed = await server._reap_idle_browsers()

        self.assertEqual(closed, 1)
        self.assertTrue(browser.closed)
        self.assertEqual(server._launched_browser_count, 0)
        self.assertEqual(server.browser_pool.qsize(), 0)

    async def test_patchright_solver_keeps_native_chromium_identity(self):
        server = TurnstileAPIServer(
            headless=False,
            useragent="legacy-override-must-not-apply",
            debug=False,
            browser_type="chromium",
            thread=1,
            proxy_support=False,
        )
        launch = AsyncMock(return_value=_FakeBrowser())
        server._playwright = mock.Mock(
            chromium=mock.Mock(launch=launch),
        )

        config = server._build_browser_config()
        await server._launch_browser(1, config)
        context_options = server._build_context_options(
            config,
            proxy={"server": "http://proxy.test:8080"},
        )

        self.assertEqual(config["browser_version"], "151.0.7922.34")
        self.assertIsNone(config["useragent"])
        self.assertIsNone(config["sec_ch_ua"])
        self.assertEqual(
            context_options,
            {
                "proxy": {"server": "http://proxy.test:8080"},
                "no_viewport": True,
            },
        )
        launch.assert_awaited_once_with(headless=False)


if __name__ == "__main__":
    unittest.main()
