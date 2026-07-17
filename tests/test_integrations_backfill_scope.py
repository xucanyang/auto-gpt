from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest
from fastapi import HTTPException
from sqlmodel import SQLModel, Session, create_engine

from api import integrations
from core.db import AccountListStateModel, AccountModel
from services import account_filters
from services.account_rate_limit_recovery import reconcile_rate_limited_accounts as real_reconcile_rate_limited_accounts


@pytest.fixture
def backfill_engine(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'integrations-backfill-scope.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)

    with Session(engine) as session:
        account_specs = (
            (1, "chatgpt", "scope-1@example.com", {}),
            (
                2,
                "chatgpt",
                "scope-2@example.com",
                {"sync_statuses": {"cliproxyapi": {"remote_state": "exists", "uploaded": True}}},
            ),
            (3, "other", "scope-3@example.com", {}),
        )
        for account_id, platform, email, extra in account_specs:
            account = AccountModel(
                id=account_id,
                platform=platform,
                email=email,
                password="password",
                token=f"at-{account_id}",
                status="registered",
            )
            account.set_extra({"access_token": f"at-{account_id}", **extra})
            session.add(account)
        session.commit()

        for account_id, platform, _, _ in account_specs:
            session.add(
                AccountListStateModel(
                    account_id=account_id,
                    platform=platform,
                    manually_used=False,
                    auth_type="access_token_only",
                    subscription_type="plus",
                    account_validity="valid",
                    sub2api_state="not_uploaded",
                    oaipay_state="not_uploaded",
                    idea_submit_state="available",
                )
            )
        session.commit()

    monkeypatch.setattr(account_filters, "refresh_stale_account_list_state", lambda *args, **kwargs: 0)
    monkeypatch.setattr(integrations, "engine", engine)
    return engine


def _successful_backfill(account, **_kwargs):
    return {
        "ok": True,
        "uploaded": True,
        "skipped": False,
        "message": f"uploaded {account.id}",
        "results": [],
    }


def test_backfill_request_uses_shared_filter_contract():
    assert account_filters.AccountFilterRequestMixin in integrations.BackfillRequest.__mro__
    assert "expected_total" in integrations.BackfillRequest.model_fields
    with pytest.raises(ValueError):
        integrations.BackfillRequest(expected_total=-1)


def test_filtered_expected_total_mismatch_is_409_after_reconcile_but_before_pending_or_upload(backfill_engine, monkeypatch):
    reconcile = mock.Mock()
    pending_check = mock.Mock(side_effect=AssertionError("pending eligibility must not run"))
    cliproxy_upload = mock.Mock(side_effect=AssertionError("upload must not run"))
    sub2api_upload = mock.Mock(side_effect=AssertionError("upload must not run"))
    oaipay_upload = mock.Mock(side_effect=AssertionError("upload must not run"))
    monkeypatch.setattr(integrations, "reconcile_rate_limited_accounts", reconcile)
    monkeypatch.setattr(integrations, "get_cliproxy_sync_state", pending_check)
    monkeypatch.setattr(integrations, "backfill_chatgpt_account_to_cpa", cliproxy_upload)
    monkeypatch.setattr(integrations, "backfill_chatgpt_account_to_sub2api", sub2api_upload)
    monkeypatch.setattr(integrations, "backfill_chatgpt_account_to_oaipay", oaipay_upload)

    request = integrations.BackfillRequest(
        platforms=["chatgpt"],
        destination="cliproxyapi",
        pending_only=True,
        email="scope-",
        subscription_type="plus",
        expected_total=3,
    )
    with pytest.raises(HTTPException) as raised:
        integrations.backfill_integrations(request)

    assert raised.value.status_code == 409
    assert raised.value.detail["code"] == "FILTER_SCOPE_CHANGED"
    assert raised.value.detail["expected_total"] == 3
    assert raised.value.detail["matched_total"] == 2
    reconcile.assert_called_once()
    assert reconcile.call_args.kwargs == {"platform": "chatgpt"}
    pending_check.assert_not_called()
    cliproxy_upload.assert_not_called()
    sub2api_upload.assert_not_called()
    oaipay_upload.assert_not_called()


def test_filtered_expected_total_match_applies_pending_only_after_scope_check(backfill_engine, monkeypatch):
    reconcile = mock.Mock()
    upload = mock.Mock(side_effect=_successful_backfill)
    monkeypatch.setattr(integrations, "reconcile_rate_limited_accounts", reconcile)
    monkeypatch.setattr(integrations, "backfill_chatgpt_account_to_cpa", upload)

    result = integrations.backfill_integrations(
        integrations.BackfillRequest(
            platforms=["chatgpt"],
            destination="cliproxyapi",
            pending_only=True,
            email="scope-",
            subscription_type="plus",
            expected_total=2,
        )
    )

    assert result["total"] == 1
    assert result["success"] == 1
    assert upload.call_count == 1
    assert upload.call_args.args[0].id == 1
    reconcile.assert_called_once()
    assert reconcile.call_args.kwargs == {"platform": "chatgpt"}


def test_filtered_scope_reconciles_due_rate_limit_before_expected_total(backfill_engine, monkeypatch):
    due_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    with Session(backfill_engine) as session:
        account = AccountModel(
            id=4,
            platform="chatgpt",
            email="scope-rate-limit@example.com",
            password="password",
            token="at-4",
            status="rate_limited",
        )
        account.set_extra(
            {
                "access_token": "at-4",
                "rate_limit_previous_status": "registered",
                "rate_limit_recover_at": due_at.isoformat().replace("+00:00", "Z"),
            }
        )
        session.add(account)
        session.commit()

    upload = mock.Mock(side_effect=AssertionError("recovered account must not remain in rate_limited scope"))
    monkeypatch.setattr(integrations, "reconcile_rate_limited_accounts", real_reconcile_rate_limited_accounts)
    monkeypatch.setattr(integrations, "backfill_chatgpt_account_to_cpa", upload)

    result = integrations.backfill_integrations(
        integrations.BackfillRequest(
            platforms=["chatgpt"],
            destination="cliproxyapi",
            status="rate_limited",
            expected_total=0,
        )
    )

    assert result["total"] == 0
    upload.assert_not_called()
    with Session(backfill_engine) as session:
        recovered = session.get(AccountModel, 4)
        assert recovered is not None
        assert recovered.status == "registered"


def test_selected_scope_ignores_residual_filters_expected_total_and_pending_only(backfill_engine, monkeypatch):
    upload = mock.Mock(side_effect=_successful_backfill)
    monkeypatch.setattr(integrations, "reconcile_rate_limited_accounts", lambda *args, **kwargs: 0)
    monkeypatch.setattr(integrations, "backfill_chatgpt_account_to_cpa", upload)

    result = integrations.backfill_integrations(
        integrations.BackfillRequest(
            platforms=["chatgpt"],
            account_ids=[2],
            destination="cliproxyapi",
            pending_only=True,
            email="does-not-match",
            status="invalid",
            manually_used="true",
            auth_type="refresh_token",
            subscription_type="free",
            account_validity="invalid",
            sub2api_state="exists",
            oaipay_state="exists",
            idea_submit_state="paid",
            expected_total=999,
        )
    )

    assert result["total"] == 1
    assert result["success"] == 1
    assert upload.call_count == 1
    assert upload.call_args.args[0].id == 2


def test_legacy_multi_platform_request_without_expected_total_still_runs(backfill_engine, monkeypatch):
    upload = mock.Mock(side_effect=_successful_backfill)
    monkeypatch.setattr(integrations, "reconcile_rate_limited_accounts", lambda *args, **kwargs: 0)
    monkeypatch.setattr(integrations, "backfill_chatgpt_account_to_cpa", upload)

    result = integrations.backfill_integrations(
        integrations.BackfillRequest(
            platforms=["chatgpt", "other"],
            destination="cliproxyapi",
        )
    )

    assert result["total"] == 3
    assert upload.call_count == 2
