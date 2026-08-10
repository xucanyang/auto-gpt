import json
import unittest
from unittest import mock

from core.db import AccountModel
from api.accounts import (
    AccountUpdate,
    _serialize_account,
    _serialize_account_compact_item,
    _serialize_account_list_item,
    get_account_secrets,
    update_account,
)


class AccountListCompactSerializationTests(unittest.TestCase):
    def _account(self, *, huge_size: int = 100_000) -> AccountModel:
        extra = {
            "access_token": "SECRET_EXTRA_AT",
            "accessToken": "SECRET_EXTRA_AT_ALIAS",
            "refresh_token": "SECRET_RT",
            "session_token": "SECRET_SESSION",
            "id_token": "SECRET_ID_TOKEN",
            "cookies": "SECRET_COOKIE_A=1; SECRET_COOKIE_B=2",
            "chatgpt_mailbox_state": "x" * huge_size,
            "chatgpt_workspace_variants": [
                {
                    "scope": "business",
                    "workspace_id": "legacy-workspace",
                    "account_id": "legacy-account",
                    "access_token": "SECRET_LEGACY_VARIANT_AT",
                    "refresh_token": "SECRET_LEGACY_VARIANT_RT",
                    "session_token": "SECRET_LEGACY_VARIANT_SESSION",
                    "cookies": "SECRET_LEGACY_VARIANT_COOKIE=1",
                    "cookie_header": "SECRET_LEGACY_VARIANT_COOKIE_HEADER=1",
                }
            ],
            "sync_statuses": {
                "sub2api": {
                    "remote_state": "exists",
                    "remote_account_id": "remote-1",
                    "last_upload": {
                        "status": "success",
                        "message": "ok",
                        "raw_request": "x" * 10_000,
                    },
                    "raw_response": "x" * 10_000,
                },
                "oaipay": {
                    "remote_state": "uploaded",
                    "uploaded": True,
                    "category_id": 2,
                    "category_name": "PLUS--已接美国长效",
                    "category_source": "auto",
                    "last_upload": {
                        "status": "success",
                        "message": "ok",
                        "category_id": 2,
                        "category_name": "PLUS--已接美国长效",
                        "category_source": "auto",
                        "raw_request": "x" * 10_000,
                    },
                },
            },
            "chatgpt_local": {
                "auth": {"state": "access_token_valid", "message": "ok"},
                "subscription": {
                    "plan": "plus",
                    "subscription_active_until": "2026-12-31T00:00:00Z",
                },
                "codex": {
                    "state": "ok",
                    "usage": {
                        "codex_5h_used_percent": 12.3,
                        "codex_7d_used_percent": 45.6,
                        "raw_usage": "x" * 10_000,
                    },
                },
            },
            "chatgpt_capabilities": {
                "auth_level": "refresh_token",
                "subscription_plan": "plus",
                "upload_gate": "ok",
            },
            "chatgpt_phone_binding": {
                "phone": "+10000000000",
                "api_url": "https://sms.example.test/order/1",
            },
            "chatgpt_last_payment_link": {
                "url": "https://payments.example.test/checkout/pix-1",
                "link_type": "pix",
                "link_status": "active",
                "payment_link_format": "long_link",
                "generated_at": "2026-07-16T00:00:00Z",
                "profile_hash": "SECRET_PAYMENT_PROFILE",
                "proxy": "https://SECRET_PAYMENT_PROXY.example.test",
                "remote_batch_id": "SECRET_REMOTE_BATCH",
            },
        }
        return AccountModel(
            id=1,
            platform="chatgpt",
            email="compact@example.com",
            password="SECRET_PASSWORD",
            token="SECRET_AT",
            status="registered",
            extra_json=json.dumps(extra, ensure_ascii=False),
        )

    def _assert_compact_payload(self, payload: dict):
        forbidden_top = {"extra_json", "token", "access_token", "refresh_token", "password", "session_token"}
        self.assertFalse(forbidden_top.intersection(payload.keys()))

        nested_extra = payload.get("extra") if isinstance(payload.get("extra"), dict) else {}
        forbidden_nested = {"access_token", "refresh_token", "session_token", "id_token", "cookies", "token", "password"}
        self.assertFalse(forbidden_nested.intersection(nested_extra.keys()))

        raw = json.dumps(payload, ensure_ascii=False, default=str)
        self.assertNotIn("SECRET_AT", raw)
        self.assertNotIn("SECRET_RT", raw)
        self.assertNotIn("SECRET_PASSWORD", raw)
        self.assertNotIn("SECRET_SESSION", raw)
        self.assertNotIn("SECRET_ID_TOKEN", raw)
        self.assertNotIn("SECRET_COOKIE", raw)
        self.assertNotIn("SECRET_LEGACY_VARIANT", raw)
        safe_extra = json.loads(payload.get("extra_json") or "{}")
        self.assertNotIn("access_token", safe_extra)
        self.assertNotIn("refresh_token", safe_extra)
        self.assertNotIn("session_token", safe_extra)
        self.assertNotIn("cookies", safe_extra)
        self.assertTrue(payload["has_access_token"])
        self.assertTrue(payload["credentials"]["has_cookies"])
        self.assertNotIn("SECRET_PAYMENT_PROFILE", raw)
        self.assertNotIn("SECRET_PAYMENT_PROXY", raw)
        self.assertNotIn("SECRET_REMOTE_BATCH", raw)
        self.assertNotIn("chatgpt_mailbox_state", raw)
        self.assertNotIn("raw_response", raw)
        self.assertNotIn("raw_usage", raw)

        self.assertTrue(payload["has_access_token"])
        self.assertTrue(payload["has_refresh_token"])
        self.assertTrue(payload["has_session_token"])
        self.assertTrue(payload["has_cookies"])
        self.assertTrue(payload["has_id_token"])
        self.assertTrue(payload["password_present"])
        self.assertEqual(
            payload["credentials"],
            {
                "has_access_token": True,
                "has_refresh_token": True,
                "has_session_token": True,
                "has_cookies": True,
                "has_id_token": True,
                "has_password": True,
            },
        )
        self.assertEqual(payload["auth_level"], "refresh_token")
        self.assertEqual(payload["subscription_plan"], "plus")
        self.assertEqual(payload["subscription_active_until"], "2026-12-31T00:00:00Z")
        self.assertEqual(payload["sub2api_remote_state"], "exists")
        self.assertEqual(payload["oaipaySync"]["category_id"], 2)
        self.assertEqual(payload["oaipaySync"]["category_name"], "PLUS--已接美国长效")
        self.assertEqual(payload["oaipaySync"]["last_upload"]["category_source"], "auto")
        self.assertEqual(payload["codex_state"], "ok")
        self.assertFalse(payload["chatgptCapabilities"]["has_confirmed_phone_binding"])
        self.assertEqual(payload["chatgptCapabilities"]["phone_binding_state"], "unconfirmed")
        self.assertEqual(payload["payment_link_platform"], "pix")
        self.assertEqual(
            payload["payment_link"],
            {
                "platform": "pix",
                "url": "https://payments.example.test/checkout/pix-1",
                "link_type": "pix",
                "link_status": "active",
                "payment_link_format": "long_link",
                "generated_at": "2026-07-16T00:00:00Z",
            },
        )

    def test_compact_serializer_does_not_return_raw_extra_or_secrets(self):
        payload = _serialize_account_compact_item(self._account())
        self._assert_compact_payload(payload)

    def test_legacy_list_serializer_is_also_compact_safe(self):
        payload = _serialize_account_list_item(self._account())
        self._assert_compact_payload(payload)

    def test_detail_serializer_redacts_raw_secrets_and_extra_json(self):
        payload = _serialize_account(self._account())
        raw = json.dumps(payload, ensure_ascii=False, default=str)

        self.assertEqual(payload.get("token"), "")
        self.assertEqual(payload.get("password"), "")
        self.assertTrue(payload.get("secrets_redacted"))
        self.assertNotIn("SECRET_AT", raw)
        self.assertNotIn("SECRET_RT", raw)
        self.assertNotIn("SECRET_PASSWORD", raw)
        self.assertNotIn("SECRET_SESSION", raw)
        self.assertNotIn("SECRET_ID_TOKEN", raw)
        self.assertNotIn("SECRET_COOKIE", raw)
        self.assertNotIn("SECRET_LEGACY_VARIANT", raw)

    def test_subscription_refresh_meta_exposes_refresh_failure_without_losing_history(self):
        account = AccountModel(
            id=9,
            platform="chatgpt",
            email="refresh-state@example.com",
            password="pw",
            token="at",
            status="registered",
            extra_json=json.dumps(
                {
                    "access_token": "at",
                    "chatgpt_local": {
                        "auth": {"state": "refresh_token_valid"},
                        "subscription": {"plan": "unknown"},
                    },
                    "chatgpt_capabilities": {
                        "auth_level": "refresh_token",
                        "subscription_plan": "unknown",
                        "last_known_subscription_plan": "free",
                    },
                    "chatgpt_local_refresh": {
                        "state": "failed",
                        "attempt_count": 3,
                        "max_attempts": 3,
                        "last_outcome": "unknown_plan",
                        "last_error": "订阅状态探测未完成",
                    },
                },
                ensure_ascii=False,
            ),
        )

        payload = _serialize_account_compact_item(account)

        self.assertEqual(payload["subscription_plan"], "unknown")
        self.assertEqual(payload["last_known_subscription_plan"], "free")
        self.assertEqual(payload["subscription_refresh_state"], "refresh_failed")
        self.assertEqual(payload["chatgptLocal"]["subscription"]["refresh_state"], "refresh_failed")
        self.assertEqual(payload["chatgptLocal"]["subscription"]["refresh_attempt_count"], 3)
        self.assertEqual(payload["chatgptLocal"]["subscription"]["refresh_last_error"], "订阅状态探测未完成")

    def test_token_patch_updates_both_token_fields_and_resets_old_probe(self):
        account = AccountModel(
            id=11,
            platform="chatgpt",
            email="token-patch@example.com",
            password="pw",
            token="at-old",
            status="subscribed",
            extra_json=json.dumps(
                {
                    "access_token": "at-old",
                    "chatgpt_local": {
                        "auth": {"state": "access_token_valid"},
                        "subscription": {"plan": "plus"},
                    },
                }
            ),
        )

        class DummySession:
            def get(self, model, account_id):
                return account if model is AccountModel and account_id == 11 else None

            def add(self, _row):
                return None

            def commit(self):
                return None

            def refresh(self, _row):
                return None

        with mock.patch("api.accounts.upsert_account_list_state_for_account_ids"), mock.patch(
            "api.accounts.schedule_chatgpt_local_status_refresh_for_account_id"
        ) as schedule_refresh:
            result = update_account(
                11,
                AccountUpdate(token="at-new"),
                session=DummySession(),
            )

        extra = result.get_extra()
        self.assertEqual(result.token, "at-new")
        self.assertEqual(extra["access_token"], "at-new")
        self.assertNotIn("chatgpt_local", extra)
        self.assertEqual(extra["chatgpt_last_confirmed_subscription"]["plan"], "plus")
        schedule_refresh.assert_called_once_with(11, reason="account_update_token")

    def test_token_patch_keeps_non_chatgpt_extra_contract_unchanged(self):
        account = AccountModel(
            id=12,
            platform="openai",
            email="generic-token@example.com",
            password="pw",
            token="token-old",
            status="active",
            extra_json=json.dumps({"provider_metadata": {"source": "legacy"}}),
        )

        class DummySession:
            def get(self, model, account_id):
                return account if model is AccountModel and account_id == 12 else None

            def add(self, _row):
                return None

            def commit(self):
                return None

            def refresh(self, _row):
                return None

        with mock.patch("api.accounts.upsert_account_list_state_for_account_ids"), mock.patch(
            "api.accounts.schedule_chatgpt_local_status_refresh_for_account_id"
        ) as schedule_refresh:
            result = update_account(
                12,
                AccountUpdate(token="token-new"),
                session=DummySession(),
            )

        self.assertEqual(result.token, "token-new")
        self.assertEqual(
            result.get_extra(),
            {"provider_metadata": {"source": "legacy"}},
        )
        schedule_refresh.assert_not_called()

    def test_compact_payload_size_does_not_scale_with_huge_extra_blob(self):
        payload = {
            "items": [_serialize_account_compact_item(self._account(huge_size=100_000)) for _ in range(20)],
            "total": 20,
            "page": 1,
        }
        raw = json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":")).encode()
        self.assertLess(len(raw), 500_000)

    def test_account_secrets_endpoint_returns_web_session_materials(self):
        account = self._account()

        class DummySession:
            def get(self, model, account_id):
                return account if model is AccountModel and account_id == 1 else None

        payload = get_account_secrets(
            1,
            fields="session_token,cookies,id_token,cookie_header",
            session=DummySession(),
        )
        self.assertEqual(payload["secrets"]["session_token"], "SECRET_SESSION")
        self.assertEqual(payload["secrets"]["cookies"], "SECRET_COOKIE_A=1; SECRET_COOKIE_B=2")
        self.assertEqual(payload["secrets"]["id_token"], "SECRET_ID_TOKEN")
        self.assertEqual(payload["secrets"]["cookie_header"], "SECRET_COOKIE_A=1; SECRET_COOKIE_B=2")
        self.assertTrue(payload["present"]["cookies"])
        self.assertEqual(payload["lengths"]["cookies"], len("SECRET_COOKIE_A=1; SECRET_COOKIE_B=2"))


if __name__ == "__main__":
    unittest.main()
