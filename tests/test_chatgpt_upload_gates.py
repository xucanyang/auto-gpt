import unittest
from unittest import mock

from core.db import AccountModel


class ChatGPTUploadGateApiTests(unittest.TestCase):
    @staticmethod
    def _pending_account() -> AccountModel:
        account = AccountModel(
            platform="chatgpt",
            email="pending_api@example.com",
            password="secret",
            token="",
            status="pending_payment",
        )
        account.set_extra({"registered_auth_pending": True})
        return account

    def test_direct_cpa_endpoint_blocks_before_token_conversion_or_network(self):
        from api import chatgpt as chatgpt_api

        account = self._pending_account()
        with mock.patch.object(chatgpt_api, "_get_account", return_value=account):
            with mock.patch.object(
                chatgpt_api,
                "_to_codex_account",
                side_effect=AssertionError("CPA conversion/network path must not run"),
            ) as convert:
                result = chatgpt_api.upload_cpa(
                    1,
                    chatgpt_api.CpaUploadReq(api_url="https://cpa.example", api_key="upload-key"),
                    session=mock.Mock(),
                )

        self.assertFalse(result["ok"])
        self.assertTrue(result["skipped"])
        self.assertEqual(result["upload_gate"], "blocked_missing_at")
        convert.assert_not_called()

    def test_sub2api_action_returns_backfill_result_without_plugin_second_upload(self):
        from api import actions as actions_api

        account = self._pending_account()
        instance = mock.Mock()
        outcome = {
            "ok": False,
            "skipped": True,
            "message": "跳过上传：缺少 access_token，认证材料尚未就绪",
            "results": [{"name": "Sub2API 上传", "ok": False}],
        }
        with mock.patch.object(
            actions_api,
            "backfill_chatgpt_account_to_sub2api",
            return_value=outcome,
        ) as backfill:
            with mock.patch.object(actions_api, "_apply_action_result") as apply_result:
                result = actions_api._execute_platform_action(
                    instance,
                    "chatgpt",
                    account,
                    "upload_sub2api",
                    {},
                    mock.Mock(),
                )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], outcome["message"])
        backfill.assert_called_once()
        apply_result.assert_called_once()
        instance.execute_action.assert_not_called()


if __name__ == "__main__":
    unittest.main()
