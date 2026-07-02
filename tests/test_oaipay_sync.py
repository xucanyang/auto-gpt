import unittest
from unittest import mock

from core.db import AccountModel
from services.oaipay_sync import backfill_chatgpt_account_to_oaipay, probe_chatgpt_oaipay_status


class OaiPaySyncTests(unittest.TestCase):
    def _make_account(self) -> AccountModel:
        account = AccountModel(
            platform="chatgpt",
            email="demo_oai@example.com",
            password="secret_password",
            token="",
            status="registered",
        )
        account.set_extra(
            {
                "access_token": "at-oai-demo",
                "refresh_token": "rt-oai-demo",
                "workspace_id": "ws-oai-demo",
            }
        )
        account.user_id = "acct-oai-demo"
        return account

    def test_probe_reports_existing_remote_account(self):
        account = self._make_account()
        rows = [
            {
                "id": 101,
                "name": "demo_oai@example.com",
                "status": "active",
                "credentials": {},
                "extra": {"email": "demo_oai@example.com"},
            }
        ]
        with mock.patch("services.oaipay_sync._fetch_oaipay_account_items", return_value=rows):
            result = probe_chatgpt_oaipay_status(account)

        self.assertEqual(result["remote_state"], "exists")
        self.assertEqual(result["remote_account_id"], 101)

    def test_backfill_does_not_abort_when_probe_is_unreachable(self):
        account = self._make_account()
        probe_res = {
            "remote_state": "unreachable",
            "uploaded": False,
            "message": "OAIPay API 探测不可连接，继续尝试直接上传",
        }
        with mock.patch("services.oaipay_sync.probe_chatgpt_oaipay_status", return_value=probe_res):
            with mock.patch("services.oaipay_sync.probe_local_chatgpt_status", return_value={"ok": True}):
                with mock.patch(
                    "services.oaipay_sync.upload_to_oaipay_detailed",
                    return_value={"ok": True, "message": "上传成功，导入 1 个账号", "remote_status": "uploaded"},
                ) as mock_upload:
                    outcome = backfill_chatgpt_account_to_oaipay(account, commit=False)

        self.assertTrue(outcome["ok"])
        self.assertTrue(outcome["uploaded"])
        mock_upload.assert_called_once()
