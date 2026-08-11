"""Logging configuration with explicit Beijing timestamps."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime

from uvicorn.config import LOGGING_CONFIG
from uvicorn.logging import AccessFormatter, DefaultFormatter

from core.timezone import PROJECT_TIMEZONE


class _BeijingTimeMixin:
    def formatTime(self, record, datefmt=None):  # noqa: N802 - logging API
        value = datetime.fromtimestamp(record.created, PROJECT_TIMEZONE)
        if datefmt:
            return value.strftime(datefmt)
        return value.isoformat(timespec="seconds")


class BeijingDefaultFormatter(_BeijingTimeMixin, DefaultFormatter):
    pass


class BeijingAccessFormatter(_BeijingTimeMixin, AccessFormatter):
    pass


def uvicorn_beijing_log_config() -> dict:
    config = deepcopy(LOGGING_CONFIG)
    config["formatters"]["default"].update(
        {
            "()": "core.logging_config.BeijingDefaultFormatter",
            "fmt": "%(asctime)s %(levelprefix)s %(message)s",
            "datefmt": "%Y-%m-%d %H:%M:%S %z",
        }
    )
    config["formatters"]["access"].update(
        {
            "()": "core.logging_config.BeijingAccessFormatter",
            "fmt": '%(asctime)s %(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s',
            "datefmt": "%Y-%m-%d %H:%M:%S %z",
        }
    )
    return config
