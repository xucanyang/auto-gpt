"""数据库模型 - SQLite via SQLModel"""
from datetime import datetime, timezone
from math import ceil
import os
import threading
from typing import Any, Optional
from sqlalchemy import Index, event, inspect, text, UniqueConstraint
from sqlmodel import Field, SQLModel, create_engine, Session, select
import json


def _utcnow():
    return datetime.now(timezone.utc)

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///account_manager.db")
_IS_SQLITE = DATABASE_URL.startswith("sqlite")

_engine_kwargs = {}
if _IS_SQLITE:
    _engine_kwargs["connect_args"] = {
        "timeout": 30,
        "check_same_thread": False,
    }

engine = create_engine(DATABASE_URL, **_engine_kwargs)

_CHATGPT_AUTH_LIFECYCLE_BACKFILL_LOCK = threading.Lock()
_CHATGPT_AUTH_LIFECYCLE_BACKFILL_RUNNING = False


if _IS_SQLITE:
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=30000")
        finally:
            cursor.close()


class AccountModel(SQLModel, table=True):
    __tablename__ = "accounts"
    __table_args__ = (
        Index("idx_accounts_platform_created_at_id", "platform", "created_at", "id"),
        Index("idx_accounts_status_platform", "status", "platform"),
        Index(
            "idx_accounts_platform_list_state_freshness",
            "platform",
            "updated_at",
            "email",
            "created_at",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    platform: str = Field(index=True)
    email: str = Field(index=True)
    password: str
    user_id: str = ""
    region: str = ""
    token: str = ""
    status: str = "registered"
    trial_end_time: int = 0
    cashier_url: str = ""
    extra_json: str = "{}"   # JSON 存储平台自定义字段
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    def get_extra(self) -> dict:
        return json.loads(self.extra_json or "{}")

    def set_extra(self, d: dict):
        self.extra_json = json.dumps(d, ensure_ascii=False)


class ChatGPTAuthLifecycleModel(SQLModel, table=True):
    """Non-secret, queryable authentication lifecycle snapshot for ChatGPT accounts."""

    __tablename__ = "chatgpt_auth_lifecycles"
    __table_args__ = (
        Index("idx_chatgpt_auth_lifecycle_access_expiry", "access_token_expires_at"),
        Index("idx_chatgpt_auth_lifecycle_derived_state", "derived_state"),
        Index("idx_chatgpt_auth_lifecycle_probe_state", "probe_state"),
    )

    account_id: int = Field(primary_key=True)
    schema_version: int = 3
    material_revision: str = ""
    access_token_present: bool = False
    access_token_state: str = "unknown"
    access_token_issued_at: str = ""
    access_token_expires_at: str = ""
    access_token_expiry_source: str = ""
    access_token_expiry_confidence: str = "unknown"
    access_token_observed_at: str = ""
    access_token_last_probe_at: str = ""
    access_token_last_http_status: int = 0
    access_token_last_error_code: str = ""
    refresh_token_present: bool = False
    refresh_token_state: str = "unknown"
    refresh_token_expires_at: str = ""
    refresh_token_expiry_source: str = ""
    refresh_token_last_attempt_at: str = ""
    refresh_token_last_success_at: str = ""
    refresh_token_last_failure_at: str = ""
    refresh_token_last_result: str = "not_attempted"
    refresh_token_last_http_status: int = 0
    refresh_token_last_error_code: str = ""
    refresh_token_last_error_message: str = ""
    session_token_present: bool = False
    cookies_present: bool = False
    web_session_expires_at: str = ""
    web_session_expiry_source: str = ""
    web_session_observed_at: str = ""
    account_evidence_state: str = "unknown"
    account_evidence_code: str = ""
    account_evidence_message: str = ""
    account_evidence_at: str = ""
    probe_state: str = "never_checked"
    probe_checked_at: str = ""
    probe_transport: str = ""
    probe_error_code: str = ""
    probe_error_message: str = ""
    derived_state: str = "unknown"
    availability_state: str = "unknown"
    updated_at: datetime = Field(default_factory=_utcnow)


class ChatGPTSubscriptionStateModel(SQLModel, table=True):
    """Current and last-confirmed subscription evidence, independent of auth expiry."""

    __tablename__ = "chatgpt_subscription_states"

    account_id: int = Field(primary_key=True)
    current_plan: str = "unknown"
    current_active_until: str = ""
    current_checked_at: str = ""
    current_state: str = "not_checked"
    last_confirmed_plan: str = ""
    last_confirmed_active_until: str = ""
    last_confirmed_at: str = ""
    workspace_plan_type: str = ""
    source: str = ""
    refresh_state: str = "not_checked"
    updated_at: datetime = Field(default_factory=_utcnow)


class ChatGPTAuthProbeEventModel(SQLModel, table=True):
    """Redacted authentication evidence history; never stores token material."""

    __tablename__ = "chatgpt_auth_probe_events"
    __table_args__ = (
        Index("idx_chatgpt_auth_probe_events_account_created", "account_id", "created_at"),
        Index("idx_chatgpt_auth_probe_events_probe_id", "probe_id"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    account_id: int = Field(index=True)
    probe_id: str = Field(default="", index=True)
    material_revision: str = ""
    operation: str = "local_status_probe"
    started_at: str = ""
    finished_at: str = ""
    refresh_attempted: bool = False
    refresh_result: str = "not_attempted"
    refresh_http_status: int = 0
    refresh_error_code: str = ""
    refresh_error_message: str = ""
    access_probe_source: str = ""
    access_probe_state: str = "unknown"
    access_probe_http_status: int = 0
    access_probe_error_code: str = ""
    access_probe_message: str = ""
    account_evidence_state: str = "unknown"
    account_evidence_code: str = ""
    subscription_plan: str = "unknown"
    subscription_active_until: str = ""
    payload_json: str = "{}"
    created_at: datetime = Field(default_factory=_utcnow)


class PaymentLinkGenerationModel(SQLModel, table=True):
    """Durable, source-neutral history for generated ChatGPT payment links.

    The table deliberately stores only redacted upstream identifiers and the
    returned link.  Access tokens, proxies and long-link admin configuration
    secrets remain outside the account database.
    """

    __tablename__ = "payment_link_generations"
    __table_args__ = (UniqueConstraint("request_id", name="uq_payment_link_generations_request_id"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    account_id: int = Field(index=True)
    # The numeric account id is reusable in SQLite.  Keep the immutable account
    # identity alongside it so an old async result cannot attach to a later row
    # that happens to receive the same id.
    account_email: str = Field(default="", index=True)
    account_created_at: str = ""
    task_id: str = Field(default="", index=True)
    request_id: str = Field(default="", index=True)
    remote_batch_id: str = Field(default="", index=True)
    remote_job_id: str = Field(default="", index=True)
    profile_hash: str = Field(default="", index=True)
    link_type: str = Field(default="", index=True)
    generation_kind: str = Field(default="plus_checkout", index=True)
    variant_key: str = Field(default="", index=True)
    status: str = Field(default="submitting", index=True)
    url: str = ""
    submitted_at: str = ""
    started_at: str = ""
    generated_at: str = ""
    persisted_at: str = ""
    sanitized_error: str = ""
    result_json: str = "{}"
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    def get_result(self) -> dict:
        try:
            value = json.loads(self.result_json or "{}")
        except (TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def set_result(self, value: dict | None) -> None:
        self.result_json = json.dumps(value if isinstance(value, dict) else {}, ensure_ascii=False)


class RegistrationPaypalPaymentFollowupModel(SQLModel, table=True):
    """Durable state machine for registration PayPal payment reconciliation."""

    __tablename__ = "registration_paypal_payment_followups"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "account_created_at",
            "batch_id",
            "item_id",
            name="uq_registration_paypal_followup_identity",
        ),
        Index(
            "idx_registration_paypal_followups_due",
            "state",
            "next_poll_at",
        ),
        Index(
            "idx_registration_paypal_followups_task",
            "task_id",
            "updated_at",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: str = Field(default="", index=True, max_length=160)
    account_id: int = Field(index=True)
    account_email: str = Field(default="", index=True, max_length=320)
    account_created_at: str = Field(default="", max_length=64)
    batch_id: str = Field(default="", index=True, max_length=128)
    item_id: str = Field(default="", index=True, max_length=128)
    state: str = Field(default="payment_pending", index=True, max_length=64)
    remote_status: str = Field(default="", max_length=64)
    remote_stage: str = Field(default="", max_length=500)
    payment_result: str = Field(default="", max_length=500)
    payment_result_code: str = Field(default="", max_length=128)
    remote_job_id: str = Field(default="", max_length=128)
    settlement_status: str = Field(default="", max_length=128)
    paypal_authorized: bool = False
    merchant_redirect_succeeded: Optional[bool] = None
    entitlement_verified: Optional[bool] = None
    attempt_count: int = 0
    next_poll_at: float = Field(default=0, index=True)
    deadline_at: float = Field(default=0, index=True)
    relogin_attempt_count: int = 0
    local_refresh_generation: str = ""
    last_error: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class RegistrationPaypalPaymentEventModel(SQLModel, table=True):
    """Append-only, credential-free timeline for registration PayPal work."""

    __tablename__ = "registration_paypal_payment_events"
    __table_args__ = (
        UniqueConstraint(
            "idempotency_key",
            name="uq_registration_paypal_payment_event_key",
        ),
        Index(
            "idx_registration_paypal_payment_events_task_created",
            "task_id",
            "created_at",
        ),
        Index(
            "idx_registration_paypal_payment_events_account_created",
            "account_id",
            "created_at",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: str = Field(default="", index=True, max_length=160)
    account_id: int = Field(default=0, index=True)
    account_email_masked: str = Field(default="", max_length=160)
    account_created_at: str = Field(default="", max_length=64)
    stage: str = Field(default="", index=True, max_length=64)
    level: str = Field(default="info", max_length=16)
    message: str = Field(default="", max_length=1000)
    safe_metadata_json: str = "{}"
    idempotency_key: str = Field(default="", index=True, max_length=320)
    created_at: datetime = Field(default_factory=_utcnow)

    def get_metadata(self) -> dict:
        try:
            value = json.loads(self.safe_metadata_json or "{}")
        except (TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def set_metadata(self, value: dict | None) -> None:
        self.safe_metadata_json = json.dumps(
            value if isinstance(value, dict) else {},
            ensure_ascii=False,
            separators=(",", ":"),
        )


class AdminAuthSessionModel(SQLModel, table=True):
    """Server-side state for one administrator JWT session."""

    __tablename__ = "admin_auth_sessions"
    __table_args__ = (
        Index(
            "idx_admin_auth_sessions_instance_active",
            "instance_id",
            "revoked_at",
            "expires_at",
        ),
    )

    jti: str = Field(primary_key=True, max_length=64)
    instance_id: str = Field(index=True, max_length=128)
    auth_version: int = Field(default=1, index=True)
    issued_at: int = Field(default=0, index=True)
    last_seen_at: int = Field(default=0, index=True)
    expires_at: int = Field(default=0, index=True)
    absolute_expires_at: int = Field(default=0, index=True)
    revoked_at: int = Field(default=0, index=True)
    revoke_reason: str = Field(default="", max_length=128)
    client_ip: str = Field(default="", max_length=128)
    user_agent: str = Field(default="", max_length=512)


class AdminAuthAuditModel(SQLModel, table=True):
    """Credential-free administrator authentication audit trail."""

    __tablename__ = "admin_auth_audit"
    __table_args__ = (
        Index(
            "idx_admin_auth_audit_lookup",
            "instance_id",
            "client_ip",
            "stage",
            "event_at",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    instance_id: str = Field(index=True, max_length=128)
    event_at: int = Field(default=0, index=True)
    client_ip: str = Field(default="", index=True, max_length=128)
    user_agent: str = Field(default="", max_length=512)
    stage: str = Field(default="", index=True, max_length=64)
    outcome: str = Field(default="", index=True, max_length=32)
    reason: str = Field(default="", max_length=128)
    jti: str = Field(default="", index=True, max_length=64)


class AdminAuthThrottleModel(SQLModel, table=True):
    """Persistent per-instance/IP/stage authentication cooldown state."""

    __tablename__ = "admin_auth_throttles"

    bucket_key: str = Field(primary_key=True, max_length=64)
    instance_id: str = Field(index=True, max_length=128)
    client_ip: str = Field(default="", index=True, max_length=128)
    stage: str = Field(default="", index=True, max_length=64)
    failure_count: int = 0
    window_started_at: int = 0
    blocked_until: int = Field(default=0, index=True)
    updated_at: int = Field(default=0, index=True)


def _has_non_empty_text(value: Any) -> bool:
    return bool(str(value or "").strip())


def _preserve_chatgpt_web_session_material(incoming_extra: dict, existing_extra: dict) -> dict:
    """保存 ChatGPT 账号时，禁止空 Web 会话材料覆盖已有非空值。"""
    if not isinstance(incoming_extra, dict):
        return {}
    if not isinstance(existing_extra, dict):
        return incoming_extra
    for key in ("session_token", "cookies", "cookie_header"):
        if not _has_non_empty_text(incoming_extra.get(key)) and _has_non_empty_text(existing_extra.get(key)):
            incoming_extra[key] = existing_extra.get(key)
    return incoming_extra


def _is_chatgpt_registered_auth_pending(account: Any, extra: dict) -> bool:
    if not isinstance(extra, dict) or not extra.get("registered_auth_pending"):
        return False
    access_token = str(
        extra.get("access_token")
        or extra.get("accessToken")
        or getattr(account, "token", "")
        or ""
    ).strip()
    return not access_token


def _has_chatgpt_auth_material(extra: dict, token: Any = "") -> bool:
    if not isinstance(extra, dict):
        extra = {}
    return bool(
        str(
            extra.get("access_token")
            or extra.get("accessToken")
            or extra.get("refresh_token")
            or extra.get("refreshToken")
            or token
            or ""
        ).strip()
    )


def _record_chatgpt_pending_attempt(existing_extra: dict, incoming_extra: dict) -> dict:
    """Retain a pending registration audit without replacing valid credentials."""
    merged = dict(existing_extra or {})
    event = {
        "seen_at": _utcnow().isoformat(),
        "source": str(incoming_extra.get("chatgpt_token_source") or "registered_auth_pending"),
        "error": str(
            incoming_extra.get("registration_full_auth_error")
            or incoming_extra.get("registration_access_token_partial_reason")
            or ""
        ),
        "requested_executor_type": str(incoming_extra.get("requested_executor_type") or ""),
        "effective_executor_type": str(incoming_extra.get("effective_executor_type") or ""),
        "registration_transport": str(incoming_extra.get("chatgpt_registration_transport") or ""),
    }
    for key in (
        "chatgpt_registration_context",
        "chatgpt_browser_runtime_profile",
        "chatgpt_mailbox_state",
    ):
        value = incoming_extra.get(key)
        if value not in (None, "", {}, []):
            event[key] = value
    event = {key: value for key, value in event.items() if value not in (None, "")}
    history = merged.get("chatgpt_registered_auth_pending_history")
    if not isinstance(history, list):
        history = []
    history.append(event)
    merged["chatgpt_registered_auth_pending_history"] = history[-20:]
    merged["chatgpt_last_registered_auth_pending"] = event
    return merged


def _preserve_chatgpt_account_browser_fingerprint(incoming_extra: dict, existing_extra: dict | None = None) -> dict:
    """保存 ChatGPT 账号时，保持账号级浏览器指纹稳定。"""
    if not isinstance(incoming_extra, dict):
        return {}
    try:
        from services.chatgpt_core.account_fingerprint import (
            merge_preserving_account_browser_fingerprint,
            persist_account_browser_fingerprint,
        )

        if isinstance(existing_extra, dict) and existing_extra:
            return merge_preserving_account_browser_fingerprint(
                incoming_extra,
                existing_extra,
                source="save_account",
            )
        return persist_account_browser_fingerprint(
            incoming_extra,
            source="save_account",
            overwrite=False,
        )
    except Exception:
        return incoming_extra


class AccountListStateModel(SQLModel, table=True):
    """List-time derived state cache for account filters/sorts.

    Keep this table free of secrets.  It is a denormalized SQL filter surface
    derived from ``accounts.extra_json`` and non-secret account columns.
    """

    __tablename__ = "account_list_state"

    account_id: int = Field(primary_key=True, foreign_key="accounts.id")
    platform: str = Field(default="", index=True)
    manually_used: bool = Field(default=False, index=True)
    auth_type: str = Field(default="unknown", index=True)
    phone_binding_state: str = Field(default="unknown", index=True)
    payment_link_platform: str = Field(default="none", index=True)
    payment_link_generated: bool = Field(default=False, index=True)
    checkout_link_type: str = Field(default="none", index=True)
    auth_level: str = Field(default="", index=True)
    subscription_type: str = Field(default="unknown", index=True)
    account_validity: str = Field(default="valid", index=True)
    sub2api_state: str = Field(default="unknown", index=True)
    oaipay_state: str = Field(default="unknown", index=True)
    idea_submit_state: str = Field(default="available", index=True)
    submit_state: str = Field(default="available", index=True)
    zero_amount_eligibility_state: str = Field(default="unknown", index=True)
    zero_amount_eligibility_display_state: str = Field(default="unknown", index=True)
    gcash_payment_method_state: str = Field(default="unknown", index=True)
    has_submitted: bool = Field(default=False, index=True)
    revival_state: str = Field(default="none", index=True)
    revival_kind: str = Field(default="none", index=True)
    subscription_active_until: str = ""
    subscription_active_until_ts: Optional[float] = Field(default=None, index=True)
    source_updated_at: str = ""
    refreshed_at: str = ""
    derivation_version: str = Field(default="", index=True)


class ChatGPTLocalStatusRefreshJobModel(SQLModel, table=True):
    """Durable, credential-free queue state for ChatGPT local-status refreshes.

    The account row remains the source of credentials and probe evidence.  This
    table only records scheduling/retry state, so an interrupted daemon thread
    can be resumed after a process restart without persisting proxy URLs or
    tokens.
    """

    __tablename__ = "chatgpt_local_status_refresh_jobs"

    account_id: int = Field(primary_key=True, foreign_key="accounts.id")
    account_email: str = Field(default="", index=True)
    account_created_at: str = ""
    auth_revision_hash: str = ""
    generation: int = 1
    state: str = "pending"
    reason: str = ""
    attempt_count: int = 0
    max_attempts: int = 3
    requested_at_ts: float = 0
    next_attempt_at_ts: float = 0
    started_at_ts: float = 0
    completed_at_ts: float = 0
    updated_at_ts: float = 0
    last_outcome: str = ""
    last_error: str = ""


class AccountFixedGroupModel(SQLModel, table=True):
    """Instance-local fixed account group attached to one dynamic preset."""

    __tablename__ = "account_fixed_groups"
    __table_args__ = (
        Index("idx_account_fixed_groups_parent", "parent_preset_id", "pinned", "updated_at"),
    )

    id: str = Field(primary_key=True, max_length=80)
    parent_preset_id: str = Field(index=True, max_length=80)
    name: str = Field(max_length=80)
    description: str = Field(default="", max_length=240)
    pinned: bool = Field(default=True, index=True)
    revision: int = Field(default=1)
    created_at: str = ""
    updated_at: str = ""


class AccountFixedGroupMemberModel(SQLModel, table=True):
    """Exclusive fixed-group ownership for one stable account identity."""

    __tablename__ = "account_fixed_group_members"

    account_id: int = Field(primary_key=True, foreign_key="accounts.id")
    fixed_group_id: str = Field(foreign_key="account_fixed_groups.id", index=True, max_length=80)
    account_email: str = Field(default="", max_length=320)
    account_created_at: str = Field(default="", max_length=80)
    assigned_at: str = ""


class ExternalSubscriptionClaimModel(SQLModel, table=True):
    __tablename__ = "external_subscription_claims"

    id: Optional[int] = Field(default=None, primary_key=True)
    claim_id: str = Field(index=True, sa_column_kwargs={"unique": True})
    account_id: int = Field(index=True)
    email: str = Field(default="", index=True)
    consumer: str = ""
    status: str = Field(default="prechecking", index=True)
    payment_link: str = ""
    plan: str = "plus"
    country: str = ""
    currency: str = ""
    precheck_expires_at: str = ""
    lease_expires_at: str = ""
    verify_after_at: str = ""
    claimed_at: str = ""
    prechecked_at: str = ""
    result_written_at: str = ""
    paid_at: str = ""
    failed_at: str = ""
    released_at: str = ""
    provider: str = ""
    external_payment_id: str = ""
    message: str = ""
    error_code: str = ""
    last_error: str = ""
    details_json: str = "{}"
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    def get_details(self) -> dict:
        try:
            return json.loads(self.details_json or "{}")
        except Exception:
            return {}

    def set_details(self, d: dict):
        self.details_json = json.dumps(d if isinstance(d, dict) else {}, ensure_ascii=False)


class ExternalAccessTokenClaimModel(SQLModel, table=True):
    __tablename__ = "external_access_token_claims"

    id: Optional[int] = Field(default=None, primary_key=True)
    claim_id: str = Field(index=True, sa_column_kwargs={"unique": True})
    account_id: int = Field(index=True)
    email: str = Field(default="", index=True)
    consumer: str = ""
    status: str = Field(default="prechecking", index=True)
    token_source: str = ""
    token_fingerprint: str = ""
    auth_state: str = ""
    subscription_plan: str = ""
    subscription_checked_at: str = ""
    lease_expires_at: str = ""
    claimed_at: str = ""
    prechecked_at: str = ""
    paid_at: str = ""
    failed_at: str = ""
    released_at: str = ""
    result_written_at: str = ""
    provider: str = ""
    external_payment_id: str = ""
    message: str = ""
    error_code: str = ""
    last_error: str = ""
    details_json: str = "{}"
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    def get_details(self) -> dict:
        try:
            return json.loads(self.details_json or "{}")
        except Exception:
            return {}

    def set_details(self, d: dict):
        self.details_json = json.dumps(d if isinstance(d, dict) else {}, ensure_ascii=False)


class TaskLog(SQLModel, table=True):
    __tablename__ = "task_logs"

    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: str = Field(default="", index=True)
    platform: str
    email: str
    status: str        # success | failed
    error: str = ""
    detail_json: str = "{}"
    created_at: datetime = Field(default_factory=_utcnow)


class TaskLogSummaryModel(SQLModel, table=True):
    """Small list projection kept separate from potentially huge task details."""

    __tablename__ = "task_log_summaries"
    __table_args__ = (
        Index(
            "idx_task_log_summaries_platform_log",
            "platform",
            "log_id",
            "group_key",
        ),
        Index(
            "idx_task_log_summaries_platform_source_log",
            "platform",
            "source",
            "log_id",
            "group_key",
        ),
    )

    log_id: int = Field(primary_key=True, foreign_key="task_logs.id")
    task_id: str = ""
    group_key: str
    platform: str = ""
    source: str = ""
    summary_json: str = "{}"


class RegistrationDiagnosticArtifactModel(SQLModel, table=True):
    """Filesystem-backed registration diagnostic bundle index.

    Trace/HAR/video payloads stay under the instance runtime mount.  SQLite
    only owns searchable lifecycle metadata so large diagnostic blobs never
    amplify the account database or its WAL.
    """

    __tablename__ = "registration_diagnostic_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "attempt_id",
            name="uq_registration_diagnostic_task_attempt",
        ),
        Index(
            "idx_registration_diagnostic_task_created",
            "task_id",
            "created_at",
        ),
        Index(
            "idx_registration_diagnostic_retention",
            "pinned",
            "expires_at",
            "created_at",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: str = Field(index=True, max_length=128)
    attempt_id: int = Field(index=True)
    attempt_number: int = 0
    mode: str = Field(default="smart", index=True, max_length=16)
    outcome: str = Field(default="recording", index=True, max_length=32)
    failure_code: str = Field(default="", index=True, max_length=96)
    failure_stage: str = Field(default="", index=True, max_length=64)
    status: str = Field(default="recording", index=True, max_length=32)
    email_masked: str = Field(default="", max_length=320)
    relative_path: str = Field(default="", max_length=512)
    size_bytes: int = 0
    checksum: str = Field(default="", max_length=64)
    pinned: bool = Field(default=False, index=True)
    summary_json: str = "{}"
    truncation_reason: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    finished_at: Optional[datetime] = None
    expires_at: Optional[datetime] = Field(default=None, index=True)

    def get_summary(self) -> dict[str, Any]:
        try:
            value = json.loads(self.summary_json or "{}")
        except (TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def set_summary(self, value: dict[str, Any] | None) -> None:
        self.summary_json = json.dumps(
            value if isinstance(value, dict) else {},
            ensure_ascii=False,
        )


class OutlookAccountModel(SQLModel, table=True):
    __tablename__ = "outlook_accounts"

    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(index=True, sa_column_kwargs={"unique": True})
    password: str
    client_id: str = ""
    refresh_token: str = ""
    enabled: bool = True
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    last_used: Optional[datetime] = None


class ProxyModel(SQLModel, table=True):
    __tablename__ = "proxies"

    id: Optional[int] = Field(default=None, primary_key=True)
    url: str = Field(unique=True)
    region: str = ""
    proxy_group: str = ""
    desired_country_code: str = ""
    provider: str = ""
    note: str = ""
    success_count: int = 0
    fail_count: int = 0
    homepage_success_count: int = 0
    homepage_fail_count: int = 0
    homepage_consecutive_failures: int = 0
    homepage_last_error: str = ""
    homepage_last_status_code: int = 0
    is_active: bool = True
    last_checked: Optional[datetime] = None
    homepage_last_checked: Optional[datetime] = None
    homepage_circuit_open_until: Optional[datetime] = None
    scheme: str = ""
    host: str = ""
    port: int = 0
    exit_ip: str = ""
    exit_country_code: str = ""
    exit_country_name: str = ""
    exit_region_name: str = ""
    exit_city: str = ""
    exit_asn: str = ""
    exit_isp: str = ""
    geo_source: str = ""
    geo_checked_at: Optional[datetime] = None
    scan_status: str = "unchecked"
    last_scan_at: Optional[datetime] = None
    last_scan_duration_ms: int = 0
    last_latency_ms: int = 0
    last_error_code: str = ""
    last_error: str = ""
    chatgpt_status: str = "unchecked"
    chatgpt_status_code: int = 0
    chatgpt_latency_ms: int = 0
    chatgpt_last_checked_at: Optional[datetime] = None
    chatgpt_last_error: str = ""
    health_score: float = 0.0
    consecutive_failures: int = 0
    cooldown_until: Optional[datetime] = None
    last_probe_json: str = "{}"


class IcloudHmeAliasModel(SQLModel, table=True):
    __tablename__ = "icloud_hme_alias"

    id: Optional[int] = Field(default=None, primary_key=True)
    anonymous_id: str = Field(index=True, sa_column_kwargs={"unique": True})
    hme: str = Field(index=True)
    label: str = ""
    note: str = ""
    forward_to: str = ""
    enabled: bool = False
    created_source: str = "unknown"
    record_source: str = "live_create"
    purpose: str = "chatgpt_register"
    bound_service: str = "chatgpt"
    bound_account_email: str = Field(default="", index=True)
    bound_account_ref: str = ""
    task_id: str = ""
    status: str = Field(default="reserved", index=True)
    use_count: int = 0
    first_claimed_at: str = ""
    last_claimed_at: str = ""
    last_synced_at: str = ""
    last_otp_at: str = ""
    last_error: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


def save_account(account) -> 'AccountModel':
    """Store the current account in one canonical row and retain legacy variants."""
    def _schedule_local_status_refresh(saved: 'AccountModel', *, reason: str) -> None:
        try:
            if str(getattr(saved, "platform", "") or "").strip().lower() != "chatgpt":
                return
            saved_extra = saved.get_extra()
            has_auth = bool(
                str(
                    saved_extra.get("refresh_token")
                    or saved_extra.get("refreshToken")
                    or saved_extra.get("access_token")
                    or saved_extra.get("accessToken")
                    or saved_extra.get("webAccessToken")
                    or getattr(saved, "token", "")
                    or ""
                ).strip()
            )
            if not has_auth:
                return
            from services.chatgpt_core.local_status_refresh import schedule_chatgpt_local_status_refresh_for_account_id

            schedule_chatgpt_local_status_refresh_for_account_id(saved.id, reason=reason, delay_seconds=2.0)
        except Exception:
            pass

    with Session(engine) as session:
        extra = dict(account.extra or {}) if isinstance(account.extra, dict) else {}
        if str(account.platform or "").strip().lower() == "chatgpt":
            extra = _preserve_chatgpt_account_browser_fingerprint(extra)
        candidates = session.exec(
            select(AccountModel)
            .where(AccountModel.platform == account.platform)
            .where(AccountModel.email == account.email)
            .order_by(AccountModel.updated_at.desc(), AccountModel.id.desc())
        ).all()

        existing = None
        for candidate in candidates:
            try:
                candidate_extra = json.loads(candidate.extra_json or "{}")
            except Exception:
                candidate_extra = {}
            variant_key = str(candidate_extra.get("chatgpt_workspace_variant_key") or "").strip()
            workspace_scope = str(candidate_extra.get("chatgpt_workspace_scope") or "").strip().lower()
            if workspace_scope in {"free", "personal", "personal_free"}:
                existing = candidate
                break
            if variant_key or workspace_scope:
                continue
            existing = candidate
            break

        if existing:
            preserve_existing_auth = False
            auth_material_changed = False
            if str(account.platform or "").strip().lower() == "chatgpt":
                try:
                    existing_extra = json.loads(existing.extra_json or "{}")
                except Exception:
                    existing_extra = {}
                preserve_existing_auth = (
                    _is_chatgpt_registered_auth_pending(account, extra)
                    and _has_chatgpt_auth_material(existing_extra, existing.token)
                )
                if preserve_existing_auth:
                    extra = _record_chatgpt_pending_attempt(existing_extra, extra)
                else:
                    extra = _preserve_chatgpt_web_session_material(extra, existing_extra)
                    extra = _preserve_chatgpt_account_browser_fingerprint(extra, existing_extra)
                    auth_material_changed = any(
                        str(value or "").strip() != str(previous or "").strip()
                        for value, previous in (
                            (account.token, existing.token),
                            (extra.get("access_token"), existing_extra.get("access_token")),
                            (extra.get("refresh_token"), existing_extra.get("refresh_token")),
                            (extra.get("id_token"), existing_extra.get("id_token")),
                            (extra.get("account_id"), existing_extra.get("account_id")),
                        )
                    )
            if not preserve_existing_auth:
                existing.password = account.password
                existing.user_id = account.user_id or ""
                existing.region = account.region or ""
                existing.token = account.token or ""
                existing.status = account.status.value
            existing.extra_json = json.dumps(extra, ensure_ascii=False)
            existing.cashier_url = extra.get("cashier_url", "")
            if auth_material_changed:
                if "chatgpt_local" not in extra and isinstance(existing_extra.get("chatgpt_local"), dict):
                    extra["chatgpt_local"] = existing_extra["chatgpt_local"]
                    existing.extra_json = json.dumps(extra, ensure_ascii=False)
                from services.chatgpt_core.local_status_refresh import prepare_chatgpt_account_for_local_status_refresh

                prepare_chatgpt_account_for_local_status_refresh(
                    existing,
                    reason="save_account:auth_material_changed",
                )
            existing.updated_at = _utcnow()
            session.add(existing)
            session.commit()
            session.refresh(existing)
            _schedule_local_status_refresh(existing, reason="save_account:update")
            return existing
        m = AccountModel(
            platform=account.platform,
            email=account.email,
            password=account.password,
            user_id=account.user_id or "",
            region=account.region or "",
            token=account.token or "",
            status=account.status.value,
            extra_json=json.dumps(extra, ensure_ascii=False),
            cashier_url=extra.get("cashier_url", ""),
        )
        session.add(m)
        session.commit()
        session.refresh(m)
        _schedule_local_status_refresh(m, reason="save_account:create")
        return m


class PhonePoolModel(SQLModel, table=True):
    """ChatGPT relay 自有手机号池。"""

    __tablename__ = "phone_pool"

    id: Optional[int] = Field(default=None, primary_key=True)
    phone_e164: str = Field(index=True, sa_column_kwargs={"unique": True})
    api_url: str = ""
    api_host: str = Field(default="", index=True)
    api_expired_date: str = ""
    api_expiry_checked_at: str = ""
    api_expiry_status: str = ""
    api_expiry_error: str = ""
    label: str = ""
    status: str = Field(default="active", index=True)
    bound_count: int = 0
    bound_account_emails_json: str = "[]"
    max_accounts: int = 3
    success_count: int = 0
    fail_count: int = 0
    last_error_code: str = ""
    last_error_message: str = ""
    cooldown_until: str = ""
    last_used_at: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class PhonePrefixStateModel(SQLModel, table=True):
    """手机号号段状态。

    这里记录的是“号段是否适合某类任务”的派生状态，不改变 phone_pool
    中任何单个手机号自身的 status / bound_count / fail_count。
    """

    __tablename__ = "phone_prefix_state"
    __table_args__ = (UniqueConstraint("purpose", "prefix", name="uq_phone_prefix_state_purpose_prefix"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    purpose: str = Field(default="phone_signup", index=True)
    prefix: str = Field(index=True)
    status: str = Field(default="untested", index=True)
    success_count: int = 0
    failure_count: int = 0
    last_success_phone: str = ""
    last_failure_phone: str = ""
    last_error_code: str = ""
    last_error_message: str = ""
    last_seen_at: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class BaxiGptCdkPoolModel(SQLModel, table=True):
    """BaxiGPT 卡密库存与提交状态。"""

    __tablename__ = "baxigpt_cdk_pool"

    id: Optional[int] = Field(default=None, primary_key=True)
    code_value: str
    code_hash: str = Field(index=True, sa_column_kwargs={"unique": True})
    code_masked: str = ""
    label: str = ""
    status: str = Field(default="available", index=True)
    bound_account_id: int = Field(default=0, index=True)
    bound_account_email: str = Field(default="", index=True)
    bound_at: str = ""
    task_id: str = Field(default="", index=True)
    order_id: str = Field(default="", index=True)
    display_id: str = ""
    remote_email: str = ""
    upstream_status: str = ""
    code_info_remaining: int = 0
    code_info_total: int = 0
    submit_response_json: str = "{}"
    last_status_response_json: str = "{}"
    last_query_response_json: str = "{}"
    last_error_code: str = ""
    last_error_message: str = ""
    submitted_at: str = ""
    paid_at: str = ""
    last_checked_at: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)




class IcloudHmeRecheckQueueModel(SQLModel, table=True):
    """iCloud HME 全量复测队列。只记录测活进度，不触发 Apple 端删除。"""

    __tablename__ = "icloud_hme_recheck_queue"

    id: Optional[int] = Field(default=None, primary_key=True)
    campaign_id: str = Field(index=True)
    anonymous_id: str = Field(index=True)
    hme: str = Field(index=True)
    account_id: int = Field(default=0, index=True)
    account_email: str = Field(default="", index=True)
    source_type: str = Field(default="icloud_hme", index=True)
    status: str = Field(default="pending", index=True)
    result_code: str = Field(default="", index=True)
    result_message: str = ""
    saved_account_id: int = 0
    access_token_saved: bool = False
    delete_candidate: bool = Field(default=False, index=True)
    delete_candidate_reason: str = ""
    apple_delete_status: str = "not_requested"
    attempt_count: int = 0
    last_task_id: str = Field(default="", index=True)
    last_error: str = ""
    details_json: str = "{}"
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    checked_at: str = ""
    started_at: str = ""

    def get_details(self) -> dict:
        try:
            data = json.loads(self.details_json or "{}")
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def set_details(self, d: dict):
        self.details_json = json.dumps(d if isinstance(d, dict) else {}, ensure_ascii=False)


class DeliverySkuModel(SQLModel, table=True):
    """对外交付卡密 SKU。"""

    __tablename__ = "delivery_skus"

    id: Optional[int] = Field(default=None, primary_key=True)
    code: str = Field(index=True, sa_column_kwargs={"unique": True})
    name: str = ""
    platform: str = Field(default="chatgpt", index=True)
    code_prefix: str = ""
    delivery_profile: str = "chatgpt_basic"
    sort_policy: str = "earliest_expiry"
    eligible_rule_json: str = "{}"
    allow_refetch: bool = True
    max_refetch_count: int = 0
    enabled: bool = True
    note: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class DeliveryCardBatchModel(SQLModel, table=True):
    """对外交付卡密批次。"""

    __tablename__ = "delivery_card_batches"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = ""
    sku_code: str = Field(index=True)
    platform: str = Field(default="chatgpt", index=True)
    code_prefix: str = ""
    total_count: int = 0
    strict_stock_check: bool = True
    expires_at: str = ""
    note: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class DeliveryCardModel(SQLModel, table=True):
    """对外 API 兑换交付卡密。"""

    __tablename__ = "delivery_cards"

    id: Optional[int] = Field(default=None, primary_key=True)
    batch_id: int = Field(default=0, index=True)
    sku_code: str = Field(index=True)
    platform: str = Field(default="chatgpt", index=True)
    code_hash: str = Field(index=True, sa_column_kwargs={"unique": True})
    code_mask: str = ""
    code_prefix: str = ""
    status: str = Field(default="unused", index=True)
    assigned_account_id: int = Field(default=0, index=True)
    assigned_email_snapshot: str = Field(default="", index=True)
    assigned_at: str = ""
    redeem_count: int = 0
    first_redeemed_at: str = ""
    last_redeemed_at: str = ""
    first_redeem_ip: str = ""
    last_redeem_ip: str = ""
    first_consumer: str = ""
    last_consumer: str = ""
    delivery_payload_json: str = "{}"
    expires_at: str = ""
    disabled_reason: str = ""
    last_failure_code: str = ""
    last_failure_at: str = ""
    note: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class DeliveryCardEventModel(SQLModel, table=True):
    """交付卡密业务事件。"""

    __tablename__ = "delivery_card_events"

    id: Optional[int] = Field(default=None, primary_key=True)
    card_id: int = Field(default=0, index=True)
    batch_id: int = Field(default=0, index=True)
    sku_code: str = Field(default="", index=True)
    account_id: int = Field(default=0, index=True)
    event_type: str = Field(index=True)
    result: str = Field(index=True)
    failure_code: str = Field(default="", index=True)
    delivery_sequence: int = 0
    request_id: str = Field(default="", index=True)
    idempotency_key: str = Field(default="", index=True)
    consumer: str = Field(default="", index=True)
    client_ip: str = ""
    user_agent: str = ""
    api_token_id: str = ""
    response_profile: str = ""
    message: str = ""
    detail_json: str = "{}"
    created_at: datetime = Field(default_factory=_utcnow)


class DeliveryRedeemApiLogModel(SQLModel, table=True):
    """兑换 API 每次调用的独立日志。"""

    __tablename__ = "delivery_redeem_api_logs"

    id: Optional[int] = Field(default=None, primary_key=True)
    trace_id: str = Field(index=True)
    request_id: str = Field(default="", index=True)
    idempotency_key: str = Field(default="", index=True)
    consumer: str = Field(default="", index=True)
    api_token_id: str = Field(default="", index=True)
    client_ip: str = ""
    user_agent: str = ""
    code_prefix: str = Field(default="", index=True)
    code_mask: str = ""
    code_hash_prefix: str = ""
    card_id: int = Field(default=0, index=True)
    batch_id: int = Field(default=0, index=True)
    sku_code: str = Field(default="", index=True)
    assigned_account_id: int = Field(default=0, index=True)
    assigned_account_email: str = Field(default="", index=True)
    action: str = Field(default="", index=True)
    result: str = Field(default="", index=True)
    error_code: str = Field(default="", index=True)
    redeem_index: int = 0
    first_redeem: bool = False
    idempotent_replay: bool = False
    duplicate_check_status: str = Field(default="", index=True)
    duplicate_check_message: str = ""
    duplicate_card_ids_json: str = "[]"
    duplicate_event_ids_json: str = "[]"
    duplicate_api_log_ids_json: str = "[]"
    stock_before_json: str = "{}"
    decision_json: str = "{}"
    response_summary_json: str = "{}"
    message: str = ""
    duration_ms: int = 0
    created_at: datetime = Field(default_factory=_utcnow)


def _ensure_proxy_schema() -> None:
    required_columns = {
        "proxy_group": "TEXT NOT NULL DEFAULT ''",
        "desired_country_code": "TEXT NOT NULL DEFAULT ''",
        "provider": "TEXT NOT NULL DEFAULT ''",
        "note": "TEXT NOT NULL DEFAULT ''",
        "homepage_success_count": "INTEGER NOT NULL DEFAULT 0",
        "homepage_fail_count": "INTEGER NOT NULL DEFAULT 0",
        "homepage_consecutive_failures": "INTEGER NOT NULL DEFAULT 0",
        "homepage_last_error": "TEXT NOT NULL DEFAULT ''",
        "homepage_last_status_code": "INTEGER NOT NULL DEFAULT 0",
        "homepage_last_checked": "TIMESTAMP NULL",
        "homepage_circuit_open_until": "TIMESTAMP NULL",
        "scheme": "TEXT NOT NULL DEFAULT ''",
        "host": "TEXT NOT NULL DEFAULT ''",
        "port": "INTEGER NOT NULL DEFAULT 0",
        "exit_ip": "TEXT NOT NULL DEFAULT ''",
        "exit_country_code": "TEXT NOT NULL DEFAULT ''",
        "exit_country_name": "TEXT NOT NULL DEFAULT ''",
        "exit_region_name": "TEXT NOT NULL DEFAULT ''",
        "exit_city": "TEXT NOT NULL DEFAULT ''",
        "exit_asn": "TEXT NOT NULL DEFAULT ''",
        "exit_isp": "TEXT NOT NULL DEFAULT ''",
        "geo_source": "TEXT NOT NULL DEFAULT ''",
        "geo_checked_at": "TIMESTAMP NULL",
        "scan_status": "TEXT NOT NULL DEFAULT 'unchecked'",
        "last_scan_at": "TIMESTAMP NULL",
        "last_scan_duration_ms": "INTEGER NOT NULL DEFAULT 0",
        "last_latency_ms": "INTEGER NOT NULL DEFAULT 0",
        "last_error_code": "TEXT NOT NULL DEFAULT ''",
        "last_error": "TEXT NOT NULL DEFAULT ''",
        "chatgpt_status": "TEXT NOT NULL DEFAULT 'unchecked'",
        "chatgpt_status_code": "INTEGER NOT NULL DEFAULT 0",
        "chatgpt_latency_ms": "INTEGER NOT NULL DEFAULT 0",
        "chatgpt_last_checked_at": "TIMESTAMP NULL",
        "chatgpt_last_error": "TEXT NOT NULL DEFAULT ''",
        "health_score": "REAL NOT NULL DEFAULT 0",
        "consecutive_failures": "INTEGER NOT NULL DEFAULT 0",
        "cooldown_until": "TIMESTAMP NULL",
        "last_probe_json": "TEXT NOT NULL DEFAULT '{}'",
    }

    with engine.begin() as conn:
        existing_columns = {
            str(row[1])
            for row in conn.exec_driver_sql("PRAGMA table_info(proxies)").fetchall()
        }
        for column_name, ddl in required_columns.items():
            if column_name in existing_columns:
                continue
            conn.exec_driver_sql(
                f"ALTER TABLE proxies ADD COLUMN {column_name} {ddl}"
            )


def _ensure_task_log_schema() -> None:
    required_columns = {
        "task_id": "TEXT NOT NULL DEFAULT ''",
    }

    with engine.begin() as conn:
        existing_columns = {
            str(row[1])
            for row in conn.exec_driver_sql("PRAGMA table_info(task_logs)").fetchall()
        }
        for column_name, ddl in required_columns.items():
            if column_name in existing_columns:
                continue
            conn.exec_driver_sql(
                f"ALTER TABLE task_logs ADD COLUMN {column_name} {ddl}"
            )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_task_logs_task_id ON task_logs(task_id)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_task_logs_platform_id_task_id "
            "ON task_logs(platform, id DESC, task_id)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_task_log_summaries_platform_log "
            "ON task_log_summaries(platform, log_id, group_key)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_task_log_summaries_platform_source_log "
            "ON task_log_summaries(platform, source, log_id, group_key)"
        )
        conn.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS trg_task_logs_delete_summary
            AFTER DELETE ON task_logs
            BEGIN
                DELETE FROM task_log_summaries WHERE log_id = OLD.id;
            END
            """
        )


def _ensure_account_sort_indexes() -> None:
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_accounts_platform_created_at_id "
            "ON accounts(platform, created_at, id)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_accounts_status_platform "
            "ON accounts(status, platform)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_accounts_platform_list_state_freshness "
            "ON accounts(platform, updated_at, email, created_at)"
        )


def _ensure_external_subscription_claim_schema() -> None:
    required_columns = {
        "claim_id": "TEXT NOT NULL DEFAULT ''",
        "account_id": "INTEGER NOT NULL DEFAULT 0",
        "email": "TEXT NOT NULL DEFAULT ''",
        "consumer": "TEXT NOT NULL DEFAULT ''",
        "status": "TEXT NOT NULL DEFAULT 'prechecking'",
        "payment_link": "TEXT NOT NULL DEFAULT ''",
        "plan": "TEXT NOT NULL DEFAULT 'plus'",
        "country": "TEXT NOT NULL DEFAULT ''",
        "currency": "TEXT NOT NULL DEFAULT ''",
        "precheck_expires_at": "TEXT NOT NULL DEFAULT ''",
        "lease_expires_at": "TEXT NOT NULL DEFAULT ''",
        "verify_after_at": "TEXT NOT NULL DEFAULT ''",
        "claimed_at": "TEXT NOT NULL DEFAULT ''",
        "prechecked_at": "TEXT NOT NULL DEFAULT ''",
        "result_written_at": "TEXT NOT NULL DEFAULT ''",
        "paid_at": "TEXT NOT NULL DEFAULT ''",
        "failed_at": "TEXT NOT NULL DEFAULT ''",
        "released_at": "TEXT NOT NULL DEFAULT ''",
        "provider": "TEXT NOT NULL DEFAULT ''",
        "external_payment_id": "TEXT NOT NULL DEFAULT ''",
        "message": "TEXT NOT NULL DEFAULT ''",
        "error_code": "TEXT NOT NULL DEFAULT ''",
        "last_error": "TEXT NOT NULL DEFAULT ''",
        "details_json": "TEXT NOT NULL DEFAULT '{}'",
        "created_at": "TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP",
        "updated_at": "TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP",
    }

    with engine.begin() as conn:
        existing_columns = {
            str(row[1])
            for row in conn.exec_driver_sql("PRAGMA table_info(external_subscription_claims)").fetchall()
        }
        if not existing_columns:
            return
        for column_name, ddl in required_columns.items():
            if column_name in existing_columns:
                continue
            conn.exec_driver_sql(
                f"ALTER TABLE external_subscription_claims ADD COLUMN {column_name} {ddl}"
            )
        conn.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_external_subscription_claims_claim_id "
            "ON external_subscription_claims(claim_id)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_external_subscription_claims_account_id "
            "ON external_subscription_claims(account_id)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_external_subscription_claims_status "
            "ON external_subscription_claims(status)"
        )
        conn.exec_driver_sql(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_external_subscription_claims_active_account
            ON external_subscription_claims(account_id)
            WHERE status IN ('prechecking', 'claimed', 'processing')
            """
        )


def _ensure_external_access_token_claim_schema() -> None:
    required_columns = {
        "claim_id": "TEXT NOT NULL DEFAULT ''",
        "account_id": "INTEGER NOT NULL DEFAULT 0",
        "email": "TEXT NOT NULL DEFAULT ''",
        "consumer": "TEXT NOT NULL DEFAULT ''",
        "status": "TEXT NOT NULL DEFAULT 'prechecking'",
        "token_source": "TEXT NOT NULL DEFAULT ''",
        "token_fingerprint": "TEXT NOT NULL DEFAULT ''",
        "auth_state": "TEXT NOT NULL DEFAULT ''",
        "subscription_plan": "TEXT NOT NULL DEFAULT ''",
        "subscription_checked_at": "TEXT NOT NULL DEFAULT ''",
        "lease_expires_at": "TEXT NOT NULL DEFAULT ''",
        "claimed_at": "TEXT NOT NULL DEFAULT ''",
        "prechecked_at": "TEXT NOT NULL DEFAULT ''",
        "paid_at": "TEXT NOT NULL DEFAULT ''",
        "failed_at": "TEXT NOT NULL DEFAULT ''",
        "released_at": "TEXT NOT NULL DEFAULT ''",
        "result_written_at": "TEXT NOT NULL DEFAULT ''",
        "provider": "TEXT NOT NULL DEFAULT ''",
        "external_payment_id": "TEXT NOT NULL DEFAULT ''",
        "message": "TEXT NOT NULL DEFAULT ''",
        "error_code": "TEXT NOT NULL DEFAULT ''",
        "last_error": "TEXT NOT NULL DEFAULT ''",
        "details_json": "TEXT NOT NULL DEFAULT '{}'",
        "created_at": "TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP",
        "updated_at": "TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP",
    }

    with engine.begin() as conn:
        existing_columns = {
            str(row[1])
            for row in conn.exec_driver_sql("PRAGMA table_info(external_access_token_claims)").fetchall()
        }
        if not existing_columns:
            return
        for column_name, ddl in required_columns.items():
            if column_name in existing_columns:
                continue
            conn.exec_driver_sql(
                f"ALTER TABLE external_access_token_claims ADD COLUMN {column_name} {ddl}"
            )
        conn.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_external_access_token_claims_claim_id "
            "ON external_access_token_claims(claim_id)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_external_access_token_claims_account_id "
            "ON external_access_token_claims(account_id)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_external_access_token_claims_email "
            "ON external_access_token_claims(email)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_external_access_token_claims_status "
            "ON external_access_token_claims(status)"
        )
        conn.exec_driver_sql(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_external_access_token_claims_active_account
            ON external_access_token_claims(account_id)
            WHERE status IN ('prechecking', 'claimed', 'processing')
            """
        )


def _ensure_icloud_hme_alias_schema() -> None:
    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS icloud_hme_alias (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                anonymous_id TEXT NOT NULL UNIQUE,
                hme TEXT NOT NULL,
                label TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                forward_to TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 0,
                created_source TEXT NOT NULL DEFAULT 'unknown',
                record_source TEXT NOT NULL DEFAULT 'live_create',
                purpose TEXT NOT NULL DEFAULT 'chatgpt_register',
                bound_service TEXT NOT NULL DEFAULT 'chatgpt',
                bound_account_email TEXT NOT NULL DEFAULT '',
                bound_account_ref TEXT NOT NULL DEFAULT '',
                task_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'reserved',
                use_count INTEGER NOT NULL DEFAULT 0,
                first_claimed_at TEXT NOT NULL DEFAULT '',
                last_claimed_at TEXT NOT NULL DEFAULT '',
                last_synced_at TEXT NOT NULL DEFAULT '',
                last_otp_at TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        required_columns = {
            "enabled": "INTEGER NOT NULL DEFAULT 0",
            "created_source": "TEXT NOT NULL DEFAULT 'unknown'",
            "record_source": "TEXT NOT NULL DEFAULT 'live_create'",
            "use_count": "INTEGER NOT NULL DEFAULT 0",
            "first_claimed_at": "TEXT NOT NULL DEFAULT ''",
            "last_claimed_at": "TEXT NOT NULL DEFAULT ''",
            "last_synced_at": "TEXT NOT NULL DEFAULT ''",
        }
        existing_columns = {
            str(row[1])
            for row in conn.exec_driver_sql("PRAGMA table_info(icloud_hme_alias)").fetchall()
        }
        for column_name, ddl in required_columns.items():
            if column_name in existing_columns:
                continue
            conn.exec_driver_sql(
                f"ALTER TABLE icloud_hme_alias ADD COLUMN {column_name} {ddl}"
            )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_icloud_hme_alias_hme ON icloud_hme_alias(hme)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_icloud_hme_alias_status ON icloud_hme_alias(status)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_icloud_hme_alias_bound_account_email ON icloud_hme_alias(bound_account_email)"
        )


def _ensure_icloud_hme_recheck_queue_schema() -> None:
    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS icloud_hme_recheck_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                campaign_id TEXT NOT NULL,
                anonymous_id TEXT NOT NULL,
                hme TEXT NOT NULL,
                account_id INTEGER NOT NULL DEFAULT 0,
                account_email TEXT NOT NULL DEFAULT '',
                source_type TEXT NOT NULL DEFAULT 'icloud_hme',
                status TEXT NOT NULL DEFAULT 'pending',
                result_code TEXT NOT NULL DEFAULT '',
                result_message TEXT NOT NULL DEFAULT '',
                saved_account_id INTEGER NOT NULL DEFAULT 0,
                access_token_saved INTEGER NOT NULL DEFAULT 0,
                delete_candidate INTEGER NOT NULL DEFAULT 0,
                delete_candidate_reason TEXT NOT NULL DEFAULT '',
                apple_delete_status TEXT NOT NULL DEFAULT 'not_requested',
                attempt_count INTEGER NOT NULL DEFAULT 0,
                last_task_id TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                checked_at TEXT NOT NULL DEFAULT '',
                started_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        required_columns = {
            "campaign_id": "TEXT NOT NULL DEFAULT ''",
            "anonymous_id": "TEXT NOT NULL DEFAULT ''",
            "hme": "TEXT NOT NULL DEFAULT ''",
            "account_id": "INTEGER NOT NULL DEFAULT 0",
            "account_email": "TEXT NOT NULL DEFAULT ''",
            "source_type": "TEXT NOT NULL DEFAULT 'icloud_hme'",
            "status": "TEXT NOT NULL DEFAULT 'pending'",
            "result_code": "TEXT NOT NULL DEFAULT ''",
            "result_message": "TEXT NOT NULL DEFAULT ''",
            "saved_account_id": "INTEGER NOT NULL DEFAULT 0",
            "access_token_saved": "INTEGER NOT NULL DEFAULT 0",
            "delete_candidate": "INTEGER NOT NULL DEFAULT 0",
            "delete_candidate_reason": "TEXT NOT NULL DEFAULT ''",
            "apple_delete_status": "TEXT NOT NULL DEFAULT 'not_requested'",
            "attempt_count": "INTEGER NOT NULL DEFAULT 0",
            "last_task_id": "TEXT NOT NULL DEFAULT ''",
            "last_error": "TEXT NOT NULL DEFAULT ''",
            "details_json": "TEXT NOT NULL DEFAULT '{}'",
            "created_at": "TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP",
            "updated_at": "TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP",
            "checked_at": "TEXT NOT NULL DEFAULT ''",
            "started_at": "TEXT NOT NULL DEFAULT ''",
        }
        existing_columns = {
            str(row[1])
            for row in conn.exec_driver_sql("PRAGMA table_info(icloud_hme_recheck_queue)").fetchall()
        }
        for column_name, ddl in required_columns.items():
            if column_name in existing_columns:
                continue
            conn.exec_driver_sql(
                f"ALTER TABLE icloud_hme_recheck_queue ADD COLUMN {column_name} {ddl}"
            )
        conn.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_icloud_hme_recheck_queue_campaign_alias "
            "ON icloud_hme_recheck_queue(campaign_id, anonymous_id)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_icloud_hme_recheck_queue_campaign_status "
            "ON icloud_hme_recheck_queue(campaign_id, status)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_icloud_hme_recheck_queue_hme "
            "ON icloud_hme_recheck_queue(hme)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_icloud_hme_recheck_queue_delete_candidate "
            "ON icloud_hme_recheck_queue(delete_candidate)"
        )


def _ensure_pipeline_schema() -> None:
    with engine.begin() as conn:
        existing_task_columns = {
            str(row[1])
            for row in conn.exec_driver_sql("PRAGMA table_info(pipeline_tasks)").fetchall()
        }
        if "logs_json" not in existing_task_columns:
            conn.exec_driver_sql(
                "ALTER TABLE pipeline_tasks ADD COLUMN logs_json TEXT NOT NULL DEFAULT '[]'"
            )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_pipeline_tasks_task_key ON pipeline_tasks(task_key)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_pipeline_tasks_status ON pipeline_tasks(status)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_pipeline_account_items_pipeline_task_id ON pipeline_account_items(pipeline_task_id)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_pipeline_account_items_account_id ON pipeline_account_items(account_id)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_pipeline_account_items_email ON pipeline_account_items(email)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_pipeline_account_items_pipeline_status ON pipeline_account_items(pipeline_status)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_pipeline_account_items_register_stage ON pipeline_account_items(register_stage)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_pipeline_account_items_payment_stage ON pipeline_account_items(payment_stage)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_pipeline_account_items_auth_stage ON pipeline_account_items(auth_stage)"
        )



def _ensure_idea_oaipay_pipeline_schema() -> None:
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_idea_oaipay_pipeline_tasks_task_key ON idea_oaipay_pipeline_tasks(task_key)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_idea_oaipay_pipeline_tasks_status ON idea_oaipay_pipeline_tasks(status)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_idea_oaipay_pipeline_tasks_source_type ON idea_oaipay_pipeline_tasks(source_type)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_idea_oaipay_pipeline_items_pipeline_task_id ON idea_oaipay_pipeline_items(pipeline_task_id)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_idea_oaipay_pipeline_items_account_id ON idea_oaipay_pipeline_items(account_id)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_idea_oaipay_pipeline_items_email ON idea_oaipay_pipeline_items(email)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_idea_oaipay_pipeline_items_overall_status ON idea_oaipay_pipeline_items(overall_status)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_idea_oaipay_pipeline_items_idea_stage ON idea_oaipay_pipeline_items(idea_stage)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_idea_oaipay_pipeline_items_check_stage ON idea_oaipay_pipeline_items(check_stage)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_idea_oaipay_pipeline_items_gate_stage ON idea_oaipay_pipeline_items(gate_stage)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_idea_oaipay_pipeline_items_phone_stage ON idea_oaipay_pipeline_items(phone_stage)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_idea_oaipay_pipeline_items_oaipay_stage ON idea_oaipay_pipeline_items(oaipay_stage)"
        )

def _row_to_icloud_hme_alias_payload(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    if hasattr(row, "_mapping"):
        row = dict(row._mapping)
    if isinstance(row, dict):
        created_at = row.get("created_at")
        updated_at = row.get("updated_at")
        status = str(row.get("status") or "")
        use_count = int(row.get("use_count") or 0)
        enabled = bool(int(row.get("enabled") or 0))
        account_disabled = status in {"account_deactivated", "account_disabled"}
        used_by_system = bool(
            use_count > 0
            or str(row.get("task_id") or "").strip()
            or str(row.get("bound_account_email") or "").strip()
            or status in {"in_use", "registered", "register_failed", "account_deactivated", "account_disabled"}
        )
        return {
            "id": row.get("id"),
            "anonymous_id": str(row.get("anonymous_id") or ""),
            "hme": str(row.get("hme") or ""),
            "label": str(row.get("label") or ""),
            "note": str(row.get("note") or ""),
            "forward_to": str(row.get("forward_to") or ""),
            "enabled": enabled,
            "created_source": str(row.get("created_source") or "unknown"),
            "record_source": str(row.get("record_source") or ""),
            "purpose": str(row.get("purpose") or ""),
            "bound_service": str(row.get("bound_service") or ""),
            "bound_account_email": str(row.get("bound_account_email") or ""),
            "bound_account_ref": str(row.get("bound_account_ref") or ""),
            "task_id": str(row.get("task_id") or ""),
            "status": status,
            "use_count": use_count,
            "first_claimed_at": str(row.get("first_claimed_at") or ""),
            "last_claimed_at": str(row.get("last_claimed_at") or ""),
            "last_synced_at": str(row.get("last_synced_at") or ""),
            "used_by_system": used_by_system,
            "account_disabled": account_disabled,
            "account_disabled_label": "账号已禁用/死号" if account_disabled else "",
            "is_manual_created": str(row.get("created_source") or "unknown") == "manual_created",
            "last_otp_at": str(row.get("last_otp_at") or ""),
            "last_error": str(row.get("last_error") or ""),
            "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at or ""),
            "updated_at": updated_at.isoformat() if hasattr(updated_at, "isoformat") else str(updated_at or ""),
        }
    if isinstance(row, IcloudHmeAliasModel):
        status = str(getattr(row, "status", "") or "")
        account_disabled = status in {"account_deactivated", "account_disabled"}
        used_by_system = bool(
            int(getattr(row, "use_count", 0) or 0) > 0
            or str(getattr(row, "task_id", "") or "").strip()
            or str(getattr(row, "bound_account_email", "") or "").strip()
            or status in {"in_use", "registered", "register_failed", "account_deactivated", "account_disabled"}
        )
        return {
            "id": row.id,
            "anonymous_id": row.anonymous_id,
            "hme": row.hme,
            "label": row.label,
            "note": row.note,
            "forward_to": row.forward_to,
            "enabled": bool(getattr(row, "enabled", False)),
            "created_source": row.created_source,
            "record_source": row.record_source,
            "purpose": row.purpose,
            "bound_service": row.bound_service,
            "bound_account_email": row.bound_account_email,
            "bound_account_ref": row.bound_account_ref,
            "task_id": row.task_id,
            "status": row.status,
            "use_count": row.use_count,
            "first_claimed_at": row.first_claimed_at,
            "last_claimed_at": row.last_claimed_at,
            "last_synced_at": row.last_synced_at,
            "used_by_system": used_by_system,
            "account_disabled": account_disabled,
            "account_disabled_label": "账号已禁用/死号" if account_disabled else "",
            "is_manual_created": row.created_source == "manual_created",
            "last_otp_at": row.last_otp_at,
            "last_error": row.last_error,
            "created_at": row.created_at.isoformat() if getattr(row, "created_at", None) else "",
            "updated_at": row.updated_at.isoformat() if getattr(row, "updated_at", None) else "",
        }
    status = str(getattr(row, "status", "") or "")
    account_disabled = status in {"account_deactivated", "account_disabled"}
    return {
        "id": getattr(row, "id", None),
        "anonymous_id": str(getattr(row, "anonymous_id", "") or ""),
        "hme": str(getattr(row, "hme", "") or ""),
        "label": str(getattr(row, "label", "") or ""),
        "note": str(getattr(row, "note", "") or ""),
        "forward_to": str(getattr(row, "forward_to", "") or ""),
        "enabled": bool(getattr(row, "enabled", False)),
        "created_source": str(getattr(row, "created_source", "unknown") or "unknown"),
        "record_source": str(getattr(row, "record_source", "") or ""),
        "purpose": str(getattr(row, "purpose", "") or ""),
        "bound_service": str(getattr(row, "bound_service", "") or ""),
        "bound_account_email": str(getattr(row, "bound_account_email", "") or ""),
        "bound_account_ref": str(getattr(row, "bound_account_ref", "") or ""),
        "task_id": str(getattr(row, "task_id", "") or ""),
        "status": status,
        "use_count": int(getattr(row, "use_count", 0) or 0),
        "first_claimed_at": str(getattr(row, "first_claimed_at", "") or ""),
        "last_claimed_at": str(getattr(row, "last_claimed_at", "") or ""),
        "last_synced_at": str(getattr(row, "last_synced_at", "") or ""),
        "used_by_system": bool(
            int(getattr(row, "use_count", 0) or 0) > 0
            or str(getattr(row, "task_id", "") or "").strip()
            or str(getattr(row, "bound_account_email", "") or "").strip()
            or status in {"in_use", "registered", "register_failed", "account_deactivated", "account_disabled"}
        ),
        "account_disabled": account_disabled,
        "account_disabled_label": "账号已禁用/死号" if account_disabled else "",
        "last_otp_at": str(getattr(row, "last_otp_at", "") or ""),
        "last_error": str(getattr(row, "last_error", "") or ""),
        "created_at": getattr(row, "created_at", ""),
        "updated_at": getattr(row, "updated_at", ""),
    }


def insert_icloud_hme_alias(
    *,
    anonymous_id: str,
    hme: str,
    label: str = "",
    note: str = "",
    forward_to: str = "",
    enabled: bool | None = None,
    created_source: str = "unknown",
    record_source: str = "live_create",
    purpose: str = "chatgpt_register",
    bound_service: str = "chatgpt",
    bound_account_email: str = "",
    bound_account_ref: str = "",
    task_id: str = "",
    status: str = "reserved",
    use_count: int = 0,
    first_claimed_at: str = "",
    last_claimed_at: str = "",
    last_synced_at: str = "",
    last_otp_at: str = "",
    last_error: str = "",
) -> dict[str, Any]:
    normalized_anonymous_id = str(anonymous_id or "").strip()
    normalized_hme = str(hme or "").strip()
    if not normalized_anonymous_id:
        raise ValueError("anonymous_id is required")
    if not normalized_hme:
        raise ValueError("hme is required")

    with Session(engine) as session:
        existing = session.exec(
            select(IcloudHmeAliasModel).where(
                IcloudHmeAliasModel.anonymous_id == normalized_anonymous_id
            )
        ).first()
        now = _utcnow()
        if existing:
            existing_used_by_system = bool(
                int(getattr(existing, "use_count", 0) or 0) > 0
                or str(getattr(existing, "task_id", "") or "").strip()
                or str(getattr(existing, "bound_account_email", "") or "").strip()
                or str(getattr(existing, "status", "") or "").strip() in {"in_use", "registered", "register_failed"}
            )
            existing.hme = normalized_hme
            existing.label = str(label or "")
            existing.note = str(note or "")
            existing.forward_to = str(forward_to or "")
            if enabled is not None:
                existing.enabled = bool(enabled)
            existing.created_source = str(created_source or getattr(existing, "created_source", "unknown") or "unknown")
            existing.record_source = str(record_source or getattr(existing, "record_source", "live_create") or "live_create")
            existing.purpose = str(purpose or "chatgpt_register")
            existing.bound_service = str(bound_service or "chatgpt")
            if existing_used_by_system:
                existing.bound_account_email = str(
                    bound_account_email or getattr(existing, "bound_account_email", "") or ""
                )
                existing.bound_account_ref = str(
                    bound_account_ref or getattr(existing, "bound_account_ref", "") or ""
                )
                existing.task_id = str(task_id or getattr(existing, "task_id", "") or "")
                existing.status = str(status or getattr(existing, "status", "reserved") or "reserved")
                existing.use_count = int(use_count or getattr(existing, "use_count", 0) or 0)
                existing.first_claimed_at = str(first_claimed_at or getattr(existing, "first_claimed_at", "") or "")
                existing.last_claimed_at = str(last_claimed_at or getattr(existing, "last_claimed_at", "") or "")
                existing.last_otp_at = str(last_otp_at or getattr(existing, "last_otp_at", "") or "")
                existing.last_error = str(last_error or getattr(existing, "last_error", "") or "")
            else:
                existing.bound_account_email = str(bound_account_email or "")
                existing.bound_account_ref = str(bound_account_ref or "")
                existing.task_id = str(task_id or "")
                existing.status = str(status or "reserved")
                existing.use_count = int(use_count or getattr(existing, "use_count", 0) or 0)
                existing.first_claimed_at = str(first_claimed_at or getattr(existing, "first_claimed_at", "") or "")
                existing.last_claimed_at = str(last_claimed_at or getattr(existing, "last_claimed_at", "") or "")
                existing.last_otp_at = str(last_otp_at or "")
                existing.last_error = str(last_error or "")
            existing.last_synced_at = str(last_synced_at or getattr(existing, "last_synced_at", "") or "")
            existing.updated_at = now
            session.add(existing)
            session.commit()
            session.refresh(existing)
            return _row_to_icloud_hme_alias_payload(existing)

        record = IcloudHmeAliasModel(
            anonymous_id=normalized_anonymous_id,
            hme=normalized_hme,
            label=str(label or ""),
            note=str(note or ""),
            forward_to=str(forward_to or ""),
            enabled=bool(enabled) if enabled is not None else False,
            created_source=str(created_source or "unknown"),
            record_source=str(record_source or "live_create"),
            purpose=str(purpose or "chatgpt_register"),
            bound_service=str(bound_service or "chatgpt"),
            bound_account_email=str(bound_account_email or ""),
            bound_account_ref=str(bound_account_ref or ""),
            task_id=str(task_id or ""),
            status=str(status or "reserved"),
            use_count=int(use_count or 0),
            first_claimed_at=str(first_claimed_at or ""),
            last_claimed_at=str(last_claimed_at or ""),
            last_synced_at=str(last_synced_at or ""),
            last_otp_at=str(last_otp_at or ""),
            last_error=str(last_error or ""),
        )
        session.add(record)
        session.commit()
        session.refresh(record)
        return _row_to_icloud_hme_alias_payload(record)


def get_icloud_hme_alias_by_anonymous_id(anonymous_id: str) -> dict[str, Any] | None:
    normalized = str(anonymous_id or "").strip()
    if not normalized:
        return None
    with Session(engine) as session:
        row = session.exec(
            select(IcloudHmeAliasModel).where(IcloudHmeAliasModel.anonymous_id == normalized)
        ).first()
        if row is None:
            return None
        return _row_to_icloud_hme_alias_payload(row)


def prune_icloud_hme_aliases_not_in_remote(
    remote_anonymous_ids: set[str] | list[str],
    *,
    purpose: str = "chatgpt_register",
    bound_service: str = "chatgpt",
    forward_to: str = "",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Delete local iCloud HME alias rows that no longer exist in Apple HME list.

    This is for local alias-pool reconciliation only. It does not delete ChatGPT
    accounts and does not call Apple's deactivate/delete endpoints.
    """
    remote_ids = {str(value or "").strip() for value in (remote_anonymous_ids or [])}
    remote_ids = {value for value in remote_ids if value}
    normalized_purpose = str(purpose or "chatgpt_register").strip() or "chatgpt_register"
    normalized_service = str(bound_service or "chatgpt").strip() or "chatgpt"
    normalized_forward_to = str(forward_to or "").strip()

    with Session(engine) as session:
        query = select(IcloudHmeAliasModel).where(
            IcloudHmeAliasModel.purpose == normalized_purpose,
            IcloudHmeAliasModel.bound_service == normalized_service,
        )
        if normalized_forward_to:
            query = query.where(IcloudHmeAliasModel.forward_to == normalized_forward_to)
        rows = session.exec(query).all()

        to_delete = [
            row
            for row in rows
            if str(getattr(row, "anonymous_id", "") or "").strip() not in remote_ids
        ]
        deleted_rows = [_row_to_icloud_hme_alias_payload(row) for row in to_delete]

        if not dry_run:
            for row in to_delete:
                session.delete(row)
            session.commit()

    return {
        "matched_local": len(rows),
        "kept": len(rows) - len(to_delete),
        "deleted": 0 if dry_run else len(to_delete),
        "would_delete": len(to_delete),
        "dry_run": bool(dry_run),
        "data": deleted_rows,
    }


def update_icloud_hme_alias_on_otp(anonymous_id: str, *, last_otp_at: str = "") -> dict[str, Any]:
    return patch_icloud_hme_alias(
        anonymous_id,
        {
            "last_otp_at": str(last_otp_at or datetime.now(timezone.utc).isoformat()),
        },
        allow_internal=True,
    )


def update_icloud_hme_alias_on_success(
    anonymous_id: str,
    *,
    bound_account_email: str = "",
    task_id: str = "",
    note: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "registered",
        "bound_account_email": str(bound_account_email or ""),
        "task_id": str(task_id or ""),
        "last_error": "",
    }
    if note is not None:
        payload["note"] = str(note)
    return patch_icloud_hme_alias(anonymous_id, payload, allow_internal=True)


def update_icloud_hme_alias_on_failure(
    anonymous_id: str,
    *,
    error_message: str = "",
    task_id: str = "",
) -> dict[str, Any]:
    return patch_icloud_hme_alias(
        anonymous_id,
        {
            "status": "register_failed",
            "last_error": str(error_message or ""),
            "task_id": str(task_id or ""),
        },
        allow_internal=True,
    )


def update_icloud_hme_alias_on_account_deactivated(
    anonymous_id: str,
    *,
    error_message: str = "",
    task_id: str = "",
) -> dict[str, Any]:
    return patch_icloud_hme_alias(
        anonymous_id,
        {
            "status": "account_deactivated",
            "enabled": False,
            "last_error": str(error_message or ""),
            "task_id": str(task_id or ""),
        },
        allow_internal=True,
    )


def release_icloud_hme_alias_after_early_failure(
    anonymous_id: str,
    *,
    error_message: str = "",
    task_id: str = "",
) -> dict[str, Any]:
    return patch_icloud_hme_alias(
        anonymous_id,
        {
            "status": "reserved",
            "last_error": str(error_message or ""),
            "task_id": "",
        },
        allow_internal=True,
    )


def list_icloud_hme_aliases(
    *,
    page: int = 1,
    size: int = 20,
    status: str = "",
    purpose: str = "",
    bound_service: str = "",
    hme: str = "",
    bound_account_email: str = "",
    enabled: str = "",
    created_source: str = "",
    ready_only: bool = False,
    forward_to: str = "",
) -> dict[str, Any]:
    page_value = max(int(page or 1), 1)
    size_value = int(size or 20)
    if size_value < 1:
        size_value = 20
    if size_value > 100:
        size_value = 100

    query = select(IcloudHmeAliasModel)
    count_query = select(IcloudHmeAliasModel)

    filters: list[Any] = []
    if str(status or "").strip():
        filters.append(IcloudHmeAliasModel.status == str(status).strip())
    if str(purpose or "").strip():
        filters.append(IcloudHmeAliasModel.purpose == str(purpose).strip())
    if str(bound_service or "").strip():
        filters.append(IcloudHmeAliasModel.bound_service == str(bound_service).strip())
    if str(hme or "").strip():
        filters.append(IcloudHmeAliasModel.hme.contains(str(hme).strip()))
    if str(bound_account_email or "").strip():
        filters.append(
            IcloudHmeAliasModel.bound_account_email.contains(str(bound_account_email).strip())
        )
    if str(created_source or "").strip():
        filters.append(IcloudHmeAliasModel.created_source == str(created_source).strip())
    if str(forward_to or "").strip():
        filters.append(IcloudHmeAliasModel.forward_to == str(forward_to).strip())
    enabled_text = str(enabled or "").strip().lower()
    if enabled_text in {"1", "true", "yes", "enabled"}:
        filters.append(IcloudHmeAliasModel.enabled == True)
    elif enabled_text in {"0", "false", "no", "disabled"}:
        filters.append(IcloudHmeAliasModel.enabled == False)
    if bool(ready_only):
        filters.extend([
            IcloudHmeAliasModel.enabled == True,
            IcloudHmeAliasModel.status == "reserved",
            IcloudHmeAliasModel.task_id == "",
            IcloudHmeAliasModel.bound_account_email == "",
            IcloudHmeAliasModel.purpose == "chatgpt_register",
            IcloudHmeAliasModel.bound_service == "chatgpt",
        ])

    for filter_clause in filters:
        query = query.where(filter_clause)
        count_query = count_query.where(filter_clause)

    query = query.order_by(IcloudHmeAliasModel.id.desc())
    query = query.offset((page_value - 1) * size_value).limit(size_value)

    with Session(engine) as session:
        rows = session.exec(query).all()
        total = len(session.exec(count_query).all())
        available_import_pool_count = len(
            session.exec(
                select(IcloudHmeAliasModel).where(
                    IcloudHmeAliasModel.enabled == True,
                    IcloudHmeAliasModel.status == "reserved",
                    IcloudHmeAliasModel.task_id == "",
                    IcloudHmeAliasModel.bound_account_email == "",
                    IcloudHmeAliasModel.purpose == (str(purpose or "chatgpt_register").strip() or "chatgpt_register"),
                    IcloudHmeAliasModel.bound_service == (str(bound_service or "chatgpt").strip() or "chatgpt"),
                    *(
                        [IcloudHmeAliasModel.forward_to == str(forward_to).strip()]
                        if str(forward_to or "").strip()
                        else []
                    ),
                )
            ).all()
        )

    return {
        "data": [_row_to_icloud_hme_alias_payload(row) for row in rows],
        "total": total,
        "page": page_value,
        "size": size_value,
        "pages": ceil(total / size_value) if size_value else 0,
        "available_import_pool_count": available_import_pool_count,
    }


def count_icloud_hme_ready_aliases(
    *,
    purpose: str = "chatgpt_register",
    bound_service: str = "chatgpt",
    forward_to: str = "",
) -> int:
    normalized_purpose = str(purpose or "chatgpt_register").strip() or "chatgpt_register"
    normalized_service = str(bound_service or "chatgpt").strip() or "chatgpt"
    normalized_forward_to = str(forward_to or "").strip()

    query = select(IcloudHmeAliasModel).where(
        IcloudHmeAliasModel.enabled == True,
        IcloudHmeAliasModel.status == "reserved",
        IcloudHmeAliasModel.task_id == "",
        IcloudHmeAliasModel.bound_account_email == "",
        IcloudHmeAliasModel.purpose == normalized_purpose,
        IcloudHmeAliasModel.bound_service == normalized_service,
    )
    if normalized_forward_to:
        query = query.where(IcloudHmeAliasModel.forward_to == normalized_forward_to)

    with Session(engine) as session:
        return len(session.exec(query).all())


def _norm_alias_email(value: Any) -> str:
    return str(value or "").strip().lower()


def list_icloud_hme_deletion_candidates(
    *,
    purpose: str = "chatgpt_register",
    bound_service: str = "chatgpt",
) -> dict[str, Any]:
    """把所有 iCloud HME 别名分类为「可删 / 保护」，供自动删除 worker 与预览使用。

    按顺序判定（命中即停）：
      - ``retired``：已退役（已在 Apple 端删除）→ 跳过。
      - ``in_flight``：``status==in_use``，或 ``reserved`` 且已被任务领取（task_id 非空）→ 保护
        （可能正在注册）。注意：已完成注册的别名 status=registered/register_failed 也会带 task_id，
        那是历史记录，不算在途，交给账号匹配判定。
      - ``ready_stock``：``enabled`` 且 ``reserved`` 且未领取且未绑定 → 保护（补池待用库存，勿删）。
      - 按 ``hme`` / ``bound_account_email`` 匹配 chatgpt 账号：
          * 无匹配           → ``orphan``（可直接删）
          * 任一账号未失效   → 保护（``account_alive``）
          * 全部 ``invalid`` → ``bound_invalid``（需先失效测活再决定），附 ``account_ids``
    """
    normalized_purpose = str(purpose or "chatgpt_register").strip() or "chatgpt_register"
    normalized_service = str(bound_service or "chatgpt").strip() or "chatgpt"

    with Session(engine) as session:
        alias_rows = session.exec(select(IcloudHmeAliasModel)).all()
        account_rows = session.exec(
            select(AccountModel).where(AccountModel.platform == "chatgpt")
        ).all()
        latest_recheck_campaign_id = _latest_icloud_hme_recheck_campaign_id(session)
        recheck_rows = (
            session.exec(
                select(IcloudHmeRecheckQueueModel).where(
                    IcloudHmeRecheckQueueModel.campaign_id == latest_recheck_campaign_id
                )
            ).all()
            if latest_recheck_campaign_id
            else []
        )
        recheck_by_anonymous_id = {
            str(getattr(row, "anonymous_id", "") or "").strip(): row
            for row in recheck_rows
            if str(getattr(row, "anonymous_id", "") or "").strip()
        }

    accounts_by_email: dict[str, list[dict[str, Any]]] = {}
    for acc in account_rows:
        key = _norm_alias_email(acc.email)
        if not key:
            continue
        accounts_by_email.setdefault(key, []).append(
            {"id": int(acc.id or 0), "status": str(acc.status or "").strip().lower()}
        )

    orphan: list[dict[str, Any]] = []
    bound_invalid: list[dict[str, Any]] = []
    protected: list[dict[str, Any]] = []
    summary = {
        "orphan": 0,
        "bound_invalid": 0,
        "protected": 0,
        "in_flight": 0,
        "ready_stock": 0,
        "account_alive": 0,
        "retired": 0,
        "recheck_not_confirmed": 0,
    }

    for row in alias_rows:
        status = str(getattr(row, "status", "") or "").strip()
        task_id = str(getattr(row, "task_id", "") or "").strip()
        bound_email = str(getattr(row, "bound_account_email", "") or "").strip()
        payload = _row_to_icloud_hme_alias_payload(row)

        if status == "retired":
            summary["retired"] += 1
            continue

        # 在途：正在注册(in_use)，或 reserved 且已被任务领取的中间态。已完成注册的别名
        # (registered/register_failed) 也会带 task_id，那是历史记录，不在此拦截。
        if status == "in_use" or (status == "reserved" and task_id):
            summary["in_flight"] += 1
            summary["protected"] += 1
            protected.append({**payload, "reason": "in_flight"})
            continue

        # 待用库存：reserved + enabled + 未领取 + 未绑定（与 count_icloud_hme_ready_aliases 对齐）
        if (
            bool(getattr(row, "enabled", False))
            and status == "reserved"
            and not task_id
            and not bound_email
            and str(getattr(row, "purpose", "") or "") == normalized_purpose
            and str(getattr(row, "bound_service", "") or "") == normalized_service
        ):
            summary["ready_stock"] += 1
            summary["protected"] += 1
            protected.append({**payload, "reason": "ready_stock"})
            continue

        # 一旦存在 HME 重跑/复测批次，Apple 端删除模块只处理明确标记的
        # delete_candidate。pending/running/retry/alive 等都先保护，避免未跑完时误删。
        recheck_row = None
        recheck_confirmed_delete = False
        if latest_recheck_campaign_id:
            recheck_row = recheck_by_anonymous_id.get(
                str(getattr(row, "anonymous_id", "") or "").strip()
            )
            recheck_confirmed_delete = bool(recheck_row is not None and getattr(recheck_row, "delete_candidate", False))
            if not recheck_confirmed_delete:
                summary["recheck_not_confirmed"] += 1
                summary["protected"] += 1
                protected.append(
                    {
                        **payload,
                        "reason": "recheck_not_confirmed",
                        "recheck_campaign_id": latest_recheck_campaign_id,
                        "recheck_status": str(getattr(recheck_row, "status", "") or "") if recheck_row is not None else "",
                        "recheck_result_code": str(getattr(recheck_row, "result_code", "") or "") if recheck_row is not None else "",
                    }
                )
                continue

        match_emails = {
            e
            for e in (
                _norm_alias_email(getattr(row, "hme", "")),
                _norm_alias_email(bound_email),
            )
            if e
        }
        matched: list[dict[str, Any]] = []
        for email_key in match_emails:
            matched.extend(accounts_by_email.get(email_key, []))

        if not matched:
            summary["orphan"] += 1
            orphan.append(
                {
                    **payload,
                    "disposition": "orphan",
                    "delete_reason": "no_account",
                    "recheck_confirmed": bool(recheck_confirmed_delete),
                    "recheck_campaign_id": latest_recheck_campaign_id,
                }
            )
            continue

        alive = [a for a in matched if a["status"] != "invalid"]
        if alive:
            summary["account_alive"] += 1
            summary["protected"] += 1
            protected.append(
                {
                    **payload,
                    "reason": "account_alive",
                    "account_statuses": sorted({a["status"] for a in matched if a["status"]}),
                }
            )
            continue

        account_ids = sorted({a["id"] for a in matched if a["id"]})
        summary["bound_invalid"] += 1
        bound_invalid.append(
            {
                **payload,
                "disposition": "bound_invalid",
                "delete_reason": "account_invalid",
                "account_ids": account_ids,
                "recheck_confirmed": bool(recheck_confirmed_delete),
                "recheck_campaign_id": latest_recheck_campaign_id,
            }
        )

    return {
        "orphan": orphan,
        "bound_invalid": bound_invalid,
        "protected": protected,
        "candidates": orphan + bound_invalid,
        "summary": summary,
        "total": len(alias_rows),
    }


def _row_to_icloud_hme_recheck_payload(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    if hasattr(row, "_mapping"):
        row = dict(row._mapping)
    if isinstance(row, dict):
        details_raw = row.get("details_json") or "{}"
        try:
            details = json.loads(details_raw) if isinstance(details_raw, str) else dict(details_raw or {})
        except Exception:
            details = {}
        created_at = row.get("created_at")
        updated_at = row.get("updated_at")
        return {
            "id": row.get("id"),
            "campaign_id": str(row.get("campaign_id") or ""),
            "anonymous_id": str(row.get("anonymous_id") or ""),
            "hme": str(row.get("hme") or ""),
            "account_id": int(row.get("account_id") or 0),
            "account_email": str(row.get("account_email") or ""),
            "source_type": str(row.get("source_type") or ""),
            "status": str(row.get("status") or ""),
            "result_code": str(row.get("result_code") or ""),
            "result_message": str(row.get("result_message") or ""),
            "saved_account_id": int(row.get("saved_account_id") or 0),
            "access_token_saved": bool(int(row.get("access_token_saved") or 0)),
            "delete_candidate": bool(int(row.get("delete_candidate") or 0)),
            "delete_candidate_reason": str(row.get("delete_candidate_reason") or ""),
            "apple_delete_status": str(row.get("apple_delete_status") or "not_requested"),
            "attempt_count": int(row.get("attempt_count") or 0),
            "last_task_id": str(row.get("last_task_id") or ""),
            "last_error": str(row.get("last_error") or ""),
            "details": details if isinstance(details, dict) else {},
            "checked_at": str(row.get("checked_at") or ""),
            "started_at": str(row.get("started_at") or ""),
            "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at or ""),
            "updated_at": updated_at.isoformat() if hasattr(updated_at, "isoformat") else str(updated_at or ""),
        }
    details = {}
    try:
        details = row.get_details() if hasattr(row, "get_details") else {}
    except Exception:
        details = {}
    return {
        "id": getattr(row, "id", None),
        "campaign_id": str(getattr(row, "campaign_id", "") or ""),
        "anonymous_id": str(getattr(row, "anonymous_id", "") or ""),
        "hme": str(getattr(row, "hme", "") or ""),
        "account_id": int(getattr(row, "account_id", 0) or 0),
        "account_email": str(getattr(row, "account_email", "") or ""),
        "source_type": str(getattr(row, "source_type", "") or ""),
        "status": str(getattr(row, "status", "") or ""),
        "result_code": str(getattr(row, "result_code", "") or ""),
        "result_message": str(getattr(row, "result_message", "") or ""),
        "saved_account_id": int(getattr(row, "saved_account_id", 0) or 0),
        "access_token_saved": bool(getattr(row, "access_token_saved", False)),
        "delete_candidate": bool(getattr(row, "delete_candidate", False)),
        "delete_candidate_reason": str(getattr(row, "delete_candidate_reason", "") or ""),
        "apple_delete_status": str(getattr(row, "apple_delete_status", "") or "not_requested"),
        "attempt_count": int(getattr(row, "attempt_count", 0) or 0),
        "last_task_id": str(getattr(row, "last_task_id", "") or ""),
        "last_error": str(getattr(row, "last_error", "") or ""),
        "details": details if isinstance(details, dict) else {},
        "checked_at": str(getattr(row, "checked_at", "") or ""),
        "started_at": str(getattr(row, "started_at", "") or ""),
        "created_at": getattr(row, "created_at", "").isoformat() if hasattr(getattr(row, "created_at", ""), "isoformat") else str(getattr(row, "created_at", "") or ""),
        "updated_at": getattr(row, "updated_at", "").isoformat() if hasattr(getattr(row, "updated_at", ""), "isoformat") else str(getattr(row, "updated_at", "") or ""),
    }


ICLOUD_HME_RECHECK_DONE_STATUSES = {"alive", "delete_candidate", "dead_kept", "skipped"}


def _latest_icloud_hme_recheck_campaign_id(session: Session | None = None) -> str:
    close_session = False
    if session is None:
        session = Session(engine)
        close_session = True
    try:
        row = session.exec(
            select(IcloudHmeRecheckQueueModel.campaign_id)
            .order_by(IcloudHmeRecheckQueueModel.created_at.desc())
            .limit(1)
        ).first()
        return str(row or "").strip()
    finally:
        if close_session:
            session.close()


def _recheck_queue_summary_for_campaign(session: Session, campaign_id: str) -> dict[str, Any]:
    rows = session.exec(
        select(IcloudHmeRecheckQueueModel).where(IcloudHmeRecheckQueueModel.campaign_id == campaign_id)
    ).all()
    by_status: dict[str, int] = {}
    by_result: dict[str, int] = {}
    delete_candidates = 0
    access_token_saved = 0
    checked = 0
    for row in rows:
        status = str(getattr(row, "status", "") or "").strip() or "unknown"
        result = str(getattr(row, "result_code", "") or "").strip() or "unknown"
        by_status[status] = by_status.get(status, 0) + 1
        by_result[result] = by_result.get(result, 0) + 1
        if bool(getattr(row, "delete_candidate", False)):
            delete_candidates += 1
        if bool(getattr(row, "access_token_saved", False)):
            access_token_saved += 1
        if status in ICLOUD_HME_RECHECK_DONE_STATUSES:
            checked += 1
    total = len(rows)
    return {
        "campaign_id": campaign_id,
        "total": total,
        "checked": checked,
        "unchecked": max(total - checked, 0),
        "pending": by_status.get("pending", 0),
        "running": by_status.get("running", 0),
        "retry": by_status.get("retry", 0),
        "failed": by_status.get("failed", 0),
        "alive": by_status.get("alive", 0),
        "delete_candidate": by_status.get("delete_candidate", 0),
        "dead_kept": by_status.get("dead_kept", 0),
        "skipped": by_status.get("skipped", 0),
        "delete_candidates": delete_candidates,
        "access_token_saved": access_token_saved,
        "by_status": by_status,
        "by_result": by_result,
    }


def create_icloud_hme_recheck_campaign(
    *,
    campaign_id: str = "",
    purpose: str = "chatgpt_register",
    bound_service: str = "chatgpt",
    forward_to: str = "",
    include_ready_stock: bool = False,
    include_in_flight: bool = False,
    reset_existing: bool = False,
) -> dict[str, Any]:
    normalized_campaign = str(campaign_id or "").strip()
    if not normalized_campaign:
        normalized_campaign = f"hme_recheck_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    normalized_purpose = str(purpose or "chatgpt_register").strip() or "chatgpt_register"
    normalized_service = str(bound_service or "chatgpt").strip() or "chatgpt"
    normalized_forward_to = str(forward_to or "").strip()
    now = _utcnow()

    with Session(engine) as session:
        alias_query = select(IcloudHmeAliasModel).where(
            IcloudHmeAliasModel.purpose == normalized_purpose,
            IcloudHmeAliasModel.bound_service == normalized_service,
        )
        if normalized_forward_to:
            alias_query = alias_query.where(IcloudHmeAliasModel.forward_to == normalized_forward_to)
        aliases = session.exec(alias_query.order_by(IcloudHmeAliasModel.id.asc())).all()
        accounts = session.exec(
            select(AccountModel).where(AccountModel.platform == "chatgpt")
        ).all()
        accounts_by_email: dict[str, list[AccountModel]] = {}
        for account in accounts:
            key = _norm_alias_email(getattr(account, "email", ""))
            if key:
                accounts_by_email.setdefault(key, []).append(account)

        inserted = 0
        updated = 0
        skipped = 0
        skipped_reasons: dict[str, int] = {}
        for alias in aliases:
            status = str(getattr(alias, "status", "") or "").strip()
            task_id = str(getattr(alias, "task_id", "") or "").strip()
            bound_email = str(getattr(alias, "bound_account_email", "") or "").strip()
            hme = str(getattr(alias, "hme", "") or "").strip()
            anonymous_id = str(getattr(alias, "anonymous_id", "") or "").strip()
            if not hme or not anonymous_id:
                skipped += 1
                skipped_reasons["missing_hme_or_anonymous_id"] = skipped_reasons.get("missing_hme_or_anonymous_id", 0) + 1
                continue
            if status == "retired":
                skipped += 1
                skipped_reasons["retired"] = skipped_reasons.get("retired", 0) + 1
                continue
            if not include_in_flight and (status == "in_use" or (status == "reserved" and task_id)):
                skipped += 1
                skipped_reasons["in_flight"] = skipped_reasons.get("in_flight", 0) + 1
                continue
            ready_stock = bool(getattr(alias, "enabled", False)) and status == "reserved" and not task_id and not bound_email
            if ready_stock and not include_ready_stock:
                skipped += 1
                skipped_reasons["ready_stock"] = skipped_reasons.get("ready_stock", 0) + 1
                continue

            match_keys = {_norm_alias_email(hme), _norm_alias_email(bound_email)} - {""}
            matched_accounts: list[AccountModel] = []
            for key in match_keys:
                matched_accounts.extend(accounts_by_email.get(key, []))
            matched_accounts = sorted(
                {int(acc.id or 0): acc for acc in matched_accounts if int(acc.id or 0) > 0}.values(),
                key=lambda acc: int(acc.id or 0),
            )
            primary = matched_accounts[0] if matched_accounts else None
            source_type = "both" if primary is not None else "icloud_orphan"

            existing = session.exec(
                select(IcloudHmeRecheckQueueModel).where(
                    IcloudHmeRecheckQueueModel.campaign_id == normalized_campaign,
                    IcloudHmeRecheckQueueModel.anonymous_id == anonymous_id,
                )
            ).first()
            details = {
                "alias_status": status,
                "alias_enabled": bool(getattr(alias, "enabled", False)),
                "alias_created_source": str(getattr(alias, "created_source", "") or ""),
                "alias_record_source": str(getattr(alias, "record_source", "") or ""),
                "alias_use_count": int(getattr(alias, "use_count", 0) or 0),
                "alias_task_id": task_id,
                "alias_bound_account_email": bound_email,
                "alias_last_otp_at": str(getattr(alias, "last_otp_at", "") or ""),
                "alias_last_error": str(getattr(alias, "last_error", "") or ""),
                "matched_account_ids": [int(acc.id or 0) for acc in matched_accounts],
                "matched_account_statuses": [str(acc.status or "") for acc in matched_accounts],
                "ready_stock": ready_stock,
            }
            if existing is None:
                existing = IcloudHmeRecheckQueueModel(
                    campaign_id=normalized_campaign,
                    anonymous_id=anonymous_id,
                    hme=hme,
                    account_id=int(getattr(primary, "id", 0) or 0) if primary is not None else 0,
                    account_email=str(getattr(primary, "email", "") or "") if primary is not None else "",
                    source_type=source_type,
                    status="pending",
                    apple_delete_status="not_requested",
                    created_at=now,
                    updated_at=now,
                )
                existing.set_details(details)
                session.add(existing)
                inserted += 1
            else:
                existing.hme = hme
                existing.account_id = int(getattr(primary, "id", 0) or 0) if primary is not None else int(existing.account_id or 0)
                existing.account_email = str(getattr(primary, "email", "") or "") if primary is not None else str(existing.account_email or "")
                existing.source_type = source_type
                existing.set_details({**existing.get_details(), **details})
                if reset_existing:
                    existing.status = "pending"
                    existing.result_code = ""
                    existing.result_message = ""
                    existing.saved_account_id = 0
                    existing.access_token_saved = False
                    existing.delete_candidate = False
                    existing.delete_candidate_reason = ""
                    existing.apple_delete_status = "not_requested"
                    existing.last_error = ""
                    existing.checked_at = ""
                    existing.started_at = ""
                existing.updated_at = now
                session.add(existing)
                updated += 1
        session.commit()
        summary = _recheck_queue_summary_for_campaign(session, normalized_campaign)

    return {
        "campaign_id": normalized_campaign,
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
        "skipped_reasons": skipped_reasons,
        "summary": summary,
    }


def list_icloud_hme_recheck_campaigns(limit: int = 20) -> dict[str, Any]:
    limit_value = max(1, min(int(limit or 20), 100))
    with Session(engine) as session:
        rows = session.exec(select(IcloudHmeRecheckQueueModel)).all()
        campaign_ids: list[str] = []
        seen: set[str] = set()
        for row in sorted(rows, key=lambda item: str(getattr(item, "created_at", "") or ""), reverse=True):
            cid = str(getattr(row, "campaign_id", "") or "").strip()
            if not cid or cid in seen:
                continue
            seen.add(cid)
            campaign_ids.append(cid)
            if len(campaign_ids) >= limit_value:
                break
        data = []
        for cid in campaign_ids:
            data.append(_recheck_queue_summary_for_campaign(session, cid))
    return {"data": data, "total": len(data)}


def get_icloud_hme_recheck_campaign(
    campaign_id: str = "",
    *,
    page: int = 1,
    size: int = 20,
    status: str = "",
    result_code: str = "",
    delete_candidate: str = "",
    hme: str = "",
) -> dict[str, Any]:
    page_value = max(int(page or 1), 1)
    size_value = max(1, min(int(size or 20), 100))
    with Session(engine) as session:
        normalized_campaign = str(campaign_id or "").strip() or _latest_icloud_hme_recheck_campaign_id(session)
        if not normalized_campaign:
            return {"campaign_id": "", "summary": {}, "data": [], "total": 0, "page": page_value, "size": size_value, "pages": 0}
        query = select(IcloudHmeRecheckQueueModel).where(IcloudHmeRecheckQueueModel.campaign_id == normalized_campaign)
        if str(status or "").strip():
            query = query.where(IcloudHmeRecheckQueueModel.status == str(status).strip())
        if str(result_code or "").strip():
            query = query.where(IcloudHmeRecheckQueueModel.result_code == str(result_code).strip())
        delete_text = str(delete_candidate or "").strip().lower()
        if delete_text in {"1", "true", "yes", "on"}:
            query = query.where(IcloudHmeRecheckQueueModel.delete_candidate == True)
        elif delete_text in {"0", "false", "no", "off"}:
            query = query.where(IcloudHmeRecheckQueueModel.delete_candidate == False)
        if str(hme or "").strip():
            query = query.where(IcloudHmeRecheckQueueModel.hme.contains(str(hme).strip()))
        rows_all = session.exec(query.order_by(IcloudHmeRecheckQueueModel.id.asc())).all()
        total = len(rows_all)
        rows = rows_all[(page_value - 1) * size_value: page_value * size_value]
        summary = _recheck_queue_summary_for_campaign(session, normalized_campaign)
    return {
        "campaign_id": normalized_campaign,
        "summary": summary,
        "data": [_row_to_icloud_hme_recheck_payload(row) for row in rows],
        "total": total,
        "page": page_value,
        "size": size_value,
        "pages": ceil(total / size_value) if size_value else 0,
    }


def claim_icloud_hme_recheck_items(
    campaign_id: str,
    *,
    limit: int = 20,
    include_retry: bool = False,
    task_id: str = "",
) -> list[dict[str, Any]]:
    normalized_campaign = str(campaign_id or "").strip()
    if not normalized_campaign:
        return []
    limit_value = max(1, min(int(limit or 20), 200))
    now = _utcnow()
    status_values = ["pending"]
    if include_retry:
        status_values.extend(["retry", "failed"])
    with Session(engine) as session:
        rows = session.exec(
            select(IcloudHmeRecheckQueueModel)
            .where(IcloudHmeRecheckQueueModel.campaign_id == normalized_campaign)
            .where(IcloudHmeRecheckQueueModel.status.in_(status_values))
            .order_by(IcloudHmeRecheckQueueModel.id.asc())
            .limit(limit_value)
        ).all()
        claimed = []
        for row in rows:
            row.status = "running"
            row.started_at = now.isoformat()
            row.last_task_id = str(task_id or row.last_task_id or "")
            row.attempt_count = int(row.attempt_count or 0) + 1
            row.updated_at = now
            session.add(row)
            claimed.append(row)
        session.commit()
        for row in claimed:
            session.refresh(row)
            # detach-safe payload
        return [_row_to_icloud_hme_recheck_payload(row) for row in claimed]


def update_icloud_hme_recheck_item(
    item_id: int,
    *,
    status: str,
    result_code: str = "",
    result_message: str = "",
    saved_account_id: int = 0,
    access_token_saved: bool = False,
    delete_candidate: bool = False,
    delete_candidate_reason: str = "",
    last_task_id: str = "",
    last_error: str = "",
    details: dict[str, Any] | None = None,
    checked: bool = True,
) -> dict[str, Any]:
    now = _utcnow()
    with Session(engine) as session:
        row = session.get(IcloudHmeRecheckQueueModel, int(item_id or 0))
        if row is None:
            raise LookupError("icloud hme recheck item not found")
        row.status = str(status or "").strip() or row.status
        row.result_code = str(result_code or "")[:200]
        row.result_message = str(result_message or "")[:1000]
        row.saved_account_id = int(saved_account_id or 0)
        row.access_token_saved = bool(access_token_saved)
        row.delete_candidate = bool(delete_candidate)
        row.delete_candidate_reason = str(delete_candidate_reason or "")[:500]
        if last_task_id:
            row.last_task_id = str(last_task_id or "")
        row.last_error = str(last_error or "")[:1000]
        if details:
            row.set_details({**row.get_details(), **dict(details or {})})
        if checked:
            row.checked_at = now.isoformat()
        row.updated_at = now
        session.add(row)
        session.commit()
        session.refresh(row)
        return _row_to_icloud_hme_recheck_payload(row)


def release_stale_icloud_hme_recheck_running(campaign_id: str = "", *, older_than_seconds: int = 7200) -> dict[str, Any]:
    normalized_campaign = str(campaign_id or "").strip()
    cutoff = datetime.now(timezone.utc).timestamp() - max(int(older_than_seconds or 7200), 60)
    released = 0
    with Session(engine) as session:
        query = select(IcloudHmeRecheckQueueModel).where(IcloudHmeRecheckQueueModel.status == "running")
        if normalized_campaign:
            query = query.where(IcloudHmeRecheckQueueModel.campaign_id == normalized_campaign)
        rows = session.exec(query).all()
        for row in rows:
            started = str(getattr(row, "started_at", "") or "").strip()
            try:
                parsed = datetime.fromisoformat(started.replace("Z", "+00:00")) if started else None
                ts = parsed.timestamp() if parsed else 0
            except Exception:
                ts = 0
            if ts and ts > cutoff:
                continue
            row.status = "retry"
            row.last_error = "running item released after stale timeout"
            row.updated_at = _utcnow()
            session.add(row)
            released += 1
        session.commit()
    return {"released": released, "campaign_id": normalized_campaign}


def reset_icloud_hme_aliases_for_rerun(
    *,
    campaign_id: str = "",
    purpose: str = "chatgpt_register",
    bound_service: str = "chatgpt",
    forward_to: str = "",
    include_in_flight: bool = False,
    include_ready_stock: bool = False,
    reset_existing_queue: bool = True,
    dry_run: bool = False,
    limit: int = 0,
) -> dict[str, Any]:
    """把已经领取/使用过的 iCloud HME 本地重置回导入池。

    只改本地 DB：不会调用 Apple deactivate/delete，也不会删除 ChatGPT 账号。
    同时创建一个最新 rerun campaign，用来记录后续注册任务的成功/失败分类。
    """
    normalized_campaign = str(campaign_id or "").strip()
    if not normalized_campaign:
        normalized_campaign = f"hme_rerun_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    normalized_purpose = str(purpose or "chatgpt_register").strip() or "chatgpt_register"
    normalized_service = str(bound_service or "chatgpt").strip() or "chatgpt"
    normalized_forward_to = str(forward_to or "").strip()
    limit_value = max(int(limit or 0), 0)
    now = _utcnow()
    now_text = now.isoformat()

    with Session(engine) as session:
        alias_query = select(IcloudHmeAliasModel).where(
            IcloudHmeAliasModel.purpose == normalized_purpose,
            IcloudHmeAliasModel.bound_service == normalized_service,
        )
        if normalized_forward_to:
            alias_query = alias_query.where(IcloudHmeAliasModel.forward_to == normalized_forward_to)
        aliases = session.exec(alias_query.order_by(IcloudHmeAliasModel.id.asc())).all()
        accounts = session.exec(select(AccountModel).where(AccountModel.platform == "chatgpt")).all()
        accounts_by_email: dict[str, list[AccountModel]] = {}
        for account in accounts:
            key = _norm_alias_email(getattr(account, "email", ""))
            if key:
                accounts_by_email.setdefault(key, []).append(account)

        matched = 0
        reset_count = 0
        inserted = 0
        updated = 0
        skipped = 0
        skipped_reasons: dict[str, int] = {}
        preview_rows: list[dict[str, Any]] = []

        for alias in aliases:
            status = str(getattr(alias, "status", "") or "").strip()
            task_id_text = str(getattr(alias, "task_id", "") or "").strip()
            bound_email = str(getattr(alias, "bound_account_email", "") or "").strip()
            hme = str(getattr(alias, "hme", "") or "").strip()
            anonymous_id = str(getattr(alias, "anonymous_id", "") or "").strip()
            enabled = bool(getattr(alias, "enabled", False))
            use_count = int(getattr(alias, "use_count", 0) or 0)
            ready_stock = bool(enabled and status == "reserved" and not task_id_text and not bound_email)
            used_by_system = bool(
                use_count > 0
                or task_id_text
                or bound_email
                or status in {"in_use", "registered", "register_failed"}
            )

            reason = ""
            if not hme or not anonymous_id:
                reason = "missing_hme_or_anonymous_id"
            elif status == "retired":
                reason = "retired"
            elif ready_stock and not include_ready_stock:
                reason = "already_ready_stock"
            elif status == "in_use" and not include_in_flight:
                reason = "in_flight"
            elif not used_by_system and not ready_stock:
                reason = "not_claimed"

            if reason:
                skipped += 1
                skipped_reasons[reason] = skipped_reasons.get(reason, 0) + 1
                continue

            if limit_value and matched >= limit_value:
                skipped += 1
                skipped_reasons["limit_exceeded"] = skipped_reasons.get("limit_exceeded", 0) + 1
                continue

            match_keys = {_norm_alias_email(hme), _norm_alias_email(bound_email)} - {""}
            matched_accounts: list[AccountModel] = []
            for key in match_keys:
                matched_accounts.extend(accounts_by_email.get(key, []))
            matched_accounts = sorted(
                {int(acc.id or 0): acc for acc in matched_accounts if int(acc.id or 0) > 0}.values(),
                key=lambda acc: int(acc.id or 0),
            )
            primary = matched_accounts[0] if matched_accounts else None
            source_type = "both" if primary is not None else "icloud_orphan"
            original_payload = _row_to_icloud_hme_alias_payload(alias)
            details = {
                "reset_mode": "alias_pool_rerun",
                "reset_at": now_text,
                "reset_campaign_id": normalized_campaign,
                "original_alias": original_payload,
                "alias_status": status,
                "alias_enabled": enabled,
                "alias_created_source": str(getattr(alias, "created_source", "") or ""),
                "alias_record_source": str(getattr(alias, "record_source", "") or ""),
                "alias_use_count": use_count,
                "alias_task_id": task_id_text,
                "alias_bound_account_email": bound_email,
                "alias_last_otp_at": str(getattr(alias, "last_otp_at", "") or ""),
                "alias_last_error": str(getattr(alias, "last_error", "") or ""),
                "matched_account_ids": [int(acc.id or 0) for acc in matched_accounts],
                "matched_account_statuses": [str(acc.status or "") for acc in matched_accounts],
                "ready_stock": ready_stock,
            }
            matched += 1
            preview_rows.append(original_payload)

            if not dry_run:
                existing = session.exec(
                    select(IcloudHmeRecheckQueueModel).where(
                        IcloudHmeRecheckQueueModel.campaign_id == normalized_campaign,
                        IcloudHmeRecheckQueueModel.anonymous_id == anonymous_id,
                    )
                ).first()
                if existing is None:
                    existing = IcloudHmeRecheckQueueModel(
                        campaign_id=normalized_campaign,
                        anonymous_id=anonymous_id,
                        hme=hme,
                        account_id=int(getattr(primary, "id", 0) or 0) if primary is not None else 0,
                        account_email=str(getattr(primary, "email", "") or "") if primary is not None else "",
                        source_type=source_type,
                        status="pending",
                        apple_delete_status="not_requested",
                        created_at=now,
                        updated_at=now,
                    )
                    existing.set_details(details)
                    session.add(existing)
                    inserted += 1
                else:
                    existing.hme = hme
                    existing.account_id = int(getattr(primary, "id", 0) or 0) if primary is not None else int(existing.account_id or 0)
                    existing.account_email = str(getattr(primary, "email", "") or "") if primary is not None else str(existing.account_email or "")
                    existing.source_type = source_type
                    existing.set_details({**existing.get_details(), **details})
                    if reset_existing_queue:
                        existing.status = "pending"
                        existing.result_code = ""
                        existing.result_message = ""
                        existing.saved_account_id = 0
                        existing.access_token_saved = False
                        existing.delete_candidate = False
                        existing.delete_candidate_reason = ""
                        existing.apple_delete_status = "not_requested"
                        existing.last_error = ""
                        existing.checked_at = ""
                        existing.started_at = ""
                    existing.updated_at = now
                    session.add(existing)
                    updated += 1

                alias.enabled = True
                alias.status = "reserved"
                alias.task_id = ""
                alias.bound_account_email = ""
                alias.bound_account_ref = ""
                alias.use_count = 0
                alias.first_claimed_at = ""
                alias.last_claimed_at = ""
                alias.last_otp_at = ""
                alias.last_error = ""
                alias.updated_at = now
                session.add(alias)
                reset_count += 1

        if not dry_run:
            session.commit()
            summary = _recheck_queue_summary_for_campaign(session, normalized_campaign)
        else:
            summary = {
                "campaign_id": normalized_campaign,
                "total": matched,
                "pending": matched,
                "checked": 0,
                "unchecked": matched,
            }

    return {
        "campaign_id": normalized_campaign,
        "matched": matched,
        "reset": reset_count,
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
        "skipped_reasons": skipped_reasons,
        "dry_run": bool(dry_run),
        "summary": summary,
        "data": preview_rows[:100],
    }


def sync_icloud_hme_rerun_result(
    *,
    anonymous_id: str = "",
    hme: str = "",
    task_id: str = "",
    success: bool = False,
    error_message: str = "",
    saved_account_id: int = 0,
    access_token_saved: bool = False,
    mailbox_state: dict[str, Any] | None = None,
    result_code: str = "",
    delete_candidate: bool = False,
) -> dict[str, Any]:
    normalized_anonymous_id = str(anonymous_id or "").strip()
    normalized_hme = str(hme or "").strip()
    if not normalized_anonymous_id and not normalized_hme:
        return {"updated": 0, "reason": "missing_alias_identity"}

    now = _utcnow()
    now_text = now.isoformat()
    error_text = str(error_message or "").strip()
    normalized_result_code = str(result_code or "").strip()
    registered_auth_pending = bool(
        success and normalized_result_code == "registered_auth_pending"
    )
    try:
        from services.chatgpt_account_state import is_account_deactivated_message

        detected_delete_candidate = is_account_deactivated_message(str(result_code or ""), error_text)
    except Exception:
        lowered = error_text.lower()
        detected_delete_candidate = any(marker in lowered for marker in ("account_deactivated", "account deleted", "account_disabled"))
    delete_candidate = bool(delete_candidate or detected_delete_candidate)

    with Session(engine) as session:
        latest_campaign_id = _latest_icloud_hme_recheck_campaign_id(session)
        if not latest_campaign_id:
            return {"updated": 0, "reason": "no_campaign"}
        query = select(IcloudHmeRecheckQueueModel).where(
            IcloudHmeRecheckQueueModel.campaign_id == latest_campaign_id
        )
        if normalized_anonymous_id:
            query = query.where(IcloudHmeRecheckQueueModel.anonymous_id == normalized_anonymous_id)
        else:
            query = query.where(IcloudHmeRecheckQueueModel.hme == normalized_hme)
        row = session.exec(query.order_by(IcloudHmeRecheckQueueModel.id.desc())).first()
        if row is None and normalized_hme:
            target = _norm_alias_email(normalized_hme)
            rows = session.exec(
                select(IcloudHmeRecheckQueueModel)
                .where(IcloudHmeRecheckQueueModel.campaign_id == latest_campaign_id)
            ).all()
            row = next((item for item in rows if _norm_alias_email(getattr(item, "hme", "")) == target), None)
        if row is None:
            return {"updated": 0, "reason": "queue_item_not_found", "campaign_id": latest_campaign_id}

        if success:
            row.status = "registered_auth_pending" if registered_auth_pending else "alive"
            row.result_code = str(result_code or "login_alive")[:200]
            row.result_message = (
                "远端注册已完成，认证材料待补抓"
                if registered_auth_pending
                else "账号可登录，已由注册流程重新保存"
            )
            if int(saved_account_id or 0) > 0:
                row.saved_account_id = int(saved_account_id or 0)
            if registered_auth_pending:
                row.access_token_saved = bool(access_token_saved)
            elif access_token_saved or int(saved_account_id or 0) > 0:
                row.access_token_saved = True
            row.delete_candidate = False
            row.delete_candidate_reason = ""
            row.last_error = ""
        else:
            row.status = "delete_candidate" if delete_candidate else "retry"
            row.result_code = str(result_code or ("account_deactivated" if delete_candidate else "register_failed"))[:200]
            row.result_message = error_text[:1000]
            row.delete_candidate = bool(delete_candidate)
            row.delete_candidate_reason = "account_deleted_or_deactivated" if delete_candidate else ""
            row.last_error = error_text[:1000]

        row.last_task_id = str(task_id or row.last_task_id or "")
        row.checked_at = now_text
        row.updated_at = now
        details = row.get_details()
        details.update({
            "rerun_register_flow": True,
            "last_rerun_task_id": str(task_id or ""),
            "last_rerun_success": bool(success),
            "last_rerun_auth_pending": registered_auth_pending,
            "last_rerun_error": error_text,
            "last_rerun_at": now_text,
        })
        if mailbox_state:
            # Queue details are audit metadata, not a second mailbox-recovery
            # store.  Persist only the bounded summary so a rerun cannot
            # duplicate global registration configuration into this table.
            try:
                from services.chatgpt_core.mailbox_state import mailbox_state_summary

                summary = mailbox_state_summary(mailbox_state, account_email=normalized_hme)
            except Exception:
                summary = {}
            if summary:
                details["mailbox_state"] = summary
        row.set_details(details)
        session.add(row)

        invalidated_accounts: list[int] = []
        if delete_candidate:
            account_ids = []
            for raw in (details.get("matched_account_ids") or []):
                try:
                    value = int(raw or 0)
                except Exception:
                    value = 0
                if value > 0 and value not in account_ids:
                    account_ids.append(value)
            email_keys = {_norm_alias_email(normalized_hme), _norm_alias_email(getattr(row, "hme", ""))} - {""}
            if email_keys:
                account_rows = session.exec(select(AccountModel).where(AccountModel.platform == "chatgpt")).all()
                for account in account_rows:
                    if _norm_alias_email(getattr(account, "email", "")) in email_keys:
                        value = int(account.id or 0)
                        if value > 0 and value not in account_ids:
                            account_ids.append(value)
            for account_id in account_ids:
                account = session.get(AccountModel, account_id)
                if account is None or account.platform != "chatgpt":
                    continue
                try:
                    extra = account.get_extra()
                except Exception:
                    extra = {}
                if not isinstance(extra, dict):
                    extra = {}
                extra["chatgpt_hme_rerun_delete_candidate"] = {
                    "result_code": row.result_code,
                    "message": row.result_message,
                    "task_id": str(task_id or ""),
                    "marked_at": now_text,
                    "delete_mode": "mark_only",
                    "campaign_id": latest_campaign_id,
                }
                capabilities = extra.get("chatgpt_capabilities") if isinstance(extra.get("chatgpt_capabilities"), dict) else {}
                capabilities = dict(capabilities or {})
                capabilities["auth_level"] = "invalid"
                capabilities["upload_gate"] = "blocked_auth_invalid"
                extra["chatgpt_capabilities"] = capabilities
                account.status = "invalid"
                account.set_extra(extra)
                account.updated_at = now
                session.add(account)
                invalidated_accounts.append(account_id)

        session.commit()
        session.refresh(row)
        return {
            "updated": 1,
            "campaign_id": latest_campaign_id,
            "item": _row_to_icloud_hme_recheck_payload(row),
            "invalidated_account_ids": invalidated_accounts,
        }


def patch_icloud_hme_alias(
    anonymous_id: str,
    updates: dict[str, Any],
    *,
    allow_internal: bool = False,
) -> dict[str, Any]:
    normalized = str(anonymous_id or "").strip()
    if not normalized:
        raise ValueError("anonymous_id is required")
    if not isinstance(updates, dict) or not updates:
        raise ValueError("updates are required")

    allowed_fields = {
        "status",
        "note",
        "purpose",
        "bound_service",
        "bound_account_email",
        "bound_account_ref",
        "enabled",
    }
    if allow_internal:
        allowed_fields = set(allowed_fields) | {
            "task_id",
            "last_otp_at",
            "last_error",
            "forward_to",
            "label",
            "hme",
            "created_source",
            "record_source",
            "use_count",
            "first_claimed_at",
            "last_claimed_at",
            "last_synced_at",
        }

    invalid_fields = [key for key in updates.keys() if key not in allowed_fields]
    if invalid_fields:
        raise ValueError(f"unsupported fields: {', '.join(sorted(invalid_fields))}")

    with Session(engine) as session:
        row = session.exec(
            select(IcloudHmeAliasModel).where(IcloudHmeAliasModel.anonymous_id == normalized)
        ).first()
        if row is None:
            raise LookupError("icloud hme alias not found")

        for key, value in updates.items():
            if key == "enabled":
                setattr(row, key, bool(value))
                continue
            setattr(row, key, str(value or "") if key != "status" else str(value or "").strip())
        row.updated_at = _utcnow()
        session.add(row)
        session.commit()
        session.refresh(row)
        return _row_to_icloud_hme_alias_payload(row)


def mark_icloud_hme_alias_retired(anonymous_id: str, *, reason: str = "") -> dict[str, Any]:
    """把别名标记为已退役（已在 Apple 端 deactivate+delete）：status=retired, enabled=False。

    本地保留行作为审计记录；retired 行会被 ready/候选查询排除，不会再被 sync-live 当作可用。
    """
    updates: dict[str, Any] = {"status": "retired", "enabled": False}
    if str(reason or "").strip():
        updates["last_error"] = str(reason).strip()[:500]
    return patch_icloud_hme_alias(anonymous_id, updates, allow_internal=True)


def import_icloud_hme_alias_rows(
    rows: list[dict[str, Any]],
    *,
    purpose: str = "chatgpt_register",
    bound_service: str = "chatgpt",
    default_forward_to: str = "b@cccy.me",
) -> dict[str, Any]:
    inserted = 0
    updated = 0
    skipped = 0
    imported: list[dict[str, Any]] = []
    for item in rows:
        anonymous_id = str((item or {}).get("anonymous_id") or "").strip()
        hme = str((item or {}).get("hme") or "").strip()
        if not anonymous_id or not hme:
            skipped += 1
            continue

        existing = get_icloud_hme_alias_by_anonymous_id(anonymous_id)
        status = str((item or {}).get("status") or "reserved").strip() or "reserved"
        record = insert_icloud_hme_alias(
            anonymous_id=anonymous_id,
            hme=hme,
            label=str((item or {}).get("label") or ""),
            note=str((item or {}).get("note") or ""),
            forward_to=str((item or {}).get("forward_to") or default_forward_to or ""),
            enabled=bool((item or {}).get("enabled", False)),
            created_source=str((item or {}).get("created_source") or "unknown"),
            record_source=str((item or {}).get("record_source") or "csv_import"),
            purpose=str((item or {}).get("purpose") or purpose or "chatgpt_register"),
            bound_service=str((item or {}).get("bound_service") or bound_service or "chatgpt"),
            bound_account_email=str((item or {}).get("bound_account_email") or ""),
            bound_account_ref=str((item or {}).get("bound_account_ref") or ""),
            task_id=str((item or {}).get("task_id") or ""),
            status=status,
            use_count=int((item or {}).get("use_count") or 0),
            first_claimed_at=str((item or {}).get("first_claimed_at") or ""),
            last_claimed_at=str((item or {}).get("last_claimed_at") or ""),
            last_synced_at=str((item or {}).get("last_synced_at") or ""),
            last_otp_at=str((item or {}).get("last_otp_at") or ""),
            last_error=str((item or {}).get("last_error") or ""),
        )
        if existing:
            updated += 1
        else:
            inserted += 1
        imported.append(record)

    return {
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
        "total": len(rows or []),
        "data": imported,
    }


def claim_icloud_hme_alias(
    *,
    task_id: str = "",
    purpose: str = "chatgpt_register",
    bound_service: str = "chatgpt",
    forward_to: str = "",
) -> dict[str, Any] | None:
    normalized_task_id = str(task_id or "").strip()
    normalized_purpose = str(purpose or "chatgpt_register").strip() or "chatgpt_register"
    normalized_service = str(bound_service or "chatgpt").strip() or "chatgpt"
    normalized_forward_to = str(forward_to or "").strip()
    now = _utcnow().isoformat()

    sql = """
        SELECT id, anonymous_id, hme, label, note, forward_to, enabled, purpose, bound_service,
               bound_account_email, bound_account_ref, task_id, status, last_otp_at,
               last_error, created_at, updated_at
        FROM icloud_hme_alias
        WHERE status = ?
          AND enabled = 1
          AND purpose = ?
          AND bound_service = ?
          AND COALESCE(task_id, '') = ''
          AND COALESCE(bound_account_email, '') = ''
    """
    params: list[Any] = ["reserved", normalized_purpose, normalized_service]
    if normalized_forward_to:
        sql += " AND forward_to = ?"
        params.append(normalized_forward_to)
    sql += " ORDER BY id ASC LIMIT 25"

    with engine.begin() as conn:
        rows = conn.exec_driver_sql(sql, tuple(params)).fetchall()
        for row in rows:
            row_payload = _row_to_icloud_hme_alias_payload(row)
            update_result = conn.exec_driver_sql(
                """
                UPDATE icloud_hme_alias
                SET status = ?, task_id = ?, use_count = COALESCE(use_count, 0) + 1,
                    first_claimed_at = CASE WHEN COALESCE(first_claimed_at, '') = '' THEN ? ELSE first_claimed_at END,
                    last_claimed_at = ?, updated_at = ?
                WHERE id = ? AND status = ? AND COALESCE(task_id, '') = ''
                """,
                ("in_use", normalized_task_id, now, now, now, row_payload.get("id"), "reserved"),
            )
            if int(getattr(update_result, "rowcount", 0) or 0) != 1:
                continue
            claimed = conn.exec_driver_sql(
                """
                SELECT id, anonymous_id, hme, label, note, forward_to, enabled, purpose, bound_service,
                       bound_account_email, bound_account_ref, task_id, status, last_otp_at,
                       last_error, created_at, updated_at
                FROM icloud_hme_alias
                WHERE id = ?
                """,
                (row_payload.get("id"),),
            ).fetchone()
            return _row_to_icloud_hme_alias_payload(claimed)
    return None


def set_icloud_hme_alias_enabled(anonymous_id: str, enabled: bool) -> dict[str, Any]:
    if bool(enabled):
        existing = get_icloud_hme_alias_by_anonymous_id(anonymous_id)
        status = str((existing or {}).get("status") or "").strip()
        if status in {"account_deactivated", "account_disabled"}:
            raise ValueError("账号已禁用/死号，不允许重新启用到邮箱池")
    return patch_icloud_hme_alias(
        anonymous_id,
        {"enabled": bool(enabled)},
        allow_internal=True,
    )


def bulk_enable_icloud_hme_aliases(
    *,
    forward_to: str = "",
    only_manual_created: bool = False,
    only_unused: bool = True,
) -> dict[str, Any]:
    normalized_forward_to = str(forward_to or "").strip()
    enabled_count = 0
    recycled_count = 0
    matched_count = 0
    updated_rows: list[dict[str, Any]] = []

    with Session(engine) as session:
        rows = session.exec(select(IcloudHmeAliasModel)).all()
        for row in rows:
            if normalized_forward_to and str(getattr(row, "forward_to", "") or "").strip() != normalized_forward_to:
                continue
            if only_manual_created and str(getattr(row, "created_source", "") or "").strip() != "manual_created":
                continue

            payload = _row_to_icloud_hme_alias_payload(row)
            status_text = str(getattr(row, "status", "") or "").strip()
            task_id_text = str(getattr(row, "task_id", "") or "").strip()
            bound_account_email_text = str(getattr(row, "bound_account_email", "") or "").strip()
            last_error_text = str(getattr(row, "last_error", "") or "").strip()
            try:
                from services.chatgpt_account_state import is_account_deactivated_message

                deactivated_failed = is_account_deactivated_message("", last_error_text)
            except Exception:
                lowered_error = last_error_text.lower()
                deactivated_failed = "account_deactivated" in lowered_error or "deleted or deactivated" in lowered_error
            recyclable_failed = status_text == "register_failed" and not deactivated_failed

            if only_unused:
                if status_text in {"registered", "in_use", "retired", "account_deactivated", "account_disabled"}:
                    continue
                if deactivated_failed:
                    continue
                if not recyclable_failed and (task_id_text or bound_account_email_text):
                    continue

            matched_count += 1
            changed = False

            if recyclable_failed:
                row.status = "reserved"
                row.task_id = ""
                row.last_error = ""
                row.bound_account_email = ""
                row.bound_account_ref = ""
                changed = True
                recycled_count += 1

            if bool(getattr(row, "enabled", False)):
                if changed:
                    row.updated_at = _utcnow()
                    session.add(row)
                updated_rows.append(_row_to_icloud_hme_alias_payload(row))
                continue

            row.enabled = True
            changed = True
            enabled_count += 1
            row.updated_at = _utcnow()
            session.add(row)
            updated_rows.append(_row_to_icloud_hme_alias_payload(row))
        session.commit()

    return {
        "matched": matched_count,
        "enabled": enabled_count,
        "recycled": recycled_count,
        "data": updated_rows,
    }


def bulk_disable_used_icloud_hme_aliases(
    *,
    forward_to: str = "",
) -> dict[str, Any]:
    normalized_forward_to = str(forward_to or "").strip()
    disabled_count = 0
    matched_count = 0
    updated_rows: list[dict[str, Any]] = []

    with Session(engine) as session:
        rows = session.exec(select(IcloudHmeAliasModel)).all()
        for row in rows:
            if normalized_forward_to and str(getattr(row, "forward_to", "") or "").strip() != normalized_forward_to:
                continue
            payload = _row_to_icloud_hme_alias_payload(row)
            if not bool(payload.get("used_by_system")):
                continue
            matched_count += 1
            if not bool(getattr(row, "enabled", False)):
                updated_rows.append(payload)
                continue
            row.enabled = False
            row.updated_at = _utcnow()
            session.add(row)
            disabled_count += 1
            updated_rows.append(_row_to_icloud_hme_alias_payload(row))
        session.commit()

    return {
        "matched": matched_count,
        "disabled": disabled_count,
        "data": updated_rows,
    }


def mark_icloud_hme_alias_used(
    anonymous_id: str,
    *,
    note: str = "",
) -> dict[str, Any]:
    normalized = str(anonymous_id or "").strip()
    if not normalized:
        raise ValueError("anonymous_id is required")

    with Session(engine) as session:
        row = session.exec(
            select(IcloudHmeAliasModel).where(IcloudHmeAliasModel.anonymous_id == normalized)
        ).first()
        if row is None:
            raise LookupError("icloud hme alias not found")
        row.use_count = int(getattr(row, "use_count", 0) or 0) + 1
        row.enabled = False
        if str(getattr(row, "status", "") or "").strip() == "reserved":
            row.status = "in_use"
        if note:
            row.note = str(note).strip()
        row.updated_at = _utcnow()
        session.add(row)
        session.commit()
        session.refresh(row)
        return _row_to_icloud_hme_alias_payload(row)


def _ensure_phone_pool_schema() -> None:
    """创建/补齐 relay 自有手机号池表。"""
    required_columns = {
        "api_url": "TEXT NOT NULL DEFAULT ''",
        "api_host": "TEXT NOT NULL DEFAULT ''",
        "api_expired_date": "TEXT NOT NULL DEFAULT ''",
        "api_expiry_checked_at": "TEXT NOT NULL DEFAULT ''",
        "api_expiry_status": "TEXT NOT NULL DEFAULT ''",
        "api_expiry_error": "TEXT NOT NULL DEFAULT ''",
        "label": "TEXT NOT NULL DEFAULT ''",
        "status": "TEXT NOT NULL DEFAULT 'active'",
        "bound_count": "INTEGER NOT NULL DEFAULT 0",
        "bound_account_emails_json": "TEXT NOT NULL DEFAULT '[]'",
        "max_accounts": "INTEGER NOT NULL DEFAULT 3",
        "success_count": "INTEGER NOT NULL DEFAULT 0",
        "fail_count": "INTEGER NOT NULL DEFAULT 0",
        "last_error_code": "TEXT NOT NULL DEFAULT ''",
        "last_error_message": "TEXT NOT NULL DEFAULT ''",
        "cooldown_until": "TEXT NOT NULL DEFAULT ''",
        "last_used_at": "TEXT NOT NULL DEFAULT ''",
        "created_at": "TIMESTAMP NULL",
        "updated_at": "TIMESTAMP NULL",
    }

    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS phone_pool (
                id INTEGER PRIMARY KEY,
                phone_e164 TEXT NOT NULL UNIQUE,
                api_url TEXT NOT NULL DEFAULT '',
                api_host TEXT NOT NULL DEFAULT '',
                api_expired_date TEXT NOT NULL DEFAULT '',
                api_expiry_checked_at TEXT NOT NULL DEFAULT '',
                api_expiry_status TEXT NOT NULL DEFAULT '',
                api_expiry_error TEXT NOT NULL DEFAULT '',
                label TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                bound_count INTEGER NOT NULL DEFAULT 0,
                bound_account_emails_json TEXT NOT NULL DEFAULT '[]',
                max_accounts INTEGER NOT NULL DEFAULT 3,
                success_count INTEGER NOT NULL DEFAULT 0,
                fail_count INTEGER NOT NULL DEFAULT 0,
                last_error_code TEXT NOT NULL DEFAULT '',
                last_error_message TEXT NOT NULL DEFAULT '',
                cooldown_until TEXT NOT NULL DEFAULT '',
                last_used_at TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMP NULL,
                updated_at TIMESTAMP NULL
            )
            """
        )
        existing_columns = {
            str(row[1])
            for row in conn.exec_driver_sql("PRAGMA table_info(phone_pool)").fetchall()
        }
        for column_name, ddl in required_columns.items():
            if column_name in existing_columns:
                continue
            conn.exec_driver_sql(f"ALTER TABLE phone_pool ADD COLUMN {column_name} {ddl}")
        conn.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_phone_pool_phone_e164 ON phone_pool(phone_e164)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_phone_pool_status ON phone_pool(status)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_phone_pool_api_host ON phone_pool(api_host)"
        )


def _ensure_phone_prefix_state_schema() -> None:
    """创建/补齐手机号号段状态表。"""
    required_columns = {
        "purpose": "TEXT NOT NULL DEFAULT 'phone_signup'",
        "prefix": "TEXT NOT NULL DEFAULT ''",
        "status": "TEXT NOT NULL DEFAULT 'untested'",
        "success_count": "INTEGER NOT NULL DEFAULT 0",
        "failure_count": "INTEGER NOT NULL DEFAULT 0",
        "last_success_phone": "TEXT NOT NULL DEFAULT ''",
        "last_failure_phone": "TEXT NOT NULL DEFAULT ''",
        "last_error_code": "TEXT NOT NULL DEFAULT ''",
        "last_error_message": "TEXT NOT NULL DEFAULT ''",
        "last_seen_at": "TEXT NOT NULL DEFAULT ''",
        "created_at": "TIMESTAMP NULL",
        "updated_at": "TIMESTAMP NULL",
    }
    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS phone_prefix_state (
                id INTEGER PRIMARY KEY,
                purpose TEXT NOT NULL DEFAULT 'phone_signup',
                prefix TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'untested',
                success_count INTEGER NOT NULL DEFAULT 0,
                failure_count INTEGER NOT NULL DEFAULT 0,
                last_success_phone TEXT NOT NULL DEFAULT '',
                last_failure_phone TEXT NOT NULL DEFAULT '',
                last_error_code TEXT NOT NULL DEFAULT '',
                last_error_message TEXT NOT NULL DEFAULT '',
                last_seen_at TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMP NULL,
                updated_at TIMESTAMP NULL
            )
            """
        )
        existing_columns = {
            str(row[1])
            for row in conn.exec_driver_sql("PRAGMA table_info(phone_prefix_state)").fetchall()
        }
        for column_name, ddl in required_columns.items():
            if column_name in existing_columns:
                continue
            conn.exec_driver_sql(f"ALTER TABLE phone_prefix_state ADD COLUMN {column_name} {ddl}")
        conn.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_phone_prefix_state_purpose_prefix "
            "ON phone_prefix_state(purpose, prefix)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_phone_prefix_state_status "
            "ON phone_prefix_state(status)"
        )


def _ensure_baxigpt_cdk_pool_schema() -> None:
    """创建/补齐 BaxiGPT 卡密池表。"""
    required_columns = {
        "code_value": "TEXT NOT NULL DEFAULT ''",
        "code_hash": "TEXT NOT NULL DEFAULT ''",
        "code_masked": "TEXT NOT NULL DEFAULT ''",
        "label": "TEXT NOT NULL DEFAULT ''",
        "status": "TEXT NOT NULL DEFAULT 'available'",
        "bound_account_id": "INTEGER NOT NULL DEFAULT 0",
        "bound_account_email": "TEXT NOT NULL DEFAULT ''",
        "bound_at": "TEXT NOT NULL DEFAULT ''",
        "task_id": "TEXT NOT NULL DEFAULT ''",
        "order_id": "TEXT NOT NULL DEFAULT ''",
        "display_id": "TEXT NOT NULL DEFAULT ''",
        "remote_email": "TEXT NOT NULL DEFAULT ''",
        "upstream_status": "TEXT NOT NULL DEFAULT ''",
        "code_info_remaining": "INTEGER NOT NULL DEFAULT 0",
        "code_info_total": "INTEGER NOT NULL DEFAULT 0",
        "submit_response_json": "TEXT NOT NULL DEFAULT '{}'",
        "last_status_response_json": "TEXT NOT NULL DEFAULT '{}'",
        "last_query_response_json": "TEXT NOT NULL DEFAULT '{}'",
        "last_error_code": "TEXT NOT NULL DEFAULT ''",
        "last_error_message": "TEXT NOT NULL DEFAULT ''",
        "submitted_at": "TEXT NOT NULL DEFAULT ''",
        "paid_at": "TEXT NOT NULL DEFAULT ''",
        "last_checked_at": "TEXT NOT NULL DEFAULT ''",
        "created_at": "TIMESTAMP NULL",
        "updated_at": "TIMESTAMP NULL",
    }

    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS baxigpt_cdk_pool (
                id INTEGER PRIMARY KEY,
                code_value TEXT NOT NULL DEFAULT '',
                code_hash TEXT NOT NULL DEFAULT '',
                code_masked TEXT NOT NULL DEFAULT '',
                label TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'available',
                bound_account_id INTEGER NOT NULL DEFAULT 0,
                bound_account_email TEXT NOT NULL DEFAULT '',
                bound_at TEXT NOT NULL DEFAULT '',
                task_id TEXT NOT NULL DEFAULT '',
                order_id TEXT NOT NULL DEFAULT '',
                display_id TEXT NOT NULL DEFAULT '',
                remote_email TEXT NOT NULL DEFAULT '',
                upstream_status TEXT NOT NULL DEFAULT '',
                code_info_remaining INTEGER NOT NULL DEFAULT 0,
                code_info_total INTEGER NOT NULL DEFAULT 0,
                submit_response_json TEXT NOT NULL DEFAULT '{}',
                last_status_response_json TEXT NOT NULL DEFAULT '{}',
                last_query_response_json TEXT NOT NULL DEFAULT '{}',
                last_error_code TEXT NOT NULL DEFAULT '',
                last_error_message TEXT NOT NULL DEFAULT '',
                submitted_at TEXT NOT NULL DEFAULT '',
                paid_at TEXT NOT NULL DEFAULT '',
                last_checked_at TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMP NULL,
                updated_at TIMESTAMP NULL
            )
            """
        )
        existing_columns = {
            str(row[1])
            for row in conn.exec_driver_sql("PRAGMA table_info(baxigpt_cdk_pool)").fetchall()
        }
        for column_name, ddl in required_columns.items():
            if column_name in existing_columns:
                continue
            conn.exec_driver_sql(f"ALTER TABLE baxigpt_cdk_pool ADD COLUMN {column_name} {ddl}")
        conn.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_baxigpt_cdk_pool_code_hash ON baxigpt_cdk_pool(code_hash)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_baxigpt_cdk_pool_status ON baxigpt_cdk_pool(status)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_baxigpt_cdk_pool_account ON baxigpt_cdk_pool(bound_account_id)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_baxigpt_cdk_pool_order ON baxigpt_cdk_pool(order_id)"
        )


def _ensure_delivery_card_schema() -> None:
    """创建交付卡密索引和默认 SKU。"""
    now = _utcnow()
    default_skus = [
        {
            "code": "plus",
            "name": "ChatGPT Plus 账号",
            "platform": "chatgpt",
            "code_prefix": "PLUS",
            "delivery_profile": "chatgpt_basic",
            "sort_policy": "earliest_expiry",
            "eligible_rule_json": json.dumps({
                "subscription_type": "plus",
                "validity": "valid",
                "exclude_unknown": True,
            }, ensure_ascii=False),
        },
        {
            "code": "free",
            "name": "ChatGPT Free 账号",
            "platform": "chatgpt",
            "code_prefix": "FREE",
            "delivery_profile": "email_password",
            "sort_policy": "oldest_created",
            "eligible_rule_json": json.dumps({
                "subscription_type": "free",
                "validity": "valid",
                "exclude_unknown": True,
            }, ensure_ascii=False),
        },
    ]
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_delivery_cards_code_hash ON delivery_cards(code_hash)"
        )
        conn.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_delivery_cards_assigned_account "
            "ON delivery_cards(assigned_account_id) WHERE assigned_account_id > 0"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_delivery_cards_sku_status ON delivery_cards(sku_code, status)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_delivery_card_events_card ON delivery_card_events(card_id)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_delivery_card_events_account ON delivery_card_events(account_id)"
        )
        conn.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_delivery_card_events_success_idempotency "
            "ON delivery_card_events(card_id, idempotency_key) "
            "WHERE idempotency_key != '' AND result = 'success'"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_delivery_redeem_api_logs_trace ON delivery_redeem_api_logs(trace_id)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_delivery_redeem_api_logs_account ON delivery_redeem_api_logs(assigned_account_id)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_delivery_redeem_api_logs_card ON delivery_redeem_api_logs(card_id)"
        )
        for sku in default_skus:
            exists = conn.exec_driver_sql(
                "SELECT id FROM delivery_skus WHERE code = ?",
                (sku["code"],),
            ).fetchone()
            if exists:
                continue
            conn.execute(
                text(
                    """
                    INSERT INTO delivery_skus (
                        code, name, platform, code_prefix, delivery_profile,
                        sort_policy, eligible_rule_json, allow_refetch,
                        max_refetch_count, enabled, note, created_at, updated_at
                    ) VALUES (
                        :code, :name, :platform, :code_prefix, :delivery_profile,
                        :sort_policy, :eligible_rule_json, 1,
                        0, 1, '', :created_at, :updated_at
                    )
                    """
                ),
                {**sku, "created_at": now, "updated_at": now},
            )


def _ensure_account_list_state_schema() -> None:
    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS account_list_state (
                account_id INTEGER PRIMARY KEY,
                platform TEXT NOT NULL DEFAULT '',
                manually_used INTEGER NOT NULL DEFAULT 0,
                auth_type TEXT NOT NULL DEFAULT 'unknown',
                phone_binding_state TEXT NOT NULL DEFAULT 'unknown',
                payment_link_platform TEXT NOT NULL DEFAULT 'none',
                payment_link_generated INTEGER NOT NULL DEFAULT 0,
                auth_level TEXT NOT NULL DEFAULT '',
                subscription_type TEXT NOT NULL DEFAULT 'unknown',
                account_validity TEXT NOT NULL DEFAULT 'valid',
                sub2api_state TEXT NOT NULL DEFAULT 'unknown',
                oaipay_state TEXT NOT NULL DEFAULT 'unknown',
                idea_submit_state TEXT NOT NULL DEFAULT 'available',
                submit_state TEXT NOT NULL DEFAULT 'available',
                zero_amount_eligibility_state TEXT NOT NULL DEFAULT 'unknown',
                zero_amount_eligibility_display_state TEXT NOT NULL DEFAULT 'unknown',
                gcash_payment_method_state TEXT NOT NULL DEFAULT 'unknown',
                has_submitted INTEGER NOT NULL DEFAULT 0,
                revival_state TEXT NOT NULL DEFAULT 'none',
                revival_kind TEXT NOT NULL DEFAULT 'none',
                subscription_active_until TEXT NOT NULL DEFAULT '',
                subscription_active_until_ts REAL,
                source_updated_at TEXT NOT NULL DEFAULT '',
                refreshed_at TEXT NOT NULL DEFAULT '',
                derivation_version TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
            )
            """
        )
        required_columns = {
            "platform": "TEXT NOT NULL DEFAULT ''",
            "manually_used": "INTEGER NOT NULL DEFAULT 0",
            "auth_type": "TEXT NOT NULL DEFAULT 'unknown'",
            "phone_binding_state": "TEXT NOT NULL DEFAULT 'unknown'",
            "payment_link_platform": "TEXT NOT NULL DEFAULT 'none'",
            "payment_link_generated": "INTEGER NOT NULL DEFAULT 0",
            "checkout_link_type": "TEXT NOT NULL DEFAULT 'none'",
            "auth_level": "TEXT NOT NULL DEFAULT ''",
            "subscription_type": "TEXT NOT NULL DEFAULT 'unknown'",
            "account_validity": "TEXT NOT NULL DEFAULT 'valid'",
            "sub2api_state": "TEXT NOT NULL DEFAULT 'unknown'",
            "oaipay_state": "TEXT NOT NULL DEFAULT 'unknown'",
            "idea_submit_state": "TEXT NOT NULL DEFAULT 'available'",
            "submit_state": "TEXT NOT NULL DEFAULT 'available'",
            "zero_amount_eligibility_state": "TEXT NOT NULL DEFAULT 'unknown'",
            "zero_amount_eligibility_display_state": "TEXT NOT NULL DEFAULT 'unknown'",
            "gcash_payment_method_state": "TEXT NOT NULL DEFAULT 'unknown'",
            "has_submitted": "INTEGER NOT NULL DEFAULT 0",
            "revival_state": "TEXT NOT NULL DEFAULT 'none'",
            "revival_kind": "TEXT NOT NULL DEFAULT 'none'",
            "subscription_active_until": "TEXT NOT NULL DEFAULT ''",
            "subscription_active_until_ts": "REAL",
            "source_updated_at": "TEXT NOT NULL DEFAULT ''",
            "refreshed_at": "TEXT NOT NULL DEFAULT ''",
            "derivation_version": "TEXT NOT NULL DEFAULT ''",
        }
        existing_columns = {
            str(row[1])
            for row in conn.exec_driver_sql("PRAGMA table_info(account_list_state)").fetchall()
        }
        for column_name, ddl in required_columns.items():
            if column_name in existing_columns:
                continue
            conn.exec_driver_sql(
                f"ALTER TABLE account_list_state ADD COLUMN {column_name} {ddl}"
            )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_account_list_state_platform "
            "ON account_list_state(platform)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_account_list_state_manually_used "
            "ON account_list_state(manually_used)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_account_list_state_auth_type "
            "ON account_list_state(auth_type)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_account_list_state_phone_binding_state "
            "ON account_list_state(phone_binding_state)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_account_list_state_payment_link_platform "
            "ON account_list_state(payment_link_platform)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_account_list_state_payment_link_generated "
            "ON account_list_state(payment_link_generated)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_account_list_state_checkout_link_type "
            "ON account_list_state(checkout_link_type)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_account_list_state_subscription_type "
            "ON account_list_state(subscription_type)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_account_list_state_account_validity "
            "ON account_list_state(account_validity)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_account_list_state_sub2api_state "
            "ON account_list_state(sub2api_state)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_account_list_state_oaipay_state "
            "ON account_list_state(oaipay_state)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_account_list_state_idea_submit_state "
            "ON account_list_state(idea_submit_state)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_account_list_state_submit_state "
            "ON account_list_state(submit_state)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_account_list_state_has_submitted "
            "ON account_list_state(has_submitted)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_account_list_state_revival_state "
            "ON account_list_state(revival_state)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_account_list_state_subscription_active_until_ts "
            "ON account_list_state(subscription_active_until_ts)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_account_list_state_zero_amount_eligibility "
            "ON account_list_state(zero_amount_eligibility_state)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_account_list_state_zero_amount_display "
            "ON account_list_state(zero_amount_eligibility_display_state)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_account_list_state_gcash_payment_method "
            "ON account_list_state(gcash_payment_method_state)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_account_list_state_derivation_version "
            "ON account_list_state(derivation_version)"
        )


def _ensure_chatgpt_local_status_refresh_job_schema() -> None:
    """Create the restart-safe local-status refresh queue on SQLite instances."""

    if not _IS_SQLITE:
        return
    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS chatgpt_local_status_refresh_jobs (
                account_id INTEGER PRIMARY KEY,
                account_email TEXT NOT NULL DEFAULT '',
                account_created_at TEXT NOT NULL DEFAULT '',
                auth_revision_hash TEXT NOT NULL DEFAULT '',
                generation INTEGER NOT NULL DEFAULT 1,
                state TEXT NOT NULL DEFAULT 'pending',
                reason TEXT NOT NULL DEFAULT '',
                attempt_count INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 3,
                requested_at_ts REAL NOT NULL DEFAULT 0,
                next_attempt_at_ts REAL NOT NULL DEFAULT 0,
                started_at_ts REAL NOT NULL DEFAULT 0,
                completed_at_ts REAL NOT NULL DEFAULT 0,
                updated_at_ts REAL NOT NULL DEFAULT 0,
                last_outcome TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
            )
            """
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_chatgpt_local_status_refresh_jobs_state_due "
            "ON chatgpt_local_status_refresh_jobs(state, next_attempt_at_ts)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_chatgpt_local_status_refresh_jobs_updated "
            "ON chatgpt_local_status_refresh_jobs(updated_at_ts)"
        )
        conn.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS trg_accounts_delete_chatgpt_local_status_refresh_job
            AFTER DELETE ON accounts
            BEGIN
                DELETE FROM chatgpt_local_status_refresh_jobs WHERE account_id = OLD.id;
            END
            """
        )


def _ensure_account_fixed_group_schema() -> None:
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_account_fixed_groups_parent_name_nocase "
            "ON account_fixed_groups(parent_preset_id, name COLLATE NOCASE)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_account_fixed_group_members_group "
            "ON account_fixed_group_members(fixed_group_id, account_id)"
        )
        conn.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS trg_accounts_delete_fixed_group_member
            AFTER DELETE ON accounts
            BEGIN
                DELETE FROM account_fixed_group_members WHERE account_id = OLD.id;
            END
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS trg_fixed_group_delete_members
            AFTER DELETE ON account_fixed_groups
            BEGIN
                DELETE FROM account_fixed_group_members WHERE fixed_group_id = OLD.id;
            END
            """
        )


def _ensure_payment_link_generation_schema() -> None:
    """Add immutable account identity columns to payment-link history.

    ``accounts.id`` is an integer primary key and may be reused after a delete.
    Existing history rows predate the identity columns, so bind only rows whose
    identity is still empty and whose account currently exists.  Orphan rows
    remain unbound and are intentionally ignored by the payment-link guard.
    """

    if not _IS_SQLITE:
        return
    with engine.begin() as conn:
        existing_columns = {
            str(row[1])
            for row in conn.exec_driver_sql("PRAGMA table_info(payment_link_generations)").fetchall()
        }
        if not existing_columns:
            return
        required_columns = {
            "account_email": "TEXT NOT NULL DEFAULT ''",
            "account_created_at": "TEXT NOT NULL DEFAULT ''",
            "generation_kind": "TEXT NOT NULL DEFAULT 'plus_checkout'",
            "variant_key": "TEXT NOT NULL DEFAULT ''",
        }
        for column_name, ddl in required_columns.items():
            if column_name in existing_columns:
                continue
            conn.exec_driver_sql(
                f"ALTER TABLE payment_link_generations ADD COLUMN {column_name} {ddl}"
            )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_payment_link_generations_account_email "
            "ON payment_link_generations(account_email)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_payment_link_generations_variant_key "
            "ON payment_link_generations(variant_key)"
        )
        conn.exec_driver_sql(
            "UPDATE payment_link_generations SET generation_kind = 'plus_checkout' "
            "WHERE trim(coalesce(generation_kind, '')) = ''"
        )
        conn.exec_driver_sql(
            """
            UPDATE payment_link_generations
            SET
                account_email = lower(trim(CAST((
                    SELECT email FROM accounts WHERE accounts.id = payment_link_generations.account_id
                ) AS TEXT))),
                account_created_at = CAST((
                    SELECT created_at FROM accounts WHERE accounts.id = payment_link_generations.account_id
                ) AS TEXT)
            WHERE EXISTS (
                SELECT 1 FROM accounts WHERE accounts.id = payment_link_generations.account_id
            )
              AND (
                  trim(coalesce(account_email, '')) = ''
                  OR trim(coalesce(account_created_at, '')) = ''
              )
            """
        )


def _ensure_payment_link_generation_cleanup_trigger() -> None:
    """Remove durable link history when its account row is deleted.

    Account ids are reusable in SQLite.  Without a database-level cleanup
    trigger, deleting an account and later creating a new row with the same id
    would incorrectly inherit the previous account's successful-link history.
    The trigger covers ORM deletes and raw SQL maintenance paths alike.
    """

    if not _IS_SQLITE:
        return
    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS trg_accounts_delete_payment_link_generations
            AFTER DELETE ON accounts
            BEGIN
                DELETE FROM payment_link_generations WHERE account_id = OLD.id;
            END
            """
        )


def _ensure_admin_auth_session_schema() -> None:
    """Add sliding-session columns without extending legacy session lifetimes.

    Existing rows used one fixed ``expires_at`` value for both the JWT and the
    server-side session.  Their absolute deadline is therefore backfilled from
    that value.  Only sessions created after this migration can use the new
    idle-time renewal policy.
    """

    with engine.begin() as conn:
        inspector = inspect(conn)
        if "admin_auth_sessions" not in inspector.get_table_names():
            return
        existing_columns = {
            str(column["name"])
            for column in inspector.get_columns("admin_auth_sessions")
        }
        required_columns = {
            "last_seen_at": "INTEGER NOT NULL DEFAULT 0",
            "absolute_expires_at": "INTEGER NOT NULL DEFAULT 0",
        }
        for column_name, ddl in required_columns.items():
            if column_name in existing_columns:
                continue
            conn.exec_driver_sql(
                f"ALTER TABLE admin_auth_sessions ADD COLUMN {column_name} {ddl}"
            )
        conn.exec_driver_sql(
            "UPDATE admin_auth_sessions SET last_seen_at = issued_at "
            "WHERE last_seen_at <= 0"
        )
        conn.exec_driver_sql(
            "UPDATE admin_auth_sessions SET absolute_expires_at = expires_at "
            "WHERE absolute_expires_at <= 0"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_admin_auth_sessions_last_seen_at "
            "ON admin_auth_sessions(last_seen_at)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_admin_auth_sessions_absolute_expires_at "
            "ON admin_auth_sessions(absolute_expires_at)"
        )


def _ensure_chatgpt_auth_lifecycle_schema(*, defer_backfill: bool = False) -> None:
    """Create and backfill the non-secret ChatGPT auth lifecycle projection."""

    try:
        from services.chatgpt_core.auth_lifecycle import backfill_existing_lifecycle_rows

        if not defer_backfill:
            backfill_existing_lifecycle_rows(engine)
            return

        global _CHATGPT_AUTH_LIFECYCLE_BACKFILL_RUNNING
        with _CHATGPT_AUTH_LIFECYCLE_BACKFILL_LOCK:
            if _CHATGPT_AUTH_LIFECYCLE_BACKFILL_RUNNING:
                return
            _CHATGPT_AUTH_LIFECYCLE_BACKFILL_RUNNING = True

        def _run_backfill() -> None:
            global _CHATGPT_AUTH_LIFECYCLE_BACKFILL_RUNNING
            try:
                backfill_existing_lifecycle_rows(engine, batch_size=250)
            except Exception:
                import logging

                logging.getLogger(__name__).exception("ChatGPT auth lifecycle background backfill failed")
            finally:
                with _CHATGPT_AUTH_LIFECYCLE_BACKFILL_LOCK:
                    _CHATGPT_AUTH_LIFECYCLE_BACKFILL_RUNNING = False

        threading.Thread(
            target=_run_backfill,
            name="chatgpt-auth-lifecycle-backfill",
            daemon=True,
        ).start()
    except Exception:
        # Lifecycle metadata must not prevent the main account database from
        # booting. The next startup retries the idempotent backfill.
        import logging

        logging.getLogger(__name__).exception("ChatGPT auth lifecycle backfill failed")


def init_db(*, defer_chatgpt_auth_lifecycle_backfill: bool = False):
    _ensure_icloud_hme_alias_schema()
    _ensure_icloud_hme_recheck_queue_schema()
    _ensure_phone_pool_schema()
    _ensure_phone_prefix_state_schema()
    _ensure_baxigpt_cdk_pool_schema()
    SQLModel.metadata.create_all(engine)
    _ensure_admin_auth_session_schema()
    _ensure_account_sort_indexes()
    _ensure_payment_link_generation_schema()
    _ensure_payment_link_generation_cleanup_trigger()
    _ensure_account_list_state_schema()
    _ensure_chatgpt_local_status_refresh_job_schema()
    _ensure_account_fixed_group_schema()
    _ensure_delivery_card_schema()
    _ensure_task_log_schema()
    _ensure_proxy_schema()
    _ensure_external_subscription_claim_schema()
    _ensure_external_access_token_claim_schema()
    _ensure_chatgpt_auth_lifecycle_schema(defer_backfill=defer_chatgpt_auth_lifecycle_backfill)


def get_session():
    with Session(engine) as session:
        yield session
