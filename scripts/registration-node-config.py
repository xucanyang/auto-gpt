#!/usr/bin/env python3
"""Export Plus business settings into an independent registration-node DB."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import tempfile
from datetime import datetime, timezone
from typing import Any


SCHEMA_VERSION = 1

# These values identify one live instance or reference consumable/runtime state.
# Copying them would turn an independent node into a stale clone of Plus.
EXCLUDED_KEYS = {
    "auth_jwt_secret",
    "chatgpt_account_filter_presets",
    "chatgpt_gopay_active_batch_task_id",
    "chatgpt_gopay_batch_tasks",
    "chatgpt_gopay_phone_candidates",
    "chatgpt_gopay_phone_pool",
    "chatgpt_gopay_smsforwarder_recent_events",
    "chatgpt_gopay_uid_bindings",
    "chatgpt_gopay_uid_sessions",
    "delivery_cards_api_token_hash",
    "delivery_cards_api_token_last4",
    "delivery_cards_code_hash_secret",
    "email_api_lines",
}
EXCLUDED_PREFIXES = (
    "_config_share_",
    "external_access_token_",
    "external_subscription_",
)

# Runtime limits intentionally differ from Plus. Business/provider settings are
# otherwise copied byte-for-byte, including the existing integration secrets.
REGISTRATION_NODE_OVERRIDES = {
    "chatgpt_register_browser_default_concurrency": "30",
    "chatgpt_register_browser_max_concurrency": "30",
    "chatgpt_register_delay_seconds": "0",
    "chatgpt_register_delay_max_seconds": "0",
    "chatgpt_runtime_browser_capacity_mode": "fixed",
    "chatgpt_runtime_auth_browser_max_concurrency": "30",
    "chatgpt_runtime_auth_browser_registration_reserve": "0",
    "chatgpt_runtime_auth_browser_recheck_reserve": "0",
    "chatgpt_web_session_hold_max_sessions": "15",
    "chatgpt_runtime_auth_browser_pid_budget": "0",
    "chatgpt_runtime_pid_emergency_reserve": "0",
    "chatgpt_runtime_host_memory_reserve_mib": "0",
    "chatgpt_runtime_cpu_psi_avg10_limit": "0",
    "chatgpt_runtime_auth_browser_launch_interval_seconds": "0",
    "chatgpt_runtime_solver_mode": "auto",
    "chatgpt_runtime_solver_max_browsers": "15",
    "chatgpt_runtime_solver_warm_browsers": "0",
    "chatgpt_runtime_solver_idle_timeout_seconds": "300",
    "icloud_hme_helper_api_url": "http://172.20.0.1:18765",
    # The dedicated node must not duplicate maintenance against shared upstream
    # stores. Registration-triggered mailbox, upload, and payment work remains on.
    "icloud_hme_auto_create_enabled": "false",
    "icloud_hme_auto_delete_enabled": "false",
    "tempmail_archive_cleanup_enabled": "false",
    "cpa_cleanup_enabled": "false",
    "proxy_scan_enabled": "false",
    "external_access_token_api_enabled": "false",
    "external_subscription_api_enabled": "false",
}


def _connect_read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _is_exportable(key: str) -> bool:
    return key not in EXCLUDED_KEYS and not key.startswith(EXCLUDED_PREFIXES)


def _disable_pipeline_autostart(value: str) -> str:
    try:
        payload = json.loads(value or "{}")
    except (TypeError, ValueError):
        return value
    if not isinstance(payload, dict):
        return value
    payload["auto_start"] = False
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def export_configs(source_db: Path) -> dict[str, str]:
    if not source_db.is_file():
        raise FileNotFoundError(f"source database does not exist: {source_db}")
    with _connect_read_only(source_db) as connection:
        if not _table_exists(connection, "configs"):
            raise RuntimeError("source database has no configs table")
        rows = connection.execute("SELECT key, value FROM configs ORDER BY key").fetchall()

    configs: dict[str, str] = {}
    for raw_key, raw_value in rows:
        key = str(raw_key or "").strip()
        if not key or not _is_exportable(key):
            continue
        value = "" if raw_value is None else str(raw_value)
        if key == "chatgpt_auto_pipeline_config":
            value = _disable_pipeline_autostart(value)
        configs[key] = value
    configs.update(REGISTRATION_NODE_OVERRIDES)
    return configs


def _config_digest(configs: dict[str, str]) -> str:
    encoded = json.dumps(
        configs,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def write_export(source_db: Path, output: Path, source_instance: str) -> dict[str, Any]:
    configs = export_configs(source_db)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "source_instance": str(source_instance or "auto-gpt-plus"),
        "config_count": len(configs),
        "config_sha256": _config_digest(configs),
        "configs": configs,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, output)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
    return {
        "config_count": len(configs),
        "config_sha256": payload["config_sha256"],
        "output": str(output),
    }


def _read_export(path: Path) -> dict[str, str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError("unsupported registration-node config schema")
    configs = payload.get("configs")
    if not isinstance(configs, dict):
        raise RuntimeError("registration-node config has no configs object")
    normalized = {str(key): "" if value is None else str(value) for key, value in configs.items()}
    expected = str(payload.get("config_sha256") or "")
    actual = _config_digest(normalized)
    if not expected or not secrets.compare_digest(expected, actual):
        raise RuntimeError("registration-node config checksum mismatch")
    return normalized


def _integrity_check(connection: sqlite3.Connection, *, label: str) -> None:
    rows = connection.execute("PRAGMA integrity_check").fetchall()
    messages = [str(row[0]) for row in rows if row]
    if messages != ["ok"]:
        detail = "; ".join(messages[:5]) or "no result"
        raise RuntimeError(f"{label} database integrity check failed: {detail}")


def _default_backup_path(target_db: Path) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return target_db.parent / "backups" / f"{target_db.name}.pre-config-import-{timestamp}.bak"


def _backup_database(
    connection: sqlite3.Connection,
    target_db: Path,
    backup_path: Path | None,
) -> Path:
    destination = backup_path or _default_backup_path(target_db)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(f"backup already exists: {destination}")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    sidecars = [Path(f"{temporary}{suffix}") for suffix in ("-wal", "-shm")]
    try:
        with sqlite3.connect(temporary) as backup_connection:
            connection.backup(backup_connection)
            journal_mode = str(
                backup_connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
            ).lower()
            if journal_mode != "delete":
                raise RuntimeError(
                    f"backup database journal mode is not DELETE: {journal_mode}"
                )
            _integrity_check(backup_connection, label="backup")
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
        for sidecar in sidecars:
            if sidecar.exists():
                sidecar.unlink()
    return destination


def import_configs(
    target_db: Path,
    input_path: Path,
    *,
    replace: bool,
    backup_path: Path | None = None,
) -> dict[str, Any]:
    configs = _read_export(input_path)
    if not target_db.is_file():
        raise FileNotFoundError(f"target database does not exist: {target_db}")
    connection = sqlite3.connect(target_db)
    try:
        _integrity_check(connection, label="target")
        if not _table_exists(connection, "configs"):
            raise RuntimeError("target database has no configs table; start the application once first")
        if _table_exists(connection, "accounts"):
            account_count = int(connection.execute("SELECT COUNT(*) FROM accounts").fetchone()[0])
            if account_count:
                raise RuntimeError(
                    f"target account database is not empty: accounts={account_count}"
                )
        backup = _backup_database(connection, target_db, backup_path)
        connection.execute("BEGIN IMMEDIATE")
        if replace:
            connection.execute("DELETE FROM configs")
        for key, value in configs.items():
            connection.execute(
                "INSERT INTO configs(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
        connection.execute(
            "INSERT INTO configs(key, value) VALUES('auth_jwt_secret', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (secrets.token_urlsafe(64),),
        )
        for key, value in {
            "_config_share_enabled": "false",
            "_config_share_baseline_revision": "",
            "_config_share_detached_at": "",
            "_config_share_last_pull_at": "",
        }.items():
            connection.execute(
                "INSERT INTO configs(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
        connection.commit()
        _integrity_check(connection, label="imported target")
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "config_count": len(configs),
        "config_sha256": _config_digest(configs),
        "target": str(target_db),
        "backup": str(backup),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    export = commands.add_parser("export", help="export a filtered Plus config snapshot")
    export.add_argument("--source-db", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--source-instance", default="auto-gpt-plus")

    import_command = commands.add_parser("import", help="import into an empty node database")
    import_command.add_argument("--target-db", type=Path, required=True)
    import_command.add_argument("--input", type=Path, required=True)
    import_command.add_argument("--replace", action="store_true")
    import_command.add_argument(
        "--backup",
        type=Path,
        help="backup destination; defaults to TARGET_DIR/backups with a UTC timestamp",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.command == "export":
        result = write_export(args.source_db, args.output, args.source_instance)
    else:
        result = import_configs(
            args.target_db,
            args.input,
            replace=args.replace,
            backup_path=args.backup,
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
