import unittest
from unittest import mock

from core.db import AccountModel
from services.external_sync import sync_account


class DummyAccount:
    def __init__(self, *, platform="chatgpt", email="demo@example.com", token="at-token", extra=None):
        self.platform = platform
        self.email = email
        self.token = token
        self.user_id = "acct-demo"
        defaults = {
            "access_token": token,
            "refresh_token": "rt-token",
            "workspace_id": "ws-demo",
        }
        defaults.update(dict(extra or {}))
        self.extra = defaults
        self.id = None

    def get_extra(self):
        return dict(self.extra)


def _config_getter(values: dict[str, str]):
    def _get(key: str, default: str = "") -> str:
        return values.get(key, default)

    return _get


class ExternalSyncContributionModeTests(unittest.TestCase):
    def test_contribution_enabled_uploads_only_to_contribution_server(self):
        account = DummyAccount()
        cfg = {
            "contribution_enabled": "1",
            "contribution_server_url": "http://contribution.local:7317",
            "contribution_key": "pk-public-1",
            "cpa_api_url": "http://cpa.local",
            "codex_proxy_url": "http://codex.local",
            "sub2api_api_url": "http://sub2api.local",
            "sub2api_api_key": "sub2-key",
        }

        with mock.patch("core.config_store.config_store.get", side_effect=_config_getter(cfg)):
            with mock.patch("services.external_sync.upload_chatgpt_account_to_cpa", return_value=(True, "ok")) as upload_mock:
                with mock.patch("services.external_sync.persist_cpa_sync_result") as persist_mock:
                    result = sync_account(account)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "Contribution")
        self.assertTrue(result[0]["ok"])
        upload_mock.assert_called_once_with(
            account,
            api_url="http://contribution.local:7317",
            api_key="pk-public-1",
        )
        persist_mock.assert_called_once_with(account, True, "ok")

    def test_contribution_enabled_without_server_url_fails_fast(self):
        account = DummyAccount()
        cfg = {
            "contribution_enabled": "true",
            "contribution_server_url": "",
            "contribution_key": "pk-public-1",
        }

        with mock.patch("core.config_store.config_store.get", side_effect=_config_getter(cfg)):
            with mock.patch("services.external_sync.upload_chatgpt_account_to_cpa") as upload_mock:
                with mock.patch("services.external_sync.persist_cpa_sync_result") as persist_mock:
                    result = sync_account(account)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "Contribution")
        self.assertFalse(result[0]["ok"])
        self.assertIn("未配置", result[0]["msg"])
        upload_mock.assert_not_called()
        persist_mock.assert_called_once()

    def test_contribution_disabled_keeps_existing_cpa_sync(self):
        account = DummyAccount()
        cfg = {
            "contribution_enabled": "0",
            "cpa_api_url": "http://cpa.local",
        }

        with mock.patch("core.config_store.config_store.get", side_effect=_config_getter(cfg)):
            with mock.patch("services.external_sync.upload_chatgpt_account_to_cpa", return_value=(True, "ok")) as upload_mock:
                with mock.patch("services.external_sync.persist_cpa_sync_result") as persist_mock:
                    result = sync_account(account)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "CPA")
        upload_mock.assert_called_once_with(account)
        persist_mock.assert_called_once_with(account, True, "ok")

    def test_sub2api_sync_uses_backfill_pipeline(self):
        account = DummyAccount()
        cfg = {
            "contribution_enabled": "0",
            "sub2api_api_url": "http://sub2api.local",
            "sub2api_api_key": "sub2-key",
        }

        with mock.patch("core.config_store.config_store.get", side_effect=_config_getter(cfg)):
            with mock.patch(
                "services.sub2api_sync.backfill_chatgpt_account_to_sub2api",
                return_value={"ok": True, "message": "补传完成，远端账号 #12"},
            ) as backfill_mock:
                result = sync_account(account)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "Sub2API")
        self.assertTrue(result[0]["ok"])
        self.assertIn("补传完成", result[0]["msg"])
        backfill_mock.assert_called_once_with(account)

    def test_sub2api_sync_persists_when_account_model_is_provided(self):
        cfg = {
            "contribution_enabled": "0",
            "sub2api_api_url": "http://sub2api.local",
            "sub2api_api_key": "sub2-key",
        }
        account = AccountModel(
            id=7,
            platform="chatgpt",
            email="demo@example.com",
            password="secret",
            token="at-token",
            status="registered",
        )
        account.user_id = "acct-demo"
        account.set_extra({"access_token": "at-token", "refresh_token": "rt-token", "workspace_id": "ws-demo"})
        db_account = AccountModel(
            id=7,
            platform="chatgpt",
            email="demo@example.com",
            password="secret",
            token="at-token",
            status="registered",
        )
        db_account.user_id = "acct-demo"
        db_account.set_extra({"access_token": "at-token", "refresh_token": "rt-token", "workspace_id": "ws-demo"})

        class _ExecResult:
            def first(self):
                return db_account

        fake_session = mock.Mock()
        fake_session.exec.return_value = _ExecResult()

        class _SessionFactory:
            def __init__(self, *_args, **_kwargs):
                pass

            def __enter__(self):
                return fake_session

            def __exit__(self, exc_type, exc, tb):
                return False

        with mock.patch("core.config_store.config_store.get", side_effect=_config_getter(cfg)):
            with mock.patch("core.db.Session", _SessionFactory):
                with mock.patch(
                    "services.sub2api_sync.backfill_chatgpt_account_to_sub2api",
                    return_value={"ok": True, "message": "补传完成，远端账号 #99"},
                ) as backfill_mock:
                    result = sync_account(account)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "Sub2API")
        self.assertTrue(result[0]["ok"])
        backfill_mock.assert_called_once_with(db_account, session=fake_session, commit=True)


if __name__ == "__main__":
    unittest.main()
