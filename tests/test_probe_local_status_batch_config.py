import time
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock
from fastapi import HTTPException
from core.proxy_utils import resolve_probe_candidate_proxies
from services.chatgpt_core.plugin import ChatGPTPlatform
from core.base_platform import RegisterConfig
from core.task_runtime import RegisterTaskStore
from services.chatgpt_core.local_status_proxy import (
    is_local_status_proxy_transport_error,
    local_status_probe_proxy_failure,
)


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


def _successful_probe(plan: str = "plus") -> dict:
    return {
        "auth": {"state": "access_token_valid", "http_status": 200},
        "subscription": {"plan": plan},
        "codex": {"state": "usable", "http_status": 200},
    }


def _run_detached_probe(_account_id, **kwargs):
    from services.chatgpt_core.local_status_refresh import (
        local_status_capacity_slot,
        local_status_identity_slot,
    )

    probe_runner = kwargs.get("probe_runner")
    prepared_account = kwargs.get("prepared_account")
    on_probe_start = kwargs.get("on_probe_start")
    stop_check = kwargs.get("stop_check")
    with local_status_identity_slot(prepared_account, stop_check=stop_check):
        with local_status_capacity_slot(stop_check=stop_check):
            if callable(on_probe_start):
                on_probe_start(prepared_account, 1)
            return {"probe": probe_runner(prepared_account)}


def _run_two_detached_probes(_account_id, **kwargs):
    from services.chatgpt_core.local_status_refresh import (
        local_status_capacity_slot,
        local_status_identity_slot,
    )

    prepared_account = kwargs["prepared_account"]
    probe_runner = kwargs["probe_runner"]
    on_probe_start = kwargs.get("on_probe_start")
    stop_check = kwargs.get("stop_check")
    latest_probe = None
    for probe_attempt in (1, 2):
        with local_status_identity_slot(prepared_account, stop_check=stop_check):
            with local_status_capacity_slot(stop_check=stop_check):
                if callable(on_probe_start):
                    on_probe_start(prepared_account, probe_attempt)
                latest_probe = probe_runner(prepared_account)
    return {"probe": latest_probe}


class ProbeLocalStatusBatchConfigTests(unittest.TestCase):
    def test_local_status_proxy_classifier_rejects_business_and_rate_limit_failures(self):
        self.assertFalse(
            is_local_status_proxy_transport_error(
                "OAuth token 刷新失败: HTTP 429",
                http_status=0,
            )
        )
        self.assertFalse(
            is_local_status_proxy_transport_error(
                "HTTP 403 account policy rejected: proxy error",
                http_status=403,
            )
        )
        for message in (
            "HTTP/1.1 429 Too Many Requests: proxy error",
            "HTTP/2 429 proxy error",
            "status_code=429 proxy error",
            "http_code=429 proxy error",
        ):
            with self.subTest(message=message):
                self.assertFalse(is_local_status_proxy_transport_error(message))
        self.assertEqual(
            local_status_probe_proxy_failure(
                {
                    "auth": {
                        "state": "probe_failed",
                        "http_status": 429,
                        "message": "too many requests",
                    }
                }
            ),
            "",
        )

    def test_local_status_proxy_classifier_accepts_explicit_transport_failures(self):
        timeout = "curl: (28) connection timed out via proxy.example:429"
        self.assertTrue(is_local_status_proxy_transport_error(timeout))
        self.assertTrue(is_local_status_proxy_transport_error("HTTPSConnectionPool: Read timed out."))
        self.assertTrue(is_local_status_proxy_transport_error("ProxyError: Connect timed out."))
        self.assertTrue(is_local_status_proxy_transport_error("proxy tunnel connection reset"))
        self.assertTrue(
            is_local_status_proxy_transport_error(
                "Proxy Authentication Required",
                http_status=407,
            )
        )
        self.assertTrue(
            is_local_status_proxy_transport_error(
                "HTTP/1.1 407 Proxy Authentication Required"
            )
        )
        self.assertEqual(
            local_status_probe_proxy_failure(
                {
                    "auth": {
                        "state": "probe_failed",
                        "http_status": 0,
                        "message": timeout,
                    }
                }
            ),
            timeout,
        )

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

    def test_execute_action_dynamic_probe_resolves_sid_lazily(self):
        platform = ChatGPTPlatform(config=RegisterConfig(extra={}))
        account = DummyAccount()
        resolver = mock.Mock(
            side_effect=[
                [("http://proxy.test/sid-first", None, "dynamic probe=ok")],
                [("http://proxy.test/sid-second", None, "dynamic probe=ok")],
            ]
        )
        probe = mock.Mock(
            side_effect=[
                {
                    "auth": {
                        "state": "probe_failed",
                        "http_status": 0,
                        "message": "curl: (28) connection timed out",
                    },
                    "subscription": {"plan": "unknown"},
                    "codex": {"state": "not_checked"},
                },
                _successful_probe(),
            ]
        )

        with mock.patch(
            "core.proxy_utils.resolve_probe_candidate_proxies",
            resolver,
        ), mock.patch(
            "services.chatgpt_core.status_probe.probe_local_chatgpt_status",
            probe,
        ):
            result = platform.execute_action(
                "probe_local_status",
                account,
                {
                    "proxy_mode": "dynamic",
                    "proxy_failover": True,
                    "dynamic_proxy_max_attempts": 5,
                    "dynamic_proxy_template": "http://region-XX-sid-seed.proxy.test:8080",
                    "proxy_country_code": "US",
                },
            )

        self.assertTrue(result["ok"])
        self.assertEqual(resolver.call_count, 2)
        self.assertEqual(probe.call_count, 2)
        for call in resolver.call_args_list:
            self.assertFalse(call.args[0]["proxy_failover"])
            self.assertEqual(call.args[0]["dynamic_proxy_max_attempts"], 1)

    def test_platform_dynamic_config_snapshot_is_frozen_for_lazy_resolver(self):
        platform = ChatGPTPlatform(
            config=RegisterConfig(
                extra={
                    "task_proxy_mode": "dynamic",
                    "task_proxy_failover": True,
                    "dynamic_proxy_max_attempts": 3,
                    "dynamic_proxy_template": "http://region-XX-sid-seed.proxy.test:8080",
                    "dynamic_proxy_default_country": "US",
                    "dynamic_proxy_probe_enabled": False,
                    "dynamic_proxy_require_country_match": False,
                    "dynamic_proxy_probe_timeout_seconds": 11,
                    "dynamic_proxy_ip_retention_minutes": 12,
                }
            )
        )
        account = DummyAccount()
        resolver = mock.Mock(
            side_effect=[
                [("http://proxy.test/sid-first", None, "dynamic probe=disabled")],
                [("http://proxy.test/sid-second", None, "dynamic probe=disabled")],
            ]
        )
        probe = mock.Mock(
            side_effect=[
                RuntimeError("ProxyError: Connect timed out."),
                _successful_probe(),
            ]
        )

        with mock.patch(
            "core.proxy_utils.resolve_probe_candidate_proxies",
            resolver,
        ), mock.patch(
            "services.chatgpt_core.status_probe.probe_local_chatgpt_status",
            probe,
        ):
            result = platform.probe_local_status_with_candidates(
                account,
                {},
                manage_local_status_slots=False,
            )

        self.assertEqual(result["subscription"]["plan"], "plus")
        self.assertEqual(resolver.call_count, 2)
        for call in resolver.call_args_list:
            frozen = call.args[0]
            self.assertEqual(frozen["proxy_mode"], "dynamic")
            self.assertFalse(frozen["proxy_failover"])
            self.assertEqual(frozen["dynamic_proxy_max_attempts"], 1)
            self.assertEqual(
                frozen["dynamic_proxy_template"],
                "http://region-XX-sid-seed.proxy.test:8080",
            )
            self.assertEqual(frozen["proxy_country_code"], "US")
            self.assertFalse(frozen["dynamic_proxy_probe_enabled"])
            self.assertFalse(frozen["dynamic_proxy_require_country_match"])
            self.assertEqual(frozen["dynamic_proxy_probe_timeout_seconds"], 11)
            self.assertEqual(frozen["dynamic_proxy_ip_retention_minutes"], 12)
            self.assertEqual(call.kwargs["default_mode"], "direct")

    def test_execute_action_probe_local_status_raises_after_structured_transport_candidates_exhausted(self):
        platform = ChatGPTPlatform(config=RegisterConfig(extra={}))
        account = DummyAccount()
        proxy_pool = mock.Mock()
        transport_failure = {
            "auth": {
                "state": "probe_failed",
                "http_status": 0,
                "message": "curl: (28) connection timed out",
            },
            "subscription": {"plan": "unknown"},
            "codex": {"state": "not_checked"},
        }

        with mock.patch(
            "core.proxy_utils.resolve_probe_candidate_proxies",
            return_value=[("http://only-proxy:80", proxy_pool, "pool only")],
        ), mock.patch(
            "services.chatgpt_core.status_probe.probe_local_chatgpt_status",
            return_value=transport_failure,
        ):
            with self.assertRaisesRegex(RuntimeError, "connection timed out"):
                platform.execute_action(
                    "probe_local_status",
                    account,
                    {"proxy_mode": "pool"},
                )

        proxy_pool.report_fail.assert_called_once_with("http://only-proxy:80")
        proxy_pool.report_success.assert_not_called()

    def test_probe_candidate_state_reuses_sid_across_auth_revision_calls(self):
        platform = ChatGPTPlatform(config=RegisterConfig(extra={}))
        account = DummyAccount()
        candidate_state = {}
        resolver = mock.Mock(
            return_value=[("http://proxy.test/sid-stable", None, "dynamic probe=ok")]
        )
        probe = mock.Mock(return_value=_successful_probe())
        params = {
            "proxy_mode": "dynamic",
            "proxy_failover": True,
            "dynamic_proxy_max_attempts": 5,
            "dynamic_proxy_template": "http://region-XX-sid-seed.proxy.test:8080",
            "proxy_country_code": "US",
        }

        with mock.patch(
            "core.proxy_utils.resolve_probe_candidate_proxies",
            resolver,
        ), mock.patch(
            "services.chatgpt_core.status_probe.probe_local_chatgpt_status",
            probe,
        ):
            first = platform.probe_local_status_with_candidates(
                account,
                params,
                manage_local_status_slots=False,
                candidate_state=candidate_state,
            )
            second = platform.probe_local_status_with_candidates(
                account,
                params,
                manage_local_status_slots=False,
                candidate_state=candidate_state,
            )

        self.assertEqual(first["subscription"]["plan"], "plus")
        self.assertEqual(second["subscription"]["plan"], "plus")
        resolver.assert_called_once()
        self.assertEqual(
            [call.kwargs["proxy"] for call in probe.call_args_list],
            ["http://proxy.test/sid-stable", "http://proxy.test/sid-stable"],
        )

    def test_execute_action_probe_local_status_does_not_failover_business_response(self):
        platform = ChatGPTPlatform(config=RegisterConfig(extra={}))
        account = DummyAccount()
        first_pool = mock.Mock()
        second_pool = mock.Mock()
        business_probe = {
            "auth": {
                "state": "probe_failed",
                "http_status": 403,
                "message": "HTTP 403 account policy rejected",
            },
            "subscription": {"plan": "unknown"},
            "codex": {"state": "skipped_auth_invalid"},
        }

        with mock.patch(
            "core.proxy_utils.resolve_probe_candidate_proxies",
            return_value=[
                ("http://first-proxy:80", first_pool, "pool first"),
                ("http://second-proxy:80", second_pool, "pool second"),
            ],
        ), mock.patch(
            "services.chatgpt_core.status_probe.probe_local_chatgpt_status",
            return_value=business_probe,
        ) as probe:
            result = platform.execute_action("probe_local_status", account, {"proxy_mode": "pool"})

        self.assertTrue(result["ok"])
        self.assertIs(result["data"]["probe"], business_probe)
        probe.assert_called_once()
        first_pool.report_success.assert_called_once_with("http://first-proxy:80")
        first_pool.report_fail.assert_not_called()
        second_pool.report_success.assert_not_called()
        second_pool.report_fail.assert_not_called()

    def test_execute_action_probe_local_status_does_not_failover_oauth_429_exception(self):
        platform = ChatGPTPlatform(config=RegisterConfig(extra={}))
        account = DummyAccount()
        first_pool = mock.Mock()
        second_pool = mock.Mock()

        with mock.patch(
            "core.proxy_utils.resolve_probe_candidate_proxies",
            return_value=[
                ("http://first-proxy:80", first_pool, "pool first"),
                ("http://second-proxy:80", second_pool, "pool second"),
            ],
        ), mock.patch(
            "services.chatgpt_core.status_probe.probe_local_chatgpt_status",
            side_effect=RuntimeError("OAuth token 刷新失败: HTTP 429"),
        ) as probe:
            with self.assertRaisesRegex(RuntimeError, "HTTP 429"):
                platform.execute_action("probe_local_status", account, {"proxy_mode": "pool"})

        probe.assert_called_once()
        first_pool.report_success.assert_not_called()
        first_pool.report_fail.assert_not_called()
        second_pool.report_success.assert_not_called()
        second_pool.report_fail.assert_not_called()

    def test_legacy_batch_action_delegates_local_probe_to_task_runner(self):
        from api.actions import BatchActionRequest, execute_batch_action

        body = BatchActionRequest(
            account_ids=[1, 2],
            params={"delay_seconds": 0.5, "delay_max_seconds": 0.5},
        )
        background = mock.Mock()
        response = {"task_id": "task_probe_alias", "source": "batch_probe_local_status"}

        with mock.patch(
            "api.tasks.enqueue_batch_account_action_task",
            return_value=response,
        ) as enqueue, mock.patch("api.actions._resolve_batch_accounts") as resolve, mock.patch(
            "api.actions._execute_platform_action"
        ) as execute:
            result = execute_batch_action(
                "chatgpt",
                "probe_local_status",
                body,
                session=mock.Mock(),
                background_tasks=background,
            )

        request = enqueue.call_args.args[0]
        self.assertEqual(request.action_id, "probe_local_status")
        self.assertEqual(request.account_ids, [1, 2])
        self.assertEqual(request.params["delay_seconds"], 0.5)
        self.assertIs(enqueue.call_args.kwargs["background_tasks"], background)
        self.assertEqual(result, response)
        resolve.assert_not_called()
        execute.assert_not_called()

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

    def test_filtered_probe_limit_can_freeze_a_bounded_subset_from_a_larger_scope(self):
        from api.tasks import (
            LOCAL_STATUS_PROBE_MAX_ACCOUNTS,
            _resolve_batch_probe_local_status_accounts,
            BatchProbeLocalStatusTaskRequest,
        )

        accounts = [
            _RunnerAccount(account_id, extra={"access_token": f"at-{account_id}"})
            for account_id in range(1, LOCAL_STATUS_PROBE_MAX_ACCOUNTS + 2)
        ]
        req = BatchProbeLocalStatusTaskRequest(
            all_filtered=True,
            subscription_type="free",
            limit=345,
        )

        with mock.patch("api.tasks.Session"), mock.patch(
            "api.tasks._filtered_chatgpt_accounts",
            return_value=accounts,
        ):
            eligible, missing, skipped, matched = _resolve_batch_probe_local_status_accounts(req)

        self.assertEqual(len(eligible), 345)
        self.assertEqual(len(matched), LOCAL_STATUS_PROBE_MAX_ACCOUNTS + 1)
        self.assertEqual(len(skipped), LOCAL_STATUS_PROBE_MAX_ACCOUNTS + 1 - 345)
        self.assertEqual(missing, [])
        self.assertTrue(all("limit=345" in item["reason"] for item in skipped))

    def test_prepare_batch_probe_freezes_concurrency_and_expands_dynamic_candidates(self):
        from api.tasks import _prepare_batch_probe_local_status_params

        with mock.patch(
            "core.config_store.config_store.get_all",
            return_value={
                "task_proxy_mode": "dynamic",
                "dynamic_proxy_max_attempts": "2",
                "chatgpt_local_status_probe_concurrency": "4",
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
        self.assertEqual(settings["global_concurrency_limit"], 4)
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

    def test_prepare_batch_probe_explicit_legacy_dynamic_defaults_to_cliproxy(self):
        from api.tasks import _prepare_batch_probe_local_status_params

        with mock.patch(
            "core.config_store.config_store.get_all",
            return_value={
                "task_proxy_mode": "dynamic",
                "dynamic_proxy_provider": "miyaip",
                "dynamic_proxy_template": "http://region-XX-sid-seed.proxy.test:8080",
                "miyaip_crc": "crc-sensitive-value",
                "miyaip_key_name": "key-sensitive-value",
            },
        ):
            params, _settings = _prepare_batch_probe_local_status_params(
                {"proxy_mode": "dynamic"},
                eligible_count=1,
            )

        self.assertEqual(params["dynamic_proxy_provider"], "cliproxy")
        self.assertEqual(
            params["dynamic_proxy_template"],
            "http://region-XX-sid-seed.proxy.test:8080",
        )
        self.assertNotIn("miyaip_crc", params)
        self.assertNotIn("miyaip_key_name", params)

    def test_prepare_batch_probe_true_global_inheritance_uses_global_miyaip(self):
        from api.tasks import _prepare_batch_probe_local_status_params

        config = {
            "task_proxy_mode": "dynamic",
            "dynamic_proxy_provider": "miyaip",
            "miyaip_crc": "crc-sensitive-value",
            "miyaip_key_name": "key-sensitive-value",
            "miyaip_pool": "3",
            "miyaip_gateway_server": "eu",
            "miyaip_protocol": "socks5",
            "miyaip_request_timeout_seconds": "12",
        }
        with mock.patch("core.config_store.config_store.get_all", return_value=config):
            omitted, _settings = _prepare_batch_probe_local_status_params({}, eligible_count=1)
            inherited, _settings = _prepare_batch_probe_local_status_params(
                {"proxy_mode": "inherit"},
                eligible_count=1,
            )

        for params in (omitted, inherited):
            self.assertEqual(params["proxy_mode"], "dynamic")
            self.assertEqual(params["dynamic_proxy_provider"], "miyaip")
            self.assertEqual(params["miyaip_crc"], "crc-sensitive-value")
            self.assertEqual(params["miyaip_key_name"], "key-sensitive-value")
            self.assertEqual(params["miyaip_pool"], 3)
            self.assertEqual(params["miyaip_gateway_server"], "eu")
            self.assertEqual(params["miyaip_protocol"], "socks5")
            self.assertEqual(params["miyaip_request_timeout_seconds"], 12)

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
        limiter_events: list[str] = []

        class LimiterUpdateGuard:
            def __enter__(self):
                limiter_events.append("guard_enter")

            def __exit__(self, *_args):
                limiter_events.append("guard_exit")
                return False

        def capture_set_many(*_args, **_kwargs):
            limiter_events.append("set_many")

        def capture_configure(value):
            limiter_events.append(f"configure:{value}")

        with mock.patch.object(config_api.config_store, "get_all", return_value=current), mock.patch.object(
            config_api.config_store,
            "set_many",
            side_effect=capture_set_many,
        ) as set_many, mock.patch(
            "services.chatgpt_core.local_status_refresh.configure_local_status_concurrency",
            side_effect=capture_configure,
        ) as configure_concurrency, mock.patch(
            "services.chatgpt_core.local_status_refresh.local_status_concurrency_update_guard",
            return_value=LimiterUpdateGuard(),
        ) as update_guard:
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
        configure_concurrency.assert_called_once_with("3")
        update_guard.assert_called_once_with()
        self.assertEqual(
            limiter_events,
            ["guard_enter", "set_many", "configure:3", "guard_exit"],
        )

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

    def test_run_batch_probe_closes_db_sessions_before_parallel_network_probes(self):
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
        open_sessions = 0
        both_started = threading.Event()

        class TrackedSession(_RunnerSession):
            def __enter__(self):
                nonlocal open_sessions
                with state_lock:
                    open_sessions += 1
                return super().__enter__()

            def __exit__(self, *_args):
                nonlocal open_sessions
                with state_lock:
                    open_sessions -= 1
                return False

        def fake_probe(_account, **_kwargs):
            nonlocal active, max_active
            with state_lock:
                self.assertEqual(open_sessions, 0)
                active += 1
                max_active = max(max_active, active)
                if active == 2:
                    both_started.set()
            both_started.wait(timeout=1)
            time.sleep(0.03)
            with state_lock:
                active -= 1
            return _successful_probe()

        with mock.patch("api.tasks._task_store", store), mock.patch(
            "api.tasks.Session",
            side_effect=lambda *_args, **_kwargs: TrackedSession(accounts),
        ), mock.patch(
            "services.chatgpt_core.local_status_refresh.sync_chatgpt_account_local_status_by_id",
            side_effect=_run_detached_probe,
        ), mock.patch(
            "services.chatgpt_core.local_status_refresh.probe_chatgpt_account_local_status",
            side_effect=fake_probe,
        ), mock.patch(
            "core.config_store.config_store.get",
            return_value="2",
        ), mock.patch("api.tasks._save_task_log"):
            _run_batch_probe_local_status(
                "task_parallel",
                [1, 2],
                {
                    "proxy_mode": "direct",
                    "concurrency": 2,
                    "global_concurrency_limit": 2,
                    "unique_exit_ip_enabled": False,
                },
            )

        snapshot = store.snapshot("task_parallel")
        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["success"], 2)
        self.assertEqual(snapshot["meta"]["subscription_counts"], {"plus": 2, "free": 0, "unknown": 0})
        self.assertEqual(max_active, 2)
        self.assertEqual(open_sessions, 0)

    def test_run_batch_probe_rolls_next_account_while_another_worker_is_active(self):
        from api.tasks import _run_batch_probe_local_status

        accounts = {
            account_id: _RunnerAccount(
                account_id,
                extra={"chatgpt_browser_fingerprint": _browser_fingerprint(f"device-{account_id}")},
            )
            for account_id in range(1, 4)
        }
        store = RegisterTaskStore()
        store.create("task_rolling", platform="chatgpt", total=3, source="batch_probe_local_status", meta={})
        state_lock = threading.Lock()
        initial_started = threading.Event()
        third_started = threading.Event()
        release_second = threading.Event()
        active_ids: set[int] = set()
        max_active = 0

        def fake_probe(account, **_kwargs):
            nonlocal max_active
            account_id = int(account.id)
            with state_lock:
                active_ids.add(account_id)
                max_active = max(max_active, len(active_ids))
                if {1, 2}.issubset(active_ids):
                    initial_started.set()
            if account_id == 1:
                initial_started.wait(timeout=1)
            elif account_id == 2:
                initial_started.wait(timeout=1)
                release_second.wait(timeout=2)
            else:
                with state_lock:
                    self.assertIn(2, active_ids)
                third_started.set()
            with state_lock:
                active_ids.discard(account_id)
            return _successful_probe()

        with mock.patch("api.tasks._task_store", store), mock.patch(
            "api.tasks.Session",
            side_effect=lambda *_args, **_kwargs: _RunnerSession(accounts),
        ), mock.patch(
            "services.chatgpt_core.local_status_refresh.sync_chatgpt_account_local_status_by_id",
            side_effect=_run_detached_probe,
        ), mock.patch(
            "services.chatgpt_core.local_status_refresh.probe_chatgpt_account_local_status",
            side_effect=fake_probe,
        ), mock.patch(
            "core.config_store.config_store.get",
            return_value="2",
        ), mock.patch("api.tasks._save_task_log"):
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    _run_batch_probe_local_status,
                    "task_rolling",
                    [1, 2, 3],
                    {
                        "proxy_mode": "direct",
                        "concurrency": 2,
                        "global_concurrency_limit": 2,
                        "unique_exit_ip_enabled": False,
                    },
                )
                try:
                    self.assertTrue(initial_started.wait(timeout=1))
                    self.assertTrue(third_started.wait(timeout=1))
                finally:
                    release_second.set()
                future.result(timeout=3)

        self.assertEqual(store.snapshot("task_rolling")["success"], 3)
        self.assertEqual(max_active, 2)

    def test_run_batch_probe_logs_identity_retry_from_on_probe_start(self):
        from api.tasks import _run_batch_probe_local_status

        accounts = {
            1: _RunnerAccount(1, extra={"chatgpt_browser_fingerprint": _browser_fingerprint("device-1")}),
        }
        store = RegisterTaskStore()
        store.create("task_identity_retry", platform="chatgpt", total=1, source="batch_probe_local_status", meta={})
        probe = mock.Mock(return_value=_successful_probe())

        with mock.patch("api.tasks._task_store", store), mock.patch(
            "api.tasks.Session",
            side_effect=lambda *_args, **_kwargs: _RunnerSession(accounts),
        ), mock.patch(
            "services.chatgpt_core.local_status_refresh.sync_chatgpt_account_local_status_by_id",
            side_effect=_run_two_detached_probes,
        ), mock.patch(
            "services.chatgpt_core.local_status_refresh.probe_chatgpt_account_local_status",
            probe,
        ), mock.patch("api.tasks._save_task_log"):
            _run_batch_probe_local_status(
                "task_identity_retry",
                [1],
                {
                    "proxy_mode": "direct",
                    "concurrency": 1,
                    "global_concurrency_limit": 1,
                    "unique_exit_ip_enabled": False,
                },
            )

        snapshot = store.snapshot("task_identity_retry")
        start_logs = [line for line in snapshot["logs"] if "开始同步本地状态" in line]
        retry_logs = [line for line in snapshot["logs"] if "认证材料已更新，重新同步账号" in line]
        self.assertEqual(len(start_logs), 1)
        self.assertEqual(len(retry_logs), 1)
        self.assertIn("探测轮次=2", retry_logs[0])
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(snapshot["success"], 1)

    def test_dynamic_auth_revision_retry_reuses_sid_and_unique_exit_claim(self):
        from api.tasks import _run_batch_probe_local_status

        accounts = {
            1: _RunnerAccount(
                1,
                extra={"chatgpt_browser_fingerprint": _browser_fingerprint("device-1")},
            ),
        }
        store = RegisterTaskStore()
        store.create("task_revision_sid", platform="chatgpt", total=1, source="batch_probe_local_status", meta={})
        resolver = mock.Mock(
            return_value=[("http://proxy.test/sid-stable", None, "dynamic probe=ok")]
        )
        used_proxies: list[str] = []

        def successful_probe(_account, **kwargs):
            used_proxies.append(str(kwargs.get("proxy") or ""))
            return _successful_probe()

        with mock.patch("api.tasks._task_store", store), mock.patch(
            "api.tasks.Session",
            side_effect=lambda *_args, **_kwargs: _RunnerSession(accounts),
        ), mock.patch(
            "services.chatgpt_core.local_status_refresh.sync_chatgpt_account_local_status_by_id",
            side_effect=_run_two_detached_probes,
        ), mock.patch(
            "services.chatgpt_core.local_status_refresh.probe_chatgpt_account_local_status",
            side_effect=successful_probe,
        ), mock.patch(
            "core.proxy_utils.resolve_probe_candidate_proxies",
            resolver,
        ), mock.patch(
            "services.proxy_scanner.probe_basic",
            return_value={"ok": True, "exit_ip": "198.51.100.77"},
        ) as exit_probe, mock.patch("api.tasks._save_task_log"):
            _run_batch_probe_local_status(
                "task_revision_sid",
                [1, 999],
                {
                    "proxy_mode": "dynamic",
                    "proxy_failover": True,
                    "dynamic_proxy_max_attempts": 5,
                    "concurrency": 1,
                    "global_concurrency_limit": 1,
                    "unique_exit_ip_enabled": True,
                },
            )

        resolver.assert_called_once()
        exit_probe.assert_called_once()
        self.assertEqual(
            used_proxies,
            ["http://proxy.test/sid-stable", "http://proxy.test/sid-stable"],
        )
        snapshot = store.snapshot("task_revision_sid")
        self.assertEqual(snapshot["success"], 1)
        self.assertEqual(snapshot["meta"]["unique_exit_ip"]["assigned_count"], 1)
        self.assertEqual(snapshot["meta"]["unique_exit_ip"]["collision_count"], 0)
        self.assertIn(
            "reused",
            {event["status"] for event in snapshot["meta"]["unique_exit_ip"]["events"]},
        )

    def test_dynamic_auth_revision_retry_switches_only_after_reused_sid_transport_failure(self):
        from api.tasks import _run_batch_probe_local_status

        accounts = {
            1: _RunnerAccount(
                1,
                extra={"chatgpt_browser_fingerprint": _browser_fingerprint("device-1")},
            ),
        }
        store = RegisterTaskStore()
        store.create("task_revision_failover", platform="chatgpt", total=1, source="batch_probe_local_status", meta={})
        resolver = mock.Mock(
            side_effect=[
                [("http://proxy.test/sid-primary", None, "dynamic probe=ok")],
                [("http://proxy.test/sid-replacement", None, "dynamic probe=ok")],
            ]
        )
        used_proxies: list[str] = []

        def revision_probe(_account, **kwargs):
            proxy = str(kwargs.get("proxy") or "")
            used_proxies.append(proxy)
            if len(used_proxies) == 2:
                return {
                    "auth": {
                        "state": "probe_failed",
                        "http_status": 0,
                        "message": "curl: (28) connection timed out",
                    },
                    "subscription": {"plan": "unknown"},
                    "codex": {"state": "not_checked"},
                }
            return _successful_probe()

        with mock.patch("api.tasks._task_store", store), mock.patch(
            "api.tasks.Session",
            side_effect=lambda *_args, **_kwargs: _RunnerSession(accounts),
        ), mock.patch(
            "services.chatgpt_core.local_status_refresh.sync_chatgpt_account_local_status_by_id",
            side_effect=_run_two_detached_probes,
        ), mock.patch(
            "services.chatgpt_core.local_status_refresh.probe_chatgpt_account_local_status",
            side_effect=revision_probe,
        ), mock.patch(
            "core.proxy_utils.resolve_probe_candidate_proxies",
            resolver,
        ), mock.patch("api.tasks._save_task_log"):
            _run_batch_probe_local_status(
                "task_revision_failover",
                [1],
                {
                    "proxy_mode": "dynamic",
                    "proxy_failover": True,
                    "dynamic_proxy_max_attempts": 5,
                    "concurrency": 1,
                    "global_concurrency_limit": 1,
                    "unique_exit_ip_enabled": False,
                },
            )

        self.assertEqual(resolver.call_count, 2)
        self.assertEqual(
            used_proxies,
            [
                "http://proxy.test/sid-primary",
                "http://proxy.test/sid-primary",
                "http://proxy.test/sid-replacement",
            ],
        )
        self.assertEqual(store.snapshot("task_revision_failover")["success"], 1)

    def test_overlapping_batch_tasks_share_one_process_wide_concurrency_limit(self):
        from api.tasks import _run_batch_probe_local_status

        accounts = {
            account_id: _RunnerAccount(
                account_id,
                extra={"chatgpt_browser_fingerprint": _browser_fingerprint(f"device-{account_id}")},
            )
            for account_id in range(1, 5)
        }
        store = RegisterTaskStore()
        store.create("task_overlap_a", platform="chatgpt", total=2, source="batch_probe_local_status", meta={})
        store.create("task_overlap_b", platform="chatgpt", total=2, source="batch_probe_local_status", meta={})
        state_lock = threading.Lock()
        two_started = threading.Event()
        release = threading.Event()
        active = 0
        max_active = 0

        def fake_probe(_account, **_kwargs):
            nonlocal active, max_active
            with state_lock:
                active += 1
                max_active = max(max_active, active)
                if active == 2:
                    two_started.set()
            release.wait(timeout=2)
            with state_lock:
                active -= 1
            return _successful_probe()

        params = {
            "proxy_mode": "direct",
            "concurrency": 2,
            "global_concurrency_limit": 2,
            "unique_exit_ip_enabled": False,
        }
        with mock.patch("api.tasks._task_store", store), mock.patch(
            "api.tasks.Session",
            side_effect=lambda *_args, **_kwargs: _RunnerSession(accounts),
        ), mock.patch(
            "services.chatgpt_core.local_status_refresh.sync_chatgpt_account_local_status_by_id",
            side_effect=_run_detached_probe,
        ), mock.patch(
            "services.chatgpt_core.local_status_refresh.probe_chatgpt_account_local_status",
            side_effect=fake_probe,
        ), mock.patch(
            "core.config_store.config_store.get",
            return_value="2",
        ), mock.patch("api.tasks._save_task_log"):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(_run_batch_probe_local_status, "task_overlap_a", [1, 2], params),
                    pool.submit(_run_batch_probe_local_status, "task_overlap_b", [3, 4], params),
                ]
                try:
                    self.assertTrue(two_started.wait(timeout=1))
                    time.sleep(0.05)
                    with state_lock:
                        self.assertEqual(active, 2)
                        self.assertEqual(max_active, 2)
                finally:
                    release.set()
                for future in futures:
                    future.result(timeout=3)

        self.assertEqual(store.snapshot("task_overlap_a")["success"], 2)
        self.assertEqual(store.snapshot("task_overlap_b")["success"], 2)
        self.assertEqual(max_active, 2)

    def test_overlapping_tasks_serialize_the_same_persisted_fingerprint(self):
        from api.tasks import _run_batch_probe_local_status

        shared_fingerprint = _browser_fingerprint("shared-device")
        accounts = {
            1: _RunnerAccount(1, extra={"chatgpt_browser_fingerprint": shared_fingerprint}),
            2: _RunnerAccount(2, extra={"chatgpt_browser_fingerprint": shared_fingerprint}),
        }
        store = RegisterTaskStore()
        store.create("task_identity_a", platform="chatgpt", total=1, source="batch_probe_local_status", meta={})
        store.create("task_identity_b", platform="chatgpt", total=1, source="batch_probe_local_status", meta={})
        state_lock = threading.Lock()
        first_started = threading.Event()
        release = threading.Event()
        active = 0
        max_active = 0

        def fake_probe(_account, **_kwargs):
            nonlocal active, max_active
            with state_lock:
                active += 1
                max_active = max(max_active, active)
                first_started.set()
            release.wait(timeout=2)
            with state_lock:
                active -= 1
            return _successful_probe()

        params = {
            "proxy_mode": "direct",
            "concurrency": 1,
            "global_concurrency_limit": 2,
            "unique_exit_ip_enabled": False,
        }
        with mock.patch("api.tasks._task_store", store), mock.patch(
            "api.tasks.Session",
            side_effect=lambda *_args, **_kwargs: _RunnerSession(accounts),
        ), mock.patch(
            "services.chatgpt_core.local_status_refresh.sync_chatgpt_account_local_status_by_id",
            side_effect=_run_detached_probe,
        ), mock.patch(
            "services.chatgpt_core.local_status_refresh.probe_chatgpt_account_local_status",
            side_effect=fake_probe,
        ), mock.patch(
            "core.config_store.config_store.get",
            return_value="2",
        ), mock.patch("api.tasks._save_task_log"):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(_run_batch_probe_local_status, "task_identity_a", [1], params),
                    pool.submit(_run_batch_probe_local_status, "task_identity_b", [2], params),
                ]
                try:
                    self.assertTrue(first_started.wait(timeout=1))
                    time.sleep(0.05)
                    with state_lock:
                        self.assertEqual(active, 1)
                        self.assertEqual(max_active, 1)
                finally:
                    release.set()
                for future in futures:
                    future.result(timeout=3)

        self.assertEqual(store.snapshot("task_identity_a")["success"], 1)
        self.assertEqual(store.snapshot("task_identity_b")["success"], 1)
        self.assertEqual(max_active, 1)

    def test_run_batch_probe_serializes_legacy_fingerprint_fallback(self):
        from api.tasks import _run_batch_probe_local_status

        accounts = {1: _RunnerAccount(1), 2: _RunnerAccount(2)}
        store = RegisterTaskStore()
        store.create("task_legacy_fingerprint", platform="chatgpt", total=2, source="batch_probe_local_status", meta={})
        state_lock = threading.Lock()
        active = 0
        max_active = 0

        def fake_probe(_account, **_kwargs):
            nonlocal active, max_active
            with state_lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.04)
            with state_lock:
                active -= 1
            return _successful_probe("free")

        with mock.patch("api.tasks._task_store", store), mock.patch(
            "api.tasks.Session",
            side_effect=lambda *_args, **_kwargs: _RunnerSession(accounts),
        ), mock.patch(
            "services.chatgpt_core.local_status_refresh.sync_chatgpt_account_local_status_by_id",
            side_effect=_run_detached_probe,
        ), mock.patch(
            "services.chatgpt_core.local_status_refresh.probe_chatgpt_account_local_status",
            side_effect=fake_probe,
        ), mock.patch("api.tasks._save_task_log"):
            _run_batch_probe_local_status(
                "task_legacy_fingerprint",
                [1, 2],
                {
                    "proxy_mode": "direct",
                    "concurrency": 2,
                    "global_concurrency_limit": 2,
                    "unique_exit_ip_enabled": False,
                },
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
        probe = mock.Mock(return_value=_successful_probe())

        with mock.patch("api.tasks._task_store", store), mock.patch(
            "api.tasks.Session",
            side_effect=lambda *_args, **_kwargs: _RunnerSession(accounts),
        ), mock.patch(
            "services.chatgpt_core.local_status_refresh.sync_chatgpt_account_local_status_by_id",
            side_effect=_run_detached_probe,
        ), mock.patch(
            "services.chatgpt_core.local_status_refresh.probe_chatgpt_account_local_status",
            probe,
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
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(snapshot["success"], 1)
        self.assertEqual(len(snapshot["errors"]), 1)
        self.assertEqual(snapshot["meta"]["unique_exit_ip"]["collision_count"], 1)

    def test_dynamic_probe_generates_only_one_primary_sid_per_account(self):
        from api.tasks import _run_batch_probe_local_status

        accounts = {
            1: _RunnerAccount(1, extra={"chatgpt_browser_fingerprint": _browser_fingerprint("device-1")}),
            2: _RunnerAccount(2, extra={"chatgpt_browser_fingerprint": _browser_fingerprint("device-2")}),
        }
        store = RegisterTaskStore()
        store.create("task_lazy_sid", platform="chatgpt", total=2, source="batch_probe_local_status", meta={})
        resolver = mock.Mock(
            side_effect=[
                [("http://proxy.test/sid-primary-1", None, "dynamic probe=ok")],
                [("http://proxy.test/sid-primary-2", None, "dynamic probe=ok")],
            ]
        )

        with mock.patch("api.tasks._task_store", store), mock.patch(
            "api.tasks.Session",
            side_effect=lambda *_args, **_kwargs: _RunnerSession(accounts),
        ), mock.patch(
            "services.chatgpt_core.local_status_refresh.sync_chatgpt_account_local_status_by_id",
            side_effect=_run_detached_probe,
        ), mock.patch(
            "services.chatgpt_core.local_status_refresh.probe_chatgpt_account_local_status",
            return_value=_successful_probe(),
        ), mock.patch(
            "core.proxy_utils.resolve_probe_candidate_proxies",
            resolver,
        ), mock.patch("api.tasks._save_task_log"):
            _run_batch_probe_local_status(
                "task_lazy_sid",
                [1, 2],
                {
                    "proxy_mode": "dynamic",
                    "proxy_failover": True,
                    "dynamic_proxy_max_attempts": 5,
                    "concurrency": 1,
                    "global_concurrency_limit": 1,
                    "unique_exit_ip_enabled": False,
                },
            )

        self.assertEqual(resolver.call_count, 2)
        for call in resolver.call_args_list:
            self.assertFalse(call.args[0]["proxy_failover"])
            self.assertEqual(call.args[0]["dynamic_proxy_max_attempts"], 1)
        self.assertEqual(store.snapshot("task_lazy_sid")["success"], 2)

    def test_dynamic_proxy_failure_reuses_another_accounts_healthy_sid(self):
        from api.tasks import _run_batch_probe_local_status

        accounts = {
            1: _RunnerAccount(1, extra={"chatgpt_browser_fingerprint": _browser_fingerprint("device-1")}),
            2: _RunnerAccount(2, extra={"chatgpt_browser_fingerprint": _browser_fingerprint("device-2")}),
        }
        store = RegisterTaskStore()
        store.create("task_shared_sid", platform="chatgpt", total=2, source="batch_probe_local_status", meta={})
        resolver = mock.Mock(
            side_effect=[
                [("http://proxy.test/sid-healthy", None, "dynamic probe=ok")],
                [("http://proxy.test/sid-bad", None, "dynamic probe=ok")],
            ]
        )
        used_proxies: list[str] = []

        def fake_probe(_account, **kwargs):
            proxy = str(kwargs.get("proxy") or "")
            used_proxies.append(proxy)
            if proxy.endswith("sid-bad"):
                return {
                    "auth": {
                        "state": "probe_failed",
                        "http_status": 0,
                        "message": "curl: (28) connection timed out via proxy.example:429",
                    },
                    "subscription": {"plan": "unknown"},
                    "codex": {"state": "not_checked"},
                }
            return _successful_probe()

        with mock.patch("api.tasks._task_store", store), mock.patch(
            "api.tasks.Session",
            side_effect=lambda *_args, **_kwargs: _RunnerSession(accounts),
        ), mock.patch(
            "services.chatgpt_core.local_status_refresh.sync_chatgpt_account_local_status_by_id",
            side_effect=_run_detached_probe,
        ), mock.patch(
            "services.chatgpt_core.local_status_refresh.probe_chatgpt_account_local_status",
            side_effect=fake_probe,
        ), mock.patch(
            "core.proxy_utils.resolve_probe_candidate_proxies",
            resolver,
        ), mock.patch("api.tasks._save_task_log"):
            _run_batch_probe_local_status(
                "task_shared_sid",
                [1, 2],
                {
                    "proxy_mode": "dynamic",
                    "proxy_failover": True,
                    "dynamic_proxy_max_attempts": 5,
                    "concurrency": 1,
                    "global_concurrency_limit": 1,
                    "unique_exit_ip_enabled": False,
                },
            )

        self.assertEqual(resolver.call_count, 2)
        self.assertEqual(
            used_proxies,
            [
                "http://proxy.test/sid-healthy",
                "http://proxy.test/sid-bad",
                "http://proxy.test/sid-healthy",
            ],
        )
        self.assertEqual(store.snapshot("task_shared_sid")["success"], 2)

    def test_dynamic_probe_does_not_switch_sid_for_account_auth_failure(self):
        from api.tasks import _run_batch_probe_local_status

        accounts = {
            1: _RunnerAccount(1, extra={"chatgpt_browser_fingerprint": _browser_fingerprint("device-1")}),
        }
        store = RegisterTaskStore()
        store.create("task_auth_failure", platform="chatgpt", total=1, source="batch_probe_local_status", meta={})
        resolver = mock.Mock(
            return_value=[("http://proxy.test/sid-primary", None, "dynamic probe=ok")]
        )
        auth_failure = {
            "auth": {
                "state": "refresh_token_invalidated",
                "http_status": 401,
                "error_code": "token_invalidated",
                "message": "refresh token invalid",
            },
            "subscription": {"plan": "unknown"},
            "codex": {"state": "skipped_auth_invalid"},
        }

        with mock.patch("api.tasks._task_store", store), mock.patch(
            "api.tasks.Session",
            side_effect=lambda *_args, **_kwargs: _RunnerSession(accounts),
        ), mock.patch(
            "services.chatgpt_core.local_status_refresh.sync_chatgpt_account_local_status_by_id",
            side_effect=_run_detached_probe,
        ), mock.patch(
            "services.chatgpt_core.local_status_refresh.probe_chatgpt_account_local_status",
            return_value=auth_failure,
        ) as probe, mock.patch(
            "core.proxy_utils.resolve_probe_candidate_proxies",
            resolver,
        ), mock.patch("api.tasks._save_task_log"):
            _run_batch_probe_local_status(
                "task_auth_failure",
                [1],
                {
                    "proxy_mode": "dynamic",
                    "proxy_failover": True,
                    "dynamic_proxy_max_attempts": 5,
                    "concurrency": 1,
                    "global_concurrency_limit": 1,
                },
            )

        resolver.assert_called_once()
        probe.assert_called_once()
        snapshot = store.snapshot("task_auth_failure")
        self.assertEqual(snapshot["success"], 1)
        self.assertEqual(snapshot["meta"]["subscription_counts"], {"plus": 0, "free": 0, "unknown": 1})

    def test_dynamic_probe_does_not_switch_sid_for_structured_http_429(self):
        from api.tasks import _run_batch_probe_local_status

        accounts = {
            1: _RunnerAccount(1, extra={"chatgpt_browser_fingerprint": _browser_fingerprint("device-1")}),
        }
        store = RegisterTaskStore()
        store.create("task_http_429", platform="chatgpt", total=1, source="batch_probe_local_status", meta={})
        resolver = mock.Mock(
            return_value=[("http://proxy.test/sid-primary", None, "dynamic probe=ok")]
        )
        rate_limited_probe = {
            "auth": {
                "state": "probe_failed",
                "http_status": 429,
                "message": "OAuth authorize failed via proxy: HTTP 429 Too Many Requests",
            },
            "subscription": {"plan": "unknown"},
            "codex": {"state": "not_checked"},
        }

        with mock.patch("api.tasks._task_store", store), mock.patch(
            "api.tasks.Session",
            side_effect=lambda *_args, **_kwargs: _RunnerSession(accounts),
        ), mock.patch(
            "services.chatgpt_core.local_status_refresh.sync_chatgpt_account_local_status_by_id",
            side_effect=_run_detached_probe,
        ), mock.patch(
            "services.chatgpt_core.local_status_refresh.probe_chatgpt_account_local_status",
            return_value=rate_limited_probe,
        ) as probe, mock.patch(
            "core.proxy_utils.resolve_probe_candidate_proxies",
            resolver,
        ), mock.patch("api.tasks._save_task_log"):
            _run_batch_probe_local_status(
                "task_http_429",
                [1],
                {
                    "proxy_mode": "dynamic",
                    "proxy_failover": True,
                    "dynamic_proxy_max_attempts": 5,
                    "concurrency": 1,
                    "global_concurrency_limit": 1,
                },
            )

        resolver.assert_called_once()
        probe.assert_called_once()
        snapshot = store.snapshot("task_http_429")
        self.assertEqual(snapshot["success"], 0)
        self.assertEqual(len(snapshot["errors"]), 1)
        self.assertIn("HTTP 429", snapshot["errors"][0])
        self.assertEqual(snapshot["meta"]["subscription_counts"], {"plus": 0, "free": 0, "unknown": 1})

    def test_batch_probe_counts_persisted_subscription_when_codex_refresh_is_incomplete(self):
        from api.tasks import _run_batch_probe_local_status

        accounts = {
            1: _RunnerAccount(1, extra={"chatgpt_browser_fingerprint": _browser_fingerprint("device-1")}),
        }
        store = RegisterTaskStore()
        store.create("task_codex_partial", platform="chatgpt", total=1, source="batch_probe_local_status", meta={})
        partial_probe = {
            "auth": {"state": "access_token_valid", "http_status": 200},
            "subscription": {"plan": "free"},
            "codex": {
                "state": "probe_failed",
                "http_status": 503,
                "message": "Codex usage temporarily unavailable",
            },
        }

        with mock.patch("api.tasks._task_store", store), mock.patch(
            "api.tasks.Session",
            side_effect=lambda *_args, **_kwargs: _RunnerSession(accounts),
        ), mock.patch(
            "services.chatgpt_core.local_status_refresh.sync_chatgpt_account_local_status_by_id",
            side_effect=_run_detached_probe,
        ), mock.patch(
            "services.chatgpt_core.local_status_refresh.probe_chatgpt_account_local_status",
            return_value=partial_probe,
        ), mock.patch("api.tasks._save_task_log"):
            _run_batch_probe_local_status(
                "task_codex_partial",
                [1],
                {"proxy_mode": "direct", "concurrency": 1},
            )

        snapshot = store.snapshot("task_codex_partial")
        self.assertEqual(snapshot["success"], 0)
        self.assertEqual(len(snapshot["errors"]), 1)
        self.assertIn("Codex探测失败", snapshot["errors"][0])
        self.assertEqual(snapshot["meta"]["subscription_counts"], {"plus": 0, "free": 1, "unknown": 0})

    def test_dynamic_probe_does_not_switch_sid_for_oauth_http_429_exception(self):
        from api.tasks import _run_batch_probe_local_status

        accounts = {
            1: _RunnerAccount(1, extra={"chatgpt_browser_fingerprint": _browser_fingerprint("device-1")}),
        }
        store = RegisterTaskStore()
        store.create("task_oauth_429", platform="chatgpt", total=1, source="batch_probe_local_status", meta={})
        resolver = mock.Mock(
            return_value=[("http://proxy.test/sid-primary", None, "dynamic probe=ok")]
        )

        with mock.patch("api.tasks._task_store", store), mock.patch(
            "api.tasks.Session",
            side_effect=lambda *_args, **_kwargs: _RunnerSession(accounts),
        ), mock.patch(
            "services.chatgpt_core.local_status_refresh.sync_chatgpt_account_local_status_by_id",
            side_effect=_run_detached_probe,
        ), mock.patch(
            "services.chatgpt_core.local_status_refresh.probe_chatgpt_account_local_status",
            side_effect=RuntimeError(
                "OAuth token 刷新失败: HTTP 429"
            ),
        ) as probe, mock.patch(
            "core.proxy_utils.resolve_probe_candidate_proxies",
            resolver,
        ), mock.patch("api.tasks._save_task_log"):
            _run_batch_probe_local_status(
                "task_oauth_429",
                [1],
                {
                    "proxy_mode": "dynamic",
                    "proxy_failover": True,
                    "dynamic_proxy_max_attempts": 5,
                    "concurrency": 1,
                    "global_concurrency_limit": 1,
                },
            )

        resolver.assert_called_once()
        probe.assert_called_once()
        snapshot = store.snapshot("task_oauth_429")
        self.assertEqual(snapshot["success"], 0)
        self.assertEqual(len(snapshot["errors"]), 1)
        self.assertIn("HTTP 429", snapshot["errors"][0])

    def test_batch_probe_marks_structured_transport_failure_after_candidates_exhausted(self):
        from api.tasks import _run_batch_probe_local_status

        accounts = {
            1: _RunnerAccount(
                1,
                extra={"chatgpt_browser_fingerprint": _browser_fingerprint("device-1")},
            ),
        }
        store = RegisterTaskStore()
        store.create("task_transport_exhausted", platform="chatgpt", total=1, source="batch_probe_local_status", meta={})
        transport_failure = {
            "auth": {
                "state": "probe_failed",
                "http_status": 0,
                "message": "curl: (28) connection timed out",
            },
            "subscription": {"plan": "unknown"},
            "codex": {"state": "not_checked"},
        }

        with mock.patch("api.tasks._task_store", store), mock.patch(
            "api.tasks.Session",
            side_effect=lambda *_args, **_kwargs: _RunnerSession(accounts),
        ), mock.patch(
            "services.chatgpt_core.local_status_refresh.sync_chatgpt_account_local_status_by_id",
            side_effect=_run_detached_probe,
        ), mock.patch(
            "services.chatgpt_core.local_status_refresh.probe_chatgpt_account_local_status",
            return_value=transport_failure,
        ), mock.patch(
            "core.proxy_utils.resolve_probe_candidate_proxies",
            return_value=[("http://proxy.test/sid-only", None, "dynamic probe=ok")],
        ), mock.patch("api.tasks._save_task_log"):
            _run_batch_probe_local_status(
                "task_transport_exhausted",
                [1],
                {
                    "proxy_mode": "dynamic",
                    "proxy_failover": False,
                    "dynamic_proxy_max_attempts": 5,
                    "concurrency": 1,
                    "global_concurrency_limit": 1,
                },
            )

        snapshot = store.snapshot("task_transport_exhausted")
        self.assertEqual(snapshot["success"], 0)
        self.assertEqual(len(snapshot["errors"]), 1)
        self.assertIn("connection timed out", snapshot["errors"][0])

    @mock.patch("api.tasks._save_task_log")
    @mock.patch("api.tasks._task_store")
    @mock.patch("services.chatgpt_core.local_status_refresh.sync_chatgpt_account_local_status_by_id")
    @mock.patch("api.tasks.Session")
    def test_run_batch_probe_local_status_execution(self, mock_session_cls, mock_sync, mock_store, mock_save_log):
        from api.tasks import _run_batch_probe_local_status
        mock_acc = _RunnerAccount(10, extra={"chatgpt_browser_fingerprint": _browser_fingerprint("device-10")})
        mock_session_cls.return_value.__enter__.return_value.get.return_value = mock_acc
        mock_sync.return_value = {"probe": _successful_probe()}
        mock_store.snapshot.return_value = {"meta": {}}
        mock_store.control_for.return_value = mock.Mock()

        _run_batch_probe_local_status("task_test_probe", [10], {"proxy_mode": "direct"})
        mock_sync.assert_called_once()
        mock_store.finish.assert_called_once()

    @mock.patch("api.tasks._save_task_log")
    @mock.patch("api.tasks._task_store")
    @mock.patch("services.chatgpt_core.local_status_refresh.sync_chatgpt_account_local_status_by_id")
    @mock.patch("api.tasks.Session")
    def test_run_batch_probe_local_status_marks_unhandled_exception_failed(
        self,
        mock_session_cls,
        mock_sync,
        mock_store,
        mock_save_log,
    ):
        from api.tasks import _run_batch_probe_local_status

        mock_acc = _RunnerAccount(10, extra={"chatgpt_browser_fingerprint": _browser_fingerprint("device-10")})
        mock_session_cls.return_value.__enter__.return_value.get.return_value = mock_acc
        mock_sync.side_effect = _run_detached_probe
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
