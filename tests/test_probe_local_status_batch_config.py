import time
import threading
import unittest
from unittest import mock
from fastapi import HTTPException
from core.proxy_utils import resolve_probe_candidate_proxies
from services.chatgpt_core.plugin import ChatGPTPlatform
from core.base_platform import RegisterConfig
from core.task_runtime import RegisterTaskStore


class DummyAccount:
    def __init__(self, email="test@example.com", user_id="u123", extra=None):
        self.email = email
        self.user_id = user_id
        self.token = "at-test"
        self.extra = dict(extra or {"access_token": "at-test"})


def _browser_fingerprint(device_id: str) -> dict:
    return {
        "device_id": device_id,
        "accept_language": "en-US,en;q=0.9",
        "impersonate": "chrome136",
        "chrome_major": 136,
        "chrome_full_version": "136.0.7103.92",
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.7103.92 Safari/537.36",
        "sec_ch_ua": '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"',
        "platform_version": "15.0.0",
        "viewport_width": 1440,
        "viewport_height": 900,
    }


class _RunnerAccount:
    platform = "chatgpt"

    def __init__(self, account_id: int, *, extra: dict | None = None):
        self.id = account_id
        self.email = f"account-{account_id}@example.com"
        self.status = "ok"
        self.extra = dict(extra or {})

    def get_extra(self):
        return dict(self.extra)


class _RunnerSession:
    def __init__(self, accounts: dict[int, _RunnerAccount]):
        self.accounts = accounts

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get(self, _model, account_id):
        return self.accounts.get(int(account_id))


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
        req = BatchProbeLocalStatusTaskRequest(
            account_ids=[10],
            params={"proxy_mode": "direct", "concurrency": 2, "unique_exit_ip_enabled": False},
        )
        res = create_batch_probe_local_status_task(req, background_tasks=mock.Mock())
        self.assertIn("task_id", res)
        self.assertEqual(res["eligible"], 1)
        self.assertEqual(res["requested_concurrency"], 2)
        self.assertEqual(res["effective_concurrency"], 1)

    def test_resolve_batch_probe_accepts_full_free_inventory_above_legacy_limit(self):
        from api.tasks import _resolve_batch_probe_local_status_accounts, BatchProbeLocalStatusTaskRequest

        account_count = 2863
        accounts = [
            _RunnerAccount(account_id, extra={"access_token": f"at-{account_id}"})
            for account_id in range(1, account_count + 1)
        ]
        req = BatchProbeLocalStatusTaskRequest(all_filtered=True, subscription_type="free")

        with mock.patch("api.tasks.Session"), mock.patch(
            "api.tasks._filtered_chatgpt_accounts",
            return_value=accounts,
        ):
            eligible, missing, skipped, matched = _resolve_batch_probe_local_status_accounts(req)

        self.assertEqual(len(eligible), account_count)
        self.assertEqual(len(matched), account_count)
        self.assertEqual(missing, [])
        self.assertEqual(skipped, [])

        session = mock.MagicMock()
        session.__enter__.return_value.exec.return_value.all.return_value = accounts
        selected_req = BatchProbeLocalStatusTaskRequest(
            account_ids=list(range(1, account_count + 1)),
        )
        with mock.patch("api.tasks.Session", return_value=session):
            selected_eligible, selected_missing, selected_skipped, selected_matched = (
                _resolve_batch_probe_local_status_accounts(selected_req)
            )

        self.assertEqual(len(selected_eligible), account_count)
        self.assertEqual(selected_missing, [])
        self.assertEqual(selected_skipped, [])
        self.assertEqual(selected_matched, [])

    def test_resolve_batch_probe_rejects_above_dedicated_safety_limit(self):
        from api.tasks import (
            LOCAL_STATUS_PROBE_MAX_ACCOUNTS,
            _resolve_batch_probe_local_status_accounts,
            BatchProbeLocalStatusTaskRequest,
        )

        account = _RunnerAccount(1, extra={"access_token": "at-1"})
        req = BatchProbeLocalStatusTaskRequest(all_filtered=True, subscription_type="free")

        with mock.patch("api.tasks.Session"), mock.patch(
            "api.tasks._filtered_chatgpt_accounts",
            return_value=[account] * (LOCAL_STATUS_PROBE_MAX_ACCOUNTS + 1),
        ), self.assertRaises(HTTPException) as error:
            _resolve_batch_probe_local_status_accounts(req)

        self.assertEqual(error.exception.status_code, 400)
        self.assertIn(str(LOCAL_STATUS_PROBE_MAX_ACCOUNTS), str(error.exception.detail))

    def test_prepare_batch_probe_freezes_concurrency_and_expands_dynamic_candidates(self):
        from api.tasks import _prepare_batch_probe_local_status_params

        with mock.patch(
            "core.config_store.config_store.get_all",
            return_value={
                "task_proxy_mode": "dynamic",
                "dynamic_proxy_max_attempts": "2",
                "chatgpt_local_status_probe_unique_exit_ip_enabled": "true",
                "chatgpt_local_status_probe_delay_seconds": "1.5",
                "chatgpt_local_status_probe_delay_max_seconds": "3",
            },
        ):
            params, settings = _prepare_batch_probe_local_status_params(
                {"concurrency": 4, "proxy_mode": "dynamic"},
                eligible_count=6,
            )

        self.assertEqual(params["concurrency"], 4)
        self.assertTrue(params["unique_exit_ip_enabled"])
        self.assertTrue(params["proxy_failover"])
        self.assertGreaterEqual(params["dynamic_proxy_max_attempts"], 8)
        self.assertEqual(settings["requested_concurrency"], 4)
        self.assertEqual(settings["effective_concurrency"], 4)
        self.assertEqual(params["delay_seconds"], 1.5)
        self.assertEqual(params["delay_max_seconds"], 3.0)
        self.assertEqual(settings["delay_seconds"], 1.5)
        self.assertEqual(settings["delay_max_seconds"], 3.0)

    def test_prepare_batch_probe_honors_global_unique_exit_with_serial_concurrency(self):
        from api.tasks import _prepare_batch_probe_local_status_params

        with mock.patch(
            "core.config_store.config_store.get_all",
            return_value={
                "task_proxy_mode": "dynamic",
                "dynamic_proxy_max_attempts": "2",
                "chatgpt_local_status_probe_unique_exit_ip_enabled": "true",
            },
        ):
            params, settings = _prepare_batch_probe_local_status_params({}, eligible_count=2)

        self.assertEqual(params["concurrency"], 1)
        self.assertTrue(params["unique_exit_ip_enabled"])
        self.assertTrue(settings["unique_exit_ip_requested"])

    def test_prepare_batch_probe_bounds_non_finite_delays(self):
        from api.tasks import _prepare_batch_probe_local_status_params

        with mock.patch(
            "core.config_store.config_store.get_all",
            return_value={"task_proxy_mode": "dynamic", "chatgpt_local_status_probe_unique_exit_ip_enabled": "false"},
        ):
            params, _settings = _prepare_batch_probe_local_status_params(
                {"delay_seconds": "inf", "delay_max_seconds": "99999"},
                eligible_count=2,
            )

        self.assertEqual(params["delay_seconds"], 0.0)
        self.assertEqual(params["delay_max_seconds"], 3600.0)

    def test_config_api_normalizes_local_status_probe_values(self):
        from api import config as config_api

        current = {
            "task_proxy_mode": "dynamic",
            "task_proxy_failover": "true",
            "chatgpt_local_status_probe_unique_exit_ip_enabled": "true",
            "chatgpt_local_status_probe_delay_seconds": "0",
            "chatgpt_local_status_probe_delay_max_seconds": "0",
        }
        with mock.patch.object(config_api.config_store, "get_all", return_value=current), mock.patch.object(
            config_api.config_store, "set_many"
        ) as set_many:
            config_api.update_config(
                config_api.ConfigUpdate(
                    data={
                        "chatgpt_local_status_probe_concurrency": "3.0",
                        "chatgpt_local_status_probe_unique_exit_ip_enabled": "false",
                        "chatgpt_local_status_probe_delay_seconds": "1.5",
                        "chatgpt_local_status_probe_delay_max_seconds": "3",
                    }
                )
            )

        saved = set_many.call_args.args[0]
        self.assertEqual(saved["chatgpt_local_status_probe_concurrency"], "3")
        self.assertEqual(saved["chatgpt_local_status_probe_unique_exit_ip_enabled"], "false")
        self.assertEqual(saved["chatgpt_local_status_probe_delay_seconds"], "1.5")
        self.assertEqual(saved["chatgpt_local_status_probe_delay_max_seconds"], "3")

    def test_config_api_rejects_incompatible_direct_mode(self):
        from api import config as config_api

        with mock.patch.object(
            config_api.config_store,
            "get_all",
            return_value={
                "task_proxy_mode": "dynamic",
                "task_proxy_failover": "true",
                "chatgpt_local_status_probe_unique_exit_ip_enabled": "true",
            },
        ), mock.patch.object(config_api.config_store, "set_many") as set_many:
            with self.assertRaises(HTTPException) as error:
                config_api.update_config(config_api.ConfigUpdate(data={"task_proxy_mode": "direct"}))

        self.assertEqual(error.exception.status_code, 400)
        set_many.assert_not_called()

    def test_prepare_batch_probe_rejects_direct_mode_with_unique_exit_requirement(self):
        from api.tasks import _prepare_batch_probe_local_status_params

        with self.assertRaises(HTTPException) as error:
            _prepare_batch_probe_local_status_params(
                {"concurrency": 2, "proxy_mode": "direct", "unique_exit_ip_enabled": True},
                eligible_count=2,
            )

        self.assertEqual(error.exception.status_code, 400)
        self.assertIn("独立出口 IP", str(error.exception.detail))

    def test_run_batch_probe_uses_distinct_sessions_and_parallelizes_distinct_fingerprints(self):
        from api.tasks import _run_batch_probe_local_status

        accounts = {
            1: _RunnerAccount(1, extra={"chatgpt_browser_fingerprint": _browser_fingerprint("device-1")}),
            2: _RunnerAccount(2, extra={"chatgpt_browser_fingerprint": _browser_fingerprint("device-2")}),
        }
        store = RegisterTaskStore()
        store.create("task_parallel", platform="chatgpt", total=2, source="batch_probe_local_status", meta={})
        state_lock = threading.Lock()
        active = 0
        max_active = 0
        sessions = set()
        both_started = threading.Event()

        def fake_sync(session, account, **_kwargs):
            nonlocal active, max_active
            with state_lock:
                active += 1
                sessions.add(id(session))
                max_active = max(max_active, active)
                if active == 2:
                    both_started.set()
            both_started.wait(timeout=1)
            time.sleep(0.03)
            with state_lock:
                active -= 1
            return {"probe": {"subscription": {"plan": "plus"}}}

        with mock.patch("api.tasks._task_store", store), mock.patch(
            "api.tasks.Session",
            side_effect=lambda *_args, **_kwargs: _RunnerSession(accounts),
        ), mock.patch(
            "services.chatgpt_core.local_status_refresh.sync_chatgpt_account_local_status",
            side_effect=fake_sync,
        ), mock.patch("api.tasks._save_task_log"):
            _run_batch_probe_local_status(
                "task_parallel",
                [1, 2],
                {"proxy_mode": "direct", "concurrency": 2, "unique_exit_ip_enabled": False},
            )

        snapshot = store.snapshot("task_parallel")
        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["success"], 2)
        self.assertEqual(max_active, 2)
        self.assertEqual(len(sessions), 2)

    def test_run_batch_probe_serializes_legacy_fingerprint_fallback(self):
        from api.tasks import _run_batch_probe_local_status

        accounts = {1: _RunnerAccount(1), 2: _RunnerAccount(2)}
        store = RegisterTaskStore()
        store.create("task_legacy_fingerprint", platform="chatgpt", total=2, source="batch_probe_local_status", meta={})
        state_lock = threading.Lock()
        active = 0
        max_active = 0

        def fake_sync(_session, _account, **_kwargs):
            nonlocal active, max_active
            with state_lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.04)
            with state_lock:
                active -= 1
            return {"probe": {"subscription": {"plan": "free"}}}

        with mock.patch("api.tasks._task_store", store), mock.patch(
            "api.tasks.Session",
            side_effect=lambda *_args, **_kwargs: _RunnerSession(accounts),
        ), mock.patch(
            "services.chatgpt_core.local_status_refresh.sync_chatgpt_account_local_status",
            side_effect=fake_sync,
        ), mock.patch("api.tasks._save_task_log"):
            _run_batch_probe_local_status(
                "task_legacy_fingerprint",
                [1, 2],
                {"proxy_mode": "direct", "concurrency": 2, "unique_exit_ip_enabled": False},
            )

        snapshot = store.snapshot("task_legacy_fingerprint")
        self.assertEqual(snapshot["success"], 2)
        self.assertEqual(max_active, 1)
        self.assertEqual(snapshot["meta"]["fingerprint_isolation"]["legacy_fallback_count"], 2)

    def test_run_batch_probe_records_and_blocks_duplicate_exit_ip(self):
        from api.tasks import _run_batch_probe_local_status

        accounts = {
            1: _RunnerAccount(1, extra={"chatgpt_browser_fingerprint": _browser_fingerprint("device-1")}),
            2: _RunnerAccount(2, extra={"chatgpt_browser_fingerprint": _browser_fingerprint("device-2")}),
        }
        store = RegisterTaskStore()
        store.create("task_exit_collision", platform="chatgpt", total=2, source="batch_probe_local_status", meta={})
        sync = mock.Mock(return_value={"probe": {"subscription": {"plan": "plus"}}})

        with mock.patch("api.tasks._task_store", store), mock.patch(
            "api.tasks.Session",
            side_effect=lambda *_args, **_kwargs: _RunnerSession(accounts),
        ), mock.patch(
            "services.chatgpt_core.local_status_refresh.sync_chatgpt_account_local_status",
            sync,
        ), mock.patch(
            "core.proxy_utils.resolve_probe_candidate_proxies",
            return_value=[("http://proxy.example:8080", None, "specified")],
        ), mock.patch(
            "services.proxy_scanner.probe_basic",
            return_value={"ok": True, "exit_ip": "198.51.100.20"},
        ), mock.patch("api.tasks._save_task_log"):
            _run_batch_probe_local_status(
                "task_exit_collision",
                [1, 2],
                {"proxy_mode": "specified", "concurrency": 2, "unique_exit_ip_enabled": True},
            )

        snapshot = store.snapshot("task_exit_collision")
        self.assertEqual(sync.call_count, 1)
        self.assertEqual(snapshot["success"], 1)
        self.assertEqual(len(snapshot["errors"]), 1)
        self.assertEqual(snapshot["meta"]["unique_exit_ip"]["collision_count"], 1)

    @mock.patch("api.tasks._save_task_log")
    @mock.patch("api.tasks._task_store")
    @mock.patch("services.chatgpt_core.local_status_refresh.sync_chatgpt_account_local_status")
    @mock.patch("api.tasks.Session")
    def test_run_batch_probe_local_status_execution(self, mock_session_cls, mock_sync, mock_store, mock_save_log):
        from api.tasks import _run_batch_probe_local_status
        mock_acc = mock.Mock(id=10, email="a10@example.com", status="ok")
        mock_session_cls.return_value.__enter__.return_value.get.return_value = mock_acc
        mock_sync.return_value = {"probe": {"subscription": {"plan": "plus"}}}
        mock_store.snapshot.return_value = {"meta": {}}
        mock_store.control_for.return_value = mock.Mock()

        _run_batch_probe_local_status("task_test_probe", [10], {"proxy_mode": "direct"})
        mock_sync.assert_called_once()
        mock_store.finish.assert_called_once()

    @mock.patch("api.tasks._save_task_log")
    @mock.patch("api.tasks._task_store")
    @mock.patch("api.tasks.Session")
    def test_run_batch_probe_local_status_marks_unhandled_exception_failed(self, mock_session_cls, mock_store, mock_save_log):
        from api.tasks import _run_batch_probe_local_status

        mock_acc = mock.Mock(id=10, email="a10@example.com", status="ok")
        mock_session_cls.return_value.__enter__.return_value.get.return_value = mock_acc
        mock_store.snapshot.return_value = {"meta": {"emails": ["a10@example.com"], "skipped_items": []}}
        mock_store.control_for.return_value = mock.Mock()

        _run_batch_probe_local_status(
            "task_test_probe_failed",
            [10],
            {
                "proxy_mode": "dynamic",
                "proxy": "http://user:pass@proxy.test:8080",
                "proxy_country_code": "US",
            },
        )

        mock_save_log.assert_called()
        finish_kwargs = mock_store.finish.call_args.kwargs
        self.assertEqual(finish_kwargs.get("status"), "failed")
        self.assertIn("region-XX", finish_kwargs.get("error", ""))
