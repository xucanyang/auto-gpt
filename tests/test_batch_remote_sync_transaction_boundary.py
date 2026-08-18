import unittest
from types import SimpleNamespace
from unittest import mock

from api import actions as actions_api


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

    def test_generic_batch_commits_each_account_before_the_next_remote_action(self):
        accounts = self._accounts()
        session = mock.Mock()
        events = []

        def execute(_instance, _platform, account, _action_id, _params, _session):
            events.append(f"execute:{account.id}")
            return {"ok": True, "data": {"message": "done"}}

        session.commit.side_effect = lambda: events.append("commit")
        with mock.patch(
            "api.actions._resolve_batch_accounts",
            return_value=(accounts, []),
        ), mock.patch(
            "api.actions.ChatGPTPlatform",
            return_value=object(),
        ), mock.patch(
            "api.actions.config_store.get_all",
            return_value={},
        ), mock.patch(
            "api.actions._execute_platform_action",
            side_effect=execute,
        ):
            result = actions_api.execute_batch_action(
                "chatgpt",
                "upload_oaipay",
                actions_api.BatchActionRequest(account_ids=[1, 2]),
                session,
            )

        self.assertEqual(events, ["execute:1", "commit", "execute:2", "commit"])
        self.assertEqual(result["success"], 2)
        session.rollback.assert_not_called()

    def test_generic_batch_rolls_back_a_failed_item_before_continuing(self):
        accounts = self._accounts()
        session = mock.Mock()
        events = []

        def execute(_instance, _platform, account, _action_id, _params, _session):
            events.append(f"execute:{account.id}")
            if account.id == 1:
                raise RuntimeError("first item failed")
            return {"ok": True, "data": {"message": "done"}}

        session.rollback.side_effect = lambda: events.append("rollback")
        session.commit.side_effect = lambda: events.append("commit")
        with mock.patch(
            "api.actions._resolve_batch_accounts",
            return_value=(accounts, []),
        ), mock.patch(
            "api.actions.ChatGPTPlatform",
            return_value=object(),
        ), mock.patch(
            "api.actions.config_store.get_all",
            return_value={},
        ), mock.patch(
            "api.actions._execute_platform_action",
            side_effect=execute,
        ):
            result = actions_api.execute_batch_action(
                "chatgpt",
                "upload_sub2api",
                actions_api.BatchActionRequest(account_ids=[1, 2]),
                session,
            )

        self.assertEqual(events, ["execute:1", "rollback", "execute:2", "commit"])
        self.assertEqual(result["success"], 1)
        self.assertEqual(result["failed"], 1)


if __name__ == "__main__":
    unittest.main()
