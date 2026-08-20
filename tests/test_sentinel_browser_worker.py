import io
import json
import os
import sys
import unittest
from unittest import mock

from services.chatgpt_core.any_auto import transport
from services.chatgpt_core.any_auto.transport import AnyAutoRegistrationResult
from services.chatgpt_core import sentinel_browser_worker


class SentinelBrowserWorkerTests(unittest.TestCase):
    def test_any_auto_registration_round_trips_callbacks_and_skips_nested_capacity(self):
        captured = {}

        def fake_registration(**kwargs):
            captured.update(kwargs)
            otp_result = kwargs["otp_callback"](
                {
                    "action": "acquire",
                    "challenge_id": "challenge-1",
                    "generation": 2,
                    "otp_sent_at": 123.5,
                    "timeout": 90,
                    "phase": "browser_register_email_otp",
                    "exclude_codes": ["111111"],
                }
            )
            self.assertEqual(otp_result["code"], "123456")
            self.assertEqual(otp_result["message_id"], "message-2")
            self.assertEqual(kwargs["phone_callback"](), "+15555550123")
            return AnyAutoRegistrationResult(
                success=True,
                email=kwargs["email"],
                password=kwargs["password"],
                access_token="at-demo",
                session_token="session-demo",
                cookies="__Secure-next-auth.session-token=session-demo",
                cookie_header="__Secure-next-auth.session-token=session-demo",
                transport="any_auto_browser",
                executor="headless",
            )

        request = {
            "operation": "any_auto_browser_registration",
            "payload": {
                "email": "buyer@example.com",
                "password": "Password123!",
                "proxy_url": None,
                "headless": True,
                "profile_name": "Example User",
                "profile_birthdate": "1990-01-02",
                "login_only": False,
                "browser_fingerprint": {},
                "phone_callback_enabled": True,
            },
        }
        control_input = "\n".join(
            (
                json.dumps(request),
                json.dumps(
                    {
                        "type": "callback_response",
                        "id": "callback-1",
                        "value": {
                            "code": "123456",
                            "message_id": "message-2",
                            "received_at": 124.0,
                            "challenge_id": "challenge-1",
                            "generation": 2,
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "callback_response",
                        "id": "callback-2",
                        "value": "+15555550123",
                    }
                ),
                "",
            )
        )
        read_fd, write_fd = os.pipe()
        try:
            with (
                mock.patch.object(
                    transport,
                    "run_any_auto_browser_registration",
                    side_effect=fake_registration,
                ),
                mock.patch.object(sys, "stdin", io.StringIO(control_input)),
                mock.patch.object(
                    sys,
                    "argv",
                    ["sentinel_browser_worker", str(write_fd)],
                ),
            ):
                exit_code = sentinel_browser_worker.main()

            raw_messages = os.read(read_fd, 65536).decode("utf-8")
        finally:
            try:
                os.close(read_fd)
            except OSError:
                pass
            try:
                os.close(write_fd)
            except OSError:
                pass

        messages = [json.loads(line) for line in raw_messages.splitlines()]
        self.assertEqual(exit_code, 0)
        self.assertEqual(
            [message["name"] for message in messages if message["type"] == "callback_request"],
            ["otp", "phone"],
        )
        otp_request = next(
            message
            for message in messages
            if message["type"] == "callback_request" and message["name"] == "otp"
        )
        self.assertEqual(otp_request["payload"]["challenge_id"], "challenge-1")
        self.assertEqual(otp_request["payload"]["generation"], 2)
        self.assertEqual(otp_request["payload"]["exclude_codes"], ["111111"])
        result_message = next(
            message for message in messages if message["type"] == "result"
        )
        self.assertTrue(result_message["value"]["success"])
        self.assertTrue(captured["capacity_managed_externally"])
        self.assertIsNone(captured["stop_check"])


if __name__ == "__main__":
    unittest.main()
