from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from scripts import migrate_hme_mailbox_state as migration


def _polluted_state(email: str = "alias@icloud.com", lease: str = "lease-123") -> dict:
    return {
        "provider": "hme_ready_api",
        "email": email,
        "account": {
            "email": email,
            "account_id": lease,
            "extra": {
                "provider": "hme_ready_api",
                "mode": "helper_ready_api",
                "lease_id": lease,
                "checkout_id": lease,
                "hme": email,
                "forward_to": "forward@example.com",
                "forward_mailbox_id": "mailbox-123",
                "copied_global_runtime": "x" * 20_000,
            },
        },
        "before_ids": [f"message-{index:04d}-{'y' * 32}" for index in range(500)],
        "config": {
            "icloud_hme_mode": "helper_ready_api",
            "icloud_hme_helper_api_url": "http://helper.internal",
            "icloud_hme_helper_internal_key": "helper-secret",
            "tempmail_api_url": "http://tempmail.internal",
            "tempmail_api_key": "tempmail-secret",
            "chatgpt_gopay_batch_tasks": "g" * 120_000,
            "chatgpt_gopay_phone_pool": ["+10000000000"] * 1_000,
            "chatgpt_auto_pipeline_config": {"items": ["global"] * 2_000},
        },
        "proxy": "http://proxy.internal:8080",
        "copied_state_sibling": "global",
    }


def _state_at_path(root: dict, path: tuple[str, ...]) -> dict:
    value = root
    for part in path:
        value = value[part]
    return value


def _account_extra_with_all_paths(email: str = "alias@icloud.com") -> dict:
    return {
        "access_token": "account-secret-that-must-not-change",
        "unrelated_metadata": {"keep": True},
        "chatgpt_mailbox_state": _polluted_state(email, "lease-direct"),
        "mailbox_state": _polluted_state(email, "lease-legacy"),
        "chatgpt_invalid_recheck": {
            "status": "pending",
            "mailbox_state": _polluted_state(email, "lease-invalid-recheck"),
        },
        "chatgpt_custom_email_recheck": {
            "status": "pending",
            "mailbox_state": _polluted_state(email, "lease-custom-recheck"),
        },
    }


def _create_database(path: Path, *, account_count: int = 1) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE accounts (
                id INTEGER PRIMARY KEY,
                platform TEXT NOT NULL,
                email TEXT NOT NULL,
                extra_json TEXT NOT NULL
            );
            """
        )
        for index in range(1, account_count + 1):
            email = f"alias-{index}@icloud.com"
            connection.execute(
                "INSERT INTO accounts(id, platform, email, extra_json) VALUES (?, ?, ?, ?)",
                (
                    index,
                    "chatgpt",
                    email,
                    json.dumps(_account_extra_with_all_paths(email), ensure_ascii=False),
                ),
            )
        connection.execute(
            "INSERT INTO accounts(id, platform, email, extra_json) VALUES (?, ?, ?, ?)",
            (10_000, "other", "other@example.com", '{"untouched": true}'),
        )
        connection.commit()
    finally:
        connection.close()


def _add_legacy_pending_table(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE pending_business_invites (
                id INTEGER PRIMARY KEY,
                email TEXT NOT NULL,
                mailbox_state_json TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO pending_business_invites(id, email, mailbox_state_json) "
            "VALUES (?, ?, ?)",
            (
                1,
                "legacy@icloud.com",
                json.dumps(
                    _polluted_state("legacy@icloud.com", "lease-legacy-row"),
                    ensure_ascii=False,
                ),
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _read_account_raw(path: Path, account_id: int = 1) -> str:
    connection = sqlite3.connect(path)
    try:
        row = connection.execute(
            "SELECT extra_json FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
        assert row is not None
        return str(row[0])
    finally:
        connection.close()


def _read_legacy_pending_rows(path: Path) -> list[tuple[int, str, str]]:
    connection = sqlite3.connect(path)
    try:
        return [
            (int(row[0]), str(row[1]), str(row[2]))
            for row in connection.execute(
                "SELECT id, email, mailbox_state_json "
                "FROM pending_business_invites ORDER BY id"
            ).fetchall()
        ]
    finally:
        connection.close()


def test_account_compaction_uses_runtime_sanitizer_for_all_historical_paths():
    original = _account_extra_with_all_paths()
    limits = migration.CompactionLimits(max_before_ids=32, max_before_id_bytes=4096)

    cleaned, meta = migration.sanitize_account_extra(
        original,
        account_email="alias@icloud.com",
        limits=limits,
    )

    assert meta.invalid_shapes == 0
    assert meta.unsanitizable == 0
    assert meta.canonical_promoted is False
    assert meta.canonical_source_path == "chatgpt_mailbox_state"
    assert len(meta.reports) == 4
    assert meta.removed_noncanonical_paths == [
        "mailbox_state",
        "chatgpt_invalid_recheck.mailbox_state",
        "chatgpt_custom_email_recheck.mailbox_state",
    ]
    assert cleaned["access_token"] == original["access_token"]
    assert cleaned["unrelated_metadata"] == {"keep": True}
    assert cleaned["chatgpt_invalid_recheck"]["status"] == "pending"
    assert cleaned["chatgpt_custom_email_recheck"]["status"] == "pending"
    assert "mailbox_state" not in cleaned
    assert "mailbox_state" not in cleaned["chatgpt_invalid_recheck"]
    assert "mailbox_state" not in cleaned["chatgpt_custom_email_recheck"]

    state = cleaned["chatgpt_mailbox_state"]
    assert state["schema_version"] == 2
    assert state["account"]["account_id"] == "lease-direct"
    assert state["account"]["extra"]["forward_mailbox_id"] == "mailbox-123"
    assert "copied_global_runtime" not in state["account"]["extra"]
    assert "chatgpt_gopay_batch_tasks" not in state["config"]
    assert "chatgpt_gopay_phone_pool" not in state["config"]
    assert "chatgpt_auto_pipeline_config" not in state["config"]
    assert "copied_state_sibling" not in state
    assert len(state["before_ids"]) <= 32
    assert len(json.dumps(state["before_ids"], ensure_ascii=False).encode("utf-8")) <= 4096

    # Running the migration transform again must be a semantic no-op.
    second, second_meta = migration.sanitize_account_extra(
        cleaned,
        account_email="alias@icloud.com",
        limits=limits,
    )
    assert second == cleaned
    assert second_meta.invalid_shapes == 0
    assert second_meta.unsanitizable == 0
    assert len(second_meta.reports) == 1
    assert second_meta.removed_noncanonical_paths == []
    assert all(not report.changed for report in second_meta.reports)


def test_missing_canonical_promotes_first_sanitizable_fallback_then_deduplicates():
    original = _account_extra_with_all_paths()
    original.pop("chatgpt_mailbox_state")

    cleaned, meta = migration.sanitize_account_extra(
        original,
        account_email="alias@icloud.com",
        limits=migration.CompactionLimits(),
    )

    assert meta.canonical_promoted is True
    assert meta.canonical_source_path == "mailbox_state"
    assert cleaned["chatgpt_mailbox_state"]["account"]["account_id"] == "lease-legacy"
    assert "mailbox_state" not in cleaned
    assert "mailbox_state" not in cleaned["chatgpt_invalid_recheck"]
    assert "mailbox_state" not in cleaned["chatgpt_custom_email_recheck"]
    assert cleaned["chatgpt_invalid_recheck"]["status"] == "pending"
    assert cleaned["chatgpt_custom_email_recheck"]["status"] == "pending"
    assert len(meta.reports) == 3


def test_fallback_promotion_preserves_non_dict_and_providerless_forensic_values():
    providerless = {
        "email": "alias@icloud.com",
        "config": {"chatgpt_gopay_batch_tasks": "unknown-shape"},
    }
    original = {
        "mailbox_state": providerless,
        "chatgpt_invalid_recheck": {"status": "failed", "mailbox_state": "opaque-state"},
        "chatgpt_custom_email_recheck": {
            "status": "pending",
            "mailbox_state": _polluted_state("alias@icloud.com", "lease-custom"),
        },
    }

    cleaned, meta = migration.sanitize_account_extra(
        original,
        account_email="alias@icloud.com",
        limits=migration.CompactionLimits(),
    )

    assert meta.canonical_promoted is True
    assert meta.canonical_source_path == "chatgpt_custom_email_recheck.mailbox_state"
    assert cleaned["chatgpt_mailbox_state"]["account"]["account_id"] == "lease-custom"
    assert cleaned["mailbox_state"] == providerless
    assert cleaned["chatgpt_invalid_recheck"]["mailbox_state"] == "opaque-state"
    assert "mailbox_state" not in cleaned["chatgpt_custom_email_recheck"]
    assert meta.invalid_shapes == 1
    assert meta.unsanitizable == 1
    assert set(meta.preserved_unsanitizable_paths) == {
        "mailbox_state",
        "chatgpt_invalid_recheck.mailbox_state",
    }


def test_present_providerless_canonical_is_never_overwritten_by_fallback():
    providerless = {"email": "alias@icloud.com", "config": {"unknown": "keep"}}
    original = {
        "chatgpt_mailbox_state": providerless,
        "mailbox_state": _polluted_state("alias@icloud.com", "lease-fallback"),
    }

    cleaned, meta = migration.sanitize_account_extra(
        original,
        account_email="alias@icloud.com",
        limits=migration.CompactionLimits(),
    )

    assert cleaned == original
    assert meta.canonical_promoted is False
    assert meta.canonical_source_path == ""
    assert meta.reports == []
    assert meta.unsanitizable == 1
    assert meta.sanitizable_state_objects == 1


def test_dry_run_is_read_only_and_reports_accounts_and_bounded_batches(tmp_path: Path):
    database = tmp_path / "account_manager.db"
    _create_database(database, account_count=5)
    account_before = _read_account_raw(database)

    report = migration.run_migration(
        database,
        batch_size=2,
        limits=migration.CompactionLimits(max_before_ids=16, max_before_id_bytes=2048),
    )

    assert report["mode"] == "dry-run"
    assert report["backup_path"] == ""
    stats = report["stats"]
    assert stats["scanned_account_rows"] == 5
    assert stats["target_account_rows"] == 5
    assert stats["changed_account_rows"] == 5
    assert stats["pending_table_present"] is False
    assert stats["scanned_pending_rows"] == 0
    assert stats["changed_pending_rows"] == 0
    assert stats["state_objects_sanitized"] == 20
    assert stats["removed_config_keys"]["chatgpt_gopay_batch_tasks"] == 20
    assert stats["canonical_promoted_account_rows"] == 0
    assert stats["removed_noncanonical_state_objects"] == 15
    assert stats["removed_noncanonical_paths"] == {
        "chatgpt_custom_email_recheck.mailbox_state": 5,
        "chatgpt_invalid_recheck.mailbox_state": 5,
        "mailbox_state": 5,
    }
    assert stats["max_batch_rows"] <= 2
    assert stats["net_bytes_removed"] > 0
    assert _read_account_raw(database) == account_before
    assert not (tmp_path / "migration-backups").exists()


def test_dry_run_compacts_legacy_pending_table_in_memory_without_mutating_it(tmp_path: Path):
    database = tmp_path / "account_manager.db"
    _create_database(database)
    _add_legacy_pending_table(database)
    rows_before = _read_legacy_pending_rows(database)

    report = migration.run_migration(
        database,
        batch_size=1,
        limits=migration.CompactionLimits(max_before_ids=16, max_before_id_bytes=2048),
    )

    stats = report["stats"]
    assert report["mode"] == "dry-run"
    assert report["backup_path"] == ""
    assert stats["pending_table_present"] is True
    assert stats["scanned_pending_rows"] == 1
    assert stats["changed_pending_rows"] == 1
    assert _read_legacy_pending_rows(database) == rows_before


def test_apply_creates_verified_original_backup_updates_atomically_and_is_idempotent(tmp_path: Path):
    database = tmp_path / "account_manager.db"
    backup_dir = tmp_path / "backups"
    _create_database(database, account_count=2)
    account_before = _read_account_raw(database)

    report = migration.run_migration(
        database,
        apply=True,
        backup_dir=backup_dir,
        vacuum=True,
        batch_size=1,
    )

    assert report["mode"] == "apply"
    backup_path = Path(report["backup_path"])
    assert backup_path.is_file()
    assert backup_path.stat().st_mode & 0o777 == 0o600
    assert _read_account_raw(backup_path) == account_before

    account_after = json.loads(_read_account_raw(database))
    state = account_after["chatgpt_mailbox_state"]
    assert state["schema_version"] == 2
    assert "chatgpt_gopay_batch_tasks" not in state["config"]
    assert "mailbox_state" not in account_after
    assert "mailbox_state" not in account_after["chatgpt_invalid_recheck"]
    assert "mailbox_state" not in account_after["chatgpt_custom_email_recheck"]

    connection = sqlite3.connect(database)
    try:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    finally:
        connection.close()

    second = migration.run_migration(database, batch_size=1)
    assert second["stats"]["changed_account_rows"] == 0
    assert second["stats"]["changed_pending_rows"] == 0
    assert second["stats"]["net_bytes_removed"] == 0


def test_apply_backs_up_and_compacts_legacy_pending_table_without_deleting_rows(
    tmp_path: Path,
):
    database = tmp_path / "account_manager.db"
    backup_dir = tmp_path / "backups"
    _create_database(database)
    _add_legacy_pending_table(database)
    rows_before = _read_legacy_pending_rows(database)

    report = migration.run_migration(
        database,
        apply=True,
        backup_dir=backup_dir,
        batch_size=1,
    )

    backup_path = Path(report["backup_path"])
    assert _read_legacy_pending_rows(backup_path) == rows_before

    rows_after = _read_legacy_pending_rows(database)
    assert [(row[0], row[1]) for row in rows_after] == [
        (row[0], row[1]) for row in rows_before
    ]
    assert len(rows_after) == len(rows_before) == 1
    compacted_state = json.loads(rows_after[0][2])
    assert compacted_state["schema_version"] == 2
    assert "chatgpt_gopay_batch_tasks" not in compacted_state["config"]

    connection = sqlite3.connect(database)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type = 'table' AND name = 'pending_business_invites'"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM pending_business_invites"
        ).fetchone() == (1,)
    finally:
        connection.close()


def test_apply_rolls_back_all_rows_if_sanitizer_fails_mid_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    database = tmp_path / "account_manager.db"
    _create_database(database, account_count=2)
    before_one = _read_account_raw(database, 1)
    before_two = _read_account_raw(database, 2)
    original_sanitizer = migration.sanitize_mailbox_state
    calls = 0

    def fail_on_fifth_state(state, **kwargs):
        nonlocal calls
        calls += 1
        # Account 1 has four paths; this fails as account 2 begins, after account 1's
        # UPDATE has already executed inside the still-uncommitted transaction.
        if calls == 5:
            raise RuntimeError("synthetic sanitizer failure")
        return original_sanitizer(state, **kwargs)

    monkeypatch.setattr(migration, "sanitize_mailbox_state", fail_on_fifth_state)
    with pytest.raises(migration.MigrationError, match="synthetic sanitizer failure") as exc_info:
        migration.run_migration(
            database,
            apply=True,
            backup_dir=tmp_path / "backups",
            batch_size=1,
        )

    assert "backup=" in str(exc_info.value)
    assert len(list((tmp_path / "backups").glob("*.db"))) == 1
    assert _read_account_raw(database, 1) == before_one
    assert _read_account_raw(database, 2) == before_two


def test_providerless_state_is_reported_and_never_cleared():
    original = {
        "chatgpt_mailbox_state": {
            "email": "alias@icloud.com",
            "config": {"chatgpt_gopay_batch_tasks": "unknown-shape"},
        },
        "unrelated": "keep",
    }

    cleaned, meta = migration.sanitize_account_extra(
        original,
        account_email="alias@icloud.com",
        limits=migration.CompactionLimits(),
    )

    assert cleaned == original
    assert meta.reports == []
    assert meta.invalid_shapes == 0
    assert meta.unsanitizable == 1
    assert meta.preserved_unsanitizable_paths == ["chatgpt_mailbox_state"]
