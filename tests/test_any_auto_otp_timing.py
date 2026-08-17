import unittest
from unittest import mock

from services.chatgpt_core.any_auto import register as register_module


class AnyAutoOtpTimingTests(unittest.TestCase):
    @staticmethod
    def _engine():
        engine = register_module.RegistrationEngine.__new__(
            register_module.RegistrationEngine
        )
        engine.email = "existing@example.com"
        engine.password = "Preferred123!"
        engine._preferred_password = "Preferred123!"
        engine._device_id = None
        engine._password_sentinel = None
        engine._is_existing_account = False
        engine._otp_sent_at = None
        engine._log = mock.Mock()
        return engine

    def test_signup_otp_uses_signup_request_start_as_otp_cutoff(self):
        engine = self._engine()
        response = mock.Mock(status_code=200, text="", url="")
        response.json.return_value = {
            "page": {"type": "email_otp_verification"},
            "signup_mode": "email_signup",
            "original_screen_hint": "signup",
            "email_verification_mode": "passwordless_signup",
        }
        engine.session = mock.Mock()
        engine.session.post.return_value = response

        with mock.patch.object(
            register_module,
            "_otp_request_started_at",
            return_value=123.0,
        ):
            result = engine._submit_signup_form("device-fixed", None)

        self.assertTrue(result.success)
        self.assertFalse(result.is_existing_account)
        self.assertEqual(engine._otp_sent_at, 123.0)

    def test_passwordless_login_otp_is_existing_account(self):
        engine = self._engine()
        response = mock.Mock(status_code=200, text="", url="")
        response.json.return_value = {
            "page": {
                "type": "email_otp_verification",
                "payload": {"email_verification_mode": "passwordless_login"},
            },
            "original_screen_hint": "login",
        }
        engine.session = mock.Mock()
        engine.session.post.return_value = response

        with mock.patch.object(
            register_module,
            "_otp_request_started_at",
            return_value=124.0,
        ):
            result = engine._submit_signup_form("device-fixed", None)

        self.assertTrue(result.success)
        self.assertTrue(result.is_existing_account)
        self.assertEqual(engine._otp_sent_at, 124.0)

    def test_password_response_uses_request_start_when_it_auto_sends_otp(self):
        engine = self._engine()
        engine.http_client = mock.Mock(
            default_headers={"User-Agent": "Mozilla/5.0 Chrome/136.0.0.0"}
        )
        engine._load_create_account_password_page = mock.Mock()
        engine._device_id = "device-fixed"
        engine._check_sentinel = mock.Mock(
            return_value=register_module.SentinelPayload(
                p="requirements",
                c="challenge",
                flow="username_password_create",
                t="turnstile",
            )
        )
        engine._generate_password = mock.Mock(
            side_effect=["Generated123!A", "Generated123!B"]
        )
        response = mock.Mock(status_code=200, text="", url="")
        response.json.return_value = {
            "page": {"type": "email_otp_verification"},
        }
        engine.session = mock.Mock()
        engine.session.post.return_value = response

        with mock.patch.object(
            register_module,
            "_otp_request_started_at",
            return_value=456.0,
        ):
            ok, password = engine._register_password()

        self.assertTrue(ok)
        self.assertEqual(password, "Preferred123!")
        self.assertFalse(engine._is_existing_account)
        self.assertEqual(engine._otp_sent_at, 456.0)

    def test_explicit_send_updates_cutoff_only_after_success(self):
        engine = self._engine()
        engine.session = mock.Mock()
        success_response = mock.Mock(status_code=200)
        success_response.json.return_value = {
            "page": {"type": "email_otp_verification"},
        }
        engine.session.get.return_value = success_response

        with mock.patch.object(
            register_module,
            "_otp_request_started_at",
            return_value=789.0,
        ):
            self.assertTrue(engine._send_verification_code())

        self.assertEqual(engine._otp_sent_at, 789.0)

        engine._otp_sent_at = 789.0
        engine.session.get.return_value = mock.Mock(status_code=500)
        with mock.patch.object(
            register_module,
            "_otp_request_started_at",
            return_value=999.0,
        ):
            self.assertFalse(engine._send_verification_code())

        self.assertEqual(engine._otp_sent_at, 789.0)


if __name__ == "__main__":
    unittest.main()
