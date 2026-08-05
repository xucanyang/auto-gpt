"""Playwright 版 Sentinel SDK token 获取辅助。"""

from __future__ import annotations

import asyncio
import json
import os
import selectors
import signal
import subprocess
import sys
import threading
import time
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

from core.browser_runtime import (
    ensure_browser_display_available,
    resolve_browser_headless,
)
from core.playwright_proxy import playwright_proxy_context
from core.task_runtime import TaskInterruption

from .sentinel_constants import (
    DEFAULT_SENTINEL_FRAME_URL,
    DEFAULT_SENTINEL_SDK_URL,
    PINNED_CHROMIUM_VERSION,
)
from .utils import build_sec_ch_ua_full_version_list, extract_chrome_full_version


def _auth_browser_concurrency_limit() -> int:
    try:
        requested = int(os.getenv("AUTH_BROWSER_MAX_CONCURRENCY", "2") or 2)
    except (TypeError, ValueError):
        requested = 2
    # Deployments choose a conservative host-specific value. Keep a finite
    # process-level ceiling even when Docker itself has no memory cgroup limit.
    return max(1, min(requested, 8))


AUTH_BROWSER_MAX_CONCURRENCY = _auth_browser_concurrency_limit()
_AUTH_BROWSER_SEMAPHORE = threading.BoundedSemaphore(AUTH_BROWSER_MAX_CONCURRENCY)
_BROWSER_SLOT_STATE_LOCK = threading.Lock()
_BROWSER_ACTIVE_COUNT = 0
_BROWSER_LAUNCH_STATE_LOCK = threading.Lock()
_BROWSER_NEXT_LAUNCH_AT = 0.0

_BROWSER_WORKER_POLL_SECONDS = 0.2
_BROWSER_WORKER_TERM_GRACE_SECONDS = 2.0
_AUTH_BROWSER_HARD_TIMEOUT_DEFAULT_SECONDS = 150.0
_SENTINEL_BROWSER_HARD_TIMEOUT_DEFAULT_SECONDS = 90.0
_BROWSER_WORKER_ID_ENV = "AUTO_GPT_BROWSER_WORKER_ID"


def _browser_second_slot_reserve_bytes() -> int:
    try:
        reserve_mib = int(
            float(os.getenv("BROWSER_SECOND_SLOT_RESERVE_MIB", "1280") or 1280)
        )
    except (TypeError, ValueError):
        reserve_mib = 1280
    return max(512, min(reserve_mib, 2048)) * 1024 * 1024


def _read_int_file(path: str) -> Optional[int]:
    try:
        with open(path, "r", encoding="ascii") as handle:
            value = handle.read().strip()
    except OSError:
        return None
    if not value or value == "max":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _browser_memory_allows_second_slot() -> tuple[bool, int, int, int]:
    current = _read_int_file("/sys/fs/cgroup/memory.current")
    limit = _read_int_file("/sys/fs/cgroup/memory.max")
    reserve = _browser_second_slot_reserve_bytes()
    if current is None or limit is None or limit <= 0:
        return True, int(current or 0), int(limit or 0), reserve
    return current + reserve <= limit, current, limit, reserve


def _auth_browser_pid_reserve() -> int:
    try:
        reserve = int(float(os.getenv("AUTH_BROWSER_PID_RESERVE", "0") or 0))
    except (TypeError, ValueError):
        reserve = 0
    return max(0, min(reserve, 4096))


def _browser_pid_headroom_allows_slot() -> tuple[bool, int, int, int]:
    current = _read_int_file("/sys/fs/cgroup/pids.current")
    limit = _read_int_file("/sys/fs/cgroup/pids.max")
    reserve = _auth_browser_pid_reserve()
    if reserve <= 0 or current is None or limit is None or limit <= 0:
        return True, int(current or 0), int(limit or 0), reserve
    return current + reserve <= limit, current, limit, reserve


def _auth_browser_launch_interval_seconds() -> float:
    try:
        interval = float(
            os.getenv("AUTH_BROWSER_LAUNCH_INTERVAL_SECONDS", "0") or 0
        )
    except (TypeError, ValueError):
        interval = 0.0
    return max(0.0, min(interval, 60.0))


def _wait_for_browser_launch_turn(
    operation: str,
    *,
    logger: Callable[[str], None],
    stop_check: Optional[Callable[[], None]] = None,
) -> None:
    """Space process-wide browser launches without reducing active capacity."""

    global _BROWSER_NEXT_LAUNCH_AT
    interval = _auth_browser_launch_interval_seconds()
    if interval <= 0:
        return
    if stop_check is not None:
        stop_check()

    with _BROWSER_LAUNCH_STATE_LOCK:
        now = time.monotonic()
        launch_at = max(now, _BROWSER_NEXT_LAUNCH_AT)
        _BROWSER_NEXT_LAUNCH_AT = launch_at + interval

    wait_logged = False
    while True:
        if stop_check is not None:
            stop_check()
        remaining = launch_at - time.monotonic()
        if remaining <= 0:
            return
        if not wait_logged:
            logger(
                "[控制] browser_launch=waiting reason=stagger "
                f"delay={remaining:.3f} interval={interval:.3f} operation={operation}"
            )
            wait_logged = True
        time.sleep(min(remaining, 0.2))


def _browser_hard_timeout_seconds(env_name: str, default: float) -> float:
    try:
        configured = float(os.getenv(env_name, str(default)) or default)
    except (TypeError, ValueError):
        configured = default
    return max(30.0, min(configured, 600.0))


@dataclass
class _BrowserWorkerOutcome:
    status: str
    value: Any = None
    error: str = ""
    exit_code: Optional[int] = None


def _browser_worker_command(protocol_fd: int) -> list[str]:
    return [
        sys.executable,
        "-m",
        "services.chatgpt_core.sentinel_browser_worker",
        str(protocol_fd),
    ]


def _marked_browser_worker_processes(worker_id: str) -> dict[int, int]:
    marker = f"{_BROWSER_WORKER_ID_ENV}={worker_id}".encode("utf-8")
    matches: dict[int, int] = {}
    if not worker_id or os.name != "posix":
        return matches
    try:
        proc_entries = os.scandir("/proc")
    except OSError:
        return matches
    with proc_entries:
        for entry in proc_entries:
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            try:
                with open(f"/proc/{pid}/environ", "rb") as handle:
                    environment = handle.read()
                if marker not in environment.split(b"\0"):
                    continue
                matches[pid] = os.getpgid(pid)
            except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
                continue
    return matches


def _signal_browser_worker_processes(
    process: subprocess.Popen[bytes],
    *,
    worker_id: str,
    signal_number: int,
    logger: Callable[[str], None],
) -> None:
    marked = _marked_browser_worker_processes(worker_id)
    marked.setdefault(int(process.pid), int(process.pid))
    current_group = os.getpgrp()
    signaled_groups: set[int] = set()
    for process_group in sorted(set(marked.values())):
        if process_group <= 0 or process_group == current_group:
            continue
        try:
            os.killpg(process_group, signal_number)
            signaled_groups.add(process_group)
        except ProcessLookupError:
            pass
        except Exception as exc:
            logger(
                "Browser Worker 进程组信号失败: "
                f"pgid={process_group} signal={signal_number} error={exc}"
            )
    for pid, process_group in marked.items():
        if process_group in signaled_groups or pid == os.getpid():
            continue
        try:
            os.kill(pid, signal_number)
        except ProcessLookupError:
            pass
        except Exception as exc:
            logger(
                "Browser Worker 进程信号失败: "
                f"pid={pid} signal={signal_number} error={exc}"
            )


def _terminate_browser_worker_group(
    process: subprocess.Popen[bytes],
    *,
    worker_id: str,
    logger: Callable[[str], None],
) -> None:
    """Terminate all marked worker, Node, Chromium, and Crashpad groups."""
    _signal_browser_worker_processes(
        process,
        worker_id=worker_id,
        signal_number=signal.SIGTERM,
        logger=logger,
    )

    deadline = time.monotonic() + _BROWSER_WORKER_TERM_GRACE_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None and not _marked_browser_worker_processes(
            worker_id
        ):
            return
        time.sleep(0.05)

    _signal_browser_worker_processes(
        process,
        worker_id=worker_id,
        signal_number=signal.SIGKILL,
        logger=logger,
    )
    try:
        process.wait(timeout=1.0)
    except Exception:
        pass


def _decode_browser_worker_message(
    raw_line: bytes,
    *,
    logger: Callable[[str], None],
) -> Optional[_BrowserWorkerOutcome]:
    try:
        message = json.loads(raw_line.decode("utf-8"))
    except Exception as exc:
        logger(f"Browser Worker 返回了无效协议消息: {exc}")
        return None
    if not isinstance(message, dict):
        return None
    kind = str(message.get("type") or "")
    if kind == "log":
        logger(str(message.get("message") or ""))
        return None
    if kind == "result":
        return _BrowserWorkerOutcome(status="ok", value=message.get("value"))
    if kind == "error":
        return _BrowserWorkerOutcome(
            status="error",
            error=str(message.get("error") or "browser worker failed")[:1000],
        )
    if kind == "callback_request":
        return _BrowserWorkerOutcome(status="callback", value=dict(message))
    logger(f"Browser Worker 返回了未知消息类型: {kind or '<empty>'}")
    return None


def _run_isolated_browser_transaction(
    operation: str,
    payload: dict[str, Any],
    *,
    hard_timeout_seconds: float,
    logger: Callable[[str], None],
    stop_check: Optional[Callable[[], None]] = None,
    callbacks: Optional[dict[str, Callable[[dict[str, Any]], Any]]] = None,
) -> _BrowserWorkerOutcome:
    """Run one browser transaction in a killable OS process group."""
    read_fd, write_fd = os.pipe()
    process: Optional[subprocess.Popen[bytes]] = None
    selector = selectors.DefaultSelector()
    protocol_buffer = b""
    outcome: Optional[_BrowserWorkerOutcome] = None
    timed_out = False
    interrupted: Optional[BaseException] = None
    worker_id = uuid.uuid4().hex

    try:
        worker_environment = dict(os.environ)
        worker_environment[_BROWSER_WORKER_ID_ENV] = worker_id
        process = subprocess.Popen(
            _browser_worker_command(write_fd),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            pass_fds=(write_fd,),
            start_new_session=True,
            close_fds=True,
            bufsize=0,
            env=worker_environment,
        )
        os.close(write_fd)
        write_fd = -1
        selector.register(read_fd, selectors.EVENT_READ)

        request_bytes = (
            json.dumps(
            {"operation": str(operation), "payload": dict(payload or {})},
            ensure_ascii=False,
            separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        if process.stdin is None:
            raise RuntimeError("browser worker stdin unavailable")
        request_view = memoryview(request_bytes)
        while request_view:
            written = process.stdin.write(request_view)
            if not written:
                raise BrokenPipeError("browser worker closed stdin before request completed")
            request_view = request_view[written:]
        process.stdin.flush()
        if not callbacks:
            process.stdin.close()

        deadline = time.monotonic() + max(float(hard_timeout_seconds), 0.1)
        protocol_eof = False
        while True:
            if stop_check is not None:
                try:
                    stop_check()
                except BaseException as exc:  # ensure cleanup before propagating task stop
                    interrupted = exc
                    break

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break

            events = selector.select(
                timeout=min(_BROWSER_WORKER_POLL_SECONDS, remaining)
            )
            for key, _mask in events:
                chunk = os.read(int(key.fd), 65536)
                if not chunk:
                    protocol_eof = True
                    try:
                        selector.unregister(read_fd)
                    except Exception:
                        pass
                    continue
                protocol_buffer += chunk
                while b"\n" in protocol_buffer:
                    raw_line, protocol_buffer = protocol_buffer.split(b"\n", 1)
                    if not raw_line:
                        continue
                    message_outcome = _decode_browser_worker_message(
                        raw_line,
                        logger=logger,
                    )
                    if message_outcome is not None:
                        if message_outcome.status == "callback":
                            callback_request = dict(message_outcome.value or {})
                            callback_name = str(
                                callback_request.get("name") or ""
                            ).strip()
                            callback_id = str(
                                callback_request.get("id") or ""
                            ).strip()
                            callback_payload = callback_request.get("payload")
                            if not isinstance(callback_payload, dict):
                                callback_payload = {}
                            callback = (callbacks or {}).get(callback_name)
                            callback_response: dict[str, Any] = {
                                "type": "callback_response",
                                "id": callback_id,
                            }
                            if callback is None:
                                callback_response["error"] = (
                                    f"unsupported browser callback: {callback_name or '<empty>'}"
                                )
                            else:
                                if stop_check is not None:
                                    try:
                                        stop_check()
                                    except BaseException as exc:
                                        interrupted = exc
                                        break
                                try:
                                    callback_response["value"] = callback(
                                        callback_payload
                                    )
                                except BaseException as exc:
                                    if isinstance(
                                        exc,
                                        (TaskInterruption, KeyboardInterrupt, SystemExit),
                                    ):
                                        interrupted = exc
                                        break
                                    callback_response["error"] = (
                                        f"{type(exc).__name__}: {exc}"
                                    )[:1000]
                            response_bytes = (
                                json.dumps(
                                    callback_response,
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                )
                                + "\n"
                            ).encode("utf-8")
                            if process.stdin is None:
                                raise RuntimeError(
                                    "browser worker stdin unavailable during callback"
                                )
                            process.stdin.write(response_bytes)
                            process.stdin.flush()
                        else:
                            outcome = message_outcome

            if interrupted is not None:
                break

            exit_code = process.poll()
            if exit_code is not None and protocol_eof:
                break

        if protocol_buffer.strip():
            message_outcome = _decode_browser_worker_message(
                protocol_buffer.strip(),
                logger=logger,
            )
            if message_outcome is not None:
                outcome = message_outcome

        if interrupted is not None:
            logger(f"Browser Worker 收到任务停止请求，清理进程组 pid={process.pid}")
            _terminate_browser_worker_group(
                process,
                worker_id=worker_id,
                logger=logger,
            )
            raise interrupted
        if timed_out:
            logger(
                "Browser Worker 超过硬截止，清理进程组: "
                f"operation={operation} timeout={hard_timeout_seconds:.1f}s pid={process.pid}"
            )
            _terminate_browser_worker_group(
                process,
                worker_id=worker_id,
                logger=logger,
            )
            return _BrowserWorkerOutcome(
                status="timeout",
                error=f"{operation} hard timeout after {hard_timeout_seconds:.1f}s",
                exit_code=process.poll(),
            )

        exit_code = process.poll()
        if outcome is not None:
            outcome.exit_code = exit_code
            if outcome.status != "ok" or exit_code not in (0, None):
                _terminate_browser_worker_group(
                    process,
                    worker_id=worker_id,
                    logger=logger,
                )
            elif _marked_browser_worker_processes(worker_id):
                logger("Browser Worker 正常退出后仍有标记进程，执行兜底清理")
                _terminate_browser_worker_group(
                    process,
                    worker_id=worker_id,
                    logger=logger,
                )
            return outcome
        if exit_code not in (0, None) or _marked_browser_worker_processes(worker_id):
            _terminate_browser_worker_group(
                process,
                worker_id=worker_id,
                logger=logger,
            )
        return _BrowserWorkerOutcome(
            status="error",
            error=f"browser worker exited without result (exit_code={exit_code})",
            exit_code=exit_code,
        )
    except BaseException as exc:
        if process is not None and (
            process.poll() is None or _marked_browser_worker_processes(worker_id)
        ):
            _terminate_browser_worker_group(
                process,
                worker_id=worker_id,
                logger=logger,
            )
        if interrupted is not None or isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        error_text = f"{type(exc).__name__}: {exc}"[:1000]
        logger(f"Browser Worker 启动或通信失败: {error_text}")
        return _BrowserWorkerOutcome(status="error", error=error_text)
    finally:
        try:
            selector.close()
        except Exception:
            pass
        try:
            os.close(read_fd)
        except OSError:
            pass
        if write_fd >= 0:
            try:
                os.close(write_fd)
            except OSError:
                pass
        if process is not None and process.stdin is not None:
            try:
                process.stdin.close()
            except Exception:
                pass


@contextmanager
def browser_capacity_slot(
    operation: str,
    *,
    logger: Optional[Callable[[str], None]] = None,
    stop_check: Optional[Callable[[], None]] = None,
):
    """Lease one process-wide browser slot, waiting interruptibly if needed."""

    global _BROWSER_ACTIVE_COUNT
    log = logger or (lambda _message: None)

    def _try_acquire(*, blocking: bool, timeout: float = 0.0):
        global _BROWSER_ACTIVE_COUNT
        if blocking:
            semaphore_acquired = _AUTH_BROWSER_SEMAPHORE.acquire(timeout=timeout)
        else:
            semaphore_acquired = _AUTH_BROWSER_SEMAPHORE.acquire(blocking=False)
        if not semaphore_acquired:
            return False, "capacity", None
        try:
            with _BROWSER_SLOT_STATE_LOCK:
                pid_state = _browser_pid_headroom_allows_slot()
                if not pid_state[0]:
                    _AUTH_BROWSER_SEMAPHORE.release()
                    return False, "pids", pid_state
                if _BROWSER_ACTIVE_COUNT >= 1:
                    memory_state = _browser_memory_allows_second_slot()
                    if not memory_state[0]:
                        _AUTH_BROWSER_SEMAPHORE.release()
                        return False, "memory", memory_state
                _BROWSER_ACTIVE_COUNT += 1
        except BaseException:
            _AUTH_BROWSER_SEMAPHORE.release()
            raise
        return True, "", None

    acquired, wait_reason, gate_state = _try_acquire(blocking=False)
    logged_wait_reasons: set[str] = set()

    def _log_wait(reason: str, state: Optional[tuple[bool, int, int, int]]) -> None:
        if reason in logged_wait_reasons:
            return
        logged_wait_reasons.add(reason)
        if reason == "memory" and state is not None:
            _allowed, current, limit, reserve = state
            log(
                "[控制] browser_slot=waiting reason=memory "
                f"current={current} limit={limit} reserve={reserve} operation={operation}"
            )
            return
        if reason == "pids" and state is not None:
            _allowed, current, limit, reserve = state
            log(
                "[控制] browser_slot=waiting reason=pids "
                f"current={current} limit={limit} reserve={reserve} operation={operation}"
            )
            return
        log(
            "[控制] browser_slot=waiting reason=capacity "
            f"limit={AUTH_BROWSER_MAX_CONCURRENCY} operation={operation}"
        )

    if not acquired:
        _log_wait(wait_reason, gate_state)
        while not acquired:
            if stop_check is not None:
                stop_check()
            if wait_reason in {"memory", "pids"}:
                time.sleep(0.5)
                acquired, wait_reason, gate_state = _try_acquire(blocking=False)
            else:
                acquired, wait_reason, gate_state = _try_acquire(
                    blocking=True,
                    timeout=0.5,
                )
            if not acquired:
                _log_wait(wait_reason, gate_state)
        log(
            "[控制] browser_slot=acquired after_wait=true "
            f"limit={AUTH_BROWSER_MAX_CONCURRENCY} operation={operation}"
        )
    try:
        yield
    finally:
        if acquired:
            with _BROWSER_SLOT_STATE_LOCK:
                _BROWSER_ACTIVE_COUNT = max(0, _BROWSER_ACTIVE_COUNT - 1)
            _AUTH_BROWSER_SEMAPHORE.release()


def run_with_browser_capacity(
    operation: str,
    callback: Callable[[], Any],
    *,
    logger: Optional[Callable[[str], None]] = None,
    stop_check: Optional[Callable[[], None]] = None,
) -> Any:
    """Run arbitrary browser work behind the shared capacity gate."""

    with browser_capacity_slot(
        operation,
        logger=logger,
        stop_check=stop_check,
    ):
        _wait_for_browser_launch_turn(
            operation,
            logger=logger or (lambda _message: None),
            stop_check=stop_check,
        )
        return callback()


def _run_with_browser_slot(
    operation: str,
    payload: dict[str, Any],
    *,
    hard_timeout_seconds: float,
    logger: Callable[[str], None],
    stop_check: Optional[Callable[[], None]] = None,
    callbacks: Optional[dict[str, Callable[[dict[str, Any]], Any]]] = None,
) -> _BrowserWorkerOutcome:
    return run_with_browser_capacity(
        operation,
        lambda: _run_isolated_browser_transaction(
            operation,
            payload,
            hard_timeout_seconds=hard_timeout_seconds,
            logger=logger,
            stop_check=stop_check,
            callbacks=callbacks,
        ),
        logger=logger,
        stop_check=stop_check,
    )


@dataclass
class BrowserAccountCreateResult:
    """Result of the browser-owned about-you account creation transaction."""

    status_code: int = 0
    response_url: str = ""
    response_text: str = ""
    response_json: dict[str, Any] = field(default_factory=dict)
    cookies: list[dict[str, Any]] = field(default_factory=list)
    cookie_names: tuple[str, ...] = ()
    sentinel_field_lengths: dict[str, int] = field(default_factory=dict)
    cf_clearance_present: bool = False
    oai_sc_present: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= int(self.status_code or 0) < 300


@dataclass
class BrowserRegistrationStageResult:
    """Result of a browser-owned email/OTP/about-you registration stage."""

    final_state: dict[str, Any] = field(default_factory=dict)
    page_url: str = ""
    cookies: list[dict[str, Any]] = field(default_factory=list)
    cookie_names: tuple[str, ...] = ()
    device_id: str = ""
    user_agent: str = ""
    web_session: dict[str, Any] = field(default_factory=dict)
    requested_executor: str = ""
    effective_executor: str = ""
    headless_reason: str = ""
    route_event: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    @property
    def signup_complete(self) -> bool:
        """Whether OpenAI signup itself finished (independent of ChatGPT AT).

        ``missing_web_session`` means about_you / callback already committed but
        ``/api/auth/session`` did not yield accessToken. That is still a finished
        registration identity and must not be treated as a hard signup failure.
        """
        error = str(self.error or "")
        if "missing_web_session" in error:
            return True
        page_type = str((self.final_state or {}).get("page_type") or "").strip().lower()
        return page_type in {
            "callback",
            "oauth_callback",
            "chatgpt_home",
            "about_you",
            "add_phone",
        }

    @property
    def ok(self) -> bool:
        """Full success: signup finished and no stage error (includes AT capture)."""
        if self.error:
            # Signup-finished-but-missing-AT is recoverable by the engine.
            if "missing_web_session" in str(self.error):
                return False
            return False
        return self.signup_complete


@dataclass
class BrowserOAuthTokenRecoveryResult:
    """Result of the isolated Codex OAuth recovery transaction."""

    tokens: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and bool(
            str(self.tokens.get("access_token") or "").strip()
        ) and bool(str(self.tokens.get("refresh_token") or "").strip())


def export_session_cookies_for_playwright(
    session: Any,
    *,
    fallback_domain: str = "auth.openai.com",
) -> list[dict[str, Any]]:
    """Export a requests-compatible cookie jar without losing domain scope."""
    cookies = getattr(session, "cookies", None)
    if cookies is None:
        return []

    jar = getattr(cookies, "jar", None)
    try:
        iterable = list(jar if jar is not None else cookies)
    except Exception:
        return []

    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for item in iterable:
        name = str(getattr(item, "name", None) or getattr(item, "key", None) or "").strip()
        value = getattr(item, "value", None)
        if not name or value is None:
            continue

        domain = str(getattr(item, "domain", "") or "").strip() or fallback_domain
        path = str(getattr(item, "path", "/") or "/").strip() or "/"
        key = (name, str(value), domain, path)
        if key in seen:
            continue
        seen.add(key)

        cookie: dict[str, Any] = {
            "name": name,
            "value": str(value),
            "domain": domain,
            "path": path,
            "secure": bool(getattr(item, "secure", False)),
        }
        expires = getattr(item, "expires", None)
        try:
            if expires is not None and float(expires) > 0:
                cookie["expires"] = float(expires)
        except (TypeError, ValueError):
            pass

        rest = getattr(item, "_rest", {}) or {}
        if any(str(key).lower() == "httponly" for key in rest):
            cookie["httpOnly"] = True
        same_site = next(
            (value for key, value in rest.items() if str(key).lower() == "samesite"),
            None,
        )
        normalized_same_site = str(same_site or "").strip().lower()
        if normalized_same_site in {"strict", "lax", "none"}:
            cookie["sameSite"] = normalized_same_site.title()
        result.append(cookie)
    return result


def merge_playwright_cookies_into_session(
    session: Any,
    cookies: list[dict[str, Any]],
    *,
    fallback_domain: str = "auth.openai.com",
) -> int:
    """Merge browser cookies back into the protocol session using their exact scope."""
    target = getattr(session, "cookies", None)
    setter = getattr(target, "set", None)
    if not callable(setter):
        return 0

    merged = 0
    seen: set[tuple[str, str, str, str]] = set()
    for item in cookies or []:
        name = str(item.get("name") or "").strip()
        value = item.get("value")
        if not name or value is None:
            continue
        domain = str(item.get("domain") or "").strip() or fallback_domain
        path = str(item.get("path") or "/").strip() or "/"
        key = (name, str(value), domain, path)
        if key in seen:
            continue
        seen.add(key)
        try:
            setter(
                name,
                str(value),
                domain=domain,
                path=path,
                secure=bool(item.get("secure")),
            )
            merged += 1
        except Exception:
            continue
    return merged


def _flow_page_url(flow: str) -> str:
    flow_name = str(flow or "").strip().lower()
    mapping = {
        "authorize_continue": "https://auth.openai.com/create-account",
        "username_password_create": "https://auth.openai.com/create-account/password",
        "password_verify": "https://auth.openai.com/log-in/password",
        "email_otp_validate": "https://auth.openai.com/email-verification",
        "oauth_create_account": "https://auth.openai.com/about-you",
    }
    return mapping.get(flow_name, "https://auth.openai.com/about-you")


def _sentinel_token_field_state(token: str) -> Optional[dict[str, bool]]:
    try:
        parsed = json.loads(str(token or ""))
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    return {key: bool(parsed.get(key)) for key in ("p", "t", "c")}


def _thread_has_running_asyncio_loop() -> bool:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    return bool(loop and loop.is_running())


def run_sync_playwright_safely(
    fn: Callable[[], Any],
    *,
    logger: Optional[Callable[[str], None]] = None,
    label: str = "Playwright Sync API",
) -> Any:
    """Run Playwright sync API outside an already-running asyncio loop.

    Playwright's sync API intentionally refuses to start in a thread that already
    owns a running asyncio loop.  FastAPI/uvicorn and some worker wrappers can put
    our otherwise-synchronous registration code in exactly that situation, so move
    only the Playwright sync section into a short-lived clean thread.
    """
    if not _thread_has_running_asyncio_loop():
        return fn()

    log = logger or (lambda _msg: None)
    log(f"{label}: 当前线程已有 asyncio loop，切换到隔离线程执行")
    result_box: dict[str, Any] = {}

    def _runner() -> None:
        try:
            result_box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - must propagate across thread boundary
            result_box["exc"] = exc

    thread = threading.Thread(target=_runner, name="sentinel-playwright-sync", daemon=True)
    thread.start()
    thread.join()
    if "exc" in result_box:
        raise result_box["exc"]
    return result_box.get("value")


def _evaluate_complete_sentinel_token(
    target: Any,
    *,
    flow: str,
    sdk_wait_timeout_ms: int,
    token_eval_timeout_ms: int,
    require_complete_signals: bool,
    logger: Callable[[str], None],
) -> Optional[str]:
    """Evaluate Sentinel from a top-level Page and validate the returned signals."""
    logger("Sentinel Browser 阶段: wait SentinelSDK ready")
    try:
        target.wait_for_function(
            "() => typeof window.SentinelSDK !== 'undefined' && typeof window.SentinelSDK.token === 'function'",
            timeout=sdk_wait_timeout_ms,
        )
    except Exception:
        logger("Sentinel Browser 未发现 SDK，注入当前固定版本")
        target.evaluate(
            """
            async (sdkUrl) => {
                const existing = Array.from(document.scripts || [])
                    .some((item) => item.src === sdkUrl);
                if (existing) return;
                await new Promise((resolve, reject) => {
                    const script = document.createElement('script');
                    script.src = sdkUrl;
                    script.async = true;
                    script.onload = () => resolve(true);
                    script.onerror = () => reject(new Error(`Failed to load ${sdkUrl}`));
                    document.head.appendChild(script);
                });
            }
            """,
            DEFAULT_SENTINEL_SDK_URL,
        )
        target.wait_for_function(
            "() => typeof window.SentinelSDK !== 'undefined' && typeof window.SentinelSDK.token === 'function'",
            timeout=sdk_wait_timeout_ms,
        )
    logger("Sentinel Browser 阶段完成: wait SentinelSDK ready")

    logger(f"Sentinel Browser 阶段: evaluate SentinelSDK.token({flow})")
    result = target.evaluate(
        """
        async ({ flow, timeoutMs }) => {
            try {
                if (window.top !== window) {
                    throw new Error('SentinelSDK must be called from a top-level page');
                }
                if (typeof window.SentinelSDK.init === 'function') {
                    await window.SentinelSDK.init(flow);
                }
                const token = await Promise.race([
                    window.SentinelSDK.token(flow),
                    new Promise((_, reject) =>
                        setTimeout(() => reject(new Error(`sentinel token timeout ${timeoutMs}ms`)), timeoutMs)
                    ),
                ]);
                return { success: true, token };
            } catch (e) {
                return {
                    success: false,
                    error: (e && (e.message || String(e))) || "unknown",
                };
            }
        }
        """,
        {
            "flow": flow,
            "timeoutMs": token_eval_timeout_ms,
        },
    )
    logger("Sentinel Browser 阶段完成: evaluate SentinelSDK.token")

    if not result or not result.get("success") or not result.get("token"):
        logger(
            "Sentinel Browser 获取失败: "
            + str((result or {}).get("error") or "no result")
        )
        return None

    token = str(result["token"] or "").strip()
    if not token:
        logger("Sentinel Browser 返回空 token")
        return None

    field_state = _sentinel_token_field_state(token)
    if field_state is None:
        logger(f"Sentinel Browser 成功: len={len(token)}")
        if require_complete_signals:
            logger("Sentinel Browser 令牌格式不可验证，拒绝降级使用")
            return None
        return token

    logger(
        "Sentinel Browser 成功: "
        f"p={'✓' if field_state['p'] else '✗'} "
        f"t={'✓' if field_state['t'] else '✗'} "
        f"c={'✓' if field_state['c'] else '✗'}"
    )
    if require_complete_signals and not all(field_state.values()):
        logger("Sentinel Browser 令牌缺少完整 p/t/c 信号，拒绝降级使用")
        return None
    return token


def get_sentinel_token_via_browser(
    *,
    flow: str,
    proxy: Optional[str] = None,
    timeout_ms: int = 45000,
    page_url: Optional[str] = None,
    headless: bool = True,
    device_id: Optional[str] = None,
    user_agent: Optional[str] = None,
    sec_ch_ua: Optional[str] = None,
    chrome_full_version: Optional[str] = None,
    accept_language: Optional[str] = None,
    platform_version: Optional[str] = None,
    viewport_width: Optional[int] = None,
    viewport_height: Optional[int] = None,
    cookie_header: Optional[str] = None,
    require_complete_signals: bool = False,
    stop_check: Optional[Callable[[], None]] = None,
    hard_timeout_seconds: Optional[float] = None,
    log_fn: Optional[Callable[[str], None]] = None,
) -> Optional[str]:
    """通过浏览器直接调用 SentinelSDK.token(flow) 获取完整 token。"""
    logger = log_fn or (lambda _msg: None)
    effective_hard_timeout = (
        max(float(hard_timeout_seconds), 0.1)
        if hard_timeout_seconds is not None
        else _browser_hard_timeout_seconds(
            "SENTINEL_BROWSER_HARD_TIMEOUT_SECONDS",
            _SENTINEL_BROWSER_HARD_TIMEOUT_DEFAULT_SECONDS,
        )
    )
    outcome = _run_with_browser_slot(
        "sentinel_token",
        {
            "flow": flow,
            "proxy": proxy,
            "timeout_ms": timeout_ms,
            "page_url": page_url,
            "headless": headless,
            "device_id": device_id,
            "user_agent": user_agent,
            "sec_ch_ua": sec_ch_ua,
            "chrome_full_version": chrome_full_version,
            "accept_language": accept_language,
            "platform_version": platform_version,
            "viewport_width": viewport_width,
            "viewport_height": viewport_height,
            "cookie_header": cookie_header,
            "require_complete_signals": require_complete_signals,
        },
        hard_timeout_seconds=effective_hard_timeout,
        logger=logger,
        stop_check=stop_check,
    )
    if outcome.status == "ok":
        return str(outcome.value or "").strip() or None
    logger(f"Sentinel Browser Worker 失败: {outcome.error}")
    return None


def _get_sentinel_token_via_browser_sync(
    *,
    flow: str,
    proxy: Optional[str] = None,
    timeout_ms: int = 45000,
    page_url: Optional[str] = None,
    headless: bool = True,
    device_id: Optional[str] = None,
    user_agent: Optional[str] = None,
    sec_ch_ua: Optional[str] = None,
    chrome_full_version: Optional[str] = None,
    accept_language: Optional[str] = None,
    platform_version: Optional[str] = None,
    viewport_width: Optional[int] = None,
    viewport_height: Optional[int] = None,
    cookie_header: Optional[str] = None,
    require_complete_signals: bool = False,
    log_fn: Optional[Callable[[str], None]] = None,
) -> Optional[str]:
    logger = log_fn or (lambda _msg: None)

    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        logger(f"Sentinel Browser 不可用: {e}")
        return None

    logical_page_url = str(page_url or _flow_page_url(flow)).strip() or _flow_page_url(flow)
    target_url = DEFAULT_SENTINEL_FRAME_URL
    effective_headless, reason = resolve_browser_headless(headless)
    ensure_browser_display_available(effective_headless)
    logger(
        f"Sentinel Browser 模式: {'headless' if effective_headless else 'headed'} ({reason})"
    )

    effective_user_agent = (
        user_agent
        or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{PINNED_CHROMIUM_VERSION} Safari/537.36"
    )
    effective_chrome_full = chrome_full_version or extract_chrome_full_version(effective_user_agent)
    effective_accept_language = str(accept_language or "en-US,en;q=0.9")
    effective_locale = (
        effective_accept_language.split(",", 1)[0].split(";", 1)[0].strip() or "en-US"
    )
    effective_platform_version = str(platform_version or "15.0.0").strip('"')
    effective_viewport_width = int(viewport_width or 1440)
    effective_viewport_height = int(viewport_height or 900)
    launch_timeout_ms = max(5000, min(int(timeout_ms or 45000), 20000))
    sdk_wait_timeout_ms = max(5000, min(int(timeout_ms or 45000), 15000))
    token_eval_timeout_ms = max(5000, min(int(timeout_ms or 45000), 15000))
    extra_http_headers = {
        "Accept-Language": effective_accept_language,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-ch-ua-arch": '"x86"',
        "sec-ch-ua-bitness": '"64"',
    }
    if sec_ch_ua:
        extra_http_headers["sec-ch-ua"] = sec_ch_ua
    if effective_chrome_full:
        extra_http_headers["sec-ch-ua-full-version"] = f'"{effective_chrome_full}"'
    if effective_platform_version:
        extra_http_headers["sec-ch-ua-platform-version"] = f'"{effective_platform_version}"'
    full_version_list = build_sec_ch_ua_full_version_list(sec_ch_ua, effective_chrome_full)
    if full_version_list:
        extra_http_headers["sec-ch-ua-full-version-list"] = full_version_list

    launch_args: dict[str, Any] = {
        "headless": effective_headless,
        "timeout": launch_timeout_ms,
        "args": [
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
        ],
    }
    logger(
        f"Sentinel Browser 启动: flow={flow}, page={logical_page_url}, frame={target_url}"
    )
    logger(
        "Sentinel Browser 参数: "
        f"launch_timeout={launch_timeout_ms}ms, "
        f"goto_timeout={int(timeout_ms or 45000)}ms, "
        f"sdk_wait_timeout={sdk_wait_timeout_ms}ms, "
        f"token_eval_timeout={token_eval_timeout_ms}ms"
    )

    browser = None
    page = None
    stage = "bootstrap"

    stack = ExitStack()
    try:
        proxy_config = stack.enter_context(
            playwright_proxy_context(proxy, logger=logger)
        )
        if proxy_config:
            launch_args["proxy"] = proxy_config
        p = stack.enter_context(sync_playwright())
    except Exception as exc:
        stack.close()
        logger(f"Sentinel Browser 异常(stage=proxy_setup): {exc}")
        return None

    with stack:
        try:
            stage = "launch"
            logger("Sentinel Browser 阶段: launch chromium")
            browser = p.chromium.launch(**launch_args)
            logger("Sentinel Browser 阶段完成: launch chromium")

            stage = "new_context"
            logger("Sentinel Browser 阶段: create context")
            context = browser.new_context(
                viewport={"width": effective_viewport_width, "height": effective_viewport_height},
                user_agent=effective_user_agent,
                locale=effective_locale,
                extra_http_headers=extra_http_headers,
                ignore_https_errors=True,
            )
            logger("Sentinel Browser 阶段完成: create context")
            cookie_names: set[str] = set()
            if cookie_header:
                cookie_items = []
                target_parts = urlsplit(logical_page_url)
                cookie_url = (
                    f"{target_parts.scheme or 'https'}://{target_parts.netloc}/"
                    if target_parts.netloc
                    else logical_page_url
                )
                for part in str(cookie_header or "").split(";"):
                    text = part.strip()
                    if not text or "=" not in text:
                        continue
                    name, _, value = text.partition("=")
                    name = name.strip()
                    if not name:
                        continue
                    cookie_names.add(name)
                    cookie_items.append(
                        {
                            "name": name,
                            "value": value.strip(),
                            "url": cookie_url,
                            "secure": cookie_url.startswith("https://"),
                            "sameSite": "Lax",
                        }
                    )
                if cookie_items:
                    try:
                        context.add_cookies(cookie_items)
                        logger(f"Sentinel Browser 阶段完成: add cookie_header cookies ({len(cookie_items)})")
                    except Exception as cookie_exc:
                        logger(f"Sentinel Browser add cookie_header 失败: {cookie_exc}")
            if device_id:
                try:
                    logical_parts = urlsplit(logical_page_url)
                    logical_cookie_url = (
                        f"{logical_parts.scheme or 'https'}://{logical_parts.netloc}/"
                        if logical_parts.netloc
                        else "https://auth.openai.com/"
                    )
                    device_cookies = [
                        {
                            "name": "oai-did",
                            "value": str(device_id),
                            "url": "https://sentinel.openai.com/",
                            "secure": True,
                            "sameSite": "Lax",
                        }
                    ]
                    if "oai-did" not in cookie_names:
                        device_cookies.append(
                            {
                                "name": "oai-did",
                                "value": str(device_id),
                                "url": logical_cookie_url,
                                "secure": True,
                                "sameSite": "Lax",
                            }
                        )
                    context.add_cookies(device_cookies)
                    logger("Sentinel Browser 阶段完成: add device cookies")
                except Exception as cookie_exc:
                    logger(f"Sentinel Browser add cookies 失败: {cookie_exc}")

            stage = "new_page"
            logger("Sentinel Browser 阶段: create page")
            page = context.new_page()
            page.set_default_timeout(int(timeout_ms or 45000))
            page.set_default_navigation_timeout(int(timeout_ms or 45000))
            logger("Sentinel Browser 阶段完成: create page")

            stage = "goto"
            logger(f"Sentinel Browser 阶段: page.goto -> {target_url}")
            page.goto(target_url, wait_until="load", timeout=int(timeout_ms or 45000))
            logger(f"Sentinel Browser 阶段完成: page.goto -> {page.url}")

            stage = "wait_sentinel_sdk"
            return _evaluate_complete_sentinel_token(
                page,
                flow=flow,
                sdk_wait_timeout_ms=sdk_wait_timeout_ms,
                token_eval_timeout_ms=token_eval_timeout_ms,
                require_complete_signals=require_complete_signals,
                logger=logger,
            )
        except Exception as e:
            current_url = ""
            if page is not None:
                try:
                    current_url = str(page.url or "")
                except Exception:
                    current_url = ""
            logger(
                f"Sentinel Browser 异常(stage={stage}): {e}"
                + (f" | current_url={current_url}" if current_url else "")
            )
            return None
        finally:
            if browser is not None:
                try:
                    browser.close()
                    logger("Sentinel Browser 阶段完成: browser.close")
                except Exception as close_exc:
                    logger(f"Sentinel Browser browser.close 异常: {close_exc}")


def _cookie_applies_to_host(cookie: dict[str, Any], host: str) -> bool:
    domain = str(cookie.get("domain") or "").strip().lstrip(".").lower()
    target = str(host or "").strip().lower()
    return bool(domain and target and (target == domain or target.endswith(f".{domain}")))


def _add_cookies_best_effort(
    context: Any,
    cookies: list[dict[str, Any]],
    *,
    logger: Callable[[str], None],
) -> int:
    allowed = {
        "name",
        "value",
        "url",
        "domain",
        "path",
        "expires",
        "httpOnly",
        "secure",
        "sameSite",
        "partitionKey",
    }
    added = 0
    for item in cookies or []:
        normalized = {key: value for key, value in item.items() if key in allowed}
        if not normalized.get("name") or normalized.get("value") is None:
            continue
        if normalized.get("url"):
            normalized.pop("domain", None)
            normalized.pop("path", None)
        elif not normalized.get("domain"):
            continue
        try:
            context.add_cookies([normalized])
            added += 1
        except Exception as exc:
            logger(
                "Auth Browser Cookie 导入跳过: "
                f"name={normalized.get('name')} domain={normalized.get('domain') or normalized.get('url')} "
                f"error={exc}"
            )
    return added


def run_browser_registration_stage(
    *,
    email: str,
    password: str,
    otp_callback: Callable[[], str],
    proxy: Optional[str] = None,
    device_id: str = "",
    headless: bool = True,
    cookies: Optional[list[dict[str, Any]]] = None,
    initial_state: Optional[dict[str, Any]] = None,
    stop_check: Optional[Callable[[], None]] = None,
    hard_timeout_seconds: Optional[float] = None,
    log_fn: Optional[Callable[[str], None]] = None,
) -> BrowserRegistrationStageResult:
    """Run browser registration in the shared, killable browser capacity gate."""

    logger = log_fn or (lambda _message: None)
    effective_hard_timeout = (
        max(float(hard_timeout_seconds), 0.1)
        if hard_timeout_seconds is not None
        else _browser_hard_timeout_seconds(
            "BROWSER_REGISTRATION_HARD_TIMEOUT_SECONDS",
            420.0,
        )
    )

    def _request_otp(callback_payload: dict[str, Any]) -> Any:
        # New callbacks receive the browser's send timestamp and exclusion
        # context; retain zero-argument compatibility for older integrations.
        try:
            value = otp_callback(dict(callback_payload or {}))
        except TypeError:
            value = otp_callback()
        return value

    outcome = _run_with_browser_slot(
        "browser_registration",
        {
            "email": str(email or ""),
            "password": str(password or ""),
            "proxy": str(proxy or "") or None,
            "device_id": str(device_id or ""),
            "headless": bool(headless),
            "cookies": list(cookies or []),
            "initial_state": dict(initial_state or {}),
        },
        hard_timeout_seconds=effective_hard_timeout,
        logger=logger,
        stop_check=stop_check,
        callbacks={"otp": _request_otp},
    )
    if outcome.status == "timeout":
        return BrowserRegistrationStageResult(
            error=f"browser_registration_hard_timeout: {outcome.error}"
        )
    if outcome.status != "ok":
        return BrowserRegistrationStageResult(
            error=f"browser_registration_unavailable: {outcome.error}"
        )
    if not isinstance(outcome.value, dict):
        return BrowserRegistrationStageResult(
            error="browser_registration_invalid_result"
        )
    payload = dict(outcome.value)
    payload["cookie_names"] = tuple(payload.get("cookie_names") or ())
    try:
        return BrowserRegistrationStageResult(**payload)
    except (TypeError, ValueError) as exc:
        return BrowserRegistrationStageResult(
            error=f"browser_registration_result_parse_failed: {exc}"
        )


def run_browser_oauth_token_recovery(
    *,
    email: str,
    password: str,
    otp_callback: Callable[[], str],
    proxy: Optional[str] = None,
    device_id: str = "",
    headless: bool = True,
    stop_check: Optional[Callable[[], None]] = None,
    hard_timeout_seconds: Optional[float] = None,
    log_fn: Optional[Callable[[str], None]] = None,
) -> BrowserOAuthTokenRecoveryResult:
    """Run the fresh-browser Codex OAuth recovery behind the shared browser gate."""

    logger = log_fn or (lambda _message: None)
    effective_hard_timeout = (
        max(float(hard_timeout_seconds), 0.1)
        if hard_timeout_seconds is not None
        else _browser_hard_timeout_seconds(
            "BROWSER_OAUTH_HARD_TIMEOUT_SECONDS",
            420.0,
        )
    )

    def _request_otp(callback_payload: dict[str, Any]) -> Any:
        try:
            return otp_callback(dict(callback_payload or {}))
        except TypeError:
            return otp_callback()

    outcome = _run_with_browser_slot(
        "browser_oauth_token_recovery",
        {
            "email": str(email or ""),
            "password": str(password or ""),
            "proxy": str(proxy or "") or None,
            "device_id": str(device_id or ""),
            "headless": bool(headless),
        },
        hard_timeout_seconds=effective_hard_timeout,
        logger=logger,
        stop_check=stop_check,
        callbacks={"otp": _request_otp},
    )
    if outcome.status == "timeout":
        return BrowserOAuthTokenRecoveryResult(
            error=f"browser_oauth_token_recovery_hard_timeout: {outcome.error}"
        )
    if outcome.status != "ok":
        return BrowserOAuthTokenRecoveryResult(
            error=f"browser_oauth_token_recovery_unavailable: {outcome.error}"
        )
    if not isinstance(outcome.value, dict):
        return BrowserOAuthTokenRecoveryResult(
            error="browser_oauth_token_recovery_invalid_result"
        )
    payload = dict(outcome.value)
    error = str(payload.pop("error", "") or "").strip()
    if error:
        return BrowserOAuthTokenRecoveryResult(error=error)
    return BrowserOAuthTokenRecoveryResult(tokens=payload)


def create_account_via_browser(
    *,
    name: str,
    birthdate: str,
    proxy: Optional[str] = None,
    page_url: str = "https://auth.openai.com/about-you",
    timeout_ms: int = 45000,
    headless: bool = True,
    device_id: Optional[str] = None,
    user_agent: Optional[str] = None,
    sec_ch_ua: Optional[str] = None,
    chrome_full_version: Optional[str] = None,
    accept_language: Optional[str] = None,
    platform_version: Optional[str] = None,
    viewport_width: Optional[int] = None,
    viewport_height: Optional[int] = None,
    cookies: Optional[list[dict[str, Any]]] = None,
    trace_headers: Optional[dict[str, str]] = None,
    stop_check: Optional[Callable[[], None]] = None,
    hard_timeout_seconds: Optional[float] = None,
    log_fn: Optional[Callable[[str], None]] = None,
) -> Optional[BrowserAccountCreateResult]:
    """Load Auth, obtain Sentinel, and submit create_account in one browser context."""
    logger = log_fn or (lambda _msg: None)
    effective_hard_timeout = (
        max(float(hard_timeout_seconds), 0.1)
        if hard_timeout_seconds is not None
        else _browser_hard_timeout_seconds(
            "AUTH_BROWSER_HARD_TIMEOUT_SECONDS",
            _AUTH_BROWSER_HARD_TIMEOUT_DEFAULT_SECONDS,
        )
    )
    outcome = _run_with_browser_slot(
        "create_account",
        {
            "name": name,
            "birthdate": birthdate,
            "proxy": proxy,
            "page_url": page_url,
            "timeout_ms": timeout_ms,
            "headless": headless,
            "device_id": device_id,
            "user_agent": user_agent,
            "sec_ch_ua": sec_ch_ua,
            "chrome_full_version": chrome_full_version,
            "accept_language": accept_language,
            "platform_version": platform_version,
            "viewport_width": viewport_width,
            "viewport_height": viewport_height,
            "cookies": list(cookies or []),
            "trace_headers": dict(trace_headers or {}),
        },
        hard_timeout_seconds=effective_hard_timeout,
        logger=logger,
        stop_check=stop_check,
    )
    if outcome.status == "timeout":
        return BrowserAccountCreateResult(error=f"auth_browser_hard_timeout: {outcome.error}")
    if outcome.status != "ok":
        logger(f"Auth Browser Worker 失败: {outcome.error}")
        return None
    if outcome.value is None:
        return None
    if not isinstance(outcome.value, dict):
        logger("Auth Browser Worker 返回结果格式错误")
        return None
    result_payload = dict(outcome.value)
    result_payload["cookie_names"] = tuple(result_payload.get("cookie_names") or ())
    try:
        return BrowserAccountCreateResult(**result_payload)
    except (TypeError, ValueError) as exc:
        logger(f"Auth Browser Worker 结果解析失败: {exc}")
        return None


def _create_account_via_browser_sync(
    *,
    name: str,
    birthdate: str,
    proxy: Optional[str],
    page_url: str,
    timeout_ms: int,
    headless: bool,
    device_id: Optional[str],
    user_agent: Optional[str],
    sec_ch_ua: Optional[str],
    chrome_full_version: Optional[str],
    accept_language: Optional[str],
    platform_version: Optional[str],
    viewport_width: Optional[int],
    viewport_height: Optional[int],
    cookies: Optional[list[dict[str, Any]]],
    trace_headers: Optional[dict[str, str]],
    stop_check: Optional[Callable[[], None]],
    log_fn: Optional[Callable[[str], None]],
) -> Optional[BrowserAccountCreateResult]:
    logger = log_fn or (lambda _msg: None)
    check_stop = stop_check or (lambda: None)
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        logger(f"Auth Browser 不可用: {exc}")
        return None

    logical_page_url = str(page_url or "https://auth.openai.com/about-you").strip()
    effective_headless, reason = resolve_browser_headless(headless)
    ensure_browser_display_available(effective_headless)
    effective_user_agent = (
        user_agent
        or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{PINNED_CHROMIUM_VERSION} Safari/537.36"
    )
    effective_chrome_full = chrome_full_version or extract_chrome_full_version(
        effective_user_agent
    )
    effective_accept_language = str(accept_language or "en-US,en;q=0.9")
    effective_locale = (
        effective_accept_language.split(",", 1)[0].split(";", 1)[0].strip() or "en-US"
    )
    effective_platform_version = str(platform_version or "15.0.0").strip('"')
    effective_viewport_width = int(viewport_width or 1440)
    effective_viewport_height = int(viewport_height or 900)
    effective_timeout_ms = max(10000, int(timeout_ms or 45000))
    sdk_wait_timeout_ms = max(5000, min(effective_timeout_ms, 15000))
    token_eval_timeout_ms = max(5000, min(effective_timeout_ms, 15000))

    extra_http_headers = {
        "Accept-Language": effective_accept_language,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-ch-ua-arch": '"x86"',
        "sec-ch-ua-bitness": '"64"',
    }
    if sec_ch_ua:
        extra_http_headers["sec-ch-ua"] = sec_ch_ua
    if effective_chrome_full:
        extra_http_headers["sec-ch-ua-full-version"] = f'"{effective_chrome_full}"'
    if effective_platform_version:
        extra_http_headers["sec-ch-ua-platform-version"] = (
            f'"{effective_platform_version}"'
        )
    full_version_list = build_sec_ch_ua_full_version_list(
        sec_ch_ua, effective_chrome_full
    )
    if full_version_list:
        extra_http_headers["sec-ch-ua-full-version-list"] = full_version_list

    launch_args: dict[str, Any] = {
        "headless": effective_headless,
        "timeout": min(effective_timeout_ms, 20000),
        "args": ["--no-sandbox", "--disable-blink-features=AutomationControlled"],
    }
    logger(
        "Auth Browser 开户启动: "
        f"page={logical_page_url}, mode={'headless' if effective_headless else 'headed'} ({reason})"
    )

    browser = None
    page = None
    stage = "bootstrap"
    with ExitStack() as stack:
        try:
            proxy_config = stack.enter_context(
                playwright_proxy_context(proxy, logger=logger)
            )
            if proxy_config:
                launch_args["proxy"] = proxy_config
            p = stack.enter_context(sync_playwright())

            check_stop()
            stage = "launch"
            browser = p.chromium.launch(**launch_args)
            context = browser.new_context(
                viewport={
                    "width": effective_viewport_width,
                    "height": effective_viewport_height,
                },
                user_agent=effective_user_agent,
                locale=effective_locale,
                extra_http_headers=extra_http_headers,
                ignore_https_errors=True,
            )

            cookie_payload = list(cookies or [])
            if device_id:
                has_auth_device = any(
                    str(item.get("name") or "") == "oai-did"
                    and _cookie_applies_to_host(item, "auth.openai.com")
                    for item in cookie_payload
                )
                has_sentinel_device = any(
                    str(item.get("name") or "") == "oai-did"
                    and _cookie_applies_to_host(item, "sentinel.openai.com")
                    for item in cookie_payload
                )
                if not has_auth_device:
                    cookie_payload.append(
                        {
                            "name": "oai-did",
                            "value": str(device_id),
                            "url": "https://auth.openai.com/",
                            "secure": True,
                            "sameSite": "Lax",
                        }
                    )
                if not has_sentinel_device:
                    cookie_payload.append(
                        {
                            "name": "oai-did",
                            "value": str(device_id),
                            "url": "https://sentinel.openai.com/",
                            "secure": True,
                            "sameSite": "Lax",
                        }
                    )
            imported = _add_cookies_best_effort(
                context, cookie_payload, logger=logger
            )
            logger(f"Auth Browser 已按域导入 cookies={imported}")

            stage = "new_page"
            page = context.new_page()
            page.set_default_timeout(effective_timeout_ms)
            page.set_default_navigation_timeout(effective_timeout_ms)
            jsd_state = {"seen": False, "status": 0}

            def _observe_response(response: Any) -> None:
                try:
                    response_url = str(response.url or "")
                    if "/cdn-cgi/challenge-platform/" in response_url:
                        jsd_state["seen"] = True
                        jsd_state["status"] = int(response.status or 0)
                except Exception:
                    return

            page.on("response", _observe_response)

            stage = "goto_auth_about_you"
            check_stop()
            page.goto(
                logical_page_url,
                wait_until="domcontentloaded",
                timeout=effective_timeout_ms,
            )
            final_url = str(page.url or "")
            if urlsplit(final_url).hostname != "auth.openai.com":
                return BrowserAccountCreateResult(
                    response_url=final_url,
                    cookies=list(context.cookies()),
                    error=f"auth_about_you_redirected: {final_url[:240]}",
                )

            stage = "wait_auth_browser_signals"
            cf_clearance_present = False
            for _ in range(16):
                check_stop()
                current_cookies = list(context.cookies())
                cf_clearance_present = any(
                    str(item.get("name") or "") == "cf_clearance"
                    and str(item.get("value") or "").strip()
                    for item in current_cookies
                )
                if cf_clearance_present:
                    break
                page.wait_for_timeout(500)
            logger(
                "Auth Browser 页面信号: "
                f"cf_jsd={'✓' if jsd_state['seen'] and jsd_state['status'] < 400 else '✗'} "
                f"cf_clearance={'✓' if cf_clearance_present else '✗'}"
            )

            stage = "sentinel_token"
            token = _evaluate_complete_sentinel_token(
                page,
                flow="oauth_create_account",
                sdk_wait_timeout_ms=sdk_wait_timeout_ms,
                token_eval_timeout_ms=token_eval_timeout_ms,
                require_complete_signals=True,
                logger=logger,
            )
            if not token:
                return BrowserAccountCreateResult(
                    response_url=final_url,
                    cookies=list(context.cookies()),
                    cf_clearance_present=cf_clearance_present,
                    error="sentinel_browser_unavailable",
                )

            parsed_token = json.loads(token)
            field_lengths = {
                key: len(str(parsed_token.get(key) or "")) for key in ("p", "t", "c")
            }
            before_create_cookies = list(context.cookies())
            before_cookie_names = tuple(
                sorted(
                    {
                        str(item.get("name") or "")
                        for item in before_create_cookies
                        if item.get("name")
                    }
                )
            )
            oai_sc_present = "oai-sc" in before_cookie_names
            logger(
                "Auth Browser 开户前上下文: "
                f"cookies={','.join(before_cookie_names)} "
                f"sentinel_lengths={field_lengths}"
            )

            stage = "create_account_fetch"
            check_stop()
            allowed_trace_headers = {
                str(key): str(value)
                for key, value in (trace_headers or {}).items()
                if str(key).lower()
                in {
                    "traceparent",
                    "tracestate",
                    "x-datadog-origin",
                    "x-datadog-parent-id",
                    "x-datadog-sampling-priority",
                    "x-datadog-trace-id",
                }
            }
            fetch_result = page.evaluate(
                """
                async ({ token, name, birthdate, traceHeaders, invocationId, timeoutMs }) => {
                    const controller = new AbortController();
                    const timer = setTimeout(() => controller.abort(), timeoutMs);
                    try {
                        const response = await fetch('/api/accounts/create_account', {
                            method: 'POST',
                            credentials: 'include',
                            cache: 'no-store',
                            redirect: 'manual',
                            signal: controller.signal,
                            headers: {
                                accept: 'application/json',
                                'content-type': 'application/json',
                                'openai-sentinel-token': token,
                                'x-access-flow-invocation-id': invocationId,
                                ...traceHeaders,
                            },
                            body: JSON.stringify({ name, birthdate }),
                        });
                        return {
                            status: response.status,
                            url: response.url,
                            text: await response.text(),
                        };
                    } catch (error) {
                        return {
                            status: 0,
                            url: location.href,
                            text: '',
                            error: (error && (error.message || String(error))) || 'unknown',
                        };
                    } finally {
                        clearTimeout(timer);
                    }
                }
                """,
                {
                    "token": token,
                    "name": str(name or "").strip(),
                    "birthdate": str(birthdate or "").strip(),
                    "traceHeaders": allowed_trace_headers,
                    "invocationId": str(uuid.uuid4()),
                    "timeoutMs": effective_timeout_ms,
                },
            )
            page.wait_for_timeout(250)
            response_status = int((fetch_result or {}).get("status") or 0)
            response_text = str((fetch_result or {}).get("text") or "")
            response_url = str((fetch_result or {}).get("url") or final_url)
            response_error = str((fetch_result or {}).get("error") or "")
            response_json: dict[str, Any] = {}
            try:
                parsed_response = json.loads(response_text or "{}")
                if isinstance(parsed_response, dict):
                    response_json = parsed_response
            except (TypeError, ValueError):
                pass
            final_cookies = list(context.cookies())
            final_cookie_names = tuple(
                sorted(
                    {
                        str(item.get("name") or "")
                        for item in final_cookies
                        if item.get("name")
                    }
                )
            )
            logger(
                "Auth Browser create_account 完成: "
                f"status={response_status} cookies={','.join(final_cookie_names)}"
            )
            return BrowserAccountCreateResult(
                status_code=response_status,
                response_url=response_url,
                response_text=response_text,
                response_json=response_json,
                cookies=final_cookies,
                cookie_names=final_cookie_names,
                sentinel_field_lengths=field_lengths,
                cf_clearance_present=cf_clearance_present,
                oai_sc_present=oai_sc_present,
                error=response_error,
            )
        except Exception as exc:
            if stop_check is not None:
                try:
                    stop_check()
                except Exception:
                    raise
            current_url = ""
            if page is not None:
                try:
                    current_url = str(page.url or "")
                except Exception:
                    current_url = ""
            logger(
                f"Auth Browser 开户异常(stage={stage}): {exc}"
                + (f" | current_url={current_url}" if current_url else "")
            )
            cookies_now: list[dict[str, Any]] = []
            try:
                if page is not None:
                    cookies_now = list(page.context.cookies())
            except Exception:
                pass
            return BrowserAccountCreateResult(
                response_url=current_url,
                cookies=cookies_now,
                error=f"{stage}: {exc}",
            )
        finally:
            if browser is not None:
                try:
                    browser.close()
                    logger("Auth Browser 阶段完成: browser.close")
                except Exception as close_exc:
                    logger(f"Auth Browser browser.close 异常: {close_exc}")
