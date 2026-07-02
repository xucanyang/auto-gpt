import time
import unittest
from unittest import mock
from core.proxy_utils import resolve_probe_candidate_proxies
from services.chatgpt_core.plugin import ChatGPTPlatform
from core.base_platform import RegisterConfig


class DummyAccount:
    def __init__(self, email="test@example.com", user_id="u123", extra=None):
        self.email = email
        self.user_id = user_id
        self.token = "at-test"
        self.extra = dict(extra or {"access_token": "at-test"})


class ProbeLocalStatusBatchConfigTests(unittest.TestCase):
    def test_resolve_probe_candidate_proxies_direct(self):
        candidates = resolve_probe_candidate_proxies({"proxy_mode": "direct"})
        self.assertEqual(candidates, [("", None, "direct")])

    def test_resolve_probe_candidate_proxies_specified(self):
        candidates = resolve_probe_candidate_proxies({
            "proxy_mode": "specified",
            "proxy": "http://user:pass@host:8080"
        })
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0][0], "http://user:pass@host:8080")
        self.assertEqual(candidates[0][2], "specified")

    @mock.patch("core.proxy_pool.proxy_pool.get_candidate_records")
    def test_resolve_probe_candidate_proxies_pool(self, mock_pool):
        mock_pool.return_value = [
            {"url": "http://pool1:80", "exit_country_code": "US", "health_score": 90, "latency_ms": 120},
            {"url": "http://pool2:80", "exit_country_code": "JP", "health_score": 85, "latency_ms": 150},
        ]
        candidates = resolve_probe_candidate_proxies({
            "proxy_mode": "pool",
            "proxy_country_code": "US",
            "proxy_min_score": 80,
            "proxy_max_candidates": 2,
        })
        self.assertEqual(len(candidates), 2)
        self.assertEqual(candidates[0][0], "http://pool1:80")
        self.assertIn("country=US", candidates[0][2])
        self.assertEqual(candidates[1][0], "http://pool2:80")

    @mock.patch("services.chatgpt_core.status_probe.probe_local_chatgpt_status")
    def test_execute_action_probe_local_status_uses_specified_proxy(self, mock_probe):
        mock_probe.return_value = {
            "auth": {"state": "access_token_valid"},
            "subscription": {"plan": "plus"},
            "codex": {"state": "usable"},
        }
        platform = ChatGPTPlatform(config=RegisterConfig(extra={}))
        account = DummyAccount()
        result = platform.execute_action("probe_local_status", account, {
            "proxy_mode": "specified",
            "proxy": "http://proxy.test:80",
        })
        self.assertTrue(result["ok"])
        mock_probe.assert_called_once()
        self.assertEqual(mock_probe.call_args.kwargs.get("proxy"), "http://proxy.test:80")

    @mock.patch("core.proxy_pool.proxy_pool.report_fail")
    @mock.patch("core.proxy_pool.proxy_pool.report_success")
    @mock.patch("core.proxy_pool.proxy_pool.get_candidate_records")
    @mock.patch("services.chatgpt_core.status_probe.probe_local_chatgpt_status")
    def test_execute_action_probe_local_status_failover(self, mock_probe, mock_get_candidates, mock_success, mock_fail):
        mock_get_candidates.return_value = [
            {"url": "http://bad-proxy:80", "exit_country_code": "US", "health_score": 90, "latency_ms": 100},
            {"url": "http://good-proxy:80", "exit_country_code": "US", "health_score": 85, "latency_ms": 120},
        ]
        mock_probe.side_effect = [
            {"auth": {"state": "probe_failed", "message": "curl: (28) timeout"}},
            {"auth": {"state": "access_token_valid"}, "subscription": {"plan": "free"}, "codex": {"state": "usable"}},
        ]
        platform = ChatGPTPlatform(config=RegisterConfig(extra={}))
        account = DummyAccount()
        result = platform.execute_action("probe_local_status", account, {
            "proxy_mode": "pool",
        })
        self.assertTrue(result["ok"])
        self.assertEqual(mock_probe.call_count, 2)
        mock_fail.assert_called_once_with("http://bad-proxy:80")
        mock_success.assert_called_once_with("http://good-proxy:80")

    @mock.patch("time.sleep")
    @mock.patch("api.actions._execute_platform_action")
    @mock.patch("api.actions._resolve_batch_accounts")
    def test_execute_batch_action_with_delay(self, mock_resolve, mock_exec, mock_sleep):
        from api.actions import execute_batch_action, BatchActionRequest
        mock_exec.return_value = {"ok": True, "message": "done"}
        acc1 = mock.Mock(id=1, email="a1@example.com", status="ok")
        acc2 = mock.Mock(id=2, email="a2@example.com", status="ok")
        mock_resolve.return_value = ([acc1, acc2], [])
        session = mock.Mock()

        body = BatchActionRequest(
            account_ids=[1, 2],
            params={"delay_seconds": 0.5, "delay_max_seconds": 0.5}
        )
        execute_batch_action("chatgpt", "probe_local_status", body, session)

        self.assertEqual(mock_exec.call_count, 2)
        mock_sleep.assert_called()

    @mock.patch("api.tasks._save_task_log")
    @mock.patch("api.tasks._resolve_batch_probe_local_status_accounts")
    def test_create_batch_probe_local_status_task(self, mock_resolve, mock_save_log):

        from api.tasks import create_batch_probe_local_status_task, BatchProbeLocalStatusTaskRequest
        mock_resolve.return_value = (
            [{"account_id": 10, "email": "a10@example.com", "status": "ok"}],
            [],
            [],
            [{"account_id": 10, "email": "a10@example.com", "status": "ok"}],
        )
        req = BatchProbeLocalStatusTaskRequest(account_ids=[10])
        res = create_batch_probe_local_status_task(req, background_tasks=mock.Mock())
        self.assertIn("task_id", res)
        self.assertEqual(res["eligible"], 1)

