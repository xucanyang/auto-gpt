from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import pytest

from services.long_link_history_sync import synchronize_long_link_success_history
from services.chatgpt_core.payment_link_cache import PIX_CLEANED_STATUSES


def _create_source(path: Path, rows: list[dict]) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            CREATE TABLE long_link_success_history (
                job_id TEXT PRIMARY KEY,
                completed_at INTEGER NOT NULL,
                account_email TEXT NOT NULL DEFAULT '',
                long_url TEXT NOT NULL,
                link_type TEXT NOT NULL,
                source TEXT NOT NULL,
                billing_country TEXT NOT NULL DEFAULT '',
                currency TEXT NOT NULL DEFAULT '',
                payment_method_type TEXT NOT NULL DEFAULT '',
                cs_id TEXT NOT NULL DEFAULT '',
                link_expires_at INTEGER
            )
            """
        )
        for row in rows:
            connection.execute(
                """
                INSERT INTO long_link_success_history (
                    job_id, completed_at, account_email, long_url, link_type, source,
                    billing_country, currency, payment_method_type, cs_id, link_expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["job_id"],
                    row["completed_at"],
                    row["account_email"],
                    row["long_url"],
                    row.get("link_type", "pix"),
                    row.get("source", "admin"),
                    row.get("billing_country", "BR"),
                    row.get("currency", "BRL"),
                    row.get("payment_method_type", "pix"),
                    row.get("cs_id", "cs-test"),
                    row.get("link_expires_at"),
                ),
            )
        connection.commit()
    finally:
        connection.close()


def _create_target(path: Path, accounts: list[dict]) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE accounts (
                id INTEGER PRIMARY KEY,
                platform TEXT NOT NULL,
                email TEXT NOT NULL,
                cashier_url TEXT NOT NULL DEFAULT '',
                extra_json TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE payment_link_generations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                task_id TEXT NOT NULL DEFAULT '',
                request_id TEXT NOT NULL UNIQUE,
                remote_batch_id TEXT NOT NULL DEFAULT '',
                remote_job_id TEXT NOT NULL DEFAULT '',
                profile_hash TEXT NOT NULL DEFAULT '',
                link_type TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '',
                url TEXT NOT NULL DEFAULT '',
                submitted_at TEXT NOT NULL DEFAULT '',
                started_at TEXT NOT NULL DEFAULT '',
                generated_at TEXT NOT NULL DEFAULT '',
                persisted_at TEXT NOT NULL DEFAULT '',
                sanitized_error TEXT NOT NULL DEFAULT '',
                result_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            );
            """
        )
        for account in accounts:
            connection.execute(
                """
                INSERT INTO accounts(id, platform, email, cashier_url, extra_json, updated_at)
                VALUES (?, 'chatgpt', ?, ?, ?, ?)
                """,
                (
                    account["id"],
                    account["email"],
                    account.get("cashier_url", ""),
                    json.dumps(account.get("extra", {}), ensure_ascii=False),
                    account.get("updated_at", "2026-07-01T00:00:00+00:00"),
                ),
            )
        connection.commit()
    finally:
        connection.close()


def _source_row(job_id: str, email: str, *, completed_at: int, url: str) -> dict:
    return {
        "job_id": job_id,
        "completed_at": completed_at,
        "account_email": email,
        "long_url": url,
        "link_type": "pix",
        "source": "admin",
        "billing_country": "BR",
        "currency": "BRL",
        "payment_method_type": "pix",
        "cs_id": f"cs-{job_id}",
        "link_expires_at": completed_at + 3600,
    }


def _add_status_column(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE accounts ADD COLUMN status TEXT NOT NULL DEFAULT 'registered'")


def test_dry_run_never_writes_target(tmp_path: Path):
    source = tmp_path / "tasks.db"
    target = tmp_path / "account_manager.db"
    _create_source(source, [_source_row("job-one", "one@example.test", completed_at=1_700_000_000, url="https://pay.example.test/one")])
    _create_target(target, [{"id": 1, "email": "one@example.test", "extra": {"keep": True}}])

    report = synchronize_long_link_success_history(
        source_database=source,
        target_databases=[target],
        apply=False,
    )

    assert report["mode"] == "dry-run"
    assert report["targets"][0]["stats"]["generation_insert"] == 1
    assert report["targets"][0]["stats"]["current_link_planned_updated"] == 1
    with sqlite3.connect(target) as connection:
        assert connection.execute("SELECT COUNT(*) FROM payment_link_generations").fetchone()[0] == 0
        raw_extra = connection.execute("SELECT extra_json FROM accounts WHERE id = 1").fetchone()[0]
    assert json.loads(raw_extra) == {"keep": True}


def test_apply_imports_all_history_but_only_latest_link_becomes_current(tmp_path: Path):
    source = tmp_path / "tasks.db"
    target = tmp_path / "account_manager.db"
    _create_source(
        source,
        [
            _source_row("job-old", "one@example.test", completed_at=1_700_000_000, url="https://pay.example.test/old"),
            _source_row("job-new", "one@example.test", completed_at=1_700_000_100, url="https://pay.example.test/new"),
        ],
    )
    _create_target(target, [{"id": 1, "email": "ONE@example.test", "extra": {"keep": {"value": True}}}])
    _add_status_column(target)
    backup_dir = tmp_path / "backups"

    report = synchronize_long_link_success_history(
        source_database=source,
        target_databases=[target],
        apply=True,
        backup_dir=backup_dir,
    )

    target_report = report["targets"][0]
    assert Path(target_report["backup"]).suffix == ".backup"
    assert Path(target_report["backup"]).is_file()
    assert target_report["stats"]["generation_applied_inserted"] == 2
    assert target_report["stats"]["current_link_updated"] == 1
    with sqlite3.connect(target) as connection:
        histories = connection.execute(
            "SELECT request_id, remote_job_id, status, url FROM payment_link_generations ORDER BY request_id"
        ).fetchall()
        cashier_url, extra_json, status = connection.execute(
            "SELECT cashier_url, extra_json, status FROM accounts WHERE id = 1"
        ).fetchone()
    assert histories == [
        ("long-link-history:job-new", "job-new", "succeeded", "https://pay.example.test/new"),
        ("long-link-history:job-old", "job-old", "succeeded", "https://pay.example.test/old"),
    ]
    extra = json.loads(extra_json)
    assert extra["keep"] == {"value": True}
    assert extra["chatgpt_last_payment_link"]["url"] == "https://pay.example.test/new"
    assert extra["chatgpt_last_payment_link"]["link_expires_at"] == 1_700_003_700
    assert cashier_url == "https://pay.example.test/new"
    assert status == "registered"


def test_existing_newer_current_link_is_not_overwritten_but_history_is_imported(tmp_path: Path):
    source = tmp_path / "tasks.db"
    target = tmp_path / "account_manager.db"
    _create_source(source, [_source_row("job-old", "one@example.test", completed_at=1_700_000_000, url="https://pay.example.test/old")])
    _create_target(
        target,
        [
            {
                "id": 1,
                "email": "one@example.test",
                "cashier_url": "https://pay.example.test/current",
                "extra": {
                    "chatgpt_last_payment_link": {
                        "url": "https://pay.example.test/current",
                        "generated_at": "2024-01-01T00:00:00+00:00",
                        "link_type": "pix",
                    }
                },
            }
        ],
    )

    report = synchronize_long_link_success_history(
        source_database=source,
        target_databases=[target],
        apply=True,
        backup_dir=tmp_path / "backups",
    )

    assert report["targets"][0]["stats"]["current_link_retained"] == 1
    with sqlite3.connect(target) as connection:
        cashier_url, extra_json = connection.execute(
            "SELECT cashier_url, extra_json FROM accounts WHERE id = 1"
        ).fetchone()
        assert connection.execute("SELECT COUNT(*) FROM payment_link_generations").fetchone()[0] == 1
    assert cashier_url == "https://pay.example.test/current"
    assert json.loads(extra_json)["chatgpt_last_payment_link"]["url"] == "https://pay.example.test/current"


@pytest.mark.parametrize("cleaned_status", sorted(PIX_CLEANED_STATUSES))
def test_pix_cleanup_tombstone_blocks_old_history_from_restoring_current_url(tmp_path: Path, cleaned_status: str):
    source = tmp_path / "tasks.db"
    target = tmp_path / "account_manager.db"
    completed_at = 1_700_000_000
    _create_source(
        source,
        [_source_row("job-cleaned", "one@example.test", completed_at=completed_at, url="https://pay.example.test/expired")],
    )
    _create_target(
        target,
        [
            {
                "id": 1,
                "email": "one@example.test",
                "extra": {
                    "chatgpt_last_payment_link": {
                        "link_type": "pix",
                        "link_status": cleaned_status,
                        "generated_at": "2023-11-14T22:13:20+00:00",
                        "pix_cleanup_through_at": "2023-11-15T03:00:00+00:00",
                        "cleaned_at": "2023-11-15T04:00:00+00:00",
                    }
                },
            }
        ],
    )

    report = synchronize_long_link_success_history(
        source_database=source,
        target_databases=[target],
        apply=True,
        backup_dir=tmp_path / "backups",
    )

    assert report["targets"][0]["stats"]["current_link_retained"] == 1
    with sqlite3.connect(target) as connection:
        cashier_url, extra_json = connection.execute(
            "SELECT cashier_url, extra_json FROM accounts WHERE id = 1"
        ).fetchone()
        assert connection.execute("SELECT COUNT(*) FROM payment_link_generations").fetchone()[0] == 1
    assert cashier_url == ""
    marker = json.loads(extra_json)["chatgpt_last_payment_link"]
    assert marker["link_status"] == cleaned_status
    assert "url" not in marker


def test_duplicate_global_email_is_skipped_and_rerun_is_idempotent(tmp_path: Path):
    source = tmp_path / "tasks.db"
    plus = tmp_path / "plus.db"
    plus2 = tmp_path / "plus2.db"
    _create_source(source, [_source_row("job-one", "one@example.test", completed_at=1_700_000_000, url="https://pay.example.test/one")])
    _create_target(plus, [{"id": 1, "email": "one@example.test"}])
    _create_target(plus2, [{"id": 2, "email": "one@example.test"}])

    dry_run = synchronize_long_link_success_history(
        source_database=source,
        target_databases=[plus, plus2],
        apply=False,
    )
    assert dry_run["mapping"]["skipped_ambiguous_email"] == 1

    with sqlite3.connect(plus) as connection:
        assert connection.execute("SELECT COUNT(*) FROM payment_link_generations").fetchone()[0] == 0
    with sqlite3.connect(plus2) as connection:
        assert connection.execute("SELECT COUNT(*) FROM payment_link_generations").fetchone()[0] == 0

    source2 = tmp_path / "tasks2.db"
    target2 = tmp_path / "target2.db"
    _create_source(source2, [_source_row("job-repeat", "unique@example.test", completed_at=1_700_000_000, url="https://pay.example.test/repeat")])
    _create_target(target2, [{"id": 3, "email": "unique@example.test"}])
    first = synchronize_long_link_success_history(
        source_database=source2,
        target_databases=[target2],
        apply=True,
        backup_dir=tmp_path / "backups-first",
    )
    second = synchronize_long_link_success_history(
        source_database=source2,
        target_databases=[target2],
        apply=True,
        backup_dir=tmp_path / "backups-second",
    )
    assert first["targets"][0]["stats"]["generation_applied_inserted"] == 1
    assert second["targets"][0]["stats"]["generation_applied_existing"] == 1
    with sqlite3.connect(target2) as connection:
        assert connection.execute("SELECT COUNT(*) FROM payment_link_generations").fetchone()[0] == 1


def test_existing_remote_job_is_not_inserted_twice_and_backfills_its_current_cache(tmp_path: Path):
    source = tmp_path / "tasks.db"
    target = tmp_path / "account_manager.db"
    source_row = _source_row(
        "job-existing",
        "one@example.test",
        completed_at=1_700_000_000,
        url="https://pay.example.test/existing",
    )
    _create_source(source, [source_row])
    _create_target(target, [{"id": 1, "email": "one@example.test"}])
    with sqlite3.connect(target) as connection:
        connection.execute(
            """
            INSERT INTO payment_link_generations (
                account_id, task_id, request_id, remote_batch_id, remote_job_id,
                profile_hash, link_type, status, url, submitted_at, started_at,
                generated_at, persisted_at, sanitized_error, result_json, created_at, updated_at
            ) VALUES (1, 'existing-task', 'existing-request', '', 'job-existing', '', 'pix',
                      'succeeded', 'https://pay.example.test/existing', '', '',
                      '2023-11-14T22:13:20+00:00', '', '', '{}', '', '')
            """
        )
        connection.commit()

    report = synchronize_long_link_success_history(
        source_database=source,
        target_databases=[target],
        apply=True,
        backup_dir=tmp_path / "backups",
    )

    stats = report["targets"][0]["stats"]
    assert stats["generation_applied_existing"] == 1
    assert stats["current_link_updated"] == 1
    with sqlite3.connect(target) as connection:
        assert connection.execute("SELECT COUNT(*) FROM payment_link_generations").fetchone()[0] == 1
        extra_json = connection.execute("SELECT extra_json FROM accounts WHERE id = 1").fetchone()[0]
    assert json.loads(extra_json)["chatgpt_last_payment_link"]["remote_job_id"] == "job-existing"


def test_paypal_history_keeps_generic_long_link_contract_and_legacy_payload(tmp_path: Path):
    source = tmp_path / "tasks.db"
    target = tmp_path / "account_manager.db"
    row = _source_row(
        "job-paypal",
        "one@example.test",
        completed_at=1_700_000_000,
        url="https://www.paypal.com/checkoutnow?token=test",
    )
    row.update({"link_type": "paypal", "payment_method_type": "paypal", "link_expires_at": None})
    _create_source(source, [row])
    _create_target(target, [{"id": 1, "email": "one@example.test"}])

    synchronize_long_link_success_history(
        source_database=source,
        target_databases=[target],
        apply=True,
        backup_dir=tmp_path / "backups",
    )

    with sqlite3.connect(target) as connection:
        extra_json = connection.execute("SELECT extra_json FROM accounts WHERE id = 1").fetchone()[0]
    extra = json.loads(extra_json)
    assert extra["chatgpt_last_payment_link"]["payment_link_format"] == "long_link"
    assert extra["chatgpt_last_payment_link"]["paypal_url"] == row["long_url"]
    assert extra["chatgpt_paypal_url"]["paypal_url"] == row["long_url"]


def test_upi_history_preserves_qr_expiry_and_payment_type(tmp_path: Path):
    source = tmp_path / "tasks.db"
    target = tmp_path / "account_manager.db"
    row = _source_row(
        "job-upi",
        "one@example.test",
        completed_at=1_700_000_000,
        url="https://payments.stripe.com/upi/instructions/upi-history",
    )
    row.update({"link_type": "upi", "payment_method_type": "upi", "link_expires_at": 1_700_000_300})
    _create_source(source, [row])
    _create_target(target, [{"id": 1, "email": "one@example.test"}])

    synchronize_long_link_success_history(
        source_database=source,
        target_databases=[target],
        apply=True,
        backup_dir=tmp_path / "backups",
    )

    with sqlite3.connect(target) as connection:
        extra_json = connection.execute("SELECT extra_json FROM accounts WHERE id = 1").fetchone()[0]
    payload = json.loads(extra_json)["chatgpt_last_payment_link"]
    assert payload["link_type"] == "upi"
    assert payload["payment_method_type"] == "upi"
    assert payload["link_expires_at"] == 1_700_000_300
    assert payload["link_expiry_source"] == "upi_qr_code"


def test_upi_history_ignores_untagged_checkout_session_expiry(tmp_path: Path):
    source = tmp_path / "tasks.db"
    target = tmp_path / "account_manager.db"
    row = _source_row(
        "job-upi-checkout-expiry",
        "one@example.test",
        completed_at=1_700_000_000,
        url="https://payments.stripe.com/upi/instructions/upi-checkout-expiry",
    )
    row.update({"link_type": "hosted", "payment_method_type": "upi", "link_expires_at": 1_700_086_400})
    _create_source(source, [row])
    _create_target(target, [{"id": 1, "email": "one@example.test"}])

    report = synchronize_long_link_success_history(
        source_database=source,
        target_databases=[target],
        apply=True,
        backup_dir=tmp_path / "backups",
    )

    assert report["source"]["upi_checkout_expiry_ignored"] == 1
    with sqlite3.connect(target) as connection:
        extra_json = connection.execute("SELECT extra_json FROM accounts WHERE id = 1").fetchone()[0]
    payload = json.loads(extra_json)["chatgpt_last_payment_link"]
    assert payload["link_type"] == "upi"
    assert "link_expires_at" not in payload
    assert "link_expiry_source" not in payload


def test_orphan_remote_job_is_not_remapped_by_email(tmp_path: Path):
    source = tmp_path / "tasks.db"
    target = tmp_path / "account_manager.db"
    row = _source_row(
        "job-orphan",
        "one@example.test",
        completed_at=1_700_000_000,
        url="https://pay.example.test/orphan",
    )
    _create_source(source, [row])
    _create_target(target, [{"id": 1, "email": "one@example.test"}])
    with sqlite3.connect(target) as connection:
        connection.execute(
            """
            INSERT INTO payment_link_generations (
                account_id, task_id, request_id, remote_batch_id, remote_job_id,
                profile_hash, link_type, status, url, submitted_at, started_at,
                generated_at, persisted_at, sanitized_error, result_json, created_at, updated_at
            ) VALUES (999, 'old-task', 'old-request', '', 'job-orphan', '', 'pix',
                      'succeeded', 'https://pay.example.test/orphan', '', '', '', '', '', '{}', '', '')
            """
        )
        connection.commit()

    report = synchronize_long_link_success_history(
        source_database=source,
        target_databases=[target],
        apply=False,
    )

    assert report["mapping"]["skipped_orphan_remote_job"] == 1
    assert report["targets"][0]["stats"] == {}
