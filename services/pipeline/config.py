from __future__ import annotations

import json

from core.config_store import config_store

from .models import PipelineConfig


class PipelineConfigStore:
    """Config manager for the ChatGPT auto pipeline."""

    CONFIG_KEY = "chatgpt_auto_pipeline_config"

    def load(self) -> PipelineConfig:
        raw = str(config_store.get(self.CONFIG_KEY, "") or "").strip()
        if not raw:
            return PipelineConfig()
        try:
            payload = json.loads(raw)
        except Exception:
            return PipelineConfig()
        if not isinstance(payload, dict):
            return PipelineConfig()
        try:
            return PipelineConfig(**payload)
        except Exception:
            return PipelineConfig()

    def save(self, config: PipelineConfig) -> PipelineConfig:
        payload = config.model_dump()
        config_store.set(self.CONFIG_KEY, json.dumps(payload, ensure_ascii=False))
        return config

    def validate(self, config: PipelineConfig) -> list[str]:
        errors: list[str] = []

        if int(config.payment_pool_threshold) < 1:
            errors.append("payment_pool_threshold 必须大于等于 1")
        if int(config.payment_pool_target) < int(config.payment_pool_threshold):
            errors.append("payment_pool_target 不能小于 payment_pool_threshold")

        non_negative_fields = {
            "payment_batch_interval_seconds": config.payment_batch_interval_seconds,
            "payment_batch_max_size": config.payment_batch_max_size,
            "auth_poll_interval_seconds": config.auth_poll_interval_seconds,
            "register_poll_interval_seconds": config.register_poll_interval_seconds,
            "gopay_batch_poll_interval_seconds": config.gopay_batch_poll_interval_seconds,
            "gopay_timeout_seconds": config.gopay_timeout_seconds,
        }
        for field_name, value in non_negative_fields.items():
            if int(value) < 0:
                errors.append(f"{field_name} 不能小于 0")

        if str(config.platform or "").strip().lower() != "chatgpt":
            errors.append("platform 必须为 chatgpt")

        return errors

    def can_update_while_running(self) -> bool:
        return False
