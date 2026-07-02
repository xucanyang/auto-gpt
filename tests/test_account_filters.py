import unittest
from datetime import datetime, timezone
from unittest import mock

try:
    from sqlalchemy import text
    from sqlmodel import Session, select

    from core.db import AccountListStateModel, AccountModel, engine, init_db
    from services.account_filters import (
        account_revival_info,
        account_revival_state,
        apply_account_list_state_filters,
        apply_account_list_state_sort,
        delete_account_list_state_for_account_ids,
        filter_account_rows,
        refresh_account_list_state,
        refresh_stale_account_list_state,
        sort_account_rows,
        upsert_account_list_state_for_account_ids,
    )

    HAS_DB_DEPS = True
except ModuleNotFoundError as exc:
    if exc.name != "sqlmodel":
        raise
    AccountListStateModel = None
    AccountModel = None
    Session = None
    engine = None
    init_db = None
    account_revival_info = None
    account_revival_state = None
    apply_account_list_state_filters = None
    apply_account_list_state_sort = None
    delete_account_list_state_for_account_ids = None
    filter_account_rows = None
    refresh_account_list_state = None
    refresh_stale_account_list_state = None
    select = None
    sort_account_rows = None
    text = None
    upsert_account_list_state_for_account_ids = None
    HAS_DB_DEPS = False


@unittest.skipUnless(HAS_DB_DEPS, "sqlmodel is not installed in this environment")
class AccountFilterSortTests(unittest.TestCase):
    def _account(self, account_id: int, expires_at="") -> AccountModel:
        account = AccountModel(
            id=account_id,
            platform="chatgpt",
            email=f"user{account_id}@example.com",
            password="",
        )
        if expires_at:
            account.set_extra(
                {
                    "chatgpt_local": {
                        "subscription": {
                            "subscription_active_until": expires_at,
                        },
                    },
                }
            )
        return account

    def test_sort_subscription_active_until_ascending_with_empty_last(self):
        rows = [
            self._account(1, ""),
            self._account(2, "1781089634000"),
            self._account(3, "2026-05-01T00:00:00+00:00"),
            self._account(4, "1781089634"),
        ]

        sorted_rows = sort_account_rows(rows, sort_by="subscription_active_until", sort_order="asc")

        self.assertEqual([row.id for row in sorted_rows], [3, 2, 4, 1])

    def test_sort_subscription_active_until_descending_with_empty_last(self):
        rows = [
            self._account(1, ""),
            self._account(2, "2026-05-01T00:00:00+00:00"),
            self._account(3, "1781089634"),
        ]

        sorted_rows = sort_account_rows(rows, sort_by="subscription_active_until", sort_order="descend")

        self.assertEqual([row.id for row in sorted_rows], [3, 2, 1])

    def test_revival_info_infers_legacy_invalid_recheck_success(self):
        account = self._account(10)
        account.set_extra(
            {
                "chatgpt_invalid_recheck": {
                    "status": "recovered_access_token",
                    "source": "invalid_account_recheck",
                    "task_id": "task-recheck",
                    "checked_at": "2026-06-12T04:00:00+00:00",
                    "has_access_token": True,
                },
            }
        )

        info = account_revival_info(account)

        self.assertEqual(info["state"], "revived")
        self.assertEqual(info["kind"], "invalid_recheck")
        self.assertEqual(info["label"], "失效测活恢复")
        self.assertEqual(info["at"], "2026-06-12T04:00:00+00:00")
        self.assertTrue(info["legacy_inferred"])

    def test_revival_info_uses_explicit_marker_when_present(self):
        account = self._account(11)
        account.set_extra(
            {
                "chatgpt_last_revival": {
                    "source": "custom_email_recheck",
                    "mode": "create_new",
                    "task_id": "task-custom",
                    "revived_at": "2026-06-12T05:00:00+00:00",
                    "auth_level": "access_token_only",
                },
            }
        )

        info = account_revival_info(account)

        self.assertEqual(info["state"], "recovery_new")
        self.assertEqual(info["kind"], "custom_email_recheck_new")
        self.assertEqual(info["label"], "邮箱测活新建")
        self.assertFalse(info["legacy_inferred"])

    def test_filter_account_rows_supports_revival_state(self):
        legacy_revived = self._account(20)
        legacy_revived.set_extra(
            {
                "chatgpt_invalid_recheck": {
                    "status": "recovered_access_token",
                    "source": "invalid_account_recheck",
                    "task_id": "task-recheck",
                    "checked_at": "2026-06-12T04:00:00+00:00",
                    "has_access_token": True,
                },
            }
        )
        recovery_new = self._account(21)
        recovery_new.set_extra(
            {
                "chatgpt_last_revival": {
                    "source": "custom_email_recheck",
                    "mode": "create_new",
                    "revived_at": "2026-06-12T05:00:00+00:00",
                },
            }
        )
        normal = self._account(22)

        self.assertEqual(account_revival_state(legacy_revived), "revived")
        self.assertEqual(account_revival_state(recovery_new), "recovery_new")
        self.assertEqual(account_revival_state(normal), "none")

        revived_rows = filter_account_rows([legacy_revived, recovery_new, normal], revival_state="revived")
        self.assertEqual([row.id for row in revived_rows], [20])

        recovery_new_rows = filter_account_rows([legacy_revived, recovery_new, normal], revival_state="recovery_new")
        self.assertEqual([row.id for row in recovery_new_rows], [21])

    def test_account_list_state_sql_filters_match_python_filters(self):
        init_db()
        rows = [
            self._account(101, "2026-05-01T00:00:00+00:00"),
            self._account(102, "1781089634000"),
            self._account(103, ""),
        ]
        rows[0].token = ""
        rows[0].set_extra(
            {
                "refresh_token": "rt-101",
                "chatgpt_capabilities": {"subscription_plan": "plus"},
                "sync_statuses": {"sub2api": {"remote_state": "exists"}},
            }
        )
        rows[1].token = "at-102"
        rows[1].set_extra(
            {
                "chatgpt_local": {
                    "auth": {"state": "unauthorized"},
                    "subscription": {"plan": "team", "subscription_active_until": "1781089634000"},
                },
                "sync_statuses": {"sub2api": {"remote_state": "not_found"}},
                "chatgpt_last_revival": {"source": "custom_email_recheck", "mode": "create_new"},
            }
        )
        rows[2].set_extra(
            {
                "manually_used": True,
                "chatgpt_workspace_scope": "free",
                "chatgpt_invalid_recheck": {
                    "status": "recovered_access_token",
                    "source": "invalid_account_recheck",
                    "task_id": "task-recheck",
                    "has_access_token": True,
                },
            }
        )

        with Session(engine) as session:
            session.exec(text("DELETE FROM account_list_state"))
            session.exec(text("DELETE FROM accounts"))
            for row in rows:
                session.add(row)
            session.commit()
            refresh_account_list_state(session)

            def sql_ids(**filters):
                q = select(AccountModel).join(
                    AccountListStateModel,
                    AccountListStateModel.account_id == AccountModel.id,
                )
                q = apply_account_list_state_filters(q, **filters)
                q = q.order_by(AccountModel.id.asc())
                return [int(row.id or 0) for row in session.exec(q).all()]

            self.assertEqual(
                sql_ids(auth_type="refresh_token"),
                [row.id for row in filter_account_rows(rows, auth_type="refresh_token")],
            )
            self.assertEqual(
                sql_ids(subscription_type="team,free"),
                [row.id for row in filter_account_rows(rows, subscription_type="team,free")],
            )
            self.assertEqual(
                sql_ids(account_validity_filter="invalid"),
                [row.id for row in filter_account_rows(rows, account_validity_filter="invalid")],
            )
            self.assertEqual(
                sql_ids(sub2api_state="unknown"),
                [row.id for row in filter_account_rows(rows, sub2api_state="unknown")],
            )
            self.assertEqual(
                sql_ids(revival_state="recovery_new"),
                [row.id for row in filter_account_rows(rows, revival_state="recovery_new")],
            )

    def test_account_list_state_stale_refresh_only_updates_changed_rows(self):
        init_db()
        fresh = self._account(301)
        stale = self._account(302)
        stale.set_extra({"chatgpt_capabilities": {"subscription_plan": "free"}})

        with Session(engine) as session:
            session.exec(text("DELETE FROM account_list_state"))
            session.exec(text("DELETE FROM accounts"))
            session.add(fresh)
            session.add(stale)
            session.commit()
            refresh_account_list_state(session)

            fresh_source_updated_at = session.exec(
                text("SELECT CAST(updated_at AS TEXT) FROM accounts WHERE id = 301")
            ).one()
            fresh_source_updated_at = fresh_source_updated_at[0]
            session.exec(
                text(
                    """
                    UPDATE account_list_state
                    SET refreshed_at = 'keep-fresh',
                        source_updated_at = :source_updated_at
                    WHERE account_id = 301
                    """
                ),
                params={"source_updated_at": fresh_source_updated_at},
            )
            session.exec(
                text(
                    """
                    UPDATE account_list_state
                    SET refreshed_at = 'replace-stale',
                        source_updated_at = '2000-01-01 00:00:00'
                    WHERE account_id = 302
                    """
                )
            )
            session.commit()

            stale_row = session.get(AccountModel, 302)
            stale_row.set_extra({"chatgpt_capabilities": {"subscription_plan": "plus"}})
            stale_row.updated_at = datetime(2026, 6, 17, 12, 0, 0, tzinfo=timezone.utc)
            session.add(stale_row)
            session.commit()

            refreshed = refresh_stale_account_list_state(session)
            self.assertEqual(refreshed, 1)

            states = {
                int(row[0]): (row[1], row[2])
                for row in session.exec(
                    text(
                        """
                        SELECT account_id, subscription_type, refreshed_at
                        FROM account_list_state
                        WHERE account_id IN (301, 302)
                        ORDER BY account_id
                        """
                    )
                ).all()
            }

        self.assertEqual(states[301], ("unknown", "keep-fresh"))
        self.assertEqual(states[302][0], "plus")
        self.assertNotEqual(states[302][1], "replace-stale")

    def test_account_list_state_write_point_upsert_and_delete(self):
        init_db()
        row = self._account(401)
        row.set_extra({"manually_used": False})

        with Session(engine) as session:
            session.exec(text("DELETE FROM account_list_state"))
            session.exec(text("DELETE FROM accounts"))
            session.add(row)
            session.commit()

            upsert_account_list_state_for_account_ids(session, [401])
            state = session.get(AccountListStateModel, 401)
            self.assertIsNotNone(state)
            self.assertFalse(bool(state.manually_used))

            account = session.get(AccountModel, 401)
            account.set_extra({"manually_used": True})
            account.updated_at = datetime(2026, 6, 17, 13, 0, 0, tzinfo=timezone.utc)
            session.add(account)
            upsert_account_list_state_for_account_ids(session, [401], commit=False)
            session.commit()

            state = session.get(AccountListStateModel, 401)
            self.assertTrue(bool(state.manually_used))

            delete_account_list_state_for_account_ids(session, [401])
            self.assertIsNone(session.get(AccountListStateModel, 401))

    def test_account_list_state_write_point_helpers_noop_for_mock_session(self):
        fake_session = mock.Mock()

        self.assertEqual(upsert_account_list_state_for_account_ids(fake_session, [999], commit=False), 0)
        self.assertEqual(delete_account_list_state_for_account_ids(fake_session, [999], commit=False), 0)

        fake_session.exec.assert_not_called()

    def test_account_list_state_schema_upgrade_adds_missing_columns(self):
        init_db()
        row = self._account(501)
        row.set_extra({"chatgpt_capabilities": {"subscription_plan": "plus"}})

        with Session(engine) as session:
            session.exec(text("DROP TABLE IF EXISTS account_list_state"))
            session.exec(
                text(
                    """
                    CREATE TABLE account_list_state (
                        account_id INTEGER PRIMARY KEY,
                        platform TEXT NOT NULL DEFAULT '',
                        manually_used INTEGER NOT NULL DEFAULT 0,
                        auth_type TEXT NOT NULL DEFAULT 'unknown'
                    )
                    """
                )
            )
            session.exec(text("DELETE FROM accounts"))
            session.add(row)
            session.commit()

            refreshed = refresh_account_list_state(session)
            columns = {
                str(info[1])
                for info in session.exec(text("PRAGMA table_info(account_list_state)")).all()
            }
            state = session.get(AccountListStateModel, 501)

        self.assertEqual(refreshed, 1)
        self.assertIn("source_updated_at", columns)
        self.assertIn("subscription_active_until_ts", columns)
        self.assertEqual(state.subscription_type, "plus")

    def test_account_list_state_sql_sort_subscription_active_until_empty_last(self):
        init_db()
        rows = [
            self._account(201, ""),
            self._account(202, "1781089634000"),
            self._account(203, "2026-05-01T00:00:00+00:00"),
            self._account(204, "1781089634"),
        ]
        with Session(engine) as session:
            session.exec(text("DELETE FROM account_list_state"))
            session.exec(text("DELETE FROM accounts"))
            for row in rows:
                session.add(row)
            session.commit()
            refresh_account_list_state(session)

            q = select(AccountModel).join(
                AccountListStateModel,
                AccountListStateModel.account_id == AccountModel.id,
            )
            asc_ids = [
                int(row.id or 0)
                for row in session.exec(
                    apply_account_list_state_sort(q, sort_by="subscription_active_until", sort_order="asc")
                ).all()
            ]
            desc_ids = [
                int(row.id or 0)
                for row in session.exec(
                    apply_account_list_state_sort(q, sort_by="subscription_active_until", sort_order="descend")
                ).all()
            ]

        self.assertEqual(asc_ids, [203, 204, 202, 201])
        self.assertEqual(desc_ids, [204, 202, 203, 201])


if __name__ == "__main__":
    unittest.main()
