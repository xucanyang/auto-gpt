from __future__ import annotations

import gzip
import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "redact-nginx-query-tokens.py"
SPEC = importlib.util.spec_from_file_location("redact_nginx_query_tokens", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_redacts_plain_and_gzip_logs_without_copying_token_to_summary(tmp_path):
    plain = tmp_path / "access.log.1"
    compressed = tmp_path / "error.log.2.gz"
    token = "header.payload.signature"
    plain.write_text(f'GET /stream?access_token={token}&since=1 HTTP/1.1\n')
    with gzip.open(compressed, "wb") as handle:
        handle.write(f'request: "GET /stream?access_token={token} HTTP/2"\n'.encode())
    summary = tmp_path / "summary.json"

    assert MODULE.main([
        "--root",
        str(tmp_path),
        "--apply",
        "--summary",
        str(summary),
    ]) == 0

    assert token not in plain.read_text()
    assert "access_token=<redacted>" in plain.read_text()
    with gzip.open(compressed, "rt") as handle:
        compressed_text = handle.read()
    assert token not in compressed_text
    assert "access_token=<redacted>" in compressed_text
    summary_text = summary.read_text()
    assert token not in summary_text
    payload = json.loads(summary_text)
    assert payload["files_with_matches"] == 2
    assert payload["redacted_values"] == 2

    assert MODULE.main(["--root", str(tmp_path)]) == 0
    assert token not in plain.read_text()
    assert "access_token=<redacted>" in plain.read_text()


def test_dry_run_reports_but_does_not_rewrite(tmp_path):
    log = tmp_path / "access.log"
    original = 'GET /stream?access_token=sensitive-value HTTP/1.1\n'
    log.write_text(original)

    assert MODULE.main(["--root", str(tmp_path)]) == 0
    assert log.read_text() == original
