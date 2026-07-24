import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sqlmodel import Session, SQLModel, create_engine, select

from core import db as core_db
from core.base_platform import Account, AccountStatus
from core.db import AccountModel


class SaveAccountCanonicalTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "save_account_canonical.db"
        self.engine = create_engine(f"sqlite:///{db_path}")
        self.core_engine_patch = mock.patch.object(core_db, "engine", self.engine)
        self.core_engine_patch.start()
        SQLModel.metadata.create_all(self.engine)
        core_db.init_db()
        self.status_refresh_patch = mock.patch(
            "services.chatgpt_core.local_status_refresh.schedule_chatgpt_local_status_refresh_for_account_id"
        )
        self.status_refresh_patch.start()

    def tearDown(self):
        self.status_refresh_patch.stop()
        self.core_engine_patch.stop()
        self._tmpdir.cleanup()

    def _insert_row(
        self,
        *,
        password: str,
        token: str,
        extra: dict,
        email: str = "canonical@example.com",
    ) -> int:
        with Session(self.engine) as session:
            row = AccountModel(
                platform="chatgpt",
                email=email,
                password=password,
                token=token,
                status="registered",
                extra_json=json.dumps(extra),
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return int(row.id or 0)

    def _incoming_account(self, *, password: str = "new-pw", token: str = "at-new") -> Account:
        return Account(
            platform="chatgpt",
            email="canonical@example.com",
            password=password,
            user_id="acct-current",
            token=token,
            status=AccountStatus.REGISTERED,
            extra={
                "access_token": token,
                "refresh_token": "rt-current",
                "account_id": "acct-current",
                "workspace_id": "ws-current",
            },
        )

    def _rows(self) -> list[AccountModel]:
        with Session(self.engine) as session:
            return list(
                session.exec(
                    select(AccountModel).order_by(AccountModel.id.asc())
                ).all()
            )

    def test_legacy_product_variants_are_retained_and_canonical_row_is_reused(self):
        legacy_ids = [
            self._insert_row(
                password="k12-pw",
                token="at-k12",
                extra={
                    "chatgpt_workspace_variant_key": "k12:ws-k12",
                    "chatgpt_workspace_scope": "k12",
                    "workspace_id": "ws-k12",
                },
            ),
            self._insert_row(
                password="business-pw",
                token="at-business",
                extra={
                    "chatgpt_workspace_variant_key": "business:ws-business",
                    "chatgpt_workspace_scope": "business",
                    "workspace_id": "ws-business",
                },
            ),
        ]

        first = core_db.save_account(self._incoming_account())
        second = core_db.save_account(
            self._incoming_account(password="newer-pw", token="at-newer")
        )

        rows = self._rows()
        self.assertEqual(len(rows), 3)
        self.assertNotIn(first.id, legacy_ids)
        self.assertEqual(second.id, first.id)
        canonical = next(row for row in rows if row.id == first.id)
        self.assertEqual(canonical.password, "newer-pw")
        self.assertEqual(canonical.token, "at-newer")
        self.assertEqual(canonical.get_extra()["workspace_id"], "ws-current")

        retained = {row.id: row for row in rows if row.id in legacy_ids}
        self.assertEqual(set(retained), set(legacy_ids))
        self.assertEqual(retained[legacy_ids[0]].token, "at-k12")
        self.assertEqual(retained[legacy_ids[1]].token, "at-business")

    def test_existing_row_without_variant_is_updated_in_place(self):
        row_id = self._insert_row(
            password="old-pw",
            token="at-old",
            extra={"workspace_id": "ws-old", "account_id": "acct-old"},
        )

        saved = core_db.save_account(self._incoming_account())

        self.assertEqual(saved.id, row_id)
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].password, "new-pw")
        self.assertEqual(rows[0].get_extra()["account_id"], "acct-current")

    def test_free_or_personal_variant_is_promoted_to_canonical_row(self):
        for scope in ("free", "personal"):
            with self.subTest(scope=scope):
                email = f"{scope}@example.com"
                row_id = self._insert_row(
                    email=email,
                    password="old-pw",
                    token="at-old",
                    extra={
                        "chatgpt_workspace_variant_key": f"{scope}:ws-old",
                        "chatgpt_workspace_scope": scope,
                        "workspace_id": "ws-old",
                    },
                )
                incoming = self._incoming_account()
                incoming.email = email

                saved = core_db.save_account(incoming)

                self.assertEqual(saved.id, row_id)
                with Session(self.engine) as session:
                    rows = list(
                        session.exec(
                            select(AccountModel).where(AccountModel.email == email)
                        ).all()
                    )
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0].token, "at-new")
                saved_extra = rows[0].get_extra()
                self.assertNotIn("chatgpt_workspace_variant_key", saved_extra)
                self.assertNotIn("chatgpt_workspace_scope", saved_extra)


if __name__ == "__main__":
    unittest.main()
