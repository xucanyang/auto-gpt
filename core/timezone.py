"""Project timezone helpers.

Database and protocol timestamps remain UTC. Project-facing timestamps are
rendered in Asia/Shanghai with an explicit offset so they stay correct outside
the production host and across browser timezones.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo


PROJECT_TIMEZONE_NAME = "Asia/Shanghai"
PROJECT_TIMEZONE = ZoneInfo(PROJECT_TIMEZONE_NAME)


def beijing_now() -> datetime:
    return datetime.now(PROJECT_TIMEZONE)


def beijing_now_iso(*, timespec: str = "seconds") -> str:
    return beijing_now().isoformat(timespec=timespec)


def as_beijing(value: datetime) -> datetime:
    """Convert an instant to Beijing time; legacy naive database values are UTC."""

    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(PROJECT_TIMEZONE)


def beijing_iso(value: datetime | None, *, timespec: str = "seconds") -> str:
    if value is None:
        return ""
    return as_beijing(value).isoformat(timespec=timespec)


def beijing_from_timestamp(timestamp: float, *, timespec: str = "seconds") -> str:
    if not timestamp:
        return ""
    return datetime.fromtimestamp(timestamp, PROJECT_TIMEZONE).isoformat(timespec=timespec)


def beijing_date(value: datetime | None = None) -> str:
    current = beijing_now() if value is None else as_beijing(value)
    return current.strftime("%Y-%m-%d")


def beijing_log_time() -> str:
    return beijing_now().strftime("%H:%M:%S")
