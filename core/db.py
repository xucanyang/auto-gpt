"""数据库模型 - SQLite via SQLModel"""
from datetime import datetime, timezone
import os
from typing import Optional
from sqlalchemy import text
from sqlmodel import Field, SQLModel, create_engine, Session, select
import json


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


class TaskLog(SQLModel, table=True):
    __tablename__ = "task_logs"

    id: Optional[int] = Field(default=None, primary_key=True)
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
    is_active: bool = True
    last_checked: Optional[datetime] = None


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


def recover_stuck_pending_business_invites() -> int:
    activation_statuses = (
        "activation_fetching_invite_mail",
        "activation_auth_login",
        "activation_consuming_invite",
        "activation_capturing_workspace",
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
    SQLModel.metadata.create_all(engine)
    _ensure_pending_business_invite_schema()


def get_session():
    with Session(engine) as session:
        yield session
