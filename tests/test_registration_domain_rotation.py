from copy import deepcopy

import pytest
from sqlmodel import Session, create_engine, select

from core.db import RegistrationDomainRotationGroupModel
from services.chatgpt_core import registration_domain_rotation as rotation_module
from services.chatgpt_core.registration_domain_rotation import (
    RegistrationDomainRotationManager,
    mark_stale_rotation_groups_interrupted,
)


class RotationHarness:
    def __init__(self) -> None:
        self.started: list[dict] = []
        self.stopped: list[tuple[str, str, str]] = []
        self.meta_updates: list[tuple[str, dict]] = []
        self.logs: list[tuple[str, str, str]] = []
        self.snapshots: dict[str, dict] = {}
        self.scheduled_retries: list[dict] = []
        self.manager = RegistrationDomainRotationManager(
            start_task=self.start_task,
            stop_task=self.stop_task,
            update_task_meta=self.update_task_meta,
            log_task=self.log_task,
            persist_snapshot=self.persist_snapshot,
            load_snapshot=self.load_snapshot,
            schedule_retry=self.schedule_retry,
        )

    def start_task(
        self,
        group_id,
        domain,
        position,
        domain_count,
        template,
        group_meta,
        before_start,
    ):
        attempt = 1 + sum(
            item["position"] == position for item in self.started
        )
        task_id = f"task-{position}-{attempt}-{domain}"
        self.started.append(
            {
                "task_id": task_id,
                "group_id": group_id,
                "domain": domain,
                "position": position,
                "attempt": attempt,
                "domain_count": domain_count,
                "template": template,
                "meta": deepcopy(group_meta),
            }
        )
        before_start(task_id)
        return task_id

    def schedule_retry(self, delay_seconds, callback):
        class RetryHandle:
            cancelled = False

            def cancel(self):
                self.cancelled = True

        handle = RetryHandle()
        self.scheduled_retries.append(
            {
                "delay_seconds": delay_seconds,
                "callback": callback,
                "handle": handle,
            }
        )
        return handle

    def run_next_retry(self):
        scheduled = self.scheduled_retries.pop(0)
        assert scheduled["handle"].cancelled is False
        scheduled["callback"]()
        return scheduled

    def stop_task(self, task_id, mode, reason_code):
        self.stopped.append((task_id, mode, reason_code))

    def update_task_meta(self, task_id, patch):
        self.meta_updates.append((task_id, deepcopy(patch)))

    def log_task(self, task_id, message, level):
        self.logs.append((task_id, message, level))

    def persist_snapshot(self, snapshot):
        self.snapshots[str(snapshot["task_group_id"])] = deepcopy(snapshot)

    def load_snapshot(self, group_id):
        snapshot = self.snapshots.get(str(group_id))
        return deepcopy(snapshot) if snapshot else None

    def create(
        self,
        *,
        group_id="group-1",
        domains=None,
        slots=1,
        rejection_threshold=50,
        rejection_min_samples=10,
        no_link_streak=10,
    ):
        return self.manager.create_group(
            group_id=group_id,
            domains=domains or ["first.example", "second.example"],
            template={"frozen": True},
            requested_count_per_task=100,
            requested_concurrency_per_task=3,
            active_domain_slots=slots,
            rejection_rate_threshold_percent=rejection_threshold,
            rejection_rate_min_samples=rejection_min_samples,
            no_link_streak_threshold=no_link_streak,
        )


@pytest.mark.parametrize(
    ("decisions", "should_stop"),
    [
        (["registration_disallowed"] * 9, False),
        (["registration_disallowed"] * 5 + ["accepted"] * 5, False),
        (["registration_disallowed"] * 6 + ["accepted"] * 4, True),
    ],
)
def test_rejection_rate_requires_minimum_sample_and_is_strictly_greater_than_half(
    decisions,
    should_stop,
):
    harness = RotationHarness()
    group = harness.create(domains=["quality.example"])
    task_id = group["tasks"][0]["task_id"]

    for decision in decisions:
        harness.manager.record_registration_result(task_id, decision=decision)

    refreshed = harness.manager.get_group("group-1")
    assert bool(harness.stopped) is should_stop
    assert refreshed["domains"][0]["state"] == (
        "draining" if should_stop else "active"
    )
    if len(decisions) == 10:
        assert refreshed["domains"][0]["quality"][
            "registration_rejection_rate_percent"
        ] in {50.0, 60.0}


def test_tenth_consecutive_business_link_miss_triggers_graceful_rotation_stop():
    harness = RotationHarness()
    group = harness.create(domains=["quality.example"])
    task_id = group["tasks"][0]["task_id"]

    for account_id in range(1, 11):
        harness.manager.record_registered_account(
            task_id,
            account_id=account_id,
            attempt_order=account_id,
        )
        harness.manager.record_eligibility_result(
            task_id,
            {
                "account_id": account_id,
                "state": "ineligible",
                "reason_code": "nonzero_checkout_amount",
            },
        )
        assert bool(harness.stopped) is (account_id == 10)

    refreshed = harness.manager.get_group("group-1")
    quality = refreshed["domains"][0]["quality"]
    assert quality["link_quality_miss"] == 10
    assert quality["link_current_miss_streak"] == 10
    assert harness.stopped == [
        (task_id, "after_current", "no_payment_link_streak_reached")
    ]


def test_link_success_resets_streak_while_technical_and_auth_states_are_neutral():
    harness = RotationHarness()
    group = harness.create(domains=["quality.example"], no_link_streak=10)
    task_id = group["tasks"][0]["task_id"]

    for account_id in range(1, 5):
        harness.manager.record_registered_account(
            task_id,
            account_id=account_id,
            attempt_order=account_id,
        )
        harness.manager.record_eligibility_result(
            task_id,
            {"account_id": account_id, "state": "ineligible"},
        )

    harness.manager.record_registered_account(task_id, account_id=5, attempt_order=5)
    harness.manager.record_link_result(
        task_id,
        {
            "account_id": 5,
            "state": "extract_failed",
            "reason_code": "payment_link_generation_failed",
            "message": "upstream HTTP 503 service unavailable",
        },
    )
    harness.manager.record_eligibility_result(
        task_id,
        {"account_id": 5, "state": "eligible", "reason_code": "amount_zero"},
    )
    harness.manager.record_registered_account(task_id, account_id=6, attempt_order=6)
    harness.manager.record_link_result(
        task_id,
        {
            "account_id": 6,
            "state": "pending_auth",
            "reason_code": "registered_auth_pending",
        },
    )

    before_success = harness.manager.get_group("group-1")["domains"][0]["quality"]
    assert before_success["link_current_miss_streak"] == 4
    assert before_success["link_technical_neutral"] == 1
    assert before_success["link_pending"] == 1

    harness.manager.record_registered_account(task_id, account_id=7, attempt_order=7)
    harness.manager.record_link_result(
        task_id,
        {
            "account_id": 7,
            "state": "submit_failed",
            "reason_code": "payment_enqueue_failed",
        },
    )
    after_success = harness.manager.get_group("group-1")["domains"][0]["quality"]
    assert after_success["link_success"] == 1
    assert after_success["link_current_miss_streak"] == 0
    assert not harness.stopped

    persisted_item = harness.snapshots["group-1"]["items"][0]
    assert "accounts" not in persisted_item
    assert "account_id" not in repr(harness.snapshots["group-1"])


def test_quality_rejected_task_releases_slot_only_after_task_terminal():
    harness = RotationHarness()
    group = harness.create(
        domains=["first.example", "second.example"],
        rejection_min_samples=1,
    )
    first_task_id = group["tasks"][0]["task_id"]

    harness.manager.record_registration_result(
        first_task_id,
        decision="registration_disallowed",
    )
    assert [item["domain"] for item in harness.started] == ["first.example"]

    harness.manager.handle_task_terminal(first_task_id, {"status": "stopped"})
    refreshed = harness.manager.get_group("group-1")
    assert [item["domain"] for item in harness.started] == [
        "first.example",
        "second.example",
    ]
    assert refreshed["domains"][0]["state"] == "quality_rejected"
    assert refreshed["domains"][1]["state"] == "active"


def test_active_slots_are_refilled_without_exceeding_the_configured_count():
    harness = RotationHarness()
    group = harness.create(
        domains=["one.example", "two.example", "three.example", "four.example"],
        slots=2,
    )
    assert [item["domain"] for item in harness.started] == [
        "one.example",
        "two.example",
    ]

    harness.manager.handle_task_terminal(group["tasks"][0]["task_id"], {"status": "done"})
    refreshed = harness.manager.get_group("group-1")
    assert [item["domain"] for item in harness.started] == [
        "one.example",
        "two.example",
        "three.example",
    ]
    assert sum(
        refreshed["counts"].get(state, 0)
        for state in ("starting", "active", "draining")
    ) == 2


def test_stopping_group_cancels_queue_and_never_refills_slot():
    harness = RotationHarness()
    group = harness.create(domains=["first.example", "second.example"])
    task_id = group["tasks"][0]["task_id"]

    stopping = harness.manager.stop_group("group-1")
    assert stopping["state"] == "stopping"
    assert stopping["counts"]["cancelled"] == 1
    assert harness.stopped == [(task_id, "after_current", "rotation_group_stop")]

    harness.manager.handle_task_terminal(task_id, {"status": "stopped"})
    final = harness.manager.get_group("group-1")
    assert final["state"] == "stopped"
    assert [item["domain"] for item in harness.started] == ["first.example"]


def test_transient_technical_failure_retries_same_domain_without_consuming_queue():
    harness = RotationHarness()
    group = harness.create(
        domains=["one.example", "two.example", "three.example"],
        slots=1,
    )
    first_task = group["tasks"][0]["task_id"]

    harness.manager.handle_task_terminal(
        first_task,
        {
            "status": "failed",
            "error": "动态代理没有可用候选: SSL WRONG_VERSION_NUMBER",
            "success": 0,
            "skipped": 0,
            "progress": "0/100",
        },
    )
    waiting = harness.manager.get_group("group-1")
    first_domain = waiting["domains"][0]
    assert waiting["state"] == "running"
    assert waiting["counts"] == {"retry_wait": 1, "pending": 2}
    assert first_domain["state"] == "retry_wait"
    assert first_domain["retry_count"] == 1
    assert first_domain["retry_limit"] == 2
    assert first_domain["technical_failure"]["code"] == "dynamic_proxy_unavailable"
    assert harness.scheduled_retries[0]["delay_seconds"] == 5.0
    assert [item["domain"] for item in harness.started] == ["one.example"]
    assert harness.stopped == []

    harness.run_next_retry()
    retrying = harness.manager.get_group("group-1")
    assert [item["domain"] for item in harness.started] == [
        "one.example",
        "one.example",
    ]
    assert retrying["domains"][0]["state"] == "active"
    assert retrying["domains"][0]["attempt_count"] == 2
    assert [task["attempt"] for task in retrying["tasks"]] == [1, 2]
    assert [task["state"] for task in retrying["tasks"]] == [
        "technical_failed",
        "active",
    ]
    assert retrying["tasks"][0]["task_id"] == first_task
    assert retrying["tasks"][1]["is_current"] is True


def test_same_technical_failure_on_two_domains_opens_group_circuit():
    harness = RotationHarness()
    group = harness.create(
        domains=["one.example", "two.example", "three.example", "four.example"],
        slots=3,
    )
    first_task, second_task, third_task = [
        item["task_id"] for item in group["tasks"]
    ]

    failure = {
        "status": "failed",
        "error": "proxy pool unavailable: upstream SSL failure",
        "progress": "0/100",
    }
    harness.manager.handle_task_terminal(first_task, failure)
    assert harness.manager.get_group("group-1")["state"] == "running"
    harness.manager.handle_task_terminal(second_task, failure)

    failing = harness.manager.get_group("group-1")
    assert failing["state"] == "failing"
    assert failing["failure"]["code"] == "technical_failure_circuit_open"
    assert failing["counts"]["technical_failed"] == 2
    assert failing["counts"]["draining"] == 1
    assert failing["counts"]["cancelled"] == 1
    assert "2 个域名" in failing["stop_reason"]
    assert harness.scheduled_retries[0]["handle"].cancelled is True
    assert (third_task, "after_current", "rotation_group_task_failed") in harness.stopped

    harness.manager.handle_task_terminal(third_task, {"status": "stopped"})
    final = harness.manager.get_group("group-1")
    assert final["state"] == "failed"
    assert final["counts"]["cancelled"] == 1


def test_recovered_domain_no_longer_contributes_to_cross_domain_circuit():
    harness = RotationHarness()
    group = harness.create(
        domains=["one.example", "two.example", "three.example"],
        slots=2,
    )
    first_task, second_task = [item["task_id"] for item in group["tasks"]]
    failure = {
        "status": "failed",
        "error": "proxy pool unavailable",
        "progress": "0/100",
    }

    harness.manager.handle_task_terminal(first_task, failure)
    harness.run_next_retry()
    first_retry_task = next(
        task["task_id"]
        for task in harness.manager.get_group("group-1")["tasks"]
        if task["domain"] == "one.example" and task["attempt"] == 2
    )
    harness.manager.record_registration_result(first_retry_task, decision="accepted")
    harness.manager.handle_task_terminal(second_task, failure)

    current = harness.manager.get_group("group-1")
    assert current["state"] == "running"
    assert current["counts"]["retry_wait"] == 1
    assert current["domains"][0]["technical_failure"]["recovered_at"]
    assert current["failure"] == {}


def test_single_domain_exhausts_two_retries_before_group_failure():
    harness = RotationHarness()
    group = harness.create(domains=["one.example", "two.example"], slots=1)
    task_id = group["tasks"][0]["task_id"]
    failure = {
        "status": "failed",
        "error": "network connection timed out",
        "progress": "0/100",
    }

    for expected_retry in (1, 2):
        harness.manager.handle_task_terminal(task_id, failure)
        waiting = harness.manager.get_group("group-1")
        assert waiting["state"] == "running"
        assert waiting["domains"][0]["retry_count"] == expected_retry
        scheduled = harness.run_next_retry()
        assert scheduled["delay_seconds"] == (5.0 if expected_retry == 1 else 15.0)
        task_id = harness.manager.get_group("group-1")["tasks"][-1]["task_id"]

    harness.manager.handle_task_terminal(task_id, failure)
    final = harness.manager.get_group("group-1")
    assert final["state"] == "failed"
    assert final["failure"]["code"] == "technical_retry_exhausted"
    assert final["domains"][0]["state"] == "technical_failed"
    assert final["domains"][0]["retry_count"] == 2
    assert len(final["tasks"]) == 3
    assert final["counts"]["cancelled"] == 1


def test_technical_failure_after_business_progress_never_replays_whole_task():
    harness = RotationHarness()
    group = harness.create(domains=["one.example", "two.example"])
    task_id = group["tasks"][0]["task_id"]
    harness.manager.record_registration_result(task_id, decision="accepted")

    harness.manager.handle_task_terminal(
        task_id,
        {
            "status": "failed",
            "error": "proxy connection timed out",
            "success": 1,
            "progress": "1/100",
        },
    )
    final = harness.manager.get_group("group-1")
    assert final["state"] == "failed"
    assert final["failure"]["code"] == "technical_failure_after_business_progress"
    assert "目标超发" in final["stop_reason"]
    assert harness.scheduled_retries == []
    assert [item["domain"] for item in harness.started] == ["one.example"]


def test_user_stop_cancels_waiting_technical_retry():
    harness = RotationHarness()
    group = harness.create(domains=["one.example", "two.example"])
    task_id = group["tasks"][0]["task_id"]
    harness.manager.handle_task_terminal(
        task_id,
        {"status": "failed", "error": "proxy connection reset", "progress": "0/100"},
    )
    retry_handle = harness.scheduled_retries[0]["handle"]

    stopped = harness.manager.stop_group("group-1")
    assert stopped["state"] == "stopped"
    assert stopped["counts"] == {"stopped": 1, "cancelled": 1}
    assert retry_handle.cancelled is True
    assert [item["domain"] for item in harness.started] == ["one.example"]


def test_nontechnical_child_failure_still_stops_replenishment():
    harness = RotationHarness()
    group = harness.create(
        domains=["one.example", "two.example", "three.example"],
        slots=2,
    )
    first_task, second_task = [item["task_id"] for item in group["tasks"]]

    harness.manager.handle_task_terminal(
        first_task,
        {"status": "failed", "error": "unexpected invariant violation"},
    )
    failing = harness.manager.get_group("group-1")
    assert failing["state"] == "failing"
    assert failing["failure"]["code"] == "domain_task_failed"
    assert failing["counts"]["cancelled"] == 1
    assert harness.scheduled_retries == []
    assert (second_task, "after_current", "rotation_group_task_failed") in harness.stopped


def test_restart_marks_running_and_corrupt_active_snapshots_interrupted(
    tmp_path,
    monkeypatch,
):
    engine = create_engine(f"sqlite:///{tmp_path / 'rotation.db'}")
    RegistrationDomainRotationGroupModel.__table__.create(engine)
    with Session(engine) as session:
        valid = RegistrationDomainRotationGroupModel(
            group_id="valid-group",
            state="running",
        )
        valid.set_snapshot(
            {
                "task_group_id": "valid-group",
                "state": "running",
                "items": [
                    {
                        "domain": "active.example",
                        "position": 1,
                        "state": "active",
                        "task_id": "task-active",
                    }
                ],
            }
        )
        corrupt = RegistrationDomainRotationGroupModel(
            group_id="corrupt-group",
            state="failing",
            snapshot_json="not-json",
        )
        session.add(valid)
        session.add(corrupt)
        session.commit()

    monkeypatch.setattr(rotation_module.core_db, "engine", engine)
    assert mark_stale_rotation_groups_interrupted() == 2

    with Session(engine) as session:
        rows = {
            row.group_id: row
            for row in session.exec(select(RegistrationDomainRotationGroupModel)).all()
        }
        assert rows["valid-group"].state == "interrupted"
        assert rows["valid-group"].get_snapshot()["items"][0]["state"] == "interrupted"
        assert rows["corrupt-group"].state == "interrupted"
        assert rows["corrupt-group"].get_snapshot()["task_group_id"] == "corrupt-group"
        assert "不会自动恢复" in rows["corrupt-group"].stop_reason

    engine.dispose()
