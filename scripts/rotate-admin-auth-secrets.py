#!/usr/bin/env python3
"""Rotate per-instance administrator JWT base secrets without exposing them."""
from __future__ import annotations

import argparse
import os
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


@dataclass(frozen=True)
class RotationResult:
    instance_id: str
    database: Path
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


def _read_auth_version(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT value FROM configs WHERE key='auth_version'"
    ).fetchone()
    try:
        return max(1, int(str(row[0] if row else "1").strip() or "1"))
    except (TypeError, ValueError):
        return 1


def _rotate_one(instance_id: str, database: Path, secret: str) -> RotationResult:
    now = int(time.time())
    with sqlite3.connect(database, timeout=30, isolation_level=None) as connection:
        connection.execute("PRAGMA busy_timeout=30000")
        _integrity_check(connection)
        _require_configs_table(connection)
        connection.execute("BEGIN IMMEDIATE")
        try:
            previous_version = _read_auth_version(connection)
            next_version = previous_version + 1
            connection.execute(
                """
                INSERT INTO configs(key, value) VALUES('auth_jwt_secret', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (secret,),
            )
            connection.execute(
                """
                INSERT INTO configs(key, value) VALUES('auth_version', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (str(next_version),),
            )
            has_sessions = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='admin_auth_sessions'"
            ).fetchone()
            revoked = 0
            if has_sessions is not None:
                cursor = connection.execute(
                    """
                    UPDATE admin_auth_sessions
                    SET revoked_at=?, revoke_reason='jwt_secret_rotated'
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
    return RotationResult(
        instance_id=instance_id,
        database=database,
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
    parser.add_argument("--apply", action="store_true", help="perform rotation; default is dry-run")
    parser.add_argument(
        "--database",
        action="append",
        type=_parse_database,
        help="override targets with INSTANCE=/absolute/path.db (repeatable)",
    )
    parser.add_argument(
        "--backup-root",
        type=Path,
        default=Path("/root/auto-gpt-auth-rotation-backups"),
    )
    args = parser.parse_args(argv)

    targets = tuple(args.database or DEFAULT_DATABASES)
    if len({instance for instance, _ in targets}) != len(targets):
        parser.error("instance IDs must be unique")
    if len({path.resolve() for _, path in targets}) != len(targets):
        parser.error("database paths must be unique")

    for instance_id, database in targets:
        if not database.is_file():
            raise FileNotFoundError(f"{instance_id}: database not found: {database}")
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=30) as connection:
            _integrity_check(connection)
            _require_configs_table(connection)

    if not args.apply:
        print(f"dry-run: {len(targets)} instance databases passed integrity and schema checks")
        return 0

    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")
    backup_dir = args.backup_root / stamp
    backup_dir.mkdir(parents=True, exist_ok=False)
    os.chmod(backup_dir, 0o700)

    generated = {instance_id: secrets.token_hex(48) for instance_id, _ in targets}
    if len(set(generated.values())) != len(generated):
        raise RuntimeError("secret generator returned a duplicate value")

    results: list[RotationResult] = []
    try:
        for instance_id, database in targets:
            backup_path = backup_dir / f"{instance_id}.account_manager.before-secret-rotation.db"
            _backup_database(database, backup_path)
        for instance_id, database in targets:
            results.append(_rotate_one(instance_id, database, generated[instance_id]))
    except Exception:
        print(f"rotation failed; untouched backups are in {backup_dir}", file=sys.stderr)
        raise
    finally:
        generated.clear()

    print(f"rotated {len(results)} instance JWT secrets; values were not printed or exported")
    for result in results:
        print(
            f"{result.instance_id}: auth_version "
            f"{result.previous_auth_version}->{result.auth_version}; "
            f"revoked_sessions={result.revoked_sessions}"
        )
    print(f"backups={backup_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
