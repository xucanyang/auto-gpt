import json

import pytest
from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, create_engine

from api import tasks
from core.task_runtime import RegisterTaskStore


@pytest.fixture(autouse=True)
def isolated_task_history_engine(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(tasks, "engine", engine)


def test_expired_runtime_task_returns_cacheable_terminal_tombstone(monkeypatch):
    monkeypatch.setattr(tasks, "_task_store", RegisterTaskStore())

    response = tasks.get_task("task_restarted_123")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, max-age=300"
    payload = json.loads(response.body)
    assert payload["task_id"] == "task_restarted_123"
    assert payload["status"] == "stopped"
    assert payload["expired"] is True
    assert payload["control"]["stop_requested"] is True


def test_unknown_non_runtime_task_keeps_404_contract(monkeypatch):
    monkeypatch.setattr(tasks, "_task_store", RegisterTaskStore())

    with pytest.raises(HTTPException) as exc_info:
        tasks.get_task("not-a-runtime-task")

    assert exc_info.value.status_code == 404


def test_existing_terminal_task_is_cacheable(monkeypatch):
    store = RegisterTaskStore()
    store.create("task_finished_123", platform="chatgpt", total=1, source="unit")
    store.finish("task_finished_123", status="done", success=1, skipped=0, errors=[])
    monkeypatch.setattr(tasks, "_task_store", store)

    response = tasks.get_task("task_finished_123")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, max-age=300"
    assert json.loads(response.body)["status"] == "done"


def test_terminal_registration_task_stays_dynamic_during_payment_followup(monkeypatch):
    store = RegisterTaskStore()
    task_id = "task_payment_followup_123"
    store.create(task_id, platform="chatgpt", total=1, source="register")
    store.update_meta(
        task_id,
        {
            "registration_paypal_payment": {
                "enabled": True,
                "payment_enabled": True,
                "finished": True,
                "counts": {"submitted": 1},
            }
        },
    )
    store.finish(task_id, status="done", success=1, skipped=0, errors=[])
    monkeypatch.setattr(tasks, "_task_store", store)
    monkeypatch.setattr(
        tasks,
        "_registration_paypal_followup_summary_for_task",
        lambda _task_id: {
            "available": True,
            "total": 1,
            "active": 1,
            "processing": 1,
            "succeeded": 0,
            "failed": 0,
            "unknown": 0,
            "finished": False,
            "counts_by_state": {"payment_pending": 1},
        },
    )

    response = tasks.get_task(task_id)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = json.loads(response.body)
    assert payload["status"] == "done"
    assert payload["meta"]["registration_paypal_payment"]["followup"] == {
        "available": True,
        "total": 1,
        "active": 1,
        "processing": 1,
        "succeeded": 0,
        "failed": 0,
        "unknown": 0,
        "finished": False,
        "counts_by_state": {"payment_pending": 1},
    }
