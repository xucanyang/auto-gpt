from __future__ import annotations

import unittest

from services.idea_oaipay_pipeline.engine import IdeaOaiPayPipelineEngine, _has_phone_binding
from services.idea_oaipay_pipeline.models import (
    IdeaOaiPayPipelineConfig,
    IdeaOaiPayPipelineItem,
    IdeaOaiPayPipelineTask,
    OaiPayStepConfig,
    PhoneStepConfig,
)
from core.db import AccountModel


class IdeaOaiPayPipelineConfigTests(unittest.TestCase):
    def test_retired_subscription_types_are_filtered_from_legacy_config(self):
        config = IdeaOaiPayPipelineConfig.model_validate(
            {
                "idea": {"skip_if_subscription_in": ["plus", "team", "business", "enterprise", "plus"]},
                "check": {"gate": {"allowed_subscription_types": ["free", "team", "unknown"]}},
                "oaipay": {"require_subscription_in": ["pro", "business"]},
            }
        )

        self.assertEqual(config.idea.skip_if_subscription_in, ["plus"])
        self.assertEqual(config.check.gate.allowed_subscription_types, ["free"])
        self.assertEqual(config.oaipay.require_subscription_in, ["pro"])

    def test_default_idea_skip_list_only_contains_active_paid_plans(self):
        self.assertEqual(IdeaOaiPayPipelineConfig().idea.skip_if_subscription_in, ["plus", "pro"])

    def test_source_register_alias_round_trips_as_register(self):
        config = IdeaOaiPayPipelineConfig.model_validate(
            {
                "source": {
                    "type": "register",
                    "target_count": 3,
                    "register": {"batch_size": 2, "mail_provider": "cloudmail"},
                }
            }
        )

        self.assertEqual(config.source.register_config["batch_size"], 2)
        dumped = config.model_dump(by_alias=True)
        self.assertEqual(dumped["source"]["register"]["mail_provider"], "cloudmail")
        self.assertNotIn("register_config", dumped["source"])

    def test_validate_rejects_missing_local_source(self):
        engine = IdeaOaiPayPipelineEngine()
        config = IdeaOaiPayPipelineConfig()

        errors = engine.validate_config(config)

        self.assertIn("本地账号来源必须提供 account_ids 或 all_filtered=true", errors)

    def test_validate_rejects_register_without_target(self):
        engine = IdeaOaiPayPipelineEngine()
        config = IdeaOaiPayPipelineConfig.model_validate({"source": {"type": "register", "target_count": 0}})

        errors = engine.validate_config(config)

        self.assertIn("注册来源必须设置 target_count > 0", errors)

    def test_validate_rejects_invalid_phone_policy(self):
        engine = IdeaOaiPayPipelineEngine()
        config = IdeaOaiPayPipelineConfig.model_validate(
            {
                "source": {"type": "local", "account_ids": [1]},
                "phone": {"policy": "must"},
            }
        )

        errors = engine.validate_config(config)

        self.assertIn("手机号策略只能是 disabled / best_effort / required", errors)


class DummyStateStore:
    def __init__(self, items: list[IdeaOaiPayPipelineItem]) -> None:
        self.items = items
        self.updated: list[tuple[int, dict]] = []

    def list_items_by_statuses(self, pipeline_task_id: int, *, oaipay_stages=None, limit=200, **_kwargs):
        result = [item for item in self.items if int(item.pipeline_task_id or 0) == int(pipeline_task_id or 0)]
        if oaipay_stages:
            stages = set(oaipay_stages)
            result = [item for item in result if item.oaipay_stage in stages]
        return result[:limit]

    def update_item(self, item_id: int, **patch):
        for item in self.items:
            if int(item.id or 0) == int(item_id or 0):
                for key, value in patch.items():
                    setattr(item, key, value)
                self.updated.append((item_id, dict(patch)))
                return item
        return None


class DummyStartStateStore:
    def __init__(self) -> None:
        self.latest: IdeaOaiPayPipelineTask | None = None
        self.saved: list[IdeaOaiPayPipelineTask] = []
        self.logs: list[str] = []

    def get_latest_task(self):
        return self.latest

    def create_task(self, task_key: str, *, status: str, source_type: str, target_success_count: int, config, runtime_config=None):
        self.latest = IdeaOaiPayPipelineTask(
            id=1,
            task_key=task_key,
            status=status,
            source_type=source_type,
            target_success_count=target_success_count,
        )
        return self.latest

    def get_task(self, task_id: int):
        if self.latest and int(self.latest.id or 0) == int(task_id or 0):
            return self.latest
        return None

    def save_task(self, task: IdeaOaiPayPipelineTask):
        self.latest = task
        self.saved.append(task)
        return task

    def append_task_log(self, _task_id: int, line: str, *, limit: int = 800):
        self.logs.append(line)


class DummyRetryStateStore:
    def __init__(self) -> None:
        self.item = IdeaOaiPayPipelineItem(id=10, pipeline_task_id=1, account_id=101)
        self.latest = IdeaOaiPayPipelineTask(id=2, task_key="latest", status="done")

    def get_item(self, _item_id: int):
        return self.item

    def get_latest_task(self):
        return self.latest


class IdeaOaiPayPipelineEngineTests(unittest.TestCase):
    def test_disabled_check_still_blocks_retired_subscription(self):
        engine = IdeaOaiPayPipelineEngine()
        item = IdeaOaiPayPipelineItem(
            id=1,
            pipeline_task_id=9,
            account_id=101,
            subscription_type_before="business",
            check_stage="pending",
            gate_stage="pending",
            overall_status="pending",
        )
        state_store = DummyStateStore([item])
        engine.state_store = state_store  # type: ignore[assignment]
        task = IdeaOaiPayPipelineTask(id=9, task_key="retired", status="running")
        config = IdeaOaiPayPipelineConfig.model_validate({"check": {"enabled": False}})

        engine._run_check_step(task, config, limit=3)

        self.assertEqual(item.check_stage, "skipped")
        self.assertEqual(item.gate_stage, "blocked")
        self.assertEqual(item.overall_status, "manual_required")
        self.assertIn("已退役", item.last_error)

    def test_retired_subscription_is_rejected_by_gate_even_when_gate_is_disabled(self):
        engine = IdeaOaiPayPipelineEngine()
        account = AccountModel(platform="chatgpt", email="retired@example.com", password="x")
        account.set_extra({"chatgpt_local": {"subscription": {"plan": "business"}}})
        config = IdeaOaiPayPipelineConfig.model_validate(
            {"check": {"gate": {"enabled": False, "mode": "none"}}}
        )

        allowed, message = engine._evaluate_status_gate(account, config)

        self.assertFalse(allowed)
        self.assertIn("已退役", message)

    def test_retired_subscription_is_rejected_by_oaipay_requirements(self):
        engine = IdeaOaiPayPipelineEngine()
        item = IdeaOaiPayPipelineItem(
            id=1,
            pipeline_task_id=9,
            account_id=101,
            subscription_type_after="team",
            oaipay_stage="pending",
        )
        state_store = DummyStateStore([item])
        engine.state_store = state_store  # type: ignore[assignment]

        allowed = engine._oaipay_requirements_pass(item, IdeaOaiPayPipelineConfig())

        self.assertFalse(allowed)
        self.assertEqual(item.oaipay_stage, "skipped")
        self.assertIn("禁止上传", item.oaipay_message)

    def test_plus_phone_scope_does_not_include_retired_products(self):
        engine = IdeaOaiPayPipelineEngine()
        config = IdeaOaiPayPipelineConfig.model_validate({"phone": {"apply_to": "plus"}})
        retired = IdeaOaiPayPipelineItem(pipeline_task_id=1, subscription_type_after="enterprise")
        active = IdeaOaiPayPipelineItem(pipeline_task_id=1, subscription_type_after="plus")

        self.assertFalse(engine._phone_applies(retired, config))
        self.assertTrue(engine._phone_applies(active, config))

    def test_local_source_with_zero_matched_accounts_finishes_failed_without_worker(self):
        engine = IdeaOaiPayPipelineEngine()
        state_store = DummyStartStateStore()
        engine.state_store = state_store  # type: ignore[assignment]
        engine._seed_local_items = lambda _task, _config: 0  # type: ignore[method-assign]
        engine._ensure_worker = lambda: self.fail("empty local source must not start worker")  # type: ignore[method-assign]
        config = IdeaOaiPayPipelineConfig.model_validate({"source": {"type": "local", "account_ids": [999]}})

        task = engine.start(config)

        self.assertEqual(task.status, "failed")
        self.assertEqual(engine.status, "failed")
        self.assertEqual(task.last_error, "本地账号来源未匹配到任何 ChatGPT 账号")
        self.assertTrue(any("来源为空" in line for line in state_store.logs))

    def test_disabled_oaipay_can_finalize_disabled_stage_items(self):
        engine = IdeaOaiPayPipelineEngine()
        item = IdeaOaiPayPipelineItem(
            id=1,
            pipeline_task_id=9,
            account_id=101,
            email="a@example.com",
            gate_stage="pass",
            phone_stage="disabled",
            oaipay_stage="disabled",
            overall_status="pending",
        )
        state_store = DummyStateStore([item])
        engine.state_store = state_store  # type: ignore[assignment]
        task = IdeaOaiPayPipelineTask(id=9, task_key="t", status="running")
        config = IdeaOaiPayPipelineConfig.model_validate(
            {
                "source": {"type": "local", "account_ids": [101]},
                "phone": PhoneStepConfig(policy="disabled").model_dump(),
                "oaipay": OaiPayStepConfig(enabled=False).model_dump(),
            }
        )

        engine._run_oaipay_step(task, config, limit=3)

        self.assertEqual(item.oaipay_stage, "disabled")
        self.assertEqual(item.overall_status, "done")
        self.assertEqual(state_store.updated[-1][1]["overall_status"], "done")

    def test_bound_phone_payload_counts_as_existing_phone_binding(self):
        account = AccountModel(
            platform="chatgpt",
            email="bound@example.com",
            password="x",
            extra_json='{"chatgpt_bound_phone":{"masked":"•••• 5704","verification_status":"required"}}',
        )

        self.assertTrue(_has_phone_binding(account))

    def test_retry_rejects_non_latest_pipeline_item(self):
        engine = IdeaOaiPayPipelineEngine()
        engine.state_store = DummyRetryStateStore()  # type: ignore[assignment]

        with self.assertRaisesRegex(ValueError, "只能重试最近一条流水线任务"):
            engine.retry_item_stage(10, "check")


if __name__ == "__main__":
    unittest.main()
