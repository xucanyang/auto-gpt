import unittest

from fastapi import HTTPException

from api.chatgpt import (
    CHATGPT_EXPORT_MODE_PIX_PAYMENT_LINKS,
    _build_chatgpt_export_content,
)
from core.db import AccountModel


def make_account(account_id: int, *, token: str = "", extra: dict | None = None, cashier_url: str = "") -> AccountModel:
    account = AccountModel(
        id=account_id,
        platform="chatgpt",
        email=f"pix-export-{account_id}@example.test",
        password="not-exported",
        token=token,
        cashier_url=cashier_url,
    )
    account.set_extra(extra or {})
    return account


class ChatGPTPixPaymentLinkExportTests(unittest.TestCase):
    def test_pix_export_contains_only_valid_saved_pix_urls(self):
        body, media_type, filename_prefix, extension = _build_chatgpt_export_content(
            accounts=[
                make_account(
                    1,
                    token="must-not-be-exported",
                    extra={
                        "chatgpt_last_payment_link": {
                            "url": "https://payments.example.test/pix-one",
                            "link_type": "pix",
                        }
                    },
                ),
                make_account(
                    2,
                    extra={
                        "chatgpt_last_payment_link": {
                            "url": "https://payments.example.test/pix-two",
                            "payment_method_type": "pix",
                        }
                    },
                ),
                make_account(
                    3,
                    extra={
                        "chatgpt_last_payment_link": {
                            "url": "https://payments.example.test/paypal",
                            "link_type": "paypal",
                        }
                    },
                ),
                make_account(
                    4,
                    extra={
                        "chatgpt_last_payment_link": {
                            "url": "javascript:alert(1)",
                            "link_type": "pix",
                        }
                    },
                ),
                make_account(
                    5,
                    extra={
                        "chatgpt_last_payment_link": {
                            "url": "http://payments.example.test/legacy-http-pix",
                            "link_type": "pix",
                        }
                    },
                ),
                make_account(6, cashier_url="https://payments.example.test/legacy-pix"),
            ],
            export_mode=CHATGPT_EXPORT_MODE_PIX_PAYMENT_LINKS,
        )

        self.assertEqual(
            body,
            "https://payments.example.test/pix-one\nhttps://payments.example.test/pix-two\n",
        )
        self.assertEqual(media_type, "text/plain; charset=utf-8")
        self.assertEqual(filename_prefix, "chatgpt-pix-payment-links")
        self.assertEqual(extension, "txt")
        self.assertNotIn("must-not-be-exported", body)
        self.assertNotIn("paypal", body)
        self.assertNotIn("legacy-http-pix", body)
        self.assertNotIn("legacy-pix", body)

    def test_pix_export_rejects_an_empty_or_non_pix_scope(self):
        with self.assertRaises(HTTPException) as raised:
            _build_chatgpt_export_content(
                accounts=[
                    make_account(
                        1,
                        extra={
                            "chatgpt_last_payment_link": {
                                "url": "https://payments.example.test/paypal",
                                "link_type": "paypal",
                            }
                        },
                    )
                ],
                export_mode=CHATGPT_EXPORT_MODE_PIX_PAYMENT_LINKS,
            )

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("没有可导出的 PIX 支付链接", str(raised.exception.detail))
