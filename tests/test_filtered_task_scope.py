import hashlib
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest
from fastapi import HTTPException
from sqlmodel import SQLModel, Session, create_engine

from api import accounts, actions, tasks
from core.db import AccountListStateModel, AccountModel
from services import account_filters
from services.account_rate_limit_recovery import reconcile_rate_limited_accounts as real_reconcile_rate_limited_accounts


FILTERED_TASK_REQUESTS = (
    tasks.BatchK12WorkspaceRecaptureTaskRequest,
    tasks.BatchResumeSubscriptionAuthTaskRequest,
    tasks.PhoneBindingTestTaskRequest,
    tasks.BaxiGptCdkSubmitTaskRequest,
    tasks.ChatGptPaypalBindTaskRequest,
    tasks.BatchPaymentLinkTaskRequest,
    tasks.BatchSub2ApiUploadTaskRequest,
    tasks.BatchOaipayUploadTaskRequest,
    tasks.BatchInvalidRecheckTaskRequest,
    tasks.BatchProbeLocalStatusTaskRequest,
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
                    sub2api_state="not_found",
                    oaipay_state=oaipay_state,
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
        with pytest.raises(ValueError):
            request_model(expected_total=-1)


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
