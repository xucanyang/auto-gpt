import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sqlmodel import Session, SQLModel, create_engine

from core.base_mailbox import ManualEmailOtpMailbox
from core import db as core_db
from core.db import AccountModel
from services.chatgpt_core import custom_email_recheck


class _FakeEngineInstance:
    def _extract_workspace_id(self, _oauth_client):
        return "ws-new"

    def _extract_session_token(self, _oauth_client):
        return "session-new"


class _DummyResolvedEmailService:
    service_type = type("ST", (), {"value": "dummy"})()

    def __init__(self, state=None):
        self._state = dict(state or {})

    def create_email(self, config=None):
        return {"email": self._state.get("email", "alive@example.com")}

    def export_state(self):
        return dict(self._state or {})

    def finalize_success(self, account_email: str = "", task_id: str = ""):
        return None

    def finalize_failure(self, error_message: str = "", task_id: str = ""):
        return None


class CustomEmailRecheckPersistenceTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "custom_email_recheck.db"
        self.engine = create_engine(f"sqlite:///{db_path}")
        self.core_engine_patch = mock.patch.object(core_db, "engine", self.engine)
        self.custom_engine_patch = mock.patch.object(custom_email_recheck, "engine", self.engine)
        self.core_engine_patch.start()
        self.custom_engine_patch.start()
        SQLModel.metadata.create_all(self.engine)
        core_db.init_db()

    def tearDown(self):
        self.custom_engine_patch.stop()
        self.core_engine_patch.stop()
        self._tmpdir.cleanup()

    def _add_account(self, *, email: str, status: str = "invalid") -> int:
        with Session(self.engine) as session:
            row = AccountModel(
                platform="chatgpt",
                email=email,
                password="pw-old",
                token="old-token",
                status=status,
                extra_json='{"chatgpt_mailbox_state":{"provider":"dummy","email":"%s"},"refresh_token":"old-rt"}' % email,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return int(row.id or 0)

    def test_upsert_updates_existing_account_when_original_row_still_exists(self):
        account_id = self._add_account(email="alive@example.com", status="invalid")

        saved, revived_existing = custom_email_recheck._upsert_custom_email_recheck_account(
            email="alive@example.com",
            password="pw-new",
            tokens={
                "access_token": "at-new",
                "refresh_token": "rt-new",
                "id_token": "id-new",
            },
            oauth_client=object(),
            engine_instance=_FakeEngineInstance(),
            task_id="task-custom",
            recheck_payload={"account_id": "acct-new", "status": "login_alive"},
            mailbox_state={"provider": "dummy", "email": "alive@example.com"},
            preferred_account_id=account_id,
        )

        self.assertTrue(revived_existing)
        self.assertEqual(int(saved.id or 0), account_id)
        with Session(self.engine) as session:
            row = session.get(AccountModel, account_id)
            extra = row.get_extra()
        self.assertEqual(row.token, "at-new")
        self.assertEqual(row.password, "pw-new")
        self.assertNotEqual(row.status, "invalid")
        self.assertEqual(extra["refresh_token"], "rt-new")
        self.assertEqual(extra["chatgpt_capabilities"]["auth_level"], "refresh_token")
        self.assertEqual(extra["chatgpt_last_revival"]["mode"], "revive_existing")
        self.assertEqual(extra["chatgpt_custom_email_recheck"]["revival_marker"]["mode"], "revive_existing")

    def test_upsert_creates_new_account_when_original_row_missing(self):
        saved, revived_existing = custom_email_recheck._upsert_custom_email_recheck_account(
            email="fresh@example.com",
            password="pw-new",
            tokens={
                "access_token": "at-new",
            },
            oauth_client=object(),
            engine_instance=_FakeEngineInstance(),
            task_id="task-custom",
            recheck_payload={"account_id": "acct-new", "status": "login_alive"},
            mailbox_state={"provider": "dummy", "email": "fresh@example.com"},
            preferred_account_id=0,
        )

        self.assertFalse(revived_existing)
        saved_id = int(saved.id or 0)
        self.assertGreater(saved_id, 0)
        with Session(self.engine) as session:
            row = session.get(AccountModel, saved_id)
            extra = row.get_extra()
        self.assertEqual(row.email, "fresh@example.com")
        self.assertEqual(extra["chatgpt_capabilities"]["auth_level"], "access_token_only")
        self.assertEqual(extra["chatgpt_last_revival"]["mode"], "create_new")
        self.assertEqual(extra["chatgpt_custom_email_recheck"]["revival_marker"]["mode"], "create_new")

    def test_custom_email_recheck_keeps_stage1_result_when_followup_auth_fails(self):
        account_id = self._add_account(email="alive@example.com", status="invalid")

        class _FakeRegisterClient:
            device_id = "dev-1"
            ua = "ua"
            sec_ch_ua = "sec"
            impersonate = "chrome"
            fingerprint = None

        class _FakeOAuthClient:
            last_error = "workspace/select failed"

            def login_and_get_tokens(self, *_args, **_kwargs):
                return None

        class _FakeFollowupEngine:
            def __init__(self, **_kwargs):
                self.email = ""
                self.password = ""

            def _build_chatgpt_client(self):
                return _FakeRegisterClient()

            def _build_oauth_client(self):
                return _FakeOAuthClient()

            def _extract_account_info(self, _tokens):
                return {"account_id": "acct-stage1"}

            def _extract_workspace_id(self, _oauth_client):
                return ""

            def _extract_session_token(self, _oauth_client):
                return ""

        resolved_state = {"provider": "dummy", "email": "alive@example.com"}
        with (
            mock.patch.object(custom_email_recheck.config_store, "get_all", return_value={}),
            mock.patch.object(
                custom_email_recheck,
                "_resolve_custom_email_service",
                return_value=(_DummyResolvedEmailService(resolved_state), resolved_state),
            ),
            mock.patch.object(
                custom_email_recheck,
                "_capture_access_token_without_refresh_token",
                return_value=(
                    {
                        "access_token": "at-stage1",
                        "session_token": "session-stage1",
                        "account_id": "acct-stage1",
                        "workspace_id": "ws-stage1",
                    },
                    resolved_state,
                    _FakeEngineInstance(),
                ),
            ),
            mock.patch.object(custom_email_recheck, "RefreshTokenRegistrationEngine", _FakeFollowupEngine),
        ):
            result = custom_email_recheck.recheck_custom_chatgpt_email(
                email="alive@example.com",
                password="pw-new",
                save_on_success=True,
                preferred_account_id=account_id,
                task_id="task-custom-stage1",
            )

        self.assertTrue(result["ok"])
        payload = result["data"]["custom_email_recheck"]
        self.assertFalse(payload["followup_auth_ok"])
        self.assertIn("完整 Auth 未补全", payload["message"])
        joined_logs = "\n".join(result["data"]["logs"])
        self.assertIn("[邮箱测活] 开始处理：alive@example.com", joined_logs)
        with Session(self.engine) as session:
            row = session.get(AccountModel, account_id)
            extra = row.get_extra()
        self.assertEqual(row.token, "at-stage1")
        self.assertNotIn("refresh_token", extra)
        self.assertEqual(extra["chatgpt_capabilities"]["auth_level"], "access_token_only")
        self.assertEqual(extra["chatgpt_custom_email_recheck"]["followup_auth_ok"], False)
        self.assertEqual(extra["chatgpt_last_revival"]["mode"], "revive_existing")

    def test_resolve_custom_email_service_prefers_hme_ready_over_restored_manual_email_otp(self):
        with Session(self.engine) as session:
            row = AccountModel(
                platform="chatgpt",
                email="hme@example.com",
                password="pw",
                token="at",
                status="access_token_only",
                extra_json='{"chatgpt_mailbox_state":{"provider":"manual_email_otp","email":"hme@example.com","account":{"email":"hme@example.com","account_id":"hme@example.com","extra":{}},"before_ids":[],"config":{"tempmail_api_url":"http://tempmail","tempmail_api_key":"tm-key"}}}',
            )
            session.add(row)
            session.commit()

        captured: dict[str, object] = {}

        class _CaptureService:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        with (
            mock.patch.object(
                custom_email_recheck,
                "_mailbox_state_from_icloud_hme_alias",
                return_value={"provider": "hme_ready_api", "email": "hme@example.com", "account": {"email": "hme@example.com", "account_id": "alias-1", "extra": {"source": "legacy-icloud-hme"}}},
            ),
            mock.patch.object(custom_email_recheck, "_mailbox_state_from_applemail_pool", return_value={}),
            mock.patch.object(custom_email_recheck, "_mailbox_state_from_tempmail_domain_match", return_value={}),
            mock.patch.object(custom_email_recheck, "RestoredEmailService", _CaptureService),
        ):
            service, state = custom_email_recheck._resolve_custom_email_service(
                email="hme@example.com",
                merged_config={},
                proxy_url=None,
                preferred_account_id=0,
                task_control="task-control",
                attempt_id=88,
                log_fn=lambda *_args: None,
            )

        self.assertIsInstance(service, _CaptureService)
        self.assertEqual(state["provider"], "hme_ready_api")
        self.assertEqual(captured["state"]["provider"], "hme_ready_api")
        self.assertEqual(captured["task_control"], "task-control")
        self.assertEqual(captured["attempt_id"], 88)

    def test_manual_email_otp_skips_tempmail_probe_for_unmanaged_domain(self):
        mailbox = ManualEmailOtpMailbox(
            email="user@icloud.com",
            extra={
                "tempmail_api_url": "http://tempmail",
                "tempmail_api_key": "tm-key",
                "tempmail_primary_domain": "edu.666800.xyz",
            },
        )

        class _TempMailMailbox:
            def _headers(self):
                return {}

            def _request(self, *_args, **_kwargs):
                response = mock.Mock()
                response.status_code = 200
                response.json.return_value = {
                    "domains": [{"domain": "edu.666800.xyz", "is_active": True}]
                }
                return response

            def ensure_mailbox_by_email(self, _email):
                raise AssertionError("non-managed domain should not trigger TempMail ensure")

        mailbox._build_tempmail_mailbox = lambda: _TempMailMailbox()
        account = mailbox.get_email()

        self.assertEqual(account.email, "user@icloud.com")
        self.assertEqual(account.account_id, "user@icloud.com")

    def test_custom_email_recheck_stage1_honors_existing_phone_verification_flag(self):
        account_id = self._add_account(email="alive2@example.com", status="invalid")

        captured: dict[str, object] = {}

        def _capture_stage1(**kwargs):
            captured.update(kwargs)
            return (
                {
                    "access_token": "at-stage1",
                    "session_token": "session-stage1",
                    "account_id": "acct-stage1",
                    "workspace_id": "ws-stage1",
                },
                {"provider": "dummy", "email": "alive2@example.com"},
                _FakeEngineInstance(),
            )

        class _FakeOAuthClient:
            last_error = "workspace/select failed"

            def login_and_get_tokens(self, *_args, **_kwargs):
                return None

        class _FakeFollowupEngine:
            def __init__(self, **_kwargs):
                self.email = ""
                self.password = ""

            def _build_chatgpt_client(self):
                return mock.Mock(device_id="dev-1", ua="ua", sec_ch_ua="sec", impersonate="chrome", fingerprint=None)

            def _build_oauth_client(self):
                return _FakeOAuthClient()

            def _extract_account_info(self, _tokens):
                return {"account_id": "acct-stage1"}

            def _extract_workspace_id(self, _oauth_client):
                return ""

            def _extract_session_token(self, _oauth_client):
                return ""

        resolved_state = {"provider": "dummy", "email": "alive2@example.com"}
        with (
            mock.patch.object(
                custom_email_recheck.config_store,
                "get_all",
                return_value={"chatgpt_recheck_allow_existing_phone_verification": True},
            ),
            mock.patch.object(
                custom_email_recheck,
                "_resolve_custom_email_service",
                return_value=(_DummyResolvedEmailService(resolved_state), resolved_state),
            ),
            mock.patch.object(
                custom_email_recheck,
                "_capture_access_token_without_refresh_token",
                side_effect=_capture_stage1,
            ),
            mock.patch.object(custom_email_recheck, "RefreshTokenRegistrationEngine", _FakeFollowupEngine),
        ):
            custom_email_recheck.recheck_custom_chatgpt_email(
                email="alive2@example.com",
                password="pw-new",
                save_on_success=True,
                preferred_account_id=account_id,
                task_id="task-custom-stage1-flag",
            )

        self.assertFalse(captured["allow_add_phone_verification"])
        self.assertTrue(captured["allow_existing_phone_verification"])


    def test_custom_email_recheck_records_add_phone_challenge_on_followup_failure(self):
        account_id = self._add_account(email="needphone@example.com", status="invalid")

        class _FakeRegisterClient:
            device_id = "dev-1"
            ua = "ua"
            sec_ch_ua = "sec"
            impersonate = "chrome"
            fingerprint = None

        class _FakeOAuthClient:
            last_error = ""

            def __init__(self, config):
                self.config = config

            def login_and_get_tokens(self, *_args, **_kwargs):
                from services.chatgpt_core.bound_phone import upsert_chatgpt_phone_challenge

                upsert_chatgpt_phone_challenge(
                    account_id=self.config.get("_current_account_id") or 0,
                    email=self.config.get("_current_account_email") or "",
                    challenge_type="add_phone",
                    status="unbound_required",
                    source="custom_email_recheck",
                    message="命中 add_phone，账号尚未绑定手机号",
                )
                self.last_error = "passwordless 登录后仍停留在 add_phone，且 add_phone 新绑开关关闭"
                return None

        class _FakeFollowupEngine:
            def __init__(self, **kwargs):
                self.extra_config = kwargs.get("extra_config") or {}
                self.email = ""
                self.password = ""

            def _build_chatgpt_client(self):
                return _FakeRegisterClient()

            def _build_oauth_client(self):
                return _FakeOAuthClient(self.extra_config)

            def _extract_account_info(self, _tokens):
                return {"account_id": "acct-stage1"}

        def _capture_stage1(**_kwargs):
            return {
                "access_token": "stage1-at",
                "account_id": "acct-stage1",
            }, {"provider": "dummy", "email": "needphone@example.com"}, _FakeEngineInstance()

        resolved_state = {"provider": "dummy", "email": "needphone@example.com"}
        with (
            mock.patch.object(
                custom_email_recheck.config_store,
                "get_all",
                return_value={"chatgpt_recheck_allow_existing_phone_verification": True},
            ),
            mock.patch.object(
                custom_email_recheck,
                "_resolve_custom_email_service",
                return_value=(_DummyResolvedEmailService(resolved_state), resolved_state),
            ),
            mock.patch.object(
                custom_email_recheck,
                "_capture_access_token_without_refresh_token",
                side_effect=_capture_stage1,
            ),
            mock.patch.object(custom_email_recheck, "RefreshTokenRegistrationEngine", _FakeFollowupEngine),
        ):
            result = custom_email_recheck.recheck_custom_chatgpt_email(
                email="needphone@example.com",
                password="pw-new",
                save_on_success=True,
                preferred_account_id=account_id,
                task_id="task-add-phone-challenge",
            )

        self.assertTrue(result["ok"])
        with Session(self.engine) as session:
            row = session.get(AccountModel, account_id)
            extra = row.get_extra()
        challenge = extra["chatgpt_phone_challenge"]
        self.assertEqual(challenge["type"], "add_phone")
        self.assertEqual(challenge["status"], "unbound_required")
        self.assertEqual(challenge["display"], "未绑定手机号")


if __name__ == "__main__":
    unittest.main()
