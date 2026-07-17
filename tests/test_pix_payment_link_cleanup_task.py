from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from api import tasks
from core.db import TaskLog
from core.task_runtime import RegisterTaskStore


PIX_CLEANUP_SOURCE = "pix_payment_link_cleanup"


@dataclass
class RecordingBackgroundTasks:
    calls: list[tuple[Callable[..., Any], tuple[Any, ...], dict[str, Any]]] = field(default_factory=list)

    def add_task(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        self.calls.append((func, args, kwargs))

    def run_one(self) -> None:
        assert len(self.calls) == 1
        func, args, kwargs = self.calls[0]
        func(*args, **kwargs)


@pytest.fixture
def cleanup_task_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'pix_cleanup_tasks.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)

    store = RegisterTaskStore()
    monkeypatch.setattr(tasks, "engine", engine)
    monkeypatch.setattr(tasks, "_task_store", store)
    # A replacement task store does not inherit api.tasks' module-level
    # terminal callback. Bind it explicitly so these tests also cover the
    # durable TaskLog snapshot, not only the in-memory runner state.
    store.set_terminal_callback(tasks._persist_terminal_task_snapshot)
    return store, engine


def _enqueue(background_tasks: RecordingBackgroundTasks, cleanup_mode: str = "expired") -> str:
    response = tasks.enqueue_expired_pix_payment_link_cleanup_task(
        background_tasks=background_tasks,
        cleanup_mode=cleanup_mode,
    )
    task_id = str(response.get("task_id") or "").strip()
    assert task_id
    return task_id


def _persisted_task_log(engine, task_id: str) -> tuple[TaskLog, dict[str, Any]]:
    with Session(engine) as session:
        row = session.exec(select(TaskLog).where(TaskLog.task_id == task_id)).one()
        detail = json.loads(row.detail_json)
    return row, detail


def _success_report() -> dict[str, Any]:
    # One missing-expiry link is intentionally present. It must be mentioned
    # in the detail logs and counted as retained in the three-part summary.
    return {
        "instance_id": "auto-gpt-plus",
        "timezone": "Asia/Shanghai",
        "cutoff_display": "2026-07-16 11:00",
        "current_pix_links": 6,
        "cleanup_mode": "expired",
        "cleanup_label": "过期",
        "active_links": 2,
        "expired_links": 3,
        "paid_links": 0,
        "cancelled_links": 0,
        "eligible_links": 3,
        "retained_links": 3,
        "missing_expiry_links": 1,
        "provider_expiry_links": 2,
        "derived_expiry_links": 3,
        "cleaned_links": 3,
        "concurrent_skipped_links": 0,
        "list_state_refreshed": 3,
        "backup_created": True,
    }


def test_pix_cleanup_task_route_is_a_dedicated_post_endpoint():
    matches = [
        route
        for route in tasks.router.routes
        if getattr(route, "path", "") == "/tasks/chatgpt/payment-links/pix-cleanup/task"
    ]

    assert len(matches) == 1
    assert "POST" in (getattr(matches[0], "methods", set()) or set())


def test_enqueue_pix_cleanup_returns_immediately_and_creates_task(
    cleanup_task_runtime,
    monkeypatch: pytest.MonkeyPatch,
):
    store, engine = cleanup_task_runtime
    background_tasks = RecordingBackgroundTasks()

    def must_not_clean_in_request(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("cleanup ran in the request instead of the queued runner")

    monkeypatch.setattr(tasks, "clean_pix_payment_links", must_not_clean_in_request)

    task_id = _enqueue(background_tasks)

    assert len(background_tasks.calls) == 1
    runner, args, kwargs = background_tasks.calls[0]
    assert runner is tasks._run_expired_pix_payment_link_cleanup
    assert args == (task_id, "expired")
    assert kwargs == {}

    snapshot = store.snapshot(task_id)
    assert snapshot["source"] == PIX_CLEANUP_SOURCE
    assert snapshot["status"] in {"pending", "running"}
    assert snapshot["capabilities"]["stop_after_current"] is False
    assert snapshot["capabilities"]["stop_modes"] == ["immediate"]

    row, detail = _persisted_task_log(engine, task_id)
    assert row.status == "running"
    assert detail["source"] == PIX_CLEANUP_SOURCE
    assert detail["attempt_outcome"] == "task_created"


def test_enqueue_pix_cleanup_reuses_the_active_task_without_queueing_twice(
    cleanup_task_runtime,
):
    store, engine = cleanup_task_runtime
    first_background_tasks = RecordingBackgroundTasks()
    second_background_tasks = RecordingBackgroundTasks()

    first = tasks.enqueue_expired_pix_payment_link_cleanup_task(
        background_tasks=first_background_tasks,
    )
    second = tasks.enqueue_expired_pix_payment_link_cleanup_task(
        background_tasks=second_background_tasks,
    )

    assert first["task_id"]
    assert second["task_id"] == first["task_id"]
    assert first["reused"] is False
    assert second["reused"] is True
    assert second["already_running"] is True
    assert len(first_background_tasks.calls) == 1
    assert second_background_tasks.calls == []
    assert len(store.list_snapshots()) == 1

    with Session(engine) as session:
        rows = session.exec(select(TaskLog)).all()
    assert len(rows) == 1


def test_enqueue_pix_cleanup_freezes_requested_terminal_mode(cleanup_task_runtime):
    store, _engine = cleanup_task_runtime
    background_tasks = RecordingBackgroundTasks()

    response = tasks.enqueue_expired_pix_payment_link_cleanup_task(
        background_tasks=background_tasks,
        cleanup_mode="paid",
    )

    task_id = str(response["task_id"])
    assert response["cleanup_mode"] == "paid"
    assert response["requested_cleanup_mode"] == "paid"
    assert len(background_tasks.calls) == 1
    runner, args, kwargs = background_tasks.calls[0]
    assert runner is tasks._run_expired_pix_payment_link_cleanup
    assert args == (task_id, "paid")
    assert kwargs == {}
    snapshot = store.snapshot(task_id)
    assert snapshot["meta"]["cleanup_mode"] == "paid"
    assert snapshot["meta"]["cleanup_label"] == "已支付"
    assert snapshot["meta"]["operation"] == "paid_pix_payment_link_cleanup"


def test_pix_cleanup_runner_persists_success_and_keeps_summary_last(
    cleanup_task_runtime,
    monkeypatch: pytest.MonkeyPatch,
):
    store, engine = cleanup_task_runtime
    background_tasks = RecordingBackgroundTasks()
    task_id = _enqueue(background_tasks)
    report = _success_report()

    monkeypatch.setattr(
        tasks,
        "preview_pix_payment_link_cleanup",
        lambda _session, *, cleanup_mode: {
            key: value
            for key, value in report.items()
            if key
            not in {
                "cleaned_links",
                "concurrent_skipped_links",
                "list_state_refreshed",
                "backup_created",
            }
        },
    )
    monkeypatch.setattr(
        tasks,
        "clean_pix_payment_links",
        lambda _session, *, cleanup_mode: dict(report),
    )

    background_tasks.run_one()

    snapshot = store.snapshot(task_id)
    assert snapshot["status"] == "done"
    assert snapshot["logs"][-1].endswith("[SUMMARY] 总：6；保留：3；过期：3")
    assert any("缺少时间" in line and "1" in line for line in snapshot["logs"][:-1])
    assert snapshot["meta"]["cleanup_result"]["current_pix_links"] == 6
    assert snapshot["meta"]["cleanup_result"]["active_links"] == 2
    assert snapshot["meta"]["cleanup_result"]["expired_links"] == 3
    assert snapshot["meta"]["cleanup_result"]["missing_expiry_links"] == 1

    row, detail = _persisted_task_log(engine, task_id)
    assert row.status == "done"
    assert detail["source"] == PIX_CLEANUP_SOURCE
    assert detail["logs"][-1].endswith("[SUMMARY] 总：6；保留：3；过期：3")
    assert detail["meta"]["cleanup_result"]["missing_expiry_links"] == 1


def test_pix_cleanup_runner_zero_expired_is_success_with_summary(
    cleanup_task_runtime,
    monkeypatch: pytest.MonkeyPatch,
):
    store, engine = cleanup_task_runtime
    background_tasks = RecordingBackgroundTasks()
    task_id = _enqueue(background_tasks)
    report = {
        "instance_id": "auto-plus2",
        "timezone": "Asia/Shanghai",
        "cutoff_display": "2026-07-16 11:00",
        "current_pix_links": 3,
        "cleanup_mode": "expired",
        "cleanup_label": "过期",
        "active_links": 2,
        "expired_links": 0,
        "paid_links": 0,
        "cancelled_links": 0,
        "eligible_links": 0,
        "retained_links": 3,
        "missing_expiry_links": 1,
        "provider_expiry_links": 1,
        "derived_expiry_links": 1,
        "cleaned_links": 0,
        "concurrent_skipped_links": 0,
        "list_state_refreshed": 0,
        "backup_created": False,
    }
    monkeypatch.setattr(tasks, "preview_pix_payment_link_cleanup", lambda _session, *, cleanup_mode: dict(report))
    monkeypatch.setattr(tasks, "clean_pix_payment_links", lambda _session, *, cleanup_mode: dict(report))

    background_tasks.run_one()

    snapshot = store.snapshot(task_id)
    assert snapshot["status"] == "done"
    assert snapshot["logs"][-1].endswith("[SUMMARY] 总：3；保留：3；过期：0")
    assert any("缺少时间" in line and "1" in line for line in snapshot["logs"][:-1])

    row, detail = _persisted_task_log(engine, task_id)
    assert row.status == "done"
    assert detail["logs"][-1].endswith("[SUMMARY] 总：3；保留：3；过期：0")


def test_pix_cleanup_runner_failure_is_terminal_and_fail_log_is_last(
    cleanup_task_runtime,
    monkeypatch: pytest.MonkeyPatch,
):
    store, engine = cleanup_task_runtime
    background_tasks = RecordingBackgroundTasks()
    task_id = _enqueue(background_tasks)
    preview = {
        key: value
        for key, value in _success_report().items()
        if key
        not in {
            "cleaned_links",
            "concurrent_skipped_links",
            "list_state_refreshed",
            "backup_created",
        }
    }
    monkeypatch.setattr(tasks, "preview_pix_payment_link_cleanup", lambda _session, *, cleanup_mode: dict(preview))

    def fail_cleanup(_session, *, cleanup_mode):
        raise RuntimeError("verified backup failed")

    monkeypatch.setattr(tasks, "clean_pix_payment_links", fail_cleanup)

    background_tasks.run_one()

    snapshot = store.snapshot(task_id)
    assert snapshot["status"] == "failed"
    assert "[FAIL]" in snapshot["logs"][-1]
    assert "verified backup failed" in snapshot["logs"][-1]
    assert snapshot["meta"]["cleanup_preview"]["missing_expiry_links"] == 1

    row, detail = _persisted_task_log(engine, task_id)
    assert row.status == "failed"
    assert "verified backup failed" in row.error
    assert "[FAIL]" in detail["logs"][-1]
    assert "verified backup failed" in detail["logs"][-1]
    assert detail["meta"]["cleanup_preview"]["missing_expiry_links"] == 1
