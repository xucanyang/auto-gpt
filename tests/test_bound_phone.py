import tempfile
import unittest
from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine

from core import db as core_db
from core.db import AccountModel
from services.chatgpt_core import bound_phone


class BoundPhonePersistenceTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "bound_phone.db"
        self.engine = create_engine(f"sqlite:///{db_path}")
        self.core_engine = core_db.engine
        self.bound_engine = bound_phone.core_db.engine
        core_db.engine = self.engine
        bound_phone.core_db.engine = self.engine
        SQLModel.metadata.create_all(self.engine)
        core_db.init_db()

    def tearDown(self):
        bound_phone.core_db.engine = self.bound_engine
        core_db.engine = self.core_engine
        self._tmpdir.cleanup()

    def _add_account(self, email="alive@example.com", extra_json="{}"):
        with Session(self.engine) as session:
            row = AccountModel(
                platform="chatgpt",
                email=email,
                password="pw",
                token="",
                status="invalid",
                extra_json=extra_json,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return int(row.id or 0)

    def _extra(self, account_id):
        with Session(self.engine) as session:
            return session.get(AccountModel, account_id).get_extra()

    def test_records_full_bound_phone_by_account_id(self):
        account_id = self._add_account()

        result = bound_phone.upsert_chatgpt_bound_phone(
            account_id=account_id,
            phone="+1 (613) 465-5704",
            source="oauth_session.phone_number",
            reason="existing_phone_otp",
        )

        self.assertTrue(result["updated"])
        extra = self._extra(account_id)
        payload = extra["chatgpt_bound_phone"]
        self.assertEqual(payload["phone"], "+16134655704")
        self.assertEqual(extra["chatgpt_bound_phone_number"], "+16134655704")

    def test_masked_does_not_overwrite_existing_full_phone(self):
        account_id = self._add_account(
            extra_json='{"chatgpt_bound_phone":{"phone":"+16134655704","masked":"","source":"old","detected_at":"old"}}',
        )

        result = bound_phone.upsert_chatgpt_bound_phone(
            account_id=account_id,
            masked="phone ending in 9999",
            source="state.payload.message",
        )

        self.assertTrue(result["updated"])
        extra = self._extra(account_id)
        payload = extra["chatgpt_bound_phone"]
        self.assertEqual(payload["phone"], "+16134655704")
        self.assertEqual(payload["last_masked_seen"], "phone ending in 9999")

    def test_full_phone_replaces_masked_and_keeps_history(self):
        account_id = self._add_account(
            extra_json='{"chatgpt_bound_phone":{"phone":"","masked":"•••• 5704","source":"old","detected_at":"old"}}',
        )

        bound_phone.upsert_chatgpt_bound_phone(
            account_id=account_id,
            phone="0016134655704",
            source="oauth_session.phone_number",
        )

        extra = self._extra(account_id)
        payload = extra["chatgpt_bound_phone"]
        self.assertEqual(payload["phone"], "+16134655704")
        self.assertEqual(extra["chatgpt_bound_phone_history"][0]["masked"], "•••• 5704")


    def test_records_add_phone_unbound_challenge(self):
        account_id = self._add_account()

        result = bound_phone.upsert_chatgpt_phone_challenge(
            account_id=account_id,
            challenge_type="add_phone",
            status="unbound_required",
            source="custom_email_recheck",
            message="命中 add_phone，账号尚未绑定手机号",
            allow_add_phone_verification=False,
            allow_existing_phone_verification=True,
        )

        self.assertTrue(result["updated"])
        extra = self._extra(account_id)
        payload = extra["chatgpt_phone_challenge"]
        self.assertEqual(payload["type"], "add_phone")
        self.assertEqual(payload["status"], "unbound_required")
        self.assertEqual(payload["display"], "未绑定手机号")
        self.assertNotIn("chatgpt_bound_phone", extra)


if __name__ == "__main__":
    unittest.main()
