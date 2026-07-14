import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sqlmodel import Session, SQLModel, create_engine

from core import db as core_db
from core.base_platform import Account, AccountStatus
from core.db import AccountModel


class SaveAccountWebSessionPreservationTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "save_account_web_session.db"
        self.engine = create_engine(f"sqlite:///{db_path}")
        self.core_engine_patch = mock.patch.object(core_db, "engine", self.engine)
        self.core_engine_patch.start()
        SQLModel.metadata.create_all(self.engine)
        core_db.init_db()

    def tearDown(self):
        self.core_engine_patch.stop()
        self._tmpdir.cleanup()

    def test_chatgpt_update_does_not_blank_existing_web_session_material(self):
        with Session(self.engine) as session:
            row = AccountModel(
                platform="chatgpt",
                email="demo@example.com",
                password="old-pw",
                token="at-old",
                status="registered",
                extra_json=json.dumps(
                    {
                        "access_token": "at-old",
                        "session_token": "session-old",
                        "cookies": "old-cookies",
                        "cookie_header": "old-cookie-header",
                    },
                    ensure_ascii=False,
                ),
            )
            session.add(row)
            session.commit()

        account = Account(
            platform="chatgpt",
            email="demo@example.com",
            password="new-pw",
            token="at-new",
            status=AccountStatus.REGISTERED,
            extra={
                "access_token": "at-new",
                "refresh_token": "rt-new",
                "session_token": "",
                "cookies": "",
                "cookie_header": "",
            },
        )

        with mock.patch(
            "services.chatgpt_core.local_status_refresh.schedule_chatgpt_local_status_refresh_for_account_id"
        ):
            saved = core_db.save_account(account)

        self.assertEqual(saved.token, "at-new")
        self.assertEqual(saved.password, "new-pw")
        extra = saved.get_extra()
        self.assertEqual(extra["access_token"], "at-new")
        self.assertEqual(extra["refresh_token"], "rt-new")
        self.assertEqual(extra["session_token"], "session-old")
        self.assertEqual(extra["cookies"], "old-cookies")
        self.assertEqual(extra["cookie_header"], "old-cookie-header")


if __name__ == "__main__":
    unittest.main()
