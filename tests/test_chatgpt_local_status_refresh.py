import json
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from sqlmodel import Session, SQLModel, create_engine

from core import db as core_db
from core.db import AccountModel
from services.chatgpt_core import local_status_refresh


def _probe(*, plan: str, active_until: str = "", auth_state: str = "refresh_token_valid") -> dict:
    return {
        "auth": {"state": auth_state},
        "subscription": {
            "plan": plan,
            "subscription_active_until": active_until,
        },
    }


class ChatGPTLocalStatusRefreshTests(unittest.TestCase):
    def _refresh(self, probes: list[dict]):
        with mock.patch.object(local_status_refresh, "probe_local_chatgpt_status", side_effect=probes) as probe_mock, mock.patch.object(
            local_status_refresh.time,
            "sleep",
        ) as sleep_mock:
            result = local_status_refresh._probe_local_status_with_subscription_retry(
                SimpleNamespace(),
                proxy=None,
                use_default_proxy=True,
            )
        return result, probe_mock, sleep_mock

    def test_unknown_plan_retries_and_persists_resolved_plus_subscription(self):
        result, probe_mock, sleep_mock = self._refresh(
            [
                _probe(plan="unknown"),
                _probe(plan="plus", active_until="2026-08-23T00:00:00Z"),
            ]
        )

        self.assertEqual(probe_mock.call_count, 2)
        sleep_mock.assert_called_once_with(local_status_refresh._SUBSCRIPTION_RETRY_DELAY_SECONDS)
        self.assertEqual(result["subscription"]["plan"], "plus")
        self.assertEqual(result["subscription"]["refresh_attempts"], 2)
        self.assertEqual(result["subscription"]["retry_reason"], "subscription_plan_unknown")
        self.assertEqual(result["subscription"]["retry_outcome"], "resolved")

    def test_paid_subscription_without_expiry_retries_once(self):
        result, probe_mock, sleep_mock = self._refresh(
            [
                _probe(plan="plus"),
                _probe(plan="plus", active_until="2026-08-23T00:00:00Z"),
            ]
        )

        self.assertEqual(probe_mock.call_count, 2)
        sleep_mock.assert_called_once_with(local_status_refresh._SUBSCRIPTION_RETRY_DELAY_SECONDS)
        self.assertEqual(result["subscription"]["subscription_active_until"], "2026-08-23T00:00:00Z")
        self.assertEqual(result["subscription"]["retry_reason"], "subscription_expiry_missing")
        self.assertEqual(result["subscription"]["retry_outcome"], "resolved")

    def test_unknown_plan_stays_unknown_after_one_bounded_retry(self):
        result, probe_mock, sleep_mock = self._refresh([_probe(plan="unknown"), _probe(plan="unknown")])

        self.assertEqual(probe_mock.call_count, 2)
        sleep_mock.assert_called_once_with(local_status_refresh._SUBSCRIPTION_RETRY_DELAY_SECONDS)
        self.assertEqual(result["subscription"]["plan"], "unknown")
        self.assertEqual(result["subscription"]["refresh_attempts"], 2)
        self.assertEqual(result["subscription"]["retry_outcome"], "still_incomplete")

    def test_confirmed_or_invalid_probe_does_not_retry(self):
        confirmed, confirmed_calls, confirmed_sleep = self._refresh(
            [_probe(plan="plus", active_until="2026-08-23T00:00:00Z")]
        )
        self.assertEqual(confirmed["subscription"]["plan"], "plus")
        confirmed_calls.assert_called_once()
        confirmed_sleep.assert_not_called()

        invalid, invalid_calls, invalid_sleep = self._refresh(
            [_probe(plan="unknown", auth_state="refresh_token_invalidated")]
        )
        self.assertEqual(invalid["subscription"]["plan"], "unknown")
        invalid_calls.assert_called_once()
        invalid_sleep.assert_not_called()

    def test_default_dynamic_sid_is_frozen_across_subscription_retry(self):
        first = _probe(plan="unknown")
        second = _probe(
            plan="plus",
            active_until="2026-09-04T03:00:00+00:00",
        )

        def configured_value(key, default=""):
            values = {
                "task_proxy_mode": "dynamic",
                "task_proxy_failover": "true",
                "dynamic_proxy_max_attempts": "5",
                "dynamic_proxy_template": (
                    "socks5://acct-region-Rand-sid-old-t-5:secret@proxy.example:3010"
                ),
                "dynamic_proxy_default_country": "US",
            }
            return values.get(key, default)

        with mock.patch(
            "core.config_store.config_store.get",
            side_effect=configured_value,
        ), mock.patch(
            "core.proxy_utils.resolve_probe_candidate_proxies",
            return_value=[
                (
                    "socks5h://account-sid-primary.example:3010",
                    None,
                    "dynamic probe=ok",
                )
            ],
        ) as resolve_candidates, mock.patch.object(
            local_status_refresh,
            "probe_local_chatgpt_status",
            side_effect=[first, second],
        ) as raw_probe, mock.patch.object(
            local_status_refresh.time,
            "sleep",
        ):
            result = local_status_refresh.probe_chatgpt_account_local_status(
                SimpleNamespace(),
                candidate_state={},
            )

        self.assertEqual(result["subscription"]["plan"], "plus")
        resolve_candidates.assert_called_once()
        self.assertEqual(raw_probe.call_count, 2)
        self.assertEqual(
            [call.kwargs.get("proxy") for call in raw_probe.call_args_list],
            [
                "socks5h://account-sid-primary.example:3010",
                "socks5h://account-sid-primary.example:3010",
            ],
        )
        self.assertTrue(
            all(
                call.kwargs.get("use_default_proxy") is False
                for call in raw_probe.call_args_list
            )
        )


class ChatGPTLocalStatusPersistenceTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "local-status-refresh.db"
        self.engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
            pool_size=1,
            max_overflow=0,
            pool_timeout=0.2,
        )
        self.core_engine_patch = mock.patch.object(core_db, "engine", self.engine)
        self.core_engine_patch.start()
        SQLModel.metadata.create_all(self.engine)
        core_db.init_db()

    def tearDown(self):
        self.core_engine_patch.stop()
        self._tmpdir.cleanup()

    def test_probe_restarts_when_auth_material_changes_before_persist(self):
        with Session(self.engine) as session:
            account = AccountModel(
                platform="chatgpt",
                email="refresh-race@example.com",
                password="pw",
                token="at-old",
                user_id="acct-1",
                status="invalid",
                extra_json=json.dumps(
                    {
                        "access_token": "at-old",
                        "refresh_token": "rt-old",
                        "workspace_id": "acct-1",
                        "chatgpt_local": {
                            "auth": {"state": "access_token_invalidated", "http_status": 401},
                            "subscription": {"plan": "unknown"},
                            "codex": {"state": "skipped_auth_invalid"},
                        },
                    }
                ),
            )
            session.add(account)
            session.commit()
            session.refresh(account)

            probes: list[str] = []

            def _probe_with_auth_rotation(probe_account, **_kwargs):
                probes.append(str(probe_account.refresh_token or ""))
                if len(probes) == 1:
                    extra = account.get_extra()
                    extra.update({"access_token": "at-new", "refresh_token": "rt-new"})
                    extra.pop("chatgpt_local", None)
                    account.token = "at-new"
                    account.status = "registered"
                    account.set_extra(extra)
                    session.add(account)
                    session.commit()
                    return _probe(plan="unknown", auth_state="access_token_invalidated")
                return {
                    "version": 1,
                    "checked_at": "2026-08-04T03:00:00+00:00",
                    "auth": {"state": "refresh_token_valid", "http_status": 200},
                    "subscription": {
                        "plan": "plus",
                        "subscription_active_until": "2026-09-04T03:00:00+00:00",
                    },
                    "codex": {"state": "usable", "http_status": 200},
                }

            with mock.patch.object(
                local_status_refresh,
                "_probe_local_status_with_subscription_retry",
                side_effect=_probe_with_auth_rotation,
            ):
                result = local_status_refresh.sync_chatgpt_account_local_status(
                    session,
                    account,
                    use_default_proxy=False,
                )

            account_id = account.id

        self.assertEqual(probes, ["rt-old", "rt-new"])
        self.assertEqual(result["probe"]["auth"]["state"], "refresh_token_valid")
        with Session(self.engine) as session:
            saved = session.get(AccountModel, account_id)
            list_state = session.get(core_db.AccountListStateModel, account_id)
            extra = saved.get_extra()

        self.assertEqual(saved.status, "subscribed")
        self.assertEqual(extra["chatgpt_local"]["auth"]["state"], "refresh_token_valid")
        self.assertEqual(list_state.account_validity, "valid")

    def test_incomplete_probe_cannot_overwrite_confirmed_subscription(self):
        confirmed_probe = {
            "version": 1,
            "auth": {"state": "refresh_token_valid", "http_status": 200},
            "subscription": {
                "plan": "plus",
                "subscription_active_until": "2026-09-04T03:00:00+00:00",
            },
            "codex": {"state": "usable"},
        }
        incomplete_probe = {
            "version": 1,
            "auth": {"state": "refresh_token_valid", "http_status": 200},
            "subscription": {"plan": "unknown"},
            "codex": {"state": "not_checked"},
        }
        with Session(self.engine) as session:
            account = AccountModel(
                platform="chatgpt",
                email="quality-guard@example.com",
                password="pw",
                token="at-quality",
                status="subscribed",
                extra_json=json.dumps(
                    {
                        "access_token": "at-quality",
                        "refresh_token": "rt-quality",
                        "chatgpt_local": confirmed_probe,
                        "chatgpt_capabilities": {
                            "auth_level": "refresh_token",
                            "subscription_plan": "plus",
                            "last_known_subscription_plan": "plus",
                        },
                    }
                ),
            )
            session.add(account)
            session.commit()
            session.refresh(account)
            result = local_status_refresh._persist_chatgpt_local_status_probe(
                session,
                account,
                incomplete_probe,
            )
            account_id = int(account.id or 0)

        self.assertEqual(result["refresh_outcome"], "unknown_plan")
        self.assertTrue(result["canonical_preserved"])
        self.assertFalse(result["probe_persisted"])
        self.assertEqual(result["probe"]["subscription"]["plan"], "unknown")
        self.assertEqual(result["canonical_probe"]["subscription"]["plan"], "plus")
        with Session(self.engine) as session:
            saved = session.get(AccountModel, account_id)
            extra = saved.get_extra()
        self.assertEqual(extra["chatgpt_local"]["subscription"]["plan"], "plus")
        self.assertEqual(extra["chatgpt_local_refresh"]["state"], "failed")
        self.assertEqual(extra["chatgpt_local_refresh"]["last_outcome"], "unknown_plan")
        self.assertTrue(extra["chatgpt_local_refresh"]["canonical_preserved"])

    def test_refresh_schedule_persists_without_proxy_secret(self):
        with Session(self.engine) as session:
            account = AccountModel(
                platform="chatgpt",
                email="durable-queue@example.com",
                password="pw",
                token="at-queue",
                status="registered",
                extra_json=json.dumps(
                    {"access_token": "at-queue", "refresh_token": "rt-queue"}
                ),
            )
            session.add(account)
            session.commit()
            session.refresh(account)
            account_id = int(account.id or 0)

        with mock.patch.object(
            local_status_refresh,
            "_start_local_status_refresh_worker",
            return_value=True,
        ) as start_worker:
            self.assertTrue(
                local_status_refresh.schedule_chatgpt_local_status_refresh_for_account_id(
                    account_id,
                    proxy="socks5h://user:secret@proxy.example:3010",
                    use_default_proxy=False,
                    reason="durable_test",
                    delay_seconds=2,
                )
            )

        start_worker.assert_called_once()
        with Session(self.engine) as session:
            job = session.get(local_status_refresh.ChatGPTLocalStatusRefreshJobModel, account_id)
            saved = session.get(AccountModel, account_id)
            meta = saved.get_extra()["chatgpt_local_refresh"]
        self.assertEqual(job.state, "pending")
        self.assertEqual(job.reason, "durable_test")
        self.assertNotIn("proxy.example", saved.get_extra()["chatgpt_local_refresh"])
        self.assertEqual(meta["state"], "pending")

    def test_durable_worker_retries_unknown_plan_and_reuses_selected_proxy(self):
        with Session(self.engine) as session:
            account = AccountModel(
                platform="chatgpt",
                email="durable-retry@example.com",
                password="pw",
                token="at-retry",
                status="registered",
                extra_json=json.dumps(
                    {"access_token": "at-retry", "refresh_token": "rt-retry"}
                ),
            )
            session.add(account)
            session.commit()
            session.refresh(account)
            account_id = int(account.id or 0)

        request = {
            "account_id": account_id,
            "proxy": "http://successful-login-proxy.example:18080",
            "use_default_proxy": False,
            "reason": "retry_test",
            "delay_seconds": 0.0,
        }
        job_info = local_status_refresh._enqueue_local_status_refresh_job(account_id, request)
        request["generation"] = int(job_info["generation"])
        unknown_result = {
            "refresh_outcome": "unknown_plan",
            "probe": {
                "auth": {"state": "refresh_token_valid"},
                "subscription": {"plan": "unknown"},
            },
        }
        confirmed_result = {
            "refresh_outcome": "confirmed",
            "probe": {
                "auth": {"state": "refresh_token_valid"},
                "subscription": {"plan": "free"},
            },
        }
        with mock.patch.object(
            local_status_refresh,
            "sync_chatgpt_account_local_status_by_id",
            side_effect=[unknown_result, unknown_result, confirmed_result],
        ) as sync_mock, mock.patch.object(
            local_status_refresh,
            "_LOCAL_STATUS_AUTO_RETRY_DELAYS_SECONDS",
            (0.0, 0.0),
        ):
            local_status_refresh._run_local_status_refresh_worker(request)

        self.assertEqual(sync_mock.call_count, 3)
        self.assertTrue(
            all(
                call.kwargs["proxy"] == "http://successful-login-proxy.example:18080"
                and call.kwargs["use_default_proxy"] is False
                for call in sync_mock.call_args_list
            )
        )
        with Session(self.engine) as session:
            job = session.get(local_status_refresh.ChatGPTLocalStatusRefreshJobModel, account_id)
            meta = session.get(AccountModel, account_id).get_extra()["chatgpt_local_refresh"]
        self.assertEqual(job.state, "succeeded")
        self.assertEqual(job.attempt_count, 3)
        self.assertEqual(job.last_outcome, "confirmed")
        self.assertEqual(meta["state"], "succeeded")
        self.assertEqual(meta["attempt_count"], 3)

    def test_startup_discovers_legacy_stale_rows_without_existing_jobs(self):
        with Session(self.engine) as session:
            account = AccountModel(
                platform="chatgpt",
                email="legacy-stale@example.com",
                password="pw",
                token="at-legacy",
                status="registered",
                extra_json=json.dumps(
                    {
                        "access_token": "at-legacy",
                        "chatgpt_local": {
                            "auth": {"state": "access_token_valid"},
                            "subscription": {"plan": "unknown"},
                        },
                        "chatgpt_capabilities": {
                            "auth_level": "access_token_only",
                            "subscription_plan": "unknown",
                            "last_known_subscription_plan": "free",
                        },
                    }
                ),
            )
            session.add(account)
            session.commit()
            session.refresh(account)
            account_id = int(account.id or 0)
            session.add(
                core_db.AccountListStateModel(
                    account_id=account_id,
                    platform="chatgpt",
                    subscription_type="unknown",
                    account_validity="valid",
                )
            )
            session.commit()

        with mock.patch.object(
            local_status_refresh,
            "schedule_chatgpt_local_status_refresh_for_account_id",
            return_value=True,
        ) as schedule_refresh:
            scheduled = local_status_refresh._schedule_legacy_stale_subscription_refreshes()

        self.assertEqual(scheduled, 1)
        schedule_refresh.assert_called_once_with(
            account_id,
            reason="startup_legacy_stale_subscription",
            delay_seconds=0.0,
        )

    def test_legacy_sync_releases_checked_out_connection_during_network_probe(self):
        with Session(self.engine) as session:
            account = AccountModel(
                platform="chatgpt",
                email="pool-release@example.com",
                password="pw",
                token="at-pool-release",
                user_id="acct-pool-release",
                status="registered",
                extra_json=json.dumps(
                    {
                        "access_token": "at-pool-release",
                        "refresh_token": "rt-pool-release",
                        "workspace_id": "acct-pool-release",
                    }
                ),
            )
            session.add(account)
            session.commit()
            account_id = account.id

        checked_out_during_probe: list[int] = []
        complete_probe = {
            "version": 1,
            "auth": {"state": "refresh_token_valid", "http_status": 200},
            "subscription": {
                "plan": "plus",
                "subscription_active_until": "2026-09-04T03:00:00+00:00",
            },
            "codex": {"state": "usable", "http_status": 200},
        }

        def probe_without_db_connection(_probe_account, **_kwargs):
            checked_out_during_probe.append(self.engine.pool.checkedout())
            return complete_probe

        with Session(self.engine) as session:
            account = session.get(AccountModel, account_id)
            self.assertEqual(self.engine.pool.checkedout(), 1)
            with mock.patch.object(
                local_status_refresh,
                "_probe_local_status_with_subscription_retry",
                side_effect=probe_without_db_connection,
            ), mock.patch(
                "core.config_store.config_store.get",
                return_value="2",
            ):
                result = local_status_refresh.sync_chatgpt_account_local_status(session, account)

        self.assertEqual(checked_out_during_probe, [0])
        self.assertEqual(result["probe"]["subscription"]["plan"], "plus")

    def test_legacy_sync_persists_before_releasing_identity_and_capacity(self):
        with Session(self.engine) as setup_session:
            account = AccountModel(
                platform="chatgpt",
                email="persist-under-lease@example.com",
                password="pw",
                token="at-persist",
                user_id="acct-persist",
                status="registered",
                extra_json=json.dumps(
                    {
                        "access_token": "at-persist",
                        "refresh_token": "rt-persist",
                        "workspace_id": "acct-persist",
                    }
                ),
            )
            setup_session.add(account)
            setup_session.commit()
            setup_session.refresh(account)
            account_id = int(account.id or 0)

        lease_events: list[str] = []
        complete_probe = {
            "auth": {"state": "refresh_token_valid", "http_status": 200},
            "subscription": {"plan": "free"},
            "codex": {"state": "usable", "http_status": 200},
        }

        @contextmanager
        def tracked_identity_slot(_probe_account, **_kwargs):
            lease_events.append("identity_enter")
            try:
                yield
            finally:
                lease_events.append("identity_exit")

        @contextmanager
        def tracked_capacity_slot(**_kwargs):
            lease_events.append("capacity_enter")
            try:
                yield
            finally:
                lease_events.append("capacity_exit")

        def tracked_persist(_session, _account, probe):
            lease_events.append("persist")
            return {"probe": probe}

        with Session(self.engine) as session:
            account = session.get(AccountModel, account_id)
            with mock.patch.object(
                local_status_refresh,
                "_probe_local_status_with_subscription_retry",
                return_value=complete_probe,
            ), mock.patch.object(
                local_status_refresh,
                "local_status_identity_slot",
                side_effect=tracked_identity_slot,
            ), mock.patch.object(
                local_status_refresh,
                "local_status_capacity_slot",
                side_effect=tracked_capacity_slot,
            ), mock.patch.object(
                local_status_refresh,
                "_persist_chatgpt_local_status_probe",
                side_effect=tracked_persist,
            ), mock.patch("core.config_store.config_store.get", return_value="1"):
                result = local_status_refresh.sync_chatgpt_account_local_status(
                    session,
                    account,
                )

        self.assertEqual(result["probe"]["subscription"]["plan"], "free")
        self.assertEqual(
            lease_events,
            [
                "identity_enter",
                "capacity_enter",
                "persist",
                "capacity_exit",
                "identity_exit",
            ],
        )

    def test_by_id_sync_holds_no_connection_while_probe_is_blocked(self):
        with Session(self.engine) as session:
            account = AccountModel(
                platform="chatgpt",
                email="by-id-pool-release@example.com",
                password="pw",
                token="at-by-id",
                user_id="acct-by-id",
                status="registered",
                extra_json=json.dumps(
                    {
                        "access_token": "at-by-id",
                        "refresh_token": "rt-by-id",
                        "workspace_id": "acct-by-id",
                    }
                ),
            )
            session.add(account)
            session.commit()
            account_id = account.id

        probe_started = threading.Event()
        release_probe = threading.Event()
        complete_probe = {
            "version": 1,
            "auth": {"state": "refresh_token_valid", "http_status": 200},
            "subscription": {
                "plan": "plus",
                "subscription_active_until": "2026-09-04T03:00:00+00:00",
            },
            "codex": {"state": "usable", "http_status": 200},
        }

        def blocked_probe(_probe_account):
            probe_started.set()
            release_probe.wait(timeout=2)
            return complete_probe

        with mock.patch("core.config_store.config_store.get", return_value="1"):
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    local_status_refresh.sync_chatgpt_account_local_status_by_id,
                    account_id,
                    probe_runner=blocked_probe,
                )
                try:
                    self.assertTrue(probe_started.wait(timeout=1))
                    self.assertEqual(self.engine.pool.checkedout(), 0)
                finally:
                    release_probe.set()
                result = future.result(timeout=2)

        self.assertEqual(result["probe"]["subscription"]["plan"], "plus")

    def test_action_probe_uses_by_id_lifecycle_without_holding_request_connection(self):
        from api import actions as actions_api
        from core.base_platform import RegisterConfig
        from services.chatgpt_core.plugin import ChatGPTPlatform

        with Session(self.engine) as setup_session:
            account = AccountModel(
                platform="chatgpt",
                email="action-pool-release@example.com",
                password="pw",
                token="at-action",
                user_id="acct-action",
                status="registered",
                extra_json=json.dumps(
                    {
                        "access_token": "at-action",
                        "refresh_token": "rt-action",
                        "workspace_id": "acct-action",
                    }
                ),
            )
            setup_session.add(account)
            setup_session.commit()
            account_id = int(account.id or 0)

        checked_out_during_probe: list[int] = []
        complete_probe = {
            "version": 1,
            "auth": {"state": "refresh_token_valid", "http_status": 200},
            "subscription": {
                "plan": "plus",
                "subscription_active_until": "2026-09-04T03:00:00+00:00",
            },
            "codex": {"state": "usable", "http_status": 200},
        }

        def probe_without_request_connection(*_args, **_kwargs):
            checked_out_during_probe.append(self.engine.pool.checkedout())
            return complete_probe

        platform = ChatGPTPlatform(config=RegisterConfig(extra={}))
        with Session(self.engine) as request_session:
            account = request_session.get(AccountModel, account_id)
            self.assertEqual(self.engine.pool.checkedout(), 1)
            with mock.patch(
                "core.config_store.config_store.get",
                return_value="2",
            ), mock.patch(
                "core.proxy_utils.resolve_probe_candidate_proxies",
                return_value=[("", None, "direct")],
            ), mock.patch(
                "services.chatgpt_core.status_probe.probe_local_chatgpt_status",
                side_effect=probe_without_request_connection,
            ), mock.patch.object(actions_api, "_apply_action_result") as apply_result:
                result = actions_api._execute_platform_action(
                    platform,
                    "chatgpt",
                    account,
                    "probe_local_status",
                    {"proxy_mode": "direct"},
                    request_session,
                )

            self.assertEqual(account.status, "subscribed")
            apply_result.assert_not_called()
            self.assertEqual(
                actions_api._action_local_status_refresh_ids(
                    "probe_local_status",
                    result,
                    account,
                ),
                [],
            )

        self.assertEqual(checked_out_during_probe, [0])
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["probe"]["subscription"]["plan"], "plus")
        with Session(self.engine) as verify_session:
            saved = verify_session.get(AccountModel, account_id)
            saved_extra = saved.get_extra()
        self.assertEqual(saved.status, "subscribed")
        self.assertEqual(saved_extra["chatgpt_local"]["auth"]["state"], "refresh_token_valid")

    def test_action_probe_transport_exhaustion_preserves_last_good_local_status(self):
        from api import actions as actions_api
        from core.base_platform import RegisterConfig
        from services.chatgpt_core.plugin import ChatGPTPlatform

        last_good = {
            "auth": {"state": "refresh_token_valid", "http_status": 200},
            "subscription": {"plan": "plus"},
            "codex": {"state": "usable", "http_status": 200},
        }
        with Session(self.engine) as setup_session:
            account = AccountModel(
                platform="chatgpt",
                email="action-preserve@example.com",
                password="pw",
                token="at-preserve",
                user_id="acct-preserve",
                status="subscribed",
                extra_json=json.dumps(
                    {
                        "access_token": "at-preserve",
                        "refresh_token": "rt-preserve",
                        "workspace_id": "acct-preserve",
                        "chatgpt_local": last_good,
                    }
                ),
            )
            setup_session.add(account)
            setup_session.commit()
            account_id = int(account.id or 0)

        failed_probe = {
            "auth": {
                "state": "probe_failed",
                "http_status": 0,
                "message": "curl: (28) connection timed out",
            },
            "subscription": {"plan": "unknown"},
            "codex": {"state": "not_checked"},
        }
        proxy_pool = mock.Mock()
        platform = ChatGPTPlatform(config=RegisterConfig(extra={}))
        with Session(self.engine) as request_session:
            account = request_session.get(AccountModel, account_id)
            with mock.patch(
                "core.config_store.config_store.get",
                return_value="1",
            ), mock.patch(
                "core.proxy_utils.resolve_probe_candidate_proxies",
                return_value=[("http://only-proxy:80", proxy_pool, "pool only")],
            ), mock.patch(
                "services.chatgpt_core.status_probe.probe_local_chatgpt_status",
                return_value=failed_probe,
            ):
                with self.assertRaisesRegex(RuntimeError, "connection timed out"):
                    actions_api._execute_platform_action(
                        platform,
                        "chatgpt",
                        account,
                        "probe_local_status",
                        {"proxy_mode": "pool"},
                        request_session,
                    )

        proxy_pool.report_fail.assert_called_once_with("http://only-proxy:80")
        proxy_pool.report_success.assert_not_called()
        with Session(self.engine) as verify_session:
            saved = verify_session.get(AccountModel, account_id)
            saved_extra = saved.get_extra()
        self.assertEqual(saved.status, "subscribed")
        self.assertEqual(saved_extra["chatgpt_local"], last_good)

    def test_batch_action_probe_uses_by_id_lifecycle_for_every_account(self):
        from api import actions as actions_api

        account_ids: list[int] = []
        with Session(self.engine) as setup_session:
            for index in range(2):
                account = AccountModel(
                    platform="chatgpt",
                    email=f"batch-action-{index}@example.com",
                    password="pw",
                    token=f"at-batch-{index}",
                    user_id=f"acct-batch-{index}",
                    status="registered",
                    extra_json=json.dumps(
                        {
                            "access_token": f"at-batch-{index}",
                            "refresh_token": f"rt-batch-{index}",
                            "workspace_id": f"acct-batch-{index}",
                        }
                    ),
                )
                setup_session.add(account)
                setup_session.commit()
                account_ids.append(int(account.id or 0))

        checked_out_during_probe: list[int] = []

        def probe_without_request_connection(*_args, **_kwargs):
            checked_out_during_probe.append(self.engine.pool.checkedout())
            return {
                "version": 1,
                "auth": {"state": "refresh_token_valid", "http_status": 200},
                "subscription": {
                    "plan": "plus",
                    "subscription_active_until": "2026-09-04T03:00:00+00:00",
                },
                "codex": {"state": "usable", "http_status": 200},
            }

        checked_out_during_delay: list[int] = []
        body = actions_api.BatchActionRequest(
            account_ids=account_ids,
            params={
                "proxy_mode": "direct",
                "delay_seconds": 0.05,
                "delay_max_seconds": 0.05,
            },
        )
        with Session(self.engine) as request_session, mock.patch(
            "core.config_store.config_store.get_all",
            return_value={},
        ), mock.patch(
            "core.config_store.config_store.get",
            return_value="2",
        ), mock.patch(
            "core.proxy_utils.resolve_probe_candidate_proxies",
            return_value=[("", None, "direct")],
        ), mock.patch(
            "services.chatgpt_core.status_probe.probe_local_chatgpt_status",
            side_effect=probe_without_request_connection,
        ), mock.patch.object(
            actions_api,
            "schedule_chatgpt_local_status_refresh_for_account_id",
        ) as schedule_refresh, mock.patch.object(
            actions_api.time,
            "sleep",
            side_effect=lambda _seconds: checked_out_during_delay.append(
                self.engine.pool.checkedout()
            ),
        ):
            result = actions_api.execute_batch_action(
                "chatgpt",
                "probe_local_status",
                body,
                session=request_session,
            )

        self.assertEqual(checked_out_during_probe, [0, 0])
        self.assertEqual(checked_out_during_delay, [0])
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["success"], 2)
        self.assertEqual(result["failed"], 0)
        self.assertEqual([item["status"] for item in result["items"]], ["subscribed", "subscribed"])
        schedule_refresh.assert_not_called()

        with Session(self.engine) as verify_session:
            saved_accounts = [verify_session.get(AccountModel, account_id) for account_id in account_ids]
        self.assertTrue(all(account.status == "subscribed" for account in saved_accounts))
        self.assertTrue(
            all(account.get_extra().get("chatgpt_local") for account in saved_accounts)
        )

    def test_by_id_sync_discards_probe_when_auth_material_changes(self):
        with Session(self.engine) as session:
            account = AccountModel(
                platform="chatgpt",
                email="by-id-auth-race@example.com",
                password="pw",
                token="at-old",
                user_id="acct-by-id-race",
                status="registered",
                extra_json=json.dumps(
                    {
                        "access_token": "at-old",
                        "refresh_token": "rt-old",
                        "workspace_id": "acct-by-id-race",
                    }
                ),
            )
            session.add(account)
            session.commit()
            account_id = account.id

        observed_refresh_tokens: list[str] = []
        lease_events: list[tuple[str, str]] = []

        @contextmanager
        def tracked_identity_slot(probe_account, **_kwargs):
            token = str(probe_account.refresh_token or "")
            lease_events.append(("identity_enter", token))
            try:
                yield
            finally:
                lease_events.append(("identity_exit", token))

        @contextmanager
        def tracked_capacity_slot(**_kwargs):
            token = observed_refresh_tokens[-1] if observed_refresh_tokens else ""
            lease_events.append(("capacity_enter", token))
            try:
                yield
            finally:
                lease_events.append(("capacity_exit", token))

        def rotating_probe(probe_account):
            observed_refresh_tokens.append(str(probe_account.refresh_token or ""))
            if len(observed_refresh_tokens) == 1:
                with Session(self.engine) as write_session:
                    current = write_session.get(AccountModel, account_id)
                    extra = current.get_extra()
                    extra.update({"access_token": "at-new", "refresh_token": "rt-new"})
                    current.token = "at-new"
                    current.set_extra(extra)
                    write_session.add(current)
                    write_session.commit()
                return _probe(plan="unknown", auth_state="refresh_token_invalidated")
            return {
                "version": 1,
                "auth": {"state": "refresh_token_valid", "http_status": 200},
                "subscription": {
                    "plan": "plus",
                    "subscription_active_until": "2026-09-04T03:00:00+00:00",
                },
                "codex": {"state": "usable", "http_status": 200},
            }

        with mock.patch(
            "core.config_store.config_store.get",
            return_value="1",
        ), mock.patch.object(
            local_status_refresh,
            "local_status_identity_slot",
            side_effect=tracked_identity_slot,
        ), mock.patch.object(
            local_status_refresh,
            "local_status_capacity_slot",
            side_effect=tracked_capacity_slot,
        ):
            result = local_status_refresh.sync_chatgpt_account_local_status_by_id(
                account_id,
                probe_runner=rotating_probe,
            )

        self.assertEqual(observed_refresh_tokens, ["rt-old", "rt-new"])
        self.assertEqual(result["probe"]["auth"]["state"], "refresh_token_valid")
        self.assertEqual(
            [event[0] for event in lease_events],
            [
                "identity_enter",
                "capacity_enter",
                "capacity_exit",
                "identity_exit",
                "identity_enter",
                "capacity_enter",
                "capacity_exit",
                "identity_exit",
            ],
        )
        self.assertEqual(
            [event[1] for event in lease_events if event[0] == "identity_enter"],
            ["rt-old", "rt-new"],
        )

    def test_by_id_default_probe_reuses_sid_when_organization_material_changes(self):
        with Session(self.engine) as session:
            account = AccountModel(
                platform="chatgpt",
                email="organization-race@example.com",
                password="pw",
                token="at-stable",
                user_id="acct-stable",
                status="registered",
                extra_json=json.dumps(
                    {
                        "access_token": "at-stable",
                        "refresh_token": "rt-stable",
                        "id_token": "id-old",
                        "organization_id": "org-old",
                        "workspace_id": "workspace-stable",
                        "chatgpt_workspace_scope": "scope-old",
                    }
                ),
            )
            session.add(account)
            session.commit()
            session.refresh(account)
            account_id = int(account.id or 0)

        observed: list[tuple[str, str]] = []

        def configured_value(key, default=""):
            values = {
                "chatgpt_local_status_probe_concurrency": "1",
                "task_proxy_mode": "dynamic",
                "task_proxy_failover": "true",
                "dynamic_proxy_max_attempts": "5",
                "dynamic_proxy_template": (
                    "socks5://acct-region-Rand-sid-old-t-5:secret@proxy.example:3010"
                ),
                "dynamic_proxy_default_country": "US",
            }
            return values.get(key, default)

        def rotating_organization_probe(probe_account, **kwargs):
            observed.append(
                (
                    str(probe_account.extra.get("id_token") or ""),
                    str(kwargs.get("proxy") or ""),
                )
            )
            if len(observed) == 1:
                with Session(self.engine) as write_session:
                    current = write_session.get(AccountModel, account_id)
                    extra = current.get_extra()
                    extra.update(
                        {
                            "id_token": "id-new",
                            "organization_id": "org-new",
                            "chatgpt_workspace_scope": "scope-new",
                        }
                    )
                    current.set_extra(extra)
                    write_session.add(current)
                    write_session.commit()
                return _probe(plan="free")
            return _probe(plan="plus", active_until="2026-09-04T03:00:00+00:00")

        with mock.patch(
            "core.config_store.config_store.get",
            side_effect=configured_value,
        ), mock.patch(
            "core.proxy_utils.resolve_probe_candidate_proxies",
            return_value=[
                (
                    "socks5h://account-sid-stable.example:3010",
                    None,
                    "dynamic probe=ok",
                )
            ],
        ) as resolve_candidates, mock.patch.object(
            local_status_refresh,
            "probe_local_chatgpt_status",
            side_effect=rotating_organization_probe,
        ):
            result = local_status_refresh.sync_chatgpt_account_local_status_by_id(
                account_id,
            )

        self.assertEqual(
            observed,
            [
                ("id-old", "socks5h://account-sid-stable.example:3010"),
                ("id-new", "socks5h://account-sid-stable.example:3010"),
            ],
        )
        resolve_candidates.assert_called_once()
        self.assertEqual(result["probe"]["subscription"]["plan"], "plus")

    def test_by_id_rejects_structured_transport_failure_without_overwriting_status(self):
        last_good = {
            "auth": {"state": "refresh_token_valid", "http_status": 200},
            "subscription": {"plan": "plus"},
            "codex": {"state": "usable", "http_status": 200},
        }
        with Session(self.engine) as session:
            account = AccountModel(
                platform="chatgpt",
                email="transport-guard@example.com",
                password="pw",
                token="at-transport-guard",
                user_id="acct-transport-guard",
                status="subscribed",
                extra_json=json.dumps(
                    {
                        "access_token": "at-transport-guard",
                        "refresh_token": "rt-transport-guard",
                        "workspace_id": "acct-transport-guard",
                        "chatgpt_local": last_good,
                    }
                ),
            )
            session.add(account)
            session.commit()
            session.refresh(account)
            account_id = int(account.id or 0)

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
            "core.config_store.config_store.get",
            return_value="1",
        ):
            with self.assertRaisesRegex(RuntimeError, "connection timed out"):
                local_status_refresh.sync_chatgpt_account_local_status_by_id(
                    account_id,
                    probe_runner=lambda _account: transport_failure,
                )

        with Session(self.engine) as session:
            saved = session.get(AccountModel, account_id)
            saved_extra = saved.get_extra()
        self.assertEqual(saved.status, "subscribed")
        self.assertEqual(saved_extra["chatgpt_local"], last_good)

    def test_by_id_sync_aborts_when_account_id_is_reused(self):
        original_created_at = datetime(2026, 8, 4, 1, 0, tzinfo=timezone.utc)
        with Session(self.engine) as session:
            account = AccountModel(
                platform="chatgpt",
                email="reused-by-id@example.com",
                password="old-password",
                token="at-old",
                user_id="acct-old",
                status="registered",
                created_at=original_created_at,
                extra_json=json.dumps(
                    {
                        "access_token": "at-old",
                        "refresh_token": "rt-old",
                        "workspace_id": "acct-old",
                    }
                ),
            )
            session.add(account)
            session.commit()
            session.refresh(account)
            account_id = int(account.id or 0)

        observed_tokens: list[str] = []

        def replace_row_during_probe(probe_account):
            observed_tokens.append(str(probe_account.refresh_token or ""))
            with Session(self.engine) as write_session:
                current = write_session.get(AccountModel, account_id)
                write_session.delete(current)
                write_session.commit()
                replacement = AccountModel(
                    id=account_id,
                    platform="chatgpt",
                    # Keep the email unchanged so created_at is what detects the
                    # replacement rather than an ordinary mutable field.
                    email="reused-by-id@example.com",
                    password="new-password",
                    token="at-replacement",
                    user_id="acct-replacement",
                    status="registered",
                    created_at=original_created_at + timedelta(seconds=1),
                    extra_json=json.dumps(
                        {
                            "access_token": "at-replacement",
                            "refresh_token": "rt-replacement",
                            "workspace_id": "acct-replacement",
                        }
                    ),
                )
                write_session.add(replacement)
                write_session.commit()
            return _probe(plan="free")

        with mock.patch("core.config_store.config_store.get", return_value="1"):
            with self.assertRaisesRegex(LookupError, "账号在探测期间已被替换"):
                local_status_refresh.sync_chatgpt_account_local_status_by_id(
                    account_id,
                    probe_runner=replace_row_during_probe,
                )

        self.assertEqual(observed_tokens, ["rt-old"])
        with Session(self.engine) as session:
            replacement = session.get(AccountModel, account_id)
            replacement_extra = replacement.get_extra()
        self.assertEqual(replacement.token, "at-replacement")
        self.assertNotIn("chatgpt_local", replacement_extra)

    def test_legacy_sync_aborts_when_refresh_resolves_to_reused_id(self):
        original_created_at = datetime(2026, 8, 4, 2, 0, tzinfo=timezone.utc)
        with Session(self.engine) as setup_session:
            account = AccountModel(
                platform="chatgpt",
                email="reused-legacy@example.com",
                password="old-password",
                token="at-old",
                user_id="acct-old",
                status="registered",
                created_at=original_created_at,
                extra_json=json.dumps(
                    {
                        "access_token": "at-old",
                        "refresh_token": "rt-old",
                        "workspace_id": "acct-old",
                    }
                ),
            )
            setup_session.add(account)
            setup_session.commit()
            setup_session.refresh(account)
            account_id = int(account.id or 0)

        observed_tokens: list[str] = []

        def replace_row_during_probe(probe_account, **_kwargs):
            observed_tokens.append(str(probe_account.refresh_token or ""))
            with Session(self.engine) as write_session:
                current = write_session.get(AccountModel, account_id)
                write_session.delete(current)
                write_session.commit()
                replacement = AccountModel(
                    id=account_id,
                    platform="chatgpt",
                    email="reused-legacy@example.com",
                    password="new-password",
                    token="at-replacement",
                    user_id="acct-replacement",
                    status="registered",
                    created_at=original_created_at + timedelta(seconds=1),
                    extra_json=json.dumps(
                        {
                            "access_token": "at-replacement",
                            "refresh_token": "rt-replacement",
                            "workspace_id": "acct-replacement",
                        }
                    ),
                )
                write_session.add(replacement)
                write_session.commit()
            return _probe(plan="free")

        with Session(self.engine) as session:
            account = session.get(AccountModel, account_id)
            with mock.patch.object(
                local_status_refresh,
                "_probe_local_status_with_subscription_retry",
                side_effect=replace_row_during_probe,
            ), mock.patch("core.config_store.config_store.get", return_value="1"):
                with self.assertRaisesRegex(LookupError, "账号在探测期间已被替换"):
                    local_status_refresh.sync_chatgpt_account_local_status(session, account)

        self.assertEqual(observed_tokens, ["rt-old"])
        with Session(self.engine) as session:
            replacement = session.get(AccountModel, account_id)
            replacement_extra = replacement.get_extra()
        self.assertEqual(replacement.token, "at-replacement")
        self.assertNotIn("chatgpt_local", replacement_extra)


class ChatGPTLocalStatusCapacityTests(unittest.TestCase):
    def tearDown(self):
        local_status_refresh.configure_local_status_concurrency(1)

    def test_process_wide_capacity_limits_overlapping_callers(self):
        local_status_refresh.configure_local_status_concurrency(2)
        state_lock = threading.Lock()
        two_entered = threading.Event()
        release = threading.Event()
        active = 0
        max_active = 0

        def worker():
            nonlocal active, max_active
            with local_status_refresh.local_status_capacity_slot():
                with state_lock:
                    active += 1
                    max_active = max(max_active, active)
                    if active == 2:
                        two_entered.set()
                release.wait(timeout=2)
                with state_lock:
                    active -= 1

        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(worker) for _ in range(6)]
            self.assertTrue(two_entered.wait(timeout=1))
            time.sleep(0.05)
            with state_lock:
                self.assertEqual(active, 2)
                self.assertEqual(max_active, 2)
            release.set()
            for future in futures:
                future.result(timeout=2)

        self.assertEqual(max_active, 2)

    def test_capacity_slot_is_released_after_exception(self):
        local_status_refresh.configure_local_status_concurrency(1)
        with self.assertRaisesRegex(RuntimeError, "boom"):
            with local_status_refresh.local_status_capacity_slot():
                raise RuntimeError("boom")

        with local_status_refresh.local_status_capacity_slot():
            pass

    def test_stopped_waiter_is_removed_without_blocking_next_caller(self):
        local_status_refresh.configure_local_status_concurrency(1)

        def stop_check():
            raise RuntimeError("stopped")

        def stopped_waiter():
            with local_status_refresh.local_status_capacity_slot(
                stop_check=stop_check,
            ):
                raise AssertionError("stopped waiter acquired a slot")

        with local_status_refresh.local_status_capacity_slot():
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(stopped_waiter)
                with self.assertRaisesRegex(RuntimeError, "stopped"):
                    future.result(timeout=1)
            with local_status_refresh._LOCAL_STATUS_CAPACITY_CONDITION:
                self.assertEqual(local_status_refresh._LOCAL_STATUS_CAPACITY_WAITERS, [])

        with local_status_refresh.local_status_capacity_slot():
            pass

    def test_in_flight_old_config_read_cannot_overwrite_authoritative_update(self):
        local_status_refresh.configure_local_status_concurrency(1)
        read_started = threading.Event()
        release_read = threading.Event()

        def blocked_old_read(_key, _default):
            read_started.set()
            release_read.wait(timeout=2)
            return "1"

        with mock.patch(
            "core.config_store.config_store.get",
            side_effect=blocked_old_read,
        ):
            with ThreadPoolExecutor(max_workers=2) as pool:
                old_refresh = pool.submit(
                    local_status_refresh.refresh_local_status_concurrency_from_store
                )
                self.assertTrue(read_started.wait(timeout=1))
                authoritative_update = pool.submit(
                    local_status_refresh.configure_local_status_concurrency,
                    4,
                )
                time.sleep(0.05)
                self.assertFalse(authoritative_update.done())
                release_read.set()
                self.assertEqual(old_refresh.result(timeout=1), 1)
                self.assertEqual(authoritative_update.result(timeout=1), 4)

        with local_status_refresh._LOCAL_STATUS_CAPACITY_CONDITION:
            self.assertEqual(local_status_refresh._LOCAL_STATUS_CAPACITY_LIMIT, 4)

    def test_config_read_failure_preserves_current_capacity(self):
        local_status_refresh.configure_local_status_concurrency(3)

        with mock.patch(
            "core.config_store.config_store.get",
            side_effect=RuntimeError("config unavailable"),
        ):
            effective = local_status_refresh.refresh_local_status_concurrency_from_store()

        self.assertEqual(effective, 3)
        with local_status_refresh._LOCAL_STATUS_CAPACITY_CONDITION:
            self.assertEqual(local_status_refresh._LOCAL_STATUS_CAPACITY_LIMIT, 3)


if __name__ == "__main__":
    unittest.main()
