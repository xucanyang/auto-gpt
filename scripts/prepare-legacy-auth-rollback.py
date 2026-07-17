#!/usr/bin/env python3
"""Prepare live auth config for a rollback to a pre-Argon2 Auto-GPT image.

The v2.3 runtime migrates a successfully verified legacy SHA-256 password to
Argon2id. Images older than v2.3 cannot verify that format. This helper restores
only the pre-deploy password/TOTP settings from a deploy.sh --backup directory,
rotates JWT secrets again, and leaves all business data untouched.
"""
from __future__ import annotations

import argparse
import os
import re
import secrets
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


DEFAULT_DATABASES = (
    ("auto-gpt", Path("/opt/auto-gpt/data/account_manager.db")),
    ("auto-gpt-plus", Path("/opt/auto-gpt-plus/data/account_manager.db")),
    ("auto-plus2", Path("/opt/auto-plus2/data/account_manager.db")),
)
LEGACY_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


@dataclass(frozen=True)
class PreparedRollback:
    instance_id: str
    previous_auth_version: int
    auth_version: int
    revoked_sessions: int


def _integrity_check(connection: sqlite3.Connection) -> None:
    row = connection.execute("PRAGMA integrity_check").fetchone()
    if not row or str(row[0]).strip().lower() != "ok":
        raise RuntimeError(f"SQLite integrity check failed: {row!r}")


def _require_configs_table(connection: sqlite3.Connection) -> None:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='configs'"
    ).fetchone()
    if row is None:
        raise RuntimeError("configs table is missing")


def _backup_database(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=30) as source_db:
        _integrity_check(source_db)
        with sqlite3.connect(destination, timeout=30) as backup_db:
            source_db.backup(backup_db)
            _integrity_check(backup_db)
    os.chmod(destination, 0o600)


def _read_optional_config(
    connection: sqlite3.Connection,
    key: str,
) -> tuple[bool, str]:
    row = connection.execute("SELECT value FROM configs WHERE key=?", (key,)).fetchone()
    return (row is not None, str(row[0] if row is not None else ""))


def _read_auth_version(connection: sqlite3.Connection) -> int:
    found, raw = _read_optional_config(connection, "auth_version")
    try:
        return max(1, int(raw if found else "1"))
    except (TypeError, ValueError):
        return 1


def _validate_legacy_source(path: Path) -> tuple[str, tuple[bool, str]]:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30) as connection:
        _integrity_check(connection)
        _require_configs_table(connection)
        found, password_hash = _read_optional_config(connection, "auth_password_hash")
        if not found or not LEGACY_SHA256_RE.fullmatch(password_hash.strip()):
            raise RuntimeError(
                f"legacy backup does not contain a SHA-256 admin password hash: {path}"
            )
        totp = _read_optional_config(connection, "auth_totp_secret")
    return password_hash.strip().lower(), totp


def _upsert_config(connection: sqlite3.Connection, key: str, value: str) -> None:
    connection.execute(
        """
        INSERT INTO configs(key, value) VALUES(?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (key, value),
    )


def _prepare_one(
    *,
    instance_id: str,
    live_database: Path,
    legacy_password_hash: str,
    legacy_totp: tuple[bool, str],
    fresh_jwt_secret: str,
) -> PreparedRollback:
    now = int(time.time())
    with sqlite3.connect(live_database, timeout=30, isolation_level=None) as connection:
        connection.execute("PRAGMA busy_timeout=30000")
        _integrity_check(connection)
        _require_configs_table(connection)
        connection.execute("BEGIN IMMEDIATE")
        try:
            previous_version = _read_auth_version(connection)
            next_version = previous_version + 1
            _upsert_config(connection, "auth_password_hash", legacy_password_hash)
            if legacy_totp[0]:
                _upsert_config(connection, "auth_totp_secret", legacy_totp[1])
            else:
                connection.execute("DELETE FROM configs WHERE key='auth_totp_secret'")
            _upsert_config(connection, "auth_jwt_secret", fresh_jwt_secret)
            _upsert_config(connection, "auth_version", str(next_version))

            has_sessions = connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='admin_auth_sessions'"
            ).fetchone()
            revoked = 0
            if has_sessions is not None:
                cursor = connection.execute(
                    """
                    UPDATE admin_auth_sessions
                    SET revoked_at=?, revoke_reason='legacy_image_auth_rollback'
                    WHERE instance_id=? AND revoked_at=0
                    """,
                    (now, instance_id),
                )
                revoked = max(0, int(cursor.rowcount or 0))
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        _integrity_check(connection)
    return PreparedRollback(
        instance_id=instance_id,
        previous_auth_version=previous_version,
        auth_version=next_version,
        revoked_sessions=revoked,
    )


def _parse_database(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("database must use INSTANCE=/absolute/path.db")
    instance_id, raw_path = value.split("=", 1)
    instance_id = instance_id.strip()
    path = Path(raw_path.strip())
    if not instance_id or not path.is_absolute():
        raise argparse.ArgumentTypeError("database must use INSTANCE=/absolute/path.db")
    return instance_id, path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--backup-dir",
        type=Path,
        required=True,
        help="deploy.sh --backup directory containing *.before_deploy.bak files",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="prepare live auth config; default is validation-only dry-run",
    )
    parser.add_argument(
        "--database",
        action="append",
        type=_parse_database,
        help="override live targets with INSTANCE=/absolute/path.db (repeatable)",
    )
    parser.add_argument(
        "--current-backup-root",
        type=Path,
        default=Path("/root/auto-gpt-auth-legacy-rollback-backups"),
        help="where to back up current live DBs before auth-only changes",
    )
    args = parser.parse_args(argv)

    backup_dir = args.backup_dir.resolve()
    if not backup_dir.is_dir():
        raise FileNotFoundError(backup_dir)

    targets = tuple(args.database or DEFAULT_DATABASES)
    if len({instance for instance, _ in targets}) != len(targets):
        parser.error("instance IDs must be unique")
    if len({path.resolve() for _, path in targets}) != len(targets):
        parser.error("live database paths must be unique")

    validated: list[tuple[str, Path, Path, str, tuple[bool, str]]] = []
    for instance_id, live_database in targets:
        if not live_database.is_file():
            raise FileNotFoundError(f"{instance_id}: live database not found: {live_database}")
        legacy_database = backup_dir / f"{instance_id}.account_manager.db.before_deploy.bak"
        if not legacy_database.is_file():
            raise FileNotFoundError(
                f"{instance_id}: deploy backup database not found: {legacy_database}"
            )
        if live_database.resolve() == legacy_database.resolve():
            raise RuntimeError(f"{instance_id}: live and backup database paths are identical")
        with sqlite3.connect(
            f"file:{live_database}?mode=ro", uri=True, timeout=30
        ) as connection:
            _integrity_check(connection)
            _require_configs_table(connection)
        password_hash, totp = _validate_legacy_source(legacy_database)
        validated.append(
            (instance_id, live_database, legacy_database, password_hash, totp)
        )

    if not args.apply:
        print(
            f"dry-run: {len(validated)} live/legacy database pairs passed integrity, "
            "schema and SHA-256 compatibility checks"
        )
        return 0

    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")
    current_backup_dir = args.current_backup_root / stamp
    current_backup_dir.mkdir(parents=True, exist_ok=False)
    os.chmod(current_backup_dir, 0o700)

    generated = {instance_id: secrets.token_hex(48) for instance_id, *_ in validated}
    if len(set(generated.values())) != len(generated):
        raise RuntimeError("secret generator returned a duplicate value")

    results: list[PreparedRollback] = []
    try:
        for instance_id, live_database, *_ in validated:
            _backup_database(
                live_database,
                current_backup_dir
                / f"{instance_id}.account_manager.before-auth-only-rollback.db",
            )
        for instance_id, live_database, _, password_hash, totp in validated:
            results.append(
                _prepare_one(
                    instance_id=instance_id,
                    live_database=live_database,
                    legacy_password_hash=password_hash,
                    legacy_totp=totp,
                    fresh_jwt_secret=generated[instance_id],
                )
            )
    except Exception:
        print(
            f"legacy rollback preparation failed; untouched current backups are in "
            f"{current_backup_dir}",
            file=sys.stderr,
        )
        raise
    finally:
        generated.clear()

    print(
        f"prepared {len(results)} instance auth configs for a pre-v2.3 image; "
        "business data was not restored and JWT secrets were not printed"
    )
    for result in results:
        print(
            f"{result.instance_id}: auth_version "
            f"{result.previous_auth_version}->{result.auth_version}; "
            f"revoked_sessions={result.revoked_sessions}"
        )
    print(f"current_backups={current_backup_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
