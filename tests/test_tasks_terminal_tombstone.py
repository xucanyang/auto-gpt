import json

import pytest
from fastapi import HTTPException

from api import tasks
from core.task_runtime import RegisterTaskStore


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
