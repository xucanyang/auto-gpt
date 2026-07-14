"""Shared PIX CDK usage registry.

PIX CDKs are user supplied, single-success credentials.  The raw code must
never leave the request/worker memory path, while a stable keyed fingerprint
is needed to stop a paid code from being submitted by another runtime.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import re
import secrets
import sqlite3
from pathlib import Path

from core.shared_config import instance_id, shared_config_db_path


STATE_RESERVED = "reserved"
STATE_PAID = "paid"
STATE_UNCERTAIN = "uncertain"
STATE_BLOCKED = "blocked"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_pix_cdk(value: object) -> str:
    """Normalize the documented case/separator-insensitive PIX CDK form."""
    return re.sub(r"[^A-Z0-9]+", "", str(value or "").upper())


@dataclass(frozen=True)
class PixCdkUsage:
    fingerprint: str
    state: str
    task_id: str = ""
    account_id: int = 0
    order_id: str = ""


class PixCdkUsageStore:
    """Atomic cross-instance CDK reservation and one-success consumption."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path) if db_path else shared_config_db_path()

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pix_cdk_usage (
                fingerprint TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                task_id TEXT NOT NULL DEFAULT '',
                account_id INTEGER NOT NULL DEFAULT 0,
                order_id TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                paid_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pix_cdk_usage_state ON pix_cdk_usage(state)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pix_cdk_usage_meta (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                hmac_secret TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.commit()
        try:
            self.db_path.chmod(0o600)
        except Exception:
            pass
        return conn

    @staticmethod
    def _usage_from_row(row: sqlite3.Row | None) -> PixCdkUsage | None:
        if row is None:
            return None
        return PixCdkUsage(
            fingerprint=str(row["fingerprint"] or ""),
            state=str(row["state"] or ""),
            task_id=str(row["task_id"] or ""),
            account_id=int(row["account_id"] or 0),
            order_id=str(row["order_id"] or ""),
        )

    def fingerprint(self, value: object) -> str:
        normalized = normalize_pix_cdk(value)
        if not normalized:
            return ""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT hmac_secret FROM pix_cdk_usage_meta WHERE id = 1").fetchone()
                secret = str(row["hmac_secret"] or "") if row else ""
                if not secret:
                    secret = secrets.token_urlsafe(48)
                    conn.execute(
                        "INSERT INTO pix_cdk_usage_meta(id, hmac_secret) VALUES(1, ?) "
                        "ON CONFLICT(id) DO UPDATE SET hmac_secret = excluded.hmac_secret",
                        (secret,),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return hmac.new(secret.encode("utf-8"), normalized.encode("utf-8"), hashlib.sha256).hexdigest()

    def states_for(self, fingerprints: list[str]) -> dict[str, PixCdkUsage]:
        values = [str(value or "").strip() for value in fingerprints if str(value or "").strip()]
        if not values:
            return {}
        placeholders = ",".join("?" for _ in values)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT fingerprint, state, task_id, account_id, order_id FROM pix_cdk_usage WHERE fingerprint IN ({placeholders})",
                values,
            ).fetchall()
        return {str(row["fingerprint"]): self._usage_from_row(row) for row in rows}

    def reserve(self, fingerprint: str, *, task_id: str, account_id: int) -> PixCdkUsage:
        value = str(fingerprint or "").strip()
        if not value:
            raise ValueError("PIX CDK fingerprint is required")
        now = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT fingerprint, state, task_id, account_id, order_id FROM pix_cdk_usage WHERE fingerprint = ?",
                    (value,),
                ).fetchone()
                existing = self._usage_from_row(row)
                if existing is None:
                    conn.execute(
                        """
                        INSERT INTO pix_cdk_usage(
                            fingerprint, state, task_id, account_id, created_at, updated_at
                        ) VALUES(?, ?, ?, ?, ?, ?)
                        """,
                        (value, STATE_RESERVED, str(task_id or ""), int(account_id or 0), now, now),
                    )
                    result = PixCdkUsage(value, STATE_RESERVED, str(task_id or ""), int(account_id or 0), "")
                else:
                    result = existing
                conn.commit()
                return result
            except Exception:
                conn.rollback()
                raise

    def release(self, fingerprint: str, *, task_id: str, account_id: int) -> bool:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = conn.execute(
                    """
                    DELETE FROM pix_cdk_usage
                    WHERE fingerprint = ? AND state = ? AND task_id = ? AND account_id = ?
                    """,
                    (str(fingerprint or ""), STATE_RESERVED, str(task_id or ""), int(account_id or 0)),
                )
                conn.commit()
                return bool(result.rowcount)
            except Exception:
                conn.rollback()
                raise

    def mark_paid(self, fingerprint: str, *, task_id: str, account_id: int, order_id: str) -> PixCdkUsage:
        now = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """
                    UPDATE pix_cdk_usage
                    SET state = ?, order_id = ?, updated_at = ?, paid_at = ?
                    WHERE fingerprint = ? AND state = ? AND task_id = ? AND account_id = ?
                    """,
                    (
                        STATE_PAID,
                        str(order_id or ""),
                        now,
                        now,
                        str(fingerprint or ""),
                        STATE_RESERVED,
                        str(task_id or ""),
                        int(account_id or 0),
                    ),
                )
                row = conn.execute(
                    "SELECT fingerprint, state, task_id, account_id, order_id FROM pix_cdk_usage WHERE fingerprint = ?",
                    (str(fingerprint or ""),),
                ).fetchone()
                conn.commit()
                return self._usage_from_row(row) or PixCdkUsage(str(fingerprint or ""), STATE_PAID)
            except Exception:
                conn.rollback()
                raise

    def mark_uncertain(self, fingerprint: str, *, task_id: str, account_id: int, order_id: str = "") -> PixCdkUsage:
        now = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """
                    UPDATE pix_cdk_usage
                    SET state = ?, order_id = ?, updated_at = ?
                    WHERE fingerprint = ? AND state = ? AND task_id = ? AND account_id = ?
                    """,
                    (
                        STATE_UNCERTAIN,
                        str(order_id or ""),
                        now,
                        str(fingerprint or ""),
                        STATE_RESERVED,
                        str(task_id or ""),
                        int(account_id or 0),
                    ),
                )
                row = conn.execute(
                    "SELECT fingerprint, state, task_id, account_id, order_id FROM pix_cdk_usage WHERE fingerprint = ?",
                    (str(fingerprint or ""),),
                ).fetchone()
                conn.commit()
                return self._usage_from_row(row) or PixCdkUsage(str(fingerprint or ""), STATE_UNCERTAIN)
            except Exception:
                conn.rollback()
                raise

    def mark_blocked(self, fingerprint: str, *, task_id: str, account_id: int) -> PixCdkUsage:
        now = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """
                    UPDATE pix_cdk_usage
                    SET state = ?, updated_at = ?
                    WHERE fingerprint = ? AND state = ? AND task_id = ? AND account_id = ?
                    """,
                    (
                        STATE_BLOCKED,
                        now,
                        str(fingerprint or ""),
                        STATE_RESERVED,
                        str(task_id or ""),
                        int(account_id or 0),
                    ),
                )
                row = conn.execute(
                    "SELECT fingerprint, state, task_id, account_id, order_id FROM pix_cdk_usage WHERE fingerprint = ?",
                    (str(fingerprint or ""),),
                ).fetchone()
                conn.commit()
                return self._usage_from_row(row) or PixCdkUsage(str(fingerprint or ""), STATE_BLOCKED)
            except Exception:
                conn.rollback()
                raise


pix_cdk_usage_store = PixCdkUsageStore()
