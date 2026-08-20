from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "registration-node-config.py"
SPEC = importlib.util.spec_from_file_location("registration_node_config", SCRIPT)
assert SPEC and SPEC.loader
registration_node_config = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(registration_node_config)


def _database(path: Path, *, accounts: int = 0) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE configs (key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '')")
        connection.execute("CREATE TABLE accounts (id INTEGER PRIMARY KEY, email TEXT)")
        for index in range(accounts):
            connection.execute(
                "INSERT INTO accounts(id, email) VALUES(?, ?)",
                (index + 1, f"user{index + 1}@example.com"),
            )


def test_export_filters_instance_state_and_applies_registration_profile(tmp_path: Path):
    source = tmp_path / "source.db"
    _database(source)
    with sqlite3.connect(source) as connection:
        connection.executemany(
            "INSERT INTO configs(key, value) VALUES(?, ?)",
            [
                ("oaipay_api_url", "http://gpt-cccy-me:8789"),
                ("oaipay_api_key", "secret-key"),
                ("auth_password_hash", "password-hash"),
                ("auth_jwt_secret", "shared-jwt-must-not-copy"),
                ("_config_share_enabled", "true"),
                ("chatgpt_gopay_active_batch_task_id", "task-live"),
                ("email_api_lines", "consumable@example.com---https://example.com"),
                ("chatgpt_auto_pipeline_config", json.dumps({"auto_start": True, "platform": "chatgpt"})),
            ],
        )

    configs = registration_node_config.export_configs(source)

    assert configs["oaipay_api_url"] == "http://gpt-cccy-me:8789"
    assert configs["oaipay_api_key"] == "secret-key"
    assert configs["auth_password_hash"] == "password-hash"
    assert "auth_jwt_secret" not in configs
    assert "_config_share_enabled" not in configs
    assert "chatgpt_gopay_active_batch_task_id" not in configs
    assert "email_api_lines" not in configs
    assert json.loads(configs["chatgpt_auto_pipeline_config"])["auto_start"] is False
    assert configs["chatgpt_register_browser_default_concurrency"] == "30"
    assert configs["chatgpt_register_browser_max_concurrency"] == "30"
    assert configs["chatgpt_register_delay_seconds"] == "0"
    assert configs["chatgpt_register_delay_max_seconds"] == "0"
    assert configs["chatgpt_runtime_browser_capacity_mode"] == "fixed"
    assert configs["chatgpt_runtime_auth_browser_max_concurrency"] == "30"
    assert configs["chatgpt_runtime_auth_browser_registration_reserve"] == "0"
    assert configs["chatgpt_runtime_auth_browser_recheck_reserve"] == "0"
    assert configs["chatgpt_runtime_auth_browser_pid_budget"] == "0"
    assert configs["chatgpt_runtime_pid_emergency_reserve"] == "0"
    assert configs["chatgpt_runtime_host_memory_reserve_mib"] == "0"
    assert configs["chatgpt_runtime_cpu_psi_avg10_limit"] == "0"
    assert configs["chatgpt_runtime_auth_browser_launch_interval_seconds"] == "0"
    assert configs["chatgpt_runtime_solver_max_browsers"] == "15"
    assert configs["chatgpt_runtime_solver_warm_browsers"] == "0"


def test_export_import_keeps_target_empty_and_rotates_instance_identity(tmp_path: Path):
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    output = tmp_path / "registration.json"
    _database(source)
    _database(target)
    with sqlite3.connect(target) as connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    with sqlite3.connect(source) as connection:
        connection.executemany(
            "INSERT INTO configs(key, value) VALUES(?, ?)",
            [
                ("auth_password_hash", "same-login"),
                ("auth_jwt_secret", "source-jwt"),
                ("sub2api_api_url", "https://api.example.com"),
            ],
        )

    exported = registration_node_config.write_export(source, output, "auto-gpt-plus")
    imported = registration_node_config.import_configs(target, output, replace=True)

    assert output.stat().st_mode & 0o777 == 0o600
    assert exported["config_sha256"] == imported["config_sha256"]
    with sqlite3.connect(target) as connection:
        values = dict(connection.execute("SELECT key, value FROM configs"))
        account_count = connection.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    assert account_count == 0
    assert values["auth_password_hash"] == "same-login"
    assert values["auth_jwt_secret"] != "source-jwt"
    assert len(values["auth_jwt_secret"]) >= 64
    assert values["_config_share_enabled"] == "false"
    assert values["sub2api_api_url"] == "https://api.example.com"
    backup = Path(imported["backup"])
    assert backup.is_file()
    assert backup.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(backup) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert connection.execute("SELECT COUNT(*) FROM configs").fetchone()[0] == 0
    assert not list(backup.parent.glob(".*.tmp*"))


def test_import_refuses_a_nonempty_account_database(tmp_path: Path):
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    output = tmp_path / "registration.json"
    _database(source)
    _database(target, accounts=1)
    registration_node_config.write_export(source, output, "auto-gpt-plus")

    with pytest.raises(RuntimeError, match="accounts=1"):
        registration_node_config.import_configs(target, output, replace=True)
