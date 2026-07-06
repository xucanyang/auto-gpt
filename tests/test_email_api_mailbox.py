import unittest
from unittest.mock import patch

from core.base_mailbox import (
    EmailApiMailbox,
    MailboxAccount,
    build_gmail_dot_variant,
    build_gmail_variants,
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

    def test_parse_gmail_line_expands_original_and_random_default_variant(self):
        candidates, errors = parse_email_api_lines(
            "name@gmail.com----smsbower.page/api/mail/getCodeBySignature?s=abc",
            gmail_variant_random_seed="unit",
        )

        self.assertEqual(errors, [])
        self.assertEqual(len(candidates), 2)
        self.assertEqual(candidates[0]["email"], "name@gmail.com")
        self.assertNotEqual(candidates[1]["email"], "name@gmail.com")
        self.assertRegex(candidates[1]["email"], r"@(gmail|googlemail)\.com$")
        self.assertEqual(candidates[0]["api_url"], candidates[1]["api_url"])
        self.assertIn(
            candidates[1]["variant"],
            {"gmail_dot", "gmail_plus", "gmail_dot_plus", "googlemail", "googlemail_dot", "googlemail_plus", "googlemail_dot_plus"},
        )

    def test_parse_gmail_line_honors_total_identity_count_and_freezes_lock_root(self):
        candidates, errors = parse_email_api_lines(
            "abcdef@gmail.com----smsbower.page/api/mail/getCodeBySignature?s=abc",
            gmail_variant_count=5,
            gmail_variant_rules="all",
            gmail_variant_random_seed="unit-count",
        )

        self.assertEqual(errors, [])
        self.assertEqual(len(candidates), 5)
        self.assertEqual(candidates[0]["email"], "abcdef@gmail.com")
        self.assertEqual({item["gmail_root"] for item in candidates}, {"abcdef@gmail.com"})
        self.assertEqual(len({item["email"] for item in candidates}), 5)
        for item in candidates:
            self.assertIn("gmail:abcdef@gmail.com", item["lock_keys"])

    def test_parse_gmail_line_can_disable_variants(self):
        candidates, errors = parse_email_api_lines(
            "abcdef@gmail.com----api.example.com/code",
            gmail_dot_variant_enabled=False,
            gmail_variant_count=10,
            gmail_variant_random_seed="unit-disabled",
        )

        self.assertEqual(errors, [])
        self.assertEqual([item["email"] for item in candidates], ["abcdef@gmail.com"])

    def test_build_gmail_variants_supports_dot_plus_and_googlemail_rules(self):
        variants = build_gmail_variants(
            "abcdef@gmail.com",
            count=8,
            rules="all",
            random_seed="unit-rules",
        )

        emails = [item["email"] for item in variants]
        self.assertEqual(emails[0], "abcdef@gmail.com")
        self.assertEqual(len(emails), 8)
        self.assertEqual(len(set(emails)), 8)
        self.assertTrue(any("+r" in email for email in emails))
        self.assertTrue(any(email.endswith("@googlemail.com") for email in emails))

    def test_googlemail_and_gmail_same_root_are_deduped(self):
        candidates, errors = parse_email_api_lines(
            "\n".join(
                [
                    "abcdef@gmail.com----api.example.com/code",
                    "abc.def@googlemail.com----https://api.example.com/code",
                ]
            ),
            gmail_variant_count=3,
            gmail_variant_random_seed="unit-root",
        )

        self.assertEqual(errors, [])
        self.assertEqual(len(candidates), 3)
        self.assertEqual({item["gmail_root"] for item in candidates}, {"abcdef@gmail.com"})

    def test_prepare_register_request_freezes_generated_email_api_candidates(self):
        from api.tasks import RegisterTaskRequest, _prepare_register_request

        prepared = _prepare_register_request(
            RegisterTaskRequest(
                platform="chatgpt",
                count=1,
                concurrency=1,
                extra={
                    "mail_provider": "email_api",
                    "email_api_lines": "abcdef@gmail.com----api.example.com/code",
                    "email_api_gmail_variant_count": 5,
                    "email_api_gmail_variant_rules": "all",
                    "email_api_gmail_variant_random_seed": "unit-freeze",
                },
            )
        )

        frozen = prepared.extra.get("email_api_candidates")
        self.assertIsInstance(frozen, list)
        self.assertEqual(len(frozen), 5)
        self.assertEqual(prepared.count, 5)
        self.assertEqual(prepared.extra.get("email_api_candidate_count"), 5)
        self.assertEqual(prepared.extra.get("email_api_gmail_variant_random_seed"), "unit-freeze")

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
