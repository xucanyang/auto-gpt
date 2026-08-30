import unittest
from unittest.mock import patch

from fastapi import HTTPException

import api.config as config_api


class RegisterTaskConfigTests(unittest.TestCase):
    def test_config_response_exposes_registration_control_defaults(self):
        with patch.object(config_api.config_store, "get_all", return_value={}):
            response = config_api._build_config_response()

        self.assertEqual(response["chatgpt_register_protocol_default_concurrency"], "2")
        self.assertEqual(response["chatgpt_register_protocol_max_concurrency"], "3")
        self.assertEqual(response["chatgpt_register_browser_default_concurrency"], "2")
        self.assertEqual(response["chatgpt_register_browser_max_concurrency"], "2")
        self.assertEqual(response["chatgpt_register_delay_seconds"], "15")
        self.assertEqual(response["chatgpt_register_delay_max_seconds"], "30")
        self.assertEqual(response["chatgpt_register_unique_exit_ip_policy"], "auto")
        self.assertEqual(response["chatgpt_register_unique_exit_ip_max_refresh_attempts"], "6")
        self.assertEqual(response["chatgpt_register_unique_exit_ip_probe_timeout_seconds"], "8")
        self.assertEqual(response["chatgpt_register_unique_exit_ip_active_ttl_seconds"], "1800")
        self.assertEqual(response["chatgpt_register_unique_exit_ip_cooldown_seconds"], "900")
        self.assertEqual(response["default_browser_family"], "chrome")
        self.assertEqual(response["chatgpt_runtime_browser_capacity_mode"], "adaptive")
        self.assertEqual(response["chatgpt_runtime_auth_browser_max_concurrency"], "6")
        self.assertEqual(response["chatgpt_runtime_auth_browser_registration_reserve"], "4")
        self.assertEqual(response["chatgpt_runtime_auth_browser_recheck_reserve"], "2")
        self.assertEqual(response["chatgpt_web_session_hold_max_sessions"], "2")
        self.assertEqual(response["chatgpt_runtime_solver_mode"], "auto")
        self.assertEqual(response["chatgpt_runtime_solver_warm_browsers"], "0")
        self.assertEqual(response["chatgpt_runtime_solver_idle_timeout_seconds"], "300")

    def test_config_response_exposes_instance_browser_runtime_as_read_only_fields(self):
        with (
            patch.object(config_api.config_store, "get_all", return_value={}),
            patch(
                "services.chatgpt_core.browser_identity.configured_browser_runtime",
                return_value="camoufox",
            ),
            patch(
                "services.chatgpt_core.browser_identity.configured_deep_browser_family",
                return_value="firefox",
            ),
            patch(
                "services.chatgpt_core.browser_identity.configured_deep_browser_operating_system",
                return_value="linux",
            ),
        ):
            response = config_api._build_config_response()

        self.assertEqual(response["effective_deep_browser_runtime"], "camoufox")
        self.assertEqual(response["effective_deep_browser_family"], "firefox")
        self.assertEqual(response["effective_deep_browser_backend"], "camoufox_firefox")
        self.assertEqual(
            response["effective_deep_browser_operating_system"],
            "linux",
        )

        readonly_keys = {
            "effective_deep_browser_runtime",
            "effective_deep_browser_family",
            "effective_deep_browser_backend",
            "effective_deep_browser_operating_system",
        }
        self.assertTrue(readonly_keys.isdisjoint(config_api.CONFIG_KEYS))

        saved = {}
        with (
            patch.object(config_api.config_store, "get_all", return_value={}),
            patch.object(
                config_api.config_store,
                "set_many",
                side_effect=lambda values, **_kwargs: saved.update(values),
            ),
        ):
            result = config_api.update_config(
                config_api.ConfigUpdate(
                    data={key: "forged-value" for key in readonly_keys}
                )
            )

        self.assertEqual(saved, {})
        self.assertEqual(result["updated"], [])

    def test_config_response_canonical_policy_masks_conflicting_legacy_boolean(self):
        with patch.object(
            config_api.config_store,
            "get_all",
            return_value={
                "chatgpt_register_unique_exit_ip_policy": "auto",
                "chatgpt_register_unique_exit_ip_enabled": "false",
            },
        ):
            response = config_api._build_config_response()

        self.assertEqual(response["chatgpt_register_unique_exit_ip_policy"], "auto")
        self.assertEqual(response["chatgpt_register_unique_exit_ip_enabled"], "")

    def test_config_update_normalizes_registration_controls(self):
        saved = {}

        def capture(values, **_kwargs):
            saved.update(values)

        with (
            patch.object(config_api.config_store, "get_all", return_value={}),
            patch.object(config_api.config_store, "set_many", side_effect=capture),
        ):
            result = config_api.update_config(
                config_api.ConfigUpdate(
                    data={
                        "chatgpt_register_protocol_default_concurrency": "3.0",
                        "chatgpt_register_protocol_max_concurrency": 3,
                        "chatgpt_register_browser_default_concurrency": 2,
                        "chatgpt_register_browser_max_concurrency": "2",
                        "chatgpt_register_delay_seconds": "5.5",
                        "chatgpt_register_delay_max_seconds": 0,
                        "chatgpt_register_unique_exit_ip_policy": "required",
                        "chatgpt_register_unique_exit_ip_max_refresh_attempts": "6.0",
                        "chatgpt_register_unique_exit_ip_probe_timeout_seconds": 8,
                        "chatgpt_register_unique_exit_ip_active_ttl_seconds": 2400,
                        "chatgpt_register_unique_exit_ip_cooldown_seconds": 0,
                    }
                )
            )

        self.assertTrue(result["ok"])
        self.assertEqual(saved["chatgpt_register_protocol_default_concurrency"], "3")
        self.assertEqual(saved["chatgpt_register_protocol_max_concurrency"], "3")
        self.assertEqual(saved["chatgpt_register_delay_seconds"], "5.5")
        self.assertEqual(saved["chatgpt_register_delay_max_seconds"], "0")
        self.assertEqual(saved["chatgpt_register_unique_exit_ip_policy"], "required")
        self.assertEqual(saved["chatgpt_register_unique_exit_ip_enabled"], "true")
        self.assertEqual(saved["chatgpt_register_unique_exit_ip_max_refresh_attempts"], "6")
        self.assertEqual(saved["chatgpt_register_unique_exit_ip_probe_timeout_seconds"], "8")
        self.assertEqual(saved["chatgpt_register_unique_exit_ip_active_ttl_seconds"], "2400")
        self.assertEqual(saved["chatgpt_register_unique_exit_ip_cooldown_seconds"], "0")

    def test_config_update_normalizes_browser_family_and_rejects_unknown_value(self):
        saved = {}
        with (
            patch.object(config_api.config_store, "get_all", return_value={}),
            patch.object(
                config_api.config_store,
                "set_many",
                side_effect=lambda values, **_kwargs: saved.update(values),
            ),
        ):
            result = config_api.update_config(
                config_api.ConfigUpdate(data={"default_browser_family": "Safari"})
            )

        self.assertTrue(result["ok"])
        self.assertEqual(saved["default_browser_family"], "safari")

        with patch.object(config_api.config_store, "get_all", return_value={}):
            with self.assertRaises(HTTPException) as error:
                config_api.update_config(
                    config_api.ConfigUpdate(data={"default_browser_family": "edge"})
                )

        self.assertEqual(error.exception.status_code, 400)
        self.assertIn("default_browser_family", str(error.exception.detail))

    def test_config_update_synchronizes_canonical_and_legacy_policy_fields(self):
        def update(data, current=None):
            saved = {}
            with (
                patch.object(config_api.config_store, "get_all", return_value=dict(current or {})),
                patch.object(
                    config_api.config_store,
                    "set_many",
                    side_effect=lambda values, **_kwargs: saved.update(values),
                ),
            ):
                config_api.update_config(config_api.ConfigUpdate(data=data))
            return saved

        canonical_auto = update(
            {
                "chatgpt_register_unique_exit_ip_policy": "auto",
                "chatgpt_register_unique_exit_ip_enabled": "false",
            },
            current={"chatgpt_register_unique_exit_ip_enabled": "false"},
        )
        legacy_required = update(
            {"chatgpt_register_unique_exit_ip_enabled": "true"},
            current={"chatgpt_register_unique_exit_ip_policy": "auto"},
        )
        legacy_off = update(
            {"chatgpt_register_unique_exit_ip_enabled": "false"},
            current={"chatgpt_register_unique_exit_ip_policy": "required"},
        )

        self.assertEqual(canonical_auto["chatgpt_register_unique_exit_ip_policy"], "auto")
        self.assertEqual(canonical_auto["chatgpt_register_unique_exit_ip_enabled"], "")
        self.assertEqual(legacy_required["chatgpt_register_unique_exit_ip_policy"], "required")
        self.assertEqual(legacy_required["chatgpt_register_unique_exit_ip_enabled"], "true")
        self.assertEqual(legacy_off["chatgpt_register_unique_exit_ip_policy"], "off")
        self.assertEqual(legacy_off["chatgpt_register_unique_exit_ip_enabled"], "false")

    def test_runtime_capacity_update_is_normalized_and_restarts_solver(self):
        saved = {}
        with (
            patch.object(config_api.config_store, "get_all", return_value={}),
            patch.object(
                config_api.config_store,
                "set_many",
                side_effect=lambda values, **_kwargs: saved.update(values),
            ),
            patch("services.solver_manager.restart_async") as restart_solver,
        ):
            result = config_api.update_config(
                config_api.ConfigUpdate(
                    data={
                        "chatgpt_register_browser_default_concurrency": 30,
                        "chatgpt_register_browser_max_concurrency": 30,
                        "chatgpt_runtime_browser_capacity_mode": "adaptive",
                        "chatgpt_runtime_auth_browser_max_concurrency": 30,
                        "chatgpt_runtime_auth_browser_registration_reserve": 24,
                        "chatgpt_runtime_auth_browser_recheck_reserve": 6,
                        "chatgpt_web_session_hold_max_sessions": 4,
                        "chatgpt_runtime_auth_browser_pid_budget": 220,
                        "chatgpt_runtime_pid_emergency_reserve": 256,
                        "chatgpt_runtime_host_memory_reserve_mib": 6144,
                        "chatgpt_runtime_cpu_psi_avg10_limit": 20,
                        "chatgpt_runtime_auth_browser_launch_interval_seconds": 4,
                        "chatgpt_runtime_solver_mode": "auto",
                        "chatgpt_runtime_solver_warm_browsers": 0,
                        "chatgpt_runtime_solver_max_browsers": 15,
                        "chatgpt_runtime_solver_idle_timeout_seconds": 300,
                        "chatgpt_runtime_registration_transition_timeout_seconds": 40,
                    }
                )
            )

        self.assertTrue(result["ok"])
        self.assertEqual(saved["chatgpt_register_browser_default_concurrency"], "30")
        self.assertEqual(saved["chatgpt_register_browser_max_concurrency"], "30")
        self.assertEqual(saved["chatgpt_runtime_auth_browser_max_concurrency"], "30")
        self.assertEqual(saved["chatgpt_runtime_auth_browser_registration_reserve"], "24")
        self.assertEqual(saved["chatgpt_runtime_auth_browser_recheck_reserve"], "6")
        self.assertEqual(saved["chatgpt_web_session_hold_max_sessions"], "4")
        self.assertEqual(saved["chatgpt_runtime_solver_warm_browsers"], "0")
        self.assertEqual(saved["chatgpt_runtime_solver_max_browsers"], "15")
        restart_solver.assert_called_once_with()

    def test_runtime_capacity_rejects_solver_warm_above_max(self):
        with patch.object(config_api.config_store, "get_all", return_value={}):
            with self.assertRaises(HTTPException) as error:
                config_api.update_config(
                    config_api.ConfigUpdate(
                        data={
                            "chatgpt_runtime_solver_warm_browsers": 15,
                            "chatgpt_runtime_solver_max_browsers": 14,
                        }
                    )
                )

        self.assertEqual(error.exception.status_code, 400)
        self.assertIn("暖浏览器数不能大于", str(error.exception.detail))

    def test_runtime_capacity_rejects_lane_reserves_above_total(self):
        with patch.object(config_api.config_store, "get_all", return_value={}):
            with self.assertRaises(HTTPException) as error:
                config_api.update_config(
                    config_api.ConfigUpdate(
                        data={
                            "chatgpt_runtime_auth_browser_max_concurrency": 5,
                            "chatgpt_runtime_auth_browser_registration_reserve": 4,
                            "chatgpt_runtime_auth_browser_recheck_reserve": 2,
                        }
                    )
                )

        self.assertEqual(error.exception.status_code, 400)
        self.assertIn("保留槽位之和不能超过", str(error.exception.detail))

    def test_config_update_rejects_invalid_caps_and_delay_range(self):
        with patch.object(config_api.config_store, "get_all", return_value={}):
            with self.assertRaises(HTTPException) as invalid_browser_cap_error:
                config_api.update_config(
                    config_api.ConfigUpdate(
                        data={"chatgpt_register_browser_max_concurrency": 0}
                    )
                )
            with self.assertRaises(HTTPException) as cap_error:
                config_api.update_config(
                    config_api.ConfigUpdate(
                        data={
                            "chatgpt_register_protocol_default_concurrency": 3,
                            "chatgpt_register_protocol_max_concurrency": 2,
                        }
                    )
                )
            with self.assertRaises(HTTPException) as delay_error:
                config_api.update_config(
                    config_api.ConfigUpdate(
                        data={
                            "chatgpt_register_delay_seconds": 30,
                            "chatgpt_register_delay_max_seconds": 15,
                        }
                    )
                )
            with self.assertRaises(HTTPException) as refresh_budget_error:
                config_api.update_config(
                    config_api.ConfigUpdate(
                        data={
                            "chatgpt_register_unique_exit_ip_max_refresh_attempts": 13,
                        }
                    )
                )

        self.assertEqual(invalid_browser_cap_error.exception.status_code, 400)
        self.assertIn("大于等于 1", str(invalid_browser_cap_error.exception.detail))
        self.assertEqual(cap_error.exception.status_code, 400)
        self.assertIn("默认并发不能大于", str(cap_error.exception.detail))
        self.assertEqual(delay_error.exception.status_code, 400)
        self.assertIn("最大启动延时不能小于", str(delay_error.exception.detail))
        self.assertEqual(refresh_budget_error.exception.status_code, 400)
        self.assertIn("1 到 12", str(refresh_budget_error.exception.detail))


if __name__ == "__main__":
    unittest.main()
