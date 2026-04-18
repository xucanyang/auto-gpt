import unittest
from unittest import mock

from core.db import AccountModel
from services.sub2api_sync import backfill_chatgpt_account_to_sub2api, probe_chatgpt_sub2api_status


class _FakeCursor:
    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, query, params):
        self.query = query
        self.params = params

    def fetchall(self):
        return list(self.rows)


class _FakeConn:
    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def cursor(self):
        return _FakeCursor(self.rows)


class Sub2ApiSyncTests(unittest.TestCase):
    def _make_account(self) -> AccountModel:
        account = AccountModel(
            platform="chatgpt",
            email="demo@example.com",
            password="secret",
            token="",
            status="registered",
        )
        account.set_extra(
            {
                "access_token": "",
                "refresh_token": "",
                "id_token": "",
            }
        )
        return account

    def test_probe_reports_existing_remote_account_by_email(self):
        account = self._make_account()
        rows = [
            {
                "id": 321,
                "name": "demo@example.com",
                "status": "active",
                "credentials": {},
                "extra": {"email": "demo@example.com"},
                "updated_at": None,
                "created_at": None,
            }
        ]

        with mock.patch("services.sub2api_sync._fetch_matching_rows", return_value=rows):
            result = probe_chatgpt_sub2api_status(account)

        self.assertEqual(result["remote_state"], "exists")
        self.assertEqual(result["remote_account_id"], 321)
        self.assertIn("email", result["matched_by"])

    def test_probe_marks_cross_workspace_only_when_workspace_identity_present(self):
        account = self._make_account()

        with mock.patch(
            "services.sub2api_sync.build_sub2api_lookup_payload",
            return_value={
                "name": "demo@example.com",
                "extra": {"email": "demo@example.com"},
                "credentials": {
                    "organization_id": "org-local",
                    "chatgpt_account_id": "acct-local",
                    "chatgpt_user_id": "user-local",
                },
            },
        ):
            with mock.patch(
                "services.sub2api_sync._fetch_matching_rows",
                side_effect=[
                    [
                        {
                            "id": 654,
                            "name": "demo@example.com",
                            "status": "active",
                            "credentials": {
                                "organization_id": "org-other",
                                "chatgpt_account_id": "acct-other",
                                "chatgpt_user_id": "user-local",
                            },
                            "extra": {"email": "demo@example.com"},
                            "updated_at": None,
                            "created_at": None,
                        }
                    ],
                    [],
                ],
            ):
                result = probe_chatgpt_sub2api_status(account)

        self.assertEqual(result["remote_state"], "cross_workspace_only")
        self.assertIn("其他 workspace", result["message"])

    def test_probe_reports_deleted_exact_match_before_weak_live_match(self):
        account = self._make_account()

        with mock.patch(
            "services.sub2api_sync.build_sub2api_lookup_payload",
            return_value={
                "name": "demo@example.com",
                "extra": {"email": "demo@example.com"},
                "credentials": {
                    "organization_id": "org-local",
                    "chatgpt_account_id": "acct-local",
                    "chatgpt_user_id": "user-local",
                },
            },
        ):
            with mock.patch(
                "services.sub2api_sync._fetch_matching_rows",
                side_effect=[
                    [
                        {
                            "id": 654,
                            "name": "demo@example.com",
                            "status": "active",
                            "credentials": {
                                "organization_id": "org-other",
                                "chatgpt_account_id": "acct-other",
                                "chatgpt_user_id": "user-local",
                            },
                            "extra": {"email": "demo@example.com"},
                            "updated_at": None,
                            "created_at": None,
                        }
                    ],
                    [
                        {
                            "id": 655,
                            "name": "demo@example.com",
                            "status": "active",
                            "credentials": {
                                "organization_id": "org-local",
                                "chatgpt_account_id": "acct-local",
                                "chatgpt_user_id": "user-local",
                            },
                            "extra": {"email": "demo@example.com"},
                            "updated_at": None,
                            "created_at": None,
                        },
                        {
                            "id": 656,
                            "name": "demo@example.com",
                            "status": "error",
                            "credentials": {
                                "organization_id": "org-local",
                                "chatgpt_account_id": "acct-local",
                                "chatgpt_user_id": "user-local",
                            },
                            "extra": {"email": "demo@example.com"},
                            "updated_at": None,
                            "created_at": None,
                        },
                    ],
                ],
            ):
                result = probe_chatgpt_sub2api_status(account)

        self.assertEqual(result["remote_state"], "deleted_exact_match")
        self.assertEqual(result["candidate_count"], 2)
        self.assertIn("已删除的精确", result["message"])

    def test_backfill_skips_when_remote_already_exists(self):
        account = self._make_account()

        with mock.patch(
            "services.sub2api_sync.probe_chatgpt_sub2api_status",
            return_value={
                "remote_state": "exists",
                "uploaded": True,
                "remote_account_id": 321,
                "matched_by": "email",
                "message": "远端已存在",
            },
        ) as probe_mock:
            with mock.patch("services.sub2api_sync.upload_to_sub2api") as upload_mock:
                result = backfill_chatgpt_account_to_sub2api(account, commit=False)

        self.assertTrue(result["ok"])
        self.assertTrue(result["skipped"])
        self.assertFalse(result["uploaded"])
        probe_mock.assert_called_once()
        upload_mock.assert_not_called()

    def test_backfill_uploads_and_verifies_when_remote_missing(self):
        account = self._make_account()

        with mock.patch(
            "services.sub2api_sync.probe_chatgpt_sub2api_status",
            side_effect=[
                {
                    "remote_state": "not_found",
                    "uploaded": False,
                    "message": "远端未发现",
                },
                {
                    "remote_state": "exists",
                    "uploaded": True,
                    "remote_account_id": 654,
                    "matched_by": "email",
                    "message": "远端已存在",
                },
            ],
        ) as probe_mock:
            with mock.patch(
                "services.sub2api_sync.probe_local_chatgpt_status",
                return_value={
                    "auth": {"state": "refresh_token_valid", "message": "ok"},
                    "subscription": {"plan": "free"},
                    "codex": {"state": "usable"},
                },
            ):
                with mock.patch(
                    "services.sub2api_sync.upload_to_sub2api",
                    return_value=(True, "上传成功"),
                ) as upload_mock:
                    result = backfill_chatgpt_account_to_sub2api(account, commit=False)

        self.assertTrue(result["ok"])
        self.assertTrue(result["uploaded"])
        self.assertFalse(result["skipped"])
        self.assertIn("补传完成", result["message"])
        self.assertEqual(probe_mock.call_count, 2)
        upload_mock.assert_called_once()

    def test_backfill_uploads_when_only_other_workspace_matches(self):
        account = self._make_account()

        with mock.patch(
            "services.sub2api_sync.probe_chatgpt_sub2api_status",
            side_effect=[
                {
                    "remote_state": "cross_workspace_only",
                    "uploaded": False,
                    "matched_by": "email, chatgpt_user_id",
                    "message": "仅命中同邮箱/同用户的其他 workspace，可为当前 workspace 补传",
                    "candidate_count": 1,
                    "candidates": [{"id": 777}],
                },
                {
                    "remote_state": "exists",
                    "uploaded": True,
                    "remote_account_id": 778,
                    "matched_by": "email, organization_account, chatgpt_user_id",
                    "message": "远端已存在",
                },
            ],
        ) as probe_mock:
            with mock.patch(
                "services.sub2api_sync.probe_local_chatgpt_status",
                return_value={
                    "auth": {"state": "refresh_token_valid", "message": "ok"},
                    "subscription": {"plan": "free"},
                    "codex": {"state": "usable"},
                },
            ):
                with mock.patch(
                    "services.sub2api_sync.upload_to_sub2api",
                    return_value=(True, "上传成功"),
                ) as upload_mock:
                    result = backfill_chatgpt_account_to_sub2api(account, commit=False)

        self.assertTrue(result["ok"])
        self.assertTrue(result["uploaded"])
        self.assertFalse(result["skipped"])
        self.assertEqual(probe_mock.call_count, 2)
        upload_mock.assert_called_once()

    def test_backfill_uploads_when_exact_remote_rows_were_soft_deleted(self):
        account = self._make_account()

        with mock.patch(
            "services.sub2api_sync.probe_chatgpt_sub2api_status",
            side_effect=[
                {
                    "remote_state": "deleted_exact_match",
                    "uploaded": False,
                    "matched_by": "organization_account, email, chatgpt_user_id",
                    "message": "远端存在 2 条已删除的精确 Sub2API 记录，可重新补传",
                    "candidate_count": 2,
                    "candidates": [{"id": 777}, {"id": 778}],
                },
                {
                    "remote_state": "exists",
                    "uploaded": True,
                    "remote_account_id": 779,
                    "matched_by": "organization_account, email, chatgpt_user_id",
                    "message": "远端已存在",
                },
            ],
        ) as probe_mock:
            with mock.patch(
                "services.sub2api_sync.probe_local_chatgpt_status",
                return_value={
                    "auth": {"state": "refresh_token_valid", "message": "ok"},
                    "subscription": {"plan": "free"},
                    "codex": {"state": "usable"},
                },
            ):
                with mock.patch(
                    "services.sub2api_sync.upload_to_sub2api",
                    return_value=(True, "上传成功"),
                ) as upload_mock:
                    result = backfill_chatgpt_account_to_sub2api(account, commit=False)

        self.assertTrue(result["ok"])
        self.assertTrue(result["uploaded"])
        self.assertFalse(result["skipped"])
        self.assertEqual(probe_mock.call_count, 2)
        upload_mock.assert_called_once()

    def test_backfill_reprobes_when_cached_state_is_not_found(self):
        account = self._make_account()
        account.set_extra(
            {
                "access_token": "",
                "refresh_token": "",
                "id_token": "",
                "sync_statuses": {
                    "sub2api": {
                        "remote_state": "not_found",
                        "uploaded": False,
                        "message": "缓存未发现",
                    }
                },
            }
        )

        with mock.patch(
            "services.sub2api_sync.probe_chatgpt_sub2api_status",
            return_value={
                "remote_state": "exists",
                "uploaded": True,
                "remote_account_id": 888,
                "matched_by": "organization_account",
                "message": "远端已存在",
            },
        ) as probe_mock:
            with mock.patch("services.sub2api_sync.upload_to_sub2api") as upload_mock:
                result = backfill_chatgpt_account_to_sub2api(account, commit=False)

        self.assertTrue(result["ok"])
        self.assertTrue(result["skipped"])
        self.assertFalse(result["uploaded"])
        probe_mock.assert_called_once()
        upload_mock.assert_not_called()

    def test_backfill_persists_blocked_message_when_local_probe_invalid(self):
        account = self._make_account()

        with mock.patch(
            "services.sub2api_sync.probe_chatgpt_sub2api_status",
            return_value={
                "remote_state": "deleted_exact_match",
                "uploaded": False,
                "message": "远端存在 2 条已删除的精确 Sub2API 记录，可重新补传",
                "candidate_count": 2,
                "candidates": [{"id": 777}, {"id": 778}],
            },
        ):
            with mock.patch(
                "services.sub2api_sync.probe_local_chatgpt_status",
                return_value={
                    "auth": {
                        "state": "access_token_invalidated",
                        "message": "token invalidated",
                    },
                    "subscription": {"plan": "unknown"},
                    "codex": {"state": "skipped_auth_invalid"},
                },
            ):
                result = backfill_chatgpt_account_to_sub2api(account, commit=False)

        self.assertFalse(result["ok"])
        self.assertFalse(result["uploaded"])
        extra = account.get_extra()
        state = extra["sync_statuses"]["sub2api"]
        self.assertEqual(state["remote_state"], "deleted_exact_match")
        self.assertIn("当前无法补传", state["message"])
        self.assertIn("token invalidated", state["message"])

    def test_backfill_reprobes_when_cached_state_is_ambiguous(self):
        account = self._make_account()
        account.set_extra(
            {
                "access_token": "",
                "refresh_token": "",
                "id_token": "",
                "sync_statuses": {
                    "sub2api": {
                        "remote_state": "ambiguous",
                        "uploaded": False,
                        "message": "缓存多候选",
                    }
                },
            }
        )

        with mock.patch(
            "services.sub2api_sync.probe_chatgpt_sub2api_status",
            return_value={
                "remote_state": "exists",
                "uploaded": True,
                "remote_account_id": 889,
                "matched_by": "organization_account",
                "message": "远端已存在",
            },
        ) as probe_mock:
            with mock.patch("services.sub2api_sync.upload_to_sub2api") as upload_mock:
                result = backfill_chatgpt_account_to_sub2api(account, commit=False)

        self.assertTrue(result["ok"])
        self.assertTrue(result["skipped"])
        self.assertFalse(result["uploaded"])
        probe_mock.assert_called_once()
        upload_mock.assert_not_called()

    def test_backfill_reprobes_when_cached_exists_contains_candidate_residue(self):
        account = self._make_account()
        account.set_extra(
            {
                "access_token": "",
                "refresh_token": "",
                "id_token": "",
                "sync_statuses": {
                    "sub2api": {
                        "remote_state": "exists",
                        "uploaded": True,
                        "message": "远端已存在",
                        "candidate_count": 2,
                        "candidates": [{"id": 1}, {"id": 2}],
                    }
                },
            }
        )

        with mock.patch(
            "services.sub2api_sync.probe_chatgpt_sub2api_status",
            return_value={
                "remote_state": "exists",
                "uploaded": True,
                "remote_account_id": 890,
                "matched_by": "organization_account",
                "message": "远端已存在",
            },
        ) as probe_mock:
            with mock.patch("services.sub2api_sync.upload_to_sub2api") as upload_mock:
                result = backfill_chatgpt_account_to_sub2api(account, commit=False)

        self.assertTrue(result["ok"])
        self.assertTrue(result["skipped"])
        self.assertFalse(result["uploaded"])
        probe_mock.assert_called_once()
        upload_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
