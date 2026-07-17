from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "prepare-legacy-auth-rollback.py"
SPEC = importlib.util.spec_from_file_location("prepare_legacy_auth_rollback", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def _create_database(
    path: Path,
    *,
    instance_id: str,
    password_hash: str,
    totp_secret: str | None,
    jwt_secret: str,
    auth_version: int,
    business_value: str,
) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE configs (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        values = [
            ("auth_password_hash", password_hash),
            ("auth_jwt_secret", jwt_secret),
            ("auth_version", str(auth_version)),
        ]
        if totp_secret is not None:
            values.append(("auth_totp_secret", totp_secret))
        connection.executemany("INSERT INTO configs(key, value) VALUES(?, ?)", values)
        connection.execute("CREATE TABLE business_state (value TEXT NOT NULL)")
        connection.execute("INSERT INTO business_state(value) VALUES(?)", (business_value,))
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


def _business_value(path: Path) -> str:
    with sqlite3.connect(path) as connection:
        return str(connection.execute("SELECT value FROM business_state").fetchone()[0])


def test_apply_restores_only_legacy_credentials_and_rotates_sessions(tmp_path):
    backup_dir = tmp_path / "deploy-backup"
    backup_dir.mkdir()
    current_backup_root = tmp_path / "current-backups"
    targets = []

    for index, instance_id in enumerate(("auto-gpt", "auto-gpt-plus", "auto-plus2"), 1):
        live = tmp_path / f"{instance_id}.live.db"
        legacy = backup_dir / f"{instance_id}.account_manager.db.before_deploy.bak"
        _create_database(
            live,
            instance_id=instance_id,
            password_hash="$argon2id$v=19$m=19456,t=2,p=1$live$hash",
            totp_secret=f"live-totp-{index}",
            jwt_secret="live-jwt-secret",
            auth_version=7,
            business_value=f"live-business-{index}",
        )
        _create_database(
            legacy,
            instance_id=instance_id,
            password_hash=(str(index) * 64),
            totp_secret=None if instance_id == "auto-gpt" else f"legacy-totp-{index}",
            jwt_secret="unsafe-old-jwt-secret",
            auth_version=2,
            business_value=f"stale-business-{index}",
        )
        targets.append((instance_id, live))

    arguments = [
        "--backup-dir",
        str(backup_dir),
        "--apply",
        "--current-backup-root",
        str(current_backup_root),
    ]
    for instance_id, live in targets:
        arguments.extend(["--database", f"{instance_id}={live}"])

    assert MODULE.main(arguments) == 0

    jwt_secrets = set()
    for index, (instance_id, live) in enumerate(targets, 1):
        values = _configs(live)
        assert values["auth_password_hash"] == str(index) * 64
        if instance_id == "auto-gpt":
            assert "auth_totp_secret" not in values
        else:
            assert values["auth_totp_secret"] == f"legacy-totp-{index}"
        assert values["auth_jwt_secret"] not in {
            "live-jwt-secret",
            "unsafe-old-jwt-secret",
        }
        jwt_secrets.add(values["auth_jwt_secret"])
        assert values["auth_version"] == "8"
        assert _business_value(live) == f"live-business-{index}"
        with sqlite3.connect(live) as connection:
            session = connection.execute(
                "SELECT revoked_at, revoke_reason FROM admin_auth_sessions WHERE instance_id=?",
                (instance_id,),
            ).fetchone()
        assert session is not None and session[0] > 0
        assert session[1] == "legacy_image_auth_rollback"

    assert len(jwt_secrets) == 3
    backup_dirs = list(current_backup_root.iterdir())
    assert len(backup_dirs) == 1
    current_backups = list(
        backup_dirs[0].glob("*.account_manager.before-auth-only-rollback.db")
    )
    assert len(current_backups) == 3
    assert all(_configs(path)["auth_password_hash"].startswith("$argon2id$") for path in current_backups)


def test_dry_run_does_not_modify_live_database(tmp_path):
    backup_dir = tmp_path / "deploy-backup"
    backup_dir.mkdir()
    live = tmp_path / "auto-gpt.live.db"
    legacy = backup_dir / "auto-gpt.account_manager.db.before_deploy.bak"
    _create_database(
        live,
        instance_id="auto-gpt",
        password_hash="$argon2id$live",
        totp_secret="live-totp",
        jwt_secret="live-jwt",
        auth_version=5,
        business_value="live-business",
    )
    _create_database(
        legacy,
        instance_id="auto-gpt",
        password_hash="a" * 64,
        totp_secret=None,
        jwt_secret="old-jwt",
        auth_version=1,
        business_value="old-business",
    )

    assert MODULE.main([
        "--backup-dir",
        str(backup_dir),
        "--database",
        f"auto-gpt={live}",
        "--current-backup-root",
        str(tmp_path / "unused"),
    ]) == 0
    assert _configs(live)["auth_password_hash"] == "$argon2id$live"
    assert _business_value(live) == "live-business"
    assert not (tmp_path / "unused").exists()


def test_rejects_backup_without_legacy_sha256_hash(tmp_path):
    backup_dir = tmp_path / "deploy-backup"
    backup_dir.mkdir()
    live = tmp_path / "auto-gpt.live.db"
    legacy = backup_dir / "auto-gpt.account_manager.db.before_deploy.bak"
    for path, value in ((live, "$argon2id$live"), (legacy, "$argon2id$already-migrated")):
        _create_database(
            path,
            instance_id="auto-gpt",
            password_hash=value,
            totp_secret=None,
            jwt_secret="jwt",
            auth_version=1,
            business_value="business",
        )

    with pytest.raises(RuntimeError, match="SHA-256"):
        MODULE.main([
            "--backup-dir",
            str(backup_dir),
            "--database",
            f"auto-gpt={live}",
        ])
