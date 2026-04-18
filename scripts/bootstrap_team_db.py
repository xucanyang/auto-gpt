from __future__ import annotations

import os
import sqlite3
from pathlib import Path


def _env_path(name: str, default: str = "") -> Path:
    return Path(str(os.environ.get(name) or default).strip())


def _bootstrap(seed_path: Path, target_path: Path) -> None:
    if target_path.exists() and target_path.stat().st_size > 0:
        print(f"[bootstrap-team-db] Reusing existing local Team DB: {target_path}")
        return

    if not seed_path.exists():
        print(f"[bootstrap-team-db] Seed Team DB not found, skip bootstrap: {seed_path}")
        return

    target_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"[bootstrap-team-db] Bootstrapping local Team DB: {seed_path} -> {target_path}")
    with sqlite3.connect(f"file:{seed_path}?mode=ro", uri=True) as source_conn:
        with sqlite3.connect(target_path) as target_conn:
            source_conn.backup(target_conn)


if __name__ == "__main__":
    seed = _env_path("TEAM_MANAGER_SEED_DB_PATH", "/team-manage-seed-data/team_manage.db")
    target = _env_path("TEAM_MANAGER_DB_PATH", "/runtime/team_manage.db")
    _bootstrap(seed, target)
