import asyncio
import threading
import unittest

from services.chatgpt_core.sentinel_browser import run_sync_playwright_safely


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


if __name__ == "__main__":
    unittest.main()
