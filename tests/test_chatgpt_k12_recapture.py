import json
import unittest

from core.db import AccountModel
from services.chatgpt_core.k12_recapture import (
    _find_matching_artifact,
    _merge_account_extra_for_artifact,
    _workspace_variants_from_artifacts,
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


if __name__ == "__main__":
    unittest.main()
