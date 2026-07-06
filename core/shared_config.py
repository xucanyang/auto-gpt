"""跨实例共享配置中心。

业务数据继续保留在各实例自己的 account_manager.db；这里只存 Settings 里的
全局配置模板。共享源使用 SQLite 而不是裸 JSON，主要为了事务、并发锁、版本
号、审计和可回滚快照。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any


CONFIG_SHARE_ENABLED_KEY = "_config_share_enabled"
CONFIG_SHARE_BASELINE_REVISION_KEY = "_config_share_baseline_revision"
CONFIG_SHARE_DETACHED_AT_KEY = "_config_share_detached_at"
CONFIG_SHARE_LAST_PULL_AT_KEY = "_config_share_last_pull_at"

CONTROL_KEYS = {
    CONFIG_SHARE_ENABLED_KEY,
    CONFIG_SHARE_BASELINE_REVISION_KEY,
    CONFIG_SHARE_DETACHED_AT_KEY,
    CONFIG_SHARE_LAST_PULL_AT_KEY,
}

# 这些 key 必须留在实例本地；否则三实例会互相指错端口、混用外部分发令牌，
# 或把运行态事件当作全局模板传播。
LOCAL_ONLY_KEYS = {
    "auth_password_hash",
    "auth_jwt_secret",
    "auth_totp_secret",
    "cliproxyapi_base_url",
    "cliproxyapi_management_key",
    "external_subscription_api_enabled",
    "external_subscription_api_token",
    "external_subscription_verify_after_seconds",
    "external_access_token_api_enabled",
    "external_access_token_api_token",
    "external_access_token_allow_refresh",
    "external_access_token_default_lease_seconds",
    "external_access_token_max_limit",
    "external_access_token_precheck_cooldown_seconds",
    "chatgpt_gopay_uid_bindings",
    "chatgpt_gopay_uid_sessions",
    "chatgpt_gopay_phone_pool",
    "chatgpt_gopay_smsforwarder_secret",
    "chatgpt_gopay_smsforwarder_recent_events",
    "chatgpt_gopay_batch_tasks",
    "chatgpt_gopay_active_batch_task_id",
    "chatgpt_account_filter_presets",
    "chatgpt_auto_pipeline_config",
}

LOCAL_ONLY_PREFIXES = (
    "auth_",
    "delivery_cards_",
)

_SECRET_KEY_PARTS = (
    "api_key",
    "token",
    "cookie",
    "password",
    "secret",
    "bearer",
    "auth",
)


class SharedConfigConflict(RuntimeError):
    """共享配置版本冲突。"""


def instance_id() -> str:
    value = (
        os.getenv("APP_INSTANCE_ID")
        or os.getenv("CONTAINER_NAME")
        or os.getenv("HOSTNAME")
        or "unknown"
    )
    return str(value or "unknown").strip() or "unknown"


def shared_config_db_path() -> Path:
    configured = str(os.getenv("SHARED_CONFIG_DB") or "").strip()
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[1] / "shared_config" / "shared_config.db"


def is_shareable_key(key: str) -> bool:
    normalized = str(key or "").strip()
    return bool(
        normalized
        and normalized not in CONTROL_KEYS
        and normalized not in LOCAL_ONLY_KEYS
        and not any(normalized.startswith(prefix) for prefix in LOCAL_ONLY_PREFIXES)
    )


def filter_shareable_config(data: dict[str, Any] | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in (data or {}).items():
        normalized = str(key or "").strip()
        if not is_shareable_key(normalized):
            continue
        result[normalized] = "" if value is None else str(value)
    return result


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _hash_config(data: dict[str, str]) -> str:
    blob = json.dumps(data or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _redact_value(key: str, value: Any) -> dict[str, Any] | str:
    text = "" if value is None else str(value)
    lower = str(key or "").lower()
    if any(part in lower for part in _SECRET_KEY_PARTS):
        return {
            "present": bool(text),
            "length": len(text),
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest() if text else "",
        }
    if len(text) > 180:
        return text[:80] + f"...<len={len(text)}>"
    return text


def _redacted_diff(before: dict[str, str], after: dict[str, str], keys: list[str]) -> dict[str, Any]:
    diff: dict[str, Any] = {}
    for key in keys:
        diff[key] = {
            "before": _redact_value(key, before.get(key, "")),
            "after": _redact_value(key, after.get(key, "")),
        }
    return diff


class SharedConfigStore:
    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path) if db_path else shared_config_db_path()

    @property
    def snapshot_dir(self) -> Path:
        return self.db_path.parent / "snapshots"

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        self._ensure_schema(conn)
        try:
            os.chmod(self.db_path, 0o600)
        except Exception:
            pass
        return conn

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS shared_config_meta (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                revision INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT '',
                updated_by TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS shared_config_items (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT '',
                revision INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT '',
                updated_by TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS shared_config_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                revision INTEGER NOT NULL,
                base_revision INTEGER NOT NULL,
                action TEXT NOT NULL DEFAULT 'update',
                updated_at TEXT NOT NULL,
                updated_by TEXT NOT NULL,
                changed_keys_json TEXT NOT NULL DEFAULT '[]',
                before_hash TEXT NOT NULL DEFAULT '',
                after_hash TEXT NOT NULL DEFAULT '',
                diff_json TEXT NOT NULL DEFAULT '{}',
                note TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO shared_config_meta(id, revision, updated_at, updated_by, note)
            VALUES(1, 0, '', '', '')
            """
        )
        conn.commit()

    @staticmethod
    def _read_all_in_tx(conn: sqlite3.Connection) -> dict[str, str]:
        rows = conn.execute("SELECT key, value FROM shared_config_items").fetchall()
        return {str(row["key"]): str(row["value"] or "") for row in rows}

    @staticmethod
    def _revision_in_tx(conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT revision FROM shared_config_meta WHERE id = 1").fetchone()
        return int(row["revision"] if row else 0)

    def exists(self) -> bool:
        return self.db_path.exists()

    def revision(self) -> int:
        with self._connect() as conn:
            return self._revision_in_tx(conn)

    def meta(self) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT revision, updated_at, updated_by, note FROM shared_config_meta WHERE id = 1").fetchone()
            total = conn.execute("SELECT COUNT(*) AS c FROM shared_config_items").fetchone()["c"]
        return {
            "path": str(self.db_path),
            "exists": self.db_path.exists(),
            "revision": int(row["revision"] if row else 0),
            "updated_at": str(row["updated_at"] if row else ""),
            "updated_by": str(row["updated_by"] if row else ""),
            "note": str(row["note"] if row else ""),
            "keys": int(total or 0),
        }

    def get_all(self) -> dict[str, str]:
        with self._connect() as conn:
            return self._read_all_in_tx(conn)

    def get_entry(self, key: str) -> tuple[bool, str]:
        if not is_shareable_key(key):
            return False, ""
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM shared_config_items WHERE key = ?", (str(key),)).fetchone()
        if row is None:
            return False, ""
        return True, str(row["value"] or "")

    def get(self, key: str) -> str:
        return self.get_entry(key)[1]

    def write(
        self,
        data: dict[str, Any],
        *,
        replace: bool = False,
        base_revision: int | None = None,
        updated_by: str | None = None,
        action: str = "update",
        note: str = "",
    ) -> dict[str, Any]:
        safe = filter_shareable_config(data)
        actor = str(updated_by or instance_id()).strip() or "unknown"
        now = _utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                current_revision = self._revision_in_tx(conn)
                if base_revision is not None and int(base_revision) != current_revision:
                    raise SharedConfigConflict(f"共享配置版本已变化: current={current_revision}, base={base_revision}")

                before = self._read_all_in_tx(conn)
                after = dict(safe) if replace else {**before, **safe}
                changed_keys = sorted(
                    key for key in set(before) | set(after)
                    if key not in before
                    or key not in after
                    or str(before.get(key, "")) != str(after.get(key, ""))
                )
                if not changed_keys:
                    conn.rollback()
                    return {
                        "ok": True,
                        "changed": False,
                        "revision": current_revision,
                        "changed_keys": [],
                    }

                next_revision = current_revision + 1
                if replace:
                    for key in set(before) - set(after):
                        conn.execute("DELETE FROM shared_config_items WHERE key = ?", (key,))
                for key in set(safe) if replace else set(safe):
                    conn.execute(
                        """
                        INSERT INTO shared_config_items(key, value, revision, updated_at, updated_by)
                        VALUES(?, ?, ?, ?, ?)
                        ON CONFLICT(key) DO UPDATE SET
                            value = excluded.value,
                            revision = excluded.revision,
                            updated_at = excluded.updated_at,
                            updated_by = excluded.updated_by
                        """,
                        (key, after.get(key, ""), next_revision, now, actor),
                    )
                if replace:
                    for key in set(after) - set(safe):
                        conn.execute(
                            """
                            INSERT INTO shared_config_items(key, value, revision, updated_at, updated_by)
                            VALUES(?, ?, ?, ?, ?)
                            ON CONFLICT(key) DO UPDATE SET
                                value = excluded.value,
                                revision = excluded.revision,
                                updated_at = excluded.updated_at,
                                updated_by = excluded.updated_by
                            """,
                            (key, after.get(key, ""), next_revision, now, actor),
                        )

                before_hash = _hash_config(before)
                after_hash = _hash_config(after)
                conn.execute(
                    """
                    UPDATE shared_config_meta
                    SET revision = ?, updated_at = ?, updated_by = ?, note = ?
                    WHERE id = 1
                    """,
                    (next_revision, now, actor, note),
                )
                conn.execute(
                    """
                    INSERT INTO shared_config_audit(
                        revision, base_revision, action, updated_at, updated_by,
                        changed_keys_json, before_hash, after_hash, diff_json, note
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        next_revision,
                        current_revision,
                        action,
                        now,
                        actor,
                        json.dumps(changed_keys, ensure_ascii=False),
                        before_hash,
                        after_hash,
                        json.dumps(_redacted_diff(before, after, changed_keys), ensure_ascii=False, sort_keys=True),
                        note,
                    ),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        self._write_snapshot(after, next_revision, actor, action, changed_keys)
        return {
            "ok": True,
            "changed": True,
            "revision": next_revision,
            "changed_keys": changed_keys,
        }

    def _write_snapshot(
        self,
        data: dict[str, str],
        revision: int,
        actor: str,
        action: str,
        changed_keys: list[str],
    ) -> None:
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "revision": revision,
            "updated_at": _utc_now(),
            "updated_by": actor,
            "action": action,
            "changed_keys": changed_keys,
            "data": data,
        }
        safe_actor = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in actor)[:80] or "unknown"
        safe_action = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in action)[:80] or "update"
        path = self.snapshot_dir / f"rev-{revision:06d}-{safe_actor}-{safe_action}.json"
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except Exception:
            pass
        tmp.replace(path)

    def audit(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT revision, base_revision, action, updated_at, updated_by,
                       changed_keys_json, before_hash, after_hash, diff_json, note
                FROM shared_config_audit
                ORDER BY revision DESC
                LIMIT ?
                """,
                (max(1, min(int(limit or 50), 200)),),
            ).fetchall()
        result = []
        for row in rows:
            result.append({
                "revision": int(row["revision"]),
                "base_revision": int(row["base_revision"]),
                "action": row["action"],
                "updated_at": row["updated_at"],
                "updated_by": row["updated_by"],
                "changed_keys": json.loads(row["changed_keys_json"] or "[]"),
                "before_hash": row["before_hash"],
                "after_hash": row["after_hash"],
                "diff": json.loads(row["diff_json"] or "{}"),
                "note": row["note"],
            })
        return result


shared_config_store = SharedConfigStore()
