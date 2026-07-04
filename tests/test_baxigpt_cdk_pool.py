import unittest
from unittest.mock import patch

from sqlmodel import SQLModel, create_engine, Session

from core import db as core_db
from core.db import AccountModel
from api import baxigpt_cdk_pool as api_module
from services.chatgpt_core import baxigpt_cdk_repository as repo_module
from services.chatgpt_core import baxigpt_client as client_module
from services.chatgpt_core import baxigpt_status_poller as poller_module
from services.chatgpt_core.baxigpt_client import BaxiGptClient, BaxiGptRequestError
from services.chatgpt_core.baxigpt_cdk_repository import BaxiGptCdkRepository, mask_code


class BaxiGptCdkRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        self.core_engine_patch = patch.object(core_db, "engine", self.engine)
        self.repo_engine_patch = patch.object(repo_module, "engine", self.engine)
        self.core_engine_patch.start()
        self.repo_engine_patch.start()
        SQLModel.metadata.create_all(self.engine)
        core_db._ensure_baxigpt_cdk_pool_schema()

    def tearDown(self):
        poller_module.stop()
        with poller_module._lock:
            poller_module._targets.clear()
        self.repo_engine_patch.stop()
        self.core_engine_patch.stop()

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
