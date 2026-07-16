from datetime import timedelta
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sqlmodel import SQLModel, create_engine

from core import db as core_db
from core.db import AccountModel
from services.chatgpt_core import phone_api_forwarding as forwarding
from services.chatgpt_core import phone_pool_repository as repo_module
from services.chatgpt_core.phone_pool_repository import PhonePoolRepository, serialize_phone_pool_records


class PhonePoolRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.forwarding_state_before = dict(forwarding._SYNC_STATE)
        with forwarding._LOCK:
            forwarding._SYNC_STATE.update({
                "status": "idle",
                "last_attempt_at": "",
                "last_success_at": "",
                "last_error": "",
                "inventory_count": 0,
                "route_count": 0,
                "owner_count": 0,
                "trigger": "",
            })
        self.engine = create_engine("sqlite://")
        self.core_engine_patch = patch.object(core_db, "engine", self.engine)
        self.repo_engine_patch = patch.object(repo_module, "engine", self.engine)
        self.core_engine_patch.start()
        self.repo_engine_patch.start()
        SQLModel.metadata.create_all(self.engine)
        core_db._ensure_phone_pool_schema()

    def tearDown(self):
        self.repo_engine_patch.stop()
        self.core_engine_patch.stop()
        with forwarding._LOCK:
            forwarding._SYNC_STATE.clear()
            forwarding._SYNC_STATE.update(self.forwarding_state_before)

    def test_import_pick_and_record_success(self):
        repo = PhonePoolRepository()
        summary = repo.import_lines("""
+15551230001----https://relay.example.com/a
+15551230002----https://relay.example.com/b
bad-line
""")
        self.assertEqual(summary["added"], 2)
        self.assertEqual(len(summary["errors"]), 1)

        picked = repo.pick_available(4)
        self.assertEqual([item.phone_e164 for item in picked], ["+15551230001", "+15551230002"])
        phone_items = repo.to_phone_items(picked, limit_accounts=4)
        self.assertEqual(len(phone_items), 2)
        self.assertTrue(all(item["pool_managed"] for item in phone_items))
        expanded_items = repo.to_phone_items(picked, limit_accounts=4, expand_capacity=True)
        self.assertEqual(len(expanded_items), 4)

        for _ in range(3):
            rec = repo.record_success("+15551230001")
        self.assertEqual(rec.bound_count, 3)
        self.assertEqual(rec.status, "exhausted")
        self.assertFalse(rec.available)


    def test_import_upserts_api_only_and_requeues_expiry_probe(self):
        repo = PhonePoolRepository()
        summary = repo.import_lines("+15551230001----https://relay.example.com/a----2026-07-01")
        self.assertEqual(summary["added"], 1)
        self.assertEqual(summary["warnings"][0]["reason"], "导入只使用手机号和 API，已忽略多余字段")

        rec = repo.get("+15551230001")
        self.assertEqual(rec.api_url, "https://relay.example.com/a")
        self.assertEqual(rec.api_expired_date, "")
        self.assertEqual(repo.to_phone_items([rec])[0]["raw_line"], "+15551230001----https://relay.example.com/a")

        updated = repo.update_api_expired_date("+15551230001", "2026-08-02")
        self.assertEqual(updated.api_expired_date, "2026-08-02")

        summary = repo.import_lines("+15551230001----https://relay.example.com/a2")
        self.assertEqual(summary["updated"], 1)
        self.assertEqual(summary["api_replaced"], 1)
        self.assertEqual(len(summary["refresh_ids"]), 1)
        saved = repo.get("+15551230001")
        self.assertEqual(saved.api_url, "https://relay.example.com/a2")
        self.assertEqual(saved.api_expired_date, "")
        self.assertEqual(saved.api_expiry_checked_at, "")


    def test_import_accepts_pipe_phone_api_lines(self):
        repo = PhonePoolRepository()
        summary = repo.import_lines("+12082260171|https://sms24.uk/api/sms/recordText?token=demo&tpl=1")

        self.assertEqual(summary["added"], 1)
        self.assertFalse(summary["errors"])
        rec = repo.get("+12082260171")
        self.assertEqual(rec.api_url, "https://sms24.uk/api/sms/recordText?token=demo&tpl=1")
        self.assertEqual(repo.to_phone_items([rec])[0]["raw_line"], "+12082260171----https://sms24.uk/api/sms/recordText?token=demo&tpl=1")


    def test_import_duplicate_phone_uses_last_api_without_resetting_runtime_state(self):
        repo = PhonePoolRepository()
        rec = repo.add(phone="+15551230001", api_url="https://relay.example.com/old")
        repo.record_task_status(rec.phone_e164, "openai_rejected", reason="OpenAI 拒绝")
        before = repo.get("+15551230001")
        self.assertEqual(before.status, "cannot_send")
        self.assertEqual(before.fail_count, 1)

        summary = repo.import_lines("""
+15551230001----https://relay.example.com/mid
+15551230002----https://relay.example.com/second
+15551230001----https://relay.example.com/new
""")
        self.assertEqual(summary["added"], 1)
        self.assertEqual(summary["updated"], 1)
        self.assertEqual(summary["deduped"], 1)
        self.assertEqual(summary["api_replaced"], 1)

        saved = repo.get("+15551230001")
        self.assertEqual(saved.api_url, "https://relay.example.com/new")
        self.assertEqual(saved.status, "cannot_send")
        self.assertEqual(saved.fail_count, 1)
        self.assertEqual(saved.last_error_code, "openai_rejected")


    def test_refresh_api_expiry_fetches_fixed_expired_date_once(self):
        repo = PhonePoolRepository()
        rec = repo.add(phone="+15551230001", api_url="https://relay.example.com/a")

        with patch.object(repo_module.requests, "get") as mock_get:
            mock_get.return_value.status_code = 200
            mock_get.return_value.json.return_value = {"data": {"expired_date": "2026-07-01", "code": ""}}
            result = repo.refresh_api_expiry_for_ids([rec.id])

        self.assertEqual(result["summary"]["checked"], 1)
        self.assertEqual(result["summary"]["success"], 1)
        saved = repo.get("+15551230001")
        self.assertEqual(saved.api_expired_date, "2026-07-01")
        self.assertEqual(saved.api_expiry_status, "ok")
        self.assertTrue(saved.api_expiry_checked_at)
        self.assertFalse(saved.api_expiry_error)

        with patch.object(repo_module.requests, "get") as mock_get:
            result = repo.refresh_api_expiry_for_ids([rec.id])

        mock_get.assert_not_called()
        self.assertEqual(result["summary"]["skipped"], 1)

    def test_refresh_api_expiry_accepts_sms24_expire_time(self):
        repo = PhonePoolRepository()
        rec = repo.add(phone="+12082260171", api_url="https://sms24.uk/api/sms/recordText?token=demo&tpl=1")

        with patch.object(repo_module.requests, "get") as mock_get:
            mock_get.return_value.status_code = 200
            mock_get.return_value.json.return_value = {
                "code": False,
                "message": "暂无短信",
                "expireTime": "2026-08-05 21:00:59",
            }
            result = repo.refresh_api_expiry_for_ids([rec.id])

        self.assertEqual(result["summary"]["checked"], 1)
        self.assertEqual(result["summary"]["success"], 1)
        saved = repo.get("+12082260171")
        self.assertEqual(saved.api_expired_date, "2026-08-05 21:00:59")
        self.assertEqual(saved.api_expiry_status, "ok")

    def test_refresh_api_expiry_records_missing_or_error_once(self):
        repo = PhonePoolRepository()
        rec = repo.add(phone="+15551230002", api_url="https://relay.example.com/b")

        with patch.object(repo_module.requests, "get") as mock_get:
            mock_get.return_value.status_code = 200
            mock_get.return_value.json.return_value = {"data": {"code": ""}}
            result = repo.refresh_api_expiry_for_ids([rec.id])

        self.assertEqual(result["summary"]["missing_expired_date"], 1)
        saved = repo.get("+15551230002")
        self.assertEqual(saved.api_expiry_status, "missing_expired_date")
        self.assertIn("expireTime", saved.api_expiry_error)

    def test_import_returns_refresh_ids_for_blank_expiry(self):
        repo = PhonePoolRepository()
        result = repo.import_lines("+15551230003----https://relay.example.com/c")
        self.assertEqual(result["added"], 1)
        self.assertEqual(len(result["refresh_ids"]), 1)

        result = repo.import_lines("+15551230004----https://relay.example.com/d----2026-07-01")
        self.assertEqual(result["added"], 1)
        self.assertEqual(len(result["refresh_ids"]), 1)
        self.assertEqual(len(result["warnings"]), 1)

    def test_record_success_tracks_bound_account_emails(self):
        repo = PhonePoolRepository()
        repo.add(phone="+15551230001", api_url="https://relay.example.com/a")

        rec = repo.record_success("+15551230001", email="User@Example.com")
        self.assertEqual(rec.bound_count, 1)
        self.assertEqual(rec.bound_account_emails, ["user@example.com"])

        rec = repo.record_success("+15551230001", email="user@example.com")
        self.assertEqual(rec.bound_count, 1)
        self.assertEqual(rec.success_count, 2)
        self.assertEqual(rec.bound_account_emails, ["user@example.com"])

        rec = repo.record_task_status("+15551230001", "bound", email="next@example.com")
        self.assertEqual(rec.bound_count, 2)
        self.assertEqual(rec.bound_account_emails, ["user@example.com", "next@example.com"])


    def test_phone_signup_prefix_status_does_not_mutate_phone_record(self):
        repo = PhonePoolRepository()
        phone = "+13430000001"
        repo.add(phone=phone, api_url="https://relay.example.com/a")

        prefix_state = repo.record_phone_signup_prefix_status(phone, "api_no_code", reason="未收到短信验证码")
        saved = repo.get(phone)

        self.assertIsNone(prefix_state)
        self.assertEqual(saved.status, "active")
        self.assertEqual(saved.fail_count, 0)
        self.assertEqual(saved.last_error_code, "")
        self.assertEqual(repo.summarize(repo.list())["phone_signup_unavailable_prefix_count"], 0)

        prefix_state = repo.record_phone_signup_prefix_status(
            phone,
            "openai_rejected",
            reason="Phone number already in use. Please try again. code=phone_number_in_use",
        )
        self.assertIsNone(prefix_state)

        prefix_state = repo.record_phone_signup_prefix_status(
            phone,
            "openai_rejected",
            reason="We've detected suspicious behavior from phone numbers similar to yours. code=fraud_guard",
        )
        saved = repo.get(phone)

        self.assertEqual(prefix_state["prefix"], "1343")
        self.assertEqual(prefix_state["status"], "unavailable")
        self.assertEqual(saved.status, "active")
        self.assertEqual(saved.fail_count, 0)
        self.assertEqual(saved.last_error_code, "")

        summary = repo.summarize(repo.list())
        self.assertEqual(summary["phone_signup_unavailable_prefix_count"], 1)
        self.assertEqual(summary["phone_signup_unavailable_prefixes"][0]["prefix"], "1343")
        self.assertEqual(summary["phone_signup_unavailable_prefixes"][0]["last_error_code"], "fraud_guard")

        prefix_state = repo.record_phone_signup_prefix_status("+13439990000", "registered_phone_signup", reason="注册成功")
        saved_after_success = repo.get(phone)
        self.assertEqual(prefix_state["status"], "available")
        self.assertEqual(saved_after_success.status, "active")
        self.assertEqual(saved_after_success.bound_count, 0)

        summary = repo.summarize(repo.list())
        self.assertEqual(summary["phone_signup_available_prefix_count"], 1)
        self.assertEqual(summary["phone_signup_available_prefixes"][0]["prefix"], "1343")
        self.assertEqual(summary["phone_signup_available_prefixes"][0]["success_count"], 1)
        self.assertEqual(summary["phone_signup_available_prefixes"][0]["failure_count"], 1)

    def test_record_task_status_maps_runtime_feedback(self):
        repo = PhonePoolRepository()
        repo.add(phone="+15551230001", api_url="https://relay.example.com/a")

        rec = repo.record_task_status("+15551230001", "api_no_code", reason="未收到短信验证码")
        self.assertEqual(rec.status, "cannot_send")
        self.assertEqual(rec.fail_count, 1)

        rec = repo.reset_status(rec.id)
        self.assertEqual(rec.status, "active")

        rec = repo.record_task_status("+15551230001", "rate_limited", reason="429")
        self.assertEqual(rec.status, "rate_limited")
        self.assertTrue(rec.cooldown_until)
        recover_at = repo_module._parse_time(rec.cooldown_until)
        self.assertIsNotNone(recover_at)
        self.assertGreater((recover_at - repo_module._utcnow()).total_seconds(), 3500)

        recovered = repo.recover_expired_temporary_statuses(now=recover_at + timedelta(seconds=1))
        self.assertEqual(recovered, 1)
        rec = repo.get("+15551230001")
        self.assertEqual(rec.status, "active")
        self.assertEqual(rec.cooldown_until, "")
        self.assertEqual(rec.last_error_code, "")

    def test_phone_failure_and_success_only_write_back_observed_number(self):
        repo = PhonePoolRepository()
        rejected = repo.add(phone="+13430000001", api_url="https://relay.example.com/rejected")
        peer = repo.add(phone="+13430000002", api_url="https://relay.example.com/peer")
        full = repo.add(phone="+13430000003", api_url="https://relay.example.com/full", max_accounts=1)
        disabled = repo.add(phone="+13430000004", api_url="https://relay.example.com/disabled")
        repo.record_success(full.phone_e164)
        repo.set_enabled(disabled.id, False)

        repo.record_task_status(rejected.phone_e164, "openai_rejected", reason="OpenAI 拒绝")
        self.assertEqual(repo.get(rejected.phone_e164).status, "cannot_send")
        self.assertEqual(repo.get(peer.phone_e164).status, "active")
        self.assertEqual(repo.get(full.phone_e164).status, "exhausted")
        self.assertEqual(repo.get(disabled.phone_e164).status, "disabled")

        restored = repo.record_task_status(peer.phone_e164, "sms_probe_received", reason="发码并收码成功")
        self.assertEqual(restored.status, "active")
        self.assertEqual(repo.get(rejected.phone_e164).status, "cannot_send")
        self.assertEqual(repo.get(rejected.phone_e164).last_error_code, "openai_rejected")
        self.assertEqual(repo.get(full.phone_e164).status, "exhausted")
        self.assertEqual(repo.get(disabled.phone_e164).status, "disabled")

    def test_prefix_reconciliation_does_not_rewrite_mixed_rows(self):
        repo = PhonePoolRepository()
        active = repo.add(phone="+13430000001", api_url="https://relay.example.com/active")
        stale = repo.add(phone="+13430000002", api_url="https://relay.example.com/stale")
        repo.record_failure(
            stale.phone_e164,
            status="cannot_send",
            error_code="openai_rejected",
            error_message="legacy state",
        )

        result = repo.reconcile_prefix_availability()

        self.assertEqual(result, {"prefixes": 0, "records": 0})
        self.assertEqual(repo.get(active.phone_e164).status, "active")
        preserved = repo.get(stale.phone_e164)
        self.assertEqual(preserved.status, "cannot_send")
        self.assertEqual(preserved.last_error_code, "openai_rejected")

    def test_terminal_api_failure_marks_only_phone_and_prefix_becomes_partial(self):
        repo = PhonePoolRepository()
        failed = repo.add(phone="+14160000001", api_url="https://relay.example.com/failed")
        peer = repo.add(phone="+14160000002", api_url="https://relay.example.com/peer")

        repo.record_task_status(failed.phone_e164, "api_no_code", reason="收码 API 无验证码")

        self.assertEqual(repo.get(failed.phone_e164).status, "cannot_send")
        self.assertEqual(repo.get(peer.phone_e164).status, "active")
        summary = repo.summarize(repo.list())
        self.assertEqual(summary["prefix_health"]["partial"][0]["prefix"], "1416")
        self.assertEqual(summary["prefix_health"]["partial"][0]["available_count"], 1)
        self.assertEqual(summary["prefix_health"]["partial"][0]["cannot_send_count"], 1)
        self.assertEqual(summary["rejected_prefix_count"], 0)

    def test_api_forward_error_keeps_phone_active(self):
        repo = PhonePoolRepository()
        repo.add(phone="+15551230009", api_url="https://supplier.example/api?token=demo")
        repo.record_failure(
            "+15551230009",
            status="rate_limited",
            error_code="rate_limited",
            error_message="temporary",
            cooldown_seconds=600,
        )

        rec = repo.record_task_status(
            "+15551230009",
            "api_forward_error",
            reason="api_forward_error: relay unavailable",
        )

        self.assertEqual(rec.status, "active")
        self.assertTrue(rec.available)
        self.assertEqual(rec.cooldown_until, "")
        self.assertEqual(rec.last_error_code, "api_forward_error")

    def test_summary_keeps_exhausted_and_disabled_separate(self):
        repo = PhonePoolRepository()
        repo.add(phone="+12261230001", api_url="https://relay.example.com/a")
        repo.add(phone="+13431230002", api_url="https://relay.example.com/b")
        repo.add(phone="+14161230003", api_url="https://relay.example.com/c")
        repo.add(phone="+15871230004", api_url="https://relay.example.com/d")

        for _ in range(3):
            repo.record_success("+13431230002")
        repo.record_task_status("+14161230003", "api_no_code", reason="未收到短信验证码")
        disabled = repo.get("+15871230004")
        repo.set_enabled(disabled.id, False)

        summary = repo.summarize(repo.list())
        self.assertEqual(summary["available"], 1)
        self.assertEqual(summary["remaining_capacity"], 3)
        self.assertEqual(summary["unavailable"], 1)
        self.assertEqual(summary["exhausted"], 1)
        self.assertEqual(summary["disabled"], 1)

    def test_summary_includes_openai_rejected_prefixes(self):
        repo = PhonePoolRepository()
        repo.add(phone="+13434832962", api_url="https://relay.example.com/a")
        repo.add(phone="+13434832712", api_url="https://relay.example.com/b")
        repo.add(phone="+12269013018", api_url="https://relay.example.com/c")
        repo.add(phone="+14169023650", api_url="https://relay.example.com/d")

        repo.record_task_status("+13434832962", "openai_rejected", reason="detected suspicious behavior from phone numbers")
        repo.record_task_status("+13434832712", "openai_rejected", reason="detected suspicious behavior from phone numbers")
        repo.record_task_status("+12269013018", "openai_rejected", reason="detected suspicious behavior from phone numbers")
        repo.record_task_status("+14169023650", "api_no_code", reason="未收到短信验证码")

        summary = repo.summarize(repo.list())

        self.assertEqual(summary["rejected_phone_count"], 3)
        self.assertEqual(summary["rejected_prefix_count"], 2)
        self.assertEqual(
            [(item["prefix"], item["count"], item["status"]) for item in summary["rejected_prefixes"]],
            [("1343", 2, "unavailable"), ("1226", 1, "unavailable")],
        )
        self.assertEqual(summary["rejected_prefix_sample_1"], 2)
        self.assertEqual(summary["rejected_prefix_sample_2"], 3)

    def test_summary_includes_available_prefixes_with_capacity(self):
        repo = PhonePoolRepository()
        repo.add(phone="+13434832962", api_url="https://relay.example.com/a")
        repo.add(phone="+13434832712", api_url="https://relay.example.com/b", max_accounts=2)
        repo.add(phone="+12269013018", api_url="https://relay.example.com/c")
        rejected = repo.add(phone="+12269023650", api_url="https://relay.example.com/d")

        repo.record_success("+13434832712")
        repo.record_task_status(rejected.phone_e164, "openai_rejected", reason="OpenAI 拒绝")

        summary = repo.summarize(repo.list())

        self.assertEqual(summary["available_prefix_count"], 2)
        self.assertEqual(summary["healthy_prefix_count"], 1)
        self.assertEqual(summary["partial_prefix_count"], 1)
        self.assertEqual(
            [(item["prefix"], item["available_count"], item["remaining_capacity"], item["status"]) for item in summary["available_prefixes"]],
            [("1343", 2, 4, "available"), ("1226", 1, 3, "partial")],
        )
        self.assertEqual(summary["rejected_prefix_count"], 1)
        self.assertEqual(summary["available"], 3)
        self.assertEqual(summary["number_available"], 3)
        self.assertEqual(
            [item.phone_e164 for item in repo.list_available()],
            ["+13434832962", "+12269013018", "+13434832712"],
        )


    def test_serialized_rows_keep_active_peer_eligible_in_mixed_prefix(self):
        repo = PhonePoolRepository()
        active_in_bad_prefix = repo.add(phone="+13430000001", api_url="https://relay.example.com/active")
        rejected_same_prefix = repo.add(phone="+13430000002", api_url="https://relay.example.com/rejected")
        healthy = repo.add(phone="+12260000001", api_url="https://relay.example.com/healthy")

        repo.record_task_status(rejected_same_prefix.phone_e164, "openai_rejected", reason="OpenAI 拒绝")

        rows = serialize_phone_pool_records(repo.list(), all_records=repo.list())
        by_phone = {row["phone_e164"]: row for row in rows}

        bad_prefix_active = by_phone[active_in_bad_prefix.phone_e164]
        self.assertTrue(bad_prefix_active["self_available"])
        self.assertEqual(bad_prefix_active["prefix_status"], "partial")
        self.assertTrue(bad_prefix_active["ordinary_task_eligible"])
        self.assertEqual(bad_prefix_active["ordinary_task_block_reason"], "")

        rejected_row = by_phone[rejected_same_prefix.phone_e164]
        self.assertFalse(rejected_row["self_available"])
        self.assertEqual(rejected_row["prefix_status"], "partial")
        self.assertFalse(rejected_row["ordinary_task_eligible"])
        self.assertEqual(rejected_row["ordinary_task_block_reason"], "openai_rejected")

        healthy_row = by_phone[healthy.phone_e164]
        self.assertTrue(healthy_row["self_available"])
        self.assertEqual(healthy_row["prefix_status"], "available")
        self.assertTrue(healthy_row["ordinary_task_eligible"])
        self.assertEqual(healthy_row["ordinary_task_block_reason"], "")

        self.assertEqual(
            [item.phone_e164 for item in repo.list_available()],
            [active_in_bad_prefix.phone_e164, healthy.phone_e164],
        )

    def test_serialized_rows_fetch_forwarding_config_once_and_keep_source_contract(self):
        repo = PhonePoolRepository()
        source_a = "https://supplier-a.example/api/code?token=a"
        source_b = "https://supplier-b.example/v1/otp?token=b&x=1"
        first = repo.add(phone="+13430000001", api_url=source_a)
        second = repo.add(phone="+12260000001", api_url=source_b)
        config = {
            "enabled": True,
            "active_origin": "https://phone-api.aa8.pl",
            "previous_origins": [],
            "relay_configured": True,
            "forward_status": "active",
        }

        # This unittest module shares the process with Relay integration tests;
        # clear a previous inventory-conflict marker before asserting the
        # pure serialization contract.
        with forwarding._LOCK:
            previous_sync_state = dict(forwarding._SYNC_STATE)
            forwarding._SYNC_STATE.update(
                {
                    "status": "synced",
                    "last_attempt_at": "2026-07-14T00:00:00Z",
                    "last_error": "",
                }
            )
        try:
            with patch.object(forwarding, "get_forwarding_config", return_value=config) as get_config:
                rows = serialize_phone_pool_records([first, second], all_records=[first, second])
        finally:
            with forwarding._LOCK:
                forwarding._SYNC_STATE.clear()
                forwarding._SYNC_STATE.update(previous_sync_state)

        self.assertEqual(get_config.call_count, 1)
        by_phone = {row["phone_e164"]: row for row in rows}
        row_a = by_phone[first.phone_e164]
        self.assertEqual(row_a["api_url"], source_a)
        self.assertEqual(row_a["source_api_url"], source_a)
        self.assertEqual(row_a["forwarded_api_url"], "https://phone-api.aa8.pl/api/code?token=a")
        self.assertEqual(row_a["forwarded_api_host"], "phone-api.aa8.pl")
        row_b = by_phone[second.phone_e164]
        self.assertEqual(row_b["api_url"], source_b)
        self.assertEqual(row_b["source_api_url"], source_b)
        self.assertEqual(row_b["forwarded_api_url"], "https://phone-api.aa8.pl/v1/otp?token=b&x=1")

    def test_serialized_rows_relay_unavailable_do_not_retry_per_record(self):
        repo = PhonePoolRepository()
        records = [
            repo.add(phone="+13430000001", api_url="https://supplier.example/a?token=a"),
            repo.add(phone="+13430000002", api_url="https://supplier.example/b?token=b"),
            repo.add(phone="+12260000001", api_url="https://supplier.example/c?token=c"),
        ]
        unavailable = {
            "enabled": False,
            "active_origin": "",
            "previous_origins": [],
            "relay_configured": True,
            "forward_status": "unavailable",
            "relay_error": "手机号 API Relay 暂时不可达",
        }
        with patch.object(forwarding, "get_forwarding_config", return_value=unavailable) as get_config:
            rows = serialize_phone_pool_records(records, all_records=records)

        self.assertEqual(get_config.call_count, 1)
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row["forward_status"] == "unavailable" for row in rows))
        self.assertTrue(all(row["forwarded_api_url"] == "" for row in rows))

    def test_sample_testable_by_prefix_covers_each_prefix_before_second_sample(self):
        repo = PhonePoolRepository()
        records = [
            SimpleNamespace(id=1, phone_e164="+13430000001", success_count=4, fail_count=0, last_used_at="2026-06-01T00:00:00Z"),
            SimpleNamespace(id=2, phone_e164="+13430000002", success_count=0, fail_count=0, last_used_at=""),
            SimpleNamespace(id=3, phone_e164="+12260000001", success_count=0, fail_count=0, last_used_at=""),
            SimpleNamespace(id=4, phone_e164="+12260000002", success_count=1, fail_count=0, last_used_at="2026-06-02T00:00:00Z"),
            SimpleNamespace(id=5, phone_e164="+16720000001", success_count=0, fail_count=0, last_used_at=""),
        ]

        with patch.object(repo, "list_prefix_sample_candidates", return_value=records):
            sampled = repo.sample_testable_by_prefix(2)

        self.assertEqual(
            [item.phone_e164 for item in sampled],
            [
                "+12260000001",
                "+13430000002",
                "+16720000001",
                "+12260000002",
                "+13430000001",
            ],
        )

    def test_sample_available_by_prefix_only_uses_healthily_available_records(self):
        repo = PhonePoolRepository()
        healthy_a = repo.add(phone="+12260000001", api_url="https://relay.example.com/healthy-a")
        healthy_b = repo.add(phone="+12260000002", api_url="https://relay.example.com/healthy-b")
        rejected_a = repo.add(phone="+13430000001", api_url="https://relay.example.com/rejected-a")
        rejected_b = repo.add(phone="+13430000002", api_url="https://relay.example.com/rejected-b")
        rate_limited = repo.add(phone="+14160000001", api_url="https://relay.example.com/rate-limited")

        repo.record_task_status(rejected_a.phone_e164, "openai_rejected", reason="OpenAI 拒绝")
        repo.record_task_status(rejected_b.phone_e164, "openai_rejected", reason="OpenAI 拒绝")
        repo.record_task_status(rate_limited.phone_e164, "rate_limited", reason="429")

        summary = repo.summarize(repo.list())
        self.assertEqual(summary["available_prefix_count"], 1)
        self.assertEqual(summary["available_prefixes"][0]["prefix"], "1226")

        sampled = repo.sample_available_by_prefix(2)
        self.assertEqual([item.phone_e164 for item in sampled], [healthy_a.phone_e164, healthy_b.phone_e164])
        self.assertNotIn(rejected_a.phone_e164, [item.phone_e164 for item in sampled])
        self.assertNotIn(rate_limited.phone_e164, [item.phone_e164 for item in sampled])

    def test_prefix_sample_candidates_include_unavailable_phones_without_pre_restore(self):
        repo = PhonePoolRepository()
        active = repo.add(phone="+12260000001", api_url="https://relay.example.com/active")
        cannot_send = repo.add(phone="+13430000001", api_url="https://relay.example.com/cannot-send")
        rate_limited = repo.add(phone="+14160000001", api_url="https://relay.example.com/rate-limit")
        exhausted = repo.add(phone="+15870000001", api_url="https://relay.example.com/exhausted")
        disabled = repo.add(phone="+16720000001", api_url="https://relay.example.com/disabled")

        repo.record_task_status(cannot_send.phone_e164, "openai_rejected", reason="OpenAI 拒绝")
        repo.record_task_status(rate_limited.phone_e164, "rate_limited", reason="429")
        for _ in range(3):
            repo.record_success(exhausted.phone_e164)
        repo.set_enabled(disabled.id, False)

        summary = repo.summarize(repo.list())
        self.assertEqual(summary["available_prefix_count"], 1)
        self.assertEqual(summary["prefix_sample_prefix_count"], 3)
        self.assertEqual(summary["prefix_sample_count_1"], 3)

        sampled = repo.sample_testable_by_prefix(1)
        self.assertEqual(
            [item.phone_e164 for item in sampled],
            [active.phone_e164, cannot_send.phone_e164, rate_limited.phone_e164],
        )

        selected_again = repo.restore_prefix_sample_records(
            [cannot_send.id, rate_limited.id, exhausted.id, disabled.id]
        )
        selected_by_phone = {record.phone_e164: record for record in selected_again}
        self.assertEqual(set(selected_by_phone), {cannot_send.phone_e164, rate_limited.phone_e164})
        self.assertEqual(selected_by_phone[cannot_send.phone_e164].status, "cannot_send")
        self.assertEqual(selected_by_phone[cannot_send.phone_e164].last_error_code, "openai_rejected")
        self.assertEqual(selected_by_phone[rate_limited.phone_e164].status, "rate_limited")
        self.assertTrue(selected_by_phone[rate_limited.phone_e164].cooldown_until)
        self.assertEqual(repo.get(cannot_send.phone_e164).status, "cannot_send")
        self.assertEqual(repo.get(rate_limited.phone_e164).status, "rate_limited")
        self.assertEqual(repo.get(exhausted.phone_e164).status, "exhausted")
        self.assertEqual(repo.get(disabled.phone_e164).status, "disabled")

    def test_sample_rejected_by_prefix_only_uses_openai_rejected_records(self):
        repo = PhonePoolRepository()
        active = repo.add(phone="+12260000001", api_url="https://relay.example.com/active")
        rejected_a = repo.add(phone="+13430000001", api_url="https://relay.example.com/rejected-a")
        rejected_b = repo.add(phone="+13430000002", api_url="https://relay.example.com/rejected-b")
        api_failed = repo.add(phone="+14160000001", api_url="https://relay.example.com/api-failed")

        repo.record_task_status(rejected_a.phone_e164, "openai_rejected", reason="OpenAI 拒绝")
        repo.record_task_status(rejected_b.phone_e164, "openai_rejected", reason="OpenAI 拒绝")
        repo.record_task_status(api_failed.phone_e164, "api_no_code", reason="未收到短信验证码")

        sampled = repo.sample_rejected_by_prefix(2)

        self.assertEqual([item.phone_e164 for item in sampled], [rejected_a.phone_e164, rejected_b.phone_e164])
        self.assertNotIn(active.phone_e164, [item.phone_e164 for item in sampled])
        self.assertNotIn(api_failed.phone_e164, [item.phone_e164 for item in sampled])

    def test_sample_selected_prefixes_ignores_prefix_health_filter(self):
        repo = PhonePoolRepository()
        healthy = repo.add(phone="+12260000001", api_url="https://relay.example.com/healthy")
        rejected = repo.add(phone="+13430000001", api_url="https://relay.example.com/rejected")
        rate_limited = repo.add(phone="+14160000001", api_url="https://relay.example.com/rate-limited")
        exhausted = repo.add(phone="+15870000001", api_url="https://relay.example.com/exhausted")

        repo.record_task_status(rejected.phone_e164, "openai_rejected", reason="OpenAI 拒绝")
        repo.record_task_status(rate_limited.phone_e164, "rate_limited", reason="429")
        for _ in range(3):
            repo.record_success(exhausted.phone_e164)

        sampled = repo.sample_selected_prefixes(["1343", "1416", "1587"], 1)

        self.assertEqual([item.phone_e164 for item in sampled], [rejected.phone_e164, rate_limited.phone_e164])
        self.assertNotIn(healthy.phone_e164, [item.phone_e164 for item in sampled])
        self.assertNotIn(exhausted.phone_e164, [item.phone_e164 for item in sampled])

    def test_list_available_by_prefixes_uses_ordinary_binding_rules(self):
        repo = PhonePoolRepository()
        healthy = repo.add(phone="+13430000001", api_url="https://relay.example.com/healthy")
        other = repo.add(phone="+12260000001", api_url="https://relay.example.com/other")
        rejected = repo.add(phone="+13430000002", api_url="https://relay.example.com/rejected")
        exhausted = repo.add(phone="+13430000003", api_url="https://relay.example.com/exhausted")

        repo.record_task_status(rejected.phone_e164, "openai_rejected", reason="OpenAI 拒绝")
        for _ in range(3):
            repo.record_success(exhausted.phone_e164)

        items = repo.list_available_by_prefixes(["1343"])

        self.assertEqual([item.phone_e164 for item in items], [healthy.phone_e164])
        self.assertNotIn(other.phone_e164, [item.phone_e164 for item in items])
        self.assertNotIn(exhausted.phone_e164, [item.phone_e164 for item in items])

    def test_sms_probe_success_restores_phone_and_prefix_availability(self):
        repo = PhonePoolRepository()
        phone = repo.add(phone="+13430000001", api_url="https://relay.example.com/probe")

        repo.record_task_status(phone.phone_e164, "openai_rejected", reason="OpenAI 拒绝")
        self.assertEqual(repo.get(phone.phone_e164).status, "cannot_send")
        self.assertEqual(repo.summarize(repo.list())["rejected_prefix_count"], 1)

        restored = repo.record_task_status(phone.phone_e164, "sms_probe_received", reason="OpenAI 已发码且收码 API 已收到验证码")

        self.assertIsNotNone(restored)
        self.assertEqual(restored.status, "active")
        self.assertEqual(restored.bound_count, 0)
        self.assertFalse(restored.last_error_code)
        summary = repo.summarize(repo.list())
        self.assertEqual(summary["available_prefix_count"], 1)
        self.assertEqual(summary["available_prefixes"][0]["prefix"], "1343")
        self.assertEqual(summary["rejected_prefix_count"], 0)

    def test_reconcile_from_accounts(self):
        with repo_module.Session(self.engine) as session:
            account = AccountModel(platform="chatgpt", email="a@example.com", password="pw")
            account.set_extra({
                "chatgpt_phone_binding": {
                    "status": "bound",
                    "phone": "+15551230001",
                    "api_url": "https://relay.example.com/a",
                }
            })
            session.add(account)
            session.commit()

        repo = PhonePoolRepository()
        summary = repo.reconcile_from_accounts()
        self.assertEqual(summary["counted_phones"], 1)
        rec = repo.get("+15551230001")
        self.assertIsNotNone(rec)
        self.assertEqual(rec.bound_count, 1)
        self.assertEqual(rec.bound_account_emails, ["a@example.com"])
        self.assertEqual(rec.api_host, "relay.example.com")


if __name__ == "__main__":
    unittest.main()
