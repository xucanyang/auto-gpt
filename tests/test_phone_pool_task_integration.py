import unittest
from unittest.mock import patch

from fastapi import HTTPException

from api.tasks import (
    PhoneBindingTestTaskRequest,
    _create_standalone_task_record,
    _phone_binding_prefix4,
    _build_phone_prefix_sample_summary,
    _run_phone_binding_test,
    _task_store,
    enqueue_phone_binding_test_task,
)
from services.chatgpt_core.phone_pool_repository import _phone_prefix4


class PhonePoolTaskIntegrationTests(unittest.TestCase):
    def test_phone_pool_prefix4_uses_local_number_digits(self):
        self.assertEqual(_phone_prefix4("+12532241242"), "1253")
        self.assertEqual(_phone_prefix4("+12509870220"), "1250")
        self.assertEqual(_phone_prefix4("+27618622884"), "2761")
        self.assertEqual(_phone_binding_prefix4("+12532241242"), "1253")

    def test_phone_binding_test_uses_pool_when_phone_lines_empty(self):
        created_meta = {}

        class _BackgroundTasks:
            def __init__(self):
                self.calls = []

            def add_task(self, *args, **kwargs):
                self.calls.append((args, kwargs))

        class _Record:
            id = 9
            phone_e164 = "+15551230001"
            api_url = "https://relay.example.com/a"
            remaining_capacity = 3

        class _FakeRepo:
            def list_available(self):
                return [_Record()]

        def _fake_create_task(_task_id, *, platform, source, total, meta):
            created_meta.update(meta)

        req = PhoneBindingTestTaskRequest(account_ids=[123], phone_lines="", use_pool=True)
        background_tasks = _BackgroundTasks()

        with (
            patch(
                "api.tasks._resolve_phone_binding_test_accounts",
                return_value=(
                    [{"account_id": 123, "email": "pool@example.com", "status": "pending_payment"}],
                    [],
                    [],
                    [],
                ),
            ),
            patch("services.chatgpt_core.phone_pool_repository.PhonePoolRepository", _FakeRepo),
            patch("api.tasks._create_standalone_task_record", side_effect=_fake_create_task),
            patch("api.tasks._save_task_log"),
        ):
            result = enqueue_phone_binding_test_task(req, background_tasks=background_tasks)

        self.assertTrue(result["task_id"])
        self.assertEqual(result["phone_count"], 1)
        self.assertTrue(created_meta["settings"]["use_pool"])
        self.assertTrue(created_meta["phone_pool_dynamic"])
        self.assertEqual(created_meta["phone_items"], [])
        self.assertEqual(background_tasks.calls[0][0][3], [])

    def test_manual_phone_lines_import_new_phone_to_pool_but_do_not_use_dynamic_pool(self):
        created_meta = {}

        class _BackgroundTasks:
            def __init__(self):
                self.calls = []

            def add_task(self, *args, **kwargs):
                self.calls.append((args, kwargs))

        class _Record:
            id = 77
            phone_e164 = "+15551230002"
            api_url = "https://relay.example.com/manual"
            remaining_capacity = 3

        class _FakeRepo:
            def __init__(self):
                self.add_calls = []

            def get(self, phone):
                return None

            def add(self, **kwargs):
                self.add_calls.append(kwargs)
                return _Record()

        def _fake_create_task(_task_id, *, platform, source, total, meta):
            created_meta.update(meta)

        req = PhoneBindingTestTaskRequest(
            account_ids=[123],
            phone_lines="+15551230002----https://relay.example.com/manual",
            use_pool=True,
            max_resend_attempts=2,
            resend_interval_seconds=45,
        )
        background_tasks = _BackgroundTasks()

        with (
            patch(
                "api.tasks._resolve_phone_binding_test_accounts",
                return_value=(
                    [{"account_id": 123, "email": "manual@example.com", "status": "pending_payment"}],
                    [],
                    [],
                    [],
                ),
            ),
            patch("services.chatgpt_core.phone_pool_repository.PhonePoolRepository", _FakeRepo),
            patch("api.tasks._create_standalone_task_record", side_effect=_fake_create_task),
            patch("api.tasks._save_task_log"),
        ):
            result = enqueue_phone_binding_test_task(req, background_tasks=background_tasks)

        self.assertTrue(result["task_id"])
        self.assertEqual(result["phone_count"], 1)
        self.assertFalse(created_meta["settings"]["use_pool"])
        self.assertEqual(created_meta["phone_pool_import"], {"imported": 1, "existing": 0, "skipped": 0})
        self.assertEqual(result["phone_pool_import"], {"imported": 1, "existing": 0, "skipped": 0})
        self.assertEqual(created_meta["settings"]["max_resend_attempts"], 2)
        self.assertEqual(created_meta["settings"]["resend_interval_seconds"], 45)
        self.assertTrue(created_meta["phone_items"][0]["pool_managed"])
        self.assertEqual(created_meta["phone_items"][0]["pool_id"], 77)
        self.assertEqual(created_meta["phone_items"][0]["phone"], "+15551230002")
        self.assertEqual(background_tasks.calls[0][0][3][0]["phone"], "+15551230002")
        self.assertTrue(background_tasks.calls[0][0][3][0]["pool_managed"])
        self.assertEqual(background_tasks.calls[0][0][4]["max_resend_attempts"], 2)

    def test_manual_phone_lines_can_enable_sms_probe_only_without_prefix_sample(self):
        created_meta = {}

        class _BackgroundTasks:
            def __init__(self):
                self.calls = []

            def add_task(self, *args, **kwargs):
                self.calls.append((args, kwargs))

        class _Record:
            id = 78
            phone_e164 = "+15551230012"
            api_url = "https://relay.example.com/manual-probe"
            remaining_capacity = 3

        class _FakeRepo:
            def get(self, phone):
                return None

            def add(self, **kwargs):
                return _Record()

        def _fake_create_task(_task_id, *, platform, source, total, meta):
            created_meta.update(meta)

        req = PhoneBindingTestTaskRequest(
            account_ids=[123],
            phone_lines="+15551230012----https://relay.example.com/manual-probe",
            use_pool=True,
            prefix_sample_enabled=False,
            prefix_sms_probe_only=True,
            reuse_phone_until_unusable=True,
        )
        background_tasks = _BackgroundTasks()

        with (
            patch(
                "api.tasks._resolve_phone_binding_test_accounts",
                return_value=(
                    [{"account_id": 123, "email": "manual-probe@example.com", "status": "pending_payment"}],
                    [],
                    [],
                    [],
                ),
            ),
            patch("services.chatgpt_core.phone_pool_repository.PhonePoolRepository", _FakeRepo),
            patch("api.tasks._create_standalone_task_record", side_effect=_fake_create_task),
            patch("api.tasks._save_task_log"),
        ):
            result = enqueue_phone_binding_test_task(req, background_tasks=background_tasks)

        self.assertTrue(result["task_id"])
        self.assertTrue(result["sms_probe_only"])
        self.assertFalse(result["prefix_sample"]["enabled"])
        self.assertTrue(result["prefix_sample"]["sms_probe_only"])
        self.assertTrue(created_meta["settings"]["prefix_sms_probe_only"])
        self.assertFalse(created_meta["settings"]["prefix_sample_enabled"])
        self.assertFalse(created_meta["settings"]["reuse_phone_until_unusable"])
        self.assertTrue(created_meta["sms_probe_only"])
        queued_settings = background_tasks.calls[0][0][4]
        self.assertTrue(queued_settings["prefix_sms_probe_only"])
        self.assertFalse(queued_settings["reuse_phone_until_unusable"])

    def test_manual_existing_phone_is_pool_managed_and_upserts_api_url(self):
        created_meta = {}
        update_calls = []

        class _BackgroundTasks:
            def __init__(self):
                self.calls = []

            def add_task(self, *args, **kwargs):
                self.calls.append((args, kwargs))

        class _Record:
            id = 88
            phone_e164 = "+15551230003"
            api_url = "https://relay.example.com/existing"
            remaining_capacity = 2

        class _FakeRepo:
            def get(self, phone):
                return _Record() if phone == "+15551230003" else None

            def update(self, record_id, **kwargs):
                update_calls.append((record_id, kwargs))
                return _Record()

            def add(self, **_kwargs):
                raise AssertionError("existing manual phone should be updated via repository update, not inserted")

        def _fake_create_task(_task_id, *, platform, source, total, meta):
            created_meta.update(meta)

        req = PhoneBindingTestTaskRequest(
            account_ids=[123],
            phone_lines="+15551230003----https://relay.example.com/pasted-new-api",
            use_pool=False,
        )
        background_tasks = _BackgroundTasks()

        with (
            patch(
                "api.tasks._resolve_phone_binding_test_accounts",
                return_value=(
                    [{"account_id": 123, "email": "manual-existing@example.com", "status": "pending_payment"}],
                    [],
                    [],
                    [],
                ),
            ),
            patch("services.chatgpt_core.phone_pool_repository.PhonePoolRepository", _FakeRepo),
            patch("api.tasks._create_standalone_task_record", side_effect=_fake_create_task),
            patch("api.tasks._save_task_log"),
        ):
            result = enqueue_phone_binding_test_task(req, background_tasks=background_tasks)

        self.assertTrue(result["task_id"])
        self.assertEqual(created_meta["phone_pool_import"], {"imported": 0, "existing": 1, "skipped": 0})
        self.assertEqual(update_calls, [(88, {"api_url": "https://relay.example.com/pasted-new-api"})])
        self.assertTrue(created_meta["phone_items"][0]["pool_managed"])
        self.assertEqual(created_meta["phone_items"][0]["pool_id"], 88)
        self.assertEqual(created_meta["phone_items"][0]["api_url"], "https://relay.example.com/pasted-new-api")

    def test_pool_empty_error_explains_manual_fallback(self):
        class _FakeRepo:
            def list_available(self):
                return []

        req = PhoneBindingTestTaskRequest(account_ids=[123], phone_lines="", use_pool=True)

        with (
            patch(
                "api.tasks._resolve_phone_binding_test_accounts",
                return_value=(
                    [{"account_id": 123, "email": "pool@example.com", "status": "pending_payment"}],
                    [],
                    [],
                    [],
                ),
            ),
            patch("services.chatgpt_core.phone_pool_repository.PhonePoolRepository", _FakeRepo),
        ):
            with self.assertRaises(HTTPException) as ctx:
                enqueue_phone_binding_test_task(req)

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("手动粘贴", str(ctx.exception.detail))

    def test_pool_mode_keeps_phone_items_dynamic(self):
        created_meta = {}

        class _BackgroundTasks:
            def __init__(self):
                self.calls = []

            def add_task(self, *args, **kwargs):
                self.calls.append((args, kwargs))

        class _Record:
            def __init__(self, record_id):
                self.id = record_id
                self.phone_e164 = f"+1555123000{record_id}"
                self.api_url = f"https://relay.example.com/{record_id}"
                self.remaining_capacity = 3

        class _FakeRepo:
            def list_available(self):
                return [_Record(1), _Record(2), _Record(3)]

        def _fake_create_task(_task_id, *, platform, source, total, meta):
            created_meta.update(meta)

        req = PhoneBindingTestTaskRequest(account_ids=[123], phone_lines="", use_pool=True)
        background_tasks = _BackgroundTasks()

        with (
            patch(
                "api.tasks._resolve_phone_binding_test_accounts",
                return_value=(
                    [{"account_id": 123, "email": "pool@example.com", "status": "pending_payment"}],
                    [],
                    [],
                    [],
                ),
            ),
            patch("services.chatgpt_core.phone_pool_repository.PhonePoolRepository", _FakeRepo),
            patch("api.tasks._create_standalone_task_record", side_effect=_fake_create_task),
            patch("api.tasks._save_task_log"),
        ):
            result = enqueue_phone_binding_test_task(req, background_tasks=background_tasks)

        self.assertEqual(result["phone_count"], 3)
        self.assertEqual(created_meta["phone_items"], [])
        self.assertEqual(background_tasks.calls[0][0][3], [])

    def test_pool_mode_can_enable_sms_probe_only_without_prefix_sample(self):
        created_meta = {}

        class _BackgroundTasks:
            def __init__(self):
                self.calls = []

            def add_task(self, *args, **kwargs):
                self.calls.append((args, kwargs))

        class _Record:
            def __init__(self, record_id):
                self.id = record_id
                self.phone_e164 = f"+1555123010{record_id}"
                self.api_url = f"https://relay.example.com/probe/{record_id}"
                self.remaining_capacity = 3

        class _FakeRepo:
            def list_available(self):
                return [_Record(1), _Record(2)]

        def _fake_create_task(_task_id, *, platform, source, total, meta):
            created_meta.update(meta)

        req = PhoneBindingTestTaskRequest(
            account_ids=[123],
            phone_lines="",
            use_pool=True,
            prefix_sample_enabled=False,
            prefix_sms_probe_only=True,
            reuse_phone_until_unusable=True,
        )
        background_tasks = _BackgroundTasks()

        with (
            patch(
                "api.tasks._resolve_phone_binding_test_accounts",
                return_value=(
                    [{"account_id": 123, "email": "pool-probe@example.com", "status": "pending_payment"}],
                    [],
                    [],
                    [],
                ),
            ),
            patch("services.chatgpt_core.phone_pool_repository.PhonePoolRepository", _FakeRepo),
            patch("api.tasks._create_standalone_task_record", side_effect=_fake_create_task),
            patch("api.tasks._save_task_log"),
        ):
            result = enqueue_phone_binding_test_task(req, background_tasks=background_tasks)

        self.assertEqual(result["phone_count"], 2)
        self.assertTrue(result["sms_probe_only"])
        self.assertFalse(result["prefix_sample"]["enabled"])
        self.assertTrue(created_meta["settings"]["use_pool"])
        self.assertTrue(created_meta["settings"]["prefix_sms_probe_only"])
        self.assertFalse(created_meta["settings"]["reuse_phone_until_unusable"])
        self.assertEqual(created_meta["phone_items"], [])
        queued_settings = background_tasks.calls[0][0][4]
        self.assertTrue(queued_settings["use_pool"])
        self.assertTrue(queued_settings["prefix_sms_probe_only"])
        self.assertFalse(queued_settings["reuse_phone_until_unusable"])

    def test_prefix_sample_mode_uses_fixed_pool_items_and_disables_phone_reuse(self):
        created_meta = {}

        class _BackgroundTasks:
            def __init__(self):
                self.calls = []

            def add_task(self, *args, **kwargs):
                self.calls.append((args, kwargs))

        class _Record:
            def __init__(self, record_id, phone):
                self.id = record_id
                self.phone_e164 = phone
                self.api_url = f"https://relay.example.com/{record_id}"
                self.remaining_capacity = 3

        sampled_records = [
            _Record(1, "+12260000001"),
            _Record(2, "+13430000001"),
            _Record(3, "+12260000002"),
            _Record(4, "+13430000002"),
        ]

        class _FakeRepo:
            def sample_testable_by_prefix(self, sample_size):
                self.sample_size = sample_size
                return sampled_records

            def restore_prefix_sample_records(self, record_ids):
                self.record_ids = record_ids
                return sampled_records

            def to_phone_items(self, records):
                return [
                    {
                        "id": record.id,
                        "pool_id": record.id,
                        "line_no": index,
                        "phone": record.phone_e164,
                        "api_url": record.api_url,
                        "raw_line": f"{record.phone_e164}----{record.api_url}",
                        "pool_managed": True,
                        "prefix4": record.phone_e164.lstrip("+")[:4],
                    }
                    for index, record in enumerate(records, start=1)
                ]

        def _fake_create_task(_task_id, *, platform, source, total, meta):
            created_meta.update(meta)

        req = PhoneBindingTestTaskRequest(
            account_ids=[123, 124],
            phone_lines="",
            use_pool=True,
            reuse_phone_until_unusable=True,
            prefix_sample_enabled=True,
            prefix_sample_size=2,
        )
        background_tasks = _BackgroundTasks()

        with (
            patch(
                "api.tasks._resolve_phone_binding_test_accounts",
                return_value=(
                    [
                        {"account_id": 123, "email": "one@example.com", "status": "pending_payment"},
                        {"account_id": 124, "email": "two@example.com", "status": "pending_payment"},
                    ],
                    [],
                    [],
                    [],
                ),
            ),
            patch("services.chatgpt_core.phone_pool_repository.PhonePoolRepository", _FakeRepo),
            patch("api.tasks._create_standalone_task_record", side_effect=_fake_create_task),
            patch("api.tasks._save_task_log"),
        ):
            result = enqueue_phone_binding_test_task(req, background_tasks=background_tasks)

        self.assertEqual(result["phone_count"], 4)
        self.assertEqual(result["prefix_sample"]["prefix_count"], 2)
        self.assertFalse(created_meta["phone_pool_dynamic"])
        self.assertTrue(created_meta["phone_pool_source"])
        self.assertTrue(created_meta["settings"]["prefix_sample_enabled"])
        self.assertEqual(created_meta["settings"]["prefix_sample_size"], 2)
        self.assertFalse(created_meta["settings"]["use_pool"])
        self.assertFalse(created_meta["settings"]["reuse_phone_until_unusable"])
        self.assertEqual(len(created_meta["phone_items"]), 4)
        self.assertTrue(all(item["prefix_sample"] for item in created_meta["phone_items"]))
        self.assertEqual(len(background_tasks.calls[0][0][3]), 4)

    def test_prefix_sample_rejected_filter_uses_rejected_sampler(self):
        created_meta = {}
        calls = {"all": 0, "rejected": 0}

        class _BackgroundTasks:
            def __init__(self):
                self.calls = []

            def add_task(self, *args, **kwargs):
                self.calls.append((args, kwargs))

        class _Record:
            def __init__(self, record_id, phone):
                self.id = record_id
                self.phone_e164 = phone
                self.api_url = f"https://relay.example.com/{record_id}"
                self.remaining_capacity = 3

        sampled_records = [
            _Record(9, "+13430000009"),
        ]

        class _FakeRepo:
            def sample_testable_by_prefix(self, sample_size):
                calls["all"] += 1
                return []

            def sample_rejected_by_prefix(self, sample_size):
                calls["rejected"] += 1
                self.sample_size = sample_size
                return sampled_records

            def restore_prefix_sample_records(self, record_ids):
                self.record_ids = record_ids
                return sampled_records

            def to_phone_items(self, records):
                return [
                    {
                        "id": record.id,
                        "pool_id": record.id,
                        "line_no": index,
                        "phone": record.phone_e164,
                        "api_url": record.api_url,
                        "raw_line": f"{record.phone_e164}----{record.api_url}",
                        "pool_managed": True,
                        "prefix4": record.phone_e164.lstrip("+")[:4],
                    }
                    for index, record in enumerate(records, start=1)
                ]

        def _fake_create_task(_task_id, *, platform, source, total, meta):
            created_meta.update(meta)

        req = PhoneBindingTestTaskRequest(
            account_ids=[123],
            phone_lines="",
            use_pool=True,
            prefix_sample_enabled=True,
            prefix_sample_size=1,
            prefix_sample_filter="rejected",
        )
        background_tasks = _BackgroundTasks()

        with (
            patch(
                "api.tasks._resolve_phone_binding_test_accounts",
                return_value=(
                    [{"account_id": 123, "email": "one@example.com", "status": "pending_payment"}],
                    [],
                    [],
                    [],
                ),
            ),
            patch("services.chatgpt_core.phone_pool_repository.PhonePoolRepository", _FakeRepo),
            patch("api.tasks._create_standalone_task_record", side_effect=_fake_create_task),
            patch("api.tasks._save_task_log"),
        ):
            result = enqueue_phone_binding_test_task(req, background_tasks=background_tasks)

        self.assertEqual(calls, {"all": 0, "rejected": 1})
        self.assertEqual(result["prefix_sample"]["filter"], "rejected")
        self.assertEqual(created_meta["settings"]["prefix_sample_filter"], "rejected")
        self.assertEqual(created_meta["prefix_sample"]["prefix_count"], 1)
        self.assertEqual(created_meta["phone_items"][0]["phone"], "+13430000009")

    def test_prefix_sample_available_filter_uses_available_sampler(self):
        created_meta = {}
        calls = {"all": 0, "available": 0, "rejected": 0}

        class _BackgroundTasks:
            def __init__(self):
                self.calls = []

            def add_task(self, *args, **kwargs):
                self.calls.append((args, kwargs))

        class _Record:
            def __init__(self, record_id, phone):
                self.id = record_id
                self.phone_e164 = phone
                self.api_url = f"https://relay.example.com/{record_id}"
                self.remaining_capacity = 3

        sampled_records = [
            _Record(11, "+12260000011"),
            _Record(12, "+12260000012"),
        ]

        class _FakeRepo:
            def sample_testable_by_prefix(self, sample_size):
                calls["all"] += 1
                return []

            def sample_available_by_prefix(self, sample_size):
                calls["available"] += 1
                self.sample_size = sample_size
                return sampled_records

            def sample_rejected_by_prefix(self, sample_size):
                calls["rejected"] += 1
                return []

            def restore_prefix_sample_records(self, record_ids):
                self.record_ids = record_ids
                return sampled_records

            def to_phone_items(self, records):
                return [
                    {
                        "id": record.id,
                        "pool_id": record.id,
                        "line_no": index,
                        "phone": record.phone_e164,
                        "api_url": record.api_url,
                        "raw_line": f"{record.phone_e164}----{record.api_url}",
                        "pool_managed": True,
                        "prefix4": record.phone_e164.lstrip("+")[:4],
                    }
                    for index, record in enumerate(records, start=1)
                ]

        def _fake_create_task(_task_id, *, platform, source, total, meta):
            created_meta.update(meta)

        req = PhoneBindingTestTaskRequest(
            account_ids=[123],
            phone_lines="",
            use_pool=True,
            prefix_sample_enabled=True,
            prefix_sample_size=2,
            prefix_sample_filter="available",
        )
        background_tasks = _BackgroundTasks()

        with (
            patch(
                "api.tasks._resolve_phone_binding_test_accounts",
                return_value=(
                    [{"account_id": 123, "email": "one@example.com", "status": "pending_payment"}],
                    [],
                    [],
                    [],
                ),
            ),
            patch("services.chatgpt_core.phone_pool_repository.PhonePoolRepository", _FakeRepo),
            patch("api.tasks._create_standalone_task_record", side_effect=_fake_create_task),
            patch("api.tasks._save_task_log"),
        ):
            result = enqueue_phone_binding_test_task(req, background_tasks=background_tasks)

        self.assertEqual(calls, {"all": 0, "available": 1, "rejected": 0})
        self.assertEqual(result["prefix_sample"]["filter"], "available")
        self.assertEqual(created_meta["settings"]["prefix_sample_filter"], "available")
        self.assertEqual(created_meta["prefix_sample"]["prefix_count"], 1)
        self.assertEqual(created_meta["phone_items"][0]["phone"], "+12260000011")

    def test_prefix_sample_selected_prefixes_override_filter(self):
        created_meta = {}
        calls = {"all": 0, "available": 0, "rejected": 0, "selected": 0}

        class _BackgroundTasks:
            def __init__(self):
                self.calls = []

            def add_task(self, *args, **kwargs):
                self.calls.append((args, kwargs))

        class _Record:
            def __init__(self, record_id, phone):
                self.id = record_id
                self.phone_e164 = phone
                self.api_url = f"https://relay.example.com/{record_id}"
                self.remaining_capacity = 3

        sampled_records = [
            _Record(21, "+13430000021"),
            _Record(22, "+14160000022"),
        ]

        class _FakeRepo:
            def sample_testable_by_prefix(self, sample_size):
                calls["all"] += 1
                return []

            def sample_available_by_prefix(self, sample_size):
                calls["available"] += 1
                return []

            def sample_rejected_by_prefix(self, sample_size):
                calls["rejected"] += 1
                return []

            def sample_selected_prefixes(self, prefixes, sample_size):
                calls["selected"] += 1
                self.prefixes = prefixes
                self.sample_size = sample_size
                return sampled_records

            def restore_prefix_sample_records(self, record_ids):
                self.record_ids = record_ids
                return sampled_records

            def to_phone_items(self, records):
                return [
                    {
                        "id": record.id,
                        "pool_id": record.id,
                        "line_no": index,
                        "phone": record.phone_e164,
                        "api_url": record.api_url,
                        "raw_line": f"{record.phone_e164}----{record.api_url}",
                        "pool_managed": True,
                        "prefix4": record.phone_e164.lstrip("+")[:4],
                    }
                    for index, record in enumerate(records, start=1)
                ]

        def _fake_create_task(_task_id, *, platform, source, total, meta):
            created_meta.update(meta)

        req = PhoneBindingTestTaskRequest(
            account_ids=[123],
            phone_lines="",
            use_pool=True,
            prefix_sample_enabled=True,
            prefix_sample_size=2,
            prefix_sample_filter="available",
            selected_prefixes=["1343", "1416", "bad", "1343"],
            prefix_sms_probe_only=True,
        )
        background_tasks = _BackgroundTasks()

        with (
            patch(
                "api.tasks._resolve_phone_binding_test_accounts",
                return_value=(
                    [{"account_id": 123, "email": "one@example.com", "status": "pending_payment"}],
                    [],
                    [],
                    [],
                ),
            ),
            patch("services.chatgpt_core.phone_pool_repository.PhonePoolRepository", _FakeRepo),
            patch("api.tasks._create_standalone_task_record", side_effect=_fake_create_task),
            patch("api.tasks._save_task_log"),
        ):
            result = enqueue_phone_binding_test_task(req, background_tasks=background_tasks)

        self.assertEqual(calls, {"all": 0, "available": 0, "rejected": 0, "selected": 1})
        self.assertEqual(result["prefix_sample"]["mode"], "selected")
        self.assertEqual(result["prefix_sample"]["requested_prefixes"], ["1343", "1416"])
        self.assertTrue(result["prefix_sample"]["sms_probe_only"])
        self.assertEqual(created_meta["settings"]["selected_prefixes"], ["1343", "1416"])
        self.assertTrue(created_meta["settings"]["prefix_sms_probe_only"])
        self.assertEqual(created_meta["prefix_sample"]["filter"], "available")
        self.assertEqual(created_meta["prefix_sample"]["prefix_count"], 2)

    def test_prefix_limited_binding_uses_selected_prefix_capacity_not_sampling(self):
        created_meta = {}
        calls = {"list": 0, "selected_sample": 0}

        class _BackgroundTasks:
            def __init__(self):
                self.calls = []

            def add_task(self, *args, **kwargs):
                self.calls.append((args, kwargs))

        class _Record:
            def __init__(self, record_id, phone, remaining_capacity):
                self.id = record_id
                self.phone_e164 = phone
                self.api_url = f"https://relay.example.com/{record_id}"
                self.remaining_capacity = remaining_capacity

        class _FakeRepo:
            def list_available_by_prefixes(self, prefixes):
                calls["list"] += 1
                self.prefixes = prefixes
                return [
                    _Record(31, "+13430000031", 3),
                    _Record(32, "+13430000032", 2),
                ]

            def sample_selected_prefixes(self, prefixes, sample_size):
                calls["selected_sample"] += 1
                return []

        def _fake_create_task(_task_id, *, platform, source, total, meta):
            created_meta.update(meta)

        req = PhoneBindingTestTaskRequest(
            account_ids=[123, 124, 125, 126],
            phone_lines="",
            use_pool=True,
            prefix_bind_enabled=True,
            prefix_sample_enabled=True,
            prefix_sample_size=2,
            selected_prefixes=["1343"],
            reuse_phone_until_unusable=True,
        )
        background_tasks = _BackgroundTasks()

        with (
            patch(
                "api.tasks._resolve_phone_binding_test_accounts",
                return_value=(
                    [
                        {"account_id": 123, "email": "one@example.com", "status": "pending_payment"},
                        {"account_id": 124, "email": "two@example.com", "status": "pending_payment"},
                        {"account_id": 125, "email": "three@example.com", "status": "pending_payment"},
                        {"account_id": 126, "email": "four@example.com", "status": "pending_payment"},
                    ],
                    [],
                    [],
                    [],
                ),
            ),
            patch("services.chatgpt_core.phone_pool_repository.PhonePoolRepository", _FakeRepo),
            patch("api.tasks._create_standalone_task_record", side_effect=_fake_create_task),
            patch("api.tasks._save_task_log"),
        ):
            result = enqueue_phone_binding_test_task(req, background_tasks=background_tasks)

        self.assertEqual(calls, {"list": 1, "selected_sample": 0})
        self.assertTrue(result["prefix_bind"]["enabled"])
        self.assertFalse(result["prefix_sample"]["enabled"])
        self.assertEqual(result["prefix_bind"]["selected_prefixes"], ["1343"])
        self.assertEqual(result["prefix_bind"]["available_phone_count"], 2)
        self.assertEqual(result["prefix_bind"]["available_slot_count"], 5)
        self.assertTrue(created_meta["settings"]["prefix_bind_enabled"])
        self.assertFalse(created_meta["settings"]["prefix_sample_enabled"])
        self.assertTrue(created_meta["settings"]["reuse_phone_until_unusable"])
        self.assertTrue(created_meta["phone_pool_dynamic"])
        self.assertEqual(created_meta["phone_count"], 2)

    def test_prefix_limited_binding_rejects_shortage(self):
        class _Record:
            id = 31
            phone_e164 = "+13430000031"
            api_url = "https://relay.example.com/31"
            remaining_capacity = 1

        class _FakeRepo:
            def list_available_by_prefixes(self, prefixes):
                return [_Record()]

        req = PhoneBindingTestTaskRequest(
            account_ids=[123, 124],
            phone_lines="",
            use_pool=True,
            prefix_bind_enabled=True,
            selected_prefixes=["1343"],
            reuse_phone_until_unusable=False,
        )

        with (
            patch(
                "api.tasks._resolve_phone_binding_test_accounts",
                return_value=(
                    [
                        {"account_id": 123, "email": "one@example.com", "status": "pending_payment"},
                        {"account_id": 124, "email": "two@example.com", "status": "pending_payment"},
                    ],
                    [],
                    [],
                    [],
                ),
            ),
            patch("services.chatgpt_core.phone_pool_repository.PhonePoolRepository", _FakeRepo),
        ):
            with self.assertRaises(HTTPException) as ctx:
                enqueue_phone_binding_test_task(req)

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("指定号段容量不足", str(ctx.exception.detail))

    def test_prefix_sample_runner_accepts_filter_setting(self):
        task_id = "task-prefix-sample-filter-runner"
        phone_items = [
            {
                "line_no": 1,
                "phone": "+13430000009",
                "api_url": "https://relay.example.com/9",
                "raw_line": "+13430000009----https://relay.example.com/9",
                "prefix4": "1343",
                "prefix_sample": True,
            }
        ]
        _create_standalone_task_record(
            task_id,
            platform="chatgpt",
            source="phone_binding_test",
            total=1,
            meta={
                "missing_ids": [],
                "parse_errors": [],
                "prefix_sample": {"enabled": True, "prefix_count": 1},
                "phone_items": phone_items,
            },
        )

        with patch("api.tasks._save_task_log"):
            _run_phone_binding_test(
                task_id,
                [],
                phone_items,
                {
                    "timeout_seconds": 180,
                    "poll_interval_seconds": 5,
                    "max_resend_attempts": 0,
                    "resend_interval_seconds": 0,
                    "account_interval_seconds": 5,
                    "prefix_sample_enabled": True,
                    "prefix_sample_size": 1,
                    "prefix_sample_filter": "rejected",
                },
            )

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["status"], "done")
        self.assertTrue(any("仅复测 OpenAI 拒绝号段" in line for line in snapshot["logs"]))


    def test_prefix_sample_summary_treats_phone_signup_success_as_available(self):
        summary = _build_phone_prefix_sample_summary(
            [{"phone": "+13430000001", "prefix4": "1343"}],
            [{"phone": "+13430000001", "status": "registered_phone_signup"}],
            phone_signup=True,
        )

        self.assertEqual(summary["available_prefix_count"], 1)
        self.assertEqual(summary["unavailable_prefix_count"], 0)
        self.assertEqual(summary["items"][0]["status"], "available")

    def test_phone_signup_prefix_summary_only_treats_fraud_guard_as_unavailable(self):
        phone_items = [
            {"phone": "+13430000001", "prefix4": "1343"},
            {"phone": "+14370000001", "prefix4": "1437"},
            {"phone": "+15810000001", "prefix4": "1581"},
        ]
        runtime_results = [
            {
                "phone": "+13430000001",
                "status": "already_registered",
                "reason": "Phone number already in use. Please try again. code=phone_number_in_use",
            },
            {
                "phone": "+14370000001",
                "status": "openai_rejected",
                "reason": "We've detected suspicious behavior from phone numbers similar to yours. code=fraud_guard",
            },
            {
                "phone": "+15810000001",
                "status": "api_no_code",
                "reason": "未收到短信验证码",
            },
        ]

        summary = _build_phone_prefix_sample_summary(phone_items, runtime_results, phone_signup=True)

        self.assertEqual(summary["available_prefix_count"], 0)
        self.assertEqual(summary["unavailable_prefix_count"], 1)
        self.assertEqual(summary["untested_prefix_count"], 2)
        self.assertEqual(summary["unavailable_prefixes"], ["1437"])

    def test_prefix_sample_summary_distinguishes_all_four_prefix_states(self):
        phone_items = [
            {"phone": "+12260000001", "prefix4": "1226"},
            {"phone": "+12260000002", "prefix4": "1226"},
            {"phone": "+13430000001", "prefix4": "1343"},
            {"phone": "+13430000002", "prefix4": "1343"},
            {"phone": "+14160000001", "prefix4": "1416"},
            {"phone": "+16720000001", "prefix4": "1672"},
        ]
        runtime_results = [
            {"phone": "+12260000001", "status": "bound"},
            {"phone": "+12260000002", "status": "bound"},
            {"phone": "+13430000001", "status": "openai_rejected"},
            {"phone": "+13430000002", "status": "api_no_code"},
            {"phone": "+14160000001", "status": "bound"},
            {"phone": "+16720000001", "status": "account_auth_error"},
        ]

        summary = _build_phone_prefix_sample_summary(phone_items, runtime_results)

        self.assertEqual(summary["available_prefix_count"], 2)
        self.assertEqual(summary["unavailable_prefix_count"], 1)
        self.assertEqual(summary["partial_prefix_count"], 0)
        self.assertEqual(summary["untested_prefix_count"], 1)
        self.assertEqual(summary["unavailable_prefixes"], ["1343"])
        self.assertEqual(summary["tested_phone_count"], 5)

        partial = _build_phone_prefix_sample_summary(
            phone_items,
            runtime_results + [{"phone": "+13430000002", "status": "bound"}],
        )
        self.assertEqual(partial["unavailable_prefix_count"], 0)
        self.assertEqual(partial["partial_prefix_count"], 1)
        self.assertEqual(partial["unavailable_prefixes"], [])


if __name__ == "__main__":
    unittest.main()
