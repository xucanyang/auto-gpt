"""Pytest safety guard.

The live deployment keeps ``account_manager.db`` as a symlink to
``/runtime/account_manager.db`` inside the container.  A plain ``pytest`` run can
therefore mutate or drop the live account inventory if ``core.db`` is imported
before an individual test swaps the database engine.

Keep test runs isolated by default.  If a maintainer really wants to test
against the live DB, they must opt in explicitly with ALLOW_LIVE_DB_TESTS=1.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from urllib.parse import unquote


REPO_ROOT = Path(__file__).resolve().parent
LIVE_DB_PATHS = {
    (REPO_ROOT / "data" / "account_manager.db").resolve(strict=False),
    (REPO_ROOT / "account_manager.db").resolve(strict=False),
    Path("/runtime/account_manager.db").resolve(strict=False),
}


def _sqlite_path(database_url: str) -> Path | str | None:
    url = (database_url or "").strip()
    if not url:
        return None
    if url == "sqlite:///:memory:":
        return ":memory:"
    if not url.startswith("sqlite:///"):
        return None

    raw_path = unquote(url[len("sqlite:///") :])
    if not raw_path:
        return None
    path = Path(raw_path)
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path.resolve(strict=False)


def _configure_safe_database_url() -> None:
    if os.environ.get("ALLOW_LIVE_DB_TESTS") == "1":
        return

    current_url = os.environ.get("DATABASE_URL", "").strip()
    current_path = _sqlite_path(current_url)
    if isinstance(current_path, Path) and current_path in LIVE_DB_PATHS:
        raise RuntimeError(
            "Refusing to run pytest against the live account database: "
            f"{current_path}. Set ALLOW_LIVE_DB_TESTS=1 only if this is intentional."
        )

    if current_url:
        return

    test_dir = Path(tempfile.mkdtemp(prefix="any-auto-register-pytest-"))
    test_db = test_dir / "account_manager.test.db"
    os.environ["DATABASE_URL"] = f"sqlite:///{test_db}"


_configure_safe_database_url()
