import unittest
from unittest.mock import patch

from core.base_mailbox import (
    EmailApiMailbox,
    MailboxAccount,
    build_gmail_dot_variant,
    normalize_email_api_url,
    parse_email_api_lines,
)


class EmailApiMailboxTests(unittest.TestCase):
    def test_normalize_email_api_url_adds_https_and_rejects_non_http(self):
        self.assertEqual(
            normalize_email_api_url("smsbower.page/api/mail/getCodeBySignature?s=abc"),
            "https://smsbower.page/api/mail/getCodeBySignature?s=abc",
        )
        self.assertEqual(
            normalize_email_api_url("http://example.com/code?id=1"),
            "http://example.com/code?id=1",
        )
        with self.assertRaises(ValueError):
            normalize_email_api_url("ftp://example.com/code")

    def test_status_code_parser_treats_zero_and_non_codes_as_pending(self):
        self.assertEqual(EmailApiMailbox.code_from_status(0), "")
        self.assertEqual(EmailApiMailbox.code_from_status(1), "")
        self.assertEqual(EmailApiMailbox.code_from_status("0"), "")
        self.assertEqual(EmailApiMailbox.code_from_status(""), "")
        self.assertEqual(EmailApiMailbox.code_from_status(None), "")
        self.assertEqual(EmailApiMailbox.code_from_status("abc123"), "")
        self.assertEqual(EmailApiMailbox.code_from_status("123"), "")
        self.assertEqual(EmailApiMailbox.code_from_status("123456789"), "")
        self.assertEqual(EmailApiMailbox.code_from_status("1234"), "1234")
        self.assertEqual(EmailApiMailbox.code_from_status("123456"), "123456")
        self.assertEqual(EmailApiMailbox.code_from_status(123456), "123456")

    def test_codes_from_payload_supports_smbower_code_and_all_codes_shape(self):
        self.assertEqual(
            EmailApiMailbox.codes_from_payload({"status": 1, "code": "123456", "all_codes": []}),
            ["123456"],
        )
        self.assertEqual(
            EmailApiMailbox.codes_from_payload({"status": 1, "code": None, "all_codes": ["111111", "222222"]}),
            ["222222", "111111"],
        )
        self.assertEqual(
            EmailApiMailbox.codes_from_payload({"status": "333333", "code": "333333", "all_codes": ["222222"]}),
            ["333333", "222222"],
        )

    def test_parse_gmail_line_expands_original_and_one_dot_variant(self):
        candidates, errors = parse_email_api_lines(
            "name@gmail.com----smsbower.page/api/mail/getCodeBySignature?s=abc"
        )

        self.assertEqual(errors, [])
        self.assertEqual(len(candidates), 2)
        self.assertEqual(candidates[0]["email"], "name@gmail.com")
        self.assertNotEqual(candidates[1]["email"], "name@gmail.com")
        self.assertTrue(candidates[1]["email"].endswith("@gmail.com"))
        self.assertEqual(candidates[0]["api_url"], candidates[1]["api_url"])
        self.assertEqual(candidates[1]["variant"], "gmail_dot")

    def test_parse_supports_typo_gamil_dot_com_and_long_delimiter(self):
        candidates, errors = parse_email_api_lines(
            "xx.xxxxx.gamil.com------smsbower.page/api/mail/getCodeBySignature?s=abc"
        )

        self.assertEqual(errors, [])
        self.assertGreaterEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["email"], "xx.xxxxx@gmail.com")
        self.assertTrue(candidates[0]["warnings"])

    def test_non_gmail_line_does_not_expand(self):
        candidates, errors = parse_email_api_lines("user@example.com----api.example.com/code")
        self.assertEqual(errors, [])
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["email"], "user@example.com")

    def test_build_gmail_dot_variant_does_not_return_original(self):
        self.assertNotEqual(build_gmail_dot_variant("name@gmail.com"), "name@gmail.com")
        self.assertEqual(build_gmail_dot_variant("user@example.com"), "")

    @patch("time.sleep", return_value=None)
    @patch("requests.request")
    def test_wait_for_code_skips_baseline_status_code(self, mock_request, _sleep):
        mock_request.side_effect = [
            _response({"status": "123456"}),
            _response({"status": "123456"}),
            _response({"status": "654321"}),
        ]
        mailbox = EmailApiMailbox(
            email="name@gmail.com",
            api_url="https://api.example.com/code?id=abc",
            poll_interval_seconds=0.5,
        )
        account = MailboxAccount(
            email="name@gmail.com",
            account_id="name@gmail.com",
            extra={"api_url": "https://api.example.com/code?id=abc"},
        )

        before_ids = mailbox.get_current_ids(account)
        code = mailbox.wait_for_code(account, timeout=3, before_ids=before_ids)

        self.assertEqual(before_ids, {"status:123456"})
        self.assertEqual(code, "654321")
        self.assertEqual(mailbox._last_verification_result["message_id"], "status:654321")

    @patch("time.sleep", return_value=None)
    @patch("requests.request")
    def test_wait_for_code_reads_smbower_code_field_and_skips_all_codes_baseline(self, mock_request, _sleep):
        mock_request.side_effect = [
            _response({"status": 1, "code": None, "all_codes": ["111111"]}),
            _response({"status": 1, "code": None, "all_codes": ["111111"]}),
            _response({"status": 1, "code": "222222", "all_codes": ["111111", "222222"]}),
        ]
        mailbox = EmailApiMailbox(
            email="name@gmail.com",
            api_url="https://api.example.com/code?id=abc",
            poll_interval_seconds=0.5,
        )
        account = MailboxAccount(
            email="name@gmail.com",
            account_id="name@gmail.com",
            extra={"api_url": "https://api.example.com/code?id=abc"},
        )

        before_ids = mailbox.get_current_ids(account)
        code = mailbox.wait_for_code(account, timeout=3, before_ids=before_ids)

        self.assertEqual(before_ids, {"status:111111"})
        self.assertEqual(code, "222222")
        self.assertEqual(mailbox._last_verification_result["message_id"], "status:222222")


def _response(payload, status_code=200):
    response = unittest.mock.Mock()
    response.status_code = status_code
    response.json.return_value = payload
    response.text = str(payload)
    return response


if __name__ == "__main__":
    unittest.main()
