import contextlib
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest import mock
import zipfile

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, Session, create_engine

from api import registration_diagnostics as diagnostics_api
from api import tasks as tasks_api
from core.db import RegistrationDiagnosticArtifactModel
from services.chatgpt_core import registration_diagnostics as diagnostics

try:
    from services.chatgpt_core.any_auto import browser_register
    from services.chatgpt_core.any_auto.browser_register import ChatGPTBrowserRegister
except ModuleNotFoundError:
    browser_register = None
    ChatGPTBrowserRegister = None


class _FakeTracing:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    def start(self, **kwargs) -> None:
        self.started = kwargs == {
            "screenshots": True,
            "snapshots": True,
            "sources": True,
        }

    def stop(self, *, path) -> None:
        self.stopped = True
        Path(path).write_bytes(b"trace-data")


class _FakeContext:
    def __init__(self) -> None:
        self.tracing = _FakeTracing()

    def cookies(self):
        return [
            {
                "name": "session-token",
                "value": "secret-cookie-value",
                "domain": ".chatgpt.com",
                "path": "/",
                "expires": 123,
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            }
        ]


class _FakePage:
    def __init__(self) -> None:
        self.url = "https://auth.openai.com/email-verification?code=secret"
        self.listeners = {}

    def on(self, event, listener) -> None:
        self.listeners[event] = listener

    def remove_listener(self, event, listener) -> None:
        if self.listeners.get(event) is listener:
            self.listeners.pop(event)

    def title(self) -> str:
        return "OpenAI sign up"

    def content(self) -> str:
        return "<html><body>failure state</body></html>"

    def screenshot(self, *, path, **_kwargs) -> None:
        Path(path).write_bytes(b"png-data")


class RegistrationDiagnosticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.runtime = root / "runtime"
        self.engine = create_engine(
            f"sqlite:///{root / 'diagnostics.db'}",
            connect_args={"check_same_thread": False},
        )
        SQLModel.metadata.create_all(self.engine)
        self.engine_patch = mock.patch.object(
            diagnostics,
            "_engine",
            return_value=self.engine,
        )
        self.engine_patch.start()
        self.env_patch = mock.patch.dict(
            os.environ,
            {
                "APP_RUNTIME_DIR": str(self.runtime),
                "APP_INSTANCE_ID": "unit-test",
                "REGISTRATION_DIAGNOSTICS_GLOBAL_MAX_BYTES": str(64 * 1024 * 1024),
                "REGISTRATION_DIAGNOSTICS_TASK_MAX_BYTES": str(32 * 1024 * 1024),
                "REGISTRATION_DIAGNOSTICS_ATTEMPT_MAX_BYTES": str(16 * 1024 * 1024),
                "REGISTRATION_DIAGNOSTICS_RESPONSE_MAX_BYTES": str(1024 * 1024),
                "REGISTRATION_DIAGNOSTICS_STRUCTURED_MAX_BYTES": str(4 * 1024 * 1024),
                "REGISTRATION_DIAGNOSTICS_FREE_RESERVE_BYTES": "1",
                "REGISTRATION_DIAGNOSTICS_SUCCESS_SAMPLES": "1",
            },
        )
        self.env_patch.start()

    def tearDown(self) -> None:
        diagnostics._CURRENT_SESSION.set(None)
        self.env_patch.stop()
        self.engine_patch.stop()
        self.engine.dispose()
        self.tmp.cleanup()

    def _session(
        self,
        attempt_id: int,
        *,
        mode: str = "smart",
        task_id: str = "task_unit_diagnostics",
    ) -> diagnostics.RegistrationDiagnosticSession:
        session = diagnostics.create_registration_diagnostic_session(
            task_id=task_id,
            attempt_id=attempt_id,
            attempt_number=attempt_id,
            mode=mode,
            metadata={"password": "must-not-persist", "executor_type": "headless"},
        )
        self.assertIsNotNone(session)
        return session

    def _finalize_failure(
        self,
        attempt_id: int,
        *,
        mode: str = "smart",
        task_id: str = "task_unit_diagnostics",
    ):
        session = self._session(attempt_id, mode=mode, task_id=task_id)
        return session, session.finalize(
            outcome="failed",
            error="email OTP 123456 failed with access_token=secret-token",
            email="operator@example.com",
            reason_code="otp_failed",
        )

    def test_mode_normalization_and_request_validation(self) -> None:
        self.assertEqual(diagnostics.normalize_registration_diagnostics_mode(True), "smart")
        self.assertEqual(diagnostics.normalize_registration_diagnostics_mode("all"), "full")
        self.assertEqual(diagnostics.normalize_registration_diagnostics_mode("disabled"), "off")
        with self.assertRaisesRegex(ValueError, "off、smart 或 full"):
            diagnostics.normalize_registration_diagnostics_mode("verbose")

        with mock.patch(
            "core.config_store.config_store.get_all",
            return_value={},
        ):
            protocol_request = tasks_api._prepare_register_request(
                tasks_api.RegisterTaskRequest(
                    platform="chatgpt",
                    executor_type="protocol",
                    registration_diagnostics_mode="smart",
                )
            )
            with self.assertRaises(HTTPException) as platform_error:
                tasks_api._prepare_register_request(
                    tasks_api.RegisterTaskRequest(
                        platform="google",
                        executor_type="headless",
                        registration_diagnostics_mode="smart",
                    )
                )

        self.assertEqual(protocol_request.registration_diagnostics_mode, "smart")
        self.assertEqual(platform_error.exception.status_code, 400)
        self.assertIn("ChatGPT", str(platform_error.exception.detail))

    def test_browser_capture_finalizes_complete_failure_bundle(self) -> None:
        session = self._session(1, mode="full")
        options = session.browser_context_options()
        self.assertEqual(options["record_har_mode"], "full")
        self.assertEqual(options["record_har_content"], "attach")
        self.assertIn("record_video_dir", options)

        context = _FakeContext()
        page = _FakePage()
        session.start_browser_capture(context, page)
        session.record_log("邮箱验证码 123456", "debug")
        session.stop_browser_capture(page, context)
        Path(options["record_har_path"]).write_bytes(b"har-data")
        result = session.finalize(
            outcome="failed",
            error="验证码 123456 failed token=secret-token",
            email="operator@example.com",
            reason_code="otp_failed",
        )

        self.assertEqual(result["status"], "ready")
        self.assertTrue(context.tracing.started)
        self.assertTrue(context.tracing.stopped)
        self.assertEqual(page.listeners, {})
        item = diagnostics.list_registration_diagnostics(session.task_id)[0]
        self.assertEqual(item["failure_code"], "otp_failed")
        for filename in (
            "manifest.json",
            "diagnosis.json",
            "trace.zip",
            "network.har.zip",
            "final-state.json",
            "final-page.html",
            "final-page.png",
        ):
            self.assertIn(filename, item["files"])
        self.assertTrue((session.final_dir / "manifest.json").is_file())
        diagnosis = json.loads((session.final_dir / "diagnosis.json").read_text())
        self.assertNotIn("123456", json.dumps(diagnosis, ensure_ascii=False))
        self.assertNotIn("secret-token", json.dumps(diagnosis, ensure_ascii=False))

    def test_browser_capture_restart_replaces_canonical_final_context_artifacts(self) -> None:
        session = self._session(40, mode="full")
        first_context = _FakeContext()
        first_page = _FakePage()
        session.start_browser_capture(first_context, first_page)
        session.stop_browser_capture(first_page, first_context)

        second_context = _FakeContext()
        second_page = _FakePage()
        second_page.url = "https://auth.openai.com/about-you"
        session.start_browser_capture(second_context, second_page)
        session.stop_browser_capture(second_page, second_context)

        final_state = json.loads(
            (session.partial_dir / "final-state.json").read_text(encoding="utf-8")
        )
        self.assertEqual(final_state["url"], "https://auth.openai.com/about-you")
        self.assertTrue(first_context.tracing.stopped)
        self.assertTrue(second_context.tracing.started)
        self.assertTrue(second_context.tracing.stopped)
        self.assertEqual(second_page.listeners, {})
        events = (session.partial_dir / "events.jsonl").read_text(encoding="utf-8")
        self.assertIn("context_capture_restarted", events)
        session.finalize(outcome="failed", error="retry context failed")

    def test_worker_capture_spec_round_trips_and_merges_real_artifacts(self) -> None:
        session = self._session(41, mode="full", task_id="task_worker_capture")
        spec = json.loads(json.dumps(session.browser_worker_capture_spec()))
        worker_session = diagnostics.RegistrationDiagnosticSession.attach_browser_worker_capture(
            spec
        )
        self.assertIsNotNone(worker_session)
        worker_session.activate()
        context = _FakeContext()
        page = _FakePage()
        worker_session.start_browser_capture(context, page)
        worker_session.stop_browser_capture(page, context)
        (worker_session.partial_dir / "network.har.zip").write_bytes(b"har-data")
        report = worker_session.write_browser_worker_capture_report()
        worker_session._detach_context()

        for name in diagnostics._BROWSER_CAPTURE_REQUIRED_ARTIFACTS:
            self.assertTrue(report["artifacts"][name]["available"], name)
        self.assertFalse(report["artifacts"]["video"]["available"])
        self.assertTrue(report["artifacts"]["video"]["reason"])
        result = session.finalize(
            outcome="failed",
            error="验证码页未找到可填写输入框",
        )

        self.assertEqual(result["failure_code"], "otp_input_missing")
        diagnosis = json.loads((session.final_dir / "diagnosis.json").read_text())
        worker_artifacts = diagnosis["capture"]["worker_artifacts"]
        self.assertTrue(worker_artifacts)
        for name in diagnostics._BROWSER_CAPTURE_REQUIRED_ARTIFACTS:
            self.assertTrue(worker_artifacts[name]["available"], name)
        self.assertFalse(worker_artifacts["video"]["available"])
        self.assertTrue(worker_artifacts["video"]["reason"])
        self.assertEqual(diagnosis["final_state"]["title"], "OpenAI sign up")

    def test_missing_worker_capture_report_names_every_unavailable_artifact(self) -> None:
        session = self._session(42, mode="full", task_id="task_worker_missing")
        session.browser_worker_capture_spec()

        result = session.finalize(
            outcome="failed",
            error="browser worker terminated",
        )

        self.assertEqual(result["status"], "ready")
        diagnosis = json.loads((session.final_dir / "diagnosis.json").read_text())
        self.assertIn("browser_worker_capture_report_missing", diagnosis["warnings"])
        for name in diagnostics._BROWSER_CAPTURE_REQUIRED_ARTIFACTS:
            self.assertTrue(
                any(
                    warning.startswith(f"browser_capture_unavailable:{name}:")
                    for warning in diagnosis["warnings"]
                ),
                name,
            )
        self.assertTrue(
            any(
                warning.startswith("browser_capture_unavailable:video:")
                for warning in diagnosis["warnings"]
            )
        )

    @unittest.skipIf(browser_register is None, "Camoufox is only available in the runtime image")
    def test_any_auto_uses_explicit_diagnostic_context_and_flush_order(self) -> None:
        page = mock.Mock()
        page.url = "https://chatgpt.com/"
        context = mock.Mock()
        context.new_page.return_value = page
        context.cookies.return_value = [
            {
                "name": "__Secure-next-auth.session-token",
                "value": "session-demo",
                "domain": "chatgpt.com",
            }
        ]
        page.context = context
        browser = mock.Mock()
        cleanup_order = []

        def fail_context_close() -> None:
            cleanup_order.append("context_close")
            raise RuntimeError("simulated HAR flush failure")

        context.close.side_effect = fail_context_close
        diagnostic_session = mock.Mock(enabled=True)
        diagnostic_session.browser_context_options.return_value = {
            "record_har_path": "/tmp/network.har.zip",
            "record_har_mode": "full",
            "record_har_content": "attach",
            "record_video_dir": "/tmp/video",
        }
        def fail_diagnostic_stop(*_args) -> None:
            cleanup_order.append("diagnostic_stop")
            raise RuntimeError("simulated trace stop failure")

        diagnostic_session.stop_browser_capture.side_effect = fail_diagnostic_stop
        session = types.SimpleNamespace(
            browser=browser,
            context=context,
            page=page,
            token="diagnostic-context",
        )
        fallback_context = mock.Mock()
        fallback_page = mock.Mock()
        fallback_page.url = "https://chatgpt.com/"
        fallback_page.context = fallback_context
        fallback_context.cookies.return_value = context.cookies.return_value
        fallback_session = types.SimpleNamespace(
            browser=browser,
            context=fallback_context,
            page=fallback_page,
            token="fallback-context",
        )
        failed_context = mock.MagicMock()
        failed_context.__enter__.side_effect = RuntimeError(
            "simulated diagnostic context setup failure"
        )
        failed_context_retry = mock.MagicMock()
        failed_context_retry.__enter__.side_effect = RuntimeError(
            "simulated diagnostic context retry failure"
        )
        failed_video_context = mock.MagicMock()
        failed_video_context.__enter__.side_effect = RuntimeError(
            "BrowserContext.new_page: Browser closed"
        )

        with (
            mock.patch.object(
                browser_register,
                "shared_camoufox_registration_session",
                side_effect=[
                    failed_video_context,
                    contextlib.nullcontext(session),
                    failed_context,
                    failed_context_retry,
                    contextlib.nullcontext(fallback_session),
                ],
            ) as shared_session,
            mock.patch.object(
                browser_register,
                "run_with_browser_capacity",
                side_effect=lambda _operation, callback, **_kwargs: callback(),
            ),
            mock.patch.object(
                browser_register,
                "_browser_registration_flow",
                return_value={
                    "page_type": "oauth_callback",
                    "continue_url": "https://chatgpt.com/auth/callback/openai?code=demo",
                },
            ),
            mock.patch(
                "services.chatgpt_core.browser_registration._wait_for_web_session",
                return_value={
                    "accessToken": "at-demo",
                    "sessionToken": "session-demo",
                },
            ),
            mock.patch(
                "services.chatgpt_core.browser_registration._normalize_browser_web_session",
                return_value={
                    "access_token": "at-demo",
                    "session_token": "session-demo",
                    "cookie_header": "__Secure-next-auth.session-token=session-demo",
                    "account_id": "acct-demo",
                },
            ),
            mock.patch.object(
                diagnostics,
                "current_registration_diagnostic_session",
                return_value=diagnostic_session,
            ),
            mock.patch.object(
                browser_register,
                "_DIAGNOSTIC_VIDEO_UNSUPPORTED",
                False,
            ),
        ):
            worker = ChatGPTBrowserRegister(
                headless=True,
                otp_callback=lambda: "123456",
                log_fn=lambda _message: None,
            )
            result = worker.run("user@example.com", "Password123!")
            fallback_result = worker.run("fallback@example.com", "Password123!")

        self.assertTrue(result["success"])
        self.assertTrue(fallback_result["success"])
        self.assertEqual(shared_session.call_count, 5)
        self.assertEqual(
            shared_session.call_args_list[0].kwargs["extra_context_options"],
            {
                "record_har_path": "/tmp/network.har.zip",
                "record_har_mode": "full",
                "record_har_content": "attach",
                "record_video_dir": "/tmp/video",
            },
        )
        self.assertNotIn(
            "record_video_dir",
            shared_session.call_args_list[1].kwargs["extra_context_options"],
        )
        self.assertIn(
            "record_video_dir",
            shared_session.call_args_list[2].kwargs["extra_context_options"],
        )
        self.assertNotIn(
            "record_video_dir",
            shared_session.call_args_list[3].kwargs["extra_context_options"],
        )
        self.assertEqual(
            shared_session.call_args_list[4].kwargs["extra_context_options"],
            {},
        )
        self.assertEqual(
            diagnostic_session.mark_video_capture_unavailable.call_count,
            2,
        )
        diagnostic_session.start_browser_capture.assert_called_once_with(context, page)
        self.assertEqual(cleanup_order, ["diagnostic_stop", "context_close"])
        self.assertEqual(diagnostic_session.record_event.call_count, 4)

    def test_diagnostic_log_write_failure_never_escapes_to_registration(self) -> None:
        session = self._session(3)
        with mock.patch.object(
            diagnostics,
            "_append_jsonl",
            side_effect=OSError("disk unavailable"),
        ):
            diagnostics.record_registration_diagnostic_log("registration continues")
        self.assertIn("event_write_failed:OSError", session._warnings)
        session.finalize(outcome="interrupted", error="unit test cleanup")

    def test_protocol_har_is_packaged_and_redacted(self) -> None:
        session = self._session(2)
        diagnostics.record_registration_protocol_http_exchange(
            method="POST",
            url="https://auth.openai.com/api/accounts/phone-otp/validate?code=123456",
            request_headers={
                "Authorization": "Bearer raw-secret-token",
                "Content-Type": "application/json",
            },
            request_body={
                "code": "123456",
                "password": "Password123!",
                "email": "raw-user@example.com",
            },
            status=401,
            response_headers={"content-type": "application/json"},
            response_body=b'{"access_token":"response-secret","error":"invalid_otp"}',
            duration_ms=42,
        )
        result = session.finalize(
            outcome="failed",
            error="phone OTP failed",
            email="phone:+15551234567",
        )

        self.assertEqual(result["status"], "ready")
        item = diagnostics.list_registration_diagnostics(session.task_id)[0]
        self.assertEqual(item["failure_code"], "otp_code_rejected")
        self.assertEqual(item["failure_stage"], "phone_otp")
        archive_path = session.final_dir / "network.har.zip"
        self.assertTrue(archive_path.is_file())
        with zipfile.ZipFile(archive_path) as archive:
            payload = archive.read("network.har").decode("utf-8")
        self.assertIn('"version":"1.2"', payload)
        self.assertIn("/api/accounts/phone-otp/validate", payload)
        for secret in (
            "123456",
            "Password123!",
            "raw-secret-token",
            "response-secret",
            "raw-user@example.com",
        ):
            self.assertNotIn(secret, payload)
        diagnosis = json.loads((session.final_dir / "diagnosis.json").read_text())
        self.assertEqual(diagnosis["analysis"]["kind"], "rule_based")
        self.assertTrue(diagnosis["capture"]["protocol_har"])

    def test_structured_business_codes_override_generic_http_status(self) -> None:
        cases = (
            (
                31,
                "/api/accounts/password/verify",
                401,
                "invalid_username_or_password",
                "existing_login_password_failed",
                "password",
            ),
            (
                32,
                "/api/accounts/user/register",
                400,
                "username_already_exists",
                "existing_account",
                "registration_route",
            ),
            (
                33,
                "/api/accounts/authorize",
                400,
                "invalid_auth_step",
                "invalid_auth_step",
                "registration_route",
            ),
            (
                34,
                "/api/accounts/authorize",
                409,
                "invalid_state",
                "invalid_auth_state",
                "registration_route",
            ),
            (
                36,
                "/api/accounts/create_account",
                400,
                "identity_provider_mismatch",
                "identity_provider_mismatch",
                "registration_route",
            ),
            (
                38,
                "/api/accounts/create_account",
                400,
                "registration_disallowed",
                "registration_disallowed",
                "about_you",
            ),
            (
                39,
                "/api/accounts/email-otp/validate",
                401,
                "invalid_otp",
                "otp_code_rejected",
                "email_otp",
            ),
        )
        for attempt_id, path, status, upstream_code, expected_code, expected_stage in cases:
            with self.subTest(upstream_code=upstream_code):
                task_id = f"task_structured_{attempt_id}"
                session = self._session(attempt_id, task_id=task_id)
                session.record_protocol_http_exchange(
                    method="POST",
                    url=f"https://auth.openai.com{path}",
                    status=status,
                    response_headers={"content-type": "application/json"},
                    response_body=json.dumps(
                        {"error": {"code": upstream_code, "message": "request failed"}}
                    ).encode(),
                )
                result = session.finalize(outcome="failed", error="upstream request failed")

                self.assertEqual(result["failure_code"], expected_code)
                self.assertEqual(result["failure_stage"], expected_stage)
                item = diagnostics.list_registration_diagnostics(task_id)[0]
                self.assertEqual(item["failure_code"], expected_code)
                self.assertEqual(item["failure_stage"], expected_stage)

    def test_post_signup_failure_markers_have_precise_stages(self) -> None:
        cases = (
            ("post_signup_auth_api_failure", "post_signup_auth_api_failure", "post_signup"),
            ("post_signup_navigation_failed", "post_signup_navigation_failed", "post_signup"),
            ("post_signup_duplicate_submission", "post_signup_duplicate_submission", "post_signup"),
            ("session_capture_pending", "session_capture_pending", "web_session"),
            ("post_signup_session_capture_failed", "post_signup_session_capture_failed", "web_session"),
        )
        for marker, expected_code, expected_stage in cases:
            with self.subTest(marker=marker):
                self.assertEqual(
                    diagnostics._classify_failure(f"registration failed: {marker}"),
                    (expected_code, expected_stage),
                )

    def test_multilingual_otp_and_registration_terminal_errors_are_precise(self) -> None:
        cases = (
            (
                "The verification code is incorrect.",
                ("otp_code_rejected", "email_otp"),
            ),
            (
                "El código de verificación incorrecto.",
                ("otp_code_rejected", "email_otp"),
            ),
            (
                "验证码不正确，请重试",
                ("otp_code_rejected", "email_otp"),
            ),
            (
                "create_account failed: registration_disallowed",
                ("registration_disallowed", "about_you"),
            ),
        )
        for message, expected in cases:
            with self.subTest(message=message):
                self.assertEqual(diagnostics._classify_failure(message), expected)

    def test_create_account_2xx_makes_later_409_a_duplicate_submission(self) -> None:
        task_id = "task_duplicate_create_account"
        session = self._session(37, task_id=task_id)
        session.record_protocol_http_exchange(
            method="POST",
            url="https://auth.openai.com/api/accounts/create_account",
            status=200,
            response_headers={"content-type": "application/json"},
            response_body=b"{}",
        )
        session.record_protocol_http_exchange(
            method="POST",
            url="https://auth.openai.com/api/accounts/create_account",
            status=409,
            response_headers={"content-type": "application/json"},
            response_body=b'{"error":{"code":"invalid_state"}}',
        )

        result = session.finalize(
            outcome="failed",
            error="registration failed after an unresolved SPA transition",
        )

        self.assertEqual(result["failure_code"], "post_signup_duplicate_submission")
        self.assertEqual(result["failure_stage"], "post_signup")

    def test_success_outcome_clears_stale_failure_and_uses_completed_title(self) -> None:
        task_id = "task_success_diagnosis"
        session = self._session(35, mode="full", task_id=task_id)
        session.record_protocol_http_exchange(
            method="POST",
            url="https://auth.openai.com/api/accounts/authorize",
            status=409,
            response_headers={"content-type": "application/json"},
            response_body=b'{"error":{"code":"invalid_state"}}',
        )

        result = session.finalize(
            outcome="success",
            error="stale failure from an earlier recovery branch",
            reason_code="invalid_state",
        )

        self.assertEqual(result["failure_code"], "")
        self.assertEqual(result["failure_stage"], "completed")
        item = diagnostics.list_registration_diagnostics(task_id)[0]
        self.assertEqual(item["failure_code"], "")
        self.assertEqual(item["failure_stage"], "completed")
        diagnosis = json.loads((session.final_dir / "diagnosis.json").read_text())
        self.assertEqual(diagnosis["analysis"]["title"], "注册尝试已完成")
        self.assertEqual(diagnosis["analysis"]["recommended_checks"], [])

    def test_smart_mode_keeps_only_configured_success_sample(self) -> None:
        first = self._session(10)
        first_result = first.finalize(
            outcome="success",
            email="first@example.com",
        )
        second = self._session(11)
        second_result = second.finalize(
            outcome="success",
            email="second@example.com",
        )

        self.assertEqual(first_result["status"], "ready")
        self.assertEqual(second_result["status"], "ready")
        items = diagnostics.list_registration_diagnostics(first.task_id)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["attempt_id"], 11)

    def test_finalize_retries_after_atomic_rename_without_changing_outcome(self) -> None:
        session = self._session(20)
        original_update = session._update_index
        failed_once = False

        def flaky_update(**kwargs):
            nonlocal failed_once
            if kwargs.get("status") == "ready" and not failed_once:
                failed_once = True
                raise RuntimeError("simulated index outage")
            return original_update(**kwargs)

        with mock.patch.object(session, "_update_index", side_effect=flaky_update):
            with self.assertRaisesRegex(RuntimeError, "simulated index outage"):
                session.finalize(
                    outcome="failed",
                    error="original failure",
                    email="retry@example.com",
                    reason_code="original_failure",
                )
            result = session.finalize(
                outcome="interrupted",
                error="fallback finalizer",
                reason_code="attempt_interrupted",
            )

        self.assertEqual(result["status"], "ready")
        item = diagnostics.list_registration_diagnostics(session.task_id)[0]
        self.assertEqual(item["outcome"], "failed")
        self.assertEqual(item["failure_code"], "original_failure")
        self.assertTrue(session.final_dir.is_dir())

    def test_quota_degrades_large_optional_artifacts_but_keeps_diagnosis(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"REGISTRATION_DIAGNOSTICS_ATTEMPT_MAX_BYTES": "800"},
        ):
            session = self._session(30, mode="full")
            video_dir = session.partial_dir / "video"
            video_dir.mkdir()
            (video_dir / "capture.webm").write_bytes(b"v" * 4096)
            (session.partial_dir / "network.har.zip").write_bytes(b"h" * 4096)
            (session.partial_dir / "trace.zip").write_bytes(b"t" * 4096)
            result = session.finalize(
                outcome="failed",
                error="quota failure",
                email="quota@example.com",
            )

        self.assertEqual(result["status"], "truncated")
        self.assertTrue((session.final_dir / "diagnosis.json").is_file())
        self.assertFalse((session.final_dir / "video.webm").exists())
        item = diagnostics.list_registration_diagnostics(session.task_id)[0]
        self.assertIn("attempt_quota_removed", item["truncation_reason"])
        self.assertEqual(set(item["files"]), {path.name for path in session.final_dir.iterdir() if path.is_file()})

    def test_path_boundary_pin_delete_and_prune_contract(self) -> None:
        session, _result = self._finalize_failure(40)
        item = diagnostics.set_registration_diagnostic_pinned(
            session.artifact_id,
            task_id=session.task_id,
            pinned=True,
        )
        self.assertTrue(item["pinned"])
        with self.assertRaisesRegex(ValueError, "取消固定"):
            diagnostics.delete_registration_diagnostic(
                session.artifact_id,
                task_id=session.task_id,
            )
        diagnostics.set_registration_diagnostic_pinned(
            session.artifact_id,
            task_id=session.task_id,
            pinned=False,
        )
        diagnostics.delete_registration_diagnostic(
            session.artifact_id,
            task_id=session.task_id,
        )
        self.assertEqual(diagnostics.list_registration_diagnostics(session.task_id), [])

        with Session(self.engine) as db_session:
            escaped = RegistrationDiagnosticArtifactModel(
                task_id="task_escape",
                attempt_id=1,
                attempt_number=1,
                status="ready",
                relative_path="../outside",
            )
            db_session.add(escaped)
            db_session.commit()
            db_session.refresh(escaped)
            escaped_id = int(escaped.id or 0)
        with self.assertRaisesRegex(ValueError, "路径越界"):
            diagnostics.registration_diagnostic_path(
                escaped_id,
                task_id="task_escape",
            )

    def test_api_list_download_pin_prune_and_delete(self) -> None:
        session, _result = self._finalize_failure(
            50,
            task_id="task_api_diagnostics",
        )
        app = FastAPI()
        app.include_router(diagnostics_api.router, prefix="/api")
        client = TestClient(app)

        listed = client.get(f"/api/tasks/{session.task_id}/diagnostics")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["summary"]["failure_count"], 1)
        artifact_id = listed.json()["items"][0]["id"]

        detail = client.get(
            f"/api/tasks/{session.task_id}/diagnostics/{artifact_id}"
        )
        self.assertEqual(detail.status_code, 200)
        single = client.get(
            f"/api/tasks/{session.task_id}/diagnostics/{artifact_id}/files/diagnosis.json"
        )
        self.assertEqual(single.status_code, 200)
        bundle = client.get(
            f"/api/tasks/{session.task_id}/diagnostics/{artifact_id}/download"
        )
        self.assertEqual(bundle.status_code, 200)
        self.assertTrue(bundle.content.startswith(b"PK"))

        pinned = client.post(
            f"/api/tasks/{session.task_id}/diagnostics/{artifact_id}/pin",
            json={"pinned": True},
        )
        self.assertEqual(pinned.status_code, 200)
        blocked_delete = client.delete(
            f"/api/tasks/{session.task_id}/diagnostics/{artifact_id}"
        )
        self.assertEqual(blocked_delete.status_code, 409)
        client.post(
            f"/api/tasks/{session.task_id}/diagnostics/{artifact_id}/pin",
            json={"pinned": False},
        )
        pruned = client.post(
            f"/api/tasks/{session.task_id}/diagnostics/prune"
        )
        self.assertEqual(pruned.status_code, 200)
        capacity = client.get("/api/tasks/registration-diagnostics/capacity")
        self.assertEqual(capacity.status_code, 200)
        deleted = client.delete(
            f"/api/tasks/{session.task_id}/diagnostics/{artifact_id}"
        )
        self.assertEqual(deleted.status_code, 200)


if __name__ == "__main__":
    unittest.main()
