import unittest
from unittest import mock

from core.db import AccountModel
from services.oaipay_sync import backfill_chatgpt_account_to_oaipay, probe_chatgpt_oaipay_status
from services.chatgpt_core.oaipay_upload import upload_to_oaipay_detailed
from services.chatgpt_core import oaipay_upload as oaipay_upload_module


class FakeOaiPayResponse:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


class OaiPaySyncTests(unittest.TestCase):
    def setUp(self):
        oaipay_upload_module._CATEGORIES_CACHE = {}
        oaipay_upload_module._CATEGORIES_ID_TO_NAME_CACHE = {}
        oaipay_upload_module._CATEGORIES_CACHE_TIME = 0
        oaipay_upload_module._CATEGORIES_CACHE_KEY = ""

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

    def test_upload_auto_category_records_resolved_category(self):
        account = self._make_account()
        categories_response = FakeOaiPayResponse(
            200,
            [
                {"id": 1, "name": "PLUS--未接码"},
                {"id": 2, "name": "PLUS--已接美国长效"},
            ],
        )
        upload_response = FakeOaiPayResponse(200, {"success": True, "imported": 1, "category_id": 2, "group": "2"})
        with mock.patch("services.chatgpt_core.oaipay_upload.cffi_requests.get", return_value=categories_response):
            with mock.patch("services.chatgpt_core.oaipay_upload.cffi_requests.post", return_value=upload_response) as mock_post:
                result = upload_to_oaipay_detailed(
                    account,
                    api_url="https://gpt.cccy.me",
                    api_key="upload-key",
                    capabilities={"has_refresh_token": True, "has_paid_subscription": True},
                )

        self.assertTrue(result["ok"])
        self.assertEqual(result["category_source"], "auto")
        self.assertEqual(result["category_rule"], "paid_with_refresh_token")
        self.assertEqual(result["category_id"], 2)
        self.assertEqual(result["category_name"], "PLUS--已接美国长效")
        self.assertEqual(mock_post.call_args.kwargs["json"]["group"], "2")

    def test_upload_manual_category_overrides_auto_category(self):
        account = self._make_account()
        categories_response = FakeOaiPayResponse(
            200,
            [
                {"id": 1, "name": "PLUS--未接码"},
                {"id": 2, "name": "PLUS--已接美国长效"},
            ],
        )
        upload_response = FakeOaiPayResponse(200, {"success": True, "imported": 1, "category_id": 1, "group": "1"})
        with mock.patch("services.chatgpt_core.oaipay_upload.cffi_requests.get", return_value=categories_response):
            with mock.patch("services.chatgpt_core.oaipay_upload.cffi_requests.post", return_value=upload_response) as mock_post:
                result = upload_to_oaipay_detailed(
                    account,
                    api_url="https://gpt.cccy.me",
                    api_key="upload-key",
                    group_ids=[1],
                    category_mode="manual",
                    capabilities={"has_refresh_token": True, "has_paid_subscription": True},
                )

        self.assertTrue(result["ok"])
        self.assertEqual(result["category_source"], "manual")
        self.assertEqual(result["category_id"], 1)
        self.assertEqual(result["category_name"], "PLUS--未接码")
        self.assertEqual(mock_post.call_args.kwargs["json"]["group"], "1")

    def test_upload_401_surfaces_server_detail(self):
        account = self._make_account()
        response = FakeOaiPayResponse(401, {"detail": "上传密钥无效"}, '{"detail":"上传密钥无效"}')
        with mock.patch("services.chatgpt_core.oaipay_upload.cffi_requests.post", return_value=response):
            result = upload_to_oaipay_detailed(
                account,
                api_url="https://gpt.cccy.me",
                api_key="old-admin-password",
                group_ids=[2],
                capabilities={"has_refresh_token": False, "has_paid_subscription": False},
            )

        self.assertFalse(result["ok"])
        self.assertIn("HTTP 401", result["message"])
        self.assertIn("上传密钥无效", result["message"])

    def test_probe_401_surfaces_server_detail(self):
        response = FakeOaiPayResponse(401, {"detail": "上传密钥无效"}, '{"detail":"上传密钥无效"}')

        def fake_config(key: str, default: str = "") -> str:
            values = {
                "oaipay_api_url": "https://gpt.cccy.me",
                "oaipay_api_key": "old-admin-password",
                "oaipay_probe_timeout_seconds": "1",
                "oaipay_probe_api_page_size": "10",
            }
            return values.get(key, default)

        account = self._make_account()
        with mock.patch("services.oaipay_sync._get_config_value", side_effect=fake_config):
            with mock.patch("services.oaipay_sync.cffi_requests.get", return_value=response):
                result = probe_chatgpt_oaipay_status(account)

        self.assertEqual(result["remote_state"], "unreachable")
        self.assertIn("HTTP 401", result["message"])
        self.assertIn("上传密钥无效", result["message"])
