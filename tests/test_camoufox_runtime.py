import contextlib
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

try:
    from services.chatgpt_core import browser_registration, shared_camoufox
except ModuleNotFoundError as exc:
    if exc.name == "camoufox":
        raise unittest.SkipTest("camoufox is only installed in the runtime image") from exc
    raise


class CamoufoxRuntimeTests(unittest.TestCase):
    def test_legacy_executable_path_is_passed_without_package_download(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            executable = root / "camoufox-bin"
            executable.write_bytes(b"binary")
            executable.chmod(0o755)
            (root / "version.json").write_text(
                '{"version":"135.0.1","release":"beta.24"}',
                encoding="utf-8",
            )

            with mock.patch.dict(
                os.environ,
                {"CAMOUFOX_EXECUTABLE_PATH": str(executable)},
                clear=False,
            ):
                options = shared_camoufox.camoufox_executable_options()

        self.assertEqual(options["executable_path"], str(executable))
        self.assertEqual(options["ff_version"], 135)
        self.assertTrue(options["i_know_what_im_doing"])

    def test_invalid_explicit_executable_path_fails_closed(self):
        with mock.patch.dict(
            os.environ,
            {"CAMOUFOX_EXECUTABLE_PATH": "/does/not/exist/camoufox-bin"},
            clear=False,
        ):
            with self.assertRaisesRegex(RuntimeError, "CAMOUFOX_EXECUTABLE_PATH"):
                shared_camoufox.camoufox_executable_options()

    def test_server_launch_config_injects_deep_process_profile_and_blocks_webrtc(self):
        fake_options = {
            "executable_path": "/runtime/camoufox-bin",
            "args": ["--example"],
            "env": {"CAMOU_CONFIG": "demo"},
            "firefox_user_prefs": {"media.peerconnection.enabled": False},
            "headless": True,
        }
        profile = types.SimpleNamespace(operating_system="linux")
        process_config = {
            "navigator.userAgent": "Mozilla/5.0 Firefox/147.0",
            "canvas:seed": 123,
        }
        with (
            mock.patch.object(
                shared_camoufox,
                "_resolve_deep_profile",
                return_value=profile,
            ),
            mock.patch(
                "services.chatgpt_core.browser_identity.build_camoufox_process_config",
                return_value=process_config,
            ) as build_process_config,
            mock.patch(
                "camoufox.utils.launch_options",
                return_value=fake_options,
            ) as launch_options,
        ):
            config = shared_camoufox._server_launch_config(
                True,
                browser_fingerprint=profile,
                context_options={"timezone_id": "America/New_York"},
            )

        launch_options.assert_called_once_with(
            config=process_config,
            os="linux",
            headless=True,
            block_webrtc=True,
            exclude_addons=[mock.ANY],
            i_know_what_im_doing=True,
        )
        build_process_config.assert_called_once_with(
            profile,
            context_options={"timezone_id": "America/New_York"},
        )
        self.assertEqual(config["executablePath"], "/runtime/camoufox-bin")
        self.assertTrue(config["_sharedBrowser"])
        self.assertEqual(config["host"], "127.0.0.1")
        self.assertEqual(config["port"], 0)
        self.assertNotIn("proxy", config)

    def test_context_options_keep_proxy_and_geoip_context_scoped(self):
        geo_options = {
            "locale": "en-US",
            "timezone_id": "America/New_York",
            "geolocation": {"latitude": 40.7, "longitude": -74.0},
            "permissions": ["geolocation"],
        }
        proxy_config = {
            "server": "http://127.0.0.1:19000",
        }
        with (
            mock.patch.object(
                shared_camoufox,
                "playwright_proxy_context",
                return_value=contextlib.nullcontext(proxy_config),
            ),
            mock.patch.object(
                shared_camoufox,
                "_proxy_geo_context_options",
                return_value=geo_options,
            ),
            shared_camoufox.shared_camoufox_context_options(
                "socks5://user:pass@proxy.local:1080"
            ) as options,
        ):
            captured = dict(options)

        self.assertEqual(captured["proxy"], proxy_config)
        self.assertEqual(captured["timezone_id"], "America/New_York")
        self.assertEqual(captured["locale"], "en-US")

    def test_worker_environment_carries_exact_preallocated_context(self):
        environment = {"EXISTING": "value"}

        shared_camoufox.bind_shared_camoufox_worker_environment(
            environment,
            endpoint="ws://127.0.0.1:19001/context-server",
            headless=True,
            context_token="context-token",
        )

        self.assertEqual(
            environment[shared_camoufox.SHARED_CAMOUFOX_ENDPOINT_ENV],
            "ws://127.0.0.1:19001/context-server",
        )
        self.assertEqual(
            environment[shared_camoufox.SHARED_CAMOUFOX_MODE_ENV],
            "headless",
        )
        self.assertEqual(
            environment[shared_camoufox.SHARED_CAMOUFOX_CONTEXT_TOKEN_ENV],
            "context-token",
        )
        self.assertEqual(environment["EXISTING"], "value")

    def test_registration_stage_claims_preallocated_context(self):
        class _Context:
            def cookies(self):
                return []

        class _Page:
            url = "https://chatgpt.com/api/auth/callback/openai?code=test"

            def __init__(self, context):
                self.context = context

            def set_default_timeout(self, _value):
                return None

            def set_default_navigation_timeout(self, _value):
                return None

            def evaluate(self, _script):
                return "Mozilla/5.0"

        context = _Context()
        page = _Page(context)
        session = types.SimpleNamespace(
            browser=mock.Mock(),
            context=context,
            page=page,
            token="context-token",
        )

        with (
            mock.patch.object(
                browser_registration,
                "shared_browser_registration_session",
                return_value=contextlib.nullcontext(session),
            ) as shared_session,
            mock.patch.object(
                browser_registration,
                "resolve_browser_headless",
                return_value=(True, "test"),
            ),
            mock.patch.object(
                browser_registration,
                "ensure_browser_display_available",
            ),
            mock.patch.object(
                browser_registration,
                "_browser_registration_flow",
                return_value={
                    "page_type": "oauth_callback",
                    "current_url": page.url,
                },
            ),
            mock.patch.object(
                browser_registration,
                "_is_registration_complete",
                return_value=True,
            ),
            mock.patch.object(
                browser_registration,
                "_wait_for_web_session",
                return_value={},
            ),
        ):
            result = browser_registration.run_browser_registration_stage_sync(
                email="user@example.com",
                password="password",
                proxy="http://proxy.local:8080",
                otp_callback=lambda: "123456",
                device_id="device-test",
                headless=True,
                log_fn=lambda _message: None,
            )

        self.assertEqual(result["page_url"], page.url)
        self.assertEqual(
            shared_session.call_args.kwargs,
            {
                "headless": True,
                "proxy": "http://proxy.local:8080",
                "logger": mock.ANY,
            },
        )


if __name__ == "__main__":
    unittest.main()
