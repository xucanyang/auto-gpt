from __future__ import annotations

from copy import deepcopy

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, create_engine

from core.task_runtime import RegisterTaskStore


class _RotationManagerStub:
    def __init__(self) -> None:
        self.groups: dict[str, dict] = {}
        self.stop_calls: list[tuple[str, str]] = []

    def get_group(self, group_id: str):
        group = self.groups.get(group_id)
        return deepcopy(group) if group is not None else None

    def stop_group(self, group_id: str, *, mode: str = "after_current"):
        group = self.groups.get(group_id)
        if group is None:
            raise KeyError(group_id)
        self.stop_calls.append((group_id, mode))
        group["state"] = "stopping"
        return deepcopy(group)


@pytest.fixture()
def batch_stop_harness(monkeypatch):
    from api import tasks

    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(test_engine)
    store = RegisterTaskStore()
    rotation_manager = _RotationManagerStub()
    monkeypatch.setattr(tasks, "engine", test_engine)
    monkeypatch.setattr(tasks, "_task_store", store)
    monkeypatch.setattr(tasks, "_registration_domain_rotation_manager", rotation_manager)
    return tasks, store, rotation_manager


def _create_running_task(
    store: RegisterTaskStore,
    task_id: str,
    *,
    supports_after_current: bool = False,
    meta: dict | None = None,
) -> None:
    store.create(
        task_id,
        platform="chatgpt",
        total=3,
        source="unit",
        supports_after_current=supports_after_current,
        meta=meta,
    )
    store.mark_running(task_id)


def test_batch_stop_is_deduplicated_and_repeated_requests_are_idempotent(batch_stop_harness):
    tasks, store, _ = batch_stop_harness
    _create_running_task(store, "task-a")
    _create_running_task(store, "task-b")

    first = tasks.batch_stop_tasks(
        tasks.BatchStopTasksRequest(
            mode="immediate",
            task_ids=["task-a", " task-b ", "task-a", ""],
        )
    )

    assert first["requested_count"] == 2
    assert first["summary"] == {
        "accepted": 2,
        "already_requested": 0,
        "already_terminal": 0,
        "not_found": 0,
        "failed": 0,
    }
    assert store.snapshot("task-a")["control"]["stop_mode"] == "immediate"
    assert store.snapshot("task-b")["control"]["stop_mode"] == "immediate"

    repeated = tasks.batch_stop_tasks(
        tasks.BatchStopTasksRequest(mode="immediate", task_ids=["task-a", "task-b"])
    )
    assert repeated["summary"]["already_requested"] == 2
    assert repeated["summary"]["accepted"] == 0


def test_batch_stop_reports_terminal_missing_and_unsupported_targets_independently(batch_stop_harness):
    tasks, store, _ = batch_stop_harness
    _create_running_task(store, "terminal", supports_after_current=True)
    store.finish("terminal", status="done", success=3, skipped=0, errors=[])
    _create_running_task(store, "immediate-only")

    response = tasks.batch_stop_tasks(
        tasks.BatchStopTasksRequest(
            mode="after_current",
            task_ids=["terminal", "missing", "immediate-only"],
        )
    )

    assert response["summary"] == {
        "accepted": 0,
        "already_requested": 0,
        "already_terminal": 1,
        "not_found": 1,
        "failed": 1,
    }
    by_id = {item["target_id"]: item for item in response["results"]}
    assert by_id["terminal"]["status"] == "already_terminal"
    assert by_id["missing"]["status"] == "not_found"
    assert by_id["immediate-only"]["code"] == "STOP_MODE_NOT_SUPPORTED"
    assert store.snapshot("immediate-only")["control"]["stop_mode"] == ""


def test_rotating_child_requires_group_stop_and_group_request_cancels_replenishment(batch_stop_harness):
    tasks, store, rotation_manager = batch_stop_harness
    group_id = "register-group-1"
    _create_running_task(
        store,
        "rotation-child",
        supports_after_current=True,
        meta={
            "registration_domain_task_group": {
                "id": group_id,
                "mode": "rotating",
                "domain": "one.example",
            }
        },
    )
    rotation_manager.groups[group_id] = {
        "task_group_id": group_id,
        "mode": "rotating",
        "state": "running",
    }

    with pytest.raises(HTTPException) as single_stop:
        tasks.stop_task(
            "rotation-child",
            tasks.StopTaskRequest(mode="immediate"),
        )
    assert single_stop.value.status_code == 409
    assert single_stop.value.detail["code"] == "ROTATION_GROUP_REQUIRED"

    child_only = tasks.batch_stop_tasks(
        tasks.BatchStopTasksRequest(mode="immediate", task_ids=["rotation-child"])
    )
    assert child_only["results"][0]["status"] == "failed"
    assert child_only["results"][0]["code"] == "ROTATION_GROUP_REQUIRED"
    assert child_only["results"][0]["registration_domain_group_id"] == group_id
    assert store.snapshot("rotation-child")["control"]["stop_mode"] == ""

    grouped = tasks.batch_stop_tasks(
        tasks.BatchStopTasksRequest(
            mode="after_current",
            task_ids=["rotation-child"],
            registration_domain_group_ids=[group_id],
        )
    )
    assert rotation_manager.stop_calls == [(group_id, "after_current")]
    assert grouped["results"][0]["status"] == "accepted"
    assert grouped["results"][1]["status"] == "already_requested"
    assert grouped["results"][1]["code"] == "COVERED_BY_ROTATION_GROUP"


def test_batch_stop_rejects_empty_and_oversized_logical_requests(batch_stop_harness):
    tasks, _, _ = batch_stop_harness

    with pytest.raises(HTTPException, match="至少选择") as empty:
        tasks.batch_stop_tasks(tasks.BatchStopTasksRequest())
    assert empty.value.status_code == 400

    with pytest.raises(HTTPException, match="单次最多") as oversized:
        tasks.batch_stop_tasks(
            tasks.BatchStopTasksRequest(
                task_ids=[f"task-{index}" for index in range(51)],
                registration_domain_group_ids=[f"group-{index}" for index in range(50)],
            )
        )
    assert oversized.value.status_code == 400


def test_active_summary_exposes_stop_capabilities_for_bulk_controls(
    batch_stop_harness,
    monkeypatch,
):
    tasks, store, _ = batch_stop_harness
    _create_running_task(store, "graceful", supports_after_current=True)
    monkeypatch.setattr(
        tasks._legacy_empty_task_summary_poll_guard,
        "should_force_refresh",
        lambda *_args, **_kwargs: False,
    )
    request = Request({"type": "http", "method": "GET", "path": "/api/tasks/active-summary", "headers": []})

    response = tasks.list_active_task_summaries(request)

    assert response[0]["id"] == "graceful"
    assert response[0]["capabilities"] == {
        "stop_after_current": True,
        "stop_modes": ["immediate", "after_current"],
    }
