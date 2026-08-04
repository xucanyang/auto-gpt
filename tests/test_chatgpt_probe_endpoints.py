import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from sqlmodel import Session, SQLModel, create_engine

from api import chatgpt as chatgpt_api
from core import db as core_db
from core.db import AccountModel
from services.chatgpt_core import local_status_refresh


def _complete_probe() -> dict:
    return {
        "version": 1,
        "auth": {"state": "refresh_token_valid", "http_status": 200},
        "subscription": {"plan": "free"},
        "codex": {"state": "usable", "http_status": 200},
    }


def _browser_fingerprint(device_id: str) -> dict:
    return {
        "device_id": device_id,
        "accept_language": "en-US,en;q=0.9",
        "impersonate": "chrome145",
        "chrome_major": 145,
        "chrome_full_version": "145.0.7632.6",
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/145.0.0.0 Safari/537.36"
        ),
        "sec_ch_ua": '"Chromium";v="145", "Google Chrome";v="145", "Not.A/Brand";v="99"',
        "platform_version": "15.0.0",
        "viewport_width": 1365,
        "viewport_height": 900,
    }


class ChatGPTProbeEndpointTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "chatgpt-probe-endpoints.db"
        self.engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
            pool_size=1,
            max_overflow=0,
            pool_timeout=0.2,
        )
        self.core_engine_patch = mock.patch.object(core_db, "engine", self.engine)
        self.core_engine_patch.start()
        self.config_patch = mock.patch(
            "core.config_store.config_store.get",
            return_value="1",
        )
        self.config_patch.start()
        SQLModel.metadata.create_all(self.engine)
        core_db.init_db()
        local_status_refresh.configure_local_status_concurrency(1)

    def tearDown(self):
        local_status_refresh.configure_local_status_concurrency(1)
        self.config_patch.stop()
        self.core_engine_patch.stop()
        self.engine.dispose()
        self._tmpdir.cleanup()

    def _create_account(self, email: str) -> int:
        with Session(self.engine) as session:
            account = AccountModel(
                platform="chatgpt",
                email=email,
                password="pw",
                token=f"at-{email}",
                user_id=f"acct-{email}",
                status="invalid",
                extra_json=json.dumps(
                    {
                        "access_token": f"at-{email}",
                        "refresh_token": f"rt-{email}",
                        "workspace_id": f"acct-{email}",
                        "chatgpt_browser_fingerprint": _browser_fingerprint(
                            f"device-{email}"
                        ),
                    }
                ),
            )
            session.add(account)
            session.commit()
            session.refresh(account)
            return int(account.id or 0)

    def _call_endpoint(self, endpoint, account_id: int):
        with Session(self.engine) as session:
            return endpoint(account_id, proxy="http://proxy.test:8080", session=session)

    def test_network_probe_holds_no_connection_and_preserves_endpoint_contracts(self):
        for endpoint, returns_ok in (
            (chatgpt_api.check_subscription, False),
            (chatgpt_api.probe_local_status, True),
        ):
            with self.subTest(endpoint=endpoint.__name__):
                email = f"{endpoint.__name__}@example.com"
                account_id = self._create_account(email)
                probe_started = threading.Event()
                release_probe = threading.Event()
                checked_out: list[int] = []
                observed_proxies: list[str] = []

                def blocked_probe(_account, **kwargs):
                    checked_out.append(self.engine.pool.checkedout())
                    observed_proxies.append(str(kwargs.get("proxy") or ""))
                    probe_started.set()
                    release_probe.wait(timeout=2)
                    return _complete_probe()

                with mock.patch.object(
                    local_status_refresh,
                    "probe_chatgpt_account_local_status",
                    side_effect=blocked_probe,
                ):
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        future = pool.submit(self._call_endpoint, endpoint, account_id)
                        try:
                            self.assertTrue(probe_started.wait(timeout=1))
                            self.assertEqual(self.engine.pool.checkedout(), 0)
                        finally:
                            release_probe.set()
                        response = future.result(timeout=2)

                self.assertEqual(checked_out, [0])
                self.assertEqual(observed_proxies, ["http://proxy.test:8080"])
                self.assertEqual(response["email"], email)
                self.assertEqual(response["probe"]["subscription"]["plan"], "free")
                if returns_ok:
                    self.assertTrue(response["ok"])
                else:
                    self.assertNotIn("ok", response)
                    self.assertEqual(response["subscription"], "free")

                with Session(self.engine) as session:
                    saved = session.get(AccountModel, account_id)
                    saved_extra = saved.get_extra()
                self.assertEqual(
                    saved_extra["chatgpt_local"]["subscription"]["plan"],
                    "free",
                )
                self.assertEqual(
                    saved_extra["chatgpt_capabilities"]["subscription_plan"],
                    "free",
                )
                self.assertNotEqual(saved.status, "invalid")

    def test_subscription_and_probe_local_share_process_wide_capacity(self):
        subscription_id = self._create_account("capacity-read@example.com")
        persistent_id = self._create_account("capacity-write@example.com")
        active = 0
        peak = 0
        active_lock = threading.Lock()
        first_entered = threading.Event()
        release_probes = threading.Event()

        def blocked_probe(_account, **_kwargs):
            nonlocal active, peak
            with active_lock:
                active += 1
                peak = max(peak, active)
                first_entered.set()
            release_probes.wait(timeout=2)
            with active_lock:
                active -= 1
            return _complete_probe()

        with mock.patch.object(
            local_status_refresh,
            "probe_chatgpt_account_local_status",
            side_effect=blocked_probe,
        ):
            with ThreadPoolExecutor(max_workers=2) as pool:
                subscription = pool.submit(
                    self._call_endpoint,
                    chatgpt_api.check_subscription,
                    subscription_id,
                )
                self.assertTrue(first_entered.wait(timeout=1))
                persistent = pool.submit(
                    self._call_endpoint,
                    chatgpt_api.probe_local_status,
                    persistent_id,
                )

                deadline = time.time() + 1
                while time.time() < deadline:
                    with local_status_refresh._LOCAL_STATUS_CAPACITY_CONDITION:
                        if local_status_refresh._LOCAL_STATUS_CAPACITY_WAITERS:
                            break
                    time.sleep(0.01)
                with local_status_refresh._LOCAL_STATUS_CAPACITY_CONDITION:
                    self.assertEqual(
                        len(local_status_refresh._LOCAL_STATUS_CAPACITY_WAITERS),
                        1,
                    )
                with active_lock:
                    self.assertEqual(active, 1)
                    self.assertEqual(peak, 1)
                self.assertEqual(self.engine.pool.checkedout(), 0)

                release_probes.set()
                subscription_result = subscription.result(timeout=2)
                persistent_result = persistent.result(timeout=2)

        self.assertEqual(peak, 1)
        self.assertEqual(subscription_result["subscription"], "free")
        self.assertTrue(persistent_result["ok"])


if __name__ == "__main__":
    unittest.main()
