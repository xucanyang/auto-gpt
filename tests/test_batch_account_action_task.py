import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest import mock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlmodel import SQLModel, Session, create_engine

from api import actions as actions_api
from api import tasks
from core.config_store import config_store
from core.db import AccountListStateModel, AccountModel
from core.task_runtime import RegisterTaskStore
from services import account_filters


class RecordingBackgroundTasks:
    def __init__(self):
        self.calls = []

    def add_task(self, *args, **kwargs):
        self.calls.append((args, kwargs))


@pytest.fixture
def account_action_engine(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'batch-account-action.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)

    account_specs = (
        (1, "first@example.com", "plus", "access_token_only"),
        (2, "second@example.com", "plus", "refresh_token"),
        (3, "third@example.com", "free", "access_token_only"),
    )
    with Session(engine) as session:
        for account_id, email, subscription_type, auth_type in account_specs:
            account = AccountModel(
                id=account_id,
                platform="chatgpt",
                email=email,
                password="password",
                token=f"at-{account_id}",
                status="registered",
                user_id=f"user-{account_id}",
            )
            extra = {
                "access_token": f"at-{account_id}",
                "session_token": f"session-{account_id}",
                "cookies": f"oai-session=secret-{account_id}",
            }
            if auth_type == "refresh_token":
                extra["refresh_token"] = f"rt-{account_id}"
            account.set_extra(extra)
            session.add(account)
            session.add(
                AccountListStateModel(
                    account_id=account_id,
                    platform="chatgpt",
                    manually_used=False,
                    auth_type=auth_type,
                    subscription_type=subscription_type,
                    account_validity="valid",
                    sub2api_state="not_uploaded",
                    oaipay_state="not_uploaded",
                    idea_submit_state="available",
                )
            )
        session.add(
            AccountModel(
                id=9,
                platform="other",
                email="other@example.com",
                password="password",
                status="registered",
            )
        )
        session.commit()

    monkeypatch.setattr(tasks, "engine", engine)
    monkeypatch.setattr(
        tasks,
        "reconcile_rate_limited_accounts",
        lambda *args, **kwargs: 0,
    )
    monkeypatch.setattr(
        account_filters,
        "refresh_stale_account_list_state",
        lambda *args, **kwargs: 0,
    )
    return engine


@pytest.fixture
def account_action_task_store(monkeypatch):
    store = RegisterTaskStore(max_finished_tasks=50, cleanup_threshold=75)
    monkeypatch.setattr(tasks, "_task_store", store)
    monkeypatch.setattr(tasks, "_save_task_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(config_store, "get_all", lambda: {})
    return store


@pytest.mark.parametrize(
    ("task_request", "expected_scope"),
    [
        (
            tasks.BatchAccountActionTaskRequest(
                action_id="refresh_token",
                account_ids=[1],
            ),
            "single",
        ),
        (
            tasks.BatchAccountActionTaskRequest(
                action_id="refresh_token",
                account_ids=[1, 2],
            ),
            "selected",
        ),
        (
            tasks.BatchAccountActionTaskRequest(
                action_id="refresh_token",
                all_filtered=True,
            ),
            "filtered",
        ),
    ],
)
def test_account_action_scope_infers_single_selected_and_filtered(
    task_request,
    expected_scope,
):
    assert tasks._account_action_scope(task_request) == expected_scope


@pytest.mark.parametrize(
    "task_request",
    [
        tasks.BatchAccountActionTaskRequest(action_id="refresh_token"),
        tasks.BatchAccountActionTaskRequest(
            action_id="refresh_token",
            account_ids=[1],
            all_filtered=True,
        ),
        tasks.BatchAccountActionTaskRequest(
            action_id="refresh_token",
            scope="single",
            account_ids=[1, 2],
        ),
        tasks.BatchAccountActionTaskRequest(
            action_id="refresh_token",
            scope="selected",
            all_filtered=True,
        ),
        tasks.BatchAccountActionTaskRequest(
            action_id="refresh_token",
            scope="filtered",
            account_ids=[1],
        ),
    ],
)
def test_account_action_scope_rejects_ambiguous_or_mismatched_requests(task_request):
    with pytest.raises(HTTPException) as raised:
        tasks._account_action_scope(task_request)

    assert raised.value.status_code == 400


def test_selected_scope_freezes_order_deduplicates_and_reports_missing_ids(
    account_action_engine,
):
    request = tasks.BatchAccountActionTaskRequest(
        action_id="refresh_token",
        scope="selected",
        account_ids=[2, 1, 2, 999],
        expected_total=999,
    )

    eligible, missing, skipped, matched, total_requested, scope = (
        tasks._resolve_batch_account_action_accounts(request)
    )

    assert [item["account_id"] for item in eligible] == [2, 1]
    assert missing == [999]
    assert skipped == []
    assert matched == []
    assert total_requested == 3
    assert scope == "selected"


def test_single_scope_is_the_selected_contract_with_exactly_one_account(
    account_action_engine,
):
    request = tasks.BatchAccountActionTaskRequest(
        action_id="refresh_token",
        scope="single",
        account_ids=[1],
    )

    eligible, missing, skipped, matched, total_requested, scope = (
        tasks._resolve_batch_account_action_accounts(request)
    )

    assert [item["account_id"] for item in eligible] == [1]
    assert missing == []
    assert skipped == []
    assert matched == []
    assert total_requested == 1
    assert scope == "single"


def test_filtered_scope_freezes_the_complete_verified_filter_result(
    account_action_engine,
):
    request = tasks.BatchAccountActionTaskRequest(
        action_id="refresh_token",
        scope="filtered",
        all_filtered=True,
        subscription_type="plus",
        expected_total=2,
    )

    eligible, missing, skipped, matched, total_requested, scope = (
        tasks._resolve_batch_account_action_accounts(request)
    )

    assert [item["account_id"] for item in eligible] == [1, 2]
    assert [item["account_id"] for item in matched] == [1, 2]
    assert missing == []
    assert skipped == []
    assert total_requested == 2
    assert scope == "filtered"


def test_filtered_scope_rejects_expected_total_drift_before_task_creation(
    account_action_engine,
):
    request = tasks.BatchAccountActionTaskRequest(
        action_id="refresh_token",
        scope="filtered",
        all_filtered=True,
        subscription_type="plus",
        expected_total=1,
    )

    with pytest.raises(HTTPException) as raised:
        tasks._resolve_batch_account_action_accounts(request)

    assert raised.value.status_code == 409
    assert raised.value.detail["code"] == "FILTER_SCOPE_CHANGED"
    assert raised.value.detail["expected_total"] == 1
    assert raised.value.detail["matched_total"] == 2


def test_filtered_scope_can_freeze_an_empty_result(account_action_engine):
    request = tasks.BatchAccountActionTaskRequest(
        action_id="refresh_token",
        scope="filtered",
        all_filtered=True,
        email="does-not-exist@example.com",
        expected_total=0,
    )

    eligible, missing, skipped, matched, total_requested, scope = (
        tasks._resolve_batch_account_action_accounts(request)
    )

    assert eligible == []
    assert missing == []
    assert skipped == []
    assert matched == []
    assert total_requested == 0
    assert scope == "filtered"


def test_account_action_safe_meta_never_contains_runtime_secrets():
    params = {
        "api_url": "https://api.example.test/upload?token=url-secret",
        "api_key": "api-secret",
        "access_token": "at-secret",
        "refresh_token": "rt-secret",
        "cookies": "oai-session=cookie-secret",
        "mode": "protocol",
        "upload_type": "codex",
        "confirm_logout": True,
    }

    safe = tasks._account_action_safe_params_meta("logout_web_session", params)
    dumped = json.dumps(safe, ensure_ascii=False)

    assert safe == {
        "mode": "protocol",
        "upload_type": "codex",
        "custom_api_url": True,
        "custom_api_key": True,
        "confirmation_acknowledged": True,
    }
    for leaked in (
        "url-secret",
        "api-secret",
        "at-secret",
        "rt-secret",
        "cookie-secret",
    ):
        assert leaked not in dumped


def test_task_history_meta_summary_keeps_action_identity_but_drops_params_and_secrets():
    summary = tasks._task_log_meta_summary(
        {
            "meta": {
                "action_id": "upload_sub2api",
                "action_label": "上传 Sub2API",
                "scope": "filtered",
                "eligible": 20,
                "total_requested": 21,
                "params": {
                    "api_url": "https://sub2api.example.test/?token=url-secret",
                    "api_key": "api-secret",
                },
                "api_key": "top-level-secret",
            }
        }
    )

    assert {
        key: summary.get(key)
        for key in (
            "action_id",
            "action_label",
            "scope",
            "eligible",
            "total_requested",
        )
    } == {
        "action_id": "upload_sub2api",
        "action_label": "上传 Sub2API",
        "scope": "filtered",
        "eligible": 20,
        "total_requested": 21,
    }
    dumped = json.dumps(summary, ensure_ascii=False)
    assert "params" not in summary
    assert "api_key" not in summary
    assert "url-secret" not in dumped
    assert "api-secret" not in dumped
    assert "top-level-secret" not in dumped


def test_auth_revision_changes_only_when_frozen_auth_material_changes():
    account = AccountModel(
        id=1,
        platform="chatgpt",
        email="revision@example.com",
        password="password",
        token="at-original",
        user_id="user-1",
        status="registered",
    )
    account.set_extra(
        {
            "access_token": "at-original",
            "refresh_token": "rt-original",
            "session_token": "session-original",
            "cookies": "oai-session=cookie-original",
        }
    )
    original = tasks._account_action_auth_revision(account)

    account.status = "pending_payment"
    assert tasks._account_action_auth_revision(account) == original

    extra = account.get_extra()
    extra["refresh_token"] = "rt-rotated"
    account.set_extra(extra)
    assert tasks._account_action_auth_revision(account) != original


def test_non_task_account_action_cannot_enter_the_unified_task_endpoint():
    with pytest.raises(HTTPException) as raised:
        tasks._account_action_definition("change_email")

    assert raised.value.status_code == 400
    assert "不支持" in str(raised.value.detail)


def test_future_catalog_task_action_uses_generic_handler_without_code_allowlist(
    monkeypatch,
):
    action = {
        "id": "future_account_action",
        "label": "未来账号操作",
        "params": [],
        "execution": {
            "mode": "task",
            "handler": "account_action",
            "scopes": ["single", "selected", "filtered"],
        },
    }
    monkeypatch.setattr(
        "services.chatgpt_core.ChatGPTPlatform.get_platform_actions",
        lambda _self: [action],
    )

    assert tasks._account_action_definition(action["id"]) == action


@pytest.mark.parametrize(
    ("action_id", "handler"),
    [
        ("future_account_action", "unknown_handler"),
        ("probe_local_status", "account_action"),
        ("future_account_action", "probe_local_status"),
    ],
)
def test_catalog_task_action_rejects_unknown_or_mismatched_handler(
    monkeypatch,
    action_id,
    handler,
):
    monkeypatch.setattr(
        "services.chatgpt_core.ChatGPTPlatform.get_platform_actions",
        lambda _self: [
            {
                "id": action_id,
                "label": action_id,
                "execution": {
                    "mode": "task",
                    "handler": handler,
                    "scopes": ["single", "selected", "filtered"],
                },
            }
        ],
    )

    with pytest.raises(HTTPException) as raised:
        tasks._account_action_definition(action_id)

    assert raised.value.status_code == 400
    assert "执行器" in str(raised.value.detail)


@pytest.mark.parametrize(
    "action_id",
    [
        "probe_local_status",
        "sync_cliproxyapi_status",
        "sync_sub2api_status",
        "sync_oaipay_status",
        "refresh_token",
        "refresh_web_session",
        "logout_web_session",
        "logout_and_revoke_tokens",
        "upload_cpa",
        "upload_sub2api",
        "upload_codex_proxy",
        "upload_oaipay",
    ],
)
def test_every_catalog_task_action_has_a_valid_dispatch_definition(action_id):
    assert tasks._account_action_definition(action_id)["id"] == action_id


def test_legacy_batch_action_route_delegates_task_mode_actions(monkeypatch):
    captured = {}
    background = RecordingBackgroundTasks()

    def fake_enqueue(request, *, background_tasks=None):
        captured["request"] = request
        captured["background_tasks"] = background_tasks
        return {"task_id": "task_legacy_batch", "source": tasks.ACCOUNT_ACTION_SOURCE}

    monkeypatch.setattr(tasks, "enqueue_batch_account_action_task", fake_enqueue)

    response = actions_api.execute_batch_action(
        "chatgpt",
        "refresh_token",
        actions_api.BatchActionRequest(account_ids=[2, 1], params={"mode": "refresh"}),
        session=mock.Mock(),
        background_tasks=background,
    )

    request = captured["request"]
    assert isinstance(request, tasks.BatchAccountActionTaskRequest)
    assert request.action_id == "refresh_token"
    assert request.account_ids == [2, 1]
    assert request.params == {"mode": "refresh"}
    assert captured["background_tasks"] is background
    assert response["task_id"] == "task_legacy_batch"


def test_legacy_single_action_route_delegates_task_mode_actions_and_freezes_danger_total(
    account_action_engine,
    monkeypatch,
):
    captured = {}
    background = RecordingBackgroundTasks()

    def fake_enqueue(request, *, background_tasks=None):
        captured["request"] = request
        captured["background_tasks"] = background_tasks
        return {"task_id": "task_legacy_single", "source": tasks.ACCOUNT_ACTION_SOURCE}

    monkeypatch.setattr(tasks, "enqueue_batch_account_action_task", fake_enqueue)

    with Session(account_action_engine) as session:
        response = actions_api.execute_action(
            "chatgpt",
            1,
            "logout_web_session",
            actions_api.ActionRequest(params={"confirm_logout": True}),
            session=session,
            background_tasks=background,
        )

    request = captured["request"]
    assert isinstance(request, tasks.BatchAccountActionTaskRequest)
    assert request.scope == "single"
    assert request.account_ids == [1]
    assert request.confirmed_total == 1
    assert request.params == {"confirm_logout": True}
    assert captured["background_tasks"] is background
    assert response["task_id"] == "task_legacy_single"


def test_legacy_dangerous_batch_route_cannot_infer_confirmed_total(
    account_action_engine,
    account_action_task_store,
):
    with Session(account_action_engine) as session, pytest.raises(HTTPException) as raised:
        actions_api.execute_batch_action(
            "chatgpt",
            "logout_web_session",
            actions_api.BatchActionRequest(
                account_ids=[1, 2],
                params={"confirm_logout": True},
            ),
            session=session,
        )

    assert raised.value.status_code == 409
    assert raised.value.detail["code"] == "ACCOUNT_ACTION_CONFIRMATION_SCOPE_CHANGED"
    assert raised.value.detail["confirmed_total"] is None
    assert raised.value.detail["total_requested"] == 2


def test_legacy_batch_action_rejects_negative_confirmed_total_during_request_validation():
    with pytest.raises(ValidationError):
        actions_api.BatchActionRequest(
            account_ids=[1],
            confirmed_total=-1,
            params={"confirm_logout": True},
        )


def test_generic_enqueue_immediately_returns_safe_task_snapshot_and_runtime_params_stay_in_memory(
    account_action_engine,
    account_action_task_store,
):
    background = RecordingBackgroundTasks()
    request = tasks.BatchAccountActionTaskRequest(
        action_id="upload_cpa",
        scope="selected",
        account_ids=[1, 2],
        params={
            "api_url": "https://cpa.example.test/upload?token=url-secret",
            "api_key": "api-secret",
        },
    )

    response = tasks.enqueue_batch_account_action_task(
        request,
        background_tasks=background,
    )

    assert response["task_id"]
    assert response["source"] == tasks.ACCOUNT_ACTION_SOURCE
    assert response["action_id"] == "upload_cpa"
    assert response["action_label"] == "上传 CPA"
    assert response["scope"] == "selected"
    assert response["total_requested"] == 2
    assert response["matched"] == 2
    assert response["eligible"] == 2
    snapshot = response["task_snapshot"]
    assert snapshot["id"] == response["task_id"]
    assert snapshot["source"] == tasks.ACCOUNT_ACTION_SOURCE
    assert snapshot["meta"]["account_action"] == {
        "action_id": "upload_cpa",
        "action_label": "上传 CPA",
        "scope": "selected",
    }
    assert snapshot["meta"]["params"] == {
        "custom_api_url": True,
        "custom_api_key": True,
    }
    serialized_response = json.dumps(response, ensure_ascii=False)
    assert "url-secret" not in serialized_response
    assert "api-secret" not in serialized_response

    assert len(background.calls) == 1
    runner_args, runner_kwargs = background.calls[0]
    assert runner_args[:4] == (
        tasks._run_batch_account_action,
        response["task_id"],
        [1, 2],
        "upload_cpa",
    )
    assert runner_args[4] == request.params
    assert runner_kwargs == {}
    assert account_action_task_store.snapshot(response["task_id"])["status"] == "pending"


def test_dangerous_selected_action_requires_confirmation_for_the_frozen_requested_total(
    account_action_engine,
    account_action_task_store,
):
    base = {
        "action_id": "logout_web_session",
        "scope": "selected",
        "account_ids": [1, 999],
    }

    with pytest.raises(HTTPException) as missing_confirmation:
        tasks.enqueue_batch_account_action_task(
            tasks.BatchAccountActionTaskRequest(**base),
            background_tasks=RecordingBackgroundTasks(),
        )
    assert missing_confirmation.value.status_code == 400
    assert "明确确认" in str(missing_confirmation.value.detail)

    with pytest.raises(HTTPException) as stale_confirmation:
        tasks.enqueue_batch_account_action_task(
            tasks.BatchAccountActionTaskRequest(
                **base,
                confirmed_total=1,
                params={"confirm_logout": True},
            ),
            background_tasks=RecordingBackgroundTasks(),
        )
    assert stale_confirmation.value.status_code == 409
    assert stale_confirmation.value.detail == {
        "code": "ACCOUNT_ACTION_CONFIRMATION_SCOPE_CHANGED",
        "message": stale_confirmation.value.detail["message"],
        "confirmed_total": 1,
        "total_requested": 2,
    }

    background = RecordingBackgroundTasks()
    response = tasks.enqueue_batch_account_action_task(
        tasks.BatchAccountActionTaskRequest(
            **base,
            confirmed_total=2,
            params={"confirm_logout": True},
        ),
        background_tasks=background,
    )
    assert response["total_requested"] == 2
    assert response["eligible"] == 1
    assert response["missing"] == 1
    assert response["task_snapshot"]["meta"]["params"] == {
        "custom_api_url": False,
        "custom_api_key": False,
        "confirmation_acknowledged": True,
    }
    assert len(background.calls) == 1


def test_dangerous_filtered_action_compares_confirmed_total_with_verified_matched_total(
    account_action_engine,
    account_action_task_store,
):
    request = tasks.BatchAccountActionTaskRequest(
        action_id="logout_and_revoke_tokens",
        scope="filtered",
        all_filtered=True,
        subscription_type="plus",
        expected_total=2,
        confirmed_total=2,
        params={"confirm_revoke_all": True},
    )

    response = tasks.enqueue_batch_account_action_task(
        request,
        background_tasks=RecordingBackgroundTasks(),
    )

    assert response["total_requested"] == 2
    assert response["matched"] == 2
    assert response["eligible"] == 2
    assert response["scope"] == "filtered"


def test_empty_filtered_action_still_returns_a_queryable_terminal_task(
    account_action_engine,
    account_action_task_store,
):
    response = tasks.enqueue_batch_account_action_task(
        tasks.BatchAccountActionTaskRequest(
            action_id="refresh_token",
            scope="filtered",
            all_filtered=True,
            email="does-not-exist@example.com",
            expected_total=0,
        ),
        background_tasks=RecordingBackgroundTasks(),
    )

    assert response["task_id"]
    assert response["eligible"] == 0
    assert response["total_requested"] == 0
    snapshot = response["task_snapshot"]
    assert snapshot["id"] == response["task_id"]
    assert snapshot["status"] == "done"
    assert snapshot["progress"] == "0/0"
    assert snapshot["meta"]["action_id"] == "refresh_token"


def test_probe_specialized_dispatch_keeps_its_existing_five_thousand_account_limit(
    account_action_task_store,
):
    background = RecordingBackgroundTasks()
    requested_ids = list(range(1, tasks.LOCAL_STATUS_PROBE_MAX_ACCOUNTS + 1))
    captured = {}

    def fake_enqueue(request, *, background_tasks=None):
        captured["request"] = request
        captured["background_tasks"] = background_tasks
        task_id = "task_probe_specialized"
        tasks._create_standalone_task_record(
            task_id,
            platform="chatgpt",
            source="batch_probe_local_status",
            total=len(requested_ids),
            meta={"params": {"proxy_mode": "direct"}},
        )
        return {
            "task_id": task_id,
            "total_requested": len(requested_ids),
            "matched": 0,
            "eligible": len(requested_ids),
            "skipped": 0,
            "missing": 0,
            "items": [],
            "skipped_items": [],
            "missing_ids": [],
        }

    with mock.patch(
        "api.tasks.enqueue_batch_probe_local_status_task",
        side_effect=fake_enqueue,
    ):
        response = tasks.enqueue_batch_account_action_task(
            tasks.BatchAccountActionTaskRequest(
                action_id="probe_local_status",
                scope="selected",
                account_ids=requested_ids,
                params={"proxy_mode": "direct"},
            ),
            background_tasks=background,
        )

    specialized_request = captured["request"]
    assert isinstance(specialized_request, tasks.BatchProbeLocalStatusTaskRequest)
    assert specialized_request.account_ids == requested_ids
    assert specialized_request.params == {"proxy_mode": "direct"}
    assert captured["background_tasks"] is background
    assert response["task_id"] == "task_probe_specialized"
    assert response["source"] == "batch_probe_local_status"
    assert response["action_id"] == "probe_local_status"
    assert response["action_label"] == "探测本地状态"
    assert response["scope"] == "selected"
    assert response["total_requested"] == tasks.LOCAL_STATUS_PROBE_MAX_ACCOUNTS
    assert response["task_snapshot"]["meta"]["account_action"] == {
        "action_id": "probe_local_status",
        "action_label": "探测本地状态",
        "scope": "selected",
    }


def test_empty_specialized_response_is_wrapped_in_a_queryable_terminal_task(
    account_action_task_store,
):
    with mock.patch(
        "api.tasks.enqueue_batch_probe_local_status_task",
        return_value={
            "task_id": "",
            "total_requested": 1,
            "matched": 0,
            "eligible": 0,
            "skipped": 0,
            "missing": 1,
            "items": [],
            "skipped_items": [],
            "missing_ids": [999],
        },
    ):
        response = tasks.enqueue_batch_account_action_task(
            tasks.BatchAccountActionTaskRequest(
                action_id="probe_local_status",
                scope="single",
                account_ids=[999],
            ),
            background_tasks=RecordingBackgroundTasks(),
        )

    assert response["task_id"]
    assert response["eligible"] == 0
    assert response["missing"] == 1
    snapshot = response["task_snapshot"]
    assert snapshot["status"] == "failed"
    assert snapshot["progress"] == "0/0"
    assert snapshot["meta"]["action_id"] == "probe_local_status"
    assert snapshot["meta"]["scope"] == "single"


@pytest.mark.parametrize(
    ("request_type", "resolver"),
    [
        (tasks.BatchSub2ApiUploadTaskRequest, tasks._resolve_batch_sub2api_upload_accounts),
        (tasks.BatchOaipayUploadTaskRequest, tasks._resolve_batch_oaipay_upload_accounts),
    ],
)
def test_specialized_selected_limit_records_overflow_as_skipped(
    account_action_engine,
    request_type,
    resolver,
):
    eligible, missing, skipped, matched = resolver(
        request_type(account_ids=[2, 1], limit=1),
    )

    assert [item["account_id"] for item in eligible] == [2]
    assert missing == []
    assert [item["account_id"] for item in matched] == [2, 1]
    assert skipped == [
        {
            "account_id": 1,
            "email": "first@example.com",
            "status": "registered",
            "reason": "超过本次限制 limit=1",
        }
    ]


def test_probe_task_ids_remain_unique_within_the_same_millisecond(
    account_action_engine,
    account_action_task_store,
    monkeypatch,
):
    monkeypatch.setattr(tasks.time, "time", lambda: 1234.567)
    request = tasks.BatchProbeLocalStatusTaskRequest(account_ids=[1], params={})

    first = tasks.enqueue_batch_probe_local_status_task(
        request,
        background_tasks=RecordingBackgroundTasks(),
    )
    second = tasks.enqueue_batch_probe_local_status_task(
        request,
        background_tasks=RecordingBackgroundTasks(),
    )

    assert first["task_id"] != second["task_id"]
    assert first["task_id"].startswith("task_1234567_")
    assert second["task_id"].startswith("task_1234567_")
    assert len(first["task_id"].rsplit("_", 1)[-1]) == 8
    assert len(second["task_id"].rsplit("_", 1)[-1]) == 8


def test_sub2api_specialized_dispatch_keeps_runtime_credentials_out_of_task_metadata(
    account_action_engine,
    account_action_task_store,
):
    background = RecordingBackgroundTasks()
    eligible = [
        {
            "account_id": 2,
            "email": "second@example.com",
            "status": "registered",
        }
    ]
    params = {
        "api_url": "https://sub2api.example.test/?token=url-secret",
        "api_key": "temporary-secret",
        "group_ids": [7, 9],
    }

    with mock.patch(
        "api.tasks._resolve_batch_sub2api_upload_accounts",
        return_value=(eligible, [], [], []),
    ):
        response = tasks.enqueue_batch_account_action_task(
            tasks.BatchAccountActionTaskRequest(
                action_id="upload_sub2api",
                scope="single",
                account_ids=[2],
                params=params,
            ),
            background_tasks=background,
        )

    assert response["source"] == "batch_sub2api_upload"
    assert response["action_id"] == "upload_sub2api"
    assert response["action_label"] == "上传 Sub2API"
    assert response["scope"] == "single"
    snapshot = response["task_snapshot"]
    assert snapshot["meta"]["params"] == {
        "custom_api_url": True,
        "custom_api_key": True,
    }
    serialized_response = json.dumps(response, ensure_ascii=False)
    assert "url-secret" not in serialized_response
    assert "temporary-secret" not in serialized_response

    assert len(background.calls) == 1
    runner_args, runner_kwargs = background.calls[0]
    assert runner_args == (
        tasks._run_batch_sub2api_upload,
        response["task_id"],
        [2],
        params,
    )
    assert runner_kwargs == {}


def test_oaipay_specialized_dispatch_freezes_category_strategy_and_action_metadata(
    account_action_task_store,
):
    background = RecordingBackgroundTasks()
    captured = {}

    def fake_enqueue(request, *, background_tasks=None):
        captured["request"] = request
        captured["background_tasks"] = background_tasks
        task_id = "task_oaipay_specialized"
        tasks._create_standalone_task_record(
            task_id,
            platform="chatgpt",
            source="batch_oaipay_upload",
            total=1,
            meta={
                "eligible": 1,
                "total_requested": 1,
                "category_mode": request.category_mode,
                "category_id": request.category_id,
                "fallback_category_id": request.fallback_category_id,
            },
        )
        return {
            "task_id": task_id,
            "total_requested": 1,
            "matched": 0,
            "eligible": 1,
            "skipped": 0,
            "missing": 0,
            "items": [],
            "skipped_items": [],
            "missing_ids": [],
        }

    with mock.patch(
        "api.tasks.enqueue_batch_oaipay_upload_task",
        side_effect=fake_enqueue,
    ):
        response = tasks.enqueue_batch_account_action_task(
            tasks.BatchAccountActionTaskRequest(
                action_id="upload_oaipay",
                scope="single",
                account_ids=[1],
                params={
                    "category_mode": "manual",
                    "category_id": "12",
                    "fallback_category_id": "34",
                },
            ),
            background_tasks=background,
        )

    specialized_request = captured["request"]
    assert isinstance(specialized_request, tasks.BatchOaipayUploadTaskRequest)
    assert specialized_request.category_mode == "manual"
    assert specialized_request.category_id == 12
    assert specialized_request.fallback_category_id == 34
    assert specialized_request.params == {}
    assert captured["background_tasks"] is background
    assert response["source"] == "batch_oaipay_upload"
    assert response["action_id"] == "upload_oaipay"
    assert response["action_label"] == "上传 OAIPay"
    assert response["task_snapshot"]["meta"]["account_action"]["scope"] == "single"


def _create_generic_runner_task(store, *, task_id, account_ids, action_id, action_label):
    store.create(
        task_id,
        platform="chatgpt",
        total=max(len(account_ids), 1),
        source=tasks.ACCOUNT_ACTION_SOURCE,
        supports_after_current=len(account_ids) > 1,
        meta={
            "action_id": action_id,
            "action_label": action_label,
            "scope": "selected" if len(account_ids) > 1 else "single",
            "account_ids": list(account_ids),
            "emails": [f"account-{account_id}@example.com" for account_id in account_ids],
            "missing_ids": [],
            "skipped_items": [],
            "params": {},
            "results": [],
        },
    )


def _platform_account(row):
    return SimpleNamespace(
        id=int(row.id or 0),
        email=str(row.email or ""),
        token=str(row.token or ""),
        user_id=str(row.user_id or ""),
        extra=row.get_extra(),
    )


def test_generic_runner_uses_short_sessions_and_continues_after_one_account_fails(
    account_action_engine,
    account_action_task_store,
    monkeypatch,
):
    task_id = "task_account_action_failure_isolation"
    _create_generic_runner_task(
        account_action_task_store,
        task_id=task_id,
        account_ids=[1, 2],
        action_id="upload_cpa",
        action_label="上传 CPA",
    )
    executed = []
    opened_sessions = []
    real_session = Session

    def recording_session(engine):
        session = real_session(engine)
        opened_sessions.append(session)
        return session

    def execute_action(_action_id, account, _params):
        executed.append(account.id)
        if account.id == 1:
            raise RuntimeError("first account failed")
        return {"ok": True, "data": {"message": "uploaded"}}

    fake_platform = SimpleNamespace(execute_action=execute_action)
    monkeypatch.setattr(tasks, "Session", recording_session)
    monkeypatch.setattr(
        "services.chatgpt_core.ChatGPTPlatform",
        lambda config: fake_platform,
    )
    monkeypatch.setattr("api.actions._to_platform_account", _platform_account)
    monkeypatch.setattr("api.actions._apply_action_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "api.actions._action_local_status_refresh_ids",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        tasks,
        "schedule_chatgpt_local_status_refresh_for_account_id",
        lambda *args, **kwargs: None,
    )

    tasks._run_batch_account_action(
        task_id,
        [1, 2],
        "upload_cpa",
        {"api_key": "runtime-only-secret"},
    )

    snapshot = account_action_task_store.snapshot(task_id)
    assert executed == [1, 2]
    assert snapshot["status"] == "done"
    assert snapshot["progress"] == "2/2"
    assert snapshot["success"] == 1
    assert len(snapshot["errors"]) == 1
    assert "first account failed" in snapshot["errors"][0]
    assert [item["status"] for item in snapshot["meta"]["results"]] == [
        "failed",
        "success",
    ]
    # Failed account: one read Session. Successful account: a separate read and
    # write Session. No request-wide Session survives the network action.
    assert len(opened_sessions) == 3
    assert len({id(session) for session in opened_sessions}) == 3
    assert "runtime-only-secret" not in json.dumps(snapshot, ensure_ascii=False)


def test_generic_runner_stops_before_starting_the_next_account_after_current(
    account_action_engine,
    account_action_task_store,
    monkeypatch,
):
    task_id = "task_account_action_stop_after_current"
    _create_generic_runner_task(
        account_action_task_store,
        task_id=task_id,
        account_ids=[1, 2],
        action_id="upload_cpa",
        action_label="上传 CPA",
    )
    executed = []

    def execute_action(_action_id, account, _params):
        executed.append(account.id)
        account_action_task_store.request_stop_after_current(task_id)
        return {"ok": True, "data": {"message": "uploaded"}}

    monkeypatch.setattr(
        "services.chatgpt_core.ChatGPTPlatform",
        lambda config: SimpleNamespace(execute_action=execute_action),
    )
    monkeypatch.setattr("api.actions._to_platform_account", _platform_account)
    monkeypatch.setattr("api.actions._apply_action_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "api.actions._action_local_status_refresh_ids",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        tasks,
        "schedule_chatgpt_local_status_refresh_for_account_id",
        lambda *args, **kwargs: None,
    )

    tasks._run_batch_account_action(task_id, [1, 2], "upload_cpa", {})

    snapshot = account_action_task_store.snapshot(task_id)
    assert executed == [1]
    assert snapshot["status"] == "stopped"
    assert snapshot["progress"] == "1/2"
    assert snapshot["success"] == 1
    assert snapshot["control"]["stop_after_current_requested"] is True


def test_generic_single_runner_persists_remote_result_before_immediate_stop(
    account_action_engine,
    account_action_task_store,
    monkeypatch,
):
    task_id = "task_account_action_immediate_stop_midflight"
    _create_generic_runner_task(
        account_action_task_store,
        task_id=task_id,
        account_ids=[1],
        action_id="upload_cpa",
        action_label="上传 CPA",
    )
    apply_result = mock.Mock()

    def execute_action(_action_id, _account, _params):
        account_action_task_store.request_stop(task_id)
        return {"ok": True, "data": {"message": "uploaded"}}

    monkeypatch.setattr(
        "services.chatgpt_core.ChatGPTPlatform",
        lambda config: SimpleNamespace(execute_action=execute_action),
    )
    monkeypatch.setattr("api.actions._to_platform_account", _platform_account)
    monkeypatch.setattr("api.actions._apply_action_result", apply_result)
    monkeypatch.setattr(
        "api.actions._action_local_status_refresh_ids",
        lambda *args, **kwargs: [],
    )

    tasks._run_batch_account_action(task_id, [1], "upload_cpa", {})

    snapshot = account_action_task_store.snapshot(task_id)
    apply_result.assert_called_once()
    assert snapshot["status"] == "stopped"
    assert snapshot["progress"] == "1/1"
    assert snapshot["success"] == 1
    assert snapshot["meta"]["results"] == [
        {
            "account_id": 1,
            "email": "first@example.com",
            "status": "success",
            "message": "uploaded",
        }
    ]


def test_generic_single_runner_atomically_honors_stop_accepted_during_finalization(
    account_action_engine,
    account_action_task_store,
    monkeypatch,
):
    task_id = "task_account_action_immediate_stop_finalization"
    _create_generic_runner_task(
        account_action_task_store,
        task_id=task_id,
        account_ids=[1],
        action_id="upload_cpa",
        action_label="上传 CPA",
    )

    monkeypatch.setattr(
        "services.chatgpt_core.ChatGPTPlatform",
        lambda config: SimpleNamespace(
            execute_action=lambda *_args, **_kwargs: {
                "ok": True,
                "data": {"message": "uploaded"},
            }
        ),
    )
    monkeypatch.setattr("api.actions._to_platform_account", _platform_account)
    monkeypatch.setattr("api.actions._apply_action_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "api.actions._action_local_status_refresh_ids",
        lambda *args, **kwargs: [],
    )

    def request_stop_during_terminal_log(_platform, _email, status, **_kwargs):
        if status == "success":
            account_action_task_store.request_stop(task_id)

    monkeypatch.setattr(tasks, "_save_task_log", request_stop_during_terminal_log)

    tasks._run_batch_account_action(task_id, [1], "upload_cpa", {})

    snapshot = account_action_task_store.snapshot(task_id)
    assert snapshot["status"] == "stopped"
    assert snapshot["progress"] == "1/1"
    assert snapshot["success"] == 1
    assert snapshot["control"]["stop_requested"] is True


def test_cliproxy_runner_fetches_auth_file_directory_once_for_the_whole_task(
    account_action_engine,
    account_action_task_store,
    monkeypatch,
):
    task_id = "task_cliproxy_single_directory_fetch"
    _create_generic_runner_task(
        account_action_task_store,
        task_id=task_id,
        account_ids=[1, 2],
        action_id="sync_cliproxyapi_status",
        action_label="同步 CLIProxyAPI 状态",
    )
    auth_files = [{"name": "first@example.com.json", "provider": "codex"}]
    fetch_files = mock.Mock(return_value=auth_files)
    sync_from_files = mock.Mock(
        side_effect=lambda account, files, **_kwargs: {
            "uploaded": True,
            "remote_state": "usable",
            "status": "active",
            "account_id": account.id,
            "directory_identity": id(files),
        }
    )
    apply_result = mock.Mock()

    monkeypatch.setattr(
        "services.chatgpt_core.ChatGPTPlatform",
        lambda config: SimpleNamespace(
            execute_action=lambda *_args, **_kwargs: pytest.fail(
                "CLIProxy task must use the shared auth-file snapshot"
            )
        ),
    )
    monkeypatch.setattr(
        "services.cliproxyapi_sync.fetch_cliproxyapi_auth_files",
        fetch_files,
    )
    monkeypatch.setattr(
        "services.cliproxyapi_sync.sync_chatgpt_cliproxyapi_status_from_files",
        sync_from_files,
    )
    monkeypatch.setattr("api.actions._to_platform_account", _platform_account)
    monkeypatch.setattr("api.actions._apply_action_result", apply_result)
    monkeypatch.setattr(
        "api.actions._action_local_status_refresh_ids",
        lambda *args, **kwargs: [],
    )

    tasks._run_batch_account_action(
        task_id,
        [1, 2],
        "sync_cliproxyapi_status",
        {
            "api_url": " https://cliproxy.example.test/ ",
            "api_key": " temporary-secret ",
        },
    )

    fetch_files.assert_called_once_with(
        api_url="https://cliproxy.example.test/",
        api_key="temporary-secret",
    )
    assert sync_from_files.call_count == 2
    for call in sync_from_files.call_args_list:
        assert call.args[1] is auth_files
        assert call.kwargs == {
            "api_url": "https://cliproxy.example.test/",
            "api_key": "temporary-secret",
            "fetch_error": "",
        }
    assert apply_result.call_count == 2
    snapshot = account_action_task_store.snapshot(task_id)
    assert snapshot["status"] == "done"
    assert snapshot["success"] == 2
    assert "temporary-secret" not in json.dumps(snapshot, ensure_ascii=False)


def test_after_current_interrupts_long_inter_account_delay_before_second_claim(
    account_action_engine,
    account_action_task_store,
    monkeypatch,
):
    task_id = "task_account_action_stop_during_delay"
    _create_generic_runner_task(
        account_action_task_store,
        task_id=task_id,
        account_ids=[1, 2],
        action_id="upload_cpa",
        action_label="上传 CPA",
    )
    executed = []
    sleep_slices = []

    def execute_action(_action_id, account, _params):
        executed.append(account.id)
        return {"ok": True, "data": {"message": "uploaded"}}

    def request_stop_during_first_sleep(seconds):
        sleep_slices.append(seconds)
        account_action_task_store.request_stop_after_current(task_id)

    monkeypatch.setattr(
        "services.chatgpt_core.ChatGPTPlatform",
        lambda config: SimpleNamespace(execute_action=execute_action),
    )
    monkeypatch.setattr("api.actions._to_platform_account", _platform_account)
    monkeypatch.setattr("api.actions._apply_action_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "api.actions._action_local_status_refresh_ids",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        tasks,
        "schedule_chatgpt_local_status_refresh_for_account_id",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(tasks.time, "sleep", request_stop_during_first_sleep)

    tasks._run_batch_account_action(
        task_id,
        [1, 2],
        "upload_cpa",
        {"delay_seconds": 120, "delay_max_seconds": 120},
    )

    snapshot = account_action_task_store.snapshot(task_id)
    assert executed == [1]
    assert sleep_slices == [0.5]
    assert snapshot["status"] == "stopped"
    assert snapshot["progress"] == "1/2"
    assert snapshot["success"] == 1
    assert snapshot["control"]["active_attempts"] == 0


def test_auth_mutation_runner_rejects_stale_result_when_credentials_change_midflight(
    account_action_engine,
    account_action_task_store,
    monkeypatch,
):
    task_id = "task_account_action_auth_revision"
    _create_generic_runner_task(
        account_action_task_store,
        task_id=task_id,
        account_ids=[1],
        action_id="refresh_token",
        action_label="刷新 Token",
    )

    def execute_action(_action_id, _account, _params):
        with Session(account_action_engine) as session:
            current = session.get(AccountModel, 1)
            current.token = "at-newer"
            extra = current.get_extra()
            extra["access_token"] = "at-newer"
            current.set_extra(extra)
            session.add(current)
            session.commit()
        return {
            "ok": True,
            "data": {"message": "old refresh completed"},
            "account_extra_patch": {"access_token": "at-stale"},
        }

    @contextmanager
    def no_op_identity_slot(*_args, **_kwargs):
        yield

    apply_result = mock.Mock()
    monkeypatch.setattr(
        "services.chatgpt_core.ChatGPTPlatform",
        lambda config: SimpleNamespace(execute_action=execute_action),
    )
    monkeypatch.setattr("api.actions._to_platform_account", _platform_account)
    monkeypatch.setattr("api.actions._apply_action_result", apply_result)
    monkeypatch.setattr(
        "api.actions._action_local_status_refresh_ids",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(tasks, "local_status_identity_slot", no_op_identity_slot)

    tasks._run_batch_account_action(task_id, [1], "refresh_token", {})

    snapshot = account_action_task_store.snapshot(task_id)
    assert snapshot["status"] == "failed"
    assert snapshot["success"] == 0
    assert len(snapshot["errors"]) == 1
    assert "认证材料在执行期间已变化" in snapshot["errors"][0]
    apply_result.assert_not_called()
    with Session(account_action_engine) as session:
        current = session.get(AccountModel, 1)
        assert current.token == "at-newer"
        assert current.get_extra()["access_token"] == "at-newer"


def test_sub2api_single_runner_persists_remote_result_before_immediate_stop(
    account_action_engine,
    account_action_task_store,
    monkeypatch,
):
    task_id = "task_sub2api_immediate_stop_midflight"
    account_action_task_store.create(
        task_id,
        platform="chatgpt",
        total=1,
        source="batch_sub2api_upload",
        meta={"account_ids": [1], "missing_ids": [], "skipped_items": []},
    )
    updater = mock.Mock()

    def backfill(_account, **_kwargs):
        account_action_task_store.request_stop(task_id)
        return {
            "ok": True,
            "uploaded": True,
            "message": "uploaded",
            "sync": {"remote_state": "exists", "status": "active"},
        }

    monkeypatch.setattr(
        "services.sub2api_sync.backfill_chatgpt_account_to_sub2api",
        backfill,
    )
    monkeypatch.setattr(
        "services.sub2api_sync.update_account_model_sub2api_sync",
        updater,
    )

    tasks._run_batch_sub2api_upload(task_id, [1], {})

    snapshot = account_action_task_store.snapshot(task_id)
    updater.assert_called_once()
    assert snapshot["status"] == "stopped"
    assert snapshot["progress"] == "1/1"
    assert snapshot["success"] == 1


def test_oaipay_single_runner_persists_remote_result_before_immediate_stop(
    account_action_engine,
    account_action_task_store,
    monkeypatch,
):
    task_id = "task_oaipay_immediate_stop_midflight"
    account_action_task_store.create(
        task_id,
        platform="chatgpt",
        total=1,
        source="batch_oaipay_upload",
        meta={"account_ids": [1], "missing_ids": [], "skipped_items": []},
    )
    updater = mock.Mock()

    def backfill(_account, **_kwargs):
        account_action_task_store.request_stop(task_id)
        return {
            "ok": True,
            "uploaded": True,
            "message": "uploaded",
            "category_id": 7,
            "category_name": "Active",
            "category_source": "auto",
            "sync": {"remote_state": "exists", "status": "active"},
        }

    monkeypatch.setattr(
        "services.oaipay_sync.backfill_chatgpt_account_to_oaipay",
        backfill,
    )
    monkeypatch.setattr(
        "services.oaipay_sync.update_account_model_oaipay_sync",
        updater,
    )

    tasks._run_batch_oaipay_upload(task_id, [1])

    snapshot = account_action_task_store.snapshot(task_id)
    updater.assert_called_once()
    assert snapshot["status"] == "stopped"
    assert snapshot["progress"] == "1/1"
    assert snapshot["success"] == 1


def test_sub2api_sync_service_receives_temporary_connection_parameters():
    from services import sub2api_sync

    account = AccountModel(
        id=1,
        platform="chatgpt",
        email="upload@example.com",
        password="password",
        token="at-upload",
        status="registered",
    )
    account.set_extra({"access_token": "at-upload", "refresh_token": "rt-upload"})
    upload_result = {
        "ok": True,
        "message": "uploaded",
        "remote_account_id": "remote-1",
        "status": "active",
    }

    with mock.patch(
        "services.sub2api_sync.get_sub2api_sync_state",
        return_value={"remote_state": "not_found"},
    ), mock.patch(
        "services.sub2api_sync.is_chatgpt_upload_ready",
        return_value=(True, "", {"upload_gate": "ready"}),
    ), mock.patch(
        "services.sub2api_sync.build_chatgpt_sync_account",
        return_value={"email": account.email},
    ), mock.patch(
        "services.sub2api_sync.upload_to_sub2api_detailed",
        return_value=upload_result,
    ) as upload, mock.patch(
        "services.sub2api_sync.update_account_model_sub2api_sync",
    ):
        result = sub2api_sync.backfill_chatgpt_account_to_sub2api(
            account,
            commit=False,
            api_url="https://sub2api.example.test/",
            api_key="temporary-secret",
            group_ids=[7, 9],
        )

    upload.assert_called_once_with(
        {"email": account.email},
        api_url="https://sub2api.example.test/",
        api_key="temporary-secret",
        group_ids=[7, 9],
    )
    assert result["ok"] is True
