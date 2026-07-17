from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "rotate-admin-auth-secrets.py"
SPEC = importlib.util.spec_from_file_location("rotate_admin_auth_secrets", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def _create_database(path: Path, instance_id: str) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE configs (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.executemany(
            "INSERT INTO configs(key, value) VALUES(?, ?)",
            [
                ("auth_jwt_secret", "legacy-shared-secret"),
                ("auth_version", "3"),
                ("auth_password_hash", "password-hash-must-not-change"),
                ("auth_totp_secret", "totp-secret-must-not-change"),
            ],
        )
        connection.execute(
            """
            CREATE TABLE admin_auth_sessions (
                jti TEXT PRIMARY KEY,
                instance_id TEXT NOT NULL,
                revoked_at INTEGER NOT NULL DEFAULT 0,
                revoke_reason TEXT NOT NULL DEFAULT ''
            )
            """
        )
        connection.execute(
            "INSERT INTO admin_auth_sessions(jti, instance_id) VALUES(?, ?)",
            (f"jti-{instance_id}", instance_id),
        )
        connection.commit()


def _configs(path: Path) -> dict[str, str]:
    with sqlite3.connect(path) as connection:
        return dict(connection.execute("SELECT key, value FROM configs"))


def test_rotation_uses_distinct_secrets_preserves_credentials_and_backs_up(tmp_path):
    targets = []
    for instance_id in ("auto-gpt", "auto-gpt-plus", "auto-plus2"):
        database = tmp_path / f"{instance_id}.db"
        _create_database(database, instance_id)
        targets.append((instance_id, database))

    backup_root = tmp_path / "backups"
    arguments = ["--apply", "--backup-root", str(backup_root)]
    for instance_id, database in targets:
        arguments.extend(["--database", f"{instance_id}={database}"])

    assert MODULE.main(arguments) == 0

    values = [_configs(database) for _, database in targets]
    assert len({item["auth_jwt_secret"] for item in values}) == 3
    assert all(item["auth_jwt_secret"] != "legacy-shared-secret" for item in values)
    assert all(item["auth_version"] == "4" for item in values)
    assert all(item["auth_password_hash"] == "password-hash-must-not-change" for item in values)
    assert all(item["auth_totp_secret"] == "totp-secret-must-not-change" for item in values)

    for instance_id, database in targets:
        with sqlite3.connect(database) as connection:
            session = connection.execute(
                "SELECT revoked_at, revoke_reason FROM admin_auth_sessions WHERE instance_id=?",
                (instance_id,),
            ).fetchone()
        assert session is not None and session[0] > 0
        assert session[1] == "jwt_secret_rotated"

    backup_dirs = list(backup_root.iterdir())
    assert len(backup_dirs) == 1
    backups = list(backup_dirs[0].glob("*.before-secret-rotation.db"))
    assert len(backups) == 3
    assert all(_configs(path)["auth_jwt_secret"] == "legacy-shared-secret" for path in backups)


def test_dry_run_does_not_modify_database_or_create_backups(tmp_path):
    database = tmp_path / "auto-gpt.db"
    _create_database(database, "auto-gpt")
    backup_root = tmp_path / "backups"

    assert MODULE.main([
        "--database",
        f"auto-gpt={database}",
        "--backup-root",
        str(backup_root),
    ]) == 0

    assert _configs(database)["auth_jwt_secret"] == "legacy-shared-secret"
    assert not backup_root.exists()
