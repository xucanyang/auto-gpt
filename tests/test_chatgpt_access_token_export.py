import unittest

from api.chatgpt import (
    CHATGPT_EXPORT_MODE_ACCESS_TOKEN,
    _build_chatgpt_export_content,
    _normalize_chatgpt_export_mode,
)
from core.db import AccountModel
from fastapi import HTTPException


def make_account(*, token: str = "", extra_json: str = "{}") -> AccountModel:
    return AccountModel(
        platform="chatgpt",
        email="export@example.com",
        password="not-used",
        token=token,
        extra_json=extra_json,
    )


class ChatGPTAccessTokenExportTests(unittest.TestCase):
    def test_access_token_export_is_plain_text_one_non_empty_token_per_line(self):
        body, media_type, filename_prefix, extension = _build_chatgpt_export_content(
            accounts=[
                make_account(token="primary-at"),
                make_account(extra_json='{"accessToken":"legacy-camel-at"}'),
                make_account(extra_json='{"webAccessToken":"legacy-web-at"}'),
                make_account(),
            ],
            export_mode=CHATGPT_EXPORT_MODE_ACCESS_TOKEN,
        )

        self.assertEqual(body, "primary-at\nlegacy-camel-at\nlegacy-web-at\n")
        self.assertEqual(media_type, "text/plain; charset=utf-8")
        self.assertEqual(filename_prefix, "chatgpt-access-token")
        self.assertEqual(extension, "txt")

    def test_access_token_export_rejects_empty_result_instead_of_downloading_blank_lines(self):
        with self.assertRaises(HTTPException) as raised:
            _build_chatgpt_export_content(
                accounts=[make_account(), make_account(extra_json='{"access_token":"  "}')],
                export_mode=CHATGPT_EXPORT_MODE_ACCESS_TOKEN,
            )

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("没有可导出的 AccessToken", str(raised.exception.detail))

    def test_unknown_export_mode_is_rejected(self):
        with self.assertRaises(HTTPException) as raised:
            _normalize_chatgpt_export_mode("csv")

        self.assertEqual(raised.exception.status_code, 400)
