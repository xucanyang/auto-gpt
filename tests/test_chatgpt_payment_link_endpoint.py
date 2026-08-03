import unittest
from unittest.mock import patch

from sqlmodel import SQLModel, Session, create_engine, select

import api.actions as actions_api
from core import db as core_db
from core.db import AccountModel, PaymentLinkGenerationModel


class _LongLinkClient:
    profile_hash = "profile-endpoint"
    batch_id = "batch_" + "e" * 32

    def __init__(self):
        self.submissions = []

    def get_profile(self, *, force_refresh=False):
        return {
            "profile_hash": self.profile_hash,
            "link_type": "pix",
            "country": "BR",
            "currency": "BRL",
            "profile": {},
        }

    def submit_batch(self, *, items, expected_profile_hash):
        self.submissions.append((items, expected_profile_hash))
        request_id = items[0]["request_id"]
        return {
            "batch_id": self.batch_id,
            "items": [
                {
                    "batch_id": self.batch_id,
                    "job_id": "job-endpoint",
                    "request_id": request_id,
                    "profile_hash": self.profile_hash,
                    "status": "done",
                    "completed_at": 1_720_000_000,
                    "result": {
                        "url": "https://pay.example.test/endpoint",
                        "link_type": "pix",
                        "billing_country": "BR",
                        "currency": "BRL",
                        "link_expires_at": 1_784_170_800,
                    },
                }
            ],
        }


class ChatGPTPaymentLinkEndpointTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        self.core_engine_patch = patch.object(core_db, "engine", self.engine)
        self.core_engine_patch.start()
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as session:
            account = AccountModel(
                platform="chatgpt",
                email="endpoint@example.com",
                password="pw",
                token="access-token-endpoint",
            )
            account.set_extra({"cookies": "__Secure-next-auth.session-token=web-session-endpoint"})
            session.add(account)
            session.commit()
            session.refresh(account)
            self.account_id = int(account.id or 0)

    def tearDown(self):
        self.core_engine_patch.stop()

    def test_compatibility_endpoint_restores_login_bound_short_link(self):
        request = actions_api.ActionRequest(
            params={
                "plan": "plus",
                "country": "ID",
                "currency": "IDR",
                "proxy": "http://local-proxy.example:8080",
                "payment_link_format": "short_chatgpt",
                "force_refresh": True,
            }
        )
        with Session(self.engine) as session, patch(
            "services.chatgpt_core.payment.generate_plus_short_link",
            return_value="https://chatgpt.com/checkout/openai_llc/cs_live_endpoint_short",
        ) as short_generator:
            response = actions_api.execute_action("chatgpt", self.account_id, "payment_link", request, session=session)
            account = session.get(AccountModel, self.account_id)
            generation = session.exec(select(PaymentLinkGenerationModel)).one()

        self.assertTrue(response["ok"])
        self.assertEqual(response["data"]["url"], "https://chatgpt.com/checkout/openai_llc/cs_live_endpoint_short")
        self.assertEqual(response["data"]["payment_source"], "chatgpt_hosted")
        self.assertEqual(response["data"]["payment_link_format"], "short_chatgpt")
        self.assertEqual(response["data"]["country"], "ID")
        self.assertEqual(response["data"]["currency"], "IDR")
        self.assertTrue(response["data"]["login_required"])
        self.assertEqual(account.get_extra()["chatgpt_last_payment_link"]["payment_source"], "chatgpt_hosted")
        self.assertEqual(account.get_extra()["chatgpt_last_payment_link"]["payment_link_format"], "short_chatgpt")
        self.assertEqual(generation.status, "succeeded")
        self.assertEqual(generation.get_result()["payment_link_format"], "short_chatgpt")
        short_generator.assert_called_once()


if __name__ == "__main__":
    unittest.main()
