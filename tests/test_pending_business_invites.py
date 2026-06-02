import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sqlmodel import Session, create_engine

from core import db as core_db
from core.base_mailbox import MailboxAccount
from core.db import AccountModel, PendingBusinessInviteModel, SQLModel
from api import actions as api_actions
from services.chatgpt_core import pending_business_invites
from services.chatgpt_core.refresh_token_registration_engine import EmailServiceAdapter, RegistrationResult


class PendingBusinessInviteRecoveryTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "pending_invites.db"
        self.engine = create_engine(f"sqlite:///{db_path}")
        self.core_engine_patch = mock.patch.object(core_db, "engine", self.engine)
        self.pending_engine_patch = mock.patch.object(pending_business_invites, "engine", self.engine)
        self.core_engine_patch.start()
        self.pending_engine_patch.start()
        SQLModel.metadata.create_all(self.engine)
        core_db.init_db()

    def tearDown(self):
        self.pending_engine_patch.stop()
        self.core_engine_patch.stop()
        self._tmpdir.cleanup()

    def _add_pending(self, *, email: str, status: str, checkpoint: str = "", error: str = "") -> int:
        with Session(self.engine) as session:
            row = PendingBusinessInviteModel(
                account_id=1,
                email=email,
                status=status,
                last_checkpoint=checkpoint,
                last_error=error,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return int(row.id or 0)

    def _add_account(
        self,
        *,
        email: str = "demo@example.com",
        password: str = "pw",
        status: str = "registered",
        extra_json: str = '{"chatgpt_mailbox_state": {"provider": "dummy"}}',
    ) -> int:
        with Session(self.engine) as session:
            row = AccountModel(
                platform="chatgpt",
                email=email,
                password=password,
                token="at-demo",
                status=status,
                extra_json=extra_json,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return int(row.id or 0)

    def test_recover_stuck_pending_business_invites_marks_retryable(self):
        invite_id = self._add_pending(
            email="stuck@example.com",
            status="activation_auth_login",
            checkpoint="activation_consuming_invite",
        )

        recovered = core_db.recover_stuck_pending_business_invites()

        self.assertEqual(recovered, 1)
        with Session(self.engine) as session:
            row = session.get(PendingBusinessInviteModel, invite_id)
            self.assertIsNotNone(row)
            self.assertEqual(row.status, "failed_retryable")
            self.assertEqual(row.last_checkpoint, "activation_consuming_invite")
            self.assertEqual(row.last_error_code, "activation_interrupted")
            self.assertIn("中断", row.last_error)

    def test_list_pending_invite_ids_for_activation_skips_non_activatable(self):
        retryable_id = self._add_pending(
            email="retryable@example.com",
            status="failed_retryable",
            checkpoint="activation_auth_login",
        )
        pending_id = self._add_pending(
            email="pending@example.com",
            status="invite_sent_pending_activation",
        )
        self._add_pending(email="done@example.com", status="completed")
        self._add_pending(email="abandoned@example.com", status="abandoned")
        self._add_pending(email="terminal@example.com", status="failed_terminal")

        invite_ids = pending_business_invites.list_pending_invite_ids_for_activation(limit=20)

        self.assertEqual(invite_ids, [retryable_id, pending_id])

    def test_upsert_pending_subscription_auth_from_account_creates_subscription_item(self):
        account_id = self._add_account(email="sub@example.com")
        with Session(self.engine) as session:
            account = session.get(AccountModel, account_id)
            extra = account.get_extra()
            extra["chatgpt_registration_context"] = {"browser_mode": "protocol"}
            account.set_extra(extra)
            session.add(account)
            session.commit()

            pending = pending_business_invites.upsert_pending_subscription_auth_from_account(
                account,
                checkout_url="https://pay.example/checkout",
                plan="plus",
                country="ID",
                currency="IDR",
            )

        self.assertGreater(int(getattr(pending, "id", 0) or 0), 0)
        items = pending_business_invites.list_pending_invites(limit=20)
        row = next(item for item in items if int(item.get("account_id") or 0) == account_id)
        self.assertEqual(row["activation_kind"], "subscription_auth")
        self.assertEqual(row["status"], "subscription_pending_auth")

    def test_activate_subscription_auth_pending_uses_subscription_branch(self):
        account_id = self._add_account(email="activate@example.com", password="pw-activate")
        with Session(self.engine) as session:
            pending = PendingBusinessInviteModel(
                account_id=account_id,
                email="activate@example.com",
                status="subscription_pending_auth",
                mailbox_state_json='{"provider":"dummy"}',
                registration_context_json='{"activation_kind":"subscription_auth","browser_mode":"protocol"}',
                last_checkpoint="subscription_pending_auth",
            )
            session.add(pending)
            session.commit()
            session.refresh(pending)
            invite_id = int(pending.id or 0)

        fake_result = RegistrationResult(
            success=True,
            email="activate@example.com",
            password="pw-activate",
            account_id="acct-new",
            workspace_id="ws-new",
            access_token="at-new",
            refresh_token="rt-new",
            workspace_artifacts=[{"scope": "free", "workspace_id": "ws-new", "account_id": "acct-new"}],
        )

        class _FakeEngine:
            def __init__(self, **_kwargs):
                self.email = ""
                self.password = ""

            def run(self):
                return fake_result

        with mock.patch.object(pending_business_invites, "RefreshTokenRegistrationEngine", _FakeEngine), \
            mock.patch.object(pending_business_invites, "_update_account_from_activation_result", return_value=(None, [])):
            out = pending_business_invites.activate_pending_invite(invite_id)

        self.assertTrue(out["ok"])
        self.assertEqual(out["activation_kind"], "subscription_auth")
        with Session(self.engine) as session:
            row = session.get(PendingBusinessInviteModel, invite_id)
            self.assertEqual(row.status, "completed")

    def test_subscription_auth_pending_retries_retryable_workspace_sync_error(self):
        account_id = self._add_account(email="retry-sync@example.com", password="pw-retry")
        with Session(self.engine) as session:
            pending = PendingBusinessInviteModel(
                account_id=account_id,
                email="retry-sync@example.com",
                status="subscription_pending_auth",
                mailbox_state_json='{"provider":"dummy"}',
                registration_context_json='{"activation_kind":"subscription_auth","browser_mode":"protocol"}',
                last_checkpoint="subscription_pending_auth",
            )
            session.add(pending)
            session.commit()
            session.refresh(pending)
            invite_id = int(pending.id or 0)

        failed = RegistrationResult(success=False, error_message="workspace/org 选择失败: page=consent")
        success = RegistrationResult(
            success=True,
            email="retry-sync@example.com",
            password="pw-retry",
            account_id="acct-retry",
            workspace_id="ws-retry",
            access_token="at-retry",
            refresh_token="rt-retry",
            workspace_artifacts=[{"scope": "free", "workspace_id": "ws-retry", "account_id": "acct-retry"}],
        )
        logs: list[str] = []

        with mock.patch.object(pending_business_invites, "_subscription_auth_retry_delays_seconds", return_value=[0]), \
            mock.patch.object(pending_business_invites, "_run_subscription_auth_engine_once", side_effect=[failed, success]) as mocked_run, \
            mock.patch.object(pending_business_invites, "_update_account_from_activation_result", return_value=(None, [])):
            out = pending_business_invites.activate_pending_invite(
                invite_id,
                log_fn=lambda message: logs.append(str(message)),
            )

        self.assertTrue(out["ok"])
        self.assertEqual(mocked_run.call_count, 2)
        self.assertTrue(any("等待" in line and "重试" in line for line in logs), logs)

    def test_subscription_auth_pending_does_not_retry_non_retryable_error(self):
        account_id = self._add_account(email="no-retry@example.com", password="pw-no-retry")
        with Session(self.engine) as session:
            pending = PendingBusinessInviteModel(
                account_id=account_id,
                email="no-retry@example.com",
                status="subscription_pending_auth",
                mailbox_state_json='{"provider":"dummy"}',
                registration_context_json='{"activation_kind":"subscription_auth","browser_mode":"protocol"}',
                last_checkpoint="subscription_pending_auth",
            )
            session.add(pending)
            session.commit()
            session.refresh(pending)
            invite_id = int(pending.id or 0)

        failed = RegistrationResult(success=False, error_message="创建邮箱失败")
        with mock.patch.object(pending_business_invites, "_run_subscription_auth_engine_once", return_value=failed) as mocked_run:
            with self.assertRaises(ValueError):
                pending_business_invites.activate_pending_invite(invite_id)

        self.assertEqual(mocked_run.call_count, 1)

    def test_restored_email_service_create_email_reuses_saved_mailbox(self):
        state = {
            "provider": "dummy",
            "email": "restored@example.com",
            "account": {"email": "restored@example.com", "account_id": "mailbox-1", "extra": {}},
            "before_ids": ["old-1"],
        }

        class _DummyMailbox:
            def get_current_ids(self, account):
                return {"old-1", "old-2"}

        with mock.patch.object(pending_business_invites, "create_mailbox", return_value=_DummyMailbox()):
            service = pending_business_invites.RestoredEmailService(state=state)
            payload = service.create_email()

        self.assertEqual(payload["email"], "restored@example.com")
        self.assertEqual(payload["mailbox_action"], "restored_existing")

    def test_restored_email_service_marks_processed_otp_message_in_before_ids(self):
        state = {
            "provider": "dummy",
            "email": "restored@example.com",
            "account": {"email": "restored@example.com", "account_id": "mailbox-1", "extra": {}},
            "before_ids": [],
        }

        class _DummyMailbox:
            _last_verification_result = {}

            def get_current_ids(self, account):
                return set()

            def wait_for_code(self, account, **kwargs):
                self._last_verification_result = {
                    "message_id": "otp-message-1",
                    "code": "476433",
                }
                return "476433"

        with mock.patch.object(pending_business_invites, "create_mailbox", return_value=_DummyMailbox()):
            service = pending_business_invites.RestoredEmailService(state=state)
            service.create_email()
            adapter = EmailServiceAdapter(service, "restored@example.com", lambda *_args: None)

            code = adapter.wait_for_verification_code(
                "restored@example.com",
                timeout=1,
                phase="oauth_email_otp",
                phase_label="OAuth 登录邮箱验证码",
            )
            exported = service.export_state()

        self.assertEqual(code, "476433")
        self.assertIn("otp-message-1", exported["before_ids"])

    def test_restored_email_service_recreates_expired_tempmail_mailbox_by_exact_email(self):
        state = {
            "provider": "tempmail_local",
            "email": "restored@example.com",
            "account": {"email": "restored@example.com", "account_id": "old-mailbox", "extra": {}},
            "before_ids": ["old-1"],
            "config": {"tempmail_api_url": "http://tempmail-api-1:8080", "tempmail_api_key": "test"},
        }

        class _TempMailMailbox:
            def __init__(self):
                self.ensured_email = ""
                self.baseline_account_id = ""

            def ensure_mailbox_by_email(self, email):
                self.ensured_email = email
                return MailboxAccount(
                    email=email,
                    account_id="new-mailbox",
                    extra={"mailbox_action": "created_exact_address"},
                )

            def get_current_ids(self, account):
                self.baseline_account_id = account.account_id
                return {"new-1"}

        mailbox = _TempMailMailbox()
        logs = []
        with mock.patch.object(pending_business_invites, "create_mailbox", return_value=mailbox):
            service = pending_business_invites.RestoredEmailService(
                state=state,
                log_fn=lambda msg, level="info": logs.append(msg),
            )
            payload = service.create_email()
            exported = service.export_state()

        self.assertEqual(mailbox.ensured_email, "restored@example.com")
        self.assertEqual(mailbox.baseline_account_id, "new-mailbox")
        self.assertEqual(payload["service_id"], "new-mailbox")
        self.assertEqual(payload["mailbox_action"], "created_exact_address")
        self.assertEqual(exported["account"]["account_id"], "new-mailbox")
        self.assertTrue(any("按原地址新建" in line for line in logs))

    def test_actions_resume_subscription_auth_uses_account_level_capture(self):
        account_id = self._add_account(email="api-resume@example.com")
        with Session(self.engine) as session:
            account = session.get(AccountModel, account_id)
            with mock.patch.object(api_actions, "_execute_chatgpt_resume_subscription_auth", return_value={
                "ok": True,
                "data": {"message": "补抓 Auth 完成", "auth_capture": {"account_id": "acct-1"}},
            }) as mocked:
                result = api_actions._execute_platform_action(None, "chatgpt", account, "resume_subscription_auth", {}, session)

        self.assertTrue(result["ok"])
        mocked.assert_called_once()

    def test_subscription_auth_upsert_rebuilds_tempmail_mailbox_state_from_global_config(self):
        with Session(self.engine) as session:
            account = AccountModel(
                platform="chatgpt",
                email="legacy@example.com",
                password="pw",
                status="registered",
                extra_json='{"mail_provider": "tempmail_local"}',
            )
            session.add(account)
            session.commit()
            session.refresh(account)
            account_id = int(account.id or 0)

        with Session(self.engine) as session:
            account = session.get(AccountModel, account_id)

        with mock.patch.object(pending_business_invites.config_store, "get_all", return_value={
            "tempmail_api_url": "http://tempmail-api-1:8080",
            "tempmail_api_key": "test-key",
            "tempmail_api_key_header": "Authorization",
            "tempmail_primary_domain": "example.com",
            "tempmail_mode": "fixed_domain",
            "tempmail_ttl_minutes": "60",
        }):
            pending = pending_business_invites.upsert_pending_subscription_auth_from_account(account)

        self.assertGreater(pending.id, 0)
        with Session(self.engine) as session:
            row = session.get(PendingBusinessInviteModel, pending.id)
            mailbox_state = pending_business_invites._loads(row.mailbox_state_json, {})
            account_row = session.get(AccountModel, account_id)
            account_extra = account_row.get_extra()

        self.assertEqual(mailbox_state["provider"], "tempmail_local")
        self.assertEqual(mailbox_state["email"], "legacy@example.com")
        self.assertEqual(mailbox_state["account"]["email"], "legacy@example.com")
        self.assertEqual(mailbox_state["config"]["tempmail_api_key"], "test-key")
        self.assertTrue(mailbox_state["recovered_from_account_config"])
        self.assertEqual(account_extra["chatgpt_mailbox_state"]["provider"], "tempmail_local")

    def test_resume_subscription_auth_returns_logs(self):
        account_id = self._add_account(email="api-log@example.com")
        with Session(self.engine) as session:
            account = session.get(AccountModel, account_id)

            with mock.patch(
                "services.chatgpt_core.subscription_auth_capture.capture_subscription_auth_for_account",
                side_effect=lambda account_id, allow_phone_verification=False, log_fn=None: (
                    log_fn("[补抓] 账号级补抓 auth") if callable(log_fn) else None,
                    {"ok": True, "data": {"message": "补抓 Auth 完成", "auth_capture": {"account_id": "acct-log"}, "logs": ["[补抓] 账号级补抓 auth"]}},
                )[1],
            ):
                result = api_actions._execute_chatgpt_resume_subscription_auth(account)

        self.assertTrue(result["ok"])
        self.assertIn("logs", result.get("data", {}))
        self.assertTrue(any("补抓 auth" in line for line in (result.get("data", {}).get("logs") or [])))

    def test_resume_subscription_auth_does_not_use_pending_activation(self):
        account_id = self._add_account(email="api-direct@example.com")
        with Session(self.engine) as session:
            account = session.get(AccountModel, account_id)

            with mock.patch(
                "services.chatgpt_core.subscription_auth_capture.capture_subscription_auth_for_account",
                return_value={
                    "ok": True,
                    "data": {
                        "message": "补抓 Auth 完成",
                        "auth_capture": {"account_id": "acct-direct"},
                        "logs": [],
                    },
                    "error": "",
                },
            ) as capture_mock, mock.patch.object(
                pending_business_invites,
                "upsert_pending_subscription_auth_from_account",
            ) as upsert_mock, mock.patch.object(
                pending_business_invites,
                "activate_pending_invite",
            ) as activate_mock:
                result = api_actions._execute_chatgpt_resume_subscription_auth(
                    account,
                    allow_phone_verification=True,
                )

        self.assertTrue(result["ok"])
        capture_mock.assert_called_once()
        self.assertTrue(capture_mock.call_args.kwargs["allow_phone_verification"])
        upsert_mock.assert_not_called()
        activate_mock.assert_not_called()

    def test_resume_subscription_auth_returns_logs_on_failure(self):
        account_id = self._add_account(email="api-log-fail@example.com")
        with Session(self.engine) as session:
            account = session.get(AccountModel, account_id)

            with mock.patch(
                "services.chatgpt_core.subscription_auth_capture.capture_subscription_auth_for_account",
                side_effect=lambda account_id, allow_phone_verification=False, log_fn=None: (
                    log_fn("[补抓] 登录失败前的详细日志") if callable(log_fn) else None,
                    {"ok": False, "error": "创建邮箱失败", "data": {"message": "创建邮箱失败", "logs": ["[补抓] 登录失败前的详细日志", "[补抓] 失败：创建邮箱失败"]}},
                )[1],
            ):
                result = api_actions._execute_chatgpt_resume_subscription_auth(account)

        self.assertFalse(result["ok"])
        logs = result.get("data", {}).get("logs") or []
        self.assertTrue(any("详细日志" in line for line in logs))
        self.assertTrue(any("创建邮箱失败" in line for line in logs))

    def test_subscription_auth_plus_scope_defaults_to_free_only(self):
        capture_free, capture_business, plan = pending_business_invites._infer_subscription_auth_capture_scope(
            registration_context={"activation_kind": "subscription_auth", "plan": "plus"},
            account_extra={},
        )

        self.assertTrue(capture_free)
        self.assertFalse(capture_business)
        self.assertEqual(plan, "plus")

    def test_subscription_auth_team_scope_defaults_to_business_only(self):
        capture_free, capture_business, plan = pending_business_invites._infer_subscription_auth_capture_scope(
            registration_context={"activation_kind": "subscription_auth", "plan": "team"},
            account_extra={},
        )

        self.assertFalse(capture_free)
        self.assertTrue(capture_business)
        self.assertEqual(plan, "team")

    def test_update_account_from_activation_result_dedupes_duplicate_workspace_accounts(self):
        account_id = self._add_account(email="dedupe@example.com", password="pw-dedupe")
        fake_result = RegistrationResult(
            success=True,
            email="dedupe@example.com",
            password="pw-dedupe",
            account_id="acct-same",
            workspace_id="ws-same",
            access_token="at-same",
            refresh_token="rt-same",
            workspace_artifacts=[
                {"scope": "free", "workspace_id": "ws-same", "account_id": "acct-same", "access_token": "at-same", "refresh_token": "rt-same", "variant_key": "free:ws-same"},
                {"scope": "free", "workspace_id": "ws-same", "account_id": "acct-same", "access_token": "at-same", "refresh_token": "rt-same", "variant_key": "free:ws-same"},
            ],
        )

        with Session(self.engine) as session:
            account = session.get(AccountModel, account_id)
            update_result = pending_business_invites._update_account_from_activation_result(account, fake_result)

        self.assertIsInstance(update_result, tuple)
        self.assertEqual(len(update_result[1]), 0)

    def test_update_account_from_activation_result_preserves_payment_statuses(self):
        pending_account_id = self._add_account(
            email="pending-status@example.com",
            password="pw-pending",
            status="pending_payment",
            extra_json='{"chatgpt_last_payment_link": {"url": "https://checkout.example.test/cs_123"}}',
        )
        subscribed_account_id = self._add_account(
            email="subscribed-status@example.com",
            password="pw-subscribed",
            status="subscribed",
        )
        failed_account_id = self._add_account(
            email="failed-status@example.com",
            password="pw-failed",
            status="payment_failed",
        )

        for account_id, email, expected_status in (
            (pending_account_id, "pending-status@example.com", "pending_payment"),
            (subscribed_account_id, "subscribed-status@example.com", "subscribed"),
            (failed_account_id, "failed-status@example.com", "payment_failed"),
        ):
            fake_result = RegistrationResult(
                success=True,
                email=email,
                password=f"pw-{expected_status}",
                account_id=f"acct-{expected_status}",
                workspace_id=f"ws-{expected_status}",
                access_token=f"at-{expected_status}",
                refresh_token=f"rt-{expected_status}",
            )
            with Session(self.engine) as session:
                account = session.get(AccountModel, account_id)
                pending_business_invites._update_account_from_activation_result(account, fake_result)

                self.assertEqual(account.status, expected_status)
                self.assertEqual(account.user_id, f"acct-{expected_status}")
                self.assertEqual(account.token, f"at-{expected_status}")

    def test_update_account_from_activation_result_recovers_plain_pending_auth_capture(self):
        account_id = self._add_account(
            email="plain-pending@example.com",
            password="pw-plain",
            status="pending_payment",
        )
        fake_result = RegistrationResult(
            success=True,
            email="plain-pending@example.com",
            password="pw-plain",
            account_id="acct-plain",
            workspace_id="ws-plain",
            access_token="at-plain",
            refresh_token="rt-plain",
        )

        with Session(self.engine) as session:
            account = session.get(AccountModel, account_id)
            pending_business_invites._update_account_from_activation_result(account, fake_result)

            self.assertEqual(account.status, "registered")
            self.assertEqual(account.user_id, "acct-plain")


class TeamInviteSourceVisibilityTests(unittest.TestCase):
    def test_free_completed_pending_invite_is_not_visible_or_removable(self):
        from api.accounts import _is_team_invite_source_removable, _is_team_invite_source_visible

        self.assertFalse(
            _is_team_invite_source_visible(
                workspace_scope="free",
                invite_status="completed",
                team_id=12,
            )
        )
        self.assertFalse(
            _is_team_invite_source_removable(
                workspace_scope="free",
                invite_status="completed",
                team_id=12,
                removed_from_team_at="",
            )
        )

    def test_business_completed_pending_invite_is_removable(self):
        from api.accounts import _is_team_invite_source_removable, _is_team_invite_source_visible

        self.assertTrue(
            _is_team_invite_source_visible(
                workspace_scope="business",
                invite_status="completed",
                team_id=12,
            )
        )
        self.assertTrue(
            _is_team_invite_source_removable(
                workspace_scope="business",
                invite_status="completed",
                team_id=12,
                removed_from_team_at="",
            )
        )


if __name__ == "__main__":
    unittest.main()
