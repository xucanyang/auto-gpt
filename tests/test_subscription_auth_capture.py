import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sqlmodel import Session, SQLModel, create_engine

from core import db as core_db
from core.db import AccountModel
from services.chatgpt_core import subscription_auth_capture


class SubscriptionAuthCaptureTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "subscription_auth_capture.db"
        self.engine = create_engine(f"sqlite:///{db_path}")
        self.core_engine_patch = mock.patch.object(core_db, "engine", self.engine)
        self.core_engine_patch.start()
        SQLModel.metadata.create_all(self.engine)
        core_db.init_db()

    def tearDown(self):
        self.core_engine_patch.stop()
        self._tmpdir.cleanup()

    def _add_account(self, *, status: str = "subscribed") -> int:
        with Session(self.engine) as session:
            row = AccountModel(
                platform="chatgpt",
                email="capture@example.com",
                password="pw",
                token="old-token",
                status=status,
                extra_json='{"chatgpt_mailbox_state": {"provider": "dummy", "email": "capture@example.com"}}',
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return int(row.id or 0)

    def _patch_capture_runtime(self, *, tokens=None, error=""):
        login_calls: list[dict] = []

        class _FakeEmailService:
            service_type = type("ST", (), {"value": "dummy"})()

            def __init__(self, *, state, log_fn=None):
                self.state = dict(state or {})

            def export_state(self):
                return {**self.state, "provider": "dummy", "email": "capture@example.com"}

        class _FakeRegisterClient:
            device_id = "dev-1"
            ua = "ua"
            sec_ch_ua = "sec"
            impersonate = "chrome"
            fingerprint = None

        class _FakeOAuthClient:
            last_error = error
            last_workspace_id = "ws-new"

            def login_and_get_tokens(self, *_args, **kwargs):
                login_calls.append(kwargs)
                return tokens

            def _decode_oauth_session_cookie(self):
                return {}

            def _get_cookie_value(self, *_args, **_kwargs):
                return "session-new"

        class _FakeEngine:
            def __init__(self, **_kwargs):
                self.email = ""
                self.password = ""

            def _create_email(self):
                return True

            def _build_chatgpt_client(self):
                return _FakeRegisterClient()

            def _build_oauth_client(self):
                return _FakeOAuthClient()

            def _populate_result_from_tokens(self, *, result, tokens, oauth_client, registration_message, source, register_client):
                result.success = True
                result.email = self.email
                result.password = self.password
                result.access_token = tokens.get("access_token", "")
                result.refresh_token = tokens.get("refresh_token", "")
                result.id_token = tokens.get("id_token", "")
                result.session_token = "session-new"
                result.account_id = "acct-new"
                result.workspace_id = "ws-new"
                result.source = source
                if not result.refresh_token:
                    result.success = False
                    result.error_message = "OAuth 登录成功但未获取 refresh_token"

            def _build_workspace_artifact(self, *, tokens, oauth_client, source, scope_hint=""):
                return {
                    "scope": scope_hint or "free",
                    "label": scope_hint or "free",
                    "account_id": "acct-new",
                    "workspace_id": "ws-new",
                    "access_token": tokens.get("access_token", ""),
                    "refresh_token": tokens.get("refresh_token", ""),
                    "id_token": tokens.get("id_token", ""),
                    "session_token": "session-new",
                    "source": source,
                    "variant_key": f"{scope_hint or 'free'}:ws-new",
                }

            def _artifact_has_refresh_token(self, artifact):
                return bool(artifact.get("refresh_token"))

            def _apply_workspace_artifact_to_result(self, result, artifact):
                result.success = True
                result.access_token = artifact.get("access_token", "")
                result.refresh_token = artifact.get("refresh_token", "")
                result.id_token = artifact.get("id_token", "")
                result.session_token = artifact.get("session_token", "")
                result.account_id = artifact.get("account_id", "")
                result.workspace_id = artifact.get("workspace_id", "")
                result.source = artifact.get("source", "")

        return (
            login_calls,
            mock.patch.object(subscription_auth_capture, "RestoredEmailService", _FakeEmailService),
            mock.patch.object(subscription_auth_capture, "RefreshTokenRegistrationEngine", _FakeEngine),
            mock.patch.object(subscription_auth_capture.config_store, "get_all", return_value={}),
        )

    def test_capture_persists_tokens_and_passes_manual_phone_flag(self):
        account_id = self._add_account(status="subscribed")
        tokens = {"access_token": "at-new", "refresh_token": "rt-new", "id_token": "id-new"}
        login_calls, email_patch, engine_patch, config_patch = self._patch_capture_runtime(tokens=tokens)

        with email_patch, engine_patch, config_patch:
            result = subscription_auth_capture.capture_subscription_auth_for_account(
                account_id,
                allow_phone_verification=True,
                retry_delays_seconds=[],
            )

        self.assertTrue(result["ok"])
        self.assertEqual(len(login_calls), 1)
        self.assertTrue(login_calls[0]["allow_phone_verification"])
        self.assertFalse(login_calls[0]["allow_add_phone_session_recovery"])
        with Session(self.engine) as session:
            account = session.get(AccountModel, account_id)
            extra = account.get_extra()
        self.assertEqual(account.status, "subscribed")
        self.assertEqual(account.token, "at-new")
        self.assertEqual(extra["refresh_token"], "rt-new")
        self.assertEqual(extra["workspace_id"], "ws-new")
        self.assertEqual(extra["chatgpt_last_auth_capture"]["source"], "subscription_auth_capture_free")

    def test_add_phone_without_phone_verification_retries_short_path(self):
        account_id = self._add_account(status="pending_payment")
        login_calls, email_patch, engine_patch, config_patch = self._patch_capture_runtime(
            tokens=None,
            error="passwordless 登录后仍停留在 add_phone，未获取到 workspace / callback",
        )

        with email_patch, engine_patch, config_patch, mock.patch.object(subscription_auth_capture.time, "sleep") as sleep_mock:
            result = subscription_auth_capture.capture_subscription_auth_for_account(
                account_id,
                allow_phone_verification=False,
                retry_delays_seconds=[0],
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["data"]["error_code"], "add_phone_required")
        self.assertEqual(len(login_calls), 2)
        sleep_mock.assert_not_called()
        self.assertTrue(login_calls[0]["allow_phone_verification"])
        self.assertFalse(login_calls[0]["allow_add_phone_verification"])
        self.assertTrue(login_calls[0]["allow_existing_phone_verification"])
        self.assertFalse(login_calls[0]["allow_add_phone_session_recovery"])

    def test_explicit_empty_retry_delays_disable_capture_retries(self):
        account_id = self._add_account(status="pending_payment")
        login_calls, email_patch, engine_patch, config_patch = self._patch_capture_runtime(
            tokens=None,
            error="passwordless 登录后仍停留在 add_phone，未获取到 workspace / callback",
        )

        with email_patch, engine_patch, config_patch, mock.patch.object(subscription_auth_capture.time, "sleep") as sleep_mock:
            result = subscription_auth_capture.capture_subscription_auth_for_account(
                account_id,
                allow_phone_verification=False,
                retry_delays_seconds=[],
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["data"]["error_code"], "add_phone_required")
        self.assertEqual(len(login_calls), 1)
        self.assertEqual(result["data"]["auth_capture"]["attempts"], 1)
        sleep_mock.assert_not_called()

    def test_no_valid_organizations_is_retryable_workspace_pending(self):
        account_id = self._add_account(status="pending_payment")
        login_calls, email_patch, engine_patch, config_patch = self._patch_capture_runtime(
            tokens=None,
            error=(
                "workspace/select 失败: HTTP 400, code=no_valid_organizations, "
                "message=You do not have any valid organizations."
            ),
        )

        with email_patch, engine_patch, config_patch, mock.patch.object(subscription_auth_capture.time, "sleep") as sleep_mock:
            result = subscription_auth_capture.capture_subscription_auth_for_account(
                account_id,
                allow_phone_verification=True,
                retry_delays_seconds=[0],
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["data"]["error_code"], "workspace_org_not_ready")
        self.assertTrue(result["data"]["retryable"])
        self.assertEqual(len(login_calls), 2)
        sleep_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
