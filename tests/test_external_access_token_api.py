import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sqlmodel import Session, SQLModel, create_engine, select

from api import external_access_tokens as external_api
from core import db as core_db
from core import config_store as config_store_module
from core.config_store import config_store
from core.db import AccountModel, ExternalAccessTokenClaimModel


def free_probe():
    return {
        "checked_at": "2026-06-09T12:00:00+00:00",
        "auth": {
            "state": "access_token_valid",
            "checked_at": "2026-06-09T12:00:00+00:00",
            "source": "access_token",
            "http_status": 200,
            "message": "ok",
        },
        "subscription": {
            "plan": "free",
            "checked_at": "2026-06-09T12:00:00+00:00",
            "source": "backend_me",
        },
        "codex": {"state": "not_checked"},
    }


def plus_probe():
    probe = free_probe()
    probe["subscription"] = {
        "plan": "plus",
        "checked_at": "2026-06-09T12:00:00+00:00",
        "source": "backend_me",
    }
    return probe


class ExternalAccessTokenApiTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "external_access_token.db"
        self.engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False, "timeout": 30},
        )
        self.core_engine_patch = mock.patch.object(core_db, "engine", self.engine)
        self.core_engine_patch.start()
        self.config_engine_patch = mock.patch.object(config_store_module, "engine", self.engine)
        self.config_engine_patch.start()
        SQLModel.metadata.create_all(self.engine)
        core_db._ensure_external_access_token_claim_schema()
        config_store.set_many({
            "external_access_token_api_enabled": "true",
            "external_access_token_api_token": "secret-token",
            "external_access_token_allow_refresh": "false",
            "external_access_token_default_lease_seconds": "86400",
            "external_access_token_max_limit": "50",
            "external_access_token_precheck_cooldown_seconds": "600",
        })
        self.probe_patch = mock.patch.object(external_api, "_probe_account_status", return_value=free_probe())
        self.probe_mock = self.probe_patch.start()

    def tearDown(self):
        self.probe_patch.stop()
        self.config_engine_patch.stop()
        self.core_engine_patch.stop()
        self._tmpdir.cleanup()

    def _session(self):
        return Session(self.engine)

    def _add_account(self, email="demo@example.com", *, access_token="at-1", status="registered") -> int:
        extra = {
            "access_token": access_token,
            "auth_level": "access_token_only",
        }
        with self._session() as session:
            row = AccountModel(
                platform="chatgpt",
                email=email,
                password="pw",
                token=access_token,
                status=status,
                extra_json=json.dumps(extra, ensure_ascii=False),
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return int(row.id or 0)

    def test_claim_returns_email_and_access_token_after_live_free_check(self):
        account_id = self._add_account(email="free@example.com", access_token="at-free")
        with self._session() as session:
            result = external_api.claim_access_tokens(
                external_api.ClaimAccessTokensRequest(consumer="worker-a", limit=1, lease_seconds=3600),
                session=session,
            )

        self.assertEqual(result["count"], 1)
        item = result["items"][0]
        self.assertEqual(item["account_id"], account_id)
        self.assertEqual(item["email"], "free@example.com")
        self.assertEqual(item["access_token"], "at-free")
        self.assertEqual(item["subscription_plan"], "free")
        self.assertTrue(item["claim_id"].startswith("atclaim_"))

        with self._session() as session:
            account = session.get(AccountModel, account_id)
            extra = account.get_extra()
            claim = extra[external_api.EXTERNAL_AT_CLAIM_KEY]
            row = session.exec(
                select(ExternalAccessTokenClaimModel)
                .where(ExternalAccessTokenClaimModel.claim_id == claim["claim_id"])
            ).first()
        self.assertEqual(account.status, "registered")
        self.assertEqual(claim["status"], "claimed")
        self.assertEqual(claim["email"], "free@example.com")
        self.assertEqual(row.status, "claimed")
        self.assertEqual(row.email, "free@example.com")
        self.assertIn("sha256:", row.token_fingerprint)
        self.assertNotIn("at-free", row.token_fingerprint)

    def test_claim_does_not_repeat_same_access_token_in_one_round(self):
        first_id = self._add_account(email="first@example.com", access_token="same-at")
        self._add_account(email="second@example.com", access_token="same-at")

        with self._session() as session:
            result = external_api.claim_access_tokens(
                external_api.ClaimAccessTokensRequest(consumer="worker-a", limit=2),
                session=session,
            )

        self.assertEqual(result["count"], 1)
        self.assertEqual(result["items"][0]["account_id"], first_id)
        with self._session() as session:
            rows = session.exec(select(ExternalAccessTokenClaimModel).order_by(ExternalAccessTokenClaimModel.id.asc())).all()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].status, "claimed")
        self.assertEqual(rows[1].status, "duplicate_in_claim_round")

    def test_claim_rejects_subscribed_live_probe_before_sending(self):
        account_id = self._add_account(email="plus@example.com", access_token="at-plus")
        self.probe_mock.return_value = plus_probe()

        with self._session() as session:
            result = external_api.claim_access_tokens(
                external_api.ClaimAccessTokensRequest(consumer="worker-a", limit=1),
                session=session,
            )

        self.assertEqual(result["count"], 0)
        with self._session() as session:
            account = session.get(AccountModel, account_id)
            row = session.exec(
                select(ExternalAccessTokenClaimModel)
                .where(ExternalAccessTokenClaimModel.account_id == account_id)
            ).first()
        self.assertEqual(account.status, "subscribed")
        self.assertEqual(row.status, "subscribed")

    def test_paid_result_triggers_local_status_refresh(self):
        account_id = self._add_account(email="paid@example.com", access_token="at-paid")
        with self._session() as session:
            result = external_api.claim_access_tokens(
                external_api.ClaimAccessTokensRequest(consumer="worker-a", limit=1),
                session=session,
            )
        claim_id = result["items"][0]["claim_id"]

        def fake_refresh(session, account, extra):
            account.status = "subscribed"
            extra["chatgpt_local"] = plus_probe()
            account.set_extra(extra)
            session.add(account)
            session.commit()
            session.refresh(account)
            return plus_probe()

        with mock.patch.object(external_api, "_refresh_account_local_status", side_effect=fake_refresh) as refresh_mock:
            with self._session() as session:
                written = external_api.write_access_token_result(
                    claim_id,
                    external_api.AccessTokenResultRequest(status="paid", external_payment_id="pay_123", message="ok"),
                    session=session,
                )

        self.assertEqual(written["email"], "paid@example.com")
        self.assertEqual(written["account_status"], "subscribed")
        refresh_mock.assert_called_once()
        with self._session() as session:
            account = session.get(AccountModel, account_id)
            row = session.exec(
                select(ExternalAccessTokenClaimModel)
                .where(ExternalAccessTokenClaimModel.claim_id == claim_id)
            ).first()
        self.assertEqual(account.status, "subscribed")
        self.assertEqual(row.status, "paid")

    def test_failed_result_does_not_change_account_status_or_refresh(self):
        account_id = self._add_account(email="failed@example.com", access_token="at-failed")
        with self._session() as session:
            result = external_api.claim_access_tokens(
                external_api.ClaimAccessTokensRequest(consumer="worker-a", limit=1),
                session=session,
            )
        claim_id = result["items"][0]["claim_id"]

        with mock.patch.object(external_api, "_refresh_account_local_status") as refresh_mock:
            with self._session() as session:
                written = external_api.write_access_token_result(
                    claim_id,
                    external_api.AccessTokenResultRequest(status="failed", external_payment_id="pay_123", error_code="declined"),
                    session=session,
                )

        self.assertEqual(written["email"], "failed@example.com")
        self.assertEqual(written["account_status"], "registered")
        refresh_mock.assert_not_called()
        with self._session() as session:
            account = session.get(AccountModel, account_id)
            row = session.exec(
                select(ExternalAccessTokenClaimModel)
                .where(ExternalAccessTokenClaimModel.claim_id == claim_id)
            ).first()
        self.assertEqual(account.status, "registered")
        self.assertEqual(row.status, "failed")

    def test_get_claim_does_not_return_access_token_again(self):
        self._add_account(email="get@example.com", access_token="at-get")
        with self._session() as session:
            result = external_api.claim_access_tokens(
                external_api.ClaimAccessTokensRequest(consumer="worker-a", limit=1),
                session=session,
            )
            claim_id = result["items"][0]["claim_id"]
            fetched = external_api.get_access_token_claim(claim_id, session=session)

        self.assertEqual(fetched["email"], "get@example.com")
        self.assertNotIn("access_token", fetched["claim"])
        self.assertNotIn("at-get", json.dumps(fetched, ensure_ascii=False))

    def test_auth_guard_requires_dedicated_token(self):
        config_store.set_many({
            "external_access_token_api_enabled": "true",
            "external_access_token_api_token": "secret-token",
        })
        external_api._require_external_api_token("Bearer secret-token")
        with self.assertRaises(Exception) as ctx:
            external_api._require_external_api_token("Bearer wrong")
        self.assertEqual(getattr(ctx.exception, "status_code", 0), 401)


if __name__ == "__main__":
    unittest.main()
