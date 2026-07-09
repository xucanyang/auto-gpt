import threading
import unittest
from unittest.mock import patch

import api.actions as api_actions
from api.tasks import _create_standalone_task_record, _run_phone_binding_test, _task_store


class PhoneBindingAssignmentTests(unittest.TestCase):
    def setUp(self):
        _task_store._records.clear()

    def test_phone_terminal_failure_retries_same_account_with_next_phone(self):
        task_id = "task-phone-retry-same-account"
        _create_standalone_task_record(
            task_id,
            platform="chatgpt",
            source="phone_binding_test",
            total=3,
            meta={"missing_ids": [], "parse_errors": []},
        )
        phone_items = [
            {
                "line_no": 1,
                "phone": "+11111111111",
                "api_url": "https://example.com/api/bad",
                "raw_line": "+11111111111----https://example.com/api/bad",
            },
            {
                "line_no": 2,
                "phone": "+11111111111",
                "api_url": "https://example.com/api/bad-duplicate",
                "raw_line": "+11111111111----https://example.com/api/bad-duplicate",
            },
            {
                "line_no": 3,
                "phone": "+12222222222",
                "api_url": "https://example.com/api/good",
                "raw_line": "+12222222222----https://example.com/api/good",
            },
        ]

        class _FakeAccount:
            def __init__(self, account_id: int):
                self.id = account_id
                self.platform = "chatgpt"
                self.email = f"same{account_id}@example.com"
                self.extra = {}

            def get_extra(self):
                return dict(self.extra)

            def set_extra(self, extra):
                self.extra = dict(extra or {})

        accounts_by_id = {}

        class _FakeSession:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def get(self, _model, account_id):
                account_id_value = int(account_id or 0)
                if account_id_value not in accounts_by_id:
                    accounts_by_id[account_id_value] = _FakeAccount(account_id_value)
                return accounts_by_id[account_id_value]

            def refresh(self, _account):
                return None

            def add(self, _account):
                return None

            def commit(self):
                return None

        attempts = []

        def _fake_execute(account, allow_phone_verification=False, log_fn=None, shared_phone_service=None, stop_checker=None, retry_delays_seconds=None, proxy_url=None, phone_sms_probe_only=False):
            entry = shared_phone_service.current_entry
            attempts.append((int(account.id or 0), entry.phone))
            if entry.phone == "+11111111111":
                shared_phone_service.mark_sms_sent(entry)
                return {
                    "ok": False,
                    "error": "add_phone 阶段失败: add-phone/send 失败: This phone number is already linked to the maximum number of accounts.",
                    "data": {"message": "This phone number is already linked to the maximum number of accounts."},
                }
            shared_phone_service.mark_sms_sent(entry)
            shared_phone_service.last_code_time = "2026-06-04 00:00:00"
            shared_phone_service.complete(entry)
            return {"ok": True, "data": {"message": "done"}}

        with (
            patch("api.tasks.Session", return_value=_FakeSession()),
            patch.object(api_actions, "_execute_chatgpt_resume_subscription_auth", side_effect=_fake_execute),
            patch.object(api_actions, "_apply_chatgpt_resume_auth_result"),
            patch("api.tasks._save_task_log"),
            patch("api.tasks.time.monotonic", side_effect=[100.0, 200.0, 300.0, 400.0]),
        ):
            _run_phone_binding_test(
                task_id,
                [456],
                phone_items,
                {
                    "timeout_seconds": 180,
                    "poll_interval_seconds": 5,
                    "max_resend_attempts": 0,
                    "resend_interval_seconds": 0,
                    "account_interval_seconds": 60,
                    "proxy_mode": "direct",
                },
            )

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(attempts, [(456, "+11111111111"), (456, "+12222222222")])
        self.assertEqual(snapshot["success"], 1)
        statuses = [item["status"] for item in snapshot["meta"]["runtime_results"]]
        self.assertEqual(statuses, ["openai_phone_limit", "bound"])
        saved_binding = accounts_by_id[456].extra["chatgpt_phone_binding"]
        self.assertEqual(saved_binding["phone"], "+12222222222")
        self.assertTrue(any("账号将继续尝试下一个手机号" in line for line in snapshot["logs"]))
        self.assertTrue(any("已在本轮判定不可继续使用，跳过重复 slot" in line for line in snapshot["logs"]))

    def test_phone_completed_retries_auth_capture_without_rebinding_phone(self):
        task_id = "task-phone-auth-retry-after-bound"
        _create_standalone_task_record(
            task_id,
            platform="chatgpt",
            source="phone_binding_test",
            total=1,
            meta={"missing_ids": [], "parse_errors": []},
        )
        phone_items = [
            {
                "line_no": 1,
                "phone": "+13434832954",
                "api_url": "https://example.com/api/phone",
                "raw_line": "+13434832954----https://example.com/api/phone",
            },
        ]

        class _FakeAccount:
            def __init__(self, account_id: int):
                self.id = account_id
                self.platform = "chatgpt"
                self.email = f"retry{account_id}@example.com"
                self.extra = {}

            def get_extra(self):
                return dict(self.extra)

            def set_extra(self, extra):
                self.extra = dict(extra or {})

        accounts_by_id = {}

        class _FakeSession:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def get(self, _model, account_id):
                account_id_value = int(account_id or 0)
                if account_id_value not in accounts_by_id:
                    accounts_by_id[account_id_value] = _FakeAccount(account_id_value)
                return accounts_by_id[account_id_value]

            def refresh(self, _account):
                return None

            def add(self, _account):
                return None

            def commit(self):
                return None

        calls = []

        def _fake_execute(account, allow_phone_verification=False, log_fn=None, shared_phone_service=None, stop_checker=None, retry_delays_seconds=None, proxy_url=None, phone_sms_probe_only=False):
            calls.append(
                {
                    "account_id": int(account.id or 0),
                    "allow_phone_verification": allow_phone_verification,
                    "has_phone_service": shared_phone_service is not None,
                    "retry_delays_seconds": retry_delays_seconds,
                    "phone": getattr(getattr(shared_phone_service, "current_entry", None), "phone", ""),
                }
            )
            if shared_phone_service is not None:
                entry = shared_phone_service.current_entry
                shared_phone_service.mark_sms_sent(entry)
                shared_phone_service.last_expired_date = "2026-07-06 00:00:00"
                shared_phone_service.last_code_time = "2026-06-09 09:50:28"
                shared_phone_service.last_code_was_extracted = True
                shared_phone_service.complete(entry)
                return {
                    "ok": False,
                    "error": "organization/select 失败: HTTP 400, code=duplicate, type=invalid_request_error, message=Organization already has a default project.",
                    "data": {"message": "Organization already has a default project."},
                }
            return {"ok": True, "data": {"message": "done", "auth_capture": {"ok": True}}}

        with (
            patch("api.tasks.Session", return_value=_FakeSession()),
            patch.object(api_actions, "_execute_chatgpt_resume_subscription_auth", side_effect=_fake_execute),
            patch.object(api_actions, "_apply_chatgpt_resume_auth_result"),
            patch("api.tasks._save_task_log"),
        ):
            _run_phone_binding_test(
                task_id,
                [456],
                phone_items,
                {
                    "timeout_seconds": 180,
                    "poll_interval_seconds": 5,
                    "max_resend_attempts": 0,
                    "resend_interval_seconds": 0,
                    "account_interval_seconds": 60,
                    "proxy_mode": "direct",
                    "phone_binding_auth_retry_delays_seconds": [0, 0, 0],
                },
            )

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["success"], 1)
        self.assertEqual(snapshot["errors"], [])
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0]["has_phone_service"])
        self.assertEqual(calls[0]["phone"], "+13434832954")
        self.assertFalse(calls[1]["has_phone_service"])
        self.assertFalse(calls[1]["allow_phone_verification"])
        runtime_result = snapshot["meta"]["runtime_results"][0]
        self.assertEqual(runtime_result["status"], "bound")
        self.assertTrue(runtime_result["auth_capture_ok"])
        self.assertEqual(runtime_result["auth_retry_attempts"], 1)
        self.assertIn("Auth/RT 重试 1 次后已获取", runtime_result["reason"])
        self.assertTrue(any("立即重试 1/3" in line for line in snapshot["logs"]))
        self.assertTrue(any("手机绑定后 Auth/RT 重试成功" in line for line in snapshot["logs"]))

    def test_phone_completed_stays_bound_when_auth_capture_retries_still_fail(self):
        task_id = "task-phone-auth-retry-still-fails"
        _create_standalone_task_record(
            task_id,
            platform="chatgpt",
            source="phone_binding_test",
            total=1,
            meta={"missing_ids": [], "parse_errors": []},
        )
        phone_items = [
            {
                "line_no": 1,
                "phone": "+13434832921",
                "api_url": "https://example.com/api/phone2",
                "raw_line": "+13434832921----https://example.com/api/phone2",
            },
        ]

        class _FakeAccount:
            def __init__(self, account_id: int):
                self.id = account_id
                self.platform = "chatgpt"
                self.email = f"fail{account_id}@example.com"
                self.extra = {}

            def get_extra(self):
                return dict(self.extra)

            def set_extra(self, extra):
                self.extra = dict(extra or {})

        accounts_by_id = {}

        class _FakeSession:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def get(self, _model, account_id):
                account_id_value = int(account_id or 0)
                if account_id_value not in accounts_by_id:
                    accounts_by_id[account_id_value] = _FakeAccount(account_id_value)
                return accounts_by_id[account_id_value]

            def refresh(self, _account):
                return None

            def add(self, _account):
                return None

            def commit(self):
                return None

        calls = []

        def _fake_execute(account, allow_phone_verification=False, log_fn=None, shared_phone_service=None, stop_checker=None, retry_delays_seconds=None, proxy_url=None, phone_sms_probe_only=False):
            calls.append(shared_phone_service is not None)
            if shared_phone_service is not None:
                entry = shared_phone_service.current_entry
                shared_phone_service.mark_sms_sent(entry)
                shared_phone_service.last_code_time = "2026-06-09 09:50:58"
                shared_phone_service.complete(entry)
            return {
                "ok": False,
                "error": "organization/select 失败: HTTP 400, code=duplicate, type=invalid_request_error, message=Organization already has a default project.",
                "data": {"message": "Organization already has a default project."},
            }

        with (
            patch("api.tasks.Session", return_value=_FakeSession()),
            patch.object(api_actions, "_execute_chatgpt_resume_subscription_auth", side_effect=_fake_execute),
            patch.object(api_actions, "_apply_chatgpt_resume_auth_result"),
            patch("api.tasks._save_task_log"),
        ):
            _run_phone_binding_test(
                task_id,
                [789],
                phone_items,
                {
                    "timeout_seconds": 180,
                    "poll_interval_seconds": 5,
                    "max_resend_attempts": 0,
                    "resend_interval_seconds": 0,
                    "account_interval_seconds": 60,
                    "proxy_mode": "direct",
                    "phone_binding_auth_retry_delays_seconds": [0, 0, 0],
                },
            )

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(calls, [True, False, False, False])
        self.assertEqual(snapshot["success"], 1)
        self.assertEqual(len(snapshot["errors"]), 1)
        runtime_result = snapshot["meta"]["runtime_results"][0]
        self.assertEqual(runtime_result["status"], "bound")
        self.assertFalse(runtime_result["auth_capture_ok"])
        self.assertEqual(runtime_result["auth_retry_attempts"], 3)
        self.assertIn("Auth/RT 重试 3 次仍失败", runtime_result["reason"])
        self.assertTrue(any("手机号已绑定完成，但 Auth/RT 获取失败" in line for line in snapshot["logs"]))

    def test_pool_mode_fetches_next_available_phone_dynamically(self):
        task_id = "task-phone-pool-dynamic"
        _create_standalone_task_record(
            task_id,
            platform="chatgpt",
            source="phone_binding_test",
            total=1,
            meta={"missing_ids": [], "parse_errors": [], "phone_pool_dynamic": True},
        )

        class _Record:
            def __init__(self, record_id: int, phone: str, api_url: str):
                self.id = record_id
                self.phone_e164 = phone
                self.api_url = api_url
                self.available = True

        class _FakeRepo:
            def __init__(self):
                self.records = [
                    _Record(1, "+11111111111", "https://example.com/api/bad"),
                    _Record(2, "+12222222222", "https://example.com/api/good"),
                ]
                self.terminal = set()

            def list_available(self):
                return [record for record in self.records if record.phone_e164 not in self.terminal]

            def get(self, phone):
                for record in self.records:
                    if record.phone_e164 == phone and record.phone_e164 not in self.terminal:
                        return record
                return None

            def record_task_status(self, phone, task_status, *, reason="", email=""):
                if task_status != "bound":
                    self.terminal.add(phone)
                return self.get(phone)

        class _FakeAccount:
            def __init__(self, account_id: int):
                self.id = account_id
                self.platform = "chatgpt"
                self.email = f"dynamic{account_id}@example.com"
                self.extra = {}

            def get_extra(self):
                return dict(self.extra)

            def set_extra(self, extra):
                self.extra = dict(extra or {})

        accounts_by_id = {}

        class _FakeSession:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def get(self, _model, account_id):
                account_id_value = int(account_id or 0)
                if account_id_value not in accounts_by_id:
                    accounts_by_id[account_id_value] = _FakeAccount(account_id_value)
                return accounts_by_id[account_id_value]

            def refresh(self, _account):
                return None

            def add(self, _account):
                return None

            def commit(self):
                return None

        attempts = []

        def _fake_execute(account, allow_phone_verification=False, log_fn=None, shared_phone_service=None, stop_checker=None, retry_delays_seconds=None, proxy_url=None, phone_sms_probe_only=False):
            entry = shared_phone_service.current_entry
            attempts.append((int(account.id or 0), entry.phone))
            shared_phone_service.mark_sms_sent(entry)
            if entry.phone == "+11111111111":
                return {
                    "ok": False,
                    "error": "add_phone 阶段失败: add-phone/send 失败: This phone number is already linked to the maximum number of accounts.",
                    "data": {"message": "This phone number is already linked to the maximum number of accounts."},
                }
            shared_phone_service.last_code_time = "2026-06-04 00:00:00"
            shared_phone_service.complete(entry)
            return {"ok": True, "data": {"message": "done"}}

        with (
            patch("api.tasks.Session", return_value=_FakeSession()),
            patch("services.chatgpt_core.phone_pool_repository.PhonePoolRepository", _FakeRepo),
            patch.object(api_actions, "_execute_chatgpt_resume_subscription_auth", side_effect=_fake_execute),
            patch.object(api_actions, "_apply_chatgpt_resume_auth_result"),
            patch("api.tasks._save_task_log"),
            patch("api.tasks.time.monotonic", side_effect=[100.0, 200.0, 300.0, 400.0]),
        ):
            _run_phone_binding_test(
                task_id,
                [456],
                [],
                {
                    "timeout_seconds": 180,
                    "poll_interval_seconds": 5,
                    "max_resend_attempts": 0,
                    "resend_interval_seconds": 0,
                    "account_interval_seconds": 60,
                    "proxy_mode": "direct",
                    "use_pool": True,
                },
            )

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(attempts, [(456, "+11111111111"), (456, "+12222222222")])
        self.assertEqual(snapshot["success"], 1)
        statuses = [item["status"] for item in snapshot["meta"]["runtime_results"]]
        self.assertEqual(statuses, ["openai_phone_limit", "bound"])
        self.assertFalse(snapshot["meta"]["no_phone_available"])
        saved_binding = accounts_by_id[456].extra["chatgpt_phone_binding"]
        self.assertEqual(saved_binding["phone"], "+12222222222")

    def test_manual_sms_probe_only_does_not_persist_binding_or_retry_auth(self):
        task_id = "task-phone-manual-sms-probe-only"
        _create_standalone_task_record(
            task_id,
            platform="chatgpt",
            source="phone_binding_test",
            total=1,
            meta={
                "missing_ids": [],
                "parse_errors": [],
                "sms_probe_only": True,
                "prefix_sample": {"enabled": False, "prefix_count": 0, "sms_probe_only": True},
            },
        )
        phone_items = [
            {
                "line_no": 1,
                "phone": "+13434832954",
                "api_url": "https://example.com/api/probe",
                "raw_line": "+13434832954----https://example.com/api/probe",
                "prefix4": "1343",
            },
        ]

        class _FakeAccount:
            def __init__(self, account_id: int):
                self.id = account_id
                self.platform = "chatgpt"
                self.email = f"probe{account_id}@example.com"
                self.extra = {}

            def get_extra(self):
                return dict(self.extra)

            def set_extra(self, extra):
                self.extra = dict(extra or {})

        accounts_by_id = {}

        class _FakeSession:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def get(self, _model, account_id):
                account_id_value = int(account_id or 0)
                if account_id_value not in accounts_by_id:
                    accounts_by_id[account_id_value] = _FakeAccount(account_id_value)
                return accounts_by_id[account_id_value]

            def refresh(self, _account):
                return None

            def add(self, _account):
                return None

            def commit(self):
                return None

        calls = []

        def _fake_execute(account, allow_phone_verification=False, log_fn=None, shared_phone_service=None, stop_checker=None, retry_delays_seconds=None, proxy_url=None, phone_sms_probe_only=False):
            calls.append({"has_phone_service": shared_phone_service is not None, "probe_only": bool(phone_sms_probe_only)})
            entry = shared_phone_service.current_entry
            shared_phone_service.mark_sms_sent(entry)
            shared_phone_service.last_code = "123456"
            shared_phone_service.last_code_time = "2026-06-11 02:00:00"
            shared_phone_service.complete(entry)
            return {
                "ok": False,
                "error": "号段短信探测完成：OpenAI 已发码且收码 API 已收到验证码，未提交验证码",
                "data": {"message": "号段短信探测完成"},
            }

        with (
            patch("api.tasks.Session", return_value=_FakeSession()),
            patch.object(api_actions, "_execute_chatgpt_resume_subscription_auth", side_effect=_fake_execute),
            patch.object(api_actions, "_apply_chatgpt_resume_auth_result") as apply_auth,
            patch("api.tasks._save_task_log"),
        ):
            _run_phone_binding_test(
                task_id,
                [456],
                phone_items,
                {
                    "timeout_seconds": 180,
                    "poll_interval_seconds": 5,
                    "max_resend_attempts": 0,
                    "resend_interval_seconds": 0,
                    "account_interval_seconds": 60,
                    "proxy_mode": "direct",
                    "prefix_sample_enabled": False,
                    "prefix_sms_probe_only": True,
                },
            )

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["success"], 1)
        self.assertEqual(calls, [{"has_phone_service": True, "probe_only": True}])
        apply_auth.assert_not_called()
        runtime_result = snapshot["meta"]["runtime_results"][0]
        self.assertEqual(runtime_result["status"], "sms_probe_received")
        self.assertFalse(runtime_result["verification_submitted"])
        self.assertTrue(runtime_result["sms_probe_only"])
        self.assertNotIn("chatgpt_phone_binding", accounts_by_id[456].extra)
        self.assertTrue(any("已收码未提交" in line for line in snapshot["logs"]))
        self.assertTrue(any("短信探测模式已开启" in line for line in snapshot["logs"]))

    def test_concurrent_phone_binding_claims_distinct_manual_phones(self):
        task_id = "task-phone-concurrent-distinct-phones"
        _create_standalone_task_record(
            task_id,
            platform="chatgpt",
            source="phone_binding_test",
            total=2,
            meta={
                "missing_ids": [],
                "parse_errors": [],
                "requested_concurrency": 2,
                "effective_concurrency": 2,
            },
        )
        phone_items = [
            {
                "line_no": 1,
                "phone": "+15550000001",
                "api_url": "https://example.com/api/one",
                "raw_line": "+15550000001----https://example.com/api/one",
            },
            {
                "line_no": 2,
                "phone": "+15550000002",
                "api_url": "https://example.com/api/two",
                "raw_line": "+15550000002----https://example.com/api/two",
            },
        ]

        class _FakeAccount:
            def __init__(self, account_id: int):
                self.id = account_id
                self.platform = "chatgpt"
                self.email = f"concurrent{account_id}@example.com"
                self.extra = {}

            def get_extra(self):
                return dict(self.extra)

            def set_extra(self, extra):
                self.extra = dict(extra or {})

        accounts_by_id = {}

        class _FakeSession:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def get(self, _model, account_id):
                account_id_value = int(account_id or 0)
                if account_id_value not in accounts_by_id:
                    accounts_by_id[account_id_value] = _FakeAccount(account_id_value)
                return accounts_by_id[account_id_value]

            def refresh(self, _account):
                return None

            def add(self, _account):
                return None

            def commit(self):
                return None

            def rollback(self):
                return None

        attempts = []
        attempts_lock = threading.Lock()
        barrier = threading.Barrier(2, timeout=3)

        def _fake_execute(account, allow_phone_verification=False, log_fn=None, shared_phone_service=None, stop_checker=None, retry_delays_seconds=None, proxy_url=None, phone_sms_probe_only=False):
            entry = shared_phone_service.current_entry
            with attempts_lock:
                attempts.append((int(account.id or 0), entry.phone))
            barrier.wait(timeout=3)
            shared_phone_service.mark_sms_sent(entry)
            shared_phone_service.last_code_time = "2026-07-09 00:00:00"
            shared_phone_service.complete(entry)
            return {"ok": True, "data": {"message": "done"}}

        with (
            patch("api.tasks.Session", return_value=_FakeSession()),
            patch.object(api_actions, "_execute_chatgpt_resume_subscription_auth", side_effect=_fake_execute),
            patch.object(api_actions, "_apply_chatgpt_resume_auth_result"),
            patch("api.tasks._save_task_log"),
        ):
            _run_phone_binding_test(
                task_id,
                [456, 457],
                phone_items,
                {
                    "timeout_seconds": 180,
                    "poll_interval_seconds": 5,
                    "max_resend_attempts": 0,
                    "resend_interval_seconds": 0,
                    "account_interval_seconds": 1,
                    "proxy_mode": "direct",
                    "requested_concurrency": 2,
                    "concurrency": 2,
                },
            )

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["success"], 2)
        self.assertEqual({phone for _account_id, phone in attempts}, {"+15550000001", "+15550000002"})
        self.assertEqual(len(snapshot["meta"]["runtime_results"]), 2)
        self.assertTrue(any("并发已开启" in line for line in snapshot["logs"]))


if __name__ == "__main__":
    unittest.main()
