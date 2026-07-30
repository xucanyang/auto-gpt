import unittest
from unittest.mock import patch

from sqlmodel import SQLModel, Session, create_engine

from core import db as core_db
from core.db import AccountListStateModel, AccountModel
from services.chatgpt_core import baxigpt_cdk_repository as repo_module
from services.chatgpt_core import baxigpt_status_poller as poller_module
from services.chatgpt_core.baxigpt_cdk_repository import BaxiGptCdkRepository


class BaxiGptAccountStatusReconcilerTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        self.core_engine_patch = patch.object(core_db, "engine", self.engine)
        self.repo_engine_patch = patch.object(repo_module, "engine", self.engine)
        self.poller_engine_patch = patch.object(poller_module, "engine", self.engine)
        self.core_engine_patch.start()
        self.repo_engine_patch.start()
        self.poller_engine_patch.start()
        SQLModel.metadata.create_all(self.engine)
        core_db._ensure_baxigpt_cdk_pool_schema()
        core_db._ensure_account_list_state_schema()

    def tearDown(self):
        poller_module.stop()
        with poller_module._lock:
            poller_module._targets.clear()
        self.poller_engine_patch.stop()
        self.repo_engine_patch.stop()
        self.core_engine_patch.stop()

    def test_account_reconcile_is_not_automatic(self):
        self.assertFalse(poller_module.ACCOUNT_RECONCILE_AUTOMATIC)

    def test_reconciles_account_level_submitted_order_to_paid(self):
        repo = BaxiGptCdkRepository()
        cdk = repo.add(code="CDK-ACCOUNT-RECONCILE-1111")
        order_id = f"{cdk.code_value}::task-paid-1"
        with Session(self.engine) as session:
            account = AccountModel(
                platform="chatgpt",
                email="paid@example.com",
                password="pw",
                token="at",
                status="pending_payment",
            )
            account.set_extra(
                {
                    "baxigpt_cdk": {
                        "status": "submitted",
                        "upstream_status": "submitted",
                        "cdk_id": cdk.id,
                        "code_masked": cdk.code_masked,
                        "order_id": order_id,
                        "display_id": "task-paid-1",
                        "submitted_at": "2026-07-09T07:00:00Z",
                        "last_checked_at": "2026-07-09T07:00:00Z",
                    },
                    "idea_submit": {"unavailable": True, "reason": "old marker"},
                    "idea_submit_unavailable": True,
                }
            )
            session.add(account)
            session.commit()
            session.refresh(account)
            account_id = int(account.id or 0)

        with patch.object(
            poller_module.BaxiGptClient,
            "status",
            return_value={
                "ok": True,
                "status": "paid",
                "display_id": "task-paid-1",
                "email": "paid@example.com",
                "message": "",
            },
        ) as status_mock:
            result = poller_module.reconcile_pending_account_statuses_once(limit=10, stale_seconds=0)

        self.assertEqual(result["checked"], 1)
        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["paid"], 1)
        status_mock.assert_called_once_with(order_id)

        with Session(self.engine) as session:
            account = session.get(AccountModel, account_id)
            extra = account.get_extra()
            self.assertEqual(extra["baxigpt_cdk"]["status"], "paid")
            self.assertEqual(extra["baxigpt_cdk"]["upstream_status"], "paid")
            self.assertNotIn("idea_submit_unavailable", extra)
            state = session.get(AccountListStateModel, account_id)
            self.assertIsNotNone(state)
            self.assertEqual(state.idea_submit_state, "paid")

    def test_rebuilds_missing_order_id_from_cdk_and_display_id(self):
        repo = BaxiGptCdkRepository()
        cdk = repo.add(code="CDK-ACCOUNT-RECONCILE-2222")
        with Session(self.engine) as session:
            account = AccountModel(
                platform="chatgpt",
                email="failed@example.com",
                password="pw",
                token="at",
                status="pending_payment",
            )
            account.set_extra(
                {
                    "baxigpt_cdk": {
                        "status": "processing",
                        "upstream_status": "processing",
                        "cdk_id": cdk.id,
                        "code_masked": cdk.code_masked,
                        "order_id": "",
                        "display_id": "task-failed-1",
                        "submitted_at": "2026-07-09T07:00:00Z",
                        "last_checked_at": "2026-07-09T07:00:00Z",
                    }
                }
            )
            session.add(account)
            session.commit()
            session.refresh(account)
            account_id = int(account.id or 0)

        expected_order_id = f"{cdk.code_value}::task-failed-1"
        with patch.object(
            poller_module.BaxiGptClient,
            "status",
            return_value={
                "ok": True,
                "status": "failed",
                "display_id": "task-failed-1",
                "email": "failed@example.com",
                "message": "上游失败",
            },
        ) as status_mock:
            result = poller_module.reconcile_pending_account_statuses_once(limit=10, stale_seconds=0)

        self.assertEqual(result["checked"], 1)
        self.assertEqual(result["failed"], 1)
        status_mock.assert_called_once_with(expected_order_id)

        with Session(self.engine) as session:
            account = session.get(AccountModel, account_id)
            extra = account.get_extra()
            self.assertEqual(extra["baxigpt_cdk"]["status"], "failed")
            self.assertEqual(extra["baxigpt_cdk"]["order_id"], expected_order_id)
            state = session.get(AccountListStateModel, account_id)
            self.assertEqual(state.idea_submit_state, "failed")

    def test_stops_local_task_orders_and_excludes_them_from_reconcile(self):
        repo = BaxiGptCdkRepository()
        cdk = repo.add(code="CDK-ACCOUNT-STOP-3333")
        reserved = repo.reserve_for_account(
            cdk.id,
            account_id=1,
            email="stopped@example.com",
            task_id="task-stop-1",
        )
        submitted = repo.mark_submit_success(
            reserved.id,
            {
                "ok": True,
                "status": "processing",
                "order_id": f"{cdk.code_value}::task-stop-1",
                "display_id": "task-stop-1",
                "email": "stopped@example.com",
            },
        )
        with Session(self.engine) as session:
            account = AccountModel(
                platform="chatgpt",
                email="stopped@example.com",
                password="pw",
                token="at",
                status="pending_payment",
            )
            account.set_extra(
                {
                    "baxigpt_cdk": {
                        "status": "processing",
                        "upstream_status": "processing",
                        "task_id": "task-stop-1",
                        "cdk_id": cdk.id,
                        "order_id": submitted.order_id,
                        "display_id": submitted.display_id,
                    }
                }
            )
            session.add(account)
            session.commit()
            session.refresh(account)
            account_id = int(account.id or 0)

        with poller_module._lock:
            poller_module._targets[int(cdk.id)] = poller_module.BaxiGptStatusPollTarget(
                record_id=int(cdk.id),
                task_id="task-stop-1",
            )

        result = poller_module.stop_task_polling("task-stop-1")

        self.assertEqual(result["accounts_marked"], 1)
        self.assertEqual(result["targets_removed"], 1)
        self.assertFalse(
            poller_module.enqueue_status_poll(
                int(cdk.id),
                immediate=True,
                task_id="task-stop-1",
            )
        )
        with Session(self.engine) as session:
            account = session.get(AccountModel, account_id)
            extra = account.get_extra()
            payload = extra["baxigpt_cdk"]
            self.assertEqual(payload["status"], "stopped")
            self.assertTrue(payload["polling_disabled"])
            self.assertEqual(extra["idea_submit"]["status"], "stopped")
            state = session.get(AccountListStateModel, account_id)
            self.assertEqual(state.idea_submit_state, "stopped")

        with patch.object(poller_module.BaxiGptClient, "status") as status_mock:
            result = poller_module.reconcile_pending_account_statuses_once(
                limit=10,
                stale_seconds=0,
            )
        self.assertEqual(result["checked"], 0)
        status_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
