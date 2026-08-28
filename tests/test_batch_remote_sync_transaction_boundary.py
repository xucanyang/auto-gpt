import unittest
from types import SimpleNamespace
from unittest import mock

from api import actions as actions_api
from core.db import AccountModel
from services import chatgpt_sync, oaipay_sync, sub2api_sync


class BatchRemoteSyncTransactionBoundaryTests(unittest.TestCase):
    @staticmethod
    def _accounts():
        return [
            SimpleNamespace(id=1, email="first@example.com", status="registered"),
            SimpleNamespace(id=2, email="second@example.com", status="registered"),
        ]

    def _assert_all_remote_probes_precede_writes(
        self,
        *,
        execute,
        probe_path,
        update_path,
    ):
        events = []
        session = mock.Mock()
        accounts = self._accounts()

        def probe(account):
            events.append(f"probe:{account.id}")
            return {"remote_state": "not_found", "status": ""}

        def update(account, sync_result, *, session: object, commit: bool):
            self.assertEqual(sync_result["remote_state"], "not_found")
            self.assertIsNotNone(session)
            self.assertFalse(commit)
            events.append(f"write:{account.id}")

        with mock.patch(probe_path, side_effect=probe), mock.patch(
            update_path,
            side_effect=update,
        ):
            result = execute(accounts, session)

        self.assertEqual(events, ["probe:1", "probe:2", "write:1", "write:2"])
        self.assertEqual(
            session.refresh.call_args_list,
            [mock.call(accounts[0]), mock.call(accounts[1])],
        )
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["success"], 2)
        self.assertEqual(result["failed"], 0)

    def test_oaipay_remote_phase_finishes_before_first_sqlite_write(self):
        self._assert_all_remote_probes_precede_writes(
            execute=actions_api._execute_batch_oaipay_sync,
            probe_path="api.actions.probe_chatgpt_oaipay_status",
            update_path="api.actions.update_account_model_oaipay_sync",
        )

    def test_sub2api_remote_phase_finishes_before_first_sqlite_write(self):
        self._assert_all_remote_probes_precede_writes(
            execute=actions_api._execute_batch_sub2api_sync,
            probe_path="api.actions.probe_chatgpt_sub2api_status",
            update_path="api.actions.update_account_model_sub2api_sync",
        )

    def test_single_sub2api_action_forwards_temporary_connection_without_committing(self):
        account = self._accounts()[0]
        session = mock.Mock()
        outcome = {
            "ok": True,
            "message": "uploaded",
            "results": [{"name": "Sub2API 上传", "ok": True}],
        }

        with mock.patch(
            "api.actions.backfill_chatgpt_account_to_sub2api",
            return_value=outcome,
        ) as upload, mock.patch("api.actions._apply_action_result") as apply_result:
            result = actions_api._execute_platform_action(
                object(),
                "chatgpt",
                account,
                "upload_sub2api",
                {
                    "api_url": " https://sub2api.example.test/ ",
                    "api_key": " temporary-secret ",
                    "group_ids": [7, 9],
                },
                session,
            )

        upload.assert_called_once_with(
            account,
            session=session,
            commit=False,
            api_url="https://sub2api.example.test/",
            api_key="temporary-secret",
            group_ids=[7, 9],
        )
        apply_result.assert_called_once_with(
            "chatgpt",
            "upload_sub2api",
            account,
            result,
            session,
        )
        self.assertTrue(result["ok"])
        session.commit.assert_not_called()

    def test_remote_sync_action_uses_dedicated_normalizer_and_drops_only_its_raw_patch(self):
        cases = (
            (
                "sync_cliproxyapi_status",
                "cliproxyapi",
                "api.actions.update_account_model_cliproxy_sync",
            ),
            (
                "sync_sub2api_status",
                "sub2api",
                "api.actions.update_account_model_sub2api_sync",
            ),
            (
                "sync_oaipay_status",
                "oaipay",
                "api.actions.update_account_model_oaipay_sync",
            ),
        )
        sync_result = {
            "remote_state": "exists",
            "status": "active",
            "message": "remote result",
        }

        for action_id, sync_key, updater_path in cases:
            with self.subTest(action_id=action_id):
                account = AccountModel(
                    id=7,
                    platform="chatgpt",
                    email="sync@example.com",
                    password="password",
                    status="registered",
                )
                account.set_extra(
                    {
                        "sync_statuses": {
                            sync_key: {"state": "before"},
                            "unrelated_sync": {"state": "keep"},
                        },
                        "existing": {"state": "keep"},
                    }
                )
                session = mock.Mock()

                def normalize(row, value, *, session, commit):
                    self.assertIs(row, account)
                    self.assertEqual(value, sync_result)
                    self.assertFalse(commit)
                    extra = row.get_extra()
                    extra.setdefault("sync_statuses", {})[sync_key] = {
                        "state": "normalized",
                    }
                    row.set_extra(extra)

                result = {
                    "ok": True,
                    "data": {"sync": sync_result},
                    "account_extra_patch": {
                        "sync_statuses": {
                            sync_key: {"state": "raw-must-not-win"},
                            "unrelated_sync": {"state": "patched"},
                        },
                        "unrelated_patch": {"state": "preserved"},
                    },
                }

                with mock.patch(updater_path, side_effect=normalize) as updater:
                    actions_api._apply_action_result(
                        "chatgpt",
                        action_id,
                        account,
                        result,
                        session,
                    )

                updater.assert_called_once_with(
                    account,
                    sync_result,
                    session=session,
                    commit=False,
                )
                extra = account.get_extra()
                self.assertEqual(
                    extra["sync_statuses"][sync_key],
                    {"state": "normalized"},
                )
                self.assertEqual(
                    extra["sync_statuses"]["unrelated_sync"],
                    {"state": "patched"},
                )
                self.assertEqual(
                    extra["unrelated_patch"],
                    {"state": "preserved"},
                )
                self.assertEqual(extra["existing"], {"state": "keep"})
                session.commit.assert_not_called()

    def test_remote_sync_normalizers_refresh_list_state_in_the_same_transaction(self):
        cases = (
            chatgpt_sync.update_account_model_cliproxy_sync,
            sub2api_sync.update_account_model_sub2api_sync,
            oaipay_sync.update_account_model_oaipay_sync,
        )

        for updater in cases:
            with self.subTest(updater=updater.__name__):
                account = AccountModel(
                    id=11,
                    platform="chatgpt",
                    email="list-state@example.com",
                    password="password",
                    status="registered",
                )
                account.set_extra({})
                session = mock.Mock()
                sync_result = {
                    "remote_state": "exists",
                    "status": "active",
                }

                with mock.patch(
                    "services.account_filters.upsert_account_list_state_for_account_ids"
                ) as upsert:
                    updater(
                        account,
                        sync_result,
                        session=session,
                        commit=False,
                    )

                upsert.assert_called_once_with(session, [account.id], commit=False)
                session.commit.assert_not_called()
                session.refresh.assert_not_called()


if __name__ == "__main__":
    unittest.main()
