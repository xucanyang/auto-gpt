import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

from core.config_store import (
    ConfigStore,
    _canonical_config_key,
    _get_env_fallback_value,
    _load_env_file,
    _merge_env_fallback,
    _normalize_config_value,
)


class ConfigStoreEnvFallbackTests(unittest.TestCase):
    def test_normalize_config_value_strips_matching_quotes(self):
        self.assertEqual(_normalize_config_value('"quoted"'), "quoted")
        self.assertEqual(_normalize_config_value("'quoted'"), "quoted")
        self.assertEqual(_normalize_config_value("plain"), "plain")

    def test_load_env_file_supports_export_and_quotes(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "# comment",
                        "export SMSTOME_COOKIE='cf_clearance=demo'",
                        'cfworker_custom_auth="secret-pass"',
                    ]
                ),
                encoding="utf-8",
            )

            values = _load_env_file(env_path)

        self.assertEqual(values["SMSTOME_COOKIE"], "cf_clearance=demo")
        self.assertEqual(values["cfworker_custom_auth"], "secret-pass")

    def test_get_env_fallback_value_matches_uppercase_env_names(self):
        env_values = {
            "SMSTOME_COOKIE": "cf_clearance=demo",
            "CFWORKER_CUSTOM_AUTH": "secret-pass",
        }

        self.assertEqual(
            _get_env_fallback_value("smstome_cookie", env_values=env_values),
            "cf_clearance=demo",
        )
        self.assertEqual(
            _get_env_fallback_value("cfworker_custom_auth", env_values=env_values),
            "secret-pass",
        )

    def test_merge_env_fallback_uses_canonical_key_without_overriding_db(self):
        merged = _merge_env_fallback(
            {
                "smstome_cookie": "",
                "cfworker_custom_auth": "db-value",
            },
            env_values={
                "SMSTOME_COOKIE": "cf_clearance=demo",
                "CFWORKER_CUSTOM_AUTH": "env-value",
            },
        )

        self.assertEqual(_canonical_config_key("SMSTOME_COOKIE"), "smstome_cookie")
        self.assertEqual(merged["smstome_cookie"], "cf_clearance=demo")
        self.assertEqual(merged["cfworker_custom_auth"], "db-value")

    def test_local_auth_read_uses_cache_on_pool_timeout_without_shared_mode_query(self):
        store = object.__new__(ConfigStore)
        store._cache = {"auth_password_hash": "cached-password-hash"}
        store.shared_enabled = mock.Mock(side_effect=AssertionError("local auth key queried shared mode"))

        with mock.patch(
            "core.config_store.Session",
            side_effect=SQLAlchemyTimeoutError("QueuePool timed out"),
        ):
            value = store.get("auth_password_hash", "")

        self.assertEqual(value, "cached-password-hash")
        store.shared_enabled.assert_not_called()

    def test_local_status_unique_exit_defaults_to_disabled_when_config_is_missing(self):
        from api import config as config_api

        with mock.patch.object(config_api.config_store, "get_all", return_value={}):
            response = config_api._build_config_response()

        self.assertEqual(
            response["chatgpt_local_status_probe_unique_exit_ip_enabled"],
            "false",
        )


if __name__ == "__main__":
    unittest.main()
