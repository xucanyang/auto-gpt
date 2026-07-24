import unittest
from unittest import mock

from core.db import AccountModel
from services.sub2api_sync import backfill_chatgpt_account_to_sub2api, probe_chatgpt_sub2api_status
from services.chatgpt_core.sub2api_upload import build_sub2api_account_payload, upload_to_sub2api_detailed


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
                "access_token": "at-demo",
                "refresh_token": "rt-demo",
                "id_token": "",
                "workspace_id": "ws-demo",
            }
        )
        account.user_id = "acct-demo"
        return account

    def test_probe_reports_existing_remote_account_by_email_via_api(self):
        account = self._make_account()
        rows = [
            {
                "id": 321,
                "name": "demo@example.com",
                "status": "active",
                "credentials": {},
                "extra": {"email": "demo@example.com"},
                "updated_at": None,
            }
        ]

        with mock.patch("services.sub2api_sync._fetch_sub2api_account_items", return_value=rows):
            result = probe_chatgpt_sub2api_status(account)

        self.assertEqual(result["remote_state"], "exists")
        self.assertEqual(result["remote_account_id"], 321)
        self.assertEqual(result["probe_source"], "api")
        self.assertIn("email", result["matched_by"])

    def test_probe_marks_cross_workspace_only_when_workspace_identity_present_via_api(self):
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
                "services.sub2api_sync._fetch_sub2api_account_items",
                return_value=[
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
                    }
                ],
            ):
                result = probe_chatgpt_sub2api_status(account)

        self.assertEqual(result["remote_state"], "cross_workspace_only")
        self.assertEqual(result["probe_source"], "api")
        self.assertIn("其他 workspace", result["message"])

    def test_probe_reports_ambiguous_exact_matches_via_api(self):
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
                "services.sub2api_sync._fetch_sub2api_account_items",
                return_value=[
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
                    },
                ],
            ):
                result = probe_chatgpt_sub2api_status(account)

        self.assertEqual(result["remote_state"], "ambiguous")
        self.assertEqual(result["candidate_count"], 2)
        self.assertIn("API 匹配到 2 条精确", result["message"])

    def test_probe_reports_unreachable_when_api_fails(self):
        account = self._make_account()
        with mock.patch("services.sub2api_sync._fetch_sub2api_account_items", side_effect=RuntimeError("boom")):
            result = probe_chatgpt_sub2api_status(account)
        self.assertEqual(result["remote_state"], "unreachable")
        self.assertEqual(result["probe_source"], "api")
        self.assertIn("Sub2API API 不可用", result["message"])

    def test_backfill_uploads_once_without_remote_or_local_probe_even_when_cached_exists(self):
        account = self._make_account()
        extra = account.get_extra()
        extra["sync_statuses"] = {
            "sub2api": {
                "remote_state": "exists",
                "uploaded": True,
                "remote_account_id": 321,
                "message": "cached exists",
                "probe_source": "api",
            }
        }
        account.set_extra(extra)

        with mock.patch(
            "services.sub2api_sync.probe_chatgpt_sub2api_status",
            side_effect=AssertionError("remote probe must not run"),
        ):
            with mock.patch(
                "services.chatgpt_core.status_probe.probe_local_chatgpt_status",
                side_effect=AssertionError("local probe must not run"),
            ):
                with mock.patch(
                    "services.sub2api_sync.upload_to_sub2api_detailed",
                    return_value={"ok": True, "message": "上传成功", "remote_account_id": 889, "remote_status": "active"},
                ) as upload_mock:
                    result = backfill_chatgpt_account_to_sub2api(account, commit=False)

        self.assertTrue(result["ok"])
        self.assertTrue(result["uploaded"])
        self.assertFalse(result["skipped"])
        upload_mock.assert_called_once()
        state = account.get_extra()["sync_statuses"]["sub2api"]
        self.assertEqual(state["remote_state"], "uploaded")
        self.assertEqual(state["probe_source"], "upload")
        self.assertEqual(state["last_upload"]["status"], "success")
        self.assertEqual(state["last_upload"]["remote_account_id"], 889)

    def test_backfill_failure_without_cache_uses_unknown_upload_attempt_state(self):
        account = self._make_account()
        with mock.patch(
            "services.sub2api_sync.upload_to_sub2api_detailed",
            return_value={"ok": False, "message": "upload rejected"},
        ) as upload_mock:
            result = backfill_chatgpt_account_to_sub2api(account, commit=False)

        self.assertFalse(result["ok"])
        upload_mock.assert_called_once()
        state = account.get_extra()["sync_statuses"]["sub2api"]
        self.assertEqual(state["remote_state"], "unknown")
        self.assertEqual(state["probe_source"], "upload")
        self.assertEqual(state["last_upload"]["status"], "failed")

    def test_backfill_failure_preserves_cached_remote_exists_fact(self):
        account = self._make_account()
        extra = account.get_extra()
        extra["sync_statuses"] = {
            "sub2api": {
                "remote_state": "exists",
                "uploaded": True,
                "remote_account_id": 654,
                "probe_source": "api",
            }
        }
        account.set_extra(extra)

        with mock.patch(
            "services.sub2api_sync.upload_to_sub2api_detailed",
            return_value={"ok": False, "message": "temporary failure"},
        ):
            result = backfill_chatgpt_account_to_sub2api(account, commit=False)

        self.assertFalse(result["ok"])
        state = account.get_extra()["sync_statuses"]["sub2api"]
        self.assertEqual(state["remote_state"], "exists")
        self.assertTrue(state["uploaded"])
        self.assertEqual(state["remote_account_id"], 654)
        self.assertEqual(state["last_upload"]["status"], "failed")

    def test_backfill_blocks_registered_auth_pending_before_low_level_upload(self):
        account = AccountModel(
            platform="chatgpt",
            email="pending@example.com",
            password="secret",
            token="",
            status="pending_payment",
        )
        account.set_extra({"registered_auth_pending": True})

        with mock.patch(
            "services.sub2api_sync.upload_to_sub2api_detailed",
            side_effect=AssertionError("upload must not run without access_token"),
        ) as upload_mock:
            result = backfill_chatgpt_account_to_sub2api(account, commit=False)

        self.assertFalse(result["ok"])
        self.assertTrue(result["skipped"])
        self.assertEqual(result["capabilities"]["upload_gate"], "blocked_missing_at")
        upload_mock.assert_not_called()
        state = account.get_extra()["sync_statuses"]["sub2api"]
        self.assertEqual(state["upload_gate"], "blocked_missing_at")
        self.assertEqual(state["last_upload"]["status"], "skipped")

    def test_direct_upload_rejects_missing_at_or_rt_without_network(self):
        cases = (
            ({"registered_auth_pending": True}, "blocked_missing_at"),
            ({"access_token": "at-only"}, "blocked_missing_rt"),
        )
        for extra, expected_gate in cases:
            with self.subTest(expected_gate=expected_gate):
                account = AccountModel(
                    platform="chatgpt",
                    email="incomplete@example.com",
                    password="secret",
                    token="",
                    status="pending_payment",
                )
                account.set_extra(extra)
                with mock.patch("services.chatgpt_core.sub2api_upload.cffi_requests.post") as post:
                    result = upload_to_sub2api_detailed(
                        account,
                        api_url="https://sub2api.example",
                        api_key="upload-key",
                        group_ids=[2],
                    )

                self.assertFalse(result["ok"])
                self.assertTrue(result["skipped"])
                self.assertEqual(result["upload_gate"], expected_gate)
                post.assert_not_called()

    def test_payload_uses_saved_subscription_without_local_probe(self):
        account = self._make_account()
        extra = account.get_extra()
        extra["chatgpt_local"] = {
            "subscription": {
                "plan": "plus",
                "subscription_active_until": "2030-01-02T03:04:05Z",
            }
        }
        account.set_extra(extra)

        with mock.patch(
            "services.chatgpt_core.status_probe.probe_local_chatgpt_status",
            side_effect=AssertionError("payload construction must not probe"),
        ):
            payload = build_sub2api_account_payload(account, group_ids=[7])

        self.assertEqual(payload["group_ids"], [7])
        self.assertEqual(payload["credentials"]["plan_type"], "plus")
        self.assertEqual(payload["credentials"]["subscription_expires_at"], "2030-01-02T03:04:05Z")


if __name__ == "__main__":
    unittest.main()
