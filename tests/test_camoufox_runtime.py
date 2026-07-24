import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    from services.chatgpt_core import browser_registration
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
                options = browser_registration._camoufox_launch_opts(
                    headless=True,
                    proxy=None,
                )

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
                browser_registration._camoufox_executable_options()

    def test_registration_stage_applies_same_executable_options(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            executable = root / "camoufox-bin"
            executable.write_bytes(b"binary")
            executable.chmod(0o755)
            (root / "version.json").write_text(
                '{"version":"135.0.1","release":"beta.24"}',
                encoding="utf-8",
            )

            class _CookieContext:
                def cookies(self):
                    return []

            class _Page:
                url = "https://chatgpt.com/api/auth/callback/openai?code=test"

                def __init__(self):
                    self.context = _CookieContext()

                def set_default_timeout(self, _value):
                    return None

                def set_default_navigation_timeout(self, _value):
                    return None

                def evaluate(self, _script):
                    return "Mozilla/5.0"

            class _Context:
                def new_page(self):
                    return _Page()

                def cookies(self):
                    return []

            class _Camoufox:
                calls = []

                def __init__(self, **kwargs):
                    self.kwargs = kwargs
                    self.browser = _Context()
                    type(self).calls.append(kwargs)

                def __enter__(self):
                    return self.browser

                def __exit__(self, *_args):
                    return False

            with mock.patch.dict(
                os.environ,
                {"CAMOUFOX_EXECUTABLE_PATH": str(executable)},
                clear=False,
            ), mock.patch.object(
                browser_registration, "Camoufox", _Camoufox
            ), mock.patch.object(
                browser_registration,
                "playwright_proxy_context",
                return_value=mock.MagicMock(__enter__=lambda _self: None, __exit__=lambda *_args: False),
            ), mock.patch.object(
                browser_registration,
                "resolve_browser_headless",
                return_value=(True, "test"),
            ), mock.patch.object(
                browser_registration, "ensure_browser_display_available"
            ), mock.patch.object(
                browser_registration,
                "_browser_registration_flow",
                return_value={
                    "page_type": "oauth_callback",
                    "current_url": "https://chatgpt.com/api/auth/callback/openai?code=test",
                },
            ), mock.patch.object(
                browser_registration, "_is_registration_complete", return_value=True
            ), mock.patch.object(
                browser_registration,
                "_wait_for_web_session",
                return_value={},
            ):
                result = browser_registration.run_browser_registration_stage_sync(
                    email="user@example.com",
                    password="password",
                    proxy=None,
                    otp_callback=lambda: "123456",
                    device_id="device-test",
                    headless=True,
                    log_fn=lambda _message: None,
                )

        self.assertEqual(result["page_url"], "https://chatgpt.com/api/auth/callback/openai?code=test")
        self.assertEqual(_Camoufox.calls[0]["executable_path"], str(executable))
        self.assertEqual(_Camoufox.calls[0]["ff_version"], 135)


if __name__ == "__main__":
    unittest.main()
