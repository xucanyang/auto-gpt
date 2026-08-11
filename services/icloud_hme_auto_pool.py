"""iCloud HME automatic import-pool replenishment."""

from __future__ import annotations

from dataclasses import dataclass
import random
import threading
import time
from typing import Any

from core.timezone import beijing_from_timestamp


DEFAULT_STOCK_LIMIT = 10
DEFAULT_INTERVAL_MINUTES_MIN = 60
DEFAULT_INTERVAL_MINUTES_MAX = 120
DEFAULT_RATE_LIMIT_BACKOFF_MINUTES = 360
DEFAULT_ERROR_BACKOFF_MINUTES = 3
DEFAULT_ERROR_BACKOFF_MAX_MINUTES = 15
LOOP_INTERVAL_SECONDS = 60

_state_lock = threading.Lock()
_worker_thread: threading.Thread | None = None
_stop_event = threading.Event()
_running = False
_next_run_at = 0.0
_rate_limit_until = 0.0
_error_backoff_until = 0.0
_consecutive_error_count = 0
_last_run_at = 0.0
_last_success_at = 0.0
_last_error = ""
_last_backoff_reason = ""
_last_created_hme = ""
_last_ready_count = 0


@dataclass
class IcloudHmeAutoPoolConfig:
    enabled: bool
    stock_limit: int
    interval_min_minutes: int
    interval_max_minutes: int
    rate_limit_backoff_minutes: int
    error_backoff_minutes: int
    icloud_cookie: str
    icloud_domain_base: str
    forward_to: str
    forward_mailbox_id: str
    tempmail_api_url: str
    tempmail_api_key: str
    tempmail_api_key_header: str


def _config_store():
    from core.config_store import config_store

    return config_store


def _to_bool(value: str | None, default: bool = False) -> bool:
    raw = str(value or "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def _to_int(value: str | None, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(float(str(value or "").strip())))
    except Exception:
        return default


def _now() -> float:
    return time.time()


def _iso(timestamp: float) -> str:
    return beijing_from_timestamp(timestamp)


def get_icloud_hme_auto_pool_config() -> IcloudHmeAutoPoolConfig:
    store = _config_store()
    min_minutes = _to_int(
        store.get("icloud_hme_auto_create_interval_min_minutes", ""),
        DEFAULT_INTERVAL_MINUTES_MIN,
        minimum=1,
    )
    max_minutes = _to_int(
        store.get("icloud_hme_auto_create_interval_max_minutes", ""),
        DEFAULT_INTERVAL_MINUTES_MAX,
        minimum=1,
    )
    if max_minutes < min_minutes:
        max_minutes = min_minutes

    return IcloudHmeAutoPoolConfig(
        enabled=_to_bool(store.get("icloud_hme_auto_create_enabled", ""), default=False),
        stock_limit=_to_int(
            store.get("icloud_hme_auto_create_stock_limit", ""),
            DEFAULT_STOCK_LIMIT,
            minimum=1,
        ),
        interval_min_minutes=min_minutes,
        interval_max_minutes=max_minutes,
        rate_limit_backoff_minutes=_to_int(
            store.get("icloud_hme_auto_create_rate_limit_backoff_minutes", ""),
            DEFAULT_RATE_LIMIT_BACKOFF_MINUTES,
            minimum=1,
        ),
        error_backoff_minutes=_to_int(
            store.get("icloud_hme_auto_create_error_backoff_minutes", ""),
            DEFAULT_ERROR_BACKOFF_MINUTES,
            minimum=0,
        ),
        icloud_cookie=str(store.get("icloud_cookie", "") or "").strip(),
        icloud_domain_base=str(store.get("icloud_domain_base", "icloud.com") or "icloud.com").strip() or "icloud.com",
        forward_to=str(store.get("icloud_forward_to", "b@cccy.me") or "b@cccy.me").strip() or "b@cccy.me",
        forward_mailbox_id=str(store.get("icloud_forward_mailbox_id", "") or "").strip(),
        tempmail_api_url=str(store.get("tempmail_api_url", "") or "").strip(),
        tempmail_api_key=str(store.get("tempmail_api_key", "") or "").strip(),
        tempmail_api_key_header=str(store.get("tempmail_api_key_header", "Authorization") or "Authorization").strip()
        or "Authorization",
    )


def _ready_count(config: IcloudHmeAutoPoolConfig) -> int:
    from core.db import count_icloud_hme_ready_aliases

    return count_icloud_hme_ready_aliases(
        purpose="chatgpt_register",
        bound_service="chatgpt",
        forward_to=config.forward_to,
    )


def _schedule_next(config: IcloudHmeAutoPoolConfig, *, base_timestamp: float | None = None) -> float:
    global _next_run_at

    interval_minutes = random.randint(config.interval_min_minutes, config.interval_max_minutes)
    next_run_at = float(base_timestamp if base_timestamp is not None else _now()) + interval_minutes * 60
    with _state_lock:
        _next_run_at = next_run_at
    return next_run_at


def _ensure_initial_next_run(config: IcloudHmeAutoPoolConfig) -> None:
    with _state_lock:
        current_next_run = _next_run_at
    if current_next_run > 0:
        return
    _schedule_next(config)


def _record_error(message: str) -> None:
    global _last_error

    with _state_lock:
        _last_error = str(message or "").strip()


def _set_error_backoff(config: IcloudHmeAutoPoolConfig, message: str) -> tuple[float, int]:
    global _error_backoff_until, _next_run_at, _last_error, _consecutive_error_count, _last_backoff_reason

    with _state_lock:
        _consecutive_error_count = max(int(_consecutive_error_count or 0), 0) + 1
        if config.error_backoff_minutes <= 0:
            wait_minutes = 0
            _error_backoff_until = 0.0
        else:
            wait_minutes = min(
                config.error_backoff_minutes * _consecutive_error_count,
                DEFAULT_ERROR_BACKOFF_MAX_MINUTES,
            )
            _error_backoff_until = _now() + wait_minutes * 60
            _next_run_at = _error_backoff_until
        _last_error = str(message or "").strip()
        _last_backoff_reason = "普通错误短退避" if wait_minutes > 0 else ""
        return _error_backoff_until, wait_minutes


def _clear_error_backoff() -> None:
    global _error_backoff_until, _consecutive_error_count, _last_backoff_reason

    with _state_lock:
        _error_backoff_until = 0.0
        _consecutive_error_count = 0
        _last_backoff_reason = ""


def _is_non_retryable_error(exc: Exception, message: str) -> bool:
    try:
        from core.base_mailbox import ICloudAuthExpiredError

        if isinstance(exc, ICloudAuthExpiredError):
            return True
    except Exception:
        pass
    text = str(message or "").lower()
    return any(marker in text for marker in ("未配置", "cookie 已失效", "invalid cookie", "session expired"))


def _create_one_alias(config: IcloudHmeAutoPoolConfig) -> dict[str, Any]:
    from core.base_mailbox import ICloudAliasLimitError, IcloudHmeMailbox
    from core.db import patch_icloud_hme_alias

    if not config.icloud_cookie:
        raise RuntimeError("iCloud HME 自动补池未配置 icloud_cookie")
    if not config.forward_to:
        raise RuntimeError("iCloud HME 自动补池未配置 icloud_forward_to")

    mailbox = IcloudHmeMailbox(
        icloud_hme_mode="live",
        icloud_cookie=config.icloud_cookie,
        icloud_domain_base=config.icloud_domain_base,
        icloud_forward_to=config.forward_to,
        icloud_forward_mailbox_id=config.forward_mailbox_id,
        tempmail_api_url=config.tempmail_api_url,
        tempmail_api_key=config.tempmail_api_key,
        tempmail_api_key_header=config.tempmail_api_key_header,
        wait_timeout_seconds=60,
        proxy=None,
    )

    try:
        account = mailbox.create_alias_for_import_pool(
            enabled=True,
            note="auto-pool",
            task_id="",
            mailbox_action="auto_pool_created",
        )
    except ICloudAliasLimitError:
        raise
    except Exception:
        raise

    anonymous_id = str(getattr(account, "account_id", "") or "").strip()
    if anonymous_id:
        try:
            patch_icloud_hme_alias(
                anonymous_id,
                {
                    "enabled": True,
                    "status": "reserved",
                    "task_id": "",
                    "bound_account_email": "",
                    "bound_account_ref": "",
                    "note": "auto-pool",
                },
                allow_internal=True,
            )
        except Exception as exc:
            print(f"[iCloudHME AutoPool] 创建成功但启用入池失败: {exc}")

    return {
        "anonymous_id": anonymous_id,
        "hme": str(getattr(account, "email", "") or "").strip(),
    }


def run_once(*, force: bool = False) -> dict[str, Any]:
    global _last_run_at, _last_success_at, _last_created_hme, _last_ready_count, _rate_limit_until, _last_error
    global _next_run_at
    global _error_backoff_until, _consecutive_error_count, _last_backoff_reason

    config = get_icloud_hme_auto_pool_config()
    ready_count = _ready_count(config)
    with _state_lock:
        _last_ready_count = ready_count

    if not force and not config.enabled:
        return {"ok": False, "reason": "disabled", "ready_count": ready_count}
    if ready_count >= config.stock_limit:
        _clear_error_backoff()
        _schedule_next(config)
        return {
            "ok": True,
            "skipped": True,
            "reason": "stock_limit_reached",
            "ready_count": ready_count,
            "stock_limit": config.stock_limit,
        }

    now = _now()
    with _state_lock:
        rate_limited = _rate_limit_until and now < _rate_limit_until
        error_backoff = _error_backoff_until and now < _error_backoff_until
    if not force and rate_limited:
        return {
            "ok": False,
            "reason": "rate_limit_backoff",
            "ready_count": ready_count,
            "rate_limit_until": _iso(_rate_limit_until),
        }
    if not force and error_backoff:
        return {
            "ok": False,
            "reason": "error_backoff",
            "ready_count": ready_count,
            "error_backoff_until": _iso(_error_backoff_until),
        }

    with _state_lock:
        _last_run_at = now
        _last_error = ""

    try:
        created = _create_one_alias(config)
    except Exception as exc:
        from core.base_mailbox import ICloudAliasLimitError

        message = str(exc)
        if isinstance(exc, ICloudAliasLimitError):
            retry_after = max(int(getattr(exc, "retry_after", 0) or 0), 0)
            backoff_seconds = max(config.rate_limit_backoff_minutes * 60, retry_after)
            with _state_lock:
                _rate_limit_until = _now() + backoff_seconds
                _next_run_at = _rate_limit_until
                _error_backoff_until = 0.0
                _consecutive_error_count = 0
                _last_backoff_reason = ""
                _last_error = message
            print(
                "[iCloudHME AutoPool] 触发 iCloud 创建限流，"
                f"延后到 {_iso(_rate_limit_until)} 再试: {message}"
            )
            return {
                "ok": False,
                "reason": "rate_limited",
                "error": message,
                "rate_limit_until": _iso(_rate_limit_until),
            }

        if _is_non_retryable_error(exc, message):
            _record_error(message)
            next_run_at = _schedule_next(config)
            print(f"[iCloudHME AutoPool] 创建失败，需要人工处理配置/凭据: {message}")
            return {
                "ok": False,
                "reason": "non_retryable_error",
                "error": message,
                "next_run_at": _iso(next_run_at),
            }

        _record_error(message)
        error_backoff_until, wait_minutes = _set_error_backoff(config, message)
        if wait_minutes > 0:
            print(f"[iCloudHME AutoPool] 创建失败，普通错误短退避 {wait_minutes} 分钟后再试: {message}")
        else:
            _schedule_next(config)
            print(f"[iCloudHME AutoPool] 创建失败: {message}")
        return {
            "ok": False,
            "reason": "error",
            "error": message,
            "error_backoff_until": _iso(error_backoff_until),
            "error_backoff_minutes": wait_minutes,
        }

    ready_count_after = _ready_count(config)
    next_run_at = _schedule_next(config)
    with _state_lock:
        _last_success_at = _now()
        _last_created_hme = str(created.get("hme") or "")
        _last_ready_count = ready_count_after
        _rate_limit_until = 0.0
    _clear_error_backoff()
    print(
        "[iCloudHME AutoPool] 已创建并加入导入池: "
        f"{created.get('hme') or '-'}，当前可用库存 {ready_count_after}/{config.stock_limit}，"
        f"下次检查 {_iso(next_run_at)}"
    )
    return {
        "ok": True,
        "created": created,
        "ready_count": ready_count_after,
        "stock_limit": config.stock_limit,
        "next_run_at": _iso(next_run_at),
    }


def get_status() -> dict[str, Any]:
    config = get_icloud_hme_auto_pool_config()
    ready_count = _ready_count(config)
    with _state_lock:
        _last_ready_count = ready_count
        next_run_at = _next_run_at
        rate_limit_until = _rate_limit_until
        error_backoff_until = _error_backoff_until
        consecutive_error_count = _consecutive_error_count
        last_backoff_reason = _last_backoff_reason
        last_run_at = _last_run_at
        last_success_at = _last_success_at
        last_error = _last_error
        last_created_hme = _last_created_hme
        running = _running

    now = _now()
    return {
        "running": running,
        "enabled": config.enabled,
        "stock_limit": config.stock_limit,
        "ready_count": ready_count,
        "interval_min_minutes": config.interval_min_minutes,
        "interval_max_minutes": config.interval_max_minutes,
        "rate_limit_backoff_minutes": config.rate_limit_backoff_minutes,
        "error_backoff_minutes": config.error_backoff_minutes,
        "next_run_at": _iso(next_run_at),
        "seconds_until_next_run": max(int(next_run_at - now), 0) if next_run_at else 0,
        "rate_limit_until": _iso(rate_limit_until),
        "in_rate_limit_backoff": bool(rate_limit_until and now < rate_limit_until),
        "error_backoff_until": _iso(error_backoff_until),
        "in_error_backoff": bool(error_backoff_until and now < error_backoff_until),
        "consecutive_error_count": consecutive_error_count,
        "last_backoff_reason": last_backoff_reason,
        "last_run_at": _iso(last_run_at),
        "last_success_at": _iso(last_success_at),
        "last_created_hme": last_created_hme,
        "last_error": last_error,
        "forward_to": config.forward_to,
    }


def _loop() -> None:
    global _running

    while not _stop_event.is_set():
        try:
            config = get_icloud_hme_auto_pool_config()
            if config.enabled:
                _ensure_initial_next_run(config)
                now = _now()
                with _state_lock:
                    next_run_at = _next_run_at
                    rate_limit_until = _rate_limit_until
                    error_backoff_until = _error_backoff_until
                if rate_limit_until and now < rate_limit_until:
                    pass
                elif error_backoff_until and now < error_backoff_until:
                    pass
                elif next_run_at and now >= next_run_at:
                    run_once()
            else:
                with _state_lock:
                    _last_ready_count = _ready_count(config)
        except Exception as exc:
            _record_error(str(exc))
            print(f"[iCloudHME AutoPool] 调度错误: {exc}")

        _stop_event.wait(LOOP_INTERVAL_SECONDS)

    with _state_lock:
        _running = False


def start() -> None:
    global _worker_thread, _running

    with _state_lock:
        if _running:
            return
        _running = True
    _stop_event.clear()
    _worker_thread = threading.Thread(target=_loop, daemon=True, name="icloud-hme-auto-pool")
    _worker_thread.start()
    print("[iCloudHME AutoPool] 已启动")


def stop() -> None:
    global _worker_thread

    _stop_event.set()
    thread = _worker_thread
    if thread and thread.is_alive():
        thread.join(timeout=5)
    _worker_thread = None
    print("[iCloudHME AutoPool] 已停止")
