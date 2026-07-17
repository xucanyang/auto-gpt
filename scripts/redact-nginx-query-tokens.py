#!/usr/bin/env python3
"""Redact URL-carried administrator tokens from nginx log history."""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import stat
import tempfile
from datetime import datetime
from pathlib import Path


TOKEN_PATTERN = re.compile(rb"(?i)(access_token=)[^&\s\"']+")


def _candidate_files(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.iterdir()
        if path.is_file()
        and (path.name.endswith(".log") or ".log." in path.name)
        and not path.is_symlink()
    )


def _read(path: Path) -> bytes:
    if path.suffix == ".gz":
        with gzip.open(path, "rb") as handle:
            return handle.read()
    return path.read_bytes()


def _atomic_write(path: Path, payload: bytes) -> None:
    original = path.stat()
    fd, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(raw_temp)
    try:
        with os.fdopen(fd, "wb") as handle:
            if path.suffix == ".gz":
                with gzip.GzipFile(fileobj=handle, mode="wb", mtime=0) as compressed:
                    compressed.write(payload)
            else:
                handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, stat.S_IMODE(original.st_mode))
        os.chown(temp, original.st_uid, original.st_gid)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("/var/log/nginx"))
    parser.add_argument("--apply", action="store_true", help="rewrite matching logs")
    parser.add_argument("--summary", type=Path, help="write credential-free JSON summary")
    args = parser.parse_args(argv)

    if not args.root.is_dir():
        raise FileNotFoundError(args.root)

    matches: list[dict[str, object]] = []
    for path in _candidate_files(args.root):
        payload = _read(path)
        redacted, count = TOKEN_PATTERN.subn(rb"\1<redacted>", payload)
        if not count:
            continue
        matches.append({"file": str(path), "redacted_values": count})
        if args.apply:
            _atomic_write(path, redacted)

    summary = {
        "scanned_at": datetime.now().astimezone().isoformat(),
        "mode": "apply" if args.apply else "dry-run",
        "files_with_matches": len(matches),
        "redacted_values": sum(int(item["redacted_values"]) for item in matches),
        "files": matches,
    }
    if args.summary:
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
        os.chmod(args.summary, 0o600)

    print(
        f"mode={summary['mode']} files_with_matches={summary['files_with_matches']} "
        f"redacted_values={summary['redacted_values']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
