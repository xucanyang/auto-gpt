import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sqlmodel import Session, create_engine

from core import db as core_db
from core.db import PendingBusinessInviteModel, SQLModel
from platforms.chatgpt import pending_business_invites


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


if __name__ == "__main__":
    unittest.main()
