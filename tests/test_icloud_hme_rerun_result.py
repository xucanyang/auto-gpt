import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sqlmodel import Session, SQLModel, create_engine

from core import db as core_db
from core.db import IcloudHmeRecheckQueueModel


class IcloudHmeRerunResultTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "icloud_hme_rerun.db"
        self.engine = create_engine(f"sqlite:///{db_path}")
        self.engine_patch = mock.patch.object(core_db, "engine", self.engine)
        self.engine_patch.start()
        SQLModel.metadata.create_all(self.engine)
        core_db.init_db()

    def tearDown(self):
        self.engine_patch.stop()
        self._tmpdir.cleanup()

    def test_registered_auth_pending_does_not_claim_login_or_access_token(self):
        with Session(self.engine) as session:
            row = IcloudHmeRecheckQueueModel(
                campaign_id="campaign-pending",
                anonymous_id="anon-pending",
                hme="pending@example.com",
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            row_id = int(row.id or 0)

        result = core_db.sync_icloud_hme_rerun_result(
            anonymous_id="anon-pending",
            hme="pending@example.com",
            task_id="task-pending",
            success=True,
            saved_account_id=91,
            access_token_saved=False,
            result_code="registered_auth_pending",
        )

        self.assertEqual(result["updated"], 1)
        with Session(self.engine) as session:
            saved = session.get(IcloudHmeRecheckQueueModel, row_id)
            self.assertIsNotNone(saved)
            self.assertEqual(saved.status, "registered_auth_pending")
            self.assertEqual(saved.result_code, "registered_auth_pending")
            self.assertFalse(saved.access_token_saved)
            self.assertEqual(saved.saved_account_id, 91)
            self.assertTrue(saved.get_details()["last_rerun_auth_pending"])


if __name__ == "__main__":
    unittest.main()
