"""Idempotently import durable long-link success history into Auto-GPT accounts.

The long-link service intentionally keeps successful URLs and the decoded account
email, but not the access token used to generate them.  This module only imports
rows that can be matched to exactly one local ChatGPT account and preserves the
normal Auto-GPT payment-link cache/history contract.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Iterable
from urllib.parse import urlsplit

from services.chatgpt_core.payment_link_cache import PIX_EXPIRED_CLEANED_STATUS


SYNC_NAME = "long_link_history_sync"
SYNC_TASK_ID = "long_link_history_sync"
SOURCE_TABLE = "long_link_success_history"
_TARGET_ACCOUNT_COLUMNS = {"id", "platform", "email", "cashier_url", "extra_json", "updated_at"}
_TARGET_GENERATION_COLUMNS = {
    "id",
    "account_id",
    "task_id",
    "request_id",
    "remote_batch_id",
    "remote_job_id",
    "profile_hash",
    "link_type",
    "status",
    "url",
    "submitted_at",
    "started_at",
    "generated_at",
    "persisted_at",
    "sanitized_error",
    "result_json",
    "created_at",
    "updated_at",
}


class LongLinkHistorySyncError(RuntimeError):
    """Raised when source or destination storage cannot be reconciled safely."""


@dataclass(frozen=True)
class SourceLink:
    job_id: str
    completed_at: int
    account_email: str
    url: str
    link_type: str
    source: str
    billing_country: str
    currency: str
    payment_method_type: str
    cs_id: str
    link_expires_at: int | None

    @property
    def request_id(self) -> str:
        return f"long-link-history:{self.job_id}"

    @property
    def generated_at(self) -> str:
        return _iso_from_epoch(self.completed_at)


@dataclass
class AccountRow:
    database: Path
    account_id: int
    email: str
    cashier_url: str
    extra_json: str
    updated_at: str


@dataclass
class GenerationRow:
    database: Path
    generation_id: int
    account_id: int
    request_id: str
    remote_job_id: str
    status: str
    url: str
    link_type: str
    generated_at: str
    result_json: str


@dataclass
class TargetSnapshot:
    database: Path
    accounts: dict[int, AccountRow]
    email_index: dict[str, list[AccountRow]]
    generations_by_remote_job: dict[str, list[GenerationRow]]
    generations_by_request_id: dict[str, GenerationRow]


@dataclass
class PlannedRecord:
    source: SourceLink
    account: AccountRow
    existing_generation: GenerationRow | None = None
    generation_action: str = "insert"


@dataclass
class TargetPlan:
    target: TargetSnapshot
    records: list[PlannedRecord] = field(default_factory=list)
    latest_by_account: dict[int, SourceLink] = field(default_factory=dict)
    counters: Counter[str] = field(default_factory=Counter)


def _safe_text(value: Any, *, limit: int = 10_000) -> str:
    text = str(value or "").strip()
    return text[:limit]


def _normalized_email(value: Any) -> str:
    return _safe_text(value, limit=320).lower()


def _valid_http_url(value: Any) -> str:
    url = _safe_text(value)
    if not url or any(ord(char) < 32 or ord(char) == 127 for char in url):
        return ""
    try:
        parsed = urlsplit(url)
    except (TypeError, ValueError):
        return ""
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return ""
    return url


def _normalized_link_type(value: Any) -> str:
    text = _safe_text(value, limit=64).lower().replace("-", "_")
    if not text:
        return "hosted"
    if all(char.isascii() and (char.isalnum() or char == "_") for char in text):
        return text
    return "other"


def _normalized_payment_method_type(value: Any) -> str:
    text = _safe_text(value, limit=80).lower().replace("-", "_")
    if not text:
        return ""
    return text if all(char.isascii() and (char.isalnum() or char == "_") for char in text) else ""


def _iso_from_epoch(value: Any) -> str:
    try:
        timestamp = int(float(value))
    except (TypeError, ValueError):
        return ""
    if timestamp <= 0:
        return ""
    try:
        return datetime.fromtimestamp(timestamp, timezone.utc).replace(microsecond=0).isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


def _timestamp_for_compare(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = _safe_text(value, limit=128)
    if not text:
        return None
    try:
        numeric = float(text)
    except ValueError:
        numeric = None
    if numeric is not None:
        return numeric / 1000 if numeric > 100_000_000_000 else numeric
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _table_columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
    ).fetchone()
    if not exists:
        return set()
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table_name})")}


def _integrity_check(connection: sqlite3.Connection, *, label: str) -> None:
    result = [str(row[0] or "").strip().lower() for row in connection.execute("PRAGMA integrity_check")]
    if result != ["ok"]:
        raise LongLinkHistorySyncError(f"SQLite integrity_check failed for {label}: {result[:3]}")


def _connect_readonly(path: Path, *, busy_timeout_seconds: int) -> sqlite3.Connection:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise LongLinkHistorySyncError(f"SQLite database does not exist: {resolved}")
    connection = sqlite3.connect(
        f"file:{resolved}?mode=ro",
        uri=True,
        timeout=max(int(busy_timeout_seconds), 1),
    )
    connection.execute(f"PRAGMA busy_timeout={max(int(busy_timeout_seconds), 1) * 1000}")
    connection.execute("PRAGMA query_only=ON")
    return connection


def _connect_writable(path: Path, *, busy_timeout_seconds: int) -> sqlite3.Connection:
    connection = sqlite3.connect(
        str(path.expanduser().resolve()),
        timeout=max(int(busy_timeout_seconds), 1),
        isolation_level=None,
    )
    connection.execute(f"PRAGMA busy_timeout={max(int(busy_timeout_seconds), 1) * 1000}")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _parse_source_rows(connection: sqlite3.Connection) -> tuple[list[SourceLink], Counter[str]]:
    required = {
        "job_id",
        "completed_at",
        "account_email",
        "long_url",
        "link_type",
        "source",
        "billing_country",
        "currency",
        "payment_method_type",
        "cs_id",
        "link_expires_at",
    }
    columns = _table_columns(connection, SOURCE_TABLE)
    missing = required - columns
    if missing:
        raise LongLinkHistorySyncError(f"{SOURCE_TABLE} is missing columns: {sorted(missing)}")

    counters: Counter[str] = Counter()
    rows: list[SourceLink] = []
    source_rows = connection.execute(
        f"""
        SELECT job_id, completed_at, account_email, long_url, link_type, source,
               billing_country, currency, payment_method_type, cs_id, link_expires_at
        FROM {SOURCE_TABLE}
        ORDER BY completed_at ASC, job_id ASC
        """
    ).fetchall()
    counters["source_rows"] = len(source_rows)
    for raw in source_rows:
        (
            job_id,
            completed_at,
            account_email,
            long_url,
            link_type,
            source,
            billing_country,
            currency,
            payment_method_type,
            cs_id,
            link_expires_at,
        ) = raw
        normalized_job_id = _safe_text(job_id, limit=96)
        normalized_email = _normalized_email(account_email)
        normalized_url = _valid_http_url(long_url)
        generated_at = _iso_from_epoch(completed_at)
        if not normalized_job_id:
            counters["skipped_missing_job_id"] += 1
            continue
        if not normalized_email:
            counters["skipped_missing_email"] += 1
            continue
        if not normalized_url:
            counters["skipped_invalid_url"] += 1
            continue
        if not generated_at:
            counters["skipped_invalid_completed_at"] += 1
            continue
        try:
            expiry = int(float(link_expires_at)) if link_expires_at not in (None, "") else None
        except (TypeError, ValueError):
            expiry = None
        rows.append(
            SourceLink(
                job_id=normalized_job_id,
                completed_at=int(float(completed_at)),
                account_email=normalized_email,
                url=normalized_url,
                link_type=_normalized_link_type(link_type),
                source=_safe_text(source, limit=40) or "unknown",
                billing_country=_safe_text(billing_country, limit=16).upper(),
                currency=_safe_text(currency, limit=16).upper(),
                payment_method_type=_normalized_payment_method_type(payment_method_type),
                cs_id=_safe_text(cs_id, limit=255),
                link_expires_at=expiry if expiry and expiry > 0 else None,
            )
        )
    counters["valid_source_rows"] = len(rows)
    return rows, counters


def _load_target_snapshot(path: Path, *, platform: str, busy_timeout_seconds: int) -> TargetSnapshot:
    connection = _connect_readonly(path, busy_timeout_seconds=busy_timeout_seconds)
    try:
        _integrity_check(connection, label=str(path))
        account_columns = _table_columns(connection, "accounts")
        generation_columns = _table_columns(connection, "payment_link_generations")
        missing_accounts = _TARGET_ACCOUNT_COLUMNS - account_columns
        missing_generations = _TARGET_GENERATION_COLUMNS - generation_columns
        if missing_accounts:
            raise LongLinkHistorySyncError(f"accounts is missing columns in {path}: {sorted(missing_accounts)}")
        if missing_generations:
            raise LongLinkHistorySyncError(
                f"payment_link_generations is missing columns in {path}: {sorted(missing_generations)}"
            )

        accounts: dict[int, AccountRow] = {}
        email_index: dict[str, list[AccountRow]] = defaultdict(list)
        for row in connection.execute(
            """
            SELECT id, email, cashier_url, extra_json, updated_at
            FROM accounts
            WHERE platform = ?
            """,
            (platform,),
        ):
            account = AccountRow(
                database=path,
                account_id=int(row[0]),
                email=_normalized_email(row[1]),
                cashier_url=_safe_text(row[2]),
                extra_json=str(row[3] or "{}"),
                updated_at=_safe_text(row[4], limit=128),
            )
            accounts[account.account_id] = account
            if account.email:
                email_index[account.email].append(account)

        by_remote_job: dict[str, list[GenerationRow]] = defaultdict(list)
        by_request_id: dict[str, GenerationRow] = {}
        for row in connection.execute(
            """
            SELECT id, account_id, request_id, remote_job_id, status, url, link_type,
                   generated_at, result_json
            FROM payment_link_generations
            """
        ):
            generation = GenerationRow(
                database=path,
                generation_id=int(row[0]),
                account_id=int(row[1]),
                request_id=_safe_text(row[2], limit=128),
                remote_job_id=_safe_text(row[3], limit=128),
                status=_safe_text(row[4], limit=48).lower(),
                url=_valid_http_url(row[5]),
                link_type=_normalized_link_type(row[6]),
                generated_at=_safe_text(row[7], limit=128),
                result_json=str(row[8] or "{}"),
            )
            if generation.remote_job_id:
                by_remote_job[generation.remote_job_id].append(generation)
            if generation.request_id:
                by_request_id[generation.request_id] = generation
    finally:
        connection.close()
    return TargetSnapshot(
        database=path,
        accounts=accounts,
        email_index=dict(email_index),
        generations_by_remote_job=dict(by_remote_job),
        generations_by_request_id=by_request_id,
    )


def _record_payload(source: SourceLink) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "url": source.url,
        "long_url": source.url,
        "plan": "plus",
        "country": source.billing_country,
        "currency": source.currency,
        "billing_country": source.billing_country,
        "payment_link_format": "long_link",
        "payment_source": "long_link",
        "link_type": source.link_type,
        "source": SYNC_NAME,
        "history_source": source.source,
        "upstream": "openai_pay_long_link",
        "remote_job_id": source.job_id,
        "remote_request_id": source.request_id,
        "generated_at": source.generated_at,
        "created_at": source.generated_at,
    }
    if source.payment_method_type:
        payload["payment_method_type"] = source.payment_method_type
    if source.cs_id:
        payload["cs_id"] = source.cs_id
    if source.link_expires_at is not None and source.link_type == "pix":
        payload["link_expires_at"] = source.link_expires_at
    if source.link_type == "paypal" or "paypal" in source.payment_method_type:
        payload["paypal_url"] = source.url
    return payload


def _load_extra(account: AccountRow) -> dict[str, Any] | None:
    try:
        value = json.loads(account.extra_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _current_cache_should_be_replaced(extra: dict[str, Any], source: SourceLink) -> bool:
    current = extra.get("chatgpt_last_payment_link")
    if not isinstance(current, dict):
        return True
    current_time = _timestamp_for_compare(current.get("generated_at") or current.get("created_at"))
    if _safe_text(current.get("link_status"), limit=64).lower() == PIX_EXPIRED_CLEANED_STATUS:
        # Cleanup deliberately leaves a URL-free tombstone. Historical rows at
        # or before that generation must not resurrect the expired current link;
        # a genuinely newer source record may replace it normally.
        tombstone_time = (
            _timestamp_for_compare(current.get("pix_cleanup_through_at"))
            or current_time
            or _timestamp_for_compare(current.get("cleaned_at"))
        )
        return tombstone_time is not None and float(source.completed_at) > tombstone_time
    if not _valid_http_url(current.get("url")):
        return True
    if current_time is None:
        return False
    return float(source.completed_at) > current_time


def _current_cache_action(account: AccountRow, source: SourceLink) -> str:
    extra = _load_extra(account)
    if extra is None:
        return "invalid_extra"
    return "updated" if _current_cache_should_be_replaced(extra, source) else "retained"


def _generation_is_current(existing: GenerationRow, source: SourceLink) -> bool:
    return (
        existing.status == "succeeded"
        and existing.remote_job_id == source.job_id
        and existing.url == source.url
        and existing.link_type == source.link_type
    )


def _unique_accounts(rows: Iterable[GenerationRow], targets: dict[Path, TargetSnapshot]) -> list[AccountRow]:
    unique: dict[tuple[Path, int], AccountRow] = {}
    for row in rows:
        target = targets.get(row.database)
        account = target.accounts.get(row.account_id) if target else None
        if account:
            unique[(account.database, account.account_id)] = account
    return list(unique.values())


def _build_plans(source_rows: list[SourceLink], targets: list[TargetSnapshot]) -> tuple[dict[Path, TargetPlan], Counter[str]]:
    plans = {target.database: TargetPlan(target=target) for target in targets}
    targets_by_path = {target.database: target for target in targets}
    global_email_index: dict[str, list[AccountRow]] = defaultdict(list)
    global_remote_job_index: dict[str, list[GenerationRow]] = defaultdict(list)
    for target in targets:
        for email, accounts in target.email_index.items():
            global_email_index[email].extend(accounts)
        for job_id, generations in target.generations_by_remote_job.items():
            global_remote_job_index[job_id].extend(generations)

    counters: Counter[str] = Counter()
    for source in source_rows:
        existing_generations = global_remote_job_index.get(source.job_id, [])
        remote_accounts = _unique_accounts(existing_generations, targets_by_path)
        if existing_generations and not remote_accounts:
            counters["skipped_orphan_remote_job"] += 1
            continue
        if len(remote_accounts) > 1:
            counters["skipped_ambiguous_remote_job"] += 1
            continue
        account: AccountRow | None = remote_accounts[0] if remote_accounts else None
        if account is not None and account.email != source.account_email:
            counters["skipped_remote_job_email_mismatch"] += 1
            continue
        if account is None:
            candidates = global_email_index.get(source.account_email, [])
            if not candidates:
                counters["skipped_unmatched_account"] += 1
                continue
            if len(candidates) != 1:
                counters["skipped_ambiguous_email"] += 1
                continue
            account = candidates[0]

        plan = plans[account.database]
        target = plan.target
        existing: GenerationRow | None = None
        if existing_generations:
            existing = existing_generations[0]
        else:
            by_request = target.generations_by_request_id.get(source.request_id)
            if by_request is not None:
                if by_request.account_id != account.account_id:
                    counters["skipped_request_id_conflict"] += 1
                    continue
                existing = by_request

        action = "insert"
        if existing is not None:
            if existing.account_id != account.account_id:
                counters["skipped_generation_account_conflict"] += 1
                continue
            action = "existing" if _generation_is_current(existing, source) else "update"
        record = PlannedRecord(source=source, account=account, existing_generation=existing, generation_action=action)
        plan.records.append(record)
        plan.counters["matched_records"] += 1
        plan.counters[f"generation_{action}"] += 1
        latest = plan.latest_by_account.get(account.account_id)
        if latest is None or (source.completed_at, source.job_id) > (latest.completed_at, latest.job_id):
            plan.latest_by_account[account.account_id] = source

    for plan in plans.values():
        for account_id, source in plan.latest_by_account.items():
            action = _current_cache_action(plan.target.accounts[account_id], source)
            plan.counters[f"current_link_planned_{action}"] += 1
    return plans, counters


def _make_backup(database: Path, backup_dir: Path, *, busy_timeout_seconds: int) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = backup_dir / f"{database.stem}.before-{SYNC_NAME}.{timestamp}.{os.getpid()}.backup"
    source = _connect_readonly(database, busy_timeout_seconds=busy_timeout_seconds)
    destination = sqlite3.connect(str(backup), timeout=max(int(busy_timeout_seconds), 1))
    try:
        source.backup(destination, pages=2048, sleep=0.05)
        destination.commit()
        _integrity_check(destination, label=str(backup))
    except Exception:
        destination.close()
        source.close()
        backup.unlink(missing_ok=True)
        raise
    destination.close()
    source.close()
    backup.chmod(0o600)
    return backup


def _upsert_generation(
    connection: sqlite3.Connection,
    *,
    record: PlannedRecord,
    now_iso: str,
) -> str:
    source = record.source
    payload = _record_payload(source)
    result_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    existing = record.existing_generation
    if existing is not None and record.generation_action == "existing":
        return "existing"
    if existing is not None:
        connection.execute(
            """
            UPDATE payment_link_generations
            SET status = 'succeeded', link_type = ?, url = ?, generated_at = ?,
                persisted_at = ?, sanitized_error = '', result_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                source.link_type,
                source.url,
                source.generated_at,
                now_iso,
                result_json,
                now_iso,
                existing.generation_id,
            ),
        )
        return "updated"
    connection.execute(
        """
        INSERT INTO payment_link_generations (
            account_id, task_id, request_id, remote_batch_id, remote_job_id,
            profile_hash, link_type, status, url, submitted_at, started_at,
            generated_at, persisted_at, sanitized_error, result_json, created_at, updated_at
        ) VALUES (?, ?, ?, '', ?, '', ?, 'succeeded', ?, '', '', ?, ?, '', ?, ?, ?)
        """,
        (
            record.account.account_id,
            SYNC_TASK_ID,
            source.request_id,
            source.job_id,
            source.link_type,
            source.url,
            source.generated_at,
            now_iso,
            result_json,
            now_iso,
            now_iso,
        ),
    )
    return "inserted"


def _update_current_cache(
    connection: sqlite3.Connection,
    *,
    account: AccountRow,
    source: SourceLink,
    now_iso: str,
) -> str:
    extra = _load_extra(account)
    action = _current_cache_action(account, source)
    if action != "updated":
        return action
    if extra is None:
        raise LongLinkHistorySyncError(f"invalid account extra unexpectedly accepted for {account.account_id}")
    payload = _record_payload(source)
    extra["chatgpt_last_payment_link"] = payload
    if source.link_type == "paypal" or "paypal" in source.payment_method_type:
        paypal_payload = dict(payload)
        paypal_payload["paypal_url"] = source.url
        extra["chatgpt_paypal_url"] = paypal_payload
    connection.execute(
        """
        UPDATE accounts
        SET cashier_url = ?, extra_json = ?, updated_at = ?
        WHERE id = ? AND platform = 'chatgpt'
        """,
        (
            source.url,
            json.dumps(extra, ensure_ascii=False, separators=(",", ":")),
            now_iso,
            account.account_id,
        ),
    )
    return "updated"


def _apply_target_plan(
    plan: TargetPlan,
    *,
    backup_dir: Path,
    busy_timeout_seconds: int,
) -> dict[str, Any]:
    target = plan.target
    if not plan.records:
        counters = Counter(plan.counters)
        counters["skipped_empty_target"] += 1
        return {
            "database": str(target.database),
            "backup": "",
            "stats": dict(sorted(counters.items())),
        }
    backup_path = _make_backup(target.database, backup_dir, busy_timeout_seconds=busy_timeout_seconds)
    connection = _connect_writable(target.database, busy_timeout_seconds=busy_timeout_seconds)
    counters = Counter(plan.counters)
    try:
        _integrity_check(connection, label=str(target.database))
        connection.execute("BEGIN IMMEDIATE")
        now_iso = _now_iso()
        for record in plan.records:
            result = _upsert_generation(connection, record=record, now_iso=now_iso)
            counters[f"generation_applied_{result}"] += 1
        for account_id, source in plan.latest_by_account.items():
            account = target.accounts[account_id]
            result = _update_current_cache(connection, account=account, source=source, now_iso=now_iso)
            counters[f"current_link_{result}"] += 1
        connection.commit()
        _integrity_check(connection, label=str(target.database))
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "database": str(target.database),
        "backup": str(backup_path),
        "stats": dict(sorted(counters.items())),
    }


def synchronize_long_link_success_history(
    *,
    source_database: Path,
    target_databases: Iterable[Path],
    apply: bool = False,
    backup_dir: Path | None = None,
    platform: str = "chatgpt",
    busy_timeout_seconds: int = 30,
) -> dict[str, Any]:
    """Build or apply a safe, cross-database long-link history reconciliation.

    ``apply=False`` is side-effect free.  Apply mode creates a verified ``.backup``
    for every target before its transaction and never modifies the long-link source.
    """

    source_path = source_database.expanduser().resolve()
    target_paths = [Path(path).expanduser().resolve() for path in target_databases]
    if not target_paths:
        raise LongLinkHistorySyncError("at least one target database is required")
    if len(set(target_paths)) != len(target_paths):
        raise LongLinkHistorySyncError("target databases must be unique")
    if source_path in target_paths:
        raise LongLinkHistorySyncError("source database cannot also be a target database")

    source_connection = _connect_readonly(source_path, busy_timeout_seconds=busy_timeout_seconds)
    try:
        _integrity_check(source_connection, label=str(source_path))
        source_rows, source_counters = _parse_source_rows(source_connection)
    finally:
        source_connection.close()

    targets = [
        _load_target_snapshot(path, platform=platform, busy_timeout_seconds=busy_timeout_seconds)
        for path in target_paths
    ]
    plans, mapping_counters = _build_plans(source_rows, targets)
    report: dict[str, Any] = {
        "sync": SYNC_NAME,
        "mode": "apply" if apply else "dry-run",
        "source_database": str(source_path),
        "platform": platform,
        "source": dict(sorted(source_counters.items())),
        "mapping": dict(sorted(mapping_counters.items())),
        "targets": [],
    }

    for target in targets:
        plan = plans[target.database]
        target_report: dict[str, Any] = {
            "database": str(target.database),
            "chatgpt_accounts": len(target.accounts),
            "stats": dict(sorted(plan.counters.items())),
        }
        if apply:
            default_backup_dir = target.database.parent / "migration-backups"
            applied = _apply_target_plan(
                plan,
                backup_dir=(backup_dir.expanduser().resolve() if backup_dir else default_backup_dir),
                busy_timeout_seconds=busy_timeout_seconds,
            )
            target_report.update(applied)
        report["targets"].append(target_report)
    return report
