from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field as PydanticField
from pydantic.json_schema import SkipJsonSchema
from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PipelineConfig(BaseModel):
    payment_pool_threshold: int = 3
    payment_pool_target: int = 6
    payment_batch_interval_seconds: int = 300
    payment_batch_max_size: int = 0
    auto_start: bool = False
    enable_auth_capture: bool = False
    auth_poll_interval_seconds: int = 3
    register_poll_interval_seconds: int = 3
    gopay_batch_poll_interval_seconds: int = 3
    gopay_timeout_seconds: int = 1800
    platform: str = "chatgpt"
    mail_provider: str = ""
    proxy: Optional[str] = None
    executor_type: str = "protocol"
    captcha_solver: str = "yescaptcha"
    register_extra: dict = PydanticField(default_factory=dict)
    gopay_country: str = "ID"
    gopay_currency: str = "IDR"
    gopay_plan: SkipJsonSchema[str] = PydanticField(default="plus", exclude=True)


class PipelineTask(SQLModel, table=True):
    __tablename__ = "pipeline_tasks"

    id: Optional[int] = Field(default=None, primary_key=True)
    task_key: str = Field(index=True, sa_column_kwargs={"unique": True})
    status: str = Field(default="stopped", index=True)
    active_register_task_id: str = ""
    active_payment_batch_id: str = ""
    active_auth_task_id: str = ""
    config_snapshot_json: str = "{}"
    last_error: str = ""
    logs_json: str = "[]"
    started_at: Optional[datetime] = None
    stopped_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class PipelineAccountItem(SQLModel, table=True):
    __tablename__ = "pipeline_account_items"

    id: Optional[int] = Field(default=None, primary_key=True)
    pipeline_task_id: int = Field(index=True)
    account_id: Optional[int] = Field(default=None, index=True)
    email: str = Field(default="", index=True)
    source: str = "pipeline_register"
    source_register_task_id: str = ""
    source_register_attempt: int = 0
    pipeline_status: str = Field(default="pending_register", index=True)
    register_stage: str = Field(default="pending", index=True)
    payment_stage: str = Field(default="pending", index=True)
    auth_stage: str = Field(default="disabled", index=True)
    account_primary_status: str = "registered"
    checkout_url: str = ""
    payment_batch_task_id: str = ""
    gopay_session_id: str = ""
    gopay_uid: str = ""
    subscription_plan_expected: str = ""
    subscription_plan_confirmed: str = ""
    subscription_refresh_status: str = ""
    subscription_refreshed_at: str = ""
    register_error_code: str = ""
    register_error_reason: str = ""
    register_error_detail: str = ""
    payment_failed_stage: str = ""
    payment_error_code: str = ""
    payment_error_reason: str = ""
    payment_error_detail: str = ""
    auth_error_code: str = ""
    auth_error_reason: str = ""
    auth_error_detail: str = ""
    success_summary: str = ""
    register_started_at: Optional[datetime] = None
    register_completed_at: Optional[datetime] = None
    payment_started_at: Optional[datetime] = None
    payment_completed_at: Optional[datetime] = None
    auth_started_at: Optional[datetime] = None
    auth_completed_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
