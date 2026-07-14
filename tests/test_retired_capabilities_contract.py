import json
import re
from unittest import mock

import pytest
from fastapi import HTTPException

from api import chatgpt as chatgpt_api
from api import config as config_api
from api import external_subscription
from api import integrations
from api import pipeline as pipeline_api
from api import tasks as tasks_api
from core.base_platform import Account, RegisterConfig
from core.db import AccountModel
from services.chatgpt_core import gopay_flow
from services.chatgpt_core import payment
from services.chatgpt_core.oauth_client import OAuthClient
from services.chatgpt_core.payment_link_cache import (
    build_payment_link_cache_payload,
    normalize_payment_link_plan,
    payment_link_cache_matches,
)
from services.chatgpt_core.plugin import ChatGPTPlatform
from services.pipeline.config import PipelineConfigStore
from services.pipeline.models import PipelineConfig


RETIRED_CONFIG_KEYS = {
    "chatgpt_enable_team_invite",
    "chatgpt_team_invite_deferred_activation",
    "chatgpt_capture_free_workspace",
    "chatgpt_capture_business_workspace",
    "chatgpt_k12_enabled",
    "chatgpt_k12_workspace_ids",
    "chatgpt_k12_save_all_spaces",
    "chatgpt_k12_strict_join",
    "chatgpt_k12_join_timeout_seconds",
    "chatgpt_k12_join_retry_count",
    "chatgpt_k12_post_join_poll_seconds",
    "chatgpt_k12_capture_refresh_tokens",
}


def test_openapi_does_not_expose_retired_product_routes():
    from main import app

    paths = set(app.openapi().get("paths") or {})
    retired_exact_paths = {
        "/api/tasks/chatgpt/k12-workspace-recapture",
        "/api/tasks/chatgpt/k12-workspace-recapture/batch",
        "/api/chatgpt/{account_id}/k12-workspaces/recapture",
        "/api/chatgpt/pending-business-invites",
        "/api/chatgpt/pending-business-invites/{invite_id}/activate",
        "/api/chatgpt/pending-business-invites/batch-activate",
        "/api/chatgpt/pending-business-invites/{invite_id}/abandon",
        "/api/actions/{account_id}/team-source",
        "/api/actions/{account_id}/chatgpt-team-remove",
        "/api/team-lite/settings",
        "/api/team-lite/teams",
        "/api/team-lite/teams/import",
        "/api/team-lite/teams/import-from-account/{account_row_id}",
        "/api/team-lite/teams/live-sync",
        "/api/team-lite/teams/{team_id}/info",
        "/api/team-lite/teams/{team_id}/update",
        "/api/team-lite/teams/{team_id}/refresh",
        "/api/team-lite/teams/batch-refresh",
        "/api/team-lite/teams/{team_id}/delete",
        "/api/team-lite/teams/batch-delete",
        "/api/team-lite/teams/{team_id}/members",
        "/api/team-lite/teams/{team_id}/invite",
        "/api/team-lite/teams/{team_id}/invites/revoke",
        "/api/team-lite/teams/{team_id}/members/delete",
        "/api/team-lite/teams/{team_id}/members/delete-all",
        "/api/team-lite/teams/{team_id}/members/check",
    }

    assert retired_exact_paths.isdisjoint(paths)
    assert not any(path.startswith("/api/team-lite") for path in paths)
    assert not any(path.startswith("/api/teams") for path in paths)
    retired_product_segment = re.compile(
        r"(?:^|[/_-])(?:k12|business|team|teams)(?=$|[/_{}-])",
        re.IGNORECASE,
    )
    assert not {
        path for path in paths if retired_product_segment.search(path)
    }


def test_openapi_hides_legacy_payment_write_fields():
    from main import app

    schemas = app.openapi().get("components", {}).get("schemas", {})
    assert "checkout_url" not in schemas["GoPayStartReq"].get("properties", {})
    assert "checkout_url" not in schemas["GoPayOtpStartByUidRequest"].get("properties", {})
    assert "gopay_plan" not in schemas["PipelineConfig"].get("properties", {})


def test_config_response_and_update_allowlist_drop_retired_keys():
    assert RETIRED_CONFIG_KEYS.isdisjoint(config_api.CONFIG_KEYS)
    stored = {key: "legacy-value" for key in RETIRED_CONFIG_KEYS}

    with mock.patch.object(config_api.config_store, "get_all", return_value=dict(stored)):
        response = config_api._build_config_response()

    assert RETIRED_CONFIG_KEYS.isdisjoint(response)

    with mock.patch.object(config_api.config_store, "get_all", return_value={}), mock.patch.object(
        config_api.config_store,
        "set_many",
    ) as set_many:
        result = config_api.update_config(
            config_api.ConfigUpdate(data=stored)
        )

    assert result == {"ok": True, "updated": []}
    set_many.assert_called_once_with({}, base_revision=None)


def test_payment_plan_normalization_is_plus_only():
    assert normalize_payment_link_plan("plus") == "plus"
    assert normalize_payment_link_plan("team") == "plus"
    assert normalize_payment_link_plan("business") == "plus"
    assert normalize_payment_link_plan("enterprise") == "plus"


def test_payment_config_response_exposes_plus_pricing_only():
    upstream = {
        "country_code": "US",
        "symbol_code": "USD",
        "symbol": "$",
        "minor_unit_exponent": 2,
        "currency_config": {
            "plus": {"price": 20},
            "team": {"price": 30},
            "business": {"price": 35},
            "enterprise": {"price": 99},
        },
    }

    with mock.patch.object(
        payment,
        "fetch_checkout_pricing_config",
        return_value=upstream,
    ), mock.patch.object(
        chatgpt_api,
        "_resolve_optional_checkout_proxy",
        return_value="",
    ):
        response = chatgpt_api.get_payment_config("US")

    assert response["plus"] == {"price": 20}
    assert {"team", "business", "enterprise"}.isdisjoint(response)


def test_oauth_free_selection_rejects_business_only_candidates():
    client = OAuthClient({}, verbose=False)
    business_only = [
        {
            "id": "ws-business",
            "kind": "workspace",
            "plan_type": "chatgptteamplan",
            "name": "Business Workspace",
        }
    ]

    selected = client._pick_workspace_candidate(business_only, "free")

    assert selected is None


@pytest.mark.parametrize("plan", ["team", "business", "enterprise"])
def test_pipeline_config_rejects_non_plus_plan_without_persisting(plan):
    config = PipelineConfig(gopay_plan=plan)
    request = pipeline_api.PipelineConfigUpdateRequest(config=config)

    with mock.patch.object(pipeline_api.pipeline_engine, "set_config") as set_config:
        with pytest.raises(HTTPException) as caught:
            pipeline_api.update_pipeline_config(request)

    assert caught.value.status_code == 400
    assert "plus" in str(caught.value.detail).lower()
    set_config.assert_not_called()


def test_pipeline_config_storage_drops_legacy_plan_field():
    store = PipelineConfigStore()

    with mock.patch(
        "services.pipeline.config.config_store.set",
    ) as set_config:
        saved = store.save(PipelineConfig(gopay_plan="team"))

    persisted = json.loads(set_config.call_args.args[1])
    assert "gopay_plan" not in persisted
    assert "gopay_plan" not in saved.model_dump()


@pytest.mark.parametrize("plan", ["team", "business", "enterprise"])
def test_plus_cache_does_not_match_historical_retired_plan(plan):
    cached = {
        "url": "https://pay.openai.com/c/pay/cs_live_legacy123#fid_real",
        "plan": plan,
        "country": "ID",
        "currency": "IDR",
        "proxy": "",
        "payment_link_format": "long_hosted",
    }

    assert payment_link_cache_matches(
        cached,
        {
            "plan": "plus",
            "country": "ID",
            "currency": "IDR",
            "payment_link_format": "long_hosted",
        },
    ) is False


@pytest.mark.parametrize("plan", ["team", "business", "enterprise"])
def test_cache_builder_does_not_relabel_historical_retired_link_as_plus(plan):
    historical = {
        "url": "https://pay.openai.com/c/pay/cs_live_legacy123#fid_real",
        "plan": plan,
        "country": "ID",
        "currency": "IDR",
        "payment_link_format": "long_hosted",
    }

    assert build_payment_link_cache_payload(
        historical,
        source="legacy_cache",
    ) == {}
    assert build_payment_link_cache_payload(
        {"url": historical["url"]},
        source="legacy_fallback",
        fallback=historical,
    ) == {}


def test_fresh_plus_link_can_replace_retired_fallback_cache():
    historical = {
        "url": "https://pay.openai.com/c/pay/cs_live_legacy123#fid_real",
        "plan": "team",
        "country": "ID",
        "currency": "IDR",
        "payment_link_format": "long_hosted",
    }
    fresh = build_payment_link_cache_payload(
        {
            "url": "https://pay.openai.com/c/pay/cs_live_plus123#fid_real",
            "plan": "plus",
            "country": "ID",
            "currency": "IDR",
            "payment_link_format": "long_hosted",
        },
        source="fresh_plus",
        fallback=historical,
    )

    assert fresh["plan"] == "plus"
    assert "cs_live_plus123" in fresh["url"]


@pytest.mark.parametrize("plan", ["team", "business", "enterprise"])
def test_external_subscription_claim_rejects_retired_plan_before_db_scan(plan):
    request = external_subscription.ClaimSubscriptionLinksRequest(plan=plan)
    session = mock.Mock()
    session.exec.side_effect = AssertionError("retired plan reached database scan")

    with mock.patch.object(
        external_subscription,
        "_schedule_due_local_verifications",
    ) as schedule_due, mock.patch.object(
        external_subscription,
        "_expire_stale_claims",
    ) as expire_stale:
        with pytest.raises(HTTPException) as caught:
            external_subscription.claim_subscription_links(request, session=session)

    assert caught.value.status_code == 400
    assert "Plus" in str(caught.value.detail)
    schedule_due.assert_not_called()
    expire_stale.assert_not_called()
    session.exec.assert_not_called()


@pytest.mark.parametrize("plan", ["team", "business", "enterprise"])
def test_external_subscription_does_not_claim_historical_retired_plan_link(plan):
    request = external_subscription.ClaimSubscriptionLinksRequest(plan="")

    assert external_subscription._claim_matches_filters(
        {"plan": plan, "country": "US", "currency": "USD"},
        request,
    ) is False


@pytest.mark.parametrize("plan", ["team", "business", "enterprise"])
def test_external_subscription_parser_cannot_relabel_retired_link_as_plus(plan):
    account = AccountModel(
        platform="chatgpt",
        email="legacy-link@example.com",
        password="pw",
        status="registered",
        cashier_url="https://chatgpt.com/checkout/openai_llc/cs_legacy",
    )
    account.set_extra(
        {
            "chatgpt_last_payment_link": {
                "url": "https://chatgpt.com/checkout/openai_llc/cs_legacy",
                "plan": plan,
                "country": "US",
                "currency": "USD",
                "checkout_amount": "0",
                "checkout_amount_is_zero": True,
            }
        }
    )

    parsed = external_subscription._payment_link_from_account(account)

    if parsed:
        assert parsed["plan"] == plan
        assert external_subscription._claim_matches_filters(
            parsed,
            external_subscription.ClaimSubscriptionLinksRequest(),
        ) is False


def test_external_subscription_rejects_bare_cashier_url_without_explicit_plus_metadata():
    account = AccountModel(
        platform="chatgpt",
        email="legacy-bare-link@example.com",
        password="pw",
        status="registered",
        cashier_url="https://chatgpt.com/checkout/openai_llc/cs_legacy_bare",
    )
    account.set_extra({})

    assert external_subscription._payment_link_from_account(account) == {}


def test_external_subscription_rejects_paypal_link_without_explicit_plus_metadata():
    account = AccountModel(
        platform="chatgpt",
        email="legacy-paypal-link@example.com",
        password="pw",
        status="registered",
    )
    account.set_extra(
        {
            "chatgpt_paypal_url": {
                "paypal_url": "https://www.paypal.com/agreements/approve?ba_token=BA-LEGACY"
            }
        }
    )

    assert external_subscription._payment_link_from_account(account) == {}


@pytest.mark.parametrize("plan", ["team", "business", "enterprise"])
def test_payment_action_rejects_retired_plans(plan):
    platform = ChatGPTPlatform(config=RegisterConfig(extra={}))
    account = Account(
        platform="chatgpt",
        email="payment@example.com",
        password="pw",
        token="at-demo",
    )

    with mock.patch("services.chatgpt_core.payment.generate_plus_link") as generate_plus:
        result = platform.execute_action(
            "payment_link",
            account,
            {"plan": plan},
        )

    assert result["ok"] is False
    assert "Plus" in result["error"]
    generate_plus.assert_not_called()


@pytest.mark.parametrize(
    "params",
    [
        {"plan": "team"},
        {"plan": "business"},
        {"promo_code": "TEAM50"},
        {"workspace_name": "Legacy Workspace"},
        {"seat_quantity": 5},
        {"price_interval": "year"},
    ],
)
def test_batch_payment_link_entry_rejects_retired_params_before_account_scan(
    params,
):
    request = tasks_api.BatchPaymentLinkTaskRequest(
        account_ids=[1],
        params=params,
    )

    with mock.patch.object(
        tasks_api,
        "_resolve_batch_payment_link_accounts",
        side_effect=AssertionError("retired payment params reached account scan"),
    ) as resolve_accounts:
        with pytest.raises(HTTPException) as caught:
            tasks_api.enqueue_batch_payment_link_task(request)

    assert caught.value.status_code == 400
    resolve_accounts.assert_not_called()


@pytest.mark.parametrize(
    "defaults",
    [
        {"plan": "team"},
        {"plan": "business"},
        {"promo_code": "TEAM50"},
        {"workspace_name": "Legacy Workspace"},
        {"seat_quantity": 5},
        {"price_interval": "year"},
    ],
)
def test_gopay_batch_entry_rejects_retired_defaults_before_runtime_lookup(
    defaults,
):
    request = chatgpt_api.GoPayBatchStartReq(
        items=[
            chatgpt_api.GoPayBatchItemReq(
                account_id=1,
                phone=chatgpt_api.GoPayBatchPhoneReq(
                    phone_country_code="62",
                    phone_number="81234567890",
                ),
            )
        ],
        defaults=defaults,
    )

    with mock.patch.object(
        chatgpt_api,
        "_active_gopay_batch_task",
        side_effect=AssertionError("retired batch defaults reached runtime lookup"),
    ) as active_batch:
        with pytest.raises(HTTPException) as caught:
            chatgpt_api.start_gopay_batch_payment(request, session=mock.Mock())

    assert caught.value.status_code == 400
    active_batch.assert_not_called()


@pytest.mark.parametrize("plan", ["team", "business"])
def test_main_gopay_start_rejects_retired_plan_before_account_lookup(plan):
    request = chatgpt_api.GoPayStartReq(
        phone_country_code="62",
        phone_number="81234567890",
        plan=plan,
    )

    with mock.patch.object(chatgpt_api, "_get_account") as get_account:
        with pytest.raises(HTTPException) as caught:
            chatgpt_api.start_gopay_payment(1, request, session=mock.Mock())

    assert caught.value.status_code == 400
    assert "Plus" in str(caught.value.detail)
    get_account.assert_not_called()


def test_main_gopay_start_rejects_external_checkout_before_account_lookup():
    request = chatgpt_api.GoPayStartReq(
        phone_country_code="62",
        phone_number="81234567890",
        plan="plus",
        checkout_url="https://chatgpt.com/checkout/openai_llc/cs_live_external123",
    )

    with mock.patch.object(chatgpt_api, "_get_account") as get_account:
        with pytest.raises(HTTPException) as caught:
            chatgpt_api.start_gopay_payment(1, request, session=mock.Mock())

    assert caught.value.status_code == 400
    assert "checkout_url" in str(caught.value.detail)
    get_account.assert_not_called()


@pytest.mark.parametrize(
    "checkout_input",
    [
        "https://chatgpt.com/checkout/team/cs_live_retired123",
        "https://chatgpt.com/checkout/business/cs_live_retired123",
        "https://checkout.stripe.com/c/pay/cs_live_retired123?plan_type=team",
        "cs_live_retired123?plan=business",
    ],
)
def test_parse_checkout_url_rejects_explicit_retired_product_markers(
    checkout_input,
):
    with pytest.raises(gopay_flow.GoPayFlowError) as caught:
        gopay_flow.parse_checkout_url(checkout_input)

    assert any(
        marker in str(caught.value)
        for marker in ("Plus", "Team", "Business", "Enterprise")
    )


@pytest.mark.parametrize(
    "checkout_input",
    [
        "https://chatgpt.com/checkout/team/cs_live_retired123",
        "cs_live_retired123?plan=business",
    ],
)
def test_external_retired_checkout_cannot_bypass_plus_request_plan(
    checkout_input,
):
    account = Account(
        platform="chatgpt",
        email="gopay@example.com",
        password="pw",
        token="at-demo",
    )

    with mock.patch.object(gopay_flow.threading, "Thread") as thread:
        with pytest.raises(gopay_flow.GoPayFlowError) as caught:
            gopay_flow.create_gopay_session(
                1,
                account,
                plan="plus",
                country="ID",
                currency="IDR",
                proxy="http://127.0.0.1:7890",
                phone_country_code="62",
                phone_number="81234567890",
                checkout_url=checkout_input,
            )

    assert any(
        marker in str(caught.value)
        for marker in ("Plus", "Team", "Business", "Enterprise")
    )
    thread.assert_not_called()


@pytest.mark.parametrize("plan", ["team", "business", "enterprise"])
def test_gopay_uid_start_rejects_retired_plan_before_lookup(plan):
    request = integrations.GoPayOtpStartByUidRequest(
        account_id=1,
        uid="uid-demo",
        plan=plan,
    )

    with mock.patch.object(integrations, "_find_uid_binding") as find_binding:
        with pytest.raises(HTTPException) as caught:
            integrations.start_gopay_payment_by_uid(request)

    assert caught.value.status_code == 400
    assert "Plus" in str(caught.value.detail)
    find_binding.assert_not_called()


def test_gopay_uid_start_rejects_external_checkout_before_lookup():
    request = integrations.GoPayOtpStartByUidRequest(
        account_id=1,
        uid="uid-demo",
        plan="plus",
        checkout_url="cs_live_external123",
    )

    with mock.patch.object(integrations, "_find_uid_binding") as find_binding:
        with pytest.raises(HTTPException) as caught:
            integrations.start_gopay_payment_by_uid(request)

    assert caught.value.status_code == 400
    assert "checkout_url" in str(caught.value.detail)
    find_binding.assert_not_called()
