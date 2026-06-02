import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from services import tempmail_archive_cleanup as cleanup


class _Response:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text or str(self._payload)

    def json(self):
        return self._payload


class _FakeStore:
    def __init__(self, values):
        self.values = dict(values)

    def get(self, key, default=""):
        value = self.values.get(key, "")
        return value if value not in (None, "") else default


class _FakeTempMailClient:
    def __init__(self, *, old_at: str, recent_at: str):
        self.old_at = old_at
        self.recent_at = recent_at
        self.deleted_ids = []

    def _headers(self):
        return {}

    def find_mailbox_by_email(self, email):
        return SimpleNamespace(account_id="mailbox-1", email=email, extra={})

    def _request(self, method, path, *, timeout, **kwargs):
        if method == "GET" and path == "/api/mailboxes/mailbox-1/emails":
            return _Response(
                200,
                {
                    "data": [
                        {"id": "old-email", "subject": "old", "received_at": self.old_at},
                        {"id": "recent-email", "subject": "recent", "received_at": self.recent_at},
                    ]
                },
            )
        if method == "DELETE":
            self.deleted_ids.append(path.rsplit("/", 1)[-1])
            return _Response(204, {})
        raise AssertionError(f"unexpected request: {method} {path}")

    def _get_email_detail(self, mailbox_id, email_id):
        if email_id == "old-email":
            return {
                "id": email_id,
                "received_at": self.old_at,
                "body_text": "old body",
                "received_for": ["b@cccy.me", "alias-old@icloud.com"],
            }
        if email_id == "recent-email":
            return {
                "id": email_id,
                "received_at": self.recent_at,
                "body_text": "recent body",
                "received_for": ["b@cccy.me", "alias-recent@icloud.com"],
            }
        raise AssertionError(f"unexpected detail: {email_id}")


class TempMailArchiveCleanupTests(unittest.TestCase):
    def setUp(self):
        cleanup._next_run_at = 0
        cleanup._last_run_at = 0
        cleanup._last_success_at = 0
        cleanup._last_error = ""
        cleanup._last_result = {}

    def test_run_once_archives_then_deletes_only_old_messages(self):
        now = datetime.now(timezone.utc)
        old_at = (now - timedelta(hours=2)).isoformat()
        recent_at = (now - timedelta(minutes=5)).isoformat()

        with tempfile.TemporaryDirectory() as tmpdir:
            backup_path = f"{tmpdir}/archive.db"
            store = _FakeStore(
                {
                    "tempmail_archive_cleanup_enabled": "true",
                    "tempmail_archive_cleanup_threshold": "1",
                    "tempmail_archive_cleanup_keep_recent_minutes": "60",
                    "tempmail_archive_cleanup_backup_path": backup_path,
                    "tempmail_archive_cleanup_mailbox": "b@cccy.me",
                    "tempmail_api_url": "http://tempmail-api-1:8080",
                    "tempmail_api_key": "key",
                }
            )
            client = _FakeTempMailClient(old_at=old_at, recent_at=recent_at)

            with patch.object(cleanup, "_config_store", return_value=store), patch.object(
                cleanup, "_active_task_snapshots", return_value=[]
            ), patch.object(cleanup, "_make_tempmail_client", return_value=client):
                result = cleanup.run_once()

            self.assertTrue(result["ok"])
            self.assertEqual(result["email_count"], 2)
            self.assertEqual(result["archived"], 2)
            self.assertEqual(result["deleted"], 1)
            self.assertEqual(result["kept_recent"], 1)
            self.assertEqual(client.deleted_ids, ["old-email"])

            with sqlite3.connect(backup_path) as conn:
                rows = conn.execute(
                    "SELECT email_id, deleted_at FROM tempmail_email_archive ORDER BY email_id"
                ).fetchall()
            self.assertEqual([row[0] for row in rows], ["old-email", "recent-email"])
            self.assertTrue(rows[0][1])
            self.assertIsNone(rows[1][1])

    def test_run_once_skips_when_active_tasks_are_present(self):
        store = _FakeStore(
            {
                "tempmail_archive_cleanup_enabled": "true",
                "tempmail_archive_cleanup_pause_active_tasks": "true",
                "tempmail_api_url": "http://tempmail-api-1:8080",
                "tempmail_api_key": "key",
            }
        )

        with patch.object(cleanup, "_config_store", return_value=store), patch.object(
            cleanup, "_active_task_snapshots", return_value=[{"id": "task-1", "status": "running"}]
        ), patch.object(cleanup, "_make_tempmail_client") as make_client:
            result = cleanup.run_once()

        self.assertFalse(result["ok"])
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "active_tasks")
        self.assertEqual(result["active_task_count"], 1)
        make_client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
