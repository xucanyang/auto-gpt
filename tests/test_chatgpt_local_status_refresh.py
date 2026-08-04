import json
import tempfile
import unittest
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


class ChatGPTLocalStatusPersistenceTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "local-status-refresh.db"
        self.engine = create_engine(f"sqlite:///{db_path}")
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
                result = local_status_refresh.sync_chatgpt_account_local_status(session, account)

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


if __name__ == "__main__":
    unittest.main()
