import json
from types import SimpleNamespace

from api import tasks
from core.task_runtime import RegisterTaskStore


class _FakeRequest:
    def __init__(self, *, real_ip: str = "203.0.113.10", authorization: str = "Bearer unit-token", protocol: str = ""):
        self.headers = {
            "x-real-ip": real_ip,
            "authorization": authorization,
        }
        if protocol:
            self.headers[tasks.TASK_POLL_PROTOCOL_HEADER] = protocol
        self.client = SimpleNamespace(host="172.20.0.1")


def test_legacy_empty_summary_guard_triggers_once_and_is_session_scoped():
    guard = tasks._LegacyEmptyTaskSummaryPollGuard(
        window_seconds=10,
        request_limit=3,
        cooldown_seconds=30,
        max_clients=4,
    )
    request = _FakeRequest()

    assert guard.should_force_refresh(request, has_runtime_active_task=False, now=0) is False
    assert guard.should_force_refresh(request, has_runtime_active_task=False, now=1) is False
    assert guard.should_force_refresh(request, has_runtime_active_task=False, now=2) is True
    assert guard.should_force_refresh(request, has_runtime_active_task=False, now=3) is False

    other_session = _FakeRequest(authorization="Bearer another-token")
    assert guard.should_force_refresh(other_session, has_runtime_active_task=False, now=3) is False


def test_current_protocol_and_real_running_task_bypass_legacy_guard():
    guard = tasks._LegacyEmptyTaskSummaryPollGuard(request_limit=2)
    modern = _FakeRequest(protocol=tasks.TASK_POLL_PROTOCOL_VERSION)
    for now in range(20):
        assert guard.should_force_refresh(modern, has_runtime_active_task=False, now=now) is False

    legacy = _FakeRequest()
    for now in range(20):
        assert guard.should_force_refresh(legacy, has_runtime_active_task=True, now=now) is False
    assert guard.should_force_refresh(legacy, has_runtime_active_task=False, now=21) is False


def test_active_summary_returns_one_no_store_refresh_response_for_legacy_storm(monkeypatch):
    monkeypatch.setattr(tasks, "_task_store", RegisterTaskStore())
    monkeypatch.setattr(
        tasks,
        "_legacy_empty_task_summary_poll_guard",
        tasks._LegacyEmptyTaskSummaryPollGuard(request_limit=2, cooldown_seconds=30),
    )
    request = _FakeRequest()

    assert tasks.list_active_task_summaries(request) == []
    response = tasks.list_active_task_summaries(request)

    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"
    assert json.loads(response.body)["detail"]["code"] == "CLIENT_REFRESH_REQUIRED"
    assert tasks.list_active_task_summaries(request) == []


def test_active_summary_does_not_evict_legacy_client_while_any_task_is_running(monkeypatch):
    store = RegisterTaskStore()
    store.create("task_running_123", platform="chatgpt", total=1, source="unit")
    monkeypatch.setattr(tasks, "_task_store", store)
    monkeypatch.setattr(
        tasks,
        "_legacy_empty_task_summary_poll_guard",
        tasks._LegacyEmptyTaskSummaryPollGuard(request_limit=2),
    )
    request = _FakeRequest()

    assert isinstance(tasks.list_active_task_summaries(request), list)
    assert isinstance(tasks.list_active_task_summaries(request), list)
    assert isinstance(tasks.list_active_task_summaries(request), list)
