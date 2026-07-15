from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from api import chatgpt as chatgpt_api
from core.task_runtime import RegisterTaskStore


def _batch_task(*, stop_mode="", items=None):
    return {
        "task_id": "gopay_batch_stop_test",
        "status": "running",
        "stop_mode": stop_mode,
        "current_round": 1,
        "next_round_at": None,
        "items": items or [],
    }


def test_single_gopay_task_record_is_immediate_stop_only():
    saved_logs = []
    task_api = SimpleNamespace(
        _task_store=RegisterTaskStore(),
        _save_task_log=lambda *args, **kwargs: saved_logs.append((args, kwargs)),
        _build_task_log_detail=lambda task_id, detail: {"task_id": task_id, **detail},
    )
    account = SimpleNamespace(id=42, email="gopay@example.com")
    snapshot = {"session_id": "session-gopay-test", "plan": "plus"}

    chatgpt_api._create_gopay_task_record(task_api, "task_gopay_test", account, snapshot)

    record = task_api._task_store.snapshot("task_gopay_test")
    assert record["capabilities"]["stop_after_current"] is False
    assert record["capabilities"]["stop_modes"] == ["immediate"]
    assert saved_logs


def test_single_gopay_stop_hook_cancels_once_after_store_stop(monkeypatch):
    class Store:
        def __init__(self):
            self.calls = 0

        @staticmethod
        def snapshot(task_id):
            return {
                "source": chatgpt_api.GOPAY_TASK_SOURCE,
                "meta": {"account_id": 42, "gopay_session_id": "session-gopay-test"},
            }

        def request_stop(self, task_id):
            self.calls += 1
            return {
                "changed": self.calls == 1,
                "task_snapshot": {
                    "source": chatgpt_api.GOPAY_TASK_SOURCE,
                    "meta": {"account_id": 42, "gopay_session_id": "session-gopay-test"},
                },
            }

    task_api = SimpleNamespace(_task_store=Store())
    cancelled = []
    monkeypatch.setattr(
        chatgpt_api,
        "_cancel_gopay_task_after_immediate_stop",
        lambda api, task_id, snapshot: cancelled.append((api, task_id, snapshot)),
    )

    chatgpt_api._install_gopay_task_stop_hook(task_api)
    task_api._task_store.request_stop("task_gopay_test")
    task_api._task_store.request_stop("task_gopay_test")

    assert task_api._task_store.calls == 2
    assert len(cancelled) == 1
    assert cancelled[0][1] == "task_gopay_test"


def test_single_gopay_stop_hook_surfaces_real_cancel_failure(monkeypatch):
    class Store:
        @staticmethod
        def snapshot(task_id):
            return {
                "source": chatgpt_api.GOPAY_TASK_SOURCE,
                "meta": {"account_id": 42, "gopay_session_id": "session-gopay-test"},
            }

        def request_stop(self, task_id):
            return {
                "changed": True,
                "task_snapshot": {
                    "source": chatgpt_api.GOPAY_TASK_SOURCE,
                    "meta": {"account_id": 42, "gopay_session_id": "session-gopay-test"},
                },
            }

    task_api = SimpleNamespace(_task_store=Store())
    monkeypatch.setattr(
        chatgpt_api,
        "_cancel_gopay_task_after_immediate_stop",
        lambda *args, **kwargs: (_ for _ in ()).throw(HTTPException(409, "GoPay 支付会话取消失败")),
    )
    chatgpt_api._install_gopay_task_stop_hook(task_api)

    with pytest.raises(HTTPException) as caught:
        task_api._task_store.request_stop("task_gopay_test")

    assert caught.value.status_code == 409


def test_graceful_batch_marks_queued_items_stopped_only_after_active_session_drains(monkeypatch):
    active = {
        "account_id": 1,
        "status": "running",
        "snapshot": {"session_id": "active-session", "phase": "waiting_otp"},
    }
    queued = {"account_id": 2, "status": "queued", "snapshot": {}}
    task = _batch_task(stop_mode="after_current", items=[active, queued])
    monkeypatch.setattr(chatgpt_api, "_save_gopay_batch_task", lambda value: value)

    draining = chatgpt_api._finalize_gopay_batch_after_current(task)
    assert draining["status"] == "running"
    assert queued["status"] == "queued"

    active["snapshot"]["phase"] = "succeeded"
    active["status"] = "done"
    completed = chatgpt_api._finalize_gopay_batch_after_current(task)
    assert completed["status"] == "stopped"
    assert queued["status"] == "stopped"
    assert completed["next_round_at"] is None


def test_graceful_batch_start_gate_does_not_create_new_session(monkeypatch):
    task = _batch_task(
        stop_mode="after_current",
        items=[{"account_id": 7, "status": "queued", "snapshot": {}}],
    )

    def mutate(_task_id, mutator):
        mutator(task)
        return task

    monkeypatch.setattr(chatgpt_api, "_mutate_gopay_batch_task", mutate)
    monkeypatch.setattr(chatgpt_api, "_load_gopay_batch_task", lambda _task_id: task)
    monkeypatch.setattr(
        chatgpt_api,
        "start_gopay_payment",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not start GoPay")),
    )

    chatgpt_api._start_gopay_batch_item(task["task_id"], 7)

    assert task["items"][0]["status"] == "stopped"


def test_legacy_batch_cancel_without_body_remains_immediate(monkeypatch):
    task = _batch_task(items=[{"account_id": 9, "status": "queued", "snapshot": {}}])
    monkeypatch.setattr(
        chatgpt_api,
        "_mutate_gopay_batch_task",
        lambda _task_id, mutator: (mutator(task), task)[1],
    )
    monkeypatch.setattr(chatgpt_api, "_save_gopay_batch_task", lambda value: value)

    result = chatgpt_api.cancel_gopay_batch_payment(task["task_id"])

    assert result["status"] == "cancelled"
    assert result["stop_mode"] == "immediate"
    assert result["items"][0]["status"] == "cancelled"


def test_immediate_batch_cancel_failure_is_not_reported_as_cancelled(monkeypatch):
    task = _batch_task(items=[{
        "account_id": 11,
        "email": "active@example.com",
        "status": "running",
        "snapshot": {"session_id": "active-session", "phase": "waiting_otp"},
    }, {
        "account_id": 12,
        "status": "queued",
        "snapshot": {},
    }])

    class FakeSession:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        chatgpt_api,
        "_mutate_gopay_batch_task",
        lambda _task_id, mutator: (mutator(task), task)[1],
    )
    monkeypatch.setattr(chatgpt_api, "_save_gopay_batch_task", lambda value: value)
    monkeypatch.setattr(chatgpt_api, "Session", FakeSession)
    monkeypatch.setattr(
        chatgpt_api,
        "cancel_gopay_payment",
        lambda *args, **kwargs: (_ for _ in ()).throw(HTTPException(503, "provider unavailable")),
    )

    with pytest.raises(HTTPException) as caught:
        chatgpt_api.cancel_gopay_batch_payment(task["task_id"])

    assert caught.value.status_code == 409
    assert task["status"] == "running"
    assert task["stop_mode"] == "immediate"
    assert task["items"][0]["status"] == "running"
    assert task["items"][1]["status"] == "cancelled"
