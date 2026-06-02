import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sqlmodel import Session, SQLModel, create_engine

from api.tasks import (
    BatchInvalidRecheckTaskRequest,
    _resolve_batch_invalid_recheck_accounts,
)
from core import db as core_db
from core.db import AccountModel
from services.chatgpt_core import invalid_account_recheck


class InvalidAccountRecheckTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "invalid_recheck.db"
        self.engine = create_engine(f"sqlite:///{db_path}")
        self.core_engine_patch = mock.patch.object(core_db, "engine", self.engine)
        self.tasks_engine_patch = mock.patch("api.tasks.engine", self.engine)
        self.core_engine_patch.start()
        self.tasks_engine_patch.start()
        SQLModel.metadata.create_all(self.engine)
        core_db.init_db()

    def tearDown(self):
        self.tasks_engine_patch.stop()
        self.core_engine_patch.stop()
        self._tmpdir.cleanup()

    def _add_account(self, *, email: str = "invalid@example.com", status: str = "invalid", extra: str | None = None) -> int:
        with Session(self.engine) as session:
            row = AccountModel(
                platform="chatgpt",
                email=email,
                password="pw",
                token="old-token",
                status=status,
                extra_json=extra
                or (
                    '{"chatgpt_mailbox_state": {"provider": "dummy", "email": "invalid@example.com"}, '
                    '"refresh_token": "old-rt", "chatgpt_has_refresh_token_solution": true, '
                    '"chatgpt_local": {"auth": {"state": "account_deactivated"}}}'
                ),
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return int(row.id or 0)

    def _patch_recheck_runtime(self, *, tokens=None, error=""):
        login_calls: list[dict] = []

        class _FakeEmailService:
            service_type = type("ST", (), {"value": "dummy"})()

            def __init__(self, *, state, log_fn=None):
                self.state = dict(state or {})

            def create_email(self):
                return {"email": "invalid@example.com"}

            def export_state(self):
                return {**self.state, "provider": "dummy", "email": "invalid@example.com"}

        class _FakeRegisterClient:
            device_id = "dev-1"
            ua = "ua"
            sec_ch_ua = "sec"
            impersonate = "chrome"
            fingerprint = None

        class _FakeOAuthClient:
            last_error = error

            def login_and_get_tokens(self, *_args, **kwargs):
                login_calls.append(kwargs)
                return tokens

            def _get_cookie_value(self, *_args, **_kwargs):
                return "session-new"

        class _FakeEngine:
            def __init__(self, **_kwargs):
                self.email = ""
                self.password = ""

            def _build_chatgpt_client(self):
                return _FakeRegisterClient()

            def _build_oauth_client(self):
                return _FakeOAuthClient()

            def _extract_account_info(self, tokens):
                return {"account_id": tokens.get("account_id", "")}

        return (
            login_calls,
            mock.patch.object(invalid_account_recheck, "RestoredEmailService", _FakeEmailService),
            mock.patch.object(invalid_account_recheck, "RefreshTokenRegistrationEngine", _FakeEngine),
            mock.patch.object(invalid_account_recheck.config_store, "get_all", return_value={}),
        )

    def test_recheck_success_saves_access_token_and_recovers_status(self):
        account_id = self._add_account()
        tokens = {"access_token": "at-new", "refresh_token": "rt-ignored", "account_id": "acct-new"}
        login_calls, email_patch, engine_patch, config_patch = self._patch_recheck_runtime(tokens=tokens)

        with email_patch, engine_patch, config_patch:
            result = invalid_account_recheck.recheck_invalid_chatgpt_account(
                account_id,
                retry_delays_seconds=[],
                task_id="task-recheck",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(len(login_calls), 1)
        self.assertFalse(login_calls[0]["allow_phone_verification"])
        self.assertFalse(login_calls[0]["allow_add_phone_session_recovery"])
        with Session(self.engine) as session:
            account = session.get(AccountModel, account_id)
            extra = account.get_extra()
        self.assertEqual(account.status, "registered")
        self.assertEqual(account.token, "at-new")
        self.assertEqual(extra["access_token"], "at-new")
        self.assertNotIn("refresh_token", extra)
        self.assertNotIn("chatgpt_local", extra)
        self.assertEqual(extra["chatgpt_invalid_recheck"]["status"], "recovered_access_token")
        self.assertEqual(extra["chatgpt_capabilities"]["auth_level"], "access_token_only")

    def test_deactivated_result_stays_invalid_and_records_reason(self):
        account_id = self._add_account()
        login_calls, email_patch, engine_patch, config_patch = self._patch_recheck_runtime(
            tokens=None,
            error="account_deactivated: You do not have an account because it has been deleted or deactivated.",
        )

        with email_patch, engine_patch, config_patch:
            result = invalid_account_recheck.recheck_invalid_chatgpt_account(
                account_id,
                retry_delays_seconds=[],
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["data"]["error_code"], "account_deactivated")
        self.assertEqual(len(login_calls), 1)
        with Session(self.engine) as session:
            account = session.get(AccountModel, account_id)
            extra = account.get_extra()
        self.assertEqual(account.status, "invalid")
        self.assertEqual(account.token, "old-token")
        self.assertEqual(extra["chatgpt_invalid_recheck"]["status"], "account_deactivated")
        self.assertEqual(extra["chatgpt_capabilities"]["auth_level"], "invalid")

    def test_batch_resolver_only_allows_invalid_accounts(self):
        invalid_id = self._add_account(email="invalid-one@example.com", status="invalid")
        registered_id = self._add_account(
            email="registered-one@example.com",
            status="registered",
            extra='{"chatgpt_mailbox_state": {"provider": "dummy", "email": "registered-one@example.com"}}',
        )
        req = BatchInvalidRecheckTaskRequest(account_ids=[invalid_id, registered_id, 999999])

        eligible, missing_ids, skipped, matched = _resolve_batch_invalid_recheck_accounts(req)

        self.assertEqual([item["account_id"] for item in eligible], [invalid_id])
        self.assertEqual(missing_ids, [999999])
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["account_id"], registered_id)
        self.assertEqual(matched, [])


if __name__ == "__main__":
    unittest.main()
