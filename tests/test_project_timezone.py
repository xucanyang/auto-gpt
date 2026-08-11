from datetime import datetime, timezone

from api.tasks import _task_log_summary
from core.logging_config import BeijingDefaultFormatter, uvicorn_beijing_log_config
from core.db import TaskLog
from core.timezone import (
    PROJECT_TIMEZONE_NAME,
    as_beijing,
    beijing_from_timestamp,
    beijing_iso,
)


def test_legacy_naive_database_datetime_is_interpreted_as_utc():
    value = datetime(2026, 8, 11, 17, 53, 57)

    converted = as_beijing(value)

    assert converted.isoformat() == "2026-08-12T01:53:57+08:00"
    assert getattr(converted.tzinfo, "key", "") == PROJECT_TIMEZONE_NAME


def test_aware_utc_datetime_and_epoch_render_with_explicit_beijing_offset():
    value = datetime(2026, 8, 11, 17, 53, 57, tzinfo=timezone.utc)

    assert beijing_iso(value) == "2026-08-12T01:53:57+08:00"
    assert beijing_from_timestamp(value.timestamp()) == "2026-08-12T01:53:57+08:00"


def test_task_history_summary_exposes_beijing_time_without_changing_storage():
    log = TaskLog(
        id=1,
        task_id="",
        platform="chatgpt",
        email="",
        status="success",
        created_at=datetime(2026, 8, 11, 17, 53, 57),
    )

    summary = _task_log_summary(log)

    assert summary["created_at"] == "2026-08-12T01:53:57+08:00"
    assert log.created_at == datetime(2026, 8, 11, 17, 53, 57)


def test_uvicorn_logs_include_explicit_beijing_timestamp():
    config = uvicorn_beijing_log_config()

    assert config["formatters"]["default"]["datefmt"] == "%Y-%m-%d %H:%M:%S %z"
    assert config["formatters"]["access"]["()"] == "core.logging_config.BeijingAccessFormatter"

    formatter = BeijingDefaultFormatter(fmt="%(asctime)s", datefmt="%Y-%m-%d %H:%M:%S %z")
    record = type("Record", (), {"created": 1786467600.0})()
    assert formatter.formatTime(record, formatter.datefmt).endswith("+0800")
