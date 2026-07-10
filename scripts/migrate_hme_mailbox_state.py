#!/usr/bin/env python3
"""Migrate oversized per-account mailbox recovery state without loading all rows.

Dry-run is the default. Apply mode refuses an open live database by default, runs a
full source integrity check, creates and verifies an SQLite online backup, updates all
targets in one transaction, checkpoints WAL, optionally VACUUMs, and runs a final full
integrity check.

The mailbox whitelist lives exclusively in
``services.chatgpt_core.mailbox_state.sanitize_mailbox_state``. This script intentionally
does not carry a second migration-only whitelist that could drift from runtime writes.
Account rows converge on the single canonical ``chatgpt_mailbox_state`` field: a missing
canonical value is recovered from the first sanitizable legacy/recheck fallback, then
sanitizable noncanonical copies are removed while malformed/providerless values are
preserved for manual recovery.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.chatgpt_core.mailbox_state import sanitize_mailbox_state


MIGRATION_NAME = "20260711_mailbox_state_compaction_v1"
ACCOUNT_STATE_PATHS = (
    ("chatgpt_mailbox_state",),
    ("mailbox_state",),
    ("chatgpt_invalid_recheck", "mailbox_state"),
    ("chatgpt_custom_email_recheck", "mailbox_state"),
)
CANONICAL_STATE_PATH = ACCOUNT_STATE_PATHS[0]
FALLBACK_STATE_PATHS = ACCOUNT_STATE_PATHS[1:]
_MISSING = object()
DEFAULT_MAX_BEFORE_IDS = 128
DEFAULT_MAX_BEFORE_ID_BYTES = 16 * 1024
DEFAULT_BATCH_SIZE = 1
REPORT_SAMPLE_LIMIT = 50
MIN_FREE_SPACE_MARGIN_BYTES = 256 * 1024 * 1024


class MigrationError(RuntimeError):
    """A migration safety precondition or verification failed."""


@dataclass(frozen=True)
class CompactionLimits:
    max_before_ids: int = DEFAULT_MAX_BEFORE_IDS
    max_before_id_bytes: int = DEFAULT_MAX_BEFORE_ID_BYTES

    def validate(self) -> None:
        if self.max_before_ids < 0:
            raise MigrationError("max_before_ids must be >= 0")
        if self.max_before_id_bytes < 2:
            raise MigrationError("max_before_id_bytes must be >= 2")


@dataclass
class StateCompaction:
    path: str
    destination_path: str
    before_bytes: int
    after_bytes: int
    before_ids_seen: int
    before_ids_kept: int
    changed: bool
    promoted: bool = False
    deleted: bool = False
    provider: str = ""
    removed_config_keys: list[str] = field(default_factory=list)
    removed_state_keys: list[str] = field(default_factory=list)
    removed_account_keys: list[str] = field(default_factory=list)
    removed_account_extra_keys: list[str] = field(default_factory=list)


@dataclass
class CandidateAssessment:
    path: tuple[str, ...]
    original: dict[str, Any]
    cleaned: dict[str, Any]

    @property
    def label(self) -> str:
        return ".".join(self.path)


@dataclass
class AccountCompaction:
    reports: list[StateCompaction] = field(default_factory=list)
    state_objects_seen: int = 0
    sanitizable_state_objects: int = 0
    invalid_shapes: int = 0
    unsanitizable: int = 0
    canonical_promoted: bool = False
    canonical_source_path: str = ""
    removed_noncanonical_paths: list[str] = field(default_factory=list)
    removed_noncanonical_bytes: int = 0
    removed_duplicate_bytes: int = 0
    preserved_unsanitizable_paths: list[str] = field(default_factory=list)


@dataclass
class MigrationStats:
    scanned_account_rows: int = 0
    parsed_account_rows: int = 0
    invalid_account_json_rows: int = 0
    non_object_account_json_rows: int = 0
    account_rows_with_state: int = 0
    target_account_rows: int = 0
    changed_account_rows: int = 0
    unchanged_target_account_rows: int = 0
    canonical_promoted_account_rows: int = 0
    removed_noncanonical_state_objects: int = 0
    removed_noncanonical_state_bytes: int = 0
    removed_duplicate_state_bytes: int = 0
    pending_table_present: bool = False
    scanned_pending_rows: int = 0
    invalid_pending_json_rows: int = 0
    non_object_pending_json_rows: int = 0
    target_pending_rows: int = 0
    changed_pending_rows: int = 0
    unchanged_target_pending_rows: int = 0
    state_objects_seen: int = 0
    state_objects_sanitized: int = 0
    changed_state_objects: int = 0
    unsanitizable_state_objects: int = 0
    invalid_state_shape_objects: int = 0
    before_json_bytes: int = 0
    projected_after_json_bytes: int = 0
    bytes_removed: int = 0
    bytes_added: int = 0
    before_mailbox_state_bytes: int = 0
    after_mailbox_state_bytes: int = 0
    before_ids_seen: int = 0
    before_ids_kept: int = 0
    before_ids_dropped: int = 0
    max_batch_rows: int = 0
    state_path_counts: Counter[str] = field(default_factory=Counter)
    canonical_source_paths: Counter[str] = field(default_factory=Counter)
    removed_noncanonical_paths: Counter[str] = field(default_factory=Counter)
    preserved_unsanitizable_paths: Counter[str] = field(default_factory=Counter)
    provider_counts: Counter[str] = field(default_factory=Counter)
    removed_config_keys: Counter[str] = field(default_factory=Counter)
    removed_state_keys: Counter[str] = field(default_factory=Counter)
    removed_account_keys: Counter[str] = field(default_factory=Counter)
    removed_account_extra_keys: Counter[str] = field(default_factory=Counter)
    changed_account_ids_sample: list[int] = field(default_factory=list)
    changed_pending_ids_sample: list[int] = field(default_factory=list)
    skipped_account_ids_sample: list[int] = field(default_factory=list)
    skipped_pending_ids_sample: list[int] = field(default_factory=list)

    def record_state(self, report: StateCompaction) -> None:
        self.changed_state_objects += int(report.changed)
        self.before_mailbox_state_bytes += report.before_bytes
        self.after_mailbox_state_bytes += report.after_bytes
        self.before_ids_seen += report.before_ids_seen
        self.before_ids_kept += report.before_ids_kept
        self.before_ids_dropped += max(report.before_ids_seen - report.before_ids_kept, 0)
        path_label = report.path
        if report.destination_path and report.destination_path != report.path:
            path_label = f"{report.path} -> {report.destination_path}"
        self.state_path_counts[path_label] += 1
        if report.provider:
            self.provider_counts[report.provider] += 1
        self.removed_config_keys.update(report.removed_config_keys)
        self.removed_state_keys.update(report.removed_state_keys)
        self.removed_account_keys.update(report.removed_account_keys)
        self.removed_account_extra_keys.update(report.removed_account_extra_keys)

    def record_row_sizes(self, before_bytes: int, after_bytes: int) -> None:
        self.before_json_bytes += before_bytes
        self.projected_after_json_bytes += after_bytes
        if after_bytes <= before_bytes:
            self.bytes_removed += before_bytes - after_bytes
        else:
            self.bytes_added += after_bytes - before_bytes

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for key in (
            "state_path_counts",
            "canonical_source_paths",
            "removed_noncanonical_paths",
            "preserved_unsanitizable_paths",
            "provider_counts",
            "removed_config_keys",
            "removed_state_keys",
            "removed_account_keys",
            "removed_account_extra_keys",
        ):
            payload[key] = dict(sorted(getattr(self, key).items()))
        net_removed = self.before_json_bytes - self.projected_after_json_bytes
        payload["net_bytes_removed"] = net_removed
        payload["reduction_percent"] = (
            round((net_removed / self.before_json_bytes) * 100, 4)
            if self.before_json_bytes
            else 0.0
        )
        return payload


def _json_bytes(value: Any) -> int:
    return len(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=False).encode("utf-8")
    )


def _raw_bytes(value: str) -> int:
    return len(value.encode("utf-8"))


def _string_list_count(value: Any) -> int:
    if not isinstance(value, (list, tuple, set)):
        return 0
    return len({str(item or "").strip() for item in value if str(item or "").strip()})


def _keys(value: Any) -> set[str]:
    return {str(key) for key in value} if isinstance(value, dict) else set()


def _describe_change(
    path: str,
    original: dict[str, Any],
    cleaned: dict[str, Any],
    *,
    destination_path: str | None = None,
    promoted: bool = False,
) -> StateCompaction:
    original_account = original.get("account") if isinstance(original.get("account"), dict) else {}
    cleaned_account = cleaned.get("account") if isinstance(cleaned.get("account"), dict) else {}
    original_account_extra = (
        original_account.get("extra") if isinstance(original_account.get("extra"), dict) else {}
    )
    cleaned_account_extra = (
        cleaned_account.get("extra") if isinstance(cleaned_account.get("extra"), dict) else {}
    )
    original_config = original.get("config") if isinstance(original.get("config"), dict) else {}
    cleaned_config = cleaned.get("config") if isinstance(cleaned.get("config"), dict) else {}
    provider = str(cleaned.get("provider") or original.get("provider") or "").strip().lower()
    return StateCompaction(
        path=path,
        destination_path=destination_path or path,
        before_bytes=_json_bytes(original),
        after_bytes=_json_bytes(cleaned),
        before_ids_seen=_string_list_count(original.get("before_ids")),
        before_ids_kept=_string_list_count(cleaned.get("before_ids")),
        changed=cleaned != original or promoted,
        promoted=promoted,
        provider=provider,
        removed_config_keys=sorted(_keys(original_config) - _keys(cleaned_config)),
        removed_state_keys=sorted(_keys(original) - _keys(cleaned)),
        removed_account_keys=sorted(_keys(original_account) - _keys(cleaned_account)),
        removed_account_extra_keys=sorted(
            _keys(original_account_extra) - _keys(cleaned_account_extra)
        ),
    )


def _describe_deletion(path: str, original: dict[str, Any]) -> StateCompaction:
    original_account = original.get("account") if isinstance(original.get("account"), dict) else {}
    original_account_extra = (
        original_account.get("extra") if isinstance(original_account.get("extra"), dict) else {}
    )
    original_config = original.get("config") if isinstance(original.get("config"), dict) else {}
    return StateCompaction(
        path=path,
        destination_path="",
        before_bytes=_json_bytes(original),
        after_bytes=0,
        before_ids_seen=_string_list_count(original.get("before_ids")),
        before_ids_kept=0,
        changed=True,
        deleted=True,
        provider=str(original.get("provider") or "").strip().lower(),
        removed_config_keys=sorted(_keys(original_config)),
        removed_state_keys=sorted(_keys(original)),
        removed_account_keys=sorted(_keys(original_account)),
        removed_account_extra_keys=sorted(_keys(original_account_extra)),
    )


def _state_at_path(
    root: dict[str, Any], path: tuple[str, ...], *, default: Any = _MISSING
) -> Any:
    current: Any = root
    for part in path:
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


def _replace_state_at_path(root: dict[str, Any], path: tuple[str, ...], value: dict[str, Any]) -> None:
    current = root
    for part in path[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            raise MigrationError(f"cannot replace mailbox state at {'.'.join(path)}")
        copied = dict(child)
        current[part] = copied
        current = copied
    current[path[-1]] = value


def _delete_state_at_path(root: dict[str, Any], path: tuple[str, ...]) -> None:
    current = root
    for part in path[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            raise MigrationError(f"cannot delete mailbox state at {'.'.join(path)}")
        copied = dict(child)
        current[part] = copied
        current = copied
    current.pop(path[-1], None)


def sanitize_account_extra(
    extra: dict[str, Any],
    *,
    account_email: str,
    limits: CompactionLimits,
) -> tuple[dict[str, Any], AccountCompaction]:
    """Converge historical mailbox-state copies to one canonical account state.

    A present but malformed/providerless value is never overwritten or deleted.  When
    canonical state is absent, the first sanitizable fallback is promoted in the
    declared priority order.  Once a valid canonical state exists, only sanitizable
    noncanonical copies are removed; malformed forensic data remains untouched.
    """

    result = dict(extra)
    meta = AccountCompaction()
    assessments: dict[tuple[str, ...], CandidateAssessment] = {}
    for path in ACCOUNT_STATE_PATHS:
        original = _state_at_path(extra, path)
        if original is _MISSING:
            continue
        meta.state_objects_seen += 1
        path_label = ".".join(path)
        if not isinstance(original, dict):
            meta.invalid_shapes += 1
            meta.preserved_unsanitizable_paths.append(path_label)
            continue
        if not original:
            meta.unsanitizable += 1
            meta.preserved_unsanitizable_paths.append(path_label)
            continue
        cleaned = sanitize_mailbox_state(
            original,
            account_email=account_email,
            max_before_ids=limits.max_before_ids,
            max_before_ids_bytes=limits.max_before_id_bytes,
        )
        # Never turn a non-empty historical state into {}. Unsupported/corrupt states
        # remain available for manual recovery and are surfaced in the report.
        if not cleaned:
            meta.unsanitizable += 1
            meta.preserved_unsanitizable_paths.append(path_label)
            continue
        assessments[path] = CandidateAssessment(path=path, original=original, cleaned=cleaned)
        meta.sanitizable_state_objects += 1

    canonical_present = _state_at_path(extra, CANONICAL_STATE_PATH) is not _MISSING
    selected: CandidateAssessment | None = None
    if canonical_present:
        selected = assessments.get(CANONICAL_STATE_PATH)
    else:
        selected = next(
            (assessments[path] for path in FALLBACK_STATE_PATHS if path in assessments),
            None,
        )
    if selected is None:
        return result, meta

    canonical_label = ".".join(CANONICAL_STATE_PATH)
    promoted = selected.path != CANONICAL_STATE_PATH
    _replace_state_at_path(result, CANONICAL_STATE_PATH, selected.cleaned)
    meta.canonical_promoted = promoted
    meta.canonical_source_path = selected.label
    meta.reports.append(
        _describe_change(
            selected.label,
            selected.original,
            selected.cleaned,
            destination_path=canonical_label,
            promoted=promoted,
        )
    )

    for path in FALLBACK_STATE_PATHS:
        assessment = assessments.get(path)
        if assessment is None:
            continue
        _delete_state_at_path(result, path)
        meta.removed_noncanonical_paths.append(assessment.label)
        state_bytes = _json_bytes(assessment.original)
        meta.removed_noncanonical_bytes += state_bytes
        if assessment.path != selected.path:
            meta.removed_duplicate_bytes += state_bytes
            meta.reports.append(_describe_deletion(assessment.label, assessment.original))

    return result, meta


def sanitize_pending_state(
    state: dict[str, Any],
    *,
    account_email: str,
    limits: CompactionLimits,
) -> tuple[dict[str, Any] | None, StateCompaction | None]:
    if not state:
        return state, None
    cleaned = sanitize_mailbox_state(
        state,
        account_email=account_email,
        max_before_ids=limits.max_before_ids,
        max_before_ids_bytes=limits.max_before_id_bytes,
    )
    if not cleaned:
        return None, None
    path = "pending_business_invites.mailbox_state_json"
    return cleaned, _describe_change(path, state, cleaned, destination_path=path)


def _connect_readonly(db_path: Path, *, busy_timeout_seconds: int) -> sqlite3.Connection:
    uri = f"file:{db_path.resolve()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=max(busy_timeout_seconds, 1))
    connection.execute(f"PRAGMA busy_timeout={max(busy_timeout_seconds, 1) * 1000}")
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA temp_store=FILE")
    connection.execute("PRAGMA cache_size=-8192")
    return connection


def _connect_writable(db_path: Path, *, busy_timeout_seconds: int) -> sqlite3.Connection:
    connection = sqlite3.connect(str(db_path), timeout=max(busy_timeout_seconds, 1), isolation_level=None)
    connection.execute(f"PRAGMA busy_timeout={max(busy_timeout_seconds, 1) * 1000}")
    connection.execute("PRAGMA temp_store=FILE")
    connection.execute("PRAGMA cache_size=-8192")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    return bool(
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
        ).fetchone()
    )


def validate_schema(connection: sqlite3.Connection) -> bool:
    if not _table_exists(connection, "accounts"):
        raise MigrationError("accounts table is missing")
    account_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(accounts)")}
    missing = {"id", "platform", "email", "extra_json"} - account_columns
    if missing:
        raise MigrationError(f"accounts table is missing required columns: {sorted(missing)}")

    pending_exists = _table_exists(connection, "pending_business_invites")
    if pending_exists:
        pending_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(pending_business_invites)")
        }
        pending_missing = {"id", "email", "mailbox_state_json"} - pending_columns
        if pending_missing:
            raise MigrationError(
                "pending_business_invites is missing required columns: "
                f"{sorted(pending_missing)}"
            )
    return pending_exists


def integrity_check(connection: sqlite3.Connection) -> None:
    rows = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
    if rows != ["ok"]:
        raise MigrationError(f"SQLite integrity_check failed: {rows[:10]}")


def _fsync_path(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())
    directory_fd = os.open(str(path.parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def create_verified_backup(
    db_path: Path,
    backup_dir: Path,
    *,
    busy_timeout_seconds: int,
) -> Path:
    """Create a transactionally consistent backup that includes committed WAL pages."""

    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = backup_dir / f"{db_path.stem}.{MIGRATION_NAME}.{timestamp}.{os.getpid()}.db"
    if backup_path.exists():
        raise MigrationError(f"backup destination already exists: {backup_path}")

    source = _connect_readonly(db_path, busy_timeout_seconds=busy_timeout_seconds)
    destination = sqlite3.connect(str(backup_path), timeout=max(busy_timeout_seconds, 1))
    try:
        validate_schema(source)
        source.backup(destination, pages=2048, sleep=0.05)
        destination.commit()
        integrity_check(destination)
    except Exception:
        destination.close()
        source.close()
        backup_path.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm", "-journal"):
            Path(f"{backup_path}{suffix}").unlink(missing_ok=True)
        raise
    else:
        destination.close()
        source.close()

    os.chmod(backup_path, 0o600)
    _fsync_path(backup_path)
    return backup_path


def database_file_sizes(db_path: Path) -> dict[str, int]:
    sizes: dict[str, int] = {}
    for label, path in (
        ("database", db_path),
        ("wal", Path(f"{db_path}-wal")),
        ("shm", Path(f"{db_path}-shm")),
    ):
        sizes[label] = path.stat().st_size if path.exists() else 0
    sizes["total"] = sum(sizes.values())
    return sizes


def _free_bytes(path: Path) -> int:
    probe = path if path.exists() else path.parent
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free


def ensure_apply_disk_headroom(db_path: Path, backup_dir: Path, *, vacuum: bool) -> None:
    main_size = max(db_path.stat().st_size, 1)
    # Backup + worst-case migration WAL; VACUUM may need another database-sized file.
    db_need = main_size + (main_size if vacuum else 0) + MIN_FREE_SPACE_MARGIN_BYTES
    backup_need = main_size + MIN_FREE_SPACE_MARGIN_BYTES

    backup_probe = backup_dir if backup_dir.exists() else backup_dir.parent
    while not backup_probe.exists() and backup_probe != backup_probe.parent:
        backup_probe = backup_probe.parent
    same_device = backup_probe.stat().st_dev == db_path.stat().st_dev
    if same_device:
        required = db_need + backup_need
        free = _free_bytes(db_path.parent)
        if free < required:
            raise MigrationError(
                f"insufficient free space on database filesystem: free={free} required={required}"
            )
        return

    db_free = _free_bytes(db_path.parent)
    backup_free = _free_bytes(backup_probe)
    if db_free < db_need:
        raise MigrationError(
            f"insufficient free space for migration/VACUUM: free={db_free} required={db_need}"
        )
    if backup_free < backup_need:
        raise MigrationError(
            f"insufficient free space for backup: free={backup_free} required={backup_need}"
        )


def find_open_database_processes(db_path: Path) -> list[dict[str, Any]]:
    """Find other Linux processes holding the DB, WAL, or SHM inode open."""

    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return []
    targets: set[tuple[int, int]] = set()
    for path in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        try:
            stat_result = path.stat()
        except OSError:
            continue
        targets.add((stat_result.st_dev, stat_result.st_ino))

    current_pid = os.getpid()
    matches: dict[int, dict[str, Any]] = {}
    for proc_dir in proc_root.iterdir():
        if not proc_dir.name.isdigit():
            continue
        pid = int(proc_dir.name)
        if pid == current_pid:
            continue
        try:
            descriptors = list((proc_dir / "fd").iterdir())
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        held: list[int] = []
        for descriptor in descriptors:
            try:
                stat_result = descriptor.stat()
            except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
                continue
            if (stat_result.st_dev, stat_result.st_ino) in targets:
                try:
                    held.append(int(descriptor.name))
                except ValueError:
                    pass
        if not held:
            continue
        try:
            command = (proc_dir / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                "utf-8", "replace"
            ).strip()
        except OSError:
            command = ""
        matches[pid] = {"pid": pid, "fds": sorted(held), "command": command[:240]}
    return [matches[pid] for pid in sorted(matches)]


def _iter_account_batches(
    connection: sqlite3.Connection,
    *,
    platform: str,
    batch_size: int,
) -> Iterable[list[tuple[int, str, str]]]:
    last_id = -1
    while True:
        rows = connection.execute(
            "SELECT id, email, extra_json "
            "FROM accounts WHERE platform = ? AND id > ? ORDER BY id LIMIT ?",
            (platform, last_id, batch_size),
        ).fetchall()
        if not rows:
            return
        normalized = [(int(row[0]), str(row[1] or ""), str(row[2] or "")) for row in rows]
        yield normalized
        last_id = normalized[-1][0]


def _iter_pending_batches(
    connection: sqlite3.Connection,
    *,
    batch_size: int,
) -> Iterable[list[tuple[int, str, str]]]:
    last_id = -1
    while True:
        rows = connection.execute(
            "SELECT id, email, mailbox_state_json "
            "FROM pending_business_invites WHERE id > ? ORDER BY id LIMIT ?",
            (last_id, batch_size),
        ).fetchall()
        if not rows:
            return
        normalized = [(int(row[0]), str(row[1] or ""), str(row[2] or "")) for row in rows]
        yield normalized
        last_id = normalized[-1][0]


def process_accounts(
    connection: sqlite3.Connection,
    *,
    platform: str,
    limits: CompactionLimits,
    batch_size: int,
    apply: bool,
    stats: MigrationStats,
) -> None:
    for rows in _iter_account_batches(connection, platform=platform, batch_size=batch_size):
        stats.max_batch_rows = max(stats.max_batch_rows, len(rows))
        for account_id, email, raw_extra in rows:
            stats.scanned_account_rows += 1
            original_bytes = _raw_bytes(raw_extra)
            try:
                extra = json.loads(raw_extra or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                stats.invalid_account_json_rows += 1
                stats.record_row_sizes(original_bytes, original_bytes)
                if len(stats.skipped_account_ids_sample) < REPORT_SAMPLE_LIMIT:
                    stats.skipped_account_ids_sample.append(account_id)
                continue
            if not isinstance(extra, dict):
                stats.non_object_account_json_rows += 1
                stats.record_row_sizes(original_bytes, original_bytes)
                if len(stats.skipped_account_ids_sample) < REPORT_SAMPLE_LIMIT:
                    stats.skipped_account_ids_sample.append(account_id)
                continue

            stats.parsed_account_rows += 1
            if any(_state_at_path(extra, path) is not _MISSING for path in ACCOUNT_STATE_PATHS):
                stats.account_rows_with_state += 1
            compacted, meta = sanitize_account_extra(
                extra,
                account_email=email,
                limits=limits,
            )
            stats.state_objects_seen += meta.state_objects_seen
            stats.state_objects_sanitized += meta.sanitizable_state_objects
            stats.invalid_state_shape_objects += meta.invalid_shapes
            stats.unsanitizable_state_objects += meta.unsanitizable
            stats.canonical_promoted_account_rows += int(meta.canonical_promoted)
            stats.removed_noncanonical_state_objects += len(meta.removed_noncanonical_paths)
            stats.removed_noncanonical_state_bytes += meta.removed_noncanonical_bytes
            stats.removed_duplicate_state_bytes += meta.removed_duplicate_bytes
            if meta.canonical_source_path:
                stats.canonical_source_paths[meta.canonical_source_path] += 1
            stats.removed_noncanonical_paths.update(meta.removed_noncanonical_paths)
            stats.preserved_unsanitizable_paths.update(meta.preserved_unsanitizable_paths)
            for report in meta.reports:
                stats.record_state(report)

            if not meta.reports:
                stats.record_row_sizes(original_bytes, original_bytes)
                continue
            stats.target_account_rows += 1
            if compacted == extra:
                stats.unchanged_target_account_rows += 1
                stats.record_row_sizes(original_bytes, original_bytes)
                continue

            new_raw = json.dumps(compacted, ensure_ascii=False, separators=(",", ":"))
            if not isinstance(json.loads(new_raw), dict):
                raise MigrationError(f"account {account_id}: compacted extra_json is not an object")
            new_bytes = _raw_bytes(new_raw)
            stats.changed_account_rows += 1
            stats.record_row_sizes(original_bytes, new_bytes)
            if len(stats.changed_account_ids_sample) < REPORT_SAMPLE_LIMIT:
                stats.changed_account_ids_sample.append(account_id)

            if apply:
                cursor = connection.execute(
                    "UPDATE accounts SET extra_json = ? WHERE id = ? AND extra_json = ?",
                    (new_raw, account_id, raw_extra),
                )
                if cursor.rowcount != 1:
                    raise MigrationError(
                        f"account {account_id}: concurrent update detected; rolling back"
                    )


def process_pending_invites(
    connection: sqlite3.Connection,
    *,
    limits: CompactionLimits,
    batch_size: int,
    apply: bool,
    stats: MigrationStats,
) -> None:
    if not stats.pending_table_present:
        return
    for rows in _iter_pending_batches(connection, batch_size=batch_size):
        stats.max_batch_rows = max(stats.max_batch_rows, len(rows))
        for pending_id, email, raw_state in rows:
            stats.scanned_pending_rows += 1
            original_bytes = _raw_bytes(raw_state)
            try:
                state = json.loads(raw_state or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                stats.invalid_pending_json_rows += 1
                stats.record_row_sizes(original_bytes, original_bytes)
                if len(stats.skipped_pending_ids_sample) < REPORT_SAMPLE_LIMIT:
                    stats.skipped_pending_ids_sample.append(pending_id)
                continue
            if not isinstance(state, dict):
                stats.non_object_pending_json_rows += 1
                stats.record_row_sizes(original_bytes, original_bytes)
                if len(stats.skipped_pending_ids_sample) < REPORT_SAMPLE_LIMIT:
                    stats.skipped_pending_ids_sample.append(pending_id)
                continue
            if not state:
                stats.record_row_sizes(original_bytes, original_bytes)
                continue

            stats.state_objects_seen += 1
            cleaned, report = sanitize_pending_state(
                state,
                account_email=email,
                limits=limits,
            )
            if cleaned is None or report is None:
                stats.unsanitizable_state_objects += 1
                stats.record_row_sizes(original_bytes, original_bytes)
                continue
            stats.target_pending_rows += 1
            stats.state_objects_sanitized += 1
            stats.record_state(report)
            if cleaned == state:
                stats.unchanged_target_pending_rows += 1
                stats.record_row_sizes(original_bytes, original_bytes)
                continue

            new_raw = json.dumps(cleaned, ensure_ascii=False, separators=(",", ":"))
            if not isinstance(json.loads(new_raw), dict):
                raise MigrationError(
                    f"pending invite {pending_id}: compacted mailbox_state_json is not an object"
                )
            new_bytes = _raw_bytes(new_raw)
            stats.changed_pending_rows += 1
            stats.record_row_sizes(original_bytes, new_bytes)
            if len(stats.changed_pending_ids_sample) < REPORT_SAMPLE_LIMIT:
                stats.changed_pending_ids_sample.append(pending_id)

            if apply:
                cursor = connection.execute(
                    "UPDATE pending_business_invites SET mailbox_state_json = ? "
                    "WHERE id = ? AND mailbox_state_json = ?",
                    (new_raw, pending_id, raw_state),
                )
                if cursor.rowcount != 1:
                    raise MigrationError(
                        f"pending invite {pending_id}: concurrent update detected; rolling back"
                    )


def process_database(
    connection: sqlite3.Connection,
    *,
    platform: str,
    limits: CompactionLimits,
    batch_size: int,
    apply: bool,
) -> MigrationStats:
    if batch_size < 1:
        raise MigrationError("batch_size must be >= 1")
    stats = MigrationStats(pending_table_present=_table_exists(connection, "pending_business_invites"))
    process_accounts(
        connection,
        platform=platform,
        limits=limits,
        batch_size=batch_size,
        apply=apply,
        stats=stats,
    )
    process_pending_invites(
        connection,
        limits=limits,
        batch_size=batch_size,
        apply=apply,
        stats=stats,
    )
    return stats


def run_migration(
    db_path: Path,
    *,
    platform: str = "chatgpt",
    apply: bool = False,
    backup_dir: Path | None = None,
    vacuum: bool = False,
    check_integrity_in_dry_run: bool = False,
    allow_open_db: bool = False,
    limits: CompactionLimits | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    busy_timeout_seconds: int = 30,
) -> dict[str, Any]:
    db_path = db_path.expanduser().resolve()
    if not db_path.is_file():
        raise MigrationError(f"database does not exist: {db_path}")
    if not platform.strip():
        raise MigrationError("platform must not be empty")
    if vacuum and not apply:
        raise MigrationError("--vacuum requires --apply")
    limits = limits or CompactionLimits()
    limits.validate()

    file_sizes_before = database_file_sizes(db_path)
    backup_path: Path | None = None
    open_processes: list[dict[str, Any]] = []

    if apply:
        open_processes = find_open_database_processes(db_path)
        if open_processes and not allow_open_db:
            summary = [
                {"pid": item["pid"], "command": item["command"]}
                for item in open_processes[:10]
            ]
            raise MigrationError(
                "database is open by another process; stop the owning service before apply: "
                f"{summary}"
            )
        backup_dir = (backup_dir or db_path.parent / "migration-backups").expanduser().resolve()
        ensure_apply_disk_headroom(db_path, backup_dir, vacuum=vacuum)

        source = _connect_readonly(db_path, busy_timeout_seconds=busy_timeout_seconds)
        try:
            validate_schema(source)
            integrity_check(source)
        finally:
            source.close()
        backup_path = create_verified_backup(
            db_path,
            backup_dir,
            busy_timeout_seconds=busy_timeout_seconds,
        )

        try:
            connection = _connect_writable(db_path, busy_timeout_seconds=busy_timeout_seconds)
            try:
                validate_schema(connection)
                connection.execute("BEGIN IMMEDIATE")
                try:
                    stats = process_database(
                        connection,
                        platform=platform,
                        limits=limits,
                        batch_size=batch_size,
                        apply=True,
                    )
                except Exception:
                    connection.rollback()
                    raise
                else:
                    connection.commit()

                checkpoint_row = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                checkpoint = list(checkpoint_row) if checkpoint_row is not None else []
                if checkpoint and int(checkpoint[0]) != 0:
                    raise MigrationError(f"post-migration WAL checkpoint remained busy: {checkpoint}")
                if vacuum:
                    connection.execute("VACUUM")
                    checkpoint_row = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                    checkpoint = list(checkpoint_row) if checkpoint_row is not None else []
                    if checkpoint and int(checkpoint[0]) != 0:
                        raise MigrationError(f"post-VACUUM WAL checkpoint remained busy: {checkpoint}")
                integrity_check(connection)
            finally:
                connection.close()
        except Exception as exc:
            raise MigrationError(
                f"migration failed after verified backup creation; backup={backup_path}; error={exc}"
            ) from exc
    else:
        connection = _connect_readonly(db_path, busy_timeout_seconds=busy_timeout_seconds)
        try:
            validate_schema(connection)
            if check_integrity_in_dry_run:
                integrity_check(connection)
            stats = process_database(
                connection,
                platform=platform,
                limits=limits,
                batch_size=batch_size,
                apply=False,
            )
        finally:
            connection.close()

    return {
        "migration": MIGRATION_NAME,
        "mode": "apply" if apply else "dry-run",
        "database": str(db_path),
        "platform": platform,
        "limits": asdict(limits),
        "backup_path": str(backup_path) if backup_path else "",
        "vacuum": bool(vacuum),
        "open_processes_seen": open_processes if allow_open_db else [],
        "file_sizes_before": file_sizes_before,
        "file_sizes_after": database_file_sizes(db_path),
        "stats": stats.to_dict(),
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Dry-run or apply the bounded mailbox-state migration. "
            "Dry-run is the default and never writes the database."
        )
    )
    parser.add_argument("database", type=Path, help="account_manager.db path")
    parser.add_argument("--platform", default="chatgpt", help="accounts.platform value")
    parser.add_argument("--apply", action="store_true", help="create verified backup and apply atomically")
    parser.add_argument(
        "--backup-dir",
        type=Path,
        help="backup directory (default: <database-dir>/migration-backups)",
    )
    parser.add_argument(
        "--vacuum",
        action="store_true",
        help="after apply/checkpoint, run exclusive VACUUM to reclaim file space",
    )
    parser.add_argument(
        "--check-integrity",
        action="store_true",
        help="also run full PRAGMA integrity_check in dry-run (apply always checks)",
    )
    parser.add_argument(
        "--allow-open-db",
        action="store_true",
        help="DANGEROUS: override the apply-time open-process guard",
    )
    parser.add_argument("--max-before-ids", type=int, default=DEFAULT_MAX_BEFORE_IDS)
    parser.add_argument("--max-before-id-bytes", type=int, default=DEFAULT_MAX_BEFORE_ID_BYTES)
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help="keyset page size; 1 is strict row-at-a-time processing",
    )
    parser.add_argument("--busy-timeout", type=int, default=30, help="SQLite busy timeout seconds")
    parser.add_argument("--report-json", type=Path, help="optionally write the non-secret JSON report")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        report = run_migration(
            args.database,
            platform=args.platform,
            apply=args.apply,
            backup_dir=args.backup_dir,
            vacuum=args.vacuum,
            check_integrity_in_dry_run=args.check_integrity,
            allow_open_db=args.allow_open_db,
            limits=CompactionLimits(
                max_before_ids=args.max_before_ids,
                max_before_id_bytes=args.max_before_id_bytes,
            ),
            batch_size=args.batch_size,
            busy_timeout_seconds=args.busy_timeout,
        )
    except (MigrationError, sqlite3.Error, OSError) as exc:
        print(
            json.dumps(
                {"migration": MIGRATION_NAME, "status": "error", "error": str(exc)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2

    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.report_json:
        report_path = args.report_json.expanduser().resolve()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = report_path.with_name(f".{report_path.name}.{os.getpid()}.tmp")
        temporary.write_text(rendered + "\n", encoding="utf-8")
        os.replace(temporary, report_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
