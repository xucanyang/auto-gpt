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
from services.chatgpt_core import sentinel_browser

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
        self.start_options = {}

    def start(self, **kwargs) -> None:
        self.started = True
        self.start_options = dict(kwargs)

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


class _FakeRequest:
    def __init__(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: dict | None = None,
        post_data: str = "",
        resource_type: str = "fetch",
        failure: str = "",
    ) -> None:
        self.url = url
        self.method = method
        self.headers = dict(headers or {})
        self.post_data = post_data
        self.resource_type = resource_type
        self.failure = failure


class _FakeResponse:
    def __init__(
        self,
        request: _FakeRequest,
        *,
        status: int,
        headers: dict | None = None,
        body: bytes = b"",
        status_text: str = "",
        body_error: Exception | None = None,
    ) -> None:
        self.request = request
        self.url = request.url
        self.status = status
        self.status_text = status_text
        self.headers = dict(headers or {})
        self._body = body
        self._body_error = body_error
        self.body_calls = 0

    def body(self) -> bytes:
        self.body_calls += 1
        if self._body_error is not None:
            raise self._body_error
        return self._body


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
        self.browser_concurrency_patch = mock.patch.object(
            sentinel_browser,
            "browser_capacity_max_concurrency",
            return_value=1,
        )
        self.browser_concurrency_patch.start()

    def tearDown(self) -> None:
        diagnostics._CURRENT_SESSION.set(None)
        self.browser_concurrency_patch.stop()
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
        self.assertNotIn("record_har_path", options)
        self.assertNotIn("record_har_mode", options)
        self.assertNotIn("record_har_content", options)
        self.assertIn("record_video_dir", options)

        context = _FakeContext()
        page = _FakePage()
        session.start_browser_capture(context, page)
        session.record_log("邮箱验证码 123456", "debug")
        session.stop_browser_capture(page, context)
        result = session.finalize(
            outcome="failed",
            error="验证码 123456 failed token=secret-token",
            email="operator@example.com",
            reason_code="otp_failed",
        )

        self.assertEqual(result["status"], "ready")
        self.assertTrue(context.tracing.started)
        self.assertEqual(
            context.tracing.start_options,
            {"screenshots": True, "snapshots": True, "sources": True},
        )
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

    def test_camoufox_capture_disables_trace_screenshots_only(self) -> None:
        session = self._session(
            47,
            mode="smart",
            task_id="task_camoufox_trace_without_screenshots",
        )
        context = _FakeContext()
        page = _FakePage()

        session.start_browser_capture(
            context,
            page,
            trace_screenshots=False,
        )
        session.stop_browser_capture(page, context)
        session.finalize(outcome="failed", error="upstream registration failed")

        self.assertEqual(
            context.tracing.start_options,
            {"screenshots": False, "snapshots": True, "sources": True},
        )
        for filename in (
            "trace.zip",
            "final-state.json",
            "final-page.html",
            "final-page.png",
        ):
            self.assertTrue((session.final_dir / filename).is_file())
        events = (session.final_dir / "events.jsonl").read_text(encoding="utf-8")
        self.assertIn('"trace_screenshots": false', events)

    def test_full_capture_gates_video_when_browser_concurrency_is_not_isolated(self) -> None:
        with mock.patch.object(
            sentinel_browser,
            "browser_capacity_max_concurrency",
            return_value=30,
        ):
            session = self._session(
                43,
                mode="full",
                task_id="task_video_concurrency_gate",
            )

        options = session.browser_context_options()
        self.assertEqual(options, {})
        self.assertNotIn("record_video_dir", options)
        self.assertFalse((session.partial_dir / "video").exists())
        spec = session.browser_worker_capture_spec()
        self.assertTrue(spec["video_requested"])
        self.assertFalse(spec["video_capture_enabled"])
        self.assertEqual(
            spec["video_unavailable_reason"],
            "disabled_by_concurrency_gate:max_concurrency=30;required_max_concurrency=1",
        )

        context = _FakeContext()
        page = _FakePage()
        session.start_browser_capture(context, page)
        session.stop_browser_capture(page, context)
        session.finalize(outcome="failed", error="upstream registration failed")

        diagnosis = json.loads((session.final_dir / "diagnosis.json").read_text())
        self.assertTrue(diagnosis["capture"]["trace"])
        self.assertTrue(diagnosis["capture"]["browser_har"])
        self.assertTrue(diagnosis["capture"]["final_dom"])
        self.assertTrue(diagnosis["capture"]["final_screenshot"])
        self.assertFalse(diagnosis["capture"]["video"])
        self.assertFalse(diagnosis["capture"]["video_capture_enabled"])
        self.assertEqual(
            diagnosis["capture"]["video_unavailable_reason"],
            spec["video_unavailable_reason"],
        )

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

    def test_browser_event_har_records_redirect_failure_and_redacted_key_body(self) -> None:
        session = self._session(44, mode="full", task_id="task_browser_event_har")
        context = _FakeContext()
        page = _FakePage()
        session.start_browser_capture(context, page)

        key_request = _FakeRequest(
            "https://auth.openai.com/api/accounts/email-otp/validate?code=123456",
            method="POST",
            headers={
                "authorization": "Bearer request-secret-token",
                "cookie": "login_session=raw-cookie",
                "content-type": "application/json",
            },
            post_data=json.dumps(
                {
                    "email": "raw-user@example.com",
                    "password": "Password123!",
                    "code": "123456",
                }
            ),
        )
        key_response = _FakeResponse(
            key_request,
            status=401,
            status_text="Unauthorized",
            headers={
                "content-type": "application/json",
                "content-length": "68",
                "set-cookie": "session=raw-response-cookie",
                "x-request-id": "request-id-1",
            },
            body=b'{"access_token":"response-secret-token","error":"invalid_otp"}',
        )
        page.listeners["request"](key_request)
        page.listeners["response"](key_response)
        page.listeners["requestfinished"](key_request)

        redirect_request = _FakeRequest(
            "https://auth.openai.com/api/accounts/authorize?token=raw-query-token",
            resource_type="document",
        )
        redirect_response = _FakeResponse(
            redirect_request,
            status=302,
            headers={
                "content-type": "text/html",
                "location": "https://chatgpt.com/auth/callback/openai?code=raw-auth-code",
            },
        )
        page.listeners["request"](redirect_request)
        page.listeners["response"](redirect_response)
        page.listeners["requestfinished"](redirect_request)

        failed_request = _FakeRequest(
            "https://chatgpt.com/api/auth/session?access_token=raw-query-token",
            failure="NS_BINDING_ABORTED token=raw-failure-token",
        )
        page.listeners["request"](failed_request)
        page.listeners["requestfailed"](failed_request)

        ignored_request = _FakeRequest("https://example.com/private?token=ignore-me")
        page.listeners["request"](ignored_request)
        page.listeners["requestfailed"](ignored_request)
        session.stop_browser_capture(page, context)

        archive_path = session.partial_dir / "network.har.zip"
        self.assertTrue(archive_path.is_file())
        self.assertTrue((session.partial_dir / "trace.zip").is_file())
        archive_before_repeat_finalize = archive_path.read_bytes()
        session._write_browser_har()
        self.assertEqual(archive_path.read_bytes(), archive_before_repeat_finalize)
        with zipfile.ZipFile(archive_path) as archive:
            self.assertEqual(archive.namelist(), ["network.har"])
            har = json.loads(archive.read("network.har"))
        self.assertEqual(har["log"]["version"], "1.2")
        self.assertEqual(
            har["log"]["creator"]["name"],
            "auto-gpt-browser-event-diagnostics",
        )
        entries = har["log"]["entries"]
        self.assertEqual([item["response"]["status"] for item in entries], [401, 302, 0])
        self.assertEqual(entries[0]["_diagnostic"]["responseBodyCapture"], "captured")
        self.assertEqual(entries[1]["response"]["redirectURL"], "https://chatgpt.com/auth/callback/openai")
        self.assertIn("NS_BINDING_ABORTED", entries[2]["_diagnostic"]["error"])
        self.assertNotIn("https://example.com/private", json.dumps(har))
        self.assertTrue((session.partial_dir / "key-http-responses.jsonl").is_file())
        serialized = json.dumps(har, ensure_ascii=False)
        for secret in (
            "123456",
            "raw-user@example.com",
            "Password123!",
            "request-secret-token",
            "raw-cookie",
            "response-secret-token",
            "raw-response-cookie",
            "raw-query-token",
            "raw-auth-code",
            "raw-failure-token",
        ):
            self.assertNotIn(secret, serialized)

    def test_smart_diagnostics_keeps_key_http_evidence_without_generating_har(self) -> None:
        session = self._session(49, mode="smart", task_id="task_smart_no_har")
        self.assertNotIn(
            "har",
            session.browser_worker_capture_spec()["required_artifacts"],
        )
        context = _FakeContext()
        page = _FakePage()
        session.start_browser_capture(context, page)

        request = _FakeRequest(
            "https://auth.openai.com/api/accounts/create_account",
            method="POST",
        )
        response = _FakeResponse(
            request,
            status=409,
            headers={"content-type": "application/json"},
            body=b'{"error":{"code":"invalid_state"}}',
        )
        page.listeners["request"](request)
        page.listeners["response"](response)
        page.listeners["requestfinished"](request)
        session.record_protocol_http_exchange(
            method="GET",
            url="https://chatgpt.com/api/auth/session",
            status=200,
            response_headers={"content-type": "application/json"},
            response_body=b'{"user":{"id":"account-1"}}',
        )
        session.stop_browser_capture(page, context)

        key_items = [
            json.loads(line)
            for line in (session.partial_dir / "key-http-responses.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        self.assertEqual(
            {item["transport"] for item in key_items},
            {"playwright_events", "curl_cffi"},
        )
        self.assertFalse((session.partial_dir / "browser-har.entries.jsonl").exists())
        self.assertFalse((session.partial_dir / "protocol-har.entries.jsonl").exists())
        self.assertFalse((session.partial_dir / "network.har.zip").exists())
        result = session.finalize(outcome="failed", error="invalid_state")

        self.assertEqual(result["status"], "ready")
        self.assertFalse((session.final_dir / "network.har.zip").exists())
        self.assertFalse((session.final_dir / "protocol.har.zip").exists())
        diagnosis = json.loads((session.final_dir / "diagnosis.json").read_text())
        self.assertFalse(diagnosis["capture"]["browser_har"])
        self.assertFalse(diagnosis["capture"]["protocol_har"])

    def test_browser_event_har_skips_known_large_response_without_reading_body(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"REGISTRATION_DIAGNOSTICS_RESPONSE_MAX_BYTES": "32"},
        ):
            session = self._session(45, mode="full", task_id="task_browser_large_body")
            context = _FakeContext()
            page = _FakePage()
            session.start_browser_capture(context, page)
            request = _FakeRequest(
                "https://auth.openai.com/api/accounts/create_account",
                method="POST",
            )
            response = _FakeResponse(
                request,
                status=500,
                headers={
                    "content-type": "application/json",
                    "content-length": "4096",
                },
                body_error=AssertionError("large body must not be loaded"),
            )
            page.listeners["request"](request)
            page.listeners["response"](response)
            page.listeners["requestfinished"](request)
            session.stop_browser_capture(page, context)

        self.assertEqual(response.body_calls, 0)
        with zipfile.ZipFile(session.partial_dir / "network.har.zip") as archive:
            entry = json.loads(archive.read("network.har"))["log"]["entries"][0]
        self.assertEqual(
            entry["response"]["content"]["text"],
            "[BODY_SKIPPED_BY_CONTENT_LENGTH_LIMIT]",
        )
        self.assertEqual(
            entry["_diagnostic"]["responseBodyCapture"],
            "content_length_limit",
        )

    def test_browser_event_har_flushes_unfinished_request_with_explicit_reason(self) -> None:
        session = self._session(46, mode="full", task_id="task_browser_pending_request")
        context = _FakeContext()
        page = _FakePage()
        session.start_browser_capture(context, page)
        request = _FakeRequest("https://platform.openai.com/login", resource_type="document")
        page.listeners["request"](request)
        session.stop_browser_capture(page, context)

        with zipfile.ZipFile(session.partial_dir / "network.har.zip") as archive:
            entry = json.loads(archive.read("network.har"))["log"]["entries"][0]
        self.assertEqual(entry["response"]["status"], 0)
        self.assertEqual(
            entry["_diagnostic"]["error"],
            "capture_stopped_before_request_finished",
        )

    def test_browser_event_har_enforces_realtime_byte_budget(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"REGISTRATION_DIAGNOSTICS_BROWSER_HAR_MAX_BYTES": "256"},
        ):
            session = self._session(48, mode="full", task_id="task_browser_har_budget")
            context = _FakeContext()
            page = _FakePage()
            session.start_browser_capture(context, page)
            request = _FakeRequest(
                "https://auth.openai.com/api/accounts/authorize",
                headers={"x-large-safe-header": "x" * 4096},
            )
            response = _FakeResponse(
                request,
                status=200,
                headers={"content-type": "application/json"},
                body=b"{}",
            )
            page.listeners["request"](request)
            page.listeners["response"](response)
            page.listeners["requestfinished"](request)
            session.stop_browser_capture(page, context)

        with zipfile.ZipFile(session.partial_dir / "network.har.zip") as archive:
            har = json.loads(archive.read("network.har"))
        self.assertEqual(har["log"]["entries"], [])
        self.assertEqual(har["log"]["_diagnostic"]["droppedEntries"], 1)
        self.assertTrue(
            any(
                warning.startswith("browser_har_byte_limit_reached:256")
                for warning in session._warnings
            )
        )

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

    def test_worker_termination_recovers_incremental_browser_har_journal(self) -> None:
        session = self._session(47, mode="full", task_id="task_worker_har_recovery")
        spec = json.loads(json.dumps(session.browser_worker_capture_spec()))
        worker_session = diagnostics.RegistrationDiagnosticSession.attach_browser_worker_capture(
            spec
        )
        self.assertIsNotNone(worker_session)
        request = _FakeRequest(
            "https://auth.openai.com/api/accounts/authorize",
            resource_type="document",
        )
        response = _FakeResponse(
            request,
            status=503,
            headers={"content-type": "application/json"},
            body=b'{"error":{"code":"temporarily_unavailable"}}',
        )
        worker_session._browser_har_begin_request(request)
        worker_session._browser_har_observe_response(response)
        worker_session._browser_har_finish_request(request)
        worker_session._detach_context()

        result = session.finalize(
            outcome="failed",
            error="browser worker terminated",
        )

        self.assertEqual(result["status"], "ready")
        with zipfile.ZipFile(session.final_dir / "network.har.zip") as archive:
            har = json.loads(archive.read("network.har"))
        self.assertEqual(har["log"]["entries"][0]["response"]["status"], 503)
        diagnosis = json.loads((session.final_dir / "diagnosis.json").read_text())
        self.assertTrue(diagnosis["capture"]["worker_artifacts"]["har"]["available"])
        self.assertEqual(
            diagnosis["capture"]["worker_artifacts"]["har"]["reason"],
            "",
        )
        self.assertIn(
            "browser_har_recovered_from_worker_journal",
            diagnosis["warnings"],
        )

    @unittest.skipIf(browser_register is None, "Camoufox is only available in the runtime image")
    def test_any_auto_uses_one_core_diagnostic_context_and_flush_order(self) -> None:
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
            raise RuntimeError("simulated context close failure")

        context.close.side_effect = fail_context_close
        diagnostic_session = mock.Mock(enabled=True)
        diagnostic_session.browser_context_options.return_value = {}
        def fail_diagnostic_stop(*_args) -> None:
            cleanup_order.append("diagnostic_stop")
            raise RuntimeError("simulated trace stop failure")

        diagnostic_session.stop_browser_capture.side_effect = fail_diagnostic_stop
        session = types.SimpleNamespace(
            browser=browser,
            context=context,
            page=page,
            token="diagnostic-context",
            browser_backend="camoufox_firefox",
        )
        with (
            mock.patch.object(
                browser_register,
                "shared_browser_registration_session",
                return_value=contextlib.nullcontext(session),
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
        ):
            worker = ChatGPTBrowserRegister(
                headless=True,
                otp_callback=lambda: "123456",
                log_fn=lambda _message: None,
            )
            result = worker.run("user@example.com", "Password123!")

        self.assertTrue(result["success"])
        self.assertEqual(shared_session.call_count, 1)
        self.assertEqual(
            shared_session.call_args_list[0].kwargs["extra_context_options"],
            {},
        )
        diagnostic_session.mark_video_capture_unavailable.assert_not_called()
        diagnostic_session.start_browser_capture.assert_called_once_with(
            context,
            page,
            trace_screenshots=False,
        )
        self.assertEqual(cleanup_order, ["diagnostic_stop", "context_close"])
        self.assertEqual(diagnostic_session.record_event.call_count, 2)

    @unittest.skipIf(browser_register is None, "Camoufox is only available in the runtime image")
    def test_any_auto_only_retries_video_for_explicit_unsupported_capability(self) -> None:
        page = mock.Mock(url="https://chatgpt.com/")
        context = mock.Mock()
        context.cookies.return_value = [
            {
                "name": "__Secure-next-auth.session-token",
                "value": "session-demo",
                "domain": "chatgpt.com",
            }
        ]
        page.context = context
        session = types.SimpleNamespace(
            browser=mock.Mock(),
            context=context,
            page=page,
            token="diagnostic-context",
            browser_backend="camoufox_firefox",
        )
        failed_video_context = mock.MagicMock()
        failed_video_context.__enter__.side_effect = RuntimeError(
            "Browser.setScreencastOptions: method is not supported"
        )
        diagnostic_session = mock.Mock(enabled=True)
        diagnostic_session.browser_context_options.return_value = {
            "record_video_dir": "/tmp/video",
        }

        with (
            mock.patch.object(
                browser_register,
                "shared_browser_registration_session",
                side_effect=[failed_video_context, contextlib.nullcontext(session)],
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

        self.assertTrue(result["success"])
        self.assertEqual(shared_session.call_count, 2)
        self.assertIn(
            "record_video_dir",
            shared_session.call_args_list[0].kwargs["extra_context_options"],
        )
        self.assertNotIn(
            "record_video_dir",
            shared_session.call_args_list[1].kwargs["extra_context_options"],
        )
        diagnostic_session.mark_video_capture_unavailable.assert_called_once()
        diagnostic_session.start_browser_capture.assert_called_once_with(
            context,
            page,
            trace_screenshots=False,
        )

    @unittest.skipIf(browser_register is None, "Camoufox is only available in the runtime image")
    def test_any_auto_browser_closed_never_retries_the_diagnostic_context(self) -> None:
        fallback_page = mock.Mock(url="https://chatgpt.com/")
        fallback_context = mock.Mock()
        fallback_context.cookies.return_value = [
            {
                "name": "__Secure-next-auth.session-token",
                "value": "session-demo",
                "domain": "chatgpt.com",
            }
        ]
        fallback_page.context = fallback_context
        fallback_session = types.SimpleNamespace(
            browser=mock.Mock(),
            context=fallback_context,
            page=fallback_page,
            token="plain-fallback-context",
        )
        failed_diagnostic_context = mock.MagicMock()
        failed_diagnostic_context.__enter__.side_effect = RuntimeError(
            "BrowserContext.new_page: Browser closed"
        )
        diagnostic_session = mock.Mock(enabled=True)
        diagnostic_session.browser_context_options.return_value = {}

        with (
            mock.patch.object(
                browser_register,
                "shared_browser_registration_session",
                side_effect=[
                    failed_diagnostic_context,
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
        ):
            worker = ChatGPTBrowserRegister(
                headless=True,
                otp_callback=lambda: "123456",
                log_fn=lambda _message: None,
            )
            result = worker.run("user@example.com", "Password123!")

        self.assertTrue(result["success"])
        self.assertEqual(shared_session.call_count, 2)
        self.assertEqual(
            shared_session.call_args_list[0].kwargs["extra_context_options"],
            {},
        )
        self.assertEqual(
            shared_session.call_args_list[1].kwargs["extra_context_options"],
            {},
        )
        diagnostic_session.start_browser_capture.assert_not_called()
        diagnostic_session.mark_video_capture_unavailable.assert_not_called()
        self.assertTrue(
            any(
                call.args[1] == "browser_diagnostic_context_setup_failed"
                for call in diagnostic_session.record_event.call_args_list
            )
        )

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
        session = self._session(2, mode="full")
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

    def test_plain_browser_closed_signature_is_classified_as_browser_crash(self) -> None:
        self.assertEqual(
            diagnostics._classify_failure(
                "any_auto_browser_exception: Page.goto: Browser closed"
            ),
            ("browser_crashed", "browser"),
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
        session = self._session(50, task_id="task_api_diagnostics")
        session.record_protocol_http_exchange(
            method="GET",
            url="https://chatgpt.com/api/auth/session",
            status=401,
            response_headers={"content-type": "application/json"},
            response_body=b'{"error":"session_expired"}',
        )
        session.finalize(
            outcome="failed",
            error="email OTP failed",
            email="operator@example.com",
            reason_code="otp_failed",
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
        http_evidence = client.get(
            f"/api/tasks/{session.task_id}/diagnostics/{artifact_id}/files/"
            "key-http-responses.jsonl"
        )
        self.assertEqual(http_evidence.status_code, 200)
        self.assertIn(b"curl_cffi", http_evidence.content)
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
