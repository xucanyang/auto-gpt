#!/usr/bin/env python3
"""Safely collapse legacy global dynamic-proxy fields into canonical fields.

Dry-run is the default. Apply mode verifies the shared SQLite database, creates an
online SQLite backup, scrubs historical audit records that could contain proxy
credentials, performs an optimistic-revision config update, and verifies integrity
again. It never prints the proxy template, URL, username, password, or full diff.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.shared_config import SharedConfigStore
from core.task_proxy_config import normalize_dynamic_proxy_snapshot


def _integrity_check(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
    values = [str(row[0] or "").strip().lower() for row in rows]
    if values != ["ok"]:
        raise RuntimeError(f"SQLite integrity_check failed: {values[:3]}")


def _online_backup(source_path: Path, backup_path: Path) -> None:
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source_path) as source, sqlite3.connect(backup_path) as target:
        source.backup(target)
    backup_path.chmod(0o600)
    _integrity_check(backup_path)


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _report(
    *,
    db_path: Path,
    revision: int,
    normalization: dict[str, Any],
    apply: bool,
    backup_path: Path | None = None,
    audit: dict[str, int] | None = None,
    result: dict[str, Any] | None = None,
) -> None:
    # normalization.report() intentionally contains only present/length/hash summaries.
    print(
        json.dumps(
            {
                "ok": True,
                "apply": apply,
                "db": str(db_path),
                "revision_before": revision,
                "normalization": normalization,
                "backup": str(backup_path) if backup_path else "",
                "audit": audit or {},
                "write": result or {},
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="归一化共享动态代理配置（默认 dry-run）")
    parser.add_argument(
        "--shared-db",
        type=Path,
        default=REPO_ROOT / "shared_config" / "shared_config.db",
        help="共享配置 SQLite 路径",
    )
    parser.add_argument("--apply", action="store_true", help="执行备份、审计脱敏和配置迁移")
    parser.add_argument("--expected-revision", type=int, default=None, help="要求当前共享 revision，避免并发覆盖")
    args = parser.parse_args()

    db_path = args.shared_db.resolve()
    if not db_path.exists():
        raise RuntimeError(f"共享配置数据库不存在: {db_path}")
    _integrity_check(db_path)

    store = SharedConfigStore(db_path)
    revision = store.revision()
    if args.expected_revision is not None and revision != args.expected_revision:
        raise RuntimeError(f"共享配置 revision 不一致: expected={args.expected_revision}, current={revision}")

    normalization = normalize_dynamic_proxy_snapshot(store.get_all())
    if not args.apply:
        _report(
            db_path=db_path,
            revision=revision,
            normalization=normalization.report(),
            apply=False,
        )
        return 0

    backup_path = db_path.parent / "backups" / f"shared_config.before-dynamic-proxy-normalize-{_timestamp()}.db"
    _online_backup(db_path, backup_path)
    audit = store.redact_legacy_audit()
    write: dict[str, Any] = {"ok": True, "changed": False, "revision": revision, "changed_keys": []}
    if normalization.updates:
        write = store.write(
            normalization.updates,
            base_revision=revision,
            updated_by="dynamic-proxy-migration",
            action="migrate",
            note="normalize-dynamic-proxy-config",
        )
    _integrity_check(db_path)
    _report(
        db_path=db_path,
        revision=revision,
        normalization=normalization.report(),
        apply=True,
        backup_path=backup_path,
        audit=audit,
        result=write,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
