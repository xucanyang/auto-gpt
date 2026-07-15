#!/usr/bin/env python3
"""Import durable successful long-link records into active Auto-GPT account DBs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.long_link_history_sync import LongLinkHistorySyncError, synchronize_long_link_success_history


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Sync long-link successful-link history into one or more Auto-GPT account databases. "
            "Dry-run is the default."
        )
    )
    parser.add_argument("--source-db", type=Path, required=True, help="long-link tasks.db path")
    parser.add_argument(
        "--target-db",
        type=Path,
        action="append",
        required=True,
        help="target account_manager.db path; repeat for each active instance",
    )
    parser.add_argument("--apply", action="store_true", help="create verified backups and commit the import")
    parser.add_argument("--backup-dir", type=Path, help="directory for generated .backup files")
    parser.add_argument("--platform", default="chatgpt", help="target accounts.platform value")
    parser.add_argument("--busy-timeout", type=int, default=30, help="SQLite busy timeout in seconds")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        report = synchronize_long_link_success_history(
            source_database=args.source_db,
            target_databases=args.target_db,
            apply=bool(args.apply),
            backup_dir=args.backup_dir,
            platform=args.platform,
            busy_timeout_seconds=max(int(args.busy_timeout), 1),
        )
    except (LongLinkHistorySyncError, OSError, sqlite3.Error) as exc:
        print(json.dumps({"sync": "long_link_history_sync", "status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
