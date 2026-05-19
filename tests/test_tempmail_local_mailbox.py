import unittest

from core.base_mailbox import TempMailLocalMailbox


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


if __name__ == "__main__":
    unittest.main()
