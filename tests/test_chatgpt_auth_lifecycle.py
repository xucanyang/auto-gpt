import base64
import json
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from sqlmodel import Session, SQLModel, create_engine, select

from core.db import (
    AccountModel,
    ChatGPTAuthLifecycleModel,
    ChatGPTAuthProbeEventModel,
    ChatGPTSubscriptionStateModel,
)
from services.chatgpt_core.auth_lifecycle import (
    apply_probe_lifecycle,
    build_account_lifecycle_projection,
    classify_access_probe,
    classify_refresh_attempt,
    backfill_existing_lifecycle_rows,
    token_timing,
)
from services.chatgpt_core.status_probe import ProbeHTTPResult, probe_local_chatgpt_status


def _jwt(*, exp: int, iat: int | None = None) -> str:
    payload = {"exp": exp}
    if iat is not None:
        payload["iat"] = iat
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


class ChatGPTAuthLifecycleTests(unittest.TestCase):
    def test_token_timing_uses_signed_jwt_exp(self):
        now = int(time.time())
        timing = token_timing(_jwt(exp=now + 864000, iat=now))
        self.assertEqual(timing["expiry_source"], "jwt_exp")
        self.assertEqual(timing["expiry_confidence"], "exact")
        self.assertTrue(timing["expires_at"].endswith("Z"))

    def test_error_classification_does_not_call_token_expiry_account_ban(self):
        self.assertEqual(classify_access_probe(401, "token_expired", "expired"), ("expired", "unknown"))
        self.assertEqual(classify_access_probe(403, "", "forbidden"), ("rejected", "banned_suspected"))
        self.assertEqual(classify_access_probe(403, "account_deactivated", "deleted"), ("rejected", "deactivated_confirmed"))
        self.assertEqual(
            classify_refresh_attempt(
                {"attempted": True, "success": False, "http_status": 401, "error_code": "invalid_grant"}
            ),
            ("rejected", "rejected"),
        )

    def test_probe_persists_refresh_failure_and_access_token_expiry_separately(self):
        now = int(time.time())
        access_token = _jwt(exp=now - 60, iat=now - 864000)
        account = AccountModel(
            platform="chatgpt",
            email="lifecycle@example.com",
            password="pw",
            token=access_token,
            extra_json=json.dumps({"access_token": access_token, "refresh_token": "rt-old"}),
        )
        engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            session.add(account)
            session.commit()
            session.refresh(account)
            extra = account.get_extra()
            projection = apply_probe_lifecycle(
                session,
                account,
                {
                    "checked_at": "2026-08-17T00:00:00Z",
                    "auth": {
                        "state": "access_token_invalidated",
                        "http_status": 401,
                        "error_code": "token_expired",
                        "message": "expired",
                    },
                    "refresh_attempt": {
                        "attempted": True,
                        "success": False,
                        "http_status": 401,
                        "error_code": "invalid_grant",
                        "message": "refresh rejected",
                    },
                    "access_token_probe": {
                        "source": "access_token_fallback",
                        "http_status": 401,
                        "error_code": "token_expired",
                        "message": "expired",
                    },
                    "subscription": {"plan": "unknown"},
                },
                extra=extra,
            )
            session.commit()
            lifecycle = session.get(ChatGPTAuthLifecycleModel, account.id)
            subscription = session.get(ChatGPTSubscriptionStateModel, account.id)
            events = session.exec(select(ChatGPTAuthProbeEventModel)).all()

        self.assertEqual(projection["access_token"]["state"], "expired")
        self.assertEqual(projection["refresh_token"]["state"], "rejected")
        self.assertEqual(projection["derived"]["state"], "refresh_failed_at_unusable")
        self.assertEqual(projection["account_evidence"]["state"], "unknown")
        self.assertEqual(lifecycle.access_token_state, "expired")
        self.assertEqual(lifecycle.refresh_token_last_error_code, "invalid_grant")
        self.assertEqual(subscription.current_state, "unconfirmable_auth")
        self.assertEqual(len(events), 1)

    def test_active_probe_replaces_historical_ban_suspicion(self):
        account = AccountModel(
            platform="chatgpt",
            email="evidence-recovery@example.com",
            password="pw",
            token="at",
            extra_json=json.dumps({"access_token": "at"}),
        )
        engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            session.add(account)
            session.commit()
            session.refresh(account)
            extra = account.get_extra()
            apply_probe_lifecycle(
                session,
                account,
                {
                    "checked_at": "2026-08-17T00:00:00Z",
                    "auth": {"http_status": 403, "error_code": "", "message": "forbidden"},
                    "access_token_probe": {
                        "attempted": True,
                        "source": "access_token",
                        "http_status": 403,
                        "error_code": "",
                        "message": "forbidden",
                    },
                },
                extra=extra,
            )
            session.commit()
            projection = apply_probe_lifecycle(
                session,
                account,
                {
                    "checked_at": "2026-08-17T01:00:00Z",
                    "auth": {"http_status": 200, "message": "ok"},
                    "access_token_probe": {
                        "attempted": True,
                        "source": "access_token",
                        "http_status": 200,
                        "error_code": "",
                        "message": "ok",
                    },
                    "subscription": {"plan": "free"},
                },
                extra=extra,
            )

        self.assertEqual(projection["account_evidence"]["state"], "active_confirmed")
        self.assertEqual(projection["access_token"]["state"], "valid")

    def test_status_probe_keeps_refresh_failure_when_falling_back_to_expired_at(self):
        account = SimpleNamespace(
            email="probe@example.com",
            token="cached-at",
            access_token="cached-at",
            refresh_token="rt-token",
            session_token="",
            user_id="acct-123",
            extra={"access_token": "cached-at", "refresh_token": "rt-token"},
        )
        with mock.patch(
            "services.chatgpt_core.status_probe.TokenRefreshManager.refresh_by_oauth_token",
            return_value=SimpleNamespace(
                success=False,
                access_token="",
                refresh_token="",
                error_message="OAuth token 刷新失败: HTTP 401",
                http_status=401,
                error_code="invalid_grant",
            ),
        ), mock.patch(
            "services.chatgpt_core.status_probe._resolve_effective_probe_proxy",
            return_value=("", "direct"),
        ), mock.patch(
            "services.chatgpt_core.status_probe._probe_backend_me",
            return_value=ProbeHTTPResult(
                status_code=401,
                headers={},
                body_text='{"error":{"code":"token_expired","message":"expired"}}',
                body_json={"error": {"code": "token_expired", "message": "expired"}},
                error_code="token_expired",
                message="expired",
            ),
        ):
            result = probe_local_chatgpt_status(account)

        self.assertEqual(result["refresh_attempt"]["error_code"], "invalid_grant")
        self.assertEqual(result["access_token_probe"]["state"], "expired")
        self.assertEqual(result["auth"]["reason"], "access_token_expired")
        self.assertEqual(result["auth"]["resolution_source"], "access_token_fallback")

    def test_at_only_projection_exposes_estimated_ten_day_expiry(self):
        account = AccountModel(
            platform="chatgpt",
            email="unknown-expiry@example.com",
            password="pw",
            token="opaque-access-token",
            extra_json=json.dumps({"access_token": "opaque-access-token"}),
        )
        projection = build_account_lifecycle_projection(account, account.get_extra())
        self.assertEqual(projection["access_token"]["expiry_source"], "at_only_10d_policy")
        self.assertEqual(projection["access_token"]["expiry_confidence"], "estimated")
        self.assertTrue(projection["access_token"]["expires_at"])
        self.assertEqual(projection["derived"]["state"], "unknown")

    def test_backfill_copies_legacy_subscription_into_dedicated_state(self):
        account = AccountModel(
            platform="chatgpt",
            email="legacy-subscription@example.com",
            password="pw",
            token="opaque-access-token",
            extra_json=json.dumps(
                {
                    "access_token": "opaque-access-token",
                    "chatgpt_local": {
                        "subscription": {
                            "plan": "Plus",
                            "subscription_active_until": "2026-09-01T00:00:00Z",
                            "checked_at": "2026-08-16T00:00:00Z",
                            "workspace_plan_type": "personal",
                            "source": "legacy_probe",
                        }
                    },
                }
            ),
        )
        engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            session.add(account)
            session.commit()
            account_id = account.id

        backfill_existing_lifecycle_rows(engine)

        with Session(engine) as session:
            subscription = session.get(ChatGPTSubscriptionStateModel, account_id)

        self.assertIsNotNone(subscription)
        self.assertEqual(subscription.current_plan, "plus")
        self.assertEqual(subscription.current_active_until, "2026-09-01T00:00:00Z")
        self.assertEqual(subscription.last_confirmed_plan, "plus")
        self.assertEqual(subscription.workspace_plan_type, "personal")
        self.assertEqual(subscription.source, "legacy_probe")
