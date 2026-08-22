import base64
import json
import tempfile
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine, select

from api import tasks
from core import db as core_db
from core.base_mailbox import HmeReadyMailbox
from core.db import AccountModel, ChatGPTEmailChangeModel
from services.chatgpt_core import email_change


SOURCE_EMAIL = "source@example.com"
TARGET_EMAIL = "target@example.net"
SOURCE_USER_ID = "user-original"
SOURCE_ACCOUNT_ID = "acct-original"
SOURCE_ORG_ID = "org-original"


def _jwt(*, user_id: str = SOURCE_USER_ID, account_id: str = SOURCE_ACCOUNT_ID) -> str:
    payload = {
        "sub": user_id,
        "https://api.openai.com/auth": {"chatgpt_account_id": account_id},
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).decode().rstrip("=")
    return f"header.{encoded}.signature"


def _mailbox_state(email: str, *, secret: str = "") -> dict:
    config = {"manual_email_address": email}
    if secret:
        config.update(
            {
                "tempmail_api_key": secret,
                "tempmail_api_url": "https://mail.invalid",
            }
        )
    return {
        "provider": "manual_email_otp",
        "email": email,
        "account": {"email": email, "account_id": email, "extra": {}},
        "before_ids": [],
        "config": config,
        "proxy": "",
    }


class ScriptedEmailChangeService(email_change.ChatGPTEmailChangeService):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.requests = []
        self.source_browser_calls = 0
        self.reauth_calls = 0
        self.otp_calls = []
        self.target_login_calls = 0
        self.target_login_states = []
        self.finalize_calls = 0
        self.begin_reauth_always = False
        self.fail_commit_count = 0
        self.fail_finalize_count = 0

    @contextmanager
    def _source_browser(self, account):
        self.source_browser_calls += 1

        def request(method, path, payload=None):
            self.requests.append((method, path, payload))
            if path == "/backend-api/me":
                return {
                    "ok": True,
                    "status": 200,
                    "data": {
                        "object": "user",
                        "id": SOURCE_USER_ID,
                        "email": SOURCE_EMAIL,
                        "orgs": {
                            "data": [
                                {"id": SOURCE_ORG_ID, "is_default": True},
                            ]
                        },
                    },
                }
            if path.endswith("/eligibility"):
                return {
                    "ok": True,
                    "status": 200,
                    "data": {"eligible": True, "eligibility_type": "password"},
                }
            if path.endswith("/begin"):
                if self.begin_reauth_always:
                    return {
                        "ok": False,
                        "status": 401,
                        "data": {"code": "reauth_required"},
                        "text": "reauth_required",
                    }
                return {"ok": True, "status": 200, "data": {"success": True}}
            if path.endswith("/verify"):
                return {"ok": True, "status": 200, "data": {"success": True}}
            raise AssertionError(f"unexpected request: {method} {path}")

        yield None, request

    def _wait_for_otp(self, state, *, email, phase, label, sent_at):
        self.otp_calls.append(
            {
                "state": dict(state),
                "email": email,
                "phase": phase,
                "label": label,
                "sent_at": sent_at,
            }
        )
        next_state = dict(state)
        next_state["before_ids"] = [
            *list(state.get("before_ids") or []),
            "change-message-id",
        ]
        return "012345", "change-message-id", next_state

    def _reauth_source(self, account, source_mailbox_state, expected_identity):
        self.reauth_calls += 1
        return account, source_mailbox_state, {
            "email": SOURCE_EMAIL,
            "user_id": SOURCE_USER_ID,
            "account_id": SOURCE_ACCOUNT_ID,
            "organization_id": SOURCE_ORG_ID,
        }

    def _prepare_target_login(self, account, target_state, source_identity):
        self.target_login_calls += 1
        self.target_login_states.append(dict(target_state))
        next_state = dict(target_state)
        next_state["before_ids"] = [
            *list(target_state.get("before_ids") or []),
            "target-login-message-id",
        ]
        return {
            "access_token": _jwt(),
            "session_token": "session-new",
            "cookies": "__Secure-next-auth.session-token=session-new",
            "cookie_header": "__Secure-next-auth.session-token=session-new",
            "account_id": SOURCE_ACCOUNT_ID,
            "workspace_id": SOURCE_ACCOUNT_ID,
            "browser_fingerprint": {},
        }, next_state

    def _verify_remote_identity(
        self,
        tokens,
        *,
        expected_email,
        expected_user_id,
        expected_account_id,
        expected_organization_id,
        context_label,
        remote_changed,
    ):
        return {
            "email": expected_email,
            "user_id": SOURCE_USER_ID,
            "account_id": SOURCE_ACCOUNT_ID,
            "organization_id": SOURCE_ORG_ID,
        }

    def _commit_account(self, **kwargs):
        if self.fail_commit_count > 0:
            self.fail_commit_count -= 1
            raise email_change.EmailChangeError(
                "local_commit_failed",
                "local commit failed",
                retryable=True,
                remote_changed=True,
            )
        return super()._commit_account(**kwargs)

    def _finalize_target_mailbox(self, **kwargs):
        self.finalize_calls += 1
        if self.fail_finalize_count > 0:
            self.fail_finalize_count -= 1
            raise email_change.EmailChangeError(
                "mailbox_finalize_failed",
                "mailbox finalize failed",
                retryable=True,
                remote_changed=True,
            )
        return dict(kwargs["mailbox_state"])


class ChatGPTEmailChangeTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "email-change.db"
        self.engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
        )
        self.patches = [
            mock.patch.object(core_db, "engine", self.engine),
            mock.patch.object(email_change, "engine", self.engine),
            mock.patch.object(tasks, "engine", self.engine),
            mock.patch.object(email_change, "apply_material_capture"),
            mock.patch.object(
                email_change,
                "schedule_chatgpt_local_status_refresh_for_account_id",
            ),
        ]
        for patcher in self.patches:
            patcher.start()
        SQLModel.metadata.create_all(self.engine)
        core_db._ensure_chatgpt_email_change_schema()

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        self.engine.dispose()
        self._tmpdir.cleanup()

    def _add_account(
        self,
        *,
        email: str = SOURCE_EMAIL,
        user_id: str = SOURCE_USER_ID,
        account_identity: str = SOURCE_ACCOUNT_ID,
    ) -> int:
        token = _jwt(user_id=user_id, account_id=account_identity)
        extra = {
            "access_token": token,
            "session_token": "session-old",
            "cookies": "__Secure-next-auth.session-token=session-old",
            "cookie_header": "__Secure-next-auth.session-token=session-old",
            "account_id": account_identity,
            "organization_id": SOURCE_ORG_ID,
            "chatgpt_mailbox_state": _mailbox_state(email),
            "mail_provider": "manual_email_otp",
        }
        with Session(self.engine) as session:
            row = AccountModel(
                platform="chatgpt",
                email=email,
                password="password",
                user_id=user_id,
                token=token,
                status="registered",
                extra_json=json.dumps(extra),
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return int(row.id or 0)

    def _add_change(
        self,
        account_id: int,
        *,
        target_email: str = TARGET_EMAIL,
        status: str = "running",
        phase: str = email_change.PHASE_CREATED,
        remote_changed: bool = False,
        verify_submitted: bool = False,
        remove_social_subs: bool = False,
        task_id: str | None = None,
        state: dict | None = None,
    ) -> int:
        row = ChatGPTEmailChangeModel(
            task_id=task_id or f"task-{uuid.uuid4().hex}",
            reservation_ref=f"reservation-{uuid.uuid4().hex}",
            account_id=account_id,
            source_email=SOURCE_EMAIL,
            target_email=target_email,
            source_chatgpt_user_id=SOURCE_USER_ID if remote_changed else "",
            source_chatgpt_account_id=SOURCE_ACCOUNT_ID if remote_changed else "",
            source_organization_id=SOURCE_ORG_ID if remote_changed else "",
            target_mailbox_ref=f"manual_email_otp:{target_email}",
            target_mailbox_provider="manual_email_otp",
            phase=phase,
            status=status,
            remove_social_subs=remove_social_subs,
            remote_changed_at="2026-08-23T00:00:00+00:00" if remote_changed else "",
            verify_submitted_at="2026-08-23T00:00:00+00:00" if verify_submitted else "",
            resumable=status in {"failed", "partial"},
        )
        row.set_mailbox_state(state or _mailbox_state(target_email))
        with Session(self.engine) as session:
            session.add(row)
            session.commit()
            session.refresh(row)
            return int(row.id or 0)

    def _service(self, row_id: int) -> ScriptedEmailChangeService:
        return ScriptedEmailChangeService(task_id=f"task-run-{row_id}", row_id=row_id)

    def test_full_flow_uses_target_mailbox_and_preserves_original_row(self):
        account_id = self._add_account()
        row_id = self._add_change(account_id)
        service = self._service(row_id)

        result = service.run()

        self.assertTrue(result["ok"])
        begin = next(item for item in service.requests if item[1].endswith("/begin"))
        verify = next(item for item in service.requests if item[1].endswith("/verify"))
        self.assertEqual(begin[2], {"email": TARGET_EMAIL})
        self.assertNotIn("remove_social_subs", begin[2])
        self.assertEqual(verify[2], {"email": TARGET_EMAIL, "code": "012345"})
        self.assertEqual(service.otp_calls[0]["email"], TARGET_EMAIL)
        self.assertEqual(service.otp_calls[0]["phase"], "chatgpt_email_change_verify_otp")
        self.assertIn(
            "change-message-id",
            service.target_login_states[0]["before_ids"],
        )
        with Session(self.engine) as session:
            accounts = session.exec(select(AccountModel)).all()
            account = session.get(AccountModel, account_id)
            row = session.get(ChatGPTEmailChangeModel, row_id)
        self.assertEqual(len(accounts), 1)
        self.assertEqual(account.id, account_id)
        self.assertEqual(account.email, TARGET_EMAIL)
        self.assertEqual(row.change_otp_message_id, "change-message-id")
        self.assertEqual(row.status, "done")
        self.assertEqual(row.phase, email_change.PHASE_COMMITTED)

    def test_remove_social_subs_is_only_sent_when_explicit(self):
        self.assertEqual(
            email_change.build_begin_payload(TARGET_EMAIL),
            {"email": TARGET_EMAIL},
        )
        self.assertEqual(
            email_change.build_begin_payload(TARGET_EMAIL, remove_social_subs=True),
            {"email": TARGET_EMAIL, "remove_social_subs": True},
        )
        account_id = self._add_account()
        row_id = self._add_change(account_id, remove_social_subs=True)
        service = self._service(row_id)
        service.run()
        begin = next(item for item in service.requests if item[1].endswith("/begin"))
        self.assertIs(begin[2]["remove_social_subs"], True)

    def test_source_reauth_runs_at_most_once(self):
        account_id = self._add_account()
        row_id = self._add_change(account_id)
        service = self._service(row_id)
        service.begin_reauth_always = True

        with self.assertRaises(email_change.EmailChangeError) as caught:
            service.run()

        self.assertEqual(caught.exception.code, "source_reauth_exhausted")
        self.assertEqual(service.reauth_calls, 1)
        self.assertEqual(
            sum(1 for item in service.requests if item[1].endswith("/begin")),
            2,
        )
        with Session(self.engine) as session:
            row = session.get(ChatGPTEmailChangeModel, row_id)
        self.assertEqual(row.status, "failed")
        self.assertEqual(row.phase, email_change.PHASE_SOURCE_REAUTH_REQUIRED)
        self.assertTrue(row.resumable)

    def test_local_target_conflict_never_creates_or_overwrites_account(self):
        account_id = self._add_account()
        other_id = self._add_account(
            email=TARGET_EMAIL,
            user_id="user-other",
            account_identity="acct-other",
        )
        row_id = self._add_change(account_id)
        service = self._service(row_id)

        with self.assertRaises(email_change.EmailChangeError) as caught:
            service.run()

        self.assertEqual(caught.exception.code, "target_email_conflict")
        with Session(self.engine) as session:
            accounts = session.exec(select(AccountModel).order_by(AccountModel.id)).all()
            row = session.get(ChatGPTEmailChangeModel, row_id)
        self.assertEqual([item.id for item in accounts], [account_id, other_id])
        self.assertEqual(accounts[0].email, SOURCE_EMAIL)
        self.assertEqual(accounts[1].email, TARGET_EMAIL)
        self.assertEqual(row.status, "partial")

    def test_remote_changed_recovery_skips_begin_and_verify(self):
        account_id = self._add_account()
        row_id = self._add_change(
            account_id,
            status="partial",
            phase=email_change.PHASE_RECOVERY_REQUIRED,
            remote_changed=True,
        )
        service = self._service(row_id)

        result = service.run()

        self.assertTrue(result["ok"])
        self.assertEqual(service.source_browser_calls, 0)
        self.assertFalse(service.requests)
        self.assertEqual(service.target_login_calls, 1)
        with Session(self.engine) as session:
            account = session.get(AccountModel, account_id)
        self.assertEqual(account.id, account_id)
        self.assertEqual(account.email, TARGET_EMAIL)

    def test_local_commit_failure_is_resumable_without_replaying_remote_change(self):
        account_id = self._add_account()
        row_id = self._add_change(account_id)
        first = self._service(row_id)
        first.fail_commit_count = 1

        with self.assertRaises(email_change.EmailChangeError):
            first.run()

        with Session(self.engine) as session:
            account = session.get(AccountModel, account_id)
            row = session.get(ChatGPTEmailChangeModel, row_id)
        self.assertEqual(account.email, SOURCE_EMAIL)
        self.assertEqual(row.status, "partial")
        self.assertTrue(row.remote_changed_at)

        second = self._service(row_id)
        result = second.run()
        self.assertTrue(result["ok"])
        self.assertEqual(second.source_browser_calls, 0)
        with Session(self.engine) as session:
            accounts = session.exec(select(AccountModel)).all()
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0].id, account_id)
        self.assertEqual(accounts[0].email, TARGET_EMAIL)

    def test_mailbox_finalize_failure_recovers_locally_without_second_login(self):
        account_id = self._add_account()
        row_id = self._add_change(account_id)
        first = self._service(row_id)
        first.fail_finalize_count = 1

        with self.assertRaises(email_change.EmailChangeError) as caught:
            first.run()

        self.assertEqual(caught.exception.code, "mailbox_finalize_failed")
        with Session(self.engine) as session:
            account = session.get(AccountModel, account_id)
            row = session.get(ChatGPTEmailChangeModel, row_id)
        self.assertEqual(account.email, TARGET_EMAIL)
        self.assertTrue(row.committed_at)
        self.assertEqual(row.status, "partial")

        second = self._service(row_id)
        result = second.run()
        self.assertTrue(result["recovered"])
        self.assertEqual(second.source_browser_calls, 0)
        self.assertEqual(second.target_login_calls, 0)
        self.assertEqual(second.finalize_calls, 1)

    def test_target_me_user_mismatch_is_rejected(self):
        account_id = self._add_account()
        row_id = self._add_change(account_id, remote_changed=True, status="partial")
        service = email_change.ChatGPTEmailChangeService(task_id="identity-user", row_id=row_id)
        response = mock.Mock(status_code=200)
        response.json.return_value = {
            "object": "user",
            "id": "user-other",
            "email": TARGET_EMAIL,
            "orgs": {"data": [{"id": SOURCE_ORG_ID, "is_default": True}]},
        }
        with mock.patch("requests.get", return_value=response):
            with self.assertRaises(email_change.EmailChangeIdentityMismatch):
                service._verify_remote_identity(
                    {"access_token": _jwt(), "cookie_header": "session=x"},
                    expected_email=TARGET_EMAIL,
                    expected_user_id=SOURCE_USER_ID,
                    expected_account_id=SOURCE_ACCOUNT_ID,
                    expected_organization_id=SOURCE_ORG_ID,
                    context_label="目标邮箱登录",
                    remote_changed=True,
                )

    def test_target_jwt_account_id_mismatch_is_rejected(self):
        account_id = self._add_account()
        row_id = self._add_change(account_id, remote_changed=True, status="partial")
        service = email_change.ChatGPTEmailChangeService(task_id="identity-account", row_id=row_id)
        response = mock.Mock(status_code=200)
        response.json.return_value = {
            "object": "user",
            "id": SOURCE_USER_ID,
            "email": TARGET_EMAIL,
            "orgs": {"data": [{"id": SOURCE_ORG_ID, "is_default": True}]},
        }
        with mock.patch("requests.get", return_value=response):
            with self.assertRaises(email_change.EmailChangeIdentityMismatch):
                service._verify_remote_identity(
                    {
                        "access_token": _jwt(account_id="acct-other"),
                        "cookie_header": "session=x",
                    },
                    expected_email=TARGET_EMAIL,
                    expected_user_id=SOURCE_USER_ID,
                    expected_account_id=SOURCE_ACCOUNT_ID,
                    expected_organization_id=SOURCE_ORG_ID,
                    context_label="目标邮箱登录",
                    remote_changed=True,
                )

    def test_real_mailbox_finalize_is_called_after_local_commit(self):
        account_id = self._add_account()
        task_id = "finalize-real"
        row_id = self._add_change(
            account_id,
            remote_changed=True,
            status="partial",
            task_id=task_id,
        )
        with Session(self.engine) as session:
            account = session.get(AccountModel, account_id)
            account.email = TARGET_EMAIL
            extra = account.get_extra()
            extra["chatgpt_email_change"] = {
                "task_id": task_id,
                "source_email": SOURCE_EMAIL,
                "target_email": TARGET_EMAIL,
            }
            account.set_extra(extra)
            session.add(account)
            session.commit()

        restored = mock.Mock()
        finalized_state = _mailbox_state(TARGET_EMAIL)
        finalized_state["before_ids"] = ["finalized"]
        restored.export_state.return_value = finalized_state
        with mock.patch.object(email_change, "RestoredEmailService", return_value=restored):
            service = email_change.ChatGPTEmailChangeService(task_id=task_id, row_id=row_id)
            result_state = service._finalize_target_mailbox(
                account_id=account_id,
                target_email=TARGET_EMAIL,
                mailbox_state=_mailbox_state(TARGET_EMAIL),
            )

        restored.finalize_success.assert_called_once_with(
            account_email=TARGET_EMAIL,
            task_id=task_id,
        )
        self.assertEqual(result_state["before_ids"], ["finalized"])
        with Session(self.engine) as session:
            account = session.get(AccountModel, account_id)
            row = session.get(ChatGPTEmailChangeModel, row_id)
        self.assertEqual(
            account.get_extra()["chatgpt_mailbox_state"]["before_ids"],
            ["finalized"],
        )
        self.assertTrue(row.mailbox_finalized_at)

    def test_reservation_release_allowed_only_before_verify_boundary(self):
        account_id = self._add_account()
        releasable_id = self._add_change(
            account_id,
            status="created",
            task_id="",
        )
        with Session(self.engine) as session:
            releasable = session.get(ChatGPTEmailChangeModel, releasable_id)
            reservation_ref = releasable.reservation_ref
        with mock.patch.object(
            tasks,
            "release_target_mailbox",
            return_value=_mailbox_state(TARGET_EMAIL),
        ) as release:
            payload = tasks.release_email_change_reservation(reservation_ref)
        self.assertEqual(payload["status"], "released")
        release.assert_called_once()

        blocked_id = self._add_change(
            account_id,
            status="partial",
            phase=email_change.PHASE_RECOVERY_REQUIRED,
            verify_submitted=True,
            target_email="target-2@example.net",
        )
        with Session(self.engine) as session:
            blocked = session.get(ChatGPTEmailChangeModel, blocked_id)
            blocked_ref = blocked.reservation_ref
        with mock.patch.object(tasks, "release_target_mailbox") as blocked_release:
            with self.assertRaises(HTTPException) as caught:
                tasks.release_email_change_reservation(blocked_ref)
        self.assertEqual(caught.exception.status_code, 409)
        blocked_release.assert_not_called()

    def test_active_reservations_are_unique_by_account_and_target(self):
        first_account = self._add_account()
        second_account = self._add_account(
            email="source-2@example.com",
            user_id="user-2",
            account_identity="acct-2",
        )
        self._add_change(first_account, status="created", task_id="")

        with self.assertRaises(IntegrityError):
            self._add_change(
                first_account,
                status="created",
                task_id="",
                target_email="other-target@example.net",
            )
        with self.assertRaises(IntegrityError):
            self._add_change(
                second_account,
                status="created",
                task_id="",
                target_email=TARGET_EMAIL.upper(),
            )

    def test_operator_payloads_never_include_mailbox_credentials(self):
        account_id = self._add_account()
        secret = "super-secret-mailbox-key"
        row_id = self._add_change(
            account_id,
            status="created",
            task_id="",
            state=_mailbox_state(TARGET_EMAIL, secret=secret),
        )
        with Session(self.engine) as session:
            row = session.get(ChatGPTEmailChangeModel, row_id)
            payload = tasks._email_change_row_payload(row)
            meta = tasks._email_change_meta(row)

        serialized = json.dumps({"payload": payload, "meta": meta})
        self.assertNotIn(secret, serialized)
        self.assertNotIn("tempmail_api_key", serialized)
        self.assertNotIn("target_mailbox_state_json", serialized)
        with Session(self.engine) as session:
            row = session.get(ChatGPTEmailChangeModel, row_id)
        self.assertIn(secret, row.target_mailbox_state_json)

    def test_proxy_resolution_freezes_one_candidate_without_midflow_failover(self):
        request = tasks.EmailChangeTaskRequest(
            account_id=1,
            target_mailbox_ref="manual_email_otp:target@example.net",
        )
        self.assertEqual(request.proxy_mode, "global")
        with mock.patch.object(
            tasks,
            "_build_custom_email_recheck_candidate_proxies",
            return_value=[
                ("http://first:8080", object(), "pool"),
                ("http://second:8080", object(), "pool"),
            ],
        ):
            proxy, source = tasks._resolve_email_change_runtime_proxy(
                {"proxy_mode": "pool"}
            )
        self.assertEqual(proxy, "http://first:8080")
        self.assertEqual(source, "pool")

    def test_explicit_preverify_release_is_reusable_even_after_otp_polling(self):
        outcome = HmeReadyMailbox._classify_helper_failure_outcome(
            "operator_released_before_remote_verify",
            lease_id="lease-1",
            wait_started=True,
        )
        self.assertEqual(outcome, "early_failure")

    def test_startup_moves_stale_remote_tasks_to_recovery(self):
        account_id = self._add_account()
        row_id = self._add_change(
            account_id,
            status="running",
            verify_submitted=True,
        )

        counts = tasks.interrupt_stale_email_change_tasks()

        self.assertEqual(counts["partial"], 1)
        with Session(self.engine) as session:
            row = session.get(ChatGPTEmailChangeModel, row_id)
        self.assertEqual(row.status, "partial")
        self.assertEqual(row.phase, email_change.PHASE_RECOVERY_REQUIRED)
        self.assertTrue(row.resumable)


if __name__ == "__main__":
    unittest.main()
