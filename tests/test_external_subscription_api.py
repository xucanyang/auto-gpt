import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sqlmodel import Session, SQLModel, create_engine

from api import external_subscription as external_api
from core import db as core_db
from core.db import AccountModel


class ExternalSubscriptionApiTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "external_subscription.db"
        self.engine = create_engine(f"sqlite:///{db_path}")
        self.core_engine_patch = mock.patch.object(core_db, "engine", self.engine)
        self.core_engine_patch.start()
        SQLModel.metadata.create_all(self.engine)

    def tearDown(self):
        self.core_engine_patch.stop()
        self._tmpdir.cleanup()

    def _session(self):
        return Session(self.engine)

    def _add_account(self, *, email: str = "demo@example.com", status: str = "registered") -> int:
        extra = {
            "chatgpt_last_payment_link": {
                "url": "https://chatgpt.com/checkout/openai_llc/cs_live_123",
                "plan": "plus",
                "country": "US",
                "currency": "USD",
                "checkout_amount": "0",
                "checkout_amount_is_zero": True,
                "source": "batch_payment_link",
            }
        }
        with self._session() as session:
            row = AccountModel(
                platform="chatgpt",
                email=email,
                password="pw",
                status=status,
                cashier_url="https://chatgpt.com/checkout/openai_llc/cs_live_123",
                extra_json=json.dumps(extra, ensure_ascii=False),
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return int(row.id or 0)

    def test_claim_marks_account_pending_and_returns_minimal_link(self):
        account_id = self._add_account()
        with self._session() as session:
            result = external_api.claim_subscription_links(
                external_api.ClaimSubscriptionLinksRequest(consumer="worker-a", limit=1, country="US", currency="USD"),
                session=session,
            )

        self.assertEqual(result["count"], 1)
        item = result["items"][0]
        self.assertEqual(item["account_id"], account_id)
        self.assertEqual(item["email"], "demo@example.com")
        self.assertEqual(item["payment_link"], "https://chatgpt.com/checkout/openai_llc/cs_live_123")
        self.assertEqual(item["plan"], "plus")
        self.assertEqual(item["country"], "US")
        self.assertEqual(item["currency"], "USD")
        self.assertNotIn("password", item)
        self.assertTrue(item["claim_id"].startswith("subclaim_"))

        with self._session() as session:
            account = session.get(AccountModel, account_id)
            self.assertEqual(account.status, "pending_payment")
            claim = account.get_extra()["external_subscription_claim"]
            self.assertEqual(claim["status"], "claimed")
            self.assertEqual(claim["consumer"], "worker-a")

    def test_claim_skips_active_claim_until_released(self):
        self._add_account(email="first@example.com")
        second_id = self._add_account(email="second@example.com")

        with self._session() as session:
            first = external_api.claim_subscription_links(
                external_api.ClaimSubscriptionLinksRequest(consumer="worker-a", limit=1),
                session=session,
            )
            second = external_api.claim_subscription_links(
                external_api.ClaimSubscriptionLinksRequest(consumer="worker-b", limit=1),
                session=session,
            )

        self.assertEqual(first["count"], 1)
        self.assertEqual(second["count"], 1)
        self.assertEqual(second["items"][0]["account_id"], second_id)

    def test_release_allows_reclaim(self):
        self._add_account()
        with self._session() as session:
            claimed = external_api.claim_subscription_links(
                external_api.ClaimSubscriptionLinksRequest(consumer="worker-a", limit=1),
                session=session,
            )
            claim_id = claimed["items"][0]["claim_id"]
            released = external_api.release_subscription_claim(
                claim_id,
                external_api.ReleaseSubscriptionLinkRequest(reason="not needed"),
                session=session,
            )
            reclaimed = external_api.claim_subscription_links(
                external_api.ClaimSubscriptionLinksRequest(consumer="worker-b", limit=1),
                session=session,
            )

        self.assertTrue(released["released"])
        self.assertEqual(reclaimed["count"], 1)
        self.assertNotEqual(reclaimed["items"][0]["claim_id"], claim_id)

    def test_paid_result_marks_account_subscribed_and_is_idempotent(self):
        account_id = self._add_account(status="pending_payment")
        with self._session() as session:
            claimed = external_api.claim_subscription_links(
                external_api.ClaimSubscriptionLinksRequest(consumer="worker-a", limit=1),
                session=session,
            )
            claim_id = claimed["items"][0]["claim_id"]
            body = external_api.SubscriptionLinkResultRequest(
                status="paid",
                provider="external-pay",
                external_payment_id="pay_123",
                message="paid",
            )
            result = external_api.write_subscription_result(claim_id, body, session=session)
            repeated = external_api.write_subscription_result(claim_id, body, session=session)

        self.assertEqual(result["account_status"], "subscribed")
        self.assertFalse(result["idempotent"])
        self.assertTrue(repeated["idempotent"])
        with self._session() as session:
            account = session.get(AccountModel, account_id)
            extra = account.get_extra()
        self.assertEqual(account.status, "subscribed")
        self.assertEqual(extra["external_subscription_claim"]["status"], "paid")
        self.assertEqual(extra["external_subscription_payment"]["status"], "paid")
        self.assertEqual(extra["external_subscription_payment"]["external_payment_id"], "pay_123")

    def test_failed_result_marks_account_payment_failed(self):
        account_id = self._add_account(status="pending_payment")
        with self._session() as session:
            claimed = external_api.claim_subscription_links(
                external_api.ClaimSubscriptionLinksRequest(consumer="worker-a", limit=1),
                session=session,
            )
            claim_id = claimed["items"][0]["claim_id"]
            result = external_api.write_subscription_result(
                claim_id,
                external_api.SubscriptionLinkResultRequest(
                    status="failed",
                    external_payment_id="pay_failed",
                    error_code="declined",
                    message="card declined",
                ),
                session=session,
            )

        self.assertEqual(result["account_status"], "payment_failed")
        with self._session() as session:
            account = session.get(AccountModel, account_id)
            extra = account.get_extra()
        self.assertEqual(account.status, "payment_failed")
        self.assertEqual(extra["external_subscription_claim"]["status"], "failed")
        self.assertEqual(extra["external_subscription_payment"]["error_code"], "declined")

    def test_dedicated_token_dependency(self):
        with (
            mock.patch.object(external_api.config_store, "get", side_effect=lambda key, default="": {
                "external_subscription_api_enabled": "true",
                "external_subscription_api_token": "secret-token",
            }.get(key, default)),
        ):
            external_api._require_external_api_token("Bearer secret-token")
            with self.assertRaises(Exception):
                external_api._require_external_api_token("Bearer wrong")


if __name__ == "__main__":
    unittest.main()
