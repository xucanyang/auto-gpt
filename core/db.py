"""数据库模型 - SQLite via SQLModel"""
from datetime import datetime, timezone
from math import ceil
import os
from typing import Any, Optional
from sqlalchemy import text
from sqlmodel import Field, SQLModel, create_engine, Session, select
import json

from services.pipeline.models import PipelineAccountItem, PipelineTask


def _utcnow():
    return datetime.now(timezone.utc)

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///account_manager.db")
engine = create_engine(DATABASE_URL)


class AccountModel(SQLModel, table=True):
    __tablename__ = "accounts"

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


class PendingBusinessInviteModel(SQLModel, table=True):
    __tablename__ = "pending_business_invites"

    id: Optional[int] = Field(default=None, primary_key=True)
    account_id: int = Field(index=True)
    email: str = Field(index=True)
    status: str = Field(default="invited_pending", index=True)
    team_id: int = 0
    team_name: str = ""
    invite_url: str = ""
    invite_workspace_id: str = ""
    invite_message_id: str = ""
    mail_provider: str = ""
    mailbox_state_json: str = "{}"
    registration_context_json: str = "{}"
    invited_at: str = ""
    join_consumed_at: str = ""
    joined_at: str = ""
    last_error: str = ""
    last_error_code: str = ""
    last_checkpoint: str = ""
    activation_attempt_count: int = 0
    last_attempt_at: str = ""
    activation_run_id: str = ""
    abandoned_at: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


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
    """从 base_platform.Account 存入数据库（支持同邮箱多工作空间变体并存）"""
    with Session(engine) as session:
        extra = account.extra or {}
        variant_key = str(extra.get("chatgpt_workspace_variant_key") or "").strip()
        candidates = session.exec(
            select(AccountModel)
            .where(AccountModel.platform == account.platform)
            .where(AccountModel.email == account.email)
        ).all()

        existing = None
        legacy_candidate = None
        for candidate in candidates:
            try:
                candidate_extra = json.loads(candidate.extra_json or "{}")
            except Exception:
                candidate_extra = {}
            candidate_variant_key = str(candidate_extra.get("chatgpt_workspace_variant_key") or "").strip()
            if variant_key:
                if candidate_variant_key == variant_key:
                    existing = candidate
                    break
                if not candidate_variant_key and legacy_candidate is None:
                    legacy_candidate = candidate
            else:
                if not candidate_variant_key:
                    existing = candidate
                    break

        if existing is None and variant_key and legacy_candidate is not None and len(candidates) == 1:
            existing = legacy_candidate

        if existing:
            existing.password = account.password
            existing.user_id = account.user_id or ""
            existing.region = account.region or ""
            existing.token = account.token or ""
            existing.status = account.status.value
            existing.extra_json = json.dumps(extra, ensure_ascii=False)
            existing.cashier_url = extra.get("cashier_url", "")
            existing.updated_at = _utcnow()
            session.add(existing)
            session.commit()
            session.refresh(existing)
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
        return m


def _ensure_pending_business_invite_schema() -> None:
    required_columns = {
        "last_error_code": "TEXT NOT NULL DEFAULT ''",
        "last_checkpoint": "TEXT NOT NULL DEFAULT ''",
        "activation_attempt_count": "INTEGER NOT NULL DEFAULT 0",
        "last_attempt_at": "TEXT NOT NULL DEFAULT ''",
        "activation_run_id": "TEXT NOT NULL DEFAULT ''",
        "abandoned_at": "TEXT NOT NULL DEFAULT ''",
    }

    with engine.begin() as conn:
        existing_columns = {
            str(row[1])
            for row in conn.exec_driver_sql("PRAGMA table_info(pending_business_invites)").fetchall()
        }
        for column_name, ddl in required_columns.items():
            if column_name in existing_columns:
                continue
            conn.exec_driver_sql(
                f"ALTER TABLE pending_business_invites ADD COLUMN {column_name} {ddl}"
            )


def _ensure_proxy_schema() -> None:
    required_columns = {
        "homepage_success_count": "INTEGER NOT NULL DEFAULT 0",
        "homepage_fail_count": "INTEGER NOT NULL DEFAULT 0",
        "homepage_consecutive_failures": "INTEGER NOT NULL DEFAULT 0",
        "homepage_last_error": "TEXT NOT NULL DEFAULT ''",
        "homepage_last_status_code": "INTEGER NOT NULL DEFAULT 0",
        "homepage_last_checked": "TIMESTAMP NULL",
        "homepage_circuit_open_until": "TIMESTAMP NULL",
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
        used_by_system = bool(
            use_count > 0
            or str(row.get("task_id") or "").strip()
            or str(row.get("bound_account_email") or "").strip()
            or status in {"in_use", "registered", "register_failed"}
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
            "is_manual_created": str(row.get("created_source") or "unknown") == "manual_created",
            "last_otp_at": str(row.get("last_otp_at") or ""),
            "last_error": str(row.get("last_error") or ""),
            "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at or ""),
            "updated_at": updated_at.isoformat() if hasattr(updated_at, "isoformat") else str(updated_at or ""),
        }
    if isinstance(row, IcloudHmeAliasModel):
        used_by_system = bool(
            int(getattr(row, "use_count", 0) or 0) > 0
            or str(getattr(row, "task_id", "") or "").strip()
            or str(getattr(row, "bound_account_email", "") or "").strip()
            or str(getattr(row, "status", "") or "") in {"in_use", "registered", "register_failed"}
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
            "is_manual_created": row.created_source == "manual_created",
            "last_otp_at": row.last_otp_at,
            "last_error": row.last_error,
            "created_at": row.created_at.isoformat() if getattr(row, "created_at", None) else "",
            "updated_at": row.updated_at.isoformat() if getattr(row, "updated_at", None) else "",
        }
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
        "status": str(getattr(row, "status", "") or ""),
        "use_count": int(getattr(row, "use_count", 0) or 0),
        "first_claimed_at": str(getattr(row, "first_claimed_at", "") or ""),
        "last_claimed_at": str(getattr(row, "last_claimed_at", "") or ""),
        "last_synced_at": str(getattr(row, "last_synced_at", "") or ""),
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
            recyclable_failed = status_text == "register_failed"

            if only_unused:
                if status_text in {"registered", "in_use", "retired"}:
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


def recover_stuck_pending_business_invites() -> int:
    activation_statuses = (
        "activation_fetching_invite_mail",
        "activation_auth_login",
        "activation_consuming_invite",
        "activation_capturing_workspace",
        "subscription_pending_auth",
    )
    recovered = 0
    now = _utcnow()
    placeholders = ", ".join("?" for _ in activation_statuses)

    with engine.begin() as conn:
        rows = conn.exec_driver_sql(
            f"""
            SELECT id, status, last_checkpoint, last_error, last_error_code
            FROM pending_business_invites
            WHERE status IN ({placeholders})
            """,
            activation_statuses,
        ).fetchall()

        for row in rows:
            checkpoint = str(row[2] or row[1] or "activation_auth_login")
            last_error = str(row[3] or "").strip() or "上次激活被中断，可重新启动激活流程"
            last_error_code = str(row[4] or "").strip() or "activation_interrupted"
            conn.execute(
                text(
                    """
                    UPDATE pending_business_invites
                    SET status = :status,
                        last_checkpoint = :checkpoint,
                        last_error = :last_error,
                        last_error_code = :last_error_code,
                        updated_at = :updated_at
                    WHERE id = :invite_id
                    """
                ),
                {
                    "status": "failed_retryable",
                    "checkpoint": checkpoint,
                    "last_error": last_error,
                    "last_error_code": last_error_code,
                    "updated_at": now,
                    "invite_id": int(row[0] or 0),
                },
            )
            recovered += 1

    return recovered


def init_db():
    _ensure_icloud_hme_alias_schema()
    SQLModel.metadata.create_all(engine)
    _ensure_task_log_schema()
    _ensure_proxy_schema()
    _ensure_pending_business_invite_schema()
    _ensure_external_subscription_claim_schema()
    _ensure_pipeline_schema()


def get_session():
    with Session(engine) as session:
        yield session
