import unittest

from fastapi import HTTPException

from api.chatgpt import (
    CHATGPT_EXPORT_MODE_PAYMENT_LINKS,
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


class ChatGPTPaymentLinkExportTests(unittest.TestCase):
    def test_generic_export_contains_each_current_payment_link_regardless_of_platform(self):
        shared_url = "https://payments.example.test/shared-current-link"
        body, media_type, filename_prefix, extension = _build_chatgpt_export_content(
            accounts=[
                make_account(
                    8,
                    extra={
                        "chatgpt_last_payment_link": {
                            "url": shared_url,
                            "link_type": "hosted",
                        }
                    },
                ),
                make_account(
                    1,
                    token="must-not-be-exported",
                    extra={
                        "chatgpt_last_payment_link": {
                            "url": "https://payments.example.test/pix-current",
                            "link_type": "pix",
                        }
                    },
                ),
                make_account(
                    3,
                    extra={
                        "chatgpt_last_payment_link": {
                            "url": "https://payments.example.test/hosted-current",
                            "link_type": "hosted",
                            "link_status": "paid",
                        },
                        "chatgpt_payment_link_variants": {
                            "old": {
                                "url": "https://payments.example.test/non-current-variant",
                                "link_type": "paypal",
                            }
                        },
                    },
                ),
                make_account(
                    2,
                    extra={
                        "chatgpt_last_payment_link": {
                            "paypal_url": "https://www.paypal.com/agreements/approve?ba_token=BA-CURRENT",
                            "payment_link_format": "paypal_url",
                        }
                    },
                ),
                make_account(
                    4,
                    extra={
                        "chatgpt_paypal_url": {
                            "paypal_url": "https://www.paypal.com/agreements/approve?ba_token=BA-LEGACY",
                        }
                    },
                ),
                make_account(
                    5,
                    extra={
                        "chatgpt_last_payment_link": {
                            "url": "http://payments.example.test/current-http-link",
                            "link_type": "other",
                        }
                    },
                ),
                make_account(
                    6,
                    extra={
                        "chatgpt_last_payment_link": {
                            "url": shared_url,
                            "link_type": "upi",
                        }
                    },
                ),
                make_account(
                    9,
                    extra={
                        "chatgpt_last_payment_link": {
                            "url": "javascript:alert(1)",
                            "link_type": "pix",
                        }
                    },
                ),
                make_account(10, cashier_url="https://payments.example.test/legacy-cashier"),
                make_account(
                    11,
                    extra={
                        "chatgpt_last_payment_link": {
                            "link_status": "payment_link_deleted",
                            "cleaned_at": "2026-08-18T00:00:00Z",
                        },
                        "chatgpt_paypal_url": {
                            "paypal_url": "https://payments.example.test/cleaned-legacy-paypal",
                        },
                    },
                ),
                make_account(
                    12,
                    extra={
                        "chatgpt_payment_link_variants": {
                            "old": {
                                "url": "https://payments.example.test/variant-without-current-link",
                                "link_type": "ideal",
                            }
                        }
                    },
                ),
            ],
            export_mode=CHATGPT_EXPORT_MODE_PAYMENT_LINKS,
        )

        self.assertEqual(
            body,
            "\n".join(
                (
                    "https://payments.example.test/pix-current",
                    "https://www.paypal.com/agreements/approve?ba_token=BA-CURRENT",
                    "https://payments.example.test/hosted-current",
                    "https://www.paypal.com/agreements/approve?ba_token=BA-LEGACY",
                    "http://payments.example.test/current-http-link",
                    shared_url,
                    shared_url,
                    "",
                )
            ),
        )
        self.assertEqual(media_type, "text/plain; charset=utf-8")
        self.assertEqual(filename_prefix, "chatgpt-payment-links")
        self.assertEqual(extension, "txt")
        self.assertNotIn("must-not-be-exported", body)
        self.assertNotIn("non-current-variant", body)
        self.assertNotIn("legacy-cashier", body)
        self.assertNotIn("cleaned-legacy-paypal", body)
        self.assertNotIn("variant-without-current-link", body)

    def test_generic_export_rejects_a_scope_without_current_payment_links(self):
        with self.assertRaises(HTTPException) as raised:
            _build_chatgpt_export_content(
                accounts=[
                    make_account(1, cashier_url="https://payments.example.test/legacy-cashier"),
                    make_account(
                        2,
                        extra={
                            "chatgpt_last_payment_link": {
                                "url": "javascript:alert(1)",
                                "link_type": "pix",
                            }
                        },
                    ),
                ],
                export_mode=CHATGPT_EXPORT_MODE_PAYMENT_LINKS,
            )

        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(raised.exception.detail, "当前操作范围没有可导出的支付链接")
