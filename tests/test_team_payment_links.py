import hashlib
import json
import unittest
from unittest import mock

from sqlmodel import Session, SQLModel, create_engine, select

import api.tasks as tasks_api
from api.actions import _apply_action_result
from core.base_platform import Account, RegisterConfig
from core.db import AccountModel, PaymentLinkGenerationModel
from services.chatgpt_core.payment_link_cache import (
    build_payment_link_cache_payload,
    normalize_payment_link_params,
    payment_link_cache_for_params,
    payment_link_cache_matches,
    payment_link_variant_key,
    store_payment_link_variant,
    validate_payment_link_request_params,
)
from services.chatgpt_core.plugin import ChatGPTPlatform


PROFILE_HASH = "f" * 64
TEAM_URL = "https://pay.openai.com/c/pay/cs_team_test#hosted"
LEGACY_CUSTOM_TEAM_URL = "https://chatgpt.com/checkout/openai_llc/cs_team_custom"
PROMO_DIGEST = hashlib.sha256(b"TEAM50").hexdigest()


def team_params(workspace: str = "Workspace A", **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "plan": "team",
        "team_plan_data": {
            "workspace_name": workspace,
            "price_interval": "month",
            "seat_quantity": 2,
        },
        "promo_code_digest": PROMO_DIGEST,
        "cancel_url": "https://chatgpt.com/team-cancel",
        "plan_name": "chatgptteamplan",
        "checkout_proxy_region": "GB",
        "checkout_ui_mode": "hosted",
        "country": "GB",
        "currency": "GBP",
        "payment_link_format": "long_link",
        "payment_source": "long_link",
        "profile_hash": PROFILE_HASH,
    }
    values.update(overrides)
    return values


def team_result(workspace: str = "Workspace A", **overrides: object) -> dict[str, object]:
    values = {
        **team_params(workspace),
        "url": TEAM_URL,
        "link_type": "team",
        "generation_kind": "team_checkout",
        "workspace_name": workspace,
        "price_interval": "month",
        "seat_quantity": 2,
        "cancel_url": "https://chatgpt.com/team-cancel",
        "remote_batch_id": "batch_" + "a" * 32,
        "remote_job_id": "job-team-1",
        "remote_request_id": "task:team:1",
        "generated_at": "2026-07-18T08:00:00+00:00",
    }
    values["variant_key"] = payment_link_variant_key(values)
    values.update(overrides)
    return values


class TeamPaymentLinkContractTests(unittest.TestCase):
    def test_team_partial_overrides_are_allowed_but_invalid_or_ambiguous_inputs_fail(self) -> None:
        validate_payment_link_request_params({"plan": "team", "checkout_proxy_region": "gb"})
        validate_payment_link_request_params({
            "plan": "team",
            "checkout_proxy_region": "US",
            "workspace_name": "Named Workspace",
        })
        filtered = tasks_api._filtered_payment_link_request_params({
            "plan": "team",
            "checkoutProxyRegion": "ca",
            "workspace_name": "Named Workspace",
            "country": "IN",
        })
        self.assertEqual(filtered["checkout_proxy_region"], "CA")
        self.assertEqual(filtered["checkout_ui_mode"], "hosted")
        self.assertNotIn("country", filtered)
        custom = tasks_api._filtered_payment_link_request_params({
            "plan": "team",
            "checkout_proxy_region": "CA",
            "checkout_ui_mode": "custom",
        })
        self.assertEqual(custom["checkout_ui_mode"], "custom")

        invalid = (
            {"plan": "team", "checkout_proxy_region": "GB", "price_interval": "week"},
            {"plan": "team"},
            {"plan": "team", "checkout_proxy_region": "United States"},
            {"plan": "team", "checkout_proxy_region": "GB", "seat_quantity": 1},
            {"plan": "team", "checkout_proxy_region": "GB", "seat_quantity": 1001},
            {"plan": "team", "checkout_proxy_region": "GB", "cancel_url": "javascript:alert(1)"},
            {"plan": "team", "checkout_proxy_region": "GB", "checkout_ui_mode": "embedded"},
            {"plan": "business"},
            {"plan": "enterprise"},
            {"plan": "plus", "promo_code": "TEAM50"},
            {"workspace_name": "Missing Team plan"},
        )
        for params in invalid:
            with self.subTest(params=params), self.assertRaises(ValueError):
                validate_payment_link_request_params(params)

    def test_variant_key_changes_for_workspace_coupon_interval_and_profile(self) -> None:
        base = team_params()
        variants = [
            team_params("Workspace B"),
            team_params(promo_code_digest=hashlib.sha256(b"OTHER").hexdigest()),
            team_params(team_plan_data={"workspace_name": "Workspace A", "price_interval": "year", "seat_quantity": 2}),
            team_params(profile_hash="e" * 64),
            team_params(checkout_proxy_region="US"),
            team_params(checkout_ui_mode="custom"),
        ]

        base_key = payment_link_variant_key(base)
        self.assertEqual(len(base_key), 64)
        self.assertEqual(len({base_key, *(payment_link_variant_key(item) for item in variants)}), 7)

    def test_browser_profile_view_keeps_team_summary_but_redacts_coupon_digest(self) -> None:
        view = tasks_api._payment_link_profile_view({
            "profile_hash": PROFILE_HASH,
            "effective_concurrency": "invalid",
            "profile": {
                "link_type": "team",
                "plan": "team",
                "generation_kind": "team_checkout",
                "billing_country": "GB",
                "currency": "GBP",
                "checkout_ui_mode": "hosted",
                "regions": {"checkout": "GB"},
                "promo_code_digest": PROMO_DIGEST,
                "team": {
                    "workspace_name": "Visible Workspace",
                    "price_interval": "year",
                    "seat_quantity": "invalid",
                    "cancel_url": "https://chatgpt.com/cancel",
                    "promo_code_configured": True,
                },
            },
        })

        self.assertEqual(view["plan"], "team")
        self.assertEqual(view["team"]["workspace_name"], "Visible Workspace")
        self.assertTrue(view["team"]["promo_code_configured"])
        self.assertEqual(view["team"]["seat_quantity"], 0)
        self.assertEqual(view["effective_concurrency"], 0)
        self.assertEqual(view["regions"]["checkout"], "GB")
        self.assertEqual(view["checkout_ui_mode"], "hosted")
        self.assertNotIn("promo_code_digest", view)
        self.assertNotIn(PROMO_DIGEST, json.dumps(view))

    def test_plus_and_team_variants_are_stored_and_reused_independently(self) -> None:
        first_params = team_params("Workspace A")
        second_params = team_params("Workspace B")
        first_cache = build_payment_link_cache_payload(
            team_result("Workspace A"),
            source="team-test",
        )
        second_cache = build_payment_link_cache_payload(
            team_result(
                "Workspace B",
                url="https://pay.openai.com/c/pay/cs_team_b#hosted",
                remote_request_id="task:team:2",
            ),
            source="team-test",
        )
        plus_params = {
            "plan": "plus",
            "country": "GB",
            "currency": "GBP",
            "payment_link_format": "long_link",
            "payment_source": "long_link",
            "profile_hash": PROFILE_HASH,
        }
        plus_cache = build_payment_link_cache_payload(
            {
                **plus_params,
                "url": "https://checkout.stripe.com/c/pay/cs_plus_test",
                "generation_kind": "plus_checkout",
            },
            source="plus-test",
        )
        extra: dict[str, object] = {}
        store_payment_link_variant(extra, plus_cache)
        store_payment_link_variant(extra, first_cache)
        store_payment_link_variant(extra, second_cache)

        self.assertEqual(payment_link_cache_for_params(extra, first_params)["workspace_name"], "Workspace A")
        self.assertEqual(payment_link_cache_for_params(extra, second_params)["workspace_name"], "Workspace B")
        self.assertEqual(payment_link_cache_for_params(extra, plus_params)["plan"], "plus")
        self.assertFalse(payment_link_cache_matches(first_cache, second_params))
        self.assertFalse(payment_link_cache_matches(first_cache, plus_params))
        self.assertFalse(payment_link_cache_matches(plus_cache, first_params))

    def test_legacy_custom_team_route_is_not_reused_as_hosted_default(self) -> None:
        legacy_custom = build_payment_link_cache_payload(
            team_result(
                url=LEGACY_CUSTOM_TEAM_URL,
                checkout_ui_mode="",
                variant_key="",
            ),
            source="legacy-custom-test",
        )

        self.assertEqual(legacy_custom["checkout_ui_mode"], "custom")
        self.assertFalse(payment_link_cache_matches(legacy_custom, team_params()))

    def test_skip_guard_blocks_only_the_matching_team_variant(self) -> None:
        first_params = normalize_payment_link_params(team_params("Workspace A"))
        second_params = normalize_payment_link_params(team_params("Workspace B"))
        first_cache = build_payment_link_cache_payload(team_result("Workspace A"), source="team-test")
        account = AccountModel(
            id=71,
            platform="chatgpt",
            email="variant@example.com",
            password="pw",
            token="access-token",
            status="registered",
        )
        account.set_extra({
            "chatgpt_last_payment_link": first_cache,
            "chatgpt_payment_link_variants": {first_cache["variant_key"]: first_cache},
        })

        self.assertIn(
            "当前支付链接",
            tasks_api._payment_link_skip_reason(account, variant_params=first_params),
        )
        self.assertEqual(tasks_api._payment_link_skip_reason(account, variant_params=second_params), "")

    def test_team_action_preserves_legacy_paypal_and_persists_redacted_history(self) -> None:
        engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            account = AccountModel(
                platform="chatgpt",
                email="team-history@example.com",
                password="pw",
                token="access-token",
                status="registered",
            )
            account.set_extra({
                "chatgpt_paypal_url": {
                    "url": "https://www.paypal.com/agreements/approve?ba_token=BA-OLD",
                    "plan": "plus",
                }
            })
            session.add(account)
            session.commit()
            session.refresh(account)

            data = team_result()
            _apply_action_result(
                "chatgpt",
                "payment_link",
                account,
                {"ok": True, "data": data},
                session,
            )
            session.commit()
            session.refresh(account)
            history = session.exec(
                select(PaymentLinkGenerationModel).where(
                    PaymentLinkGenerationModel.request_id == "task:team:1"
                )
            ).one()

            extra = account.get_extra()
            self.assertIn("BA-OLD", extra["chatgpt_paypal_url"]["url"])
            self.assertEqual(extra["chatgpt_last_payment_link"]["plan"], "team")
            self.assertEqual(extra["chatgpt_last_payment_link"]["workspace_name"], "Workspace A")
            self.assertEqual(history.generation_kind, "team_checkout")
            self.assertEqual(history.variant_key, data["variant_key"])
            self.assertEqual(history.get_result()["workspace_name"], "Workspace A")
            self.assertEqual(history.get_result()["checkout_proxy_region"], "GB")
            self.assertEqual(history.get_result()["checkout_ui_mode"], "hosted")
            self.assertEqual(history.get_result()["promo_code_digest"], PROMO_DIGEST)
            self.assertNotIn("TEAM50", history.result_json)

    def test_plugin_sends_team_overrides_to_profile_and_batch(self) -> None:
        account = Account(
            platform="chatgpt",
            email="team-action@example.com",
            password="pw",
            token="access-token-secret",
        )
        overrides = {
            "plan": "team",
            "team_plan_data": {
                "workspace_name": "Action Workspace",
                "price_interval": "year",
                "seat_quantity": 6,
            },
            "promo_code": "TEAM50",
            "checkout_proxy_region": "CA",
            "cancel_url": "https://chatgpt.com/action",
            "request_id": "task:team:action",
        }
        client = mock.Mock()
        client.get_profile.return_value = {
            "profile_hash": PROFILE_HASH,
            "link_type": "team",
            "plan": "team",
            "generation_kind": "team_checkout",
            "country": "GB",
            "currency": "GBP",
            "checkout_ui_mode": "hosted",
            "regions": {"checkout": "CA"},
            "promo_code_digest": PROMO_DIGEST,
            "team": {
                "workspace_name": "Action Workspace",
                "price_interval": "year",
                "seat_quantity": 6,
                "cancel_url": "https://chatgpt.com/action",
            },
            "profile": {"checkout_ui_mode": "hosted"},
        }
        client.submit_batch.return_value = {
            "batch_id": "batch_" + "c" * 32,
            "items": [
                {
                    "batch_id": "batch_" + "c" * 32,
                    "job_id": "job-team-action",
                    "request_id": "task:team:action",
                    "profile_hash": PROFILE_HASH,
                    "status": "done",
                    "completed_at": 1_721_000_000,
                    "result": {
                        "url": TEAM_URL,
                        "link_type": "team",
                        "generation_kind": "team_checkout",
                        "plan_name": "chatgptteamplan",
                        "billing_country": "GB",
                        "currency": "GBP",
                        "team_plan_data": {
                            "workspace_name": "Action Workspace",
                            "price_interval": "year",
                            "seat_quantity": 6,
                            "cancel_url": "https://chatgpt.com/action",
                        },
                        "promo_code_digest": PROMO_DIGEST,
                    },
                }
            ],
        }
        platform = ChatGPTPlatform(config=RegisterConfig(extra={}))

        with mock.patch(
            "services.chatgpt_core.long_link_payment_client.LongLinkPaymentClient.from_env",
            return_value=client,
        ), mock.patch("services.chatgpt_core.payment.generate_plus_link") as hosted_plus:
            result = platform.execute_action("payment_link", account, overrides)

        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["plan"], "team")
        self.assertEqual(result["data"]["workspace_name"], "Action Workspace")
        self.assertEqual(result["data"]["checkout_proxy_region"], "CA")
        self.assertEqual(result["data"]["checkout_ui_mode"], "hosted")
        effective_overrides = {**overrides, "checkout_ui_mode": "hosted"}
        client.get_profile.assert_called_once_with(overrides=effective_overrides)
        client.submit_batch.assert_called_once_with(
            items=[{"access_token": "access-token-secret", "request_id": "task:team:action"}],
            expected_profile_hash=PROFILE_HASH,
            profile_overrides=effective_overrides,
        )
        hosted_plus.assert_not_called()


if __name__ == "__main__":
    unittest.main()
