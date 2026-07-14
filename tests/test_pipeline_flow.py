from __future__ import annotations

from datetime import datetime, timezone
import unittest
from unittest import mock

from services.chatgpt_account_state import apply_chatgpt_status_policy
from services.pipeline.auth_scheduler import AuthCaptureScheduler
from services.pipeline.engine import PipelineEngine
from services.pipeline.models import PipelineAccountItem, PipelineConfig, PipelineTask
from services.pipeline.payment_scheduler import PaymentBatchScheduler
from services.pipeline.register_scheduler import RegisterRefillScheduler


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _make_task(**overrides) -> PipelineTask:
    task = PipelineTask(
        id=1,
        task_key="pipeline-test",
        status="running",
        created_at=_utcnow(),
        updated_at=_utcnow(),
    )
    for key, value in overrides.items():
        setattr(task, key, value)
    return task


def _make_item(**overrides) -> PipelineAccountItem:
    item = PipelineAccountItem(
        id=int(overrides.pop("id", 1)),
        pipeline_task_id=int(overrides.pop("pipeline_task_id", 1)),
        account_id=overrides.pop("account_id", None),
        email=overrides.pop("email", ""),
        source=overrides.pop("source", "pipeline_register"),
        source_register_task_id=overrides.pop("source_register_task_id", ""),
        pipeline_status=overrides.pop("pipeline_status", "pending_payment"),
        register_stage=overrides.pop("register_stage", "success"),
        payment_stage=overrides.pop("payment_stage", "pending"),
        auth_stage=overrides.pop("auth_stage", "disabled"),
        account_primary_status=overrides.pop("account_primary_status", "registered"),
        created_at=overrides.pop("created_at", _utcnow()),
        updated_at=overrides.pop("updated_at", _utcnow()),
    )
    for key, value in overrides.items():
        setattr(item, key, value)
    return item


class DummyConfigStore:
    def __init__(self, config: PipelineConfig | None = None) -> None:
        self._config = config or PipelineConfig()

    def load(self) -> PipelineConfig:
        return self._config


class DummyLogBus:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def publish(self, line: str) -> None:
        self.lines.append(str(line))


class DummyStateStore:
    def __init__(self) -> None:
        self.latest_task: PipelineTask | None = None
        self.pending_items: list[PipelineAccountItem] = []
        self.paid_items: list[PipelineAccountItem] = []
        self.failed_items: list[PipelineAccountItem] = []
        self.auth_pending_items: list[PipelineAccountItem] = []
        self.account_items: list[PipelineAccountItem] = []
        self.batch_items: list[PipelineAccountItem] = []
        self.saved_tasks: list[PipelineTask] = []
        self.updated_items: list[tuple[int, dict]] = []
        self.created_items: list[PipelineAccountItem] = []
        self.task_logs: list[dict] = []

    def _items_by_id(self) -> dict[int, PipelineAccountItem]:
        result: dict[int, PipelineAccountItem] = {}
        for group in (
            self.pending_items,
            self.paid_items,
            self.failed_items,
            self.auth_pending_items,
            self.account_items,
            self.batch_items,
            self.created_items,
        ):
            for item in group:
                if int(item.id or 0) > 0:
                    result[int(item.id or 0)] = item
        return result

    def get_latest_task(self) -> PipelineTask | None:
        return self.latest_task

    def save_task(self, task: PipelineTask) -> PipelineTask:
        self.latest_task = task
        self.saved_tasks.append(task)
        return task

    def create_task(self, task_key: str, *, status: str = "stopped", config_snapshot=None) -> PipelineTask:
        task = _make_task(
            id=999,
            task_key=task_key,
            status=status,
        )
        self.latest_task = task
        self.saved_tasks.append(task)
        return task

    def list_pending_payment_items(self, _pipeline_task_id: int) -> list[PipelineAccountItem]:
        return list(self.pending_items)

    def reserve_pending_payment_items(self, _pipeline_task_id: int, *, limit: int) -> list[PipelineAccountItem]:
        reserved = list(self.pending_items[:limit])
        for item in reserved:
            item.pipeline_status = "payment_reserved"
            item.payment_stage = "reserved"
        return reserved

    def list_paid_items(self, _pipeline_task_id: int) -> list[PipelineAccountItem]:
        return list(self.paid_items)

    def list_failed_items(self, _pipeline_task_id: int) -> list[PipelineAccountItem]:
        return list(self.failed_items)

    def list_auth_pending_items(self, _pipeline_task_id: int) -> list[PipelineAccountItem]:
        return list(self.auth_pending_items)

    def list_account_items(self, _pipeline_task_id: int) -> list[PipelineAccountItem]:
        return list(self.account_items)

    def list_account_items_by_batch(self, _pipeline_task_id: int, _batch_id: str) -> list[PipelineAccountItem]:
        return list(self.batch_items)

    def get_account_item_by_account_id(self, _pipeline_task_id: int, account_id: int) -> PipelineAccountItem | None:
        for item in self._items_by_id().values():
            if int(item.account_id or 0) == int(account_id or 0):
                return item
        return None

    def update_account_item(self, item_id: int, **patch):
        self.updated_items.append((int(item_id or 0), dict(patch)))
        item = self._items_by_id().get(int(item_id or 0))
        if item is not None:
            for key, value in patch.items():
                setattr(item, key, value)
            item.updated_at = _utcnow()
        return item

    def create_account_item(self, item: PipelineAccountItem) -> PipelineAccountItem:
        self.created_items.append(item)
        self.account_items.append(item)
        return item

    def list_task_logs(self, _pipeline_task_id: int, *, limit: int = 200) -> list[dict]:
        return list(self.task_logs[:limit])

    def list_tasks(self, *, limit: int = 20) -> list[PipelineTask]:
        tasks = list(self.saved_tasks)
        if self.latest_task is not None and self.latest_task not in tasks:
            tasks.insert(0, self.latest_task)
        return tasks[:limit]


class DummyAccount:
    def __init__(self, status: str = "registered", extra: dict | None = None) -> None:
        self.status = status
        self.extra = dict(extra or {})
        self.token = str(self.extra.get("access_token") or "")
        self.user_id = ""

    def get_extra(self):
        return dict(self.extra)


class FakePaymentSession:
    def __init__(self, accounts: dict[int, object]) -> None:
        self.accounts = accounts
        self.committed = False
        self.added: list[object] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get(self, _model, account_id: int):
        return self.accounts.get(int(account_id or 0))

    def commit(self):
        self.committed = True

    def add(self, account: object):
        self.added.append(account)


class RegisterSchedulerTests(unittest.TestCase):
    def test_refill_starts_when_pending_pool_below_threshold(self):
        state_store = DummyStateStore()
        state_store.pending_items = [_make_item(id=1, account_id=11, email="a@example.com")]
        config_store = DummyConfigStore(
            PipelineConfig(
                payment_pool_threshold=2,
                payment_pool_target=4,
                mail_provider="icloud_hme",
                register_extra={
                    "chatgpt_registration_mode": "refresh_token",
                    "chatgpt_access_token_only_checkout_amount_check_enabled": True,
                },
            )
        )
        log_bus = DummyLogBus()
        scheduler = RegisterRefillScheduler(
            state_store=state_store,
            config_store=config_store,
            log_bus=log_bus,
        )
        task = _make_task(id=9, task_key="pipe-9")

        with (
            mock.patch("services.pipeline.register_scheduler.has_active_register_task", return_value=False),
            mock.patch("services.pipeline.register_scheduler.enqueue_register_task", return_value="reg-1") as enqueue_mock,
        ):
            result = scheduler.tick(task)

        self.assertEqual(result["action"], "start")
        self.assertEqual(result["refill_count"], 3)
        self.assertEqual(task.active_register_task_id, "reg-1")
        req = enqueue_mock.call_args.args[0]
        self.assertEqual(req.platform, "chatgpt")
        self.assertEqual(req.count, 3)
        self.assertEqual(req.extra["chatgpt_registration_mode"], "access_token_only")
        self.assertFalse(req.extra["chatgpt_access_token_only_checkout_amount_check_enabled"])
        self.assertEqual(enqueue_mock.call_args.kwargs["source"], "pipeline")
        self.assertEqual(enqueue_mock.call_args.kwargs["meta"]["pipeline_task_id"], 9)
        self.assertEqual(enqueue_mock.call_args.kwargs["meta"]["pipeline_key"], "pipe-9")


class PaymentSchedulerTests(unittest.TestCase):
    def _make_scheduler(self, *, config: PipelineConfig | None = None, state_store: DummyStateStore | None = None):
        return PaymentBatchScheduler(
            state_store=state_store or DummyStateStore(),
            config_store=DummyConfigStore(config or PipelineConfig()),
            log_bus=DummyLogBus(),
        )

    def test_payment_tick_polls_existing_batch_instead_of_starting_new_batch(self):
        scheduler = self._make_scheduler()
        task = _make_task(active_payment_batch_id="batch-live")

        with mock.patch.object(scheduler, "poll_active_batch", return_value={"status": "running"}) as poll_mock:
            result = scheduler.tick(task)

        self.assertEqual(result["action"], "poll")
        poll_mock.assert_called_once_with(task)

    def test_available_phone_candidates_only_counts_unique_ready_enabled_entries(self):
        scheduler = self._make_scheduler()
        phone_pool = [
            {"uid": "u1", "phone_country_code": "62", "phone_number": "811111111", "enabled": True, "status": "ready"},
            {"uid": "u2", "phone_country_code": "62", "phone_number": "811111111", "enabled": True, "status": "ready"},
            {"uid": "u3", "phone_country_code": "62", "phone_number": "822222222", "enabled": False, "status": "ready"},
            {"uid": "u4", "phone_country_code": "62", "phone_number": "833333333", "enabled": True, "status": "reserved"},
            {"uid": "u5", "phone_country_code": "62", "phone_number": "844444444", "enabled": True, "status": "ready"},
        ]

        with mock.patch("services.pipeline.payment_scheduler._adapter_state", return_value={"phone_pool": phone_pool}):
            result = scheduler._available_phone_candidates()

        self.assertEqual([item["uid"] for item in result], ["u1", "u5"])

    def test_payment_tick_reserves_accounts_before_generating_links(self):
        state_store = DummyStateStore()
        reserved_items = [
            _make_item(id=1, account_id=101, email="a@example.com"),
            _make_item(id=2, account_id=102, email="b@example.com"),
        ]
        state_store.pending_items = list(reserved_items)
        scheduler = self._make_scheduler(
            config=PipelineConfig(payment_batch_max_size=2),
            state_store=state_store,
        )
        task = _make_task(id=1)
        call_order: list[str] = []

        def reserve_items(_pipeline_task_id: int, *, limit: int):
            self.assertEqual(limit, 2)
            call_order.append("reserve")
            return list(reserved_items)

        def prepare_links(items):
            self.assertEqual(call_order, ["reserve"])
            self.assertEqual(items, reserved_items)
            call_order.append("prepare")
            return {"ready_items": list(reserved_items), "failed_item_ids": []}

        def start_batch(_task, items, phones):
            self.assertEqual(call_order, ["reserve", "prepare"])
            self.assertEqual(items, reserved_items)
            self.assertEqual(len(phones), 2)
            call_order.append("start")
            return {"task_id": "batch-1"}

        state_store.reserve_pending_payment_items = reserve_items
        with (
            mock.patch.object(scheduler, "_available_phone_candidates", return_value=[{"uid": "u1"}, {"uid": "u2"}]),
            mock.patch.object(scheduler, "_prepare_checkout_links", side_effect=prepare_links),
            mock.patch.object(scheduler, "start_batch", side_effect=start_batch),
        ):
            result = scheduler.tick(task)

        self.assertEqual(result["action"], "start")
        self.assertEqual(call_order, ["reserve", "prepare", "start"])

    def test_prepare_checkout_links_records_success_and_payment_link_failures(self):
        state_store = DummyStateStore()
        success_item = _make_item(id=11, account_id=201, email="ok@example.com")
        failed_item = _make_item(id=12, account_id=202, email="bad@example.com")
        state_store.account_items = [success_item, failed_item]
        scheduler = self._make_scheduler(state_store=state_store)
        fake_accounts = {
            201: type("AccountModel", (), {"platform": "chatgpt", "email": "ok@example.com", "status": "registered"})(),
            202: type("AccountModel", (), {"platform": "chatgpt", "email": "bad@example.com", "status": "registered"})(),
        }
        fake_session = FakePaymentSession(fake_accounts)

        class FakePlatform:
            def __init__(self, config=None):
                self.config = config

        action_results = [
            {"ok": True, "data": {"url": "https://pay.example.com/checkout/ok"}},
            {"ok": False, "error": "订阅链接失效"},
        ]

        with (
            mock.patch("services.pipeline.payment_scheduler.Session", return_value=fake_session),
            mock.patch("services.pipeline.payment_scheduler.get", return_value=FakePlatform),
            mock.patch(
                "services.pipeline.payment_scheduler._execute_platform_action",
                side_effect=action_results,
            ),
        ):
            result = scheduler._prepare_checkout_links([success_item, failed_item])

        self.assertTrue(fake_session.committed)
        self.assertEqual(len(result["ready_items"]), 1)
        self.assertEqual(result["ready_items"][0].checkout_url, "https://pay.example.com/checkout/ok")
        failure_patch = next(patch for item_id, patch in state_store.updated_items if item_id == 12 and patch.get("payment_stage") == "failed")
        self.assertEqual(failure_patch["payment_failed_stage"], "payment_link")
        self.assertEqual(failure_patch["payment_error_code"], "checkout_invalid")

    def test_payment_success_updates_primary_status_and_subscription_plan(self):
        state_store = DummyStateStore()
        batch_item = _make_item(id=21, account_id=301, payment_batch_task_id="batch-1", pipeline_status="paying", payment_stage="paying")
        state_store.batch_items = [batch_item]
        scheduler = self._make_scheduler(state_store=state_store)

        snapshot = {
            "task_id": "batch-1",
            "items": [
                {
                    "account_id": 301,
                    "status": "done",
                    "snapshot": {"phase": "succeeded"},
                    "phone_deferred_reason": "支付成功",
                }
            ],
        }

        with mock.patch.object(
            scheduler,
            "_refresh_subscription_state",
            return_value={
                "subscription_plan_confirmed": "plus",
                "subscription_refresh_status": "success",
                "subscription_refreshed_at": _utcnow().isoformat(),
            },
        ), mock.patch.object(scheduler, "_persist_account_primary_status") as persist_mock:
            scheduler._sync_batch_result_to_items(1, snapshot)

        success_patch = next(patch for item_id, patch in state_store.updated_items if item_id == 21 and patch.get("payment_stage") == "success")
        self.assertEqual(success_patch["pipeline_status"], "paid")
        self.assertEqual(success_patch["account_primary_status"], "subscribed")
        self.assertEqual(success_patch["subscription_plan_confirmed"], "plus")
        self.assertEqual(success_patch["subscription_refresh_status"], "success")
        persist_mock.assert_called_once_with(301, "subscribed")

    def test_payment_and_failure_reason_mapping_is_preserved(self):
        state_store = DummyStateStore()
        batch_item = _make_item(id=22, account_id=302, payment_batch_task_id="batch-2", pipeline_status="paying", payment_stage="paying")
        state_store.batch_items = [batch_item]
        scheduler = self._make_scheduler(state_store=state_store)

        snapshot = {
            "task_id": "batch-2",
            "items": [
                {
                    "account_id": 302,
                    "status": "failed",
                    "snapshot": {"phase": "failed"},
                    "error": "No active subscription plans found",
                }
            ],
        }

        with mock.patch.object(scheduler, "_persist_account_primary_status") as persist_mock:
            scheduler._sync_batch_result_to_items(1, snapshot)

        failure_patch = next(patch for item_id, patch in state_store.updated_items if item_id == 22 and patch.get("payment_stage") == "failed")
        self.assertEqual(failure_patch["pipeline_status"], "failed")
        self.assertEqual(failure_patch["account_primary_status"], "payment_failed")
        self.assertEqual(failure_patch["payment_error_code"], "not_eligible")
        self.assertEqual(failure_patch["payment_error_reason"], "No active subscription plans found")
        persist_mock.assert_called_once_with(302, "payment_failed")

    def test_payment_cancelled_updates_primary_status_to_payment_failed(self):
        state_store = DummyStateStore()
        batch_item = _make_item(id=23, account_id=303, payment_batch_task_id="batch-3", pipeline_status="paying", payment_stage="paying")
        state_store.batch_items = [batch_item]
        scheduler = self._make_scheduler(state_store=state_store)

        snapshot = {
            "task_id": "batch-3",
            "items": [
                {
                    "account_id": 303,
                    "status": "cancelled",
                    "snapshot": {"phase": "cancelled"},
                }
            ],
        }

        with mock.patch.object(scheduler, "_persist_account_primary_status") as persist_mock:
            scheduler._sync_batch_result_to_items(1, snapshot)

        failure_patch = next(patch for item_id, patch in state_store.updated_items if item_id == 23 and patch.get("payment_stage") == "failed")
        self.assertEqual(failure_patch["account_primary_status"], "payment_failed")
        self.assertEqual(failure_patch["payment_error_code"], "payment_cancelled")
        persist_mock.assert_called_once_with(303, "payment_failed")

    def test_batch_start_failure_marks_ready_items_failed(self):
        state_store = DummyStateStore()
        ready_item = _make_item(id=24, account_id=304, email="ready@example.com", pipeline_status="link_ready", payment_stage="link_ready")
        state_store.account_items = [ready_item]
        state_store.pending_items = [ready_item]
        scheduler = self._make_scheduler(state_store=state_store)
        task = _make_task(id=1)

        with (
            mock.patch.object(scheduler, "_available_phone_candidates", return_value=[{"uid": "u1"}]),
            mock.patch.object(scheduler, "_prepare_checkout_links", return_value={"ready_items": [ready_item], "failed_item_ids": []}),
            mock.patch.object(scheduler, "start_batch", side_effect=RuntimeError("session lost")),
            mock.patch.object(scheduler, "_persist_account_primary_status") as persist_mock,
        ):
            result = scheduler.tick(task)

        self.assertEqual(result["action"], "failed")
        failure_patch = next(patch for item_id, patch in state_store.updated_items if item_id == 24 and patch.get("payment_failed_stage") == "gopay_start")
        self.assertEqual(failure_patch["pipeline_status"], "failed")
        self.assertEqual(failure_patch["account_primary_status"], "payment_failed")
        self.assertEqual(failure_patch["payment_error_code"], "session_missing")
        persist_mock.assert_called_once_with(304, "payment_failed")


class AuthSchedulerTests(unittest.TestCase):
    def _make_scheduler(self, *, config: PipelineConfig | None = None, state_store: DummyStateStore | None = None):
        return AuthCaptureScheduler(
            state_store=state_store or DummyStateStore(),
            config_store=DummyConfigStore(config or PipelineConfig()),
            log_bus=DummyLogBus(),
        )

    def test_auth_disabled_marks_paid_items_done_without_reverting_payment_result(self):
        state_store = DummyStateStore()
        state_store.paid_items = [
            _make_item(id=31, account_id=401, pipeline_status="paid", payment_stage="success", auth_stage="disabled", success_summary="支付成功")
        ]
        scheduler = self._make_scheduler(
            config=PipelineConfig(enable_auth_capture=False),
            state_store=state_store,
        )

        result = scheduler.tick(_make_task(id=1))

        self.assertEqual(result["action"], "skip_auth")
        done_patch = next(patch for item_id, patch in state_store.updated_items if item_id == 31)
        self.assertEqual(done_patch["pipeline_status"], "done")
        self.assertEqual(done_patch["auth_stage"], "skipped")

    def test_auth_enabled_processes_one_item_sequentially_and_records_failure_reason(self):
        state_store = DummyStateStore()
        first_item = _make_item(id=41, account_id=501, pipeline_status="paid", payment_stage="success", auth_stage="pending", account_primary_status="subscribed")
        second_item = _make_item(id=42, account_id=502, pipeline_status="paid", payment_stage="success", auth_stage="pending", account_primary_status="subscribed")
        state_store.paid_items = [first_item, second_item]
        state_store.account_items = [first_item, second_item]
        scheduler = self._make_scheduler(
            config=PipelineConfig(enable_auth_capture=True),
            state_store=state_store,
        )
        task = _make_task(id=7)

        with mock.patch("services.pipeline.auth_scheduler.enqueue_resume_subscription_auth_task", return_value="auth-1"):
            start_result = scheduler.tick(task)

        self.assertEqual(start_result["action"], "start")
        self.assertEqual(start_result["account_item_id"], 41)
        self.assertEqual(task.active_auth_task_id, "auth-1")
        self.assertTrue(any(item_id == 41 and patch.get("pipeline_status") == "auth_running" for item_id, patch in state_store.updated_items))
        self.assertFalse(any(item_id == 42 and patch.get("pipeline_status") == "auth_running" for item_id, patch in state_store.updated_items))

        with mock.patch(
            "services.pipeline.auth_scheduler.get_task",
            return_value={
                "status": "failed",
                "meta": {"account_id": 501},
                "errors": ["add_phone required"],
            },
        ):
            scheduler.poll_active_task(task)

        failure_patch = next(patch for item_id, patch in state_store.updated_items if item_id == 41 and patch.get("auth_stage") == "failed")
        self.assertEqual(failure_patch["pipeline_status"], "auth_failed")
        self.assertEqual(failure_patch["auth_error_code"], "auth_capture_failed")
        self.assertEqual(failure_patch["auth_error_reason"], "add_phone required")
        self.assertNotIn("account_primary_status", failure_patch)


class PipelineEngineRecoveryTests(unittest.TestCase):
    def test_status_snapshot_restores_queues_and_active_batch_for_page_reopen(self):
        engine = PipelineEngine()
        engine.config_store = DummyConfigStore(PipelineConfig())
        state_store = DummyStateStore()
        task = _make_task(
            id=18,
            task_key="pipe-18",
            active_payment_batch_id="batch-18",
        )
        state_store.latest_task = task
        state_store.pending_items = [_make_item(id=51, account_id=601, email="pending@example.com", pipeline_status="pending_payment")]
        state_store.paid_items = [_make_item(id=52, account_id=602, email="paid@example.com", pipeline_status="paid", payment_stage="success", account_primary_status="subscribed")]
        state_store.failed_items = [_make_item(id=53, account_id=603, email="failed@example.com", pipeline_status="failed", payment_stage="failed")]
        state_store.auth_pending_items = [_make_item(id=54, account_id=604, email="auth@example.com", pipeline_status="auth_pending", auth_stage="pending")]
        engine.state_store = state_store

        with mock.patch(
            "services.pipeline.engine.get_active_gopay_batch_payment",
            return_value={"task": {"task_id": "batch-18", "status": "running"}},
        ):
            snapshot = engine.get_status_snapshot()

        self.assertEqual(snapshot["task"]["active_payment_batch_id"], "batch-18")
        self.assertEqual(snapshot["summary"]["pending_payment_count"], 1)
        self.assertEqual(snapshot["summary"]["paid_count"], 1)
        self.assertEqual(snapshot["summary"]["failed_count"], 1)
        self.assertEqual(snapshot["summary"]["auth_pending_count"], 1)
        self.assertEqual(snapshot["active_payment_batch"]["task_id"], "batch-18")

    def test_service_restart_reconcile_marks_lost_states_and_adopts_live_batch(self):
        engine = PipelineEngine()
        engine.config_store = DummyConfigStore(PipelineConfig())
        state_store = DummyStateStore()
        task = _make_task(
            id=19,
            task_key="pipe-19",
            status="stopped",
            active_register_task_id="register-lost",
            active_payment_batch_id="old-batch",
            active_auth_task_id="auth-lost",
        )
        state_store.latest_task = task
        state_store.account_items = [
            _make_item(id=61, account_id=701, pipeline_status="registering", register_stage="running"),
            _make_item(id=62, account_id=702, pipeline_status="auth_running", auth_stage="running", account_primary_status="subscribed"),
        ]
        engine.state_store = state_store
        engine.log_bus = DummyLogBus()

        with mock.patch(
            "services.pipeline.engine.get_active_gopay_batch_payment",
            return_value={"task": {"task_id": "batch-live", "status": "running"}},
        ):
            reconciled = engine.reconcile_task(task)

        self.assertEqual(reconciled.active_payment_batch_id, "batch-live")
        self.assertEqual(reconciled.active_register_task_id, "")
        self.assertEqual(reconciled.active_auth_task_id, "")
        self.assertEqual(reconciled.status, "running")
        register_patch = next(patch for item_id, patch in state_store.updated_items if item_id == 61)
        auth_patch = next(patch for item_id, patch in state_store.updated_items if item_id == 62)
        self.assertEqual(register_patch["register_error_code"], "register_state_lost")
        self.assertEqual(auth_patch["auth_error_code"], "auth_state_lost")

    def test_restore_or_start_relaunches_worker_for_running_task(self):
        engine = PipelineEngine()
        engine.config_store = DummyConfigStore(PipelineConfig())
        state_store = DummyStateStore()
        state_store.latest_task = _make_task(id=20, task_key="pipe-20", status="running")
        engine.state_store = state_store

        with mock.patch.object(engine, "_ensure_worker_thread") as ensure_worker_mock:
            restored = engine.restore_or_start()

        self.assertEqual(restored.status, "running")
        ensure_worker_mock.assert_called_once_with(paused=False, log_message="自动流水线已恢复")

    def test_start_relaunches_worker_when_status_running_but_thread_missing(self):
        engine = PipelineEngine()
        engine.config_store = DummyConfigStore(PipelineConfig())
        state_store = DummyStateStore()
        state_store.latest_task = _make_task(id=21, task_key="pipe-21", status="running")
        engine.state_store = state_store
        engine.status = "running"

        with (
            mock.patch.object(engine, "_thread_is_alive", return_value=False),
            mock.patch.object(engine, "_ensure_worker_thread") as ensure_worker_mock,
        ):
            engine.start()

        ensure_worker_mock.assert_called_once()

    def test_payment_scheduler_uses_batch_poll_interval_for_active_batch(self):
        engine = PipelineEngine()
        engine.config_store = DummyConfigStore(
            PipelineConfig(
                payment_batch_interval_seconds=300,
                gopay_batch_poll_interval_seconds=7,
            )
        )

        active_task = _make_task(active_payment_batch_id="batch-live")
        idle_task = _make_task(active_payment_batch_id="")

        self.assertEqual(engine._payment_interval_seconds(active_task, engine.config_store.load()), 7.0)
        self.assertEqual(engine._payment_interval_seconds(idle_task, engine.config_store.load()), 300.0)


class AccountStatusConsistencyTests(unittest.TestCase):
    def test_subscribed_status_is_not_downgraded_when_subscription_refresh_is_incomplete(self):
        account = DummyAccount(
            status="subscribed",
            extra={"access_token": "at-demo"},
        )

        reason = apply_chatgpt_status_policy(
            account,
            local_probe={
                "auth": {"state": "probe_failed", "http_status": 0},
                "subscription": {"plan": "unknown"},
                "codex": {"state": "not_checked"},
            },
        )

        self.assertEqual(reason, "")
        self.assertEqual(account.status, "subscribed")

    def test_subscribed_status_is_demoted_when_free_plan_is_confirmed_without_payment_success(self):
        account = DummyAccount(
            status="subscribed",
            extra={"access_token": "at-demo"},
        )

        reason = apply_chatgpt_status_policy(
            account,
            local_probe={
                "auth": {"state": "access_token_valid", "http_status": 200},
                "subscription": {"plan": "free"},
                "codex": {"state": "not_checked"},
            },
        )

        self.assertEqual(reason, "")
        self.assertEqual(account.status, "registered")


if __name__ == "__main__":
    unittest.main()
