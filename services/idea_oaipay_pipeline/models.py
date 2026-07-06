from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field as PydanticField
from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AccountSourceConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    type: str = "local"  # local | register
    account_ids: list[int] = PydanticField(default_factory=list)
    all_filtered: bool = False
    email: str = ""
    status: str = ""
    manually_used: str | None = None
    auth_type: str = ""
    subscription_type: str = ""
    account_validity: str = ""
    sub2api_state: str = ""
    oaipay_state: str = ""
    limit: int = 0
    target_count: int = 0
    register_config: dict = PydanticField(default_factory=dict, alias="register")


class IdeaStepConfig(BaseModel):
    enabled: bool = True
    use_pool: bool = True
    code_lines: str = ""
    precheck: bool = True
    failure_continue: bool = True
    submit_interval_seconds: int = 5
    auto_poll_status: bool = True
    status_poll_interval_seconds: int = 5
    status_poll_timeout_seconds: int = 1800
    skip_if_subscription_in: list[str] = PydanticField(default_factory=lambda: ["plus", "pro", "team", "enterprise"])


class StatusGateConfig(BaseModel):
    enabled: bool = True
    mode: str = "account_valid"  # none | account_valid | subscription_in | upload_ready
    allowed_subscription_types: list[str] = PydanticField(default_factory=list)


class CheckStepConfig(BaseModel):
    enabled: bool = True
    gate: StatusGateConfig = PydanticField(default_factory=StatusGateConfig)


class PhoneStepConfig(BaseModel):
    policy: str = "disabled"  # disabled | best_effort | required
    apply_to: str = "gate_passed"  # gate_passed | all | free | plus
    use_pool: bool = True
    phone_lines: str = ""
    timeout_seconds: int = 180
    poll_interval_seconds: int = 5
    max_resend_attempts: int = 0
    resend_interval_seconds: int = 30
    account_interval_seconds: int = 60
    proxy: str | None = None
    proxy_mode: str = "pool"
    proxy_country_code: str = ""
    proxy_failover: bool = True
    proxy_max_candidates: int = 0
    proxy_min_score: float = 0


class OaiPayStepConfig(BaseModel):
    enabled: bool = False
    category_id: int | None = None
    exists_as_success: bool = True
    require_phone_bound: bool = False
    require_subscription_in: list[str] = PydanticField(default_factory=list)


class IdeaOaiPayPipelineConfig(BaseModel):
    source: AccountSourceConfig = PydanticField(default_factory=AccountSourceConfig)
    idea: IdeaStepConfig = PydanticField(default_factory=IdeaStepConfig)
    check: CheckStepConfig = PydanticField(default_factory=CheckStepConfig)
    phone: PhoneStepConfig = PydanticField(default_factory=PhoneStepConfig)
    oaipay: OaiPayStepConfig = PydanticField(default_factory=OaiPayStepConfig)
    auto_start: bool = False
    tick_interval_seconds: int = 3


class IdeaOaiPayPipelineTask(SQLModel, table=True):
    __tablename__ = "idea_oaipay_pipeline_tasks"

    id: Optional[int] = Field(default=None, primary_key=True)
    task_key: str = Field(index=True, sa_column_kwargs={"unique": True})
    status: str = Field(default="stopped", index=True)
    source_type: str = Field(default="local", index=True)
    target_success_count: int = 0
    config_json: str = "{}"
    runtime_config_json: str = "{}"
    summary_json: str = "{}"
    logs_json: str = "[]"
    active_register_task_id: str = ""
    active_idea_task_id: str = ""
    active_phone_task_id: str = ""
    last_error: str = ""
    started_at: Optional[datetime] = None
    stopped_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class IdeaOaiPayPipelineItem(SQLModel, table=True):
    __tablename__ = "idea_oaipay_pipeline_items"

    id: Optional[int] = Field(default=None, primary_key=True)
    pipeline_task_id: int = Field(index=True)
    account_id: Optional[int] = Field(default=None, index=True)
    email: str = Field(default="", index=True)
    source_stage: str = Field(default="selected", index=True)
    register_stage: str = Field(default="skipped", index=True)
    idea_stage: str = Field(default="pending", index=True)
    check_stage: str = Field(default="pending", index=True)
    gate_stage: str = Field(default="pending", index=True)
    phone_stage: str = Field(default="disabled", index=True)
    oaipay_stage: str = Field(default="disabled", index=True)
    overall_status: str = Field(default="pending", index=True)
    subscription_type_before: str = ""
    subscription_type_after: str = ""
    account_validity: str = ""
    cdk_id: int = 0
    cdk_masked: str = ""
    idea_task_id: str = Field(default="", index=True)
    idea_order_id: str = ""
    idea_display_id: str = ""
    idea_error: str = ""
    phone_task_id: str = Field(default="", index=True)
    phone_policy: str = ""
    phone_error: str = ""
    oaipay_remote_state: str = ""
    oaipay_remote_account_id: str = ""
    oaipay_message: str = ""
    last_error: str = ""
    details_json: str = "{}"
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
