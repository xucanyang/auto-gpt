import unittest
from unittest.mock import patch

from sqlmodel import SQLModel, Session, create_engine, select

import api.chatgpt as chatgpt_api
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
            session.add(account)
            session.commit()
            session.refresh(account)
            self.account_id = int(account.id or 0)

    def tearDown(self):
        self.core_engine_patch.stop()

    def test_compatibility_endpoint_ignores_retired_local_controls_and_uses_long_link(self):
        client = _LongLinkClient()
        request = chatgpt_api.PaymentReq.model_validate(
            {
                "plan": "plus",
                "country": "ID",
                "currency": "IDR",
                "proxy": "http://local-proxy.example:8080",
                "payment_link_format": "short_chatgpt",
                "force_refresh": True,
            }
        )
        with Session(self.engine) as session, patch(
            "services.chatgpt_core.long_link_payment_client.LongLinkPaymentClient.from_env",
            return_value=client,
        ), patch("services.chatgpt_core.payment.generate_plus_link") as local_generator:
            response = chatgpt_api.generate_payment_link(self.account_id, request, session)
            account = session.get(AccountModel, self.account_id)
            generation = session.exec(select(PaymentLinkGenerationModel)).one()

        self.assertEqual(response["url"], "https://pay.example.test/endpoint")
        self.assertEqual(response["payment_source"], "long_link")
        self.assertEqual(response["payment_link_format"], "long_link")
        self.assertEqual(response["country"], "BR")
        self.assertEqual(response["currency"], "BRL")
        self.assertEqual(len(client.submissions), 1)
        self.assertFalse(client.submissions[0][0][0]["access_token"] == "")
        self.assertEqual(client.submissions[0][1], client.profile_hash)
        self.assertFalse(client.submissions[0][0][0].get("country"))
        self.assertEqual(account.get_extra()["chatgpt_last_payment_link"]["payment_source"], "long_link")
        self.assertEqual(account.get_extra()["chatgpt_last_payment_link"]["link_expires_at"], 1_784_170_800)
        self.assertEqual(generation.status, "succeeded")
        self.assertEqual(generation.get_result()["link_expires_at"], 1_784_170_800)
        local_generator.assert_not_called()


if __name__ == "__main__":
    unittest.main()
