import hashlib
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest
from fastapi import HTTPException
from sqlmodel import SQLModel, Session, create_engine

from api import accounts, actions, chatgpt, tasks
from core.db import AccountListStateModel, AccountModel, PaymentLinkGenerationModel
from services import account_filters
from services.account_rate_limit_recovery import reconcile_rate_limited_accounts as real_reconcile_rate_limited_accounts


FILTERED_TASK_REQUESTS = (
    tasks.BatchResumeSubscriptionAuthTaskRequest,
    tasks.PhoneBindingTestTaskRequest,
    tasks.BaxiGptCdkSubmitTaskRequest,
    tasks.ChatGptPaypalBindTaskRequest,
    tasks.BatchPaymentLinkTaskRequest,
    tasks.BatchSub2ApiUploadTaskRequest,
    tasks.BatchOaipayUploadTaskRequest,
    tasks.BatchInvalidRecheckTaskRequest,
    tasks.BatchPaymentEligibilityTaskRequest,
    tasks.BatchProbeLocalStatusTaskRequest,
    tasks.BatchAccountActionTaskRequest,
)


class RecordingBackgroundTasks:
    def __init__(self):
        self.calls = []

    def add_task(self, *args, **kwargs):
        self.calls.append((args, kwargs))


@pytest.fixture
def filter_engine(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'filtered-task-scope.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)

    account_specs = (
        (1, "scope-1@example.com", "plus", "access_token_only", "valid", "not_found"),
        (2, "scope-2@example.com", "plus", "access_token_only", "valid", "exists"),
        (3, "scope-3@example.com", "free", "access_token_only", "valid", "not_found"),
        (4, "scope-4@example.com", "plus", "refresh_token", "valid", "not_found"),
        (5, "scope-5@example.com", "plus", "access_token_only", "invalid", "not_found"),
    )
    with Session(engine) as session:
        for account_id, email, subscription_type, auth_type, _, oaipay_state in account_specs:
            account = AccountModel(
                id=account_id,
                platform="chatgpt",
                email=email,
                password="password",
                token=f"at-{account_id}",
                status="registered",
            )
            extra = {
                "access_token": f"at-{account_id}",
                "sync_statuses": {
                    "oaipay": {
                        "remote_state": oaipay_state,
                        "uploaded": oaipay_state == "exists",
                    }
                },
            }
            if auth_type == "refresh_token":
                extra["refresh_token"] = f"rt-{account_id}"
            account.set_extra(extra)
            session.add(account)
        session.commit()

        for account_id, _, subscription_type, auth_type, account_validity, oaipay_state in account_specs:
            session.add(
                AccountListStateModel(
                    account_id=account_id,
                    platform="chatgpt",
                    manually_used=False,
                    auth_type=auth_type,
                    subscription_type=subscription_type,
                    account_validity=account_validity,
                    sub2api_state="not_uploaded",
                    oaipay_state="uploaded" if oaipay_state == "exists" else "not_uploaded",
                    idea_submit_state="available",
                )
            )
        session.commit()

    monkeypatch.setattr(account_filters, "refresh_stale_account_list_state", lambda *args, **kwargs: 0)
    monkeypatch.setattr(tasks, "engine", engine)
    monkeypatch.setattr(tasks, "reconcile_rate_limited_accounts", lambda *args, **kwargs: 0)
    monkeypatch.setattr(actions, "reconcile_rate_limited_accounts", lambda *args, **kwargs: 0)
    monkeypatch.setattr(accounts, "_maybe_reconcile_rate_limited_accounts", lambda *args, **kwargs: None)
    return engine


def test_all_filtered_capable_request_schemas_share_oaipay_and_expected_total():
    expected_fields = set(account_filters.ACCOUNT_FILTER_FIELD_NAMES) | {"expected_total"}
    for request_model in (*FILTERED_TASK_REQUESTS, actions.BatchActionRequest):
        assert account_filters.AccountFilterRequestMixin in request_model.__mro__
        assert expected_fields <= set(request_model.model_fields), request_model.__name__
        assert request_model.model_fields["expected_total"].metadata
        request_kwargs = (
            {"action_id": "refresh_token"}
            if request_model is tasks.BatchAccountActionTaskRequest
            else {}
        )
        request = request_model(
            submit_state="failed",
            has_submitted="true",
            **request_kwargs,
        )
        assert request.submit_state == "failed"
        assert request.has_submitted == "true"
        with pytest.raises(ValueError):
            request_model(expected_total=-1, **request_kwargs)


def test_payment_link_generated_both_values_normalize_to_unfiltered_state():
    assert account_filters.normalize_account_filter({"payment_link_generated": "true"})["payment_link_generated"] is True
    assert account_filters.normalize_account_filter({"payment_link_generated": "false"})["payment_link_generated"] is False
    assert account_filters.normalize_account_filter({"payment_link_generated": "true,false"})["payment_link_generated"] is None


def test_oaipay_plus_not_received_filter_does_not_expand_and_old_request_is_compatible(filter_engine):
    request = tasks.BatchOaipayUploadTaskRequest(
        all_filtered=True,
        auth_type="access_token_only,unknown",
        subscription_type="plus,pro",
        account_validity="valid",
        oaipay_state="unknown,not_found,deleted_exact_match,cross_workspace_only",
    )

    eligible, missing_ids, skipped, matched = tasks._resolve_batch_oaipay_upload_accounts(request)

    assert request.expected_total is None
    assert [item["account_id"] for item in matched] == [1]
    assert [item["account_id"] for item in eligible] == [1]
    assert missing_ids == []
    assert skipped == []


def test_accounts_list_and_task_resolver_return_the_same_scope(filter_engine):
    request = tasks.BatchOaipayUploadTaskRequest(
        all_filtered=True,
        auth_type="access_token_only,unknown",
        subscription_type="plus,pro",
        account_validity="valid",
        oaipay_state="unknown,not_found,deleted_exact_match,cross_workspace_only",
    )
    with Session(filter_engine) as session:
        resolution = account_filters.resolve_filtered_accounts(
            session,
            platform="chatgpt",
            filter_source=request,
            verify_expected_total=True,
        )
        listed = accounts.list_accounts(
            platform="chatgpt",
            auth_type="access_token_only,unknown",
            subscription_type="plus,pro",
            account_validity="valid",
            oaipay_state="unknown,not_found,deleted_exact_match,cross_workspace_only",
            page=1,
            page_size=200,
            session=session,
        )

    assert listed["total"] == resolution.matched_total == 1
    assert resolution.expected_total is None
    assert resolution.verified is False
    assert {item["id"] for item in listed["items"]} == set(resolution.account_ids) == {1}


def test_exact_email_list_is_case_insensitive_deduplicated_and_shared_by_tasks(filter_engine):
    raw_emails = [
        "  SCOPE-1@EXAMPLE.COM ",
        "scope-3@example.com",
        "scope-1@example.com",
    ]
    normalized = account_filters.normalize_account_filter({"emails": raw_emails})
    assert normalized["email"] == ""
    assert normalized["emails"] == ["scope-1@example.com", "scope-3@example.com"]

    request = tasks.BatchOaipayUploadTaskRequest(
        all_filtered=True,
        emails=raw_emails,
    )
    with Session(filter_engine) as session:
        listed = accounts.query_accounts(
            accounts.AccountListQueryRequest(
                emails=raw_emails,
                page=1,
                page_size=200,
            ),
            session=session,
        )
        resolution = account_filters.resolve_filtered_accounts(
            session,
            platform="chatgpt",
            filter_source=request,
            verify_expected_total=True,
        )

    assert listed["total"] == resolution.matched_total == 2
    assert {item["id"] for item in listed["items"]} == set(resolution.account_ids) == {1, 3}
    assert resolution.audit["filter"]["email"] == ""
    assert resolution.audit["filter"]["emails"] == ["scope-1@example.com", "scope-3@example.com"]

    with pytest.raises(ValueError):
        accounts.AccountListQueryRequest(
            emails=[f"account-{index}@example.com" for index in range(1001)]
        )


def test_multiline_email_compatibility_is_exact_while_single_email_remains_fuzzy(filter_engine):
    multiline = "scope-2@example.com\n\n SCOPE-4@EXAMPLE.COM\nscope-2@example.com"
    normalized = account_filters.normalize_account_filter({"email": multiline})
    assert normalized["email"] == ""
    assert normalized["emails"] == ["scope-2@example.com", "scope-4@example.com"]

    long_exact_list = [f"missing-{index}@example.com" for index in range(805)]
    long_exact_list.extend(["scope-1@example.com", "scope-5@example.com"])
    with Session(filter_engine) as session:
        multiline_rows = session.exec(
            account_filters.account_filtered_query(
                session,
                platform="chatgpt",
                filter_source={"email": multiline},
            )[0]
        ).all()
        fuzzy_rows = session.exec(
            account_filters.account_base_query(platform="chatgpt", email="scope-")
        ).all()
        chunked_rows = session.exec(
            account_filters.account_base_query(platform="chatgpt", emails=long_exact_list)
        ).all()

    assert {item.id for item in multiline_rows} == {2, 4}
    assert {item.id for item in fuzzy_rows} == {1, 2, 3, 4, 5}
    assert {item.id for item in chunked_rows} == {1, 5}


def test_accounts_list_defaults_to_registration_desc_and_supports_expiry_then_registration(filter_engine):
    created_at_by_id = {
        1: datetime(2026, 1, 3, tzinfo=timezone.utc),
        2: datetime(2026, 1, 2, tzinfo=timezone.utc),
        3: datetime(2026, 1, 1, tzinfo=timezone.utc),
        4: datetime(2026, 1, 5, tzinfo=timezone.utc),
        5: datetime(2026, 1, 4, tzinfo=timezone.utc),
    }
    expiry_by_id = {
        1: "2030-01-02T00:00:00Z",
        2: "2030-01-01T00:00:00Z",
        3: "2030-01-01T00:00:00Z",
    }
    with Session(filter_engine) as session:
        for account_id, created_at in created_at_by_id.items():
            account = session.get(AccountModel, account_id)
            assert account is not None
            account.created_at = created_at
            extra = account.get_extra()
            expiry = expiry_by_id.get(account_id, "")
            if expiry:
                extra["chatgpt_local"] = {
                    "subscription": {"subscription_active_until": expiry}
                }
            account.set_extra(extra)
            session.add(account)
        session.commit()
        account_filters.refresh_account_list_state(session)

        default_list = accounts.list_accounts(
            platform="chatgpt",
            page=1,
            page_size=200,
            session=session,
        )
        legacy_expiry_list = accounts.list_accounts(
            platform="chatgpt",
            sort_by="subscription_active_until",
            sort_order="asc",
            page=1,
            page_size=200,
            session=session,
        )
        combined_list = accounts.list_accounts(
            platform="chatgpt",
            sort_by="subscription_active_until,created_at",
            sort_order="asc,desc",
            page=1,
            page_size=200,
            session=session,
        )

    assert [item["id"] for item in default_list["items"]] == [4, 5, 1, 2, 3]
    assert [item["id"] for item in legacy_expiry_list["items"]] == [2, 3, 1, 4, 5]
    assert [item["id"] for item in combined_list["items"]] == [2, 3, 1, 4, 5]


def test_filtered_selection_freezes_the_first_n_accounts_in_current_list_order(filter_engine):
    created_at_by_id = {
        1: datetime(2026, 1, 1, tzinfo=timezone.utc),
        2: datetime(2026, 1, 5, tzinfo=timezone.utc),
        3: datetime(2026, 1, 3, tzinfo=timezone.utc),
        4: datetime(2026, 1, 4, tzinfo=timezone.utc),
        5: datetime(2026, 1, 2, tzinfo=timezone.utc),
    }
    with Session(filter_engine) as session:
        for account_id, created_at in created_at_by_id.items():
            account = session.get(AccountModel, account_id)
            assert account is not None
            account.created_at = created_at
            session.add(account)
        session.commit()

        listed = accounts.list_accounts(
            platform="chatgpt",
            sort_by="created_at",
            sort_order="desc",
            page=1,
            page_size=200,
            session=session,
        )
        resolved = accounts.resolve_account_filter_selection(
            accounts.AccountFilterSelectionRequest(
                expected_total=5,
                sort_by="created_at",
                sort_order="desc",
                limit=3,
            ),
            session=session,
        )

    assert resolved["matched_total"] == listed["total"] == 5
    assert resolved["selected_count"] == 3
    assert resolved["account_ids"] == [item["id"] for item in listed["items"][:3]] == [2, 4, 3]
    assert [item["id"] for item in resolved["preview_items"]] == resolved["account_ids"]


def test_filtered_selection_rejects_scope_drift_and_unbounded_quantity(filter_engine):
    with Session(filter_engine) as session, pytest.raises(HTTPException) as raised:
        accounts.resolve_account_filter_selection(
            accounts.AccountFilterSelectionRequest(expected_total=4, limit=3),
            session=session,
        )

    assert raised.value.status_code == 409
    assert raised.value.detail["code"] == "FILTER_SCOPE_CHANGED"
    assert raised.value.detail["expected_total"] == 4
    assert raised.value.detail["matched_total"] == 5

    with pytest.raises(ValueError):
        accounts.AccountFilterSelectionRequest(
            expected_total=5,
            limit=accounts.ACCOUNT_FILTER_SELECTION_MAX_IDS + 1,
        )

    with pytest.raises(ValueError):
        accounts.AccountFilterSelectionRequest(limit=3)


def test_filtered_selection_matches_subscription_expiry_sort_with_stable_tiebreak(filter_engine):
    expiry_by_id = {
        1: "2030-01-02T00:00:00Z",
        2: "2030-01-01T00:00:00Z",
        3: "2030-01-01T00:00:00Z",
    }
    tied_created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with Session(filter_engine) as session:
        for account_id, expiry in expiry_by_id.items():
            account = session.get(AccountModel, account_id)
            assert account is not None
            if account_id in {2, 3}:
                account.created_at = tied_created_at
            extra = account.get_extra()
            extra["chatgpt_local"] = {
                "subscription": {"subscription_active_until": expiry}
            }
            account.set_extra(extra)
            session.add(account)
        session.commit()
        account_filters.refresh_account_list_state(session)

        listed = accounts.list_accounts(
            platform="chatgpt",
            sort_by="subscription_active_until,created_at",
            sort_order="asc,desc",
            page=1,
            page_size=200,
            session=session,
        )
        resolved = accounts.resolve_account_filter_selection(
            accounts.AccountFilterSelectionRequest(
                expected_total=5,
                sort_by="subscription_active_until,created_at",
                sort_order="asc,desc",
                limit=4,
            ),
            session=session,
        )

    assert resolved["account_ids"] == [item["id"] for item in listed["items"][:4]]
    assert resolved["account_ids"][:3] == [3, 2, 1]


def test_unfiltered_account_list_refreshes_payment_history_state(filter_engine):
    with Session(filter_engine) as session:
        account = session.get(AccountModel, 1)
        assert account is not None
        state = session.get(AccountListStateModel, 1)
        assert state is not None
        state.payment_link_generated = False
        state.source_updated_at = str(account.updated_at)
        state.derivation_version = account_filters.ACCOUNT_LIST_STATE_DERIVATION_VERSION
        session.add(state)
        session.add(
            PaymentLinkGenerationModel(
                account_id=1,
                account_email=str(account.email or "").strip().lower(),
                account_created_at=account.created_at.replace(tzinfo=None).isoformat(sep=" "),
                request_id="unfiltered-payment-history-1",
                status="succeeded",
                url="https://payments.example.test/history-1",
            )
        )
        session.commit()

        listed = accounts.list_accounts(
            platform="chatgpt",
            page=1,
            page_size=200,
            session=session,
        )

    item = next(item for item in listed["items"] if item["id"] == 1)
    assert item["payment_link_generated"] is True


def test_account_detail_refreshes_payment_history_state(filter_engine, monkeypatch):
    monkeypatch.setattr(accounts, "reconcile_rate_limited_accounts", lambda *args, **kwargs: 0)
    with Session(filter_engine) as session:
        account = session.get(AccountModel, 1)
        assert account is not None
        state = session.get(AccountListStateModel, 1)
        assert state is not None
        state.payment_link_generated = False
        state.source_updated_at = str(account.updated_at)
        state.derivation_version = account_filters.ACCOUNT_LIST_STATE_DERIVATION_VERSION
        session.add(state)
        session.add(
            PaymentLinkGenerationModel(
                account_id=1,
                account_email=str(account.email or "").strip().lower(),
                account_created_at=account.created_at.replace(tzinfo=None).isoformat(sep=" "),
                request_id="detail-payment-history-1",
                status="succeeded",
                url="https://payments.example.test/detail-history-1",
            )
        )
        session.commit()

        payload = accounts.get_account(1, session=session)

    assert payload["payment_link_generated"] is True


def test_phone_binding_filter_keeps_list_and_task_scope_identical(filter_engine):
    with Session(filter_engine) as session:
        state = session.get(AccountListStateModel, 4)
        assert state is not None
        state.phone_binding_state = "confirmed"
        session.add(state)
        session.commit()

        request = tasks.PhoneBindingTestTaskRequest(
            all_filtered=True,
            phone_binding_state="confirmed",
        )
        resolution = account_filters.resolve_filtered_accounts(
            session,
            platform="chatgpt",
            filter_source=request,
            verify_expected_total=True,
        )
        listed = accounts.list_accounts(
            platform="chatgpt",
            phone_binding_state="confirmed",
            page=1,
            page_size=200,
            session=session,
        )

    assert listed["total"] == resolution.matched_total == 1
    assert {item["id"] for item in listed["items"]} == set(resolution.account_ids) == {4}


def test_payment_link_platform_filter_keeps_list_and_task_scope_identical(filter_engine):
    with Session(filter_engine) as session:
        state = session.get(AccountListStateModel, 2)
        assert state is not None
        state.payment_link_platform = "pix"
        session.add(state)
        session.commit()

        request = tasks.BatchPaymentLinkTaskRequest(
            all_filtered=True,
            payment_link_platform="pix",
        )
        resolution = account_filters.resolve_filtered_accounts(
            session,
            platform="chatgpt",
            filter_source=request,
            verify_expected_total=True,
        )
        listed = accounts.list_accounts(
            platform="chatgpt",
            payment_link_platform="pix",
            page=1,
            page_size=200,
            session=session,
        )

    assert listed["total"] == resolution.matched_total == 1
    assert {item["id"] for item in listed["items"]} == set(resolution.account_ids) == {2}


def test_payment_eligibility_filters_keep_list_and_task_scope_identical(filter_engine):
    with Session(filter_engine) as session:
        state = session.get(AccountListStateModel, 3)
        assert state is not None
        state.zero_amount_eligibility_state = "eligible"
        state.zero_amount_eligibility_display_state = "eligible"
        state.gcash_payment_method_state = "available"
        session.add(state)
        session.commit()

        request = tasks.BatchPaymentEligibilityTaskRequest(
            all_filtered=True,
            zero_amount_eligibility_state="eligible",
            gcash_payment_method_state="available",
        )
        resolution = account_filters.resolve_filtered_accounts(
            session,
            platform="chatgpt",
            filter_source=request,
            verify_expected_total=True,
        )
        listed = accounts.list_accounts(
            platform="chatgpt",
            zero_amount_eligibility_state="eligible",
            gcash_payment_method_state="available",
            page=1,
            page_size=200,
            session=session,
        )

    assert listed["total"] == resolution.matched_total == 1
    assert {item["id"] for item in listed["items"]} == set(resolution.account_ids) == {3}

    with Session(filter_engine) as session:
        state = session.get(AccountListStateModel, 3)
        assert state is not None
        state.zero_amount_eligibility_display_state = "probe_failed"
        session.add(state)
        session.commit()

        request = tasks.BatchPaymentEligibilityTaskRequest(
            all_filtered=True,
            zero_amount_eligibility_state="probe_failed",
        )
        resolution = account_filters.resolve_filtered_accounts(
            session,
            platform="chatgpt",
            filter_source=request,
            verify_expected_total=True,
        )
        listed = accounts.list_accounts(
            platform="chatgpt",
            zero_amount_eligibility_state="probe_failed",
            page=1,
            page_size=200,
            session=session,
        )

    assert listed["total"] == resolution.matched_total == 1
    assert {item["id"] for item in listed["items"]} == set(resolution.account_ids) == {3}


def test_filtered_pix_export_scope_freezes_only_saved_pix_links(filter_engine):
    with Session(filter_engine) as session:
        account = session.get(AccountModel, 2)
        assert account is not None
        extra = account.get_extra()
        extra["chatgpt_last_payment_link"] = {
            "url": "https://payments.example.test/filtered-pix-link",
            "link_type": "pix",
        }
        account.set_extra(extra)
        state = session.get(AccountListStateModel, 2)
        assert state is not None
        state.payment_link_platform = "pix"
        session.add(account)
        session.add(state)
        session.commit()

        request = chatgpt.Sub2ApiExportTicketReq(
            mode=chatgpt.CHATGPT_EXPORT_MODE_PIX_PAYMENT_LINKS,
            all_filtered=True,
            payment_link_platform="pix",
            expected_total=1,
        )
        account_ids = chatgpt._resolve_pix_payment_link_export_account_ids(
            req=request,
            session=session,
        )

    assert account_ids == [2]


def test_filtered_pix_export_rejects_a_stale_scope_before_ticket_creation(filter_engine):
    with Session(filter_engine) as session:
        state = session.get(AccountListStateModel, 2)
        assert state is not None
        state.payment_link_platform = "pix"
        session.add(state)
        session.commit()

        request = chatgpt.Sub2ApiExportTicketReq(
            mode=chatgpt.CHATGPT_EXPORT_MODE_PIX_PAYMENT_LINKS,
            all_filtered=True,
            payment_link_platform="pix",
            expected_total=2,
        )
        with pytest.raises(HTTPException) as raised:
            chatgpt._resolve_pix_payment_link_export_account_ids(req=request, session=session)

    assert raised.value.status_code == 409
    assert raised.value.detail["code"] == "FILTER_SCOPE_CHANGED"


def test_filtered_payment_link_export_freezes_mixed_current_link_types(filter_engine):
    with Session(filter_engine) as session:
        for account_id, link_type in ((1, "pix"), (2, "paypal")):
            account = session.get(AccountModel, account_id)
            assert account is not None
            extra = account.get_extra()
            extra["chatgpt_last_payment_link"] = {
                "url": f"https://payments.example.test/{link_type}-current",
                "link_type": link_type,
            }
            account.set_extra(extra)
            state = session.get(AccountListStateModel, account_id)
            assert state is not None
            state.payment_link_platform = link_type
            session.add(account)
            session.add(state)
        session.commit()

        request = chatgpt.Sub2ApiExportTicketReq(
            mode=chatgpt.CHATGPT_EXPORT_MODE_PAYMENT_LINKS,
            all_filtered=True,
            auth_type="access_token_only",
            subscription_type="plus",
            account_validity="valid",
            expected_total=2,
        )
        account_ids = chatgpt._resolve_payment_link_export_account_ids(
            req=request,
            session=session,
        )

    assert account_ids == [1, 2]


@pytest.mark.parametrize(
    "export_mode",
    (
        chatgpt.CHATGPT_EXPORT_MODE_SUB2API,
        chatgpt.CHATGPT_EXPORT_MODE_ACCESS_TOKEN,
    ),
)
def test_standard_export_without_selected_ids_freezes_the_complete_filtered_scope(filter_engine, export_mode):
    request = chatgpt.Sub2ApiExportTicketReq(
        mode=export_mode,
        all_filtered=True,
        auth_type="access_token_only",
        subscription_type="plus",
        account_validity="valid",
        expected_total=2,
    )

    with Session(filter_engine) as session:
        account_ids = chatgpt._resolve_chatgpt_export_account_ids(
            req=request,
            session=session,
            export_mode=export_mode,
        )

    assert account_ids == [1, 2]


def test_standard_filtered_export_rejects_an_empty_scope_instead_of_falling_back_to_all_accounts(filter_engine):
    request = chatgpt.Sub2ApiExportTicketReq(
        mode=chatgpt.CHATGPT_EXPORT_MODE_SUB2API,
        all_filtered=True,
        email="missing-export-account@example.com",
        expected_total=0,
    )

    with Session(filter_engine) as session, pytest.raises(HTTPException) as raised:
        chatgpt._resolve_chatgpt_export_account_ids(
            req=request,
            session=session,
            export_mode=request.mode,
        )

    assert raised.value.status_code == 400
    assert "当前筛选范围没有可导出的账号" in str(raised.value.detail)


def test_pix_user_link_filtered_scope_uses_saved_links_without_access_token(filter_engine):
    with Session(filter_engine) as session:
        account = session.get(AccountModel, 2)
        assert account is not None
        extra = account.get_extra()
        extra.pop("access_token", None)
        extra["chatgpt_last_payment_link"] = {
            "url": "https://payments.stripe.com/qr/instructions/filtered-pix-link",
            "link_type": "pix",
            "link_expires_at": 4_102_444_800,
        }
        account.token = ""
        account.set_extra(extra)
        state = session.get(AccountListStateModel, 2)
        assert state is not None
        state.payment_link_platform = "pix"
        session.add(account)
        session.add(state)
        session.commit()

    request = tasks.BaxiGptCdkSubmitTaskRequest(
        all_filtered=True,
        payment_channel="pix",
        pix_submit_mode="user_link",
        payment_link_platform="pix",
    )
    eligible, missing_ids, skipped, matched = tasks._resolve_baxigpt_cdk_submit_accounts(
        request,
        require_access_token=False,
        require_saved_pix_link=True,
    )

    assert [item["account_id"] for item in matched] == [2]
    assert [item["account_id"] for item in eligible] == [2]
    assert missing_ids == []
    assert skipped == []


def test_expected_total_mismatch_has_no_task_thread_background_or_phone_pool_side_effect(filter_engine, monkeypatch):
    create_task = mock.Mock()
    import_phones = mock.Mock()
    thread = mock.Mock()
    reconcile = mock.Mock()
    background_tasks = RecordingBackgroundTasks()
    monkeypatch.setattr(tasks, "_create_standalone_task_record", create_task)
    monkeypatch.setattr(tasks, "_import_manual_phone_entries_to_pool", import_phones)
    monkeypatch.setattr(tasks.threading, "Thread", thread)
    monkeypatch.setattr(tasks, "reconcile_rate_limited_accounts", reconcile)

    request = tasks.PhoneBindingTestTaskRequest(
        all_filtered=True,
        email="scope-1@example.com",
        expected_total=2,
        phone_lines="+15551230001----https://sms.example.com/code",
    )
    with pytest.raises(HTTPException) as raised:
        tasks.enqueue_phone_binding_test_task(request, background_tasks=background_tasks)

    assert raised.value.status_code == 409
    assert raised.value.detail == {
        "code": "FILTER_SCOPE_CHANGED",
        "expected_total": 2,
        "matched_total": 1,
        "message": raised.value.detail["message"],
    }
    assert "筛选结果已变化" in raised.value.detail["message"]
    create_task.assert_not_called()
    import_phones.assert_not_called()
    thread.assert_not_called()
    reconcile.assert_called_once()
    assert background_tasks.calls == []


def test_batch_action_expected_total_mismatch_is_409_before_execution(filter_engine, monkeypatch):
    reconcile = mock.Mock()
    monkeypatch.setattr(actions, "reconcile_rate_limited_accounts", reconcile)
    request = actions.BatchActionRequest(
        all_filtered=True,
        email="scope-1@example.com",
        expected_total=0,
    )
    with Session(filter_engine) as session, pytest.raises(HTTPException) as raised:
        actions._resolve_batch_accounts("chatgpt", request, session)

    assert raised.value.status_code == 409
    assert raised.value.detail["code"] == "FILTER_SCOPE_CHANGED"
    assert raised.value.detail["expected_total"] == 0
    assert raised.value.detail["matched_total"] == 1
    reconcile.assert_called_once()


def test_filtered_task_reconciles_due_rate_limits_before_scope_resolution(filter_engine, monkeypatch):
    due_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    with Session(filter_engine) as session:
        account = AccountModel(
            id=6,
            platform="chatgpt",
            email="scope-rate-limit@example.com",
            password="password",
            token="at-6",
            status="rate_limited",
        )
        account.set_extra(
            {
                "access_token": "at-6",
                "rate_limit_previous_status": "registered",
                "rate_limit_recover_at": due_at.isoformat().replace("+00:00", "Z"),
            }
        )
        session.add(account)
        session.commit()

    monkeypatch.setattr(tasks, "reconcile_rate_limited_accounts", real_reconcile_rate_limited_accounts)
    request = tasks.BatchProbeLocalStatusTaskRequest(
        all_filtered=True,
        status="rate_limited",
        expected_total=0,
    )
    with Session(filter_engine) as session:
        rows = tasks._filtered_chatgpt_accounts(session, request)
        recovered = session.get(AccountModel, 6)

    assert rows == []
    assert recovered is not None
    assert recovered.status == "registered"


def test_batch_auth_task_meta_contains_complete_verified_filter_audit(monkeypatch):
    captured = {}
    matched = [
        {"account_id": 11, "email": "a@example.com", "status": "pending_payment"},
        {"account_id": 12, "email": "b@example.com", "status": "pending_payment"},
    ]
    eligible = [matched[0]]
    skipped = [{**matched[1], "reason": "账号当前无需补抓 Auth"}]
    monkeypatch.setattr(
        tasks,
        "_resolve_batch_resume_auth_accounts",
        lambda _request: (list(eligible), [], list(skipped), list(matched)),
    )
    monkeypatch.setattr(
        tasks,
        "_create_standalone_task_record",
        lambda _task_id, *, platform, source, total, meta: captured.update(meta=meta),
    )
    monkeypatch.setattr(tasks, "_save_task_log", lambda *args, **kwargs: None)
    background_tasks = RecordingBackgroundTasks()
    request = tasks.BatchResumeSubscriptionAuthTaskRequest(
        all_filtered=True,
        email=" @example.com ",
        status="registered,pending_payment",
        manually_used="true",
        auth_type="access_token,refresh_token",
        subscription_type="plus",
        account_validity="valid",
        sub2api_state="not_found",
        oaipay_state="not_found",
        idea_submit_state="unsubmitted",
        expected_total=2,
    )

    result = tasks.enqueue_batch_resume_subscription_auth_task(
        request,
        background_tasks=background_tasks,
    )

    audit = captured["meta"]["filter_audit"]
    assert result["eligible"] == 1
    assert set(audit["filter"]) == set(account_filters.ACCOUNT_FILTER_FIELD_NAMES)
    assert audit["filter"]["email"] == "@example.com"
    assert audit["filter"]["manually_used"] is True
    assert audit["filter"]["idea_submit_state"] == "available"
    assert audit["filter"]["sub2api_state"] == "not_uploaded"
    assert audit["filter"]["oaipay_state"] == "not_uploaded"
    assert audit["expected_total"] == 2
    assert audit["matched_total"] == 2
    assert audit["verified"] is True
    assert audit["account_ids_count"] == 1
    assert audit["account_ids_sha256"] == hashlib.sha256(b"[11]").hexdigest()
    assert audit["matched_account_ids_count"] == 2
    assert audit["matched_account_ids_sha256"] == hashlib.sha256(b"[11,12]").hexdigest()
    assert audit["resolver_version"] == account_filters.ACCOUNT_FILTER_RESOLVER_VERSION
    assert captured["meta"]["account_ids"] == [11]
    assert background_tasks.calls[0][0][2] == [11]
