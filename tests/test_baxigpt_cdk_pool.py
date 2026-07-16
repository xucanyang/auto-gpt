import unittest
from unittest.mock import patch
import tempfile
from pathlib import Path

from fastapi import BackgroundTasks, HTTPException
from sqlmodel import SQLModel, create_engine, Session, select

from core import db as core_db
from core.db import AccountModel, TaskLog
from api import tasks as tasks_module
from api import baxigpt_cdk_pool as api_module
from services.chatgpt_core import baxigpt_cdk_repository as repo_module
from services.chatgpt_core import baxigpt_client as client_module
from services.chatgpt_core import baxigpt_status_poller as poller_module
from services.chatgpt_core.baxigpt_client import BaxiGptClient, BaxiGptRequestError
from services.chatgpt_core.baxigpt_cdk_repository import BaxiGptCdkRepository, mask_code
from core.pix_cdk_usage import PixCdkUsageStore, STATE_PAID


class BaxiGptCdkRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        self.pix_usage_tmp = tempfile.TemporaryDirectory(prefix="auto-gpt-pix-cdk-usage-")
        self.pix_usage_store = PixCdkUsageStore(Path(self.pix_usage_tmp.name) / "shared_config.db")
        self.core_engine_patch = patch.object(core_db, "engine", self.engine)
        self.repo_engine_patch = patch.object(repo_module, "engine", self.engine)
        self.tasks_engine_patch = patch.object(tasks_module, "engine", self.engine)
        self.pix_usage_store_patch = patch.object(tasks_module, "_pix_cdk_usage_store", self.pix_usage_store)
        self.core_engine_patch.start()
        self.repo_engine_patch.start()
        self.tasks_engine_patch.start()
        self.pix_usage_store_patch.start()
        SQLModel.metadata.create_all(self.engine)
        core_db._ensure_baxigpt_cdk_pool_schema()

    def tearDown(self):
        poller_module.stop()
        with poller_module._lock:
            poller_module._targets.clear()
        self.tasks_engine_patch.stop()
        self.repo_engine_patch.stop()
        self.core_engine_patch.stop()
        self.pix_usage_store_patch.stop()
        self.pix_usage_tmp.cleanup()

    def test_import_keeps_extra_codes_available(self):
        repo = BaxiGptCdkRepository()
        result = repo.import_lines("""
CDK-AAAA-1111
CDK-BBBB-2222----second
CDK-AAAA-1111
""")
        self.assertEqual(result["added"], 2)
        self.assertEqual(len(result["errors"]), 1)
        records = repo.list_available()
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0].code_masked, mask_code("CDK-AAAA-1111"))
        self.assertEqual(records[1].label, "second")

    def test_search_matches_plain_code_value(self):
        repo = BaxiGptCdkRepository()
        repo.add(code="BX-VISIBLE-SEARCH-1111")
        repo.add(code="BX-OTHER-2222")

        records = repo.list(search="visible-search")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].code_value, "BX-VISIBLE-SEARCH-1111")

    def test_submit_success_binds_account_and_updates_extra(self):
        repo = BaxiGptCdkRepository()
        code = repo.add(code="CDK-AAAA-1111")
        with Session(self.engine) as session:
            account = AccountModel(platform="chatgpt", email="user@example.com", password="pw", token="at")
            session.add(account)
            session.commit()
            session.refresh(account)
            account_id = int(account.id or 0)

        reserved = repo.reserve_for_account(code.id, account_id=account_id, email="user@example.com", task_id="task_1")
        submitted = repo.mark_submit_success(reserved.id, {
            "ok": True,
            "order_id": "order_1",
            "display_id": "PAY-TEST",
            "email": "remote@example.com",
            "status": "processing",
        })
        with Session(self.engine) as session:
            account = session.get(AccountModel, account_id)
            repo.persist_account_binding_extra(account, submitted)
            session.add(account)
            session.commit()
            session.refresh(account)
            extra = account.get_extra()

        self.assertEqual(submitted.status, "processing")
        self.assertEqual(submitted.bound_account_email, "user@example.com")
        self.assertEqual(extra["baxigpt_cdk"]["order_id"], "order_1")
        self.assertEqual(extra["baxigpt_cdk"]["code_masked"], submitted.code_masked)

    def test_paid_status_syncs_account_primary_status(self):
        repo = BaxiGptCdkRepository()
        code = repo.add(code="CDK-AAAA-1111")
        with Session(self.engine) as session:
            account = AccountModel(platform="chatgpt", email="user@example.com", password="pw", token="at", status="pending_payment")
            session.add(account)
            session.commit()
            session.refresh(account)
            account_id = int(account.id or 0)

        reserved = repo.reserve_for_account(code.id, account_id=account_id, email="user@example.com", task_id="task_1")
        submitted = repo.mark_submit_success(reserved.id, {
            "ok": True,
            "order_id": "order_1",
            "display_id": "PAY-TEST",
            "email": "user@example.com",
            "status": "processing",
        })
        paid = repo.mark_status_response(submitted.id, {"ok": True, "order_id": "order_1", "status": "paid"})
        with Session(self.engine) as session:
            account = session.get(AccountModel, account_id)
            repo.persist_account_binding_extra(account, paid)
            session.add(account)
            session.commit()
            session.refresh(account)

        self.assertEqual(account.status, "subscribed")

    def test_paid_extra_can_skip_direct_account_status_and_store_refresh_summary(self):
        repo = BaxiGptCdkRepository()
        code = repo.add(code="CDK-AAAA-1111")
        with Session(self.engine) as session:
            account = AccountModel(platform="chatgpt", email="user@example.com", password="pw", token="at", status="pending_payment")
            session.add(account)
            session.commit()
            session.refresh(account)
            account_id = int(account.id or 0)

        reserved = repo.reserve_for_account(code.id, account_id=account_id, email="user@example.com", task_id="task_1")
        submitted = repo.mark_submit_success(reserved.id, {
            "ok": True,
            "order_id": "order_1",
            "display_id": "PAY-TEST",
            "email": "user@example.com",
            "status": "processing",
        })
        paid = repo.mark_status_response(submitted.id, {"ok": True, "order_id": "order_1", "status": "paid"})
        refresh_summary = {
            "trigger": "baxigpt_cdk_paid",
            "status": "registered",
            "subscription_plan": "",
            "auth_state": "ok",
        }
        with Session(self.engine) as session:
            account = session.get(AccountModel, account_id)
            repo.persist_account_binding_extra(
                account,
                paid,
                local_status_refresh=refresh_summary,
                apply_payment_state=False,
            )
            session.add(account)
            session.commit()
            session.refresh(account)
            extra = account.get_extra()

        self.assertEqual(account.status, "pending_payment")
        self.assertEqual(extra["baxigpt_cdk"]["status"], "paid")
        self.assertEqual(extra["baxigpt_cdk"]["local_status_refresh"], refresh_summary)

    def test_failed_status_syncs_account_primary_status(self):
        repo = BaxiGptCdkRepository()
        code = repo.add(code="CDK-AAAA-1111")
        with Session(self.engine) as session:
            account = AccountModel(platform="chatgpt", email="user@example.com", password="pw", token="at", status="pending_payment")
            session.add(account)
            session.commit()
            session.refresh(account)
            account_id = int(account.id or 0)

        reserved = repo.reserve_for_account(code.id, account_id=account_id, email="user@example.com", task_id="task_1")
        failed = repo.mark_failure(reserved.id, error_code="submit_failed", error_message="上游提交失败")
        with Session(self.engine) as session:
            account = session.get(AccountModel, account_id)
            repo.persist_account_binding_extra(account, failed)
            session.add(account)
            session.commit()
            session.refresh(account)

        self.assertEqual(account.status, "payment_failed")

    def test_status_response_marks_paid(self):
        repo = BaxiGptCdkRepository()
        record = repo.add(code="CDK-AAAA-1111")
        reserved = repo.reserve_for_account(record.id, account_id=1, email="user@example.com", task_id="task_1")
        submitted = repo.mark_submit_success(reserved.id, {"ok": True, "order_id": "order_1", "status": "processing"})
        updated = repo.mark_status_response(submitted.id, {"ok": True, "order_id": "order_1", "status": "paid"})
        self.assertEqual(updated.status, "paid")
        self.assertTrue(updated.paid_at)

    def test_status_syncs_bound_account_extra(self):
        repo = BaxiGptCdkRepository()
        record = repo.add(code="CDK-AAAA-1111")
        with Session(self.engine) as session:
            account = AccountModel(platform="chatgpt", email="user@example.com", password="pw", token="at")
            session.add(account)
            session.commit()
            session.refresh(account)
            account_id = int(account.id or 0)

        reserved = repo.reserve_for_account(record.id, account_id=account_id, email="user@example.com", task_id="task_1")
        submitted = repo.mark_submit_success(reserved.id, {
            "ok": True,
            "order_id": "order_1",
            "display_id": "PAY-TEST",
            "email": "remote@example.com",
            "status": "processing",
        })
        updated = repo.mark_status_response(submitted.id, {"ok": True, "order_id": "order_1", "display_id": "PAY-TEST", "status": "paid"})
        self.assertTrue(repo.persist_bound_account_extra(updated))

        with Session(self.engine) as session:
            account = session.get(AccountModel, account_id)
            extra = account.get_extra()

        self.assertEqual(extra["baxigpt_cdk"]["status"], "paid")
        self.assertEqual(extra["baxigpt_cdk"]["order_id"], "order_1")
        self.assertTrue(extra["baxigpt_cdk"]["paid_at"])

    def test_query_used_without_order_marks_failed(self):
        repo = BaxiGptCdkRepository()
        record = repo.add(code="CDK-AAAA-1111")
        updated = repo.mark_query_response("CDK-AAAA-1111", {
            "ok": True,
            "remaining": 0,
            "total": 1,
            "used": 1,
            "status_code": "active",
            "orders": [],
        })
        self.assertEqual(updated.status, "failed")
        self.assertEqual(updated.last_error_code, "quota_exhausted")

    def test_query_failed_orders_with_remaining_keeps_available(self):
        repo = BaxiGptCdkRepository()
        repo.add(code="CDK-AAAA-1111")
        updated = repo.mark_query_response("CDK-AAAA-1111", {
            "ok": True,
            "remaining": 1,
            "total": 1,
            "used": 0,
            "status_code": "active",
            "orders": [{"status": "failed", "display_id": "PAY-FAIL"}],
        })
        self.assertEqual(updated.status, "available")
        self.assertEqual(updated.upstream_status, "active")
        self.assertEqual(updated.code_info_remaining, 1)
        self.assertEqual(len(updated.last_query_response["orders"]), 1)

    def test_query_paid_order_binds_matching_account(self):
        repo = BaxiGptCdkRepository()
        repo.add(code="CDK-AAAA-1111")
        with Session(self.engine) as session:
            account = AccountModel(platform="chatgpt", email="paid@example.com", password="pw", token="at")
            session.add(account)
            session.commit()
            session.refresh(account)
            account_id = int(account.id or 0)
        updated = repo.mark_query_response("CDK-AAAA-1111", {
            "ok": True,
            "remaining": 0,
            "total": 1,
            "used": 1,
            "status_code": "active",
            "orders": [{
                "email": "paid@example.com",
                "display_id": "PAY-PAID",
                "status": "paid",
                "created_at": "2026-06-08 10:16",
                "paid_at": "2026-06-08 10:17",
            }],
        })
        self.assertEqual(updated.status, "paid")
        self.assertEqual(updated.bound_account_id, account_id)
        self.assertEqual(updated.bound_account_email, "paid@example.com")
        self.assertEqual(updated.display_id, "PAY-PAID")
        self.assertEqual(updated.paid_at, "2026-06-08 10:17")

    def test_code_info_quota_exhausted_marks_failed(self):
        repo = BaxiGptCdkRepository()
        record = repo.add(code="CDK-AAAA-1111")
        updated = repo.mark_code_info(record.id, {"ok": False, "msg": "卡密配额已用完"})
        self.assertEqual(updated.status, "failed")
        self.assertEqual(updated.last_error_message, "卡密配额已用完")

    def test_list_by_ids_keeps_failed_manual_codes_for_task_log(self):
        repo = BaxiGptCdkRepository()
        record = repo.add(code="CDK-AAAA-1111")
        repo.mark_code_info(record.id, {"ok": False, "msg": "卡密配额已用完"})

        self.assertEqual(repo.list_available(ids=[record.id]), [])
        records = repo.list_by_ids([record.id])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].status, "failed")
        self.assertEqual(records[0].last_error_message, "卡密配额已用完")

    def test_submit_candidates_reuse_terminal_cards_with_remaining_quota(self):
        repo = BaxiGptCdkRepository()
        paid = repo.add(code="CDK-PAID-REMAINING-1111")
        failed = repo.add(code="CDK-FAILED-REMAINING-2222")
        exhausted = repo.add(code="CDK-FAILED-EMPTY-3333")
        disabled = repo.add(code="CDK-DISABLED-4444")

        repo.mark_code_info(paid.id, {"ok": True, "remaining": 2, "total": 3})
        repo.mark_status_response(paid.id, {"ok": True, "status": "paid"})
        repo.mark_code_info(failed.id, {"ok": True, "remaining": 1, "total": 3})
        repo.mark_failure(failed.id, error_code="submit_error", error_message="账号临时失败")
        repo.mark_code_info(exhausted.id, {"ok": False, "remaining": 0, "total": 1, "msg": "额度已用完"})
        repo.set_status(disabled.id, "disabled")

        candidates = repo.list_submit_candidates()
        self.assertEqual([record.id for record in candidates], [paid.id, failed.id])
        self.assertEqual([record.id for record in repo.list_submit_candidates(ids=[failed.id, paid.id])], [failed.id, paid.id])

    def test_code_info_remaining_keeps_reserved(self):
        repo = BaxiGptCdkRepository()
        record = repo.add(code="CDK-AAAA-1111")
        reserved = repo.reserve_for_account(record.id, account_id=1, email="user@example.com", task_id="task_1")
        updated = repo.mark_code_info(reserved.id, {"ok": True, "remaining": 1, "total": 1})
        self.assertEqual(updated.status, "reserved")
        self.assertEqual(updated.code_info_remaining, 1)

    def test_poller_only_enqueues_submitted_or_processing_with_order_id(self):
        repo = BaxiGptCdkRepository()
        available = repo.add(code="CDK-AVAILABLE-1111")
        reserved = repo.reserve_for_account(available.id, account_id=1, email="reserved@example.com", task_id="task_1")
        submitted = repo.mark_submit_success(reserved.id, {"ok": True, "order_id": "order_1", "status": "processing"})
        failed_source = repo.add(code="CDK-FAILED-2222")
        failed = repo.mark_failure(failed_source.id, error_code="history_failed", error_message="历史失败")

        self.assertTrue(poller_module.enqueue_status_poll(submitted.id, immediate=True, source="manual_poll"))
        self.assertFalse(poller_module.enqueue_status_poll(failed.id, immediate=True, source="manual_poll"))

        snap = poller_module.snapshot()
        self.assertEqual(snap["queued"], 1)
        self.assertEqual(snap["ids"], [submitted.id])
        self.assertEqual(snap["targets"][0]["record_id"], submitted.id)
        self.assertEqual(snap["targets"][0]["source"], "manual_poll")
        self.assertIn("last_error", snap["targets"][0])

    def test_restore_pending_targets_filters_old_or_missing_order(self):
        repo = BaxiGptCdkRepository()
        recent = repo.add(code="CDK-RECENT-1111")
        recent_reserved = repo.reserve_for_account(recent.id, account_id=1, email="recent@example.com", task_id="task_1")
        recent_submitted = repo.mark_submit_success(recent_reserved.id, {"ok": True, "order_id": "order_recent", "status": "processing"})

        no_order = repo.add(code="CDK-NO-ORDER-2222")
        repo.reserve_for_account(no_order.id, account_id=2, email="noorder@example.com", task_id="task_2")

        old = repo.add(code="CDK-OLD-3333")
        old_reserved = repo.reserve_for_account(old.id, account_id=3, email="old@example.com", task_id="task_3")
        old_submitted = repo.mark_submit_success(old_reserved.id, {"ok": True, "order_id": "order_old", "status": "processing"})
        with Session(self.engine) as session:
            model = session.get(core_db.BaxiGptCdkPoolModel, old_submitted.id)
            model.submitted_at = "2026-01-01T00:00:00Z"
            model.last_checked_at = "2026-01-01T00:00:00Z"
            session.add(model)
            session.commit()

        count = poller_module.restore_pending_targets()
        snap = poller_module.snapshot()
        self.assertEqual(count, 1)
        self.assertEqual(snap["ids"], [recent_submitted.id])
        self.assertEqual(snap["targets"][0]["source"], "restart_restore")

    def test_diagnostics_include_safe_account_and_poller_details(self):
        repo = BaxiGptCdkRepository()
        code = repo.add(code="CDK-DIAG-1111")
        with Session(self.engine) as session:
            account = AccountModel(
                platform="chatgpt",
                email="diag@example.com",
                password="secret",
                token="access-token",
                status="pending_payment",
            )
            session.add(account)
            session.commit()
            session.refresh(account)
            account_id = int(account.id or 0)

        reserved = repo.reserve_for_account(code.id, account_id=account_id, email="diag@example.com", task_id="task_diag")
        submitted = repo.mark_submit_success(reserved.id, {
            "ok": True,
            "order_id": "order_diag",
            "display_id": "PAY-DIAG",
            "email": "diag@example.com",
            "status": "processing",
        })
        repo.persist_bound_account_extra(submitted)
        with poller_module._lock:
            poller_module._targets[int(submitted.id)] = poller_module.BaxiGptStatusPollTarget(
                record_id=int(submitted.id),
                task_id="task_diag",
                interval_seconds=5,
                timeout_seconds=300,
                source="manual_poll",
                last_status="processing",
            )

        diag = api_module.get_baxigpt_cdk_diagnostics(submitted.id)

        self.assertTrue(diag["ok"])
        self.assertEqual(diag["item"]["id"], submitted.id)
        self.assertEqual(diag["poller_target"]["source"], "manual_poll")
        self.assertEqual(diag["bound_account"]["email"], "diag@example.com")
        self.assertIn("baxigpt_cdk", diag["bound_account"])
        self.assertNotIn("token", diag["bound_account"])
        self.assertNotIn("password", diag["bound_account"])

    def test_repo_multi_quota_reserve_and_ineligible(self):
        repo = BaxiGptCdkRepository()
        code = repo.add(code="CDK-MULTI-1111")
        
        rec1 = repo.reserve_for_account(code.id, account_id=101, email="acc1@example.com", task_id="t-1")
        self.assertIsNotNone(rec1)
        self.assertEqual(rec1.status, "reserved")
        
        rec2 = repo.reserve_for_account(code.id, account_id=102, email="acc2@example.com", task_id="t-1")
        self.assertIsNotNone(rec2)
        self.assertEqual(rec2.bound_account_id, 102)
        
        with Session(self.engine) as session:
            account = AccountModel(id=102, platform="chatgpt", email="acc2@example.com", password="pw", token="at")
            session.add(account)
            session.commit()
            
            repo.mark_account_ineligible(account, rec2, "该账号没有开通资格")
            session.add(account)
            session.commit()
            
            extra = account.get_extra()
            self.assertTrue(extra.get("chatgpt_account_unavailable"))
            self.assertEqual(extra.get("chatgpt_unavailable_reason"), "该账号没有开通资格")
            self.assertTrue(extra.get("chatgpt_skip_save_account"))
            self.assertTrue(extra.get("idea_submit_unavailable"))
            self.assertEqual(extra.get("idea_submit_unavailable_reason"), "该账号没有开通资格")
            self.assertTrue(extra.get("idea_submit", {}).get("unavailable"))
            self.assertEqual(extra.get("idea_submit", {}).get("source"), "baxigpt_cdk_submit")

            repo.persist_account_binding_extra(account, rec2, status="paid")
            session.add(account)
            session.commit()
            session.refresh(account)
            extra = account.get_extra()
            self.assertIsNone(extra.get("idea_submit_unavailable"))
            self.assertFalse(extra.get("idea_submit", {}).get("unavailable"))
            self.assertTrue(extra.get("idea_submit", {}).get("available"))

    def test_submit_task_can_use_selected_pool_cdks_only(self):
        repo = BaxiGptCdkRepository()
        selected_code = repo.add(code="CDK-SELECTED-1111")
        other_code = repo.add(code="CDK-OTHER-2222")
        self.assertIsNotNone(selected_code)
        self.assertIsNotNone(other_code)
        with Session(self.engine) as session:
            account_1 = AccountModel(platform="chatgpt", email="one@example.com", password="pw", token="at-1")
            account_2 = AccountModel(platform="chatgpt", email="two@example.com", password="pw", token="at-2")
            session.add(account_1)
            session.add(account_2)
            session.commit()
            session.refresh(account_1)
            session.refresh(account_2)
            account_ids = [int(account_1.id or 0), int(account_2.id or 0)]

        result = tasks_module.enqueue_baxigpt_cdk_submit_task(
            tasks_module.BaxiGptCdkSubmitTaskRequest(
                account_ids=account_ids,
                use_pool=True,
                cdk_ids=[int(selected_code.id), 0, int(selected_code.id)],
                auto_poll_status=False,
            ),
            background_tasks=BackgroundTasks(),
        )

        self.assertEqual(result["available_codes"], 1)
        self.assertEqual(result["selected_cdk_ids"], [int(selected_code.id)])
        self.assertEqual(result["pair_count"], 1)
        self.assertEqual(result["pairs"][0]["cdk_id"], int(selected_code.id))
        self.assertEqual(result["skipped_accounts"][0]["reason"], "可用卡密额度不足，本轮未提交")

    def test_submit_task_uses_terminal_card_when_it_still_has_quota(self):
        repo = BaxiGptCdkRepository()
        record = repo.add(code="CDK-TERMINAL-REMAINING-1111")
        repo.mark_code_info(record.id, {"ok": True, "remaining": 2, "total": 3})
        repo.mark_status_response(record.id, {"ok": True, "status": "paid"})
        with Session(self.engine) as session:
            account = AccountModel(platform="chatgpt", email="reusable@example.com", password="pw", token="at")
            session.add(account)
            session.commit()
            session.refresh(account)
            account_id = int(account.id or 0)

        result = tasks_module.enqueue_baxigpt_cdk_submit_task(
            tasks_module.BaxiGptCdkSubmitTaskRequest(
                account_ids=[account_id],
                use_pool=True,
                auto_poll_status=False,
            ),
            background_tasks=BackgroundTasks(),
        )

        self.assertEqual(result["available_codes"], 1)
        self.assertEqual(result["pair_count"], 1)
        self.assertEqual(result["pairs"][0]["cdk_id"], int(record.id))

    def test_submit_task_stores_target_success_count(self):
        repo = BaxiGptCdkRepository()
        selected_code = repo.add(code="CDK-TARGET-1111")
        repo.mark_code_info(selected_code.id, {"ok": True, "remaining": 3, "total": 3})
        with Session(self.engine) as session:
            accounts = [
                AccountModel(platform="chatgpt", email=f"target-{index}@example.com", password="pw", token=f"at-{index}")
                for index in range(3)
            ]
            session.add_all(accounts)
            session.commit()
            account_ids = []
            for account in accounts:
                session.refresh(account)
                account_ids.append(int(account.id or 0))

        result = tasks_module.enqueue_baxigpt_cdk_submit_task(
            tasks_module.BaxiGptCdkSubmitTaskRequest(
                account_ids=account_ids,
                use_pool=True,
                cdk_ids=[int(selected_code.id)],
                target_success_count=2,
                auto_poll_status=False,
            ),
            background_tasks=BackgroundTasks(),
        )

        self.assertEqual(result["pair_count"], 3)
        self.assertEqual(result["target_success_count"], 2)
        self.assertEqual(result["effective_target_success_count"], 2)
        snapshot = tasks_module._task_store.snapshot(result["task_id"])
        self.assertEqual(snapshot["progress"], "0/2")
        self.assertEqual(snapshot["meta"]["settings"]["target_success_count"], 2)
        self.assertEqual(snapshot["meta"]["settings"]["requested_target_success_count"], 2)

    def test_submit_runtime_stops_after_target_success_count(self):
        repo = BaxiGptCdkRepository()
        selected_code = repo.add(code="CDK-RUNTIME-TARGET-1111")
        repo.mark_code_info(selected_code.id, {"ok": True, "remaining": 3, "total": 3})
        with Session(self.engine) as session:
            accounts = [
                AccountModel(platform="chatgpt", email=f"runtime-target-{index}@example.com", password="pw", token=f"at-{index}")
                for index in range(3)
            ]
            session.add_all(accounts)
            session.commit()
            account_ids = []
            for account in accounts:
                session.refresh(account)
                account_ids.append(int(account.id or 0))

        result = tasks_module.enqueue_baxigpt_cdk_submit_task(
            tasks_module.BaxiGptCdkSubmitTaskRequest(
                account_ids=account_ids,
                use_pool=True,
                cdk_ids=[int(selected_code.id)],
                target_success_count=1,
                submit_interval_seconds=0,
                status_poll_interval_seconds=1,
                status_poll_timeout_seconds=1800,
            ),
            background_tasks=BackgroundTasks(),
        )
        task_id = result["task_id"]
        snapshot = tasks_module._task_store.snapshot(task_id)
        pairs = snapshot["meta"]["pairs"]
        settings = snapshot["meta"]["settings"]

        class FakeBaxiClient:
            submit_calls: list[str] = []

            def code_info(self, _code):
                return {"ok": True, "remaining": 3, "total": 3}

            def submit(self, code, access_token):
                self.__class__.submit_calls.append(access_token)
                return {
                    "ok": True,
                    "submitted_items": [
                        {
                            "order_id": f"{code}::task-{len(self.__class__.submit_calls)}",
                            "display_id": f"task-{len(self.__class__.submit_calls)}",
                            "status": "submitted",
                        }
                    ],
                }

            def status(self, _order_id):
                return {"ok": True, "status": "paid"}

        with patch("services.chatgpt_core.baxigpt_client.BaxiGptClient", FakeBaxiClient), \
             patch.object(tasks_module, "sync_chatgpt_account_local_status", return_value={"status": "subscribed"}), \
             patch.object(tasks_module, "summarize_status_refresh", return_value={"status": "subscribed"}), \
             patch.object(tasks_module.time, "sleep", return_value=None):
            tasks_module._run_baxigpt_cdk_submit(task_id, pairs, settings)

        final_snapshot = tasks_module._task_store.snapshot(task_id)
        summary = final_snapshot["meta"]["idea_submit_summary"]
        self.assertEqual(len(FakeBaxiClient.submit_calls), 1)
        self.assertEqual(final_snapshot["status"], "done")
        self.assertEqual(final_snapshot["success"], 1)
        self.assertEqual(summary["target_success_count"], 1)
        self.assertEqual(summary["paid"], 1)
        self.assertEqual(summary["unsubmitted"], 2)
        self.assertIn("已达到本次目标成功数量 1", summary["unsubmitted_accounts"][0]["reason"])

    def test_submit_runtime_surfaces_upstream_card_rejection_reason(self):
        repo = BaxiGptCdkRepository()
        record = repo.add(code="CDK-UPSTREAM-BLOCKED-1111")
        with Session(self.engine) as session:
            account = AccountModel(platform="chatgpt", email="blocked@example.com", password="pw", token="at")
            session.add(account)
            session.commit()
            session.refresh(account)
            account_id = int(account.id or 0)

        result = tasks_module.enqueue_baxigpt_cdk_submit_task(
            tasks_module.BaxiGptCdkSubmitTaskRequest(
                account_ids=[account_id],
                code_lines="CDK-UPSTREAM-BLOCKED-1111",
                use_pool=False,
                auto_poll_status=False,
            ),
            background_tasks=BackgroundTasks(),
        )
        snapshot = tasks_module._task_store.snapshot(result["task_id"])

        class FakeBaxiClient:
            def code_info(self, _code):
                return {
                    "ok": False,
                    "remaining": 990,
                    "total": 1000,
                    "message": "该卡密失败次数过多，已被风控限制",
                }

        with patch("services.chatgpt_core.baxigpt_client.BaxiGptClient", FakeBaxiClient):
            tasks_module._run_baxigpt_cdk_submit(result["task_id"], snapshot["meta"]["pairs"], snapshot["meta"]["settings"])

        final_snapshot = tasks_module._task_store.snapshot(result["task_id"])
        summary = final_snapshot["meta"]["idea_submit_summary"]
        self.assertEqual(final_snapshot["status"], "failed")
        self.assertIn("该卡密失败次数过多，已被风控限制", final_snapshot["errors"][0])
        self.assertIn("该卡密失败次数过多，已被风控限制", summary["unsubmitted_accounts"][0]["reason"])

    def test_pix_submit_task_never_persists_pix_cdk_or_status_token(self):
        pix_cdk = "PIX-SENSITIVE-CDK-123456"
        status_token = "pix-status-token-sensitive"
        with Session(self.engine) as session:
            account = AccountModel(platform="chatgpt", email="pix@example.com", password="pw", token="at-pix")
            session.add(account)
            session.commit()
            session.refresh(account)
            account_id = int(account.id or 0)

        result = tasks_module.enqueue_baxigpt_cdk_submit_task(
            tasks_module.BaxiGptCdkSubmitTaskRequest(
                account_ids=[account_id],
                payment_channel="pix",
                pix_cdk=pix_cdk,
                submit_interval_seconds=0,
                status_poll_interval_seconds=1,
            ),
            background_tasks=BackgroundTasks(),
        )
        self.assertEqual(result["payment_channel"], "pix")
        self.assertEqual(result["pair_count"], 1)
        self.assertEqual(BaxiGptCdkRepository().list(), [])

        snapshot = tasks_module._task_store.snapshot(result["task_id"])
        snapshot_text = str(snapshot)
        self.assertNotIn(pix_cdk, snapshot_text)
        self.assertNotIn(status_token, snapshot_text)
        self.assertEqual(snapshot["meta"]["settings"]["payment_channel"], "pix")
        self.assertTrue(snapshot["meta"]["settings"]["pix_cdk_configured"])
        expected_status_token = status_token

        class FakeBaxiClient:
            submit_calls: list[tuple[str, str]] = []

            def submit_pix(self, *, pix_cdk, access_token):
                self.__class__.submit_calls.append((pix_cdk, access_token))
                return {
                    "ok": True,
                    "task_id": "pix-task-1",
                    "order_id": "pix-task-1",
                    "display_id": "pix-task-1",
                    "status_token": status_token,
                }

            def pix_status(self, *, task_id, status_token):
                if task_id != "pix-task-1" or status_token != expected_status_token:
                    raise AssertionError("unexpected PIX poll credential")
                return {"ok": True, "status": "paid"}

        with patch("services.chatgpt_core.baxigpt_client.BaxiGptClient", FakeBaxiClient), \
             patch.object(tasks_module, "sync_chatgpt_account_local_status", return_value={"status": "subscribed"}), \
             patch.object(tasks_module, "summarize_status_refresh", return_value={"status": "subscribed"}), \
             patch.object(tasks_module.time, "sleep", return_value=None):
            tasks_module._run_pix_submit(
                result["task_id"],
                snapshot["meta"]["pairs"],
                snapshot["meta"]["settings"],
                pix_cdk,
            )

        final_snapshot = tasks_module._task_store.snapshot(result["task_id"])
        self.assertEqual(FakeBaxiClient.submit_calls, [(pix_cdk, "at-pix")])
        self.assertEqual(final_snapshot["status"], "done")
        self.assertEqual(final_snapshot["meta"]["idea_submit_summary"]["payment_channel"], "pix")
        final_text = str(final_snapshot)
        self.assertNotIn(pix_cdk, final_text)
        self.assertNotIn(status_token, final_text)

        with Session(self.engine) as session:
            account = session.get(AccountModel, account_id)
            extra = account.get_extra()
            task_log = session.exec(select(TaskLog).where(TaskLog.task_id == result["task_id"])).first()
        self.assertEqual(extra["baxigpt_cdk"]["payment_channel"], "pix")
        self.assertEqual(extra["baxigpt_cdk"]["code_masked"], "PIX CDK")
        self.assertNotIn(pix_cdk, str(extra))
        self.assertNotIn(status_token, str(extra))
        self.assertIsNotNone(task_log)
        self.assertNotIn(pix_cdk, str(task_log.detail_json))
        self.assertNotIn(status_token, str(task_log.detail_json))

    def test_pix_multiple_cdks_reuse_after_failure_and_lock_after_paid(self):
        pix_cdk_a = "PIX-MULTI-ALPHA"
        pix_cdk_b = "PIX-MULTI-BRAVO"
        with Session(self.engine) as session:
            account_ids = []
            for index in range(1, 4):
                account = AccountModel(
                    platform="chatgpt",
                    email=f"pix-multi-{index}@example.com",
                    password="pw",
                    token=f"at-pix-multi-{index}",
                )
                session.add(account)
                session.commit()
                session.refresh(account)
                account_ids.append(int(account.id or 0))

        result = tasks_module.enqueue_baxigpt_cdk_submit_task(
            tasks_module.BaxiGptCdkSubmitTaskRequest(
                account_ids=account_ids,
                payment_channel="pix",
                pix_cdk_lines=f"{pix_cdk_a}\n{pix_cdk_b}",
                submit_interval_seconds=0,
                status_poll_interval_seconds=1,
            ),
            background_tasks=BackgroundTasks(),
        )
        snapshot = tasks_module._task_store.snapshot(result["task_id"])
        self.assertEqual(result["pix_cdk_count"], 2)
        self.assertEqual(snapshot["meta"]["pix_cdk_input"]["accepted"], 2)
        self.assertNotIn(pix_cdk_a, str(snapshot))
        self.assertNotIn(pix_cdk_b, str(snapshot))

        class FakeBaxiClient:
            submit_calls: list[tuple[str, str]] = []

            def submit_pix(self, *, pix_cdk, access_token):
                self.__class__.submit_calls.append((pix_cdk, access_token))
                return {
                    "ok": True,
                    "order_id": f"order-{access_token}",
                    "display_id": f"order-{access_token}",
                    "status_token": f"status-{access_token}",
                }

            def pix_status(self, *, task_id, status_token):
                return {
                    "ok": True,
                    "status": "failed" if task_id == "order-at-pix-multi-1" else "paid",
                    "message": "trial unavailable" if task_id == "order-at-pix-multi-1" else "",
                }

        runtime_cdks = [
            {"code": pix_cdk_a, "fingerprint": self.pix_usage_store.fingerprint(pix_cdk_a)},
            {"code": pix_cdk_b, "fingerprint": self.pix_usage_store.fingerprint(pix_cdk_b)},
        ]
        with patch("services.chatgpt_core.baxigpt_client.BaxiGptClient", FakeBaxiClient), \
             patch.object(tasks_module, "sync_chatgpt_account_local_status", return_value={"status": "subscribed"}), \
             patch.object(tasks_module, "summarize_status_refresh", return_value={"status": "subscribed"}), \
             patch.object(tasks_module.time, "sleep", return_value=None):
            tasks_module._run_pix_submit(
                result["task_id"],
                snapshot["meta"]["pairs"],
                snapshot["meta"]["settings"],
                runtime_cdks,
            )

        final_snapshot = tasks_module._task_store.snapshot(result["task_id"])
        self.assertEqual(final_snapshot["status"], "done")
        self.assertEqual(final_snapshot["meta"]["idea_submit_summary"]["paid"], 2)
        self.assertEqual(final_snapshot["meta"]["idea_submit_summary"]["failed"], 1)
        self.assertEqual(
            FakeBaxiClient.submit_calls,
            [
                (pix_cdk_a, "at-pix-multi-1"),
                (pix_cdk_b, "at-pix-multi-2"),
                (pix_cdk_a, "at-pix-multi-3"),
            ],
        )
        states = self.pix_usage_store.states_for([item["fingerprint"] for item in runtime_cdks])
        self.assertTrue(all(item.state == STATE_PAID for item in states.values()))
        self.assertNotIn(pix_cdk_a, str(final_snapshot))
        self.assertNotIn(pix_cdk_b, str(final_snapshot))

        with self.assertRaises(HTTPException) as ctx:
            tasks_module.enqueue_baxigpt_cdk_submit_task(
                tasks_module.BaxiGptCdkSubmitTaskRequest(
                    account_ids=[account_ids[0]],
                    payment_channel="pix",
                    pix_cdk_lines=f"{pix_cdk_a}\n{pix_cdk_b}",
                ),
                background_tasks=BackgroundTasks(),
            )
        self.assertEqual(ctx.exception.status_code, 409)

    def test_pix_unknown_submit_outcome_is_not_retried(self):
        pix_cdk = "PIX-SENSITIVE-NO-RETRY"
        with Session(self.engine) as session:
            account = AccountModel(platform="chatgpt", email="pix-unknown@example.com", password="pw", token="at-pix-unknown")
            session.add(account)
            session.commit()
            session.refresh(account)
            account_id = int(account.id or 0)

        result = tasks_module.enqueue_baxigpt_cdk_submit_task(
            tasks_module.BaxiGptCdkSubmitTaskRequest(
                account_ids=[account_id],
                payment_channel="pix",
                pix_cdk=pix_cdk,
                submit_interval_seconds=0,
                status_poll_interval_seconds=1,
            ),
            background_tasks=BackgroundTasks(),
        )
        snapshot = tasks_module._task_store.snapshot(result["task_id"])

        class FakeBaxiClient:
            calls = 0

            def submit_pix(self, *, pix_cdk, access_token):
                self.__class__.calls += 1
                raise BaxiGptRequestError(
                    f"upstream unavailable after submit {pix_cdk}",
                    http_status=502,
                )

        with patch("services.chatgpt_core.baxigpt_client.BaxiGptClient", FakeBaxiClient), \
             patch.object(tasks_module.time, "sleep", return_value=None):
            tasks_module._run_pix_submit(
                result["task_id"],
                snapshot["meta"]["pairs"],
                snapshot["meta"]["settings"],
                pix_cdk,
            )

        final_snapshot = tasks_module._task_store.snapshot(result["task_id"])
        summary = final_snapshot["meta"]["idea_submit_summary"]
        self.assertEqual(FakeBaxiClient.calls, 1)
        self.assertEqual(summary["timeout"], 1)
        self.assertNotIn(pix_cdk, str(final_snapshot))
        with Session(self.engine) as session:
            account = session.get(AccountModel, account_id)
            extra = account.get_extra()
        self.assertEqual(extra["baxigpt_cdk"]["status"], "timeout")
        self.assertEqual(extra["idea_submit"]["status"], "timeout")
        fingerprint = self.pix_usage_store.fingerprint(pix_cdk)
        self.assertEqual(self.pix_usage_store.states_for([fingerprint])[fingerprint].state, "uncertain")

    def test_pix_user_link_uses_saved_stripe_link_without_access_token_and_keeps_it_transient(self):
        pix_cdk = "PIX-USER-LINK-SENSITIVE-CDK"
        pix_link = "https://payments.stripe.com/qr/instructions/pix-user-link-sensitive"
        status_token = "pix-user-link-status-token-sensitive"
        with Session(self.engine) as session:
            account = AccountModel(platform="chatgpt", email="pix-link@example.com", password="pw", token="")
            account.set_extra({
                "chatgpt_last_payment_link": {
                    "url": pix_link,
                    "link_type": "pix",
                    "link_expires_at": 4_102_444_800,
                }
            })
            session.add(account)
            session.commit()
            session.refresh(account)
            account_id = int(account.id or 0)

        with patch.object(tasks_module, "_assert_pix_user_link_submission_enabled"):
            result = tasks_module.enqueue_baxigpt_cdk_submit_task(
                tasks_module.BaxiGptCdkSubmitTaskRequest(
                    account_ids=[account_id],
                    payment_channel="pix",
                    pix_submit_mode="user_link",
                    pix_cdk=pix_cdk,
                    submit_interval_seconds=0,
                    status_poll_interval_seconds=1,
                ),
                background_tasks=BackgroundTasks(),
            )
        snapshot = tasks_module._task_store.snapshot(result["task_id"])
        self.assertEqual(result["pix_submit_mode"], "user_link")
        self.assertEqual(snapshot["meta"]["settings"]["pix_submit_mode"], "user_link")
        self.assertNotIn(pix_cdk, str(snapshot))
        self.assertNotIn(pix_link, str(snapshot))

        class FakeBaxiClient:
            submit_calls: list[tuple[str, str]] = []

            def submit_pix_user_link(self, *, pix_cdk, pix_pay_link):
                self.__class__.submit_calls.append((pix_cdk, pix_pay_link))
                return {
                    "ok": True,
                    "task_id": "pix-user-link-task-1",
                    "order_id": "pix-user-link-task-1",
                    "display_id": "pix-user-link-task-1",
                    "status_token": status_token,
                }

            def pix_status(self, *, task_id, status_token):
                if task_id != "pix-user-link-task-1" or status_token != "pix-user-link-status-token-sensitive":
                    raise AssertionError("unexpected PIX user-link poll credential")
                return {"ok": True, "status": "paid"}

        with patch("services.chatgpt_core.baxigpt_client.BaxiGptClient", FakeBaxiClient), \
             patch.object(tasks_module, "sync_chatgpt_account_local_status", return_value={"status": "subscribed"}), \
             patch.object(tasks_module, "summarize_status_refresh", return_value={"status": "subscribed"}), \
             patch.object(tasks_module.time, "sleep", return_value=None):
            tasks_module._run_pix_submit(
                result["task_id"],
                snapshot["meta"]["pairs"],
                snapshot["meta"]["settings"],
                pix_cdk,
            )

        final_snapshot = tasks_module._task_store.snapshot(result["task_id"])
        self.assertEqual(FakeBaxiClient.submit_calls, [(pix_cdk, pix_link)])
        self.assertEqual(final_snapshot["status"], "done")
        self.assertNotIn(pix_cdk, str(final_snapshot))
        self.assertNotIn(pix_link, str(final_snapshot))
        self.assertNotIn(status_token, str(final_snapshot))
        with Session(self.engine) as session:
            account = session.get(AccountModel, account_id)
            extra = account.get_extra()
            task_log = session.exec(select(TaskLog).where(TaskLog.task_id == result["task_id"])).first()
        self.assertEqual(extra["baxigpt_cdk"]["pix_submit_mode"], "user_link")
        self.assertEqual(extra["idea_submit"]["pix_submit_mode"], "user_link")
        self.assertIsNotNone(task_log)
        self.assertNotIn(pix_cdk, str(task_log.detail_json))
        self.assertNotIn(pix_link, str(task_log.detail_json))
        self.assertNotIn(status_token, str(task_log.detail_json))

    def test_pix_user_link_resolution_allows_no_at_and_rejects_incompatible_or_expiring_links(self):
        with Session(self.engine) as session:
            good = AccountModel(platform="chatgpt", email="pix-good@example.com", password="pw", token="")
            good.set_extra({"chatgpt_last_payment_link": {
                "url": "https://payments.stripe.com/qr/instructions/good-link",
                "link_type": "pix",
                "link_expires_at": 4_102_444_800,
            }})
            incompatible = AccountModel(platform="chatgpt", email="pix-bad@example.com", password="pw", token="")
            incompatible.set_extra({"chatgpt_last_payment_link": {
                "url": "https://payments.stripe.com/checkout/bad-link",
                "link_type": "pix",
            }})
            expiring = AccountModel(platform="chatgpt", email="pix-expiring@example.com", password="pw", token="")
            expiring.set_extra({"chatgpt_last_payment_link": {
                "url": "https://payments.stripe.com/qr/instructions/expiring-link",
                "link_type": "pix",
                "link_expires_at": 1,
            }})
            session.add_all([good, incompatible, expiring])
            session.commit()
            session.refresh(good)
            session.refresh(incompatible)
            session.refresh(expiring)

        request = tasks_module.BaxiGptCdkSubmitTaskRequest(
            account_ids=[int(good.id), int(incompatible.id), int(expiring.id)],
            payment_channel="pix",
            pix_submit_mode="user_link",
        )
        eligible, missing_ids, skipped, matched = tasks_module._resolve_baxigpt_cdk_submit_accounts(
            request,
            require_access_token=False,
            require_saved_pix_link=True,
        )

        self.assertEqual([item["account_id"] for item in matched], [int(good.id), int(incompatible.id), int(expiring.id)])
        self.assertEqual([item["account_id"] for item in eligible], [int(good.id)])
        self.assertEqual(missing_ids, [])
        reasons = "\n".join(str(item.get("reason") or "") for item in skipped)
        self.assertIn("不是可上传的 Stripe PIX 指令链接", reasons)
        self.assertIn("即将到期", reasons)

    def test_pix_user_link_closed_channel_rejects_before_creating_task(self):
        class ClosedChannelClient:
            def public_submit_options(self):
                return {"pix_channel_enabled": True, "pix_user_link_enabled": False}

        with patch("services.chatgpt_core.baxigpt_client.BaxiGptClient", ClosedChannelClient), \
             patch.object(tasks_module, "_create_standalone_task_record") as create_task:
            with self.assertRaises(HTTPException) as raised:
                tasks_module.enqueue_baxigpt_cdk_submit_task(
                    tasks_module.BaxiGptCdkSubmitTaskRequest(
                        account_ids=[1],
                        payment_channel="pix",
                        pix_submit_mode="user_link",
                        pix_cdk="PIX-CLOSED-CHANNEL-CDK",
                    ),
                    background_tasks=BackgroundTasks(),
                )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("尚未开启 PIX 链接上传", str(raised.exception.detail))
        create_task.assert_not_called()


class BaxiGptClientRetryTests(unittest.TestCase):
    def test_code_info_retries_transient_request_error(self):
        calls = {"count": 0}

        class FakeResponse:
            status_code = 200
            text = "{}"

            def json(self):
                return {"ok": True, "remaining": 1, "total": 1}

        def fake_post(*_args, **_kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("temporary timeout")
            return FakeResponse()

        with patch.object(client_module.cffi_requests, "request", fake_post):
            client = BaxiGptClient(timeout=1, retries=1, retry_backoff_seconds=0)
            result = client.code_info("CDK-AAAA-1111")

        self.assertEqual(calls["count"], 2)
        self.assertTrue(result["ok"])

    def test_submit_does_not_retry_by_default(self):
        calls = {"count": 0}

        def fake_post(*_args, **_kwargs):
            calls["count"] += 1
            raise RuntimeError("timeout")

        with patch.object(client_module.cffi_requests, "request", fake_post):
            client = BaxiGptClient(timeout=1, submit_timeout=1, retries=2, retry_backoff_seconds=0)
            with self.assertRaises(BaxiGptRequestError):
                client.submit(code="CDK-AAAA-1111", access_token="at")

        self.assertEqual(calls["count"], 1)

    def test_pix_submit_uses_dedicated_upstream_contract_and_status_token(self):
        requests: list[tuple[str, str, dict]] = []

        class FakeResponse:
            status_code = 200
            text = "{}"

            def __init__(self, data):
                self._data = data

            def json(self):
                return self._data

        def fake_request(method, url, **kwargs):
            requests.append((method, url, kwargs))
            if url.endswith("/api/task/submit"):
                return FakeResponse({
                    "status": "ok",
                    "created_tasks": [{
                        "task_id": "pix-task-1",
                        "status": "PENDING",
                        "status_token": "pix-status-token-1",
                    }],
                })
            self.assertTrue(url.endswith("/api/pix/tasks/status"))
            return FakeResponse({
                "tasks": [{"task_id": "pix-task-1", "status": "SUCCESS"}],
                "channel": "pix",
            })

        with patch.object(client_module.cffi_requests, "request", fake_request):
            client = BaxiGptClient(base_url="https://submit.example.test")
            submitted = client.submit_pix(pix_cdk="PIX-CDK-SECRET", access_token="access-token-1")
            status = client.pix_status(task_id=submitted["task_id"], status_token=submitted["status_token"])

        self.assertTrue(submitted["ok"])
        self.assertEqual(submitted["task_id"], "pix-task-1")
        self.assertEqual(status["status"], "paid")
        submit_method, submit_url, submit_kwargs = requests[0]
        self.assertEqual((submit_method, submit_url), ("POST", "https://submit.example.test/api/task/submit"))
        self.assertEqual(submit_kwargs["json"], {
            "submitMode": "pix_auto_extract",
            "pixCdk": "PIX-CDK-SECRET",
            "accounts": ["access-token-1"],
        })
        self.assertEqual(requests[1][2]["params"], {
            "task_id": "pix-task-1",
            "status_token": "pix-status-token-1",
        })
        self.assertNotIn("status_token", status)

    def test_pix_user_link_submit_uses_dedicated_upstream_contract_and_status_token(self):
        requests: list[tuple[str, str, dict]] = []
        pix_link = "https://payments.stripe.com/qr/instructions/pix-link-secret"

        class FakeResponse:
            status_code = 200
            text = "{}"

            def __init__(self, data):
                self._data = data

            def json(self):
                return self._data

        def fake_request(method, url, **kwargs):
            requests.append((method, url, kwargs))
            if url.endswith("/api/task/submit"):
                return FakeResponse({
                    "status": "ok",
                    "created_tasks": [{
                        "task_id": "pix-link-task-1",
                        "status": "PENDING",
                        "status_token": "pix-link-status-token-1",
                    }],
                })
            self.assertTrue(url.endswith("/api/pix/tasks/status"))
            return FakeResponse({
                "tasks": [{"task_id": "pix-link-task-1", "status": "SUCCESS"}],
                "channel": "pix",
            })

        with patch.object(client_module.cffi_requests, "request", fake_request):
            client = BaxiGptClient(base_url="https://submit.example.test")
            submitted = client.submit_pix_user_link(
                pix_cdk="PIX-CDK-SECRET",
                pix_pay_link=pix_link,
            )
            status = client.pix_status(task_id=submitted["task_id"], status_token=submitted["status_token"])

        self.assertTrue(submitted["ok"])
        self.assertEqual(submitted["task_id"], "pix-link-task-1")
        self.assertEqual(status["status"], "paid")
        submit_method, submit_url, submit_kwargs = requests[0]
        self.assertEqual((submit_method, submit_url), ("POST", "https://submit.example.test/api/task/submit"))
        self.assertEqual(submit_kwargs["json"], {
            "submitMode": "pix_user_link",
            "pixCdk": "PIX-CDK-SECRET",
            "pixPayLink": pix_link,
            "accounts": [],
        })
        self.assertEqual(requests[1][2]["params"], {
            "task_id": "pix-link-task-1",
            "status_token": "pix-link-status-token-1",
        })
        self.assertNotIn(pix_link, str(submitted))
        self.assertNotIn("status_token", status)

    def test_batch_submit_with_multiple_accounts(self):
        class FakeResponse:
            status_code = 200
            def __init__(self, data): self._data = data
            def json(self): return self._data

        def fake_request(method, url, **kwargs):
            if "/api/task/submit" in url:
                return FakeResponse({"ok": True, "order_id": "CDK-MULTI::task-1"})
            if "/api/task/status" in url:
                return FakeResponse({
                    "ok": True,
                    "tasks": [
                        {"id": "task-1", "account": "token1", "status": "SUCCESS"},
                        {"id": "task-2", "account": "token2", "status": "FAILED", "error": "No trial eligibility"}
                    ]
                })
            return FakeResponse({})

        with patch.object(client_module.cffi_requests, "request", fake_request):
            client = BaxiGptClient()
            res = client.submit(code="CDK-MULTI", access_token=["token1", "token2"])
            self.assertTrue(res["ok"])
            self.assertEqual(len(res["submitted_items"]), 2)
            self.assertEqual(res["submitted_items"][0]["status"], "submitted")
            
            st1 = client.status("CDK-MULTI::task-1")
            self.assertEqual(st1["status"], "paid")
            
            st2 = client.status("CDK-MULTI::task-2")
            self.assertEqual(st2["status"], "failed")
            self.assertEqual(st2["message"], "No trial eligibility")

    def test_submit_prefers_created_task_ids_from_upstream(self):
        class FakeResponse:
            status_code = 200
            def __init__(self, data): self._data = data
            def json(self): return self._data

        def fake_request(method, url, **kwargs):
            self.assertIn("/api/task/submit", url)
            return FakeResponse({
                "status": "ok",
                "created_tasks": [
                    {"task_id": "real-task-1", "email": "user@example.com", "status": "PENDING"}
                ],
            })

        with patch.object(client_module.cffi_requests, "request", fake_request):
            client = BaxiGptClient()
            res = client.submit(code="CDK-REAL", access_token="not-a-jwt")

        self.assertTrue(res["ok"])
        self.assertEqual(res["order_id"], "CDK-REAL::real-task-1")
        self.assertEqual(res["submitted_items"][0]["display_id"], "real-task-1")

    def test_submit_fails_when_upstream_task_id_cannot_be_resolved(self):
        class FakeResponse:
            status_code = 200
            def __init__(self, data): self._data = data
            def json(self): return self._data

        def fake_request(method, url, **kwargs):
            if "/api/task/submit" in url:
                return FakeResponse({"status": "ok", "message": "1 tasks created."})
            if "/api/task/status" in url:
                return FakeResponse({"tasks": []})
            return FakeResponse({})

        with patch.object(client_module.cffi_requests, "request", fake_request):
            client = BaxiGptClient()
            res = client.submit(code="CDK-NO-ID", access_token="eyJhbGciOiJSUzI1NiIs.fake")

        self.assertFalse(res["ok"])
        self.assertEqual(res["status"], "unresolved")
        self.assertIn("未返回可轮询任务ID", res["message"])

    def test_status_reads_fail_reason_from_upstream_task(self):
        class FakeResponse:
            status_code = 200
            def __init__(self, data): self._data = data
            def json(self): return self._data

        def fake_request(method, url, **kwargs):
            self.assertIn("/api/task/status", url)
            return FakeResponse({
                "tasks": [
                    {
                        "task_id": "task-fail",
                        "email": "user@example.com",
                        "status": "FAILED",
                        "fail_reason": "Billing country must match request country",
                    }
                ],
            })

        with patch.object(client_module.cffi_requests, "request", fake_request):
            client = BaxiGptClient()
            res = client.status("CDK-FAIL::task-fail")

        self.assertEqual(res["status"], "failed")
        self.assertEqual(res["message"], "Billing country must match request country")


if __name__ == "__main__":
    unittest.main()
