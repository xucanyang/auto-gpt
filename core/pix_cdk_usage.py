"""Shared PIX CDK usage registry.

Raw CDKs never leave the request/worker memory path.  The shared SQLite file
stores only a keyed fingerprint and separates a *current* cross-instance lock
from immutable paid history: a locally issued multi-credit CDK can be consumed
under one task lease while paid history is recorded independently, while an
external one-success CDK remains blocked.
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
    paid_at: str = ""


class PixCdkUsageStore:
    """Atomic cross-instance CDK reservation, paid history, and review locks."""

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
            CREATE TABLE IF NOT EXISTS pix_cdk_usage_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fingerprint TEXT NOT NULL,
                task_id TEXT NOT NULL DEFAULT '',
                account_id INTEGER NOT NULL DEFAULT 0,
                order_id TEXT NOT NULL DEFAULT '',
                paid_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pix_cdk_usage_history_fingerprint_paid_at "
            "ON pix_cdk_usage_history(fingerprint, paid_at)"
        )
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
        keys = set(row.keys())
        return PixCdkUsage(
            fingerprint=str(row["fingerprint"] or ""),
            state=str(row["state"] or ""),
            task_id=str(row["task_id"] or ""),
            account_id=int(row["account_id"] or 0),
            order_id=str(row["order_id"] or ""),
            paid_at=str(row["paid_at"] or "") if "paid_at" in keys else "",
        )

    @staticmethod
    def _migrate_legacy_paid_locked(conn: sqlite3.Connection, fingerprints: list[str] | None = None) -> int:
        """Move old current-table ``paid`` rows into immutable history.

        Prior releases used ``state=paid`` as a permanent lock.  Treat that
        row as a completed audit event, then remove it from the current-lock
        table in the same transaction so recharged multi-credit CDKs can be
        reserved again without losing historical evidence.
        """
        clauses = ["state = ?"]
        params: list[object] = [STATE_PAID]
        values = [str(value or "").strip() for value in (fingerprints or []) if str(value or "").strip()]
        if values:
            clauses.append(f"fingerprint IN ({','.join('?' for _ in values)})")
            params.extend(values)
        where = " AND ".join(clauses)
        rows = conn.execute(
            "SELECT fingerprint, task_id, account_id, order_id, paid_at, created_at "
            f"FROM pix_cdk_usage WHERE {where}",
            params,
        ).fetchall()
        if not rows:
            return 0
        now = _now()
        conn.executemany(
            """
            INSERT INTO pix_cdk_usage_history(
                fingerprint, task_id, account_id, order_id, paid_at, created_at
            ) VALUES(?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    str(row["fingerprint"] or ""),
                    str(row["task_id"] or ""),
                    int(row["account_id"] or 0),
                    str(row["order_id"] or ""),
                    str(row["paid_at"] or "") or now,
                    str(row["created_at"] or "") or now,
                )
                for row in rows
            ],
        )
        conn.executemany(
            "DELETE FROM pix_cdk_usage WHERE fingerprint = ? AND state = ?",
            [(str(row["fingerprint"] or ""), STATE_PAID) for row in rows],
        )
        return len(rows)

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
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._migrate_legacy_paid_locked(conn, values)
                rows = conn.execute(
                    f"SELECT fingerprint, state, task_id, account_id, order_id FROM pix_cdk_usage WHERE fingerprint IN ({placeholders})",
                    values,
                ).fetchall()
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return {str(row["fingerprint"]): self._usage_from_row(row) for row in rows}

    def history_for(self, fingerprint: str) -> list[PixCdkUsage]:
        """Return non-sensitive paid audit records for one fingerprint."""
        value = str(fingerprint or "").strip()
        if not value:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT fingerprint, task_id, account_id, order_id, paid_at
                FROM pix_cdk_usage_history
                WHERE fingerprint = ?
                ORDER BY id ASC
                """,
                (value,),
            ).fetchall()
        return [
            PixCdkUsage(
                fingerprint=str(row["fingerprint"] or ""),
                state=STATE_PAID,
                task_id=str(row["task_id"] or ""),
                account_id=int(row["account_id"] or 0),
                order_id=str(row["order_id"] or ""),
                paid_at=str(row["paid_at"] or ""),
            )
            for row in rows
        ]

    def reserve(self, fingerprint: str, *, task_id: str, account_id: int) -> PixCdkUsage:
        value = str(fingerprint or "").strip()
        if not value:
            raise ValueError("PIX CDK fingerprint is required")
        now = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._migrate_legacy_paid_locked(conn, [value])
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

    @staticmethod
    def _paid_history_exists_locked(
        conn: sqlite3.Connection,
        *,
        fingerprint: str,
        task_id: str,
        account_id: int,
        order_id: str,
    ) -> bool:
        row = conn.execute(
            """
            SELECT 1
            FROM pix_cdk_usage_history
            WHERE fingerprint = ? AND task_id = ? AND account_id = ? AND order_id = ?
            LIMIT 1
            """,
            (fingerprint, task_id, int(account_id or 0), order_id),
        ).fetchone()
        return row is not None

    @staticmethod
    def _insert_paid_history_locked(
        conn: sqlite3.Connection,
        *,
        fingerprint: str,
        task_id: str,
        account_id: int,
        order_id: str,
        paid_at: str,
    ) -> None:
        """Insert one immutable paid event, idempotently."""
        if PixCdkUsageStore._paid_history_exists_locked(
            conn,
            fingerprint=fingerprint,
            task_id=task_id,
            account_id=account_id,
            order_id=order_id,
        ):
            return
        conn.execute(
            """
            INSERT INTO pix_cdk_usage_history(
                fingerprint, task_id, account_id, order_id, paid_at, created_at
            ) VALUES(?, ?, ?, ?, ?, ?)
            """,
            (fingerprint, task_id, int(account_id or 0), order_id, paid_at, paid_at),
        )

    def record_paid_history(
        self,
        fingerprint: str,
        *,
        task_id: str,
        account_id: int,
        order_id: str,
        paid_at: str = "",
    ) -> PixCdkUsage:
        """Record one paid order independently of the current task lease."""
        now = str(paid_at or "").strip() or _now()
        value = str(fingerprint or "").strip()
        task_value = str(task_id or "")
        account_value = int(account_id or 0)
        order_value = str(order_id or "")
        if not value or not task_value or not order_value:
            raise ValueError("PIX paid history requires fingerprint, task_id and order_id")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._insert_paid_history_locked(
                    conn,
                    fingerprint=value,
                    task_id=task_value,
                    account_id=account_value,
                    order_id=order_value,
                    paid_at=now,
                )
                conn.commit()
                return PixCdkUsage(value, STATE_PAID, task_value, account_value, order_value, now)
            except Exception:
                conn.rollback()
                raise

    def mark_paid(
        self,
        fingerprint: str,
        *,
        task_id: str,
        account_id: int,
        order_id: str,
        retain_block: bool = False,
        reservation_account_id: int | None = None,
    ) -> PixCdkUsage:
        """Record paid atomically, then free or permanently block the lock.

        ``retain_block`` is for the legacy external PIX one-success contract.
        Local balance CDKs use the default: delete the current reservation only
        after history is durable, allowing exactly one later serial reserve.
        """
        now = _now()
        value = str(fingerprint or "")
        task_value = str(task_id or "")
        account_value = int(account_id or 0)
        reservation_account_value = (
            account_value if reservation_account_id is None else int(reservation_account_id or 0)
        )
        order_value = str(order_id or "")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT fingerprint, state, task_id, account_id, order_id FROM pix_cdk_usage WHERE fingerprint = ?",
                    (value,),
                ).fetchone()
                usage = self._usage_from_row(row)
                if (
                    usage is None
                    or usage.state != STATE_RESERVED
                    or usage.task_id != task_value
                    or usage.account_id != reservation_account_value
                ):
                    raise RuntimeError("PIX CDK 当前占用不存在或已变化，拒绝释放复用")
                self._insert_paid_history_locked(
                    conn,
                    fingerprint=value,
                    task_id=task_value,
                    account_id=account_value,
                    order_id=order_value,
                    paid_at=now,
                )
                if retain_block:
                    updated = conn.execute(
                        """
                        UPDATE pix_cdk_usage
                        SET state = ?, order_id = ?, updated_at = ?, paid_at = ?
                        WHERE fingerprint = ? AND state = ? AND task_id = ? AND account_id = ?
                        """,
                        (STATE_BLOCKED, order_value, now, now, value, STATE_RESERVED, task_value, reservation_account_value),
                    )
                    result = PixCdkUsage(value, STATE_BLOCKED, task_value, account_value, order_value, now)
                else:
                    updated = conn.execute(
                        """
                        DELETE FROM pix_cdk_usage
                        WHERE fingerprint = ? AND state = ? AND task_id = ? AND account_id = ?
                        """,
                        (value, STATE_RESERVED, task_value, reservation_account_value),
                    )
                    result = PixCdkUsage(value, STATE_PAID, task_value, account_value, order_value, now)
                if updated.rowcount != 1:
                    raise RuntimeError("PIX CDK paid 记录提交时占用已变化")
                conn.commit()
                return result
            except Exception:
                conn.rollback()
                raise

    def mark_uncertain(self, fingerprint: str, *, task_id: str, account_id: int, order_id: str = "") -> PixCdkUsage:
        now = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                updated = conn.execute(
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
                # Exact-quota reservations are released immediately after a
                # complete upstream acceptance. If that later order becomes
                # uncertain, create a durable review lock even though there is
                # no longer a RESERVED row to update. Never overwrite a
                # different live reservation owned by another order.
                if updated.rowcount != 1:
                    existing_row = conn.execute(
                        "SELECT fingerprint, state, task_id, account_id, order_id FROM pix_cdk_usage WHERE fingerprint = ?",
                        (str(fingerprint or ""),),
                    ).fetchone()
                    if existing_row is None:
                        conn.execute(
                            """
                            INSERT INTO pix_cdk_usage(
                                fingerprint, state, task_id, account_id, order_id, created_at, updated_at
                            ) VALUES(?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                str(fingerprint or ""),
                                STATE_UNCERTAIN,
                                str(task_id or ""),
                                int(account_id or 0),
                                str(order_id or ""),
                                now,
                                now,
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
