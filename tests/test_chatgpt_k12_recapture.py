import json
import unittest
from unittest.mock import MagicMock, patch

from core.base_platform import RegisterConfig
from core.db import AccountModel
from services.chatgpt_core import ChatGPTPlatform
from services.chatgpt_core.k12_recapture import (
    _find_matching_artifact,
    _merge_account_extra_for_artifact,
    _workspace_variants_from_artifacts,
    recapture_saved_account_k12_workspaces,
    safe_artifact_summary,
)


class K12RecapturePersistenceTests(unittest.TestCase):
    def _account(self):
        extra = {
            "access_token": "old-at",
            "refresh_token": "keep-rt",
            "session_token": "old-session",
            "cookies": "old_cookie=1",
            "workspace_id": "acct-free",
            "account_id": "acct-free",
            "chatgpt_workspace_scope": "free",
            "chatgpt_workspace_variant_key": "free:acct-free",
            "chatgpt_registration_context": {"device_id": "device-1"},
        }
        return AccountModel(
            id=10,
            platform="chatgpt",
            email="k12@example.com",
            password="pw",
            user_id="acct-free",
            token="old-at",
            status="registered",
            extra_json=json.dumps(extra, ensure_ascii=False),
        )

    def test_safe_artifact_summary_never_returns_secret_material(self):
        artifact = {
            "scope": "k12",
            "label": "k12",
            "workspace_id": "ws-k12",
            "account_id": "acct-k12",
            "access_token": "SECRET_AT",
            "session_token": "SECRET_SESSION",
            "cookies": "SECRET_COOKIE=1",
            "refresh_token": "SECRET_RT",
            "source": "k12_workspace_join",
            "auth_level": "access_token_only",
            "partial_auth": True,
            "space": {"name": "School Lab", "workspace_id": "ws-k12"},
        }
        payload = safe_artifact_summary(artifact)
        raw = json.dumps(payload, ensure_ascii=False)
        self.assertTrue(payload["has_access_token"])
        self.assertTrue(payload["has_session_token"])
        self.assertTrue(payload["has_cookies"])
        self.assertNotIn("SECRET_AT", raw)
        self.assertNotIn("SECRET_SESSION", raw)
        self.assertNotIn("SECRET_COOKIE", raw)
        self.assertNotIn("SECRET_RT", raw)

    def test_current_account_merge_preserves_refresh_token_while_updating_saved_web_session(self):
        account = self._account()
        source_extra = account.get_extra()
        artifacts = [
            {
                "scope": "free",
                "label": "free",
                "workspace_id": "acct-free",
                "account_id": "acct-free",
                "variant_key": "free:acct-free",
                "access_token": "new-at",
                "refresh_token": "",
                "session_token": "new-session",
                "cookies": "new_cookie=1",
                "source": "all_spaces_capture",
                "auth_level": "access_token_only",
                "partial_auth": True,
            },
            {
                "scope": "k12",
                "label": "k12",
                "workspace_id": "ws-k12",
                "account_id": "ws-k12",
                "variant_key": "k12:ws-k12",
                "access_token": "k12-at",
                "session_token": "new-session",
                "cookies": "new_cookie=1",
                "source": "k12_workspace_join",
                "auth_level": "access_token_only",
                "partial_auth": True,
            },
        ]
        matched = _find_matching_artifact(account, source_extra, artifacts)
        variants = _workspace_variants_from_artifacts(artifacts)
        next_extra = _merge_account_extra_for_artifact(
            account,
            matched,
            source_extra=source_extra,
            captured_at="2026-07-06T00:00:00Z",
            workspace_variants=variants,
            capture={"summary": {"saved_spaces": 2}, "join_results": [], "spaces": []},
            request_summary={"target_workspace_ids": ["ws-k12"], "save_all_spaces": True, "strict_join": False},
        )
        self.assertEqual(account.token, "new-at")
        self.assertEqual(next_extra["access_token"], "new-at")
        self.assertEqual(next_extra["refresh_token"], "keep-rt")
        self.assertEqual(next_extra["session_token"], "new-session")
        self.assertEqual(next_extra["cookies"], "new_cookie=1")
        self.assertEqual(len(next_extra["chatgpt_workspace_variants"]), 2)
        self.assertEqual(next_extra["chatgpt_k12_manual_recapture"]["summary"]["saved_spaces"], 2)

    def test_platform_actions_exposes_k12_workspace_recapture_for_action_column(self):
        actions = ChatGPTPlatform(RegisterConfig()).get_platform_actions()
        matched = [item for item in actions if item.get("id") == "k12_workspace_recapture"]
        self.assertEqual(len(matched), 1)
        self.assertTrue(matched[0].get("params"))

    def test_action_wrapper_returns_changed_ids_for_batch_refresh(self):
        from api import actions as actions_api

        account = self._account()
        mocked_result = {
            "ok": True,
            "summary": {"saved_spaces": 2},
            "artifacts": [{"workspace_id": "ws-k12"}],
            "saved_accounts": [{"id": 11}],
            "changed_account_ids": [10, 11],
            "logs": [],
        }
        with patch(
            "services.chatgpt_core.k12_recapture.recapture_saved_account_k12_workspaces",
            return_value=mocked_result,
        ) as mocked_recapture:
            result = actions_api._execute_chatgpt_k12_workspace_recapture_action(
                account,
                MagicMock(),
                {"proxy_mode": "direct", "workspace_ids": "ws-k12"},
            )
        self.assertTrue(result["ok"])
        self.assertIn("K12 / Workspace 重跑完成", result["data"]["message"])
        self.assertEqual(result["data"]["changed_account_ids"], [10, 11])
        mocked_recapture.assert_called_once()

        refresh_ids = actions_api._action_local_status_refresh_ids(
            "k12_workspace_recapture",
            result,
            account,
        )
        self.assertEqual(refresh_ids, [10, 11])

    def test_recapture_without_exported_artifact_is_failure_even_when_join_ran(self):
        account = self._account()

        class _Cookies:
            def set(self, *args, **kwargs):
                return None

        class _Client:
            def __init__(self):
                self.session = MagicMock()
                self.session.cookies = _Cookies()

            def close(self):
                return None

        mocked_capture = {
            "artifacts": [],
            "spaces": [{"workspace_id": "ws-k12", "account_id": "ws-k12", "scope": "k12"}],
            "join_results": [{"workspace_id": "ws-k12", "ok": True}],
            "summary": {
                "enabled": True,
                "targets": 1,
                "joined": 1,
                "failed": 0,
                "saved_spaces": 0,
                "strict_join_failed": False,
            },
            "exchange_failures": [{"workspace_id": "ws-k12", "message": "exchange failed"}],
        }
        with patch("services.chatgpt_core.k12_recapture.ChatGPTClient", return_value=_Client()), patch(
            "services.chatgpt_core.k12_recapture.capture_k12_and_all_spaces",
            return_value=mocked_capture,
        ):
            result = recapture_saved_account_k12_workspaces(
                session=MagicMock(),
                account=account,
                config={"chatgpt_k12_workspace_ids": "ws-k12"},
                workspace_ids="ws-k12",
                save_all_spaces=True,
                strict_join=False,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["summary"]["saved_spaces"], 0)
        self.assertIn("未导出任何可写回", result["summary"]["error"])

    def test_enqueue_k12_recapture_task_returns_task_id_and_background_work(self):
        from api import tasks as tasks_api

        account = self._account()

        class _Session:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def get(self, model, account_id):
                return account if int(account_id) == int(account.id) else None

        background_tasks = MagicMock()
        with patch("api.tasks.Session", return_value=_Session()), patch("api.tasks._create_standalone_task_record") as create_record, patch(
            "api.tasks._save_task_log"
        ) as save_log:
            task_id = tasks_api.enqueue_k12_workspace_recapture_task(
                tasks_api.K12WorkspaceRecaptureTaskRequest(account_id=int(account.id), workspace_ids="ws-k12"),
                background_tasks=background_tasks,
            )

        self.assertTrue(task_id.startswith("task_"))
        create_record.assert_called_once()
        save_log.assert_called_once()
        background_tasks.add_task.assert_called_once()


if __name__ == "__main__":
    unittest.main()
