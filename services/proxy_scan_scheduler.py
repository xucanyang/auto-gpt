"""Periodic proxy scan scheduler."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import threading
import time
from typing import Any

from services.proxy_scanner import parse_bool, parse_int, normalize_targets, proxy_scan_manager

DEFAULT_INTERVAL_MINUTES = 30
DEFAULT_CONCURRENCY = 8
DEFAULT_TIMEOUT_SECONDS = 8
LOOP_INTERVAL_SECONDS = 30

_state_lock = threading.Lock()
_stop_event = threading.Event()
_worker_thread: threading.Thread | None = None
_running = False
_next_run_at = 0.0
_last_run_at = 0.0
_last_job_id = ""
_last_error = ""


@dataclass
class ProxyScanSchedulerConfig:
    enabled: bool
    interval_minutes: int
    concurrency: int
    timeout_seconds: int
    targets: list[str]
    only_active: bool


def _config_store():
    from core.config_store import config_store

    return config_store


def _now() -> float:
    return time.time()


def _iso(timestamp: float) -> str:
    if not timestamp:
        return ""
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def get_proxy_scan_scheduler_config() -> ProxyScanSchedulerConfig:
    store = _config_store()
    targets_raw = str(store.get("proxy_scan_targets", "basic,geo,chatgpt") or "basic,geo,chatgpt")
    return ProxyScanSchedulerConfig(
        enabled=parse_bool(store.get("proxy_scan_enabled", "false"), default=False),
        interval_minutes=parse_int(store.get("proxy_scan_interval_minutes", ""), DEFAULT_INTERVAL_MINUTES, minimum=1, maximum=24 * 60),
        concurrency=parse_int(store.get("proxy_scan_concurrency", ""), DEFAULT_CONCURRENCY, minimum=1, maximum=32),
        timeout_seconds=parse_int(store.get("proxy_scan_timeout_seconds", ""), DEFAULT_TIMEOUT_SECONDS, minimum=2, maximum=60),
        targets=normalize_targets([item.strip() for item in targets_raw.split(",") if item.strip()]),
        only_active=parse_bool(store.get("proxy_scan_only_active", "true"), default=True),
    )


def _schedule_next(config: ProxyScanSchedulerConfig, *, base_timestamp: float | None = None) -> None:
    global _next_run_at
    with _state_lock:
        _next_run_at = float(base_timestamp if base_timestamp is not None else _now()) + config.interval_minutes * 60


def _ensure_next_run(config: ProxyScanSchedulerConfig) -> None:
    with _state_lock:
        current = _next_run_at
    if current <= 0:
        _schedule_next(config)


def _has_running_job(job_id: str) -> bool:
    if not job_id:
        return False
    job = proxy_scan_manager.get_job(job_id)
    return bool(job and job.get("status") in {"pending", "running"})


def run_once(*, force: bool = False) -> dict[str, Any]:
    global _last_run_at, _last_job_id, _last_error

    config = get_proxy_scan_scheduler_config()
    if not force and not config.enabled:
        return {"started": False, "reason": "disabled"}
    with _state_lock:
        last_job_id = _last_job_id
    if _has_running_job(last_job_id):
        return {"started": False, "reason": "previous_job_running", "job_id": last_job_id}
    try:
        job = proxy_scan_manager.start_job_from_query(
            only_active=config.only_active,
            targets=config.targets,
            concurrency=config.concurrency,
            timeout_seconds=config.timeout_seconds,
            refresh_geo=True,
        )
        with _state_lock:
            _last_run_at = _now()
            _last_job_id = str(job.get("job_id") or "")
            _last_error = ""
        _schedule_next(config, base_timestamp=_now())
        return {"started": True, "job_id": job.get("job_id"), "job": job}
    except Exception as exc:
        with _state_lock:
            _last_error = str(exc)[:500]
        _schedule_next(config, base_timestamp=_now())
        return {"started": False, "reason": "error", "error": str(exc)[:500]}


def status() -> dict[str, Any]:
    config = get_proxy_scan_scheduler_config()
    with _state_lock:
        last_job_id = _last_job_id
        payload = {
            "running": _running,
            "enabled": config.enabled,
            "interval_minutes": config.interval_minutes,
            "concurrency": config.concurrency,
            "timeout_seconds": config.timeout_seconds,
            "targets": config.targets,
            "only_active": config.only_active,
            "next_run_at": _iso(_next_run_at),
            "last_run_at": _iso(_last_run_at),
            "last_job_id": last_job_id,
            "last_error": _last_error,
        }
    if last_job_id:
        payload["last_job"] = proxy_scan_manager.get_job(last_job_id)
    return payload


def _loop() -> None:
    global _running
    with _state_lock:
        _running = True
    try:
        while not _stop_event.is_set():
            config = get_proxy_scan_scheduler_config()
            if not config.enabled:
                _ensure_next_run(config)
                _stop_event.wait(LOOP_INTERVAL_SECONDS)
                continue
            _ensure_next_run(config)
            with _state_lock:
                due = _next_run_at <= _now()
            if due:
                run_once(force=False)
            _stop_event.wait(LOOP_INTERVAL_SECONDS)
    finally:
        with _state_lock:
            _running = False


def start() -> None:
    global _worker_thread
    if _worker_thread and _worker_thread.is_alive():
        return
    _stop_event.clear()
    _worker_thread = threading.Thread(target=_loop, name="proxy-scan-scheduler", daemon=True)
    _worker_thread.start()


def stop() -> None:
    _stop_event.set()
    thread = _worker_thread
    if thread and thread.is_alive():
        thread.join(timeout=3)
