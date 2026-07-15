import json
import unittest
from unittest.mock import Mock, patch

from sqlmodel import SQLModel, Session, create_engine

import api.tasks as tasks_api
from core import db as core_db
from core.db import PaymentLinkGenerationModel


class PaymentLinkGenerationHistoryTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        self.core_engine_patch = patch.object(core_db, "engine", self.engine)
        self.tasks_engine_patch = patch.object(tasks_api, "engine", self.engine)
        self.core_engine_patch.start()
        self.tasks_engine_patch.start()
        SQLModel.metadata.create_all(self.engine)

    def tearDown(self):
        self.tasks_engine_patch.stop()
        self.core_engine_patch.stop()

    def test_history_is_paginated_and_contains_only_persisted_safe_result_fields(self):
        with Session(self.engine) as session:
            tasks_api._upsert_payment_link_generation(
                session,
                account_id=11,
                task_id="task-history-one",
                request_id="request-history-one",
                status="succeeded",
                profile_hash="profile-one",
                link_type="pix",
                remote_batch_id="batch-one",
                remote_job_id="job-one",
                generated_at="2026-07-16T01:00:00+00:00",
                url="https://pay.example.test/one",
                result={
                    "url": "https://pay.example.test/one",
                    "payment_source": "long_link",
                    "link_expires_at": 1_784_170_800,
                    "access_token": "token-must-not-persist",
                    "proxy": "socks5://secret@proxy.test:1080",
                },
            )
            tasks_api._upsert_payment_link_generation(
                session,
                account_id=11,
                task_id="task-history-two",
                request_id="request-history-two",
                status="interrupted",
                profile_hash="profile-two",
                link_type="paypal",
                remote_batch_id="batch-two",
                remote_job_id="job-two",
                error="remote process interrupted",
            )
            session.commit()

        first_page = tasks_api.list_chatgpt_payment_link_history(account_id=11, limit=1)
        second_page = tasks_api.list_chatgpt_payment_link_history(account_id=11, limit=1, offset=1)

        self.assertEqual(first_page["total"], 2)
        self.assertTrue(first_page["has_more"])
        self.assertEqual(first_page["items"][0]["request_id"], "request-history-two")
        self.assertEqual(second_page["items"][0]["request_id"], "request-history-one")
        serialized = json.dumps({"first": first_page, "second": second_page})
        self.assertNotIn("token-must-not-persist", serialized)
        self.assertNotIn("socks5://secret", serialized)
        self.assertEqual(second_page["items"][0]["result"]["payment_source"], "long_link")
        self.assertEqual(second_page["items"][0]["result"]["link_expires_at"], 1_784_170_800)

    def test_profile_view_is_redacted_before_it_reaches_the_browser(self):
        client = Mock()
        client.get_profile.return_value = {
            "profile_hash": "profile-redacted",
            "link_type": "pix",
            "country": "BR",
            "currency": "BRL",
            "effective_concurrency": 6,
            "profile": {
                "billing_country": "BR",
                "checkout_ui_mode": "custom",
                "payment_locale": "pt-BR",
                "client_fingerprint": "chrome",
                "proxy_chain_strategy": "matrix8",
                "proxy": "socks5://user:secret@proxy.test:1080",
                "proxy_configured": True,
                "regions": {"checkout": "BR", "promotion": "VN", "provider": "BR", "approve": "BR"},
                "pix": {"request_preset": "br_vn_br", "seed_pool_configured": True},
            },
        }

        with patch(
            "services.chatgpt_core.long_link_payment_client.LongLinkPaymentClient.from_env",
            return_value=client,
        ):
            profile = tasks_api.get_chatgpt_payment_link_profile()

        self.assertEqual(profile["link_type"], "pix")
        self.assertEqual(profile["country"], "BR")
        self.assertEqual(profile["currency"], "BRL")
        self.assertEqual(profile["effective_concurrency"], 6)
        self.assertTrue(profile["proxy_configured"])
        self.assertEqual(profile["regions"]["promotion"], "VN")
        serialized = json.dumps(profile)
        self.assertNotIn("socks5://", serialized)
        self.assertNotIn("secret", serialized)
        client.get_profile.assert_called_once_with(force_refresh=True)


if __name__ == "__main__":
    unittest.main()
