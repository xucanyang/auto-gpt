import unittest

from core.base_mailbox import TempMailLocalMailbox, TempMailReadyAuthError


class _Response:
    def __init__(self, status_code, payload, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or str(payload)

    def json(self):
        return self._payload


class _FakeTempMailLocalMailbox(TempMailLocalMailbox):
    def __init__(self, responses):
        super().__init__(
            api_url="http://tempmail-api-1:8080",
            api_key="test-key",
            ttl_minutes=60,
            permanent=False,
        )
        self.responses = list(responses)
        self.calls = []

    def _request(self, method: str, path: str, *, timeout: int, **kwargs):
        self.calls.append({"method": method, "path": path, "timeout": timeout, **kwargs})
        if not self.responses:
            raise AssertionError("unexpected TempMail request")
        return self.responses.pop(0)


class _MailboxMessages(TempMailLocalMailbox):
    def __init__(self, messages, details):
        super().__init__(
            api_url="http://tempmail-api-1:8080",
            api_key="test-key",
            ttl_minutes=60,
            permanent=False,
        )
        self.messages = list(messages)
        self.details = dict(details)

    def _list_emails(self, _mailbox_id):
        return list(self.messages)

    def _get_email_detail(self, _mailbox_id, message_id):
        return dict(self.details.get(message_id) or {})


class TempMailLocalMailboxTests(unittest.TestCase):
    def test_ensure_mailbox_by_email_reuses_existing_exact_address(self):
        mailbox = _FakeTempMailLocalMailbox([
            _Response(200, {"data": [
                {"id": "mailbox-1", "full_address": "user@example.com"},
            ]}),
        ])

        account = mailbox.ensure_mailbox_by_email("USER@example.com")

        self.assertEqual(account.email, "user@example.com")
        self.assertEqual(account.account_id, "mailbox-1")
        self.assertEqual(account.extra["mailbox_action"], "reused_existing")
        self.assertEqual([call["method"] for call in mailbox.calls], ["GET"])
        self.assertEqual(mailbox.calls[0]["params"]["q"], "user@example.com")

    def test_ensure_mailbox_by_email_creates_exact_address_when_missing(self):
        mailbox = _FakeTempMailLocalMailbox([
            _Response(200, {"data": []}),
            _Response(201, {"mailbox": {
                "id": "mailbox-2",
                "address": "user",
                "domain": "example.com",
                "full_address": "user@example.com",
            }}),
        ])

        account = mailbox.ensure_mailbox_by_email("user@example.com")

        self.assertEqual(account.email, "user@example.com")
        self.assertEqual(account.account_id, "mailbox-2")
        self.assertEqual(account.extra["mailbox_action"], "created_exact_address")
        self.assertEqual(mailbox.calls[1]["json"]["address"], "user")
        self.assertEqual(mailbox.calls[1]["json"]["domain"], "example.com")

    def test_find_mailbox_falls_back_when_legacy_api_ignores_q(self):
        mailbox = _FakeTempMailLocalMailbox([
            _Response(200, {"data": [
                {"id": "other-mailbox", "full_address": "other@example.com"},
            ]}),
            _Response(200, {"data": [
                {"id": "mailbox-3", "full_address": "user@example.com"},
            ]}),
        ])

        account = mailbox.find_mailbox_by_email("user@example.com")

        self.assertIsNotNone(account)
        self.assertEqual(account.account_id, "mailbox-3")
        self.assertEqual(mailbox.calls[0]["params"]["q"], "user@example.com")
        self.assertEqual(mailbox.calls[1]["params"], {"page": 1, "size": 100})

    def test_conflict_rebind_uses_exact_query_after_create_race(self):
        mailbox = _FakeTempMailLocalMailbox([
            _Response(200, {"data": [], "total": 0}),
            _Response(409, {"error": "address already taken, try again"}),
            _Response(200, {"data": [
                {"id": "mailbox-4", "full_address": "user@example.com"},
            ], "total": 1}),
        ])

        account = mailbox.ensure_mailbox_by_email("user@example.com")

        self.assertEqual(account.account_id, "mailbox-4")
        self.assertEqual(account.extra["mailbox_action"], "reused_existing")
        self.assertEqual([call["method"] for call in mailbox.calls], ["GET", "POST", "GET"])
        self.assertEqual(mailbox.calls[2]["params"]["q"], "user@example.com")

    def test_find_mailbox_by_email_raises_auth_error_without_retrying_as_empty(self):
        mailbox = _FakeTempMailLocalMailbox([
            _Response(401, {"error": "invalid api_key"}, '{"error":"invalid api_key"}'),
        ])

        with self.assertRaises(TempMailReadyAuthError):
            mailbox.find_mailbox_by_email("user@example.com")

        self.assertEqual(len(mailbox.calls), 1)

    def test_wait_for_code_raises_auth_error_instead_of_polling_until_timeout(self):
        mailbox = _FakeTempMailLocalMailbox([
            _Response(401, {"error": "invalid api_key"}, '{"error":"invalid api_key"}'),
        ])

        with self.assertRaises(TempMailReadyAuthError):
            mailbox.wait_for_code(
                account=type("Account", (), {"email": "user@example.com", "account_id": "mailbox-1"})(),
                timeout=30,
            )

        self.assertEqual(len(mailbox.calls), 1)

    def test_wait_for_code_enforces_cutoff_and_excluded_codes(self):
        mailbox = _MailboxMessages(
            messages=[
                {"id": "old-message", "received_at": 100.0},
                {"id": "consumed-message", "received_at": 201.0},
                {"id": "fresh-message", "received_at": 202.0},
            ],
            details={
                "old-message": {"body_text": "Your verification code is 000000"},
                "consumed-message": {
                    "body_text": "Your verification code is 111111"
                },
                "fresh-message": {"body_text": "Your verification code is 222222"},
            },
        )
        account = type(
            "Account",
            (),
            {"email": "user@example.com", "account_id": "mailbox-1"},
        )()

        cursor = set()
        code = mailbox.wait_for_code(
            account=account,
            timeout=30,
            before_ids=cursor,
            otp_sent_at=200.0,
            exclude_codes={"111111"},
            phase="browser_register_email_otp",
        )

        self.assertEqual(code, "222222")
        self.assertEqual(
            mailbox._last_verification_result["message_id"],
            "fresh-message",
        )
        self.assertEqual(mailbox._last_verification_result["received_at"], 202.0)
        self.assertEqual(
            cursor,
            {"old-message", "consumed-message", "fresh-message"},
        )


if __name__ == "__main__":
    unittest.main()
