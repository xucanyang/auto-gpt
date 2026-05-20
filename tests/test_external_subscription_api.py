import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from sqlmodel import Session, SQLModel, create_engine, select

from api import external_subscription as external_api
from core import db as core_db
from core.db import AccountModel, ExternalSubscriptionClaimModel


class ExternalSubscriptionApiTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "external_subscription.db"
        self.engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False, "timeout": 30},
        )
        self.core_engine_patch = mock.patch.object(core_db, "engine", self.engine)
        self.core_engine_patch.start()
        SQLModel.metadata.create_all(self.engine)
        core_db._ensure_external_subscription_claim_schema()
        self.preflight_patch = mock.patch.object(
            external_api,
            "_preflight_subscription_link",
            return_value={
                "ok_to_send": True,
                "link_status": "available",
                "reason": "",
                "probe": {"currency": "usd", "amount": 0, "amount_text": "0", "amount_is_zero": True},
                "checkout_amount": "0",
                "checkout_amount_is_zero": True,
            },
        )
        self.schedule_patch = mock.patch.object(external_api, "_schedule_subscription_verification")
        self.upsert_patch = mock.patch.object(external_api, "_upsert_pending_subscription_auth")
        self.preflight_mock = self.preflight_patch.start()
        self.schedule_mock = self.schedule_patch.start()
        self.upsert_mock = self.upsert_patch.start()

    def tearDown(self):
        self.upsert_patch.stop()
        self.schedule_patch.stop()
        self.preflight_patch.stop()
        self.core_engine_patch.stop()
        self._tmpdir.cleanup()

    def _session(self):
        return Session(self.engine)

    def _add_account(
        self,
        *,
        email: str = "demo@example.com",
        status: str = "registered",
        currency: str = "USD",
        link_status: str = "",
    ) -> int:
        extra = {
            "chatgpt_last_payment_link": {
                "url": "https://chatgpt.com/checkout/openai_llc/cs_live_123",
                "plan": "plus",
                "country": "US",
                "currency": currency,
                "checkout_amount": "0",
                "checkout_amount_is_zero": True,
                "source": "batch_payment_link",
            }
        }
        if link_status:
            extra["chatgpt_last_payment_link"]["link_status"] = link_status
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
            self.assertIn("verify_after_at", claim)
            link = account.get_extra()["chatgpt_last_payment_link"]
            self.assertEqual(link["link_status"], "leased")
            self.assertEqual(link["claim_id"], claim["claim_id"])
            row = session.exec(
                select(ExternalSubscriptionClaimModel)
                .where(ExternalSubscriptionClaimModel.claim_id == claim["claim_id"])
            ).first()
            self.assertIsNotNone(row)
            self.assertEqual(row.status, "claimed")
        self.schedule_mock.assert_called_once()

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
            row = session.exec(
                select(ExternalSubscriptionClaimModel)
                .where(ExternalSubscriptionClaimModel.claim_id == extra["external_subscription_claim"]["claim_id"])
            ).first()
        self.assertEqual(account.status, "subscribed")
        self.assertEqual(extra["external_subscription_claim"]["status"], "paid")
        self.assertEqual(extra["external_subscription_payment"]["status"], "paid")
        self.assertEqual(extra["external_subscription_payment"]["external_payment_id"], "pay_123")
        self.assertIsNotNone(row)
        self.assertEqual(row.status, "paid")

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
            row = session.exec(
                select(ExternalSubscriptionClaimModel)
                .where(ExternalSubscriptionClaimModel.claim_id == extra["external_subscription_claim"]["claim_id"])
            ).first()
        self.assertEqual(account.status, "payment_failed")
        self.assertEqual(extra["external_subscription_claim"]["status"], "failed")
        self.assertEqual(extra["external_subscription_payment"]["error_code"], "declined")
        self.assertIsNotNone(row)
        self.assertEqual(row.status, "failed")

    def test_claim_scans_beyond_old_twenty_account_window(self):
        for index in range(25):
            self._add_account(email=f"blocked{index}@example.com", link_status="already_paid")
        valid_id = self._add_account(email="valid@example.com")

        with self._session() as session:
            result = external_api.claim_subscription_links(
                external_api.ClaimSubscriptionLinksRequest(consumer="worker-a", limit=1),
                session=session,
            )

        self.assertEqual(result["count"], 1)
        self.assertEqual(result["items"][0]["account_id"], valid_id)

    def test_preflight_failure_marks_link_refreshes_and_continues(self):
        first_id = self._add_account(email="bad@example.com")
        second_id = self._add_account(email="good@example.com")
        self.preflight_mock.side_effect = [
            {
                "ok_to_send": False,
                "link_status": "already_paid",
                "reason": "you have paid",
                "probe": {},
            },
            {
                "ok_to_send": True,
                "link_status": "available",
                "reason": "",
                "probe": {"currency": "usd", "amount": 0, "amount_text": "0", "amount_is_zero": True},
                "checkout_amount": "0",
                "checkout_amount_is_zero": True,
            },
        ]
        with (
            mock.patch.object(external_api, "_refresh_account_local_status", return_value={}) as refresh_mock,
            self._session() as session,
        ):
            result = external_api.claim_subscription_links(
                external_api.ClaimSubscriptionLinksRequest(consumer="worker-a", limit=1),
                session=session,
            )

        self.assertEqual(result["count"], 1)
        self.assertEqual(result["items"][0]["account_id"], second_id)
        refresh_mock.assert_called_once()
        with self._session() as session:
            first = session.get(AccountModel, first_id)
            second = session.get(AccountModel, second_id)
            failed_row = session.exec(
                select(ExternalSubscriptionClaimModel)
                .where(ExternalSubscriptionClaimModel.account_id == first_id)
            ).first()
        self.assertEqual(first.get_extra()["chatgpt_last_payment_link"]["link_status"], "already_paid")
        self.assertEqual(second.get_extra()["chatgpt_last_payment_link"]["link_status"], "leased")
        self.assertIsNotNone(failed_row)
        self.assertEqual(failed_row.status, "already_paid")

    def test_due_local_verification_marks_paid_from_checkout_recheck(self):
        account_id = self._add_account(status="pending_payment")
        with self._session() as session:
            claimed = external_api.claim_subscription_links(
                external_api.ClaimSubscriptionLinksRequest(consumer="worker-a", limit=1),
                session=session,
            )
            claim_id = claimed["items"][0]["claim_id"]

        self.preflight_mock.return_value = {
            "ok_to_send": False,
            "link_status": "already_paid",
            "reason": "you have paid",
            "probe": {},
        }
        with (
            mock.patch.object(external_api, "_refresh_account_local_status", return_value={}),
            mock.patch.object(external_api, "_paid_from_probe", return_value=False),
            mock.patch.object(external_api, "_enqueue_resume_subscription_auth", return_value="task_auth_1") as enqueue_mock,
            self._session() as session,
        ):
            result = external_api._verify_subscription_claim_now(session, claim_id)

        self.assertEqual(result["status"], "paid")
        enqueue_mock.assert_called_once_with(account_id)
        with self._session() as session:
            account = session.get(AccountModel, account_id)
            extra = account.get_extra()
        self.assertEqual(account.status, "subscribed")
        self.assertEqual(extra["external_subscription_claim"]["status"], "paid")
        self.assertEqual(extra["external_subscription_payment"]["provider"], "local_checkout_verify")
        self.assertEqual(extra["chatgpt_last_payment_link"]["link_status"], "paid")

    def test_due_local_verification_runs_after_short_lease_expires(self):
        account_id = self._add_account(status="pending_payment")
        with self._session() as session:
            claimed = external_api.claim_subscription_links(
                external_api.ClaimSubscriptionLinksRequest(consumer="worker-a", limit=1),
                session=session,
            )
            account = session.get(AccountModel, account_id)
            extra = account.get_extra()
            claim = extra["external_subscription_claim"]
            claim["lease_expires_at"] = external_api._iso(external_api._utcnow() - external_api.timedelta(seconds=1))
            claim["verify_after_at"] = external_api._iso(external_api._utcnow() - external_api.timedelta(seconds=1))
            extra["external_subscription_claim"] = claim
            account.set_extra(extra)
            row = session.exec(
                select(ExternalSubscriptionClaimModel)
                .where(ExternalSubscriptionClaimModel.claim_id == claim["claim_id"])
            ).first()
            row.lease_expires_at = claim["lease_expires_at"]
            row.verify_after_at = claim["verify_after_at"]
            session.add(row)
            session.add(account)
            session.commit()

        self.preflight_mock.return_value = {
            "ok_to_send": False,
            "link_status": "already_paid",
            "reason": "you have paid",
            "probe": {},
        }
        with (
            mock.patch.object(external_api, "_refresh_account_local_status", return_value={}),
            mock.patch.object(external_api, "_paid_from_probe", return_value=False),
            mock.patch.object(external_api, "_enqueue_resume_subscription_auth", return_value="task_auth_1"),
            self._session() as session,
        ):
            checked = external_api._run_due_local_verifications(session, external_api._utcnow())

        self.assertEqual(checked, 1)
        with self._session() as session:
            account = session.get(AccountModel, account_id)
            extra = account.get_extra()
        self.assertEqual(claimed["count"], 1)
        self.assertEqual(account.status, "subscribed")
        self.assertEqual(extra["external_subscription_claim"]["status"], "paid")

    def test_concurrent_claims_do_not_duplicate_single_link(self):
        account_id = self._add_account()

        def claim_once(index: int) -> dict:
            with self._session() as session:
                return external_api.claim_subscription_links(
                    external_api.ClaimSubscriptionLinksRequest(consumer=f"worker-{index}", limit=1),
                    session=session,
                )

        with ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(claim_once, range(10)))

        claimed_items = [item for result in results for item in result.get("items", [])]
        self.assertEqual(len(claimed_items), 1)
        self.assertEqual(claimed_items[0]["account_id"], account_id)
        with self._session() as session:
            rows = session.exec(
                select(ExternalSubscriptionClaimModel)
                .where(ExternalSubscriptionClaimModel.account_id == account_id)
            ).all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].status, "claimed")

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
