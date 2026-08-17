import base64
import json
import types
import unittest
from unittest import mock

from curl_cffi import requests as cffi_requests

from services.chatgpt_core.access_token_only_registration_engine import (
    AccessTokenOnlyRegistrationEngine,
)
from services.chatgpt_core.any_auto import register as register_module


class _EmailService:
    service_type = types.SimpleNamespace(value="test_mail")

    def create_email(self):
        return {"email": "user@example.com", "service_id": "mail-1"}

    def get_verification_code(self, **_kwargs):
        return "123456"


def _response(status, payload=None, *, text="", url="https://auth.openai.com/"):
    response = mock.Mock(status_code=status, text=text, url=url, headers={})
    if payload is None:
        response.json.side_effect = ValueError("not json")
        response.content = text.encode()
    else:
        response.json.return_value = payload
        response.content = json.dumps(payload).encode()
        response.text = json.dumps(payload)
        response.headers = {"content-type": "application/json"}
    return response


def _jwt(payload):
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    return f"{encode({'alg': 'none'})}.{encode(payload)}.signature"


class AnyAutoProtocolFlowTests(unittest.TestCase):
    def _engine(self, **kwargs):
        engine = register_module.RegistrationEngine(
            _EmailService(),
            profile_name="Alice Smith",
            profile_birthdate="1990-01-02",
            **kwargs,
        )
        engine.email = "user@example.com"
        engine.password = "Preferred123!"
        engine._preferred_password = engine.password
        engine._device_id = "device-1"
        engine.http_client = mock.Mock(
            default_headers={"User-Agent": "Mozilla/5.0 Chrome/146.0.0.0"}
        )
        engine._load_create_account_password_page = mock.Mock(return_value=True)
        engine._check_sentinel = mock.Mock(
            return_value=register_module.SentinelPayload(
                p="requirements",
                c="challenge",
                flow="username_password_create",
                t="turnstile",
            )
        )
        return engine

    def _post_signup_engine(self):
        engine = self._engine()
        engine.http_client.close = mock.Mock()
        engine._check_ip_location = mock.Mock(return_value=(True, "GB"))
        engine._create_email = mock.Mock(
            side_effect=lambda: setattr(engine, "email", "user@example.com") or True
        )
        engine._init_session = mock.Mock(return_value=True)
        engine._start_oauth = mock.Mock(return_value=True)
        engine._get_device_id = mock.Mock(return_value="device-1")
        engine._submit_signup_form = mock.Mock(
            return_value=register_module.SignupFormResult(
                success=True,
                page_type="create_account_password",
                response_data={"page": {"type": "create_account_password"}},
            )
        )
        engine._register_password = mock.Mock(
            side_effect=lambda: setattr(engine, "_password_page_type", "about_you")
            or (True, "Preferred123!")
        )
        engine._create_user_account = mock.Mock(
            side_effect=lambda: (
                setattr(
                    engine,
                    "_create_account_continue_url",
                    "https://chatgpt.com/api/auth/callback/openai?code=demo",
                )
                or setattr(engine, "_signup_committed", True)
                or True
            )
        )
        return engine

    def test_password_business_rejection_is_not_replayed(self):
        engine = self._engine()
        engine.session = mock.Mock()
        engine.session.post.return_value = _response(
            400,
            {
                "error": {
                    "code": "registration_disallowed",
                    "message": "Sorry, we cannot create your account.",
                }
            },
        )

        ok, password = engine._register_password()

        self.assertFalse(ok)
        self.assertIsNone(password)
        engine.session.post.assert_called_once()
        failure = engine._last_protocol_failure
        self.assertEqual(failure.code, "registration_disallowed")
        self.assertEqual(failure.stage, "password")
        self.assertFalse(failure.retriable)

    def test_password_policy_rejection_gets_one_fresh_sentinel_retry(self):
        engine = self._engine()
        engine.session = mock.Mock()
        engine.session.post.side_effect = [
            _response(
                400,
                {
                    "error": {
                        "code": "password_too_weak",
                        "message": "Password does not meet requirements",
                    }
                },
            ),
            _response(200, {"page": {"type": "email_otp_send"}}),
        ]
        engine._generate_password = mock.Mock(return_value="Replacement456!")

        ok, password = engine._register_password()

        self.assertTrue(ok)
        self.assertEqual(password, "Replacement456!")
        self.assertEqual(engine.session.post.call_count, 2)
        self.assertEqual(engine._check_sentinel.call_count, 2)
        self.assertEqual(engine._password_page_type, "email_otp_send")

    def test_create_account_uses_task_frozen_profile_and_requires_dump(self):
        engine = self._engine()
        engine.session = mock.Mock()
        engine.session.get.return_value = _response(200, {})
        engine.session.post.return_value = _response(
            200,
            {
                "page": {"type": "external_url"},
                "continue_url": "https://chatgpt.com/api/auth/callback/openai?code=demo",
            },
        )

        self.assertTrue(engine._create_user_account())

        self.assertIn("client_auth_session_dump", engine.session.get.call_args.args[0])
        body = json.loads(engine.session.post.call_args.kwargs["data"])
        self.assertEqual(
            body,
            {"name": "Alice Smith", "birthdate": "1990-01-02"},
        )

    def test_create_account_2xx_invalid_response_commits_without_retry(self):
        engine = self._engine()
        engine.session = mock.Mock()
        engine.session.get.return_value = _response(200, {})
        engine.session.post.return_value = _response(204)

        self.assertFalse(engine._create_user_account())
        self.assertTrue(engine._signup_committed)
        self.assertEqual(engine._last_protocol_failure.code, "create_account_invalid_response")
        self.assertFalse(engine._last_protocol_failure.retriable)


    def test_web_session_polls_until_all_required_material_is_present(self):
        engine = self._engine()
        session = cffi_requests.Session()
        session.cookies.set(
            "__Secure-next-auth.session-token",
            "session-cookie",
            domain="chatgpt.com",
            secure=True,
        )
        access_token = _jwt(
            {
                "https://api.openai.com/auth": {
                    "chatgpt_account_id": "account-1",
                    "chatgpt_user_id": "user-1",
                }
            }
        )
        empty = _response(200, {})
        complete = _response(
            200,
            {
                "accessToken": access_token,
                "sessionToken": "session-cookie",
                "account": {"id": "account-1"},
                "user": {"id": "user-1"},
            },
        )

        def get(url, **_kwargs):
            if str(url).endswith("/api/auth/session"):
                return empty if get.session_calls == 0 else complete
            return _response(200, {})

        get.session_calls = 0

        def counted_get(url, **kwargs):
            response = get(url, **kwargs)
            if str(url).endswith("/api/auth/session"):
                get.session_calls += 1
            return response

        session.get = mock.Mock(side_effect=counted_get)
        engine.session = session

        with mock.patch.object(register_module.time, "sleep"):
            result = engine._capture_chatgpt_web_session()

        self.assertEqual(result["account_id"], "account-1")
        self.assertEqual(result["session_token"], "session-cookie")
        self.assertIn("session-token=session-cookie", result["cookie_header"])
        self.assertEqual(engine._session_poll_attempts, 2)

    def test_current_state_machine_sends_otp_only_for_email_otp_send(self):
        engine = self._engine()
        engine.http_client.close = mock.Mock()
        engine._check_ip_location = mock.Mock(return_value=(True, "GB"))
        engine._create_email = mock.Mock(side_effect=lambda: setattr(engine, "email", "user@example.com") or True)
        engine._init_session = mock.Mock(return_value=True)
        engine._start_oauth = mock.Mock(return_value=True)
        engine._get_device_id = mock.Mock(return_value="device-1")
        engine._submit_signup_form = mock.Mock(
            return_value=register_module.SignupFormResult(
                success=True,
                page_type="create_account_password",
                response_data={"page": {"type": "create_account_password"}},
            )
        )

        def register_password():
            engine._password_page_type = "email_otp_send"
            return True, "Preferred123!"

        engine._register_password = mock.Mock(side_effect=register_password)
        engine._send_verification_code = mock.Mock(return_value=True)
        engine._get_verification_code = mock.Mock(return_value="123456")

        def validate_otp(_code):
            engine._otp_page_type = "about_you"
            return True

        engine._validate_verification_code = mock.Mock(side_effect=validate_otp)

        def create_account():
            engine._create_account_continue_url = (
                "https://chatgpt.com/api/auth/callback/openai?code=demo"
            )
            return True

        engine._create_user_account = mock.Mock(side_effect=create_account)
        engine._follow_protocol_callback = mock.Mock(return_value=True)
        engine._capture_chatgpt_web_session = mock.Mock(
            return_value={
                "access_token": "access-token",
                "session_token": "session-token",
                "cookie_header": "session-cookie=value",
                "account_id": "account-1",
                "workspace_id": "account-1",
            }
        )

        result = engine.run()

        self.assertTrue(result.success)
        engine._send_verification_code.assert_called_once()
        engine._validate_verification_code.assert_called_once_with("123456")
        self.assertEqual(result.metadata["protocol_stage"], "completed")
        engine.http_client.close.assert_called_once()

    def test_protocol_otp_timeout_resends_on_same_session(self):
        engine = self._engine()
        engine.email_service.get_verification_code = mock.Mock(
            side_effect=[None, "654321"]
        )
        engine._send_verification_code = mock.Mock(return_value=True)

        code = engine._get_verification_code(
            timeout=120,
            resend_timeout=90,
            resend=True,
        )

        self.assertEqual(code, "654321")
        self.assertEqual(engine._otp_resend_count, 1)
        engine._send_verification_code.assert_called_once_with(
            referer="https://auth.openai.com/email-verification",
            record_failure=False,
        )
        self.assertEqual(
            [call.kwargs["timeout"] for call in engine.email_service.get_verification_code.call_args_list],
            [120, 90],
        )

    def test_send_verification_code_accepts_any_successful_2xx(self):
        engine = self._engine()
        engine.session = mock.Mock()
        engine.session.get.return_value = _response(204)

        self.assertTrue(engine._send_verification_code())
        self.assertEqual(engine._otp_send_count, 1)

    def test_send_verification_code_keeps_protocol_identity_headers(self):
        engine = self._engine()
        engine.session = mock.Mock()
        engine.session.get.return_value = _response(200)

        self.assertTrue(
            engine._send_verification_code(
                referer="https://auth.openai.com/email-verification"
            )
        )
        headers = engine.session.get.call_args.kwargs["headers"]
        self.assertEqual(headers["oai-device-id"], "device-1")
        self.assertEqual(headers["Origin"], "https://auth.openai.com")
        self.assertEqual(headers["Accept"], "application/json, text/plain, */*")
        self.assertIn("x-datadog-trace-id", headers)

    def test_initial_email_otp_verification_advances_signup_without_resend(self):
        engine = self._post_signup_engine()
        engine._submit_signup_form = mock.Mock(
            return_value=register_module.SignupFormResult(
                success=True,
                page_type="email_otp_verification",
                response_data={
                    "page": {
                        "type": "email_otp_verification",
                        "payload": {
                            "email_verification_mode": "passwordless_signup",
                        },
                    },
                },
            )
        )
        engine._send_verification_code = mock.Mock(return_value=True)
        engine._get_verification_code = mock.Mock(return_value="123456")
        engine._validate_verification_code = mock.Mock(
            side_effect=lambda _code: setattr(engine, "_otp_page_type", "about_you")
            or True
        )
        engine._follow_protocol_callback = mock.Mock(return_value=True)
        engine._capture_chatgpt_web_session = mock.Mock(
            return_value={
                "access_token": "access-token",
                "session_token": "session-token",
                "cookie_header": "session-cookie=value",
                "account_id": "account-1",
                "workspace_id": "account-1",
            }
        )

        result = engine.run()

        self.assertTrue(result.success)
        engine._register_password.assert_not_called()
        engine._send_verification_code.assert_not_called()
        engine._validate_verification_code.assert_called_once_with("123456")
        engine._create_user_account.assert_called_once()

    def test_post_signup_callback_failure_is_pending_and_never_retriable(self):
        engine = self._post_signup_engine()

        def fail_callback(_url):
            engine._record_failure(
                "oauth_callback_failed",
                "oauth_callback",
                "callback HTTP 503",
                http_status=503,
                retriable=True,
            )
            return False

        engine._follow_protocol_callback = mock.Mock(side_effect=fail_callback)
        engine._capture_chatgpt_web_session = mock.Mock()

        result = engine.run()

        self.assertTrue(result.success)
        self.assertEqual(result.source, "registered_auth_pending")
        self.assertTrue(result.metadata["registration_signup_committed"])
        self.assertTrue(result.metadata["registered_auth_pending"])
        self.assertTrue(result.metadata["session_capture_pending"])
        self.assertEqual(
            result.metadata["registration_post_signup_failure_code"],
            "oauth_callback_failed",
        )
        self.assertFalse(result.metadata["protocol_retriable"])
        engine._capture_chatgpt_web_session.assert_not_called()
        engine._create_user_account.assert_called_once()
        engine.http_client.close.assert_called_once()

    def test_post_signup_web_session_failure_is_pending_and_not_replayed(self):
        engine = self._post_signup_engine()
        engine._follow_protocol_callback = mock.Mock(return_value=True)
        engine._capture_chatgpt_web_session = mock.Mock(
            side_effect=lambda: (
                engine._record_failure(
                    "web_session_incomplete",
                    "web_session",
                    "session API missing access token",
                    http_status=200,
                    retriable=True,
                )
                and None
            )
        )

        result = engine.run()

        self.assertTrue(result.success)
        self.assertEqual(result.source, "registered_auth_pending")
        self.assertTrue(result.metadata["registered_auth_pending"])
        self.assertEqual(result.metadata["protocol_failure_stage"], "web_session")
        self.assertFalse(result.metadata["protocol_retriable"])
        engine._submit_signup_form.assert_called_once()
        engine._create_user_account.assert_called_once()

    def test_existing_account_state_routes_out_before_password_submission(self):
        engine = self._engine()
        engine.http_client.close = mock.Mock()
        engine._check_ip_location = mock.Mock(return_value=(True, "GB"))
        engine._create_email = mock.Mock(side_effect=lambda: setattr(engine, "email", "user@example.com") or True)
        engine._init_session = mock.Mock(return_value=True)
        engine._start_oauth = mock.Mock(return_value=True)
        engine._get_device_id = mock.Mock(return_value="device-1")
        engine._submit_signup_form = mock.Mock(
            return_value=register_module.SignupFormResult(
                success=True,
                page_type="email_otp_verification",
                is_existing_account=True,
                response_data={"page": {"type": "email_otp_verification"}},
            )
        )
        engine._register_password = mock.Mock()

        result = engine.run()

        self.assertFalse(result.success)
        self.assertIn("user_already_exists", result.error_message)
        self.assertEqual(
            result.metadata["protocol_failure_code"],
            "existing_account_detected",
        )
        engine._register_password.assert_not_called()

    def test_http_trace_is_forwarded_to_protocol_har_recorder(self):
        engine = self._engine()

        class FakeSession:
            def __init__(self):
                self.headers = {"User-Agent": "test"}

            def request(self, method, url, **_kwargs):
                return _response(200, {"ok": True}, url=url)

            def get(self, url, **kwargs):
                return self.request("GET", url, **kwargs)

        engine.session = FakeSession()
        engine._install_http_trace()
        with mock.patch(
            "services.chatgpt_core.registration_diagnostics.record_registration_protocol_http_exchange"
        ) as recorder:
            response = engine.session.get("https://auth.openai.com/api/accounts/authorize")

        self.assertEqual(response.status_code, 200)
        recorder.assert_called_once()
        self.assertEqual(recorder.call_args.kwargs["status"], 200)
        self.assertEqual(recorder.call_args.kwargs["method"], "GET")

    def test_structured_protocol_retry_contract_takes_precedence(self):
        engine = AccessTokenOnlyRegistrationEngine(_EmailService())

        self.assertFalse(
            engine._should_retry(
                "protocol_failure code=registration_disallowed stage=about_you "
                "retriable=false http=400"
            )
        )
        self.assertTrue(
            engine._should_retry(
                "protocol_failure code=upstream_server_error stage=oauth_start "
                "retriable=true http=503"
            )
        )


if __name__ == "__main__":
    unittest.main()
