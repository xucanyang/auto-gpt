"""Shared Camoufox process with isolated Playwright browser contexts.

The synchronous registration state machines run in different threads and, for
killable transactions, different worker processes. Playwright's hidden
``launch-server`` command lets each worker own its driver connection while all
connections use one Camoufox process. A manager connection pre-creates each
incognito context serially; workers claim only the context selected by their
random marker token and can then drive those contexts concurrently.
"""

from __future__ import annotations

import atexit
import ipaddress
import json
import os
import selectors
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from collections import deque
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Optional
from urllib.parse import urlsplit

from core.playwright_proxy import playwright_proxy_context


SHARED_CAMOUFOX_ENDPOINT_ENV = "AUTO_GPT_SHARED_CAMOUFOX_WS_ENDPOINT"
SHARED_CAMOUFOX_MODE_ENV = "AUTO_GPT_SHARED_CAMOUFOX_MODE"
SHARED_CAMOUFOX_CONTEXT_TOKEN_ENV = "AUTO_GPT_SHARED_CAMOUFOX_CONTEXT_TOKEN"

_SERVER_START_TIMEOUT_SECONDS = 45.0
_SERVER_STOP_TIMEOUT_SECONDS = 5.0
_CONNECT_TIMEOUT_MS = 30_000


def _mode_name(headless: bool) -> str:
    return "headless" if headless else "headed"


def camoufox_executable_options() -> dict[str, Any]:
    """Resolve an installed Camoufox binary without triggering downloads."""

    configured = str(os.environ.get("CAMOUFOX_EXECUTABLE_PATH") or "").strip()
    if configured:
        path = Path(configured).expanduser()
        if not path.is_file() or not os.access(path, os.X_OK):
            raise RuntimeError(
                f"CAMOUFOX_EXECUTABLE_PATH does not exist or is not executable: {path}"
            )
        options: dict[str, Any] = {"executable_path": str(path)}
        try:
            metadata = json.loads(
                (path.parent / "version.json").read_text(encoding="utf-8")
            )
            major = int(str(metadata.get("version") or "").split(".", 1)[0])
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            major = 0
        if major > 0:
            options.update({"ff_version": major, "i_know_what_im_doing": True})
        return options

    try:
        from camoufox.pkgman import INSTALL_DIR
    except Exception:
        return {}

    legacy_path = Path(INSTALL_DIR) / "camoufox-bin"
    if not legacy_path.is_file() or not os.access(legacy_path, os.X_OK):
        return {}

    options = {"executable_path": str(legacy_path)}
    try:
        metadata = json.loads(
            (legacy_path.parent / "version.json").read_text(encoding="utf-8")
        )
        major = int(str(metadata.get("version") or "").split(".", 1)[0])
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        major = 0
    if major > 0:
        options.update({"ff_version": major, "i_know_what_im_doing": True})
    return options


def _idle_timeout_seconds() -> float:
    try:
        value = float(
            os.environ.get("SHARED_CAMOUFOX_IDLE_TIMEOUT_SECONDS", "300") or 300
        )
    except (TypeError, ValueError):
        value = 300.0
    return max(0.0, min(value, 3600.0))


def _server_launch_config(headless: bool) -> dict[str, Any]:
    from camoufox import DefaultAddons
    from camoufox.utils import launch_options

    options = launch_options(
        headless=bool(headless),
        block_webrtc=True,
        exclude_addons=[DefaultAddons.UBO],
        **camoufox_executable_options(),
    )
    environment = {
        str(key): str(value)
        for key, value in dict(options.get("env") or {}).items()
    }
    return {
        "executablePath": str(options["executable_path"]),
        "args": list(options.get("args") or []),
        "env": environment,
        "firefoxUserPrefs": dict(options.get("firefox_user_prefs") or {}),
        "headless": bool(options.get("headless", headless)),
        "host": "127.0.0.1",
        "port": 0,
        # Multiple remote clients must see the pre-created contexts. Context
        # routing uses an unguessable marker page plus explicit cleanup rather
        # than Playwright's per-connection dispatcher, whose Firefox
        # implementation deadlocks during concurrent page creation.
        "_sharedBrowser": True,
    }


@dataclass
class _ServerState:
    headless: bool
    process: subprocess.Popen[str]
    endpoint: str
    generation: int
    started_at: float
    leases: int = 0
    stderr_tail: deque[str] = field(default_factory=lambda: deque(maxlen=40))
    idle_timer: Optional[threading.Timer] = None
    allocation_lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass(frozen=True)
class SharedCamoufoxContextAllocation:
    endpoint: str
    token: str
    headless: bool


@dataclass(frozen=True)
class SharedCamoufoxContextSession:
    browser: Any
    context: Any
    page: Any
    token: str


class SharedCamoufoxServerManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._states: dict[bool, _ServerState] = {}
        self._generation = 0

    @staticmethod
    def _is_alive(state: Optional[_ServerState]) -> bool:
        return bool(state is not None and state.process.poll() is None)

    @staticmethod
    def _terminate_process(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                process.terminate()
            except ProcessLookupError:
                return
        try:
            process.wait(timeout=_SERVER_STOP_TIMEOUT_SECONDS)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                process.kill()
            except ProcessLookupError:
                pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass

    @staticmethod
    def _drain_stderr(state: _ServerState) -> None:
        stream = state.process.stderr
        if stream is None:
            return
        try:
            for line in stream:
                text = str(line or "").strip()
                if text:
                    state.stderr_tail.append(text[:1000])
        except Exception:
            return

    def _stop_state_locked(self, headless: bool) -> None:
        state = self._states.pop(bool(headless), None)
        if state is None:
            return
        if state.idle_timer is not None:
            state.idle_timer.cancel()
            state.idle_timer = None
        self._terminate_process(state.process)

    def _read_endpoint(self, state: _ServerState) -> str:
        stream = state.process.stdout
        if stream is None:
            raise RuntimeError("shared Camoufox server stdout is unavailable")
        selector = selectors.DefaultSelector()
        selector.register(stream, selectors.EVENT_READ)
        deadline = time.monotonic() + _SERVER_START_TIMEOUT_SECONDS
        try:
            while time.monotonic() < deadline:
                if state.process.poll() is not None:
                    detail = " | ".join(state.stderr_tail)
                    raise RuntimeError(
                        "shared Camoufox server exited during startup"
                        + (f": {detail}" if detail else "")
                    )
                events = selector.select(timeout=0.2)
                if not events:
                    continue
                endpoint = str(stream.readline() or "").strip()
                parsed = urlsplit(endpoint)
                if parsed.scheme in {"ws", "wss"} and parsed.hostname in {
                    "127.0.0.1",
                    "localhost",
                    "::1",
                }:
                    return endpoint
                if endpoint:
                    state.stderr_tail.append(f"unexpected stdout: {endpoint[:500]}")
        finally:
            selector.close()
        detail = " | ".join(state.stderr_tail)
        raise RuntimeError(
            "shared Camoufox server startup timed out"
            + (f": {detail}" if detail else "")
        )

    def _start_locked(self, headless: bool) -> _ServerState:
        from playwright._impl._driver import compute_driver_executable

        config = _server_launch_config(bool(headless))
        node, cli = compute_driver_executable()
        config_path = ""
        process: Optional[subprocess.Popen[str]] = None
        try:
            fd, config_path = tempfile.mkstemp(
                prefix=f"auto-gpt-camoufox-{_mode_name(headless)}-",
                suffix=".json",
            )
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(config, handle, ensure_ascii=True, separators=(",", ":"))
            process = subprocess.Popen(
                [
                    str(node),
                    str(cli),
                    "launch-server",
                    "--browser",
                    "firefox",
                    "--config",
                    config_path,
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                start_new_session=True,
                close_fds=True,
                env=dict(os.environ),
            )
            self._generation += 1
            state = _ServerState(
                headless=bool(headless),
                process=process,
                endpoint="",
                generation=self._generation,
                started_at=time.time(),
            )
            threading.Thread(
                target=self._drain_stderr,
                args=(state,),
                name=f"shared-camoufox-stderr-{_mode_name(headless)}",
                daemon=True,
            ).start()
            state.endpoint = self._read_endpoint(state)
            self._states[bool(headless)] = state
            return state
        except BaseException:
            if process is not None:
                self._terminate_process(process)
            raise
        finally:
            if config_path:
                try:
                    os.unlink(config_path)
                except OSError:
                    pass

    def _ensure_locked(self, headless: bool) -> tuple[_ServerState, bool]:
        key = bool(headless)
        state = self._states.get(key)
        if self._is_alive(state):
            return state, False
        if state is not None:
            self._stop_state_locked(key)
        return self._start_locked(key), True

    def _expire_idle(self, headless: bool, generation: int) -> None:
        with self._lock:
            state = self._states.get(bool(headless))
            if (
                state is None
                or state.generation != generation
                or state.leases > 0
            ):
                return
            self._stop_state_locked(bool(headless))

    @contextmanager
    def lease(
        self,
        headless: bool,
        *,
        logger: Optional[Callable[[str], None]] = None,
    ) -> Iterator[str]:
        log = logger or (lambda _message: None)
        with self._lock:
            state, started = self._ensure_locked(bool(headless))
            if state.idle_timer is not None:
                state.idle_timer.cancel()
                state.idle_timer = None
            state.leases += 1
            endpoint = state.endpoint
            generation = state.generation
            pid = state.process.pid
            leases = state.leases
        log(
            "[control] shared_camoufox=leased "
            f"mode={_mode_name(headless)} started={'true' if started else 'false'} "
            f"pid={pid} active_contexts={leases}"
        )
        try:
            yield endpoint
        finally:
            with self._lock:
                current = self._states.get(bool(headless))
                if current is not None and current.generation == generation:
                    current.leases = max(0, current.leases - 1)
                    if current.leases == 0:
                        timeout = _idle_timeout_seconds()
                        timer = threading.Timer(
                            timeout,
                            self._expire_idle,
                            args=(bool(headless), generation),
                        )
                        timer.name = (
                            f"shared-camoufox-idle-{_mode_name(headless)}"
                        )
                        timer.daemon = True
                        current.idle_timer = timer
                        timer.start()

    @staticmethod
    def _marker_url(token: str) -> str:
        return f"about:blank#auto-gpt-context-{token}"

    @classmethod
    def _find_context(cls, browser: Any, token: str) -> tuple[Any, Any] | None:
        marker_url = cls._marker_url(token)
        for context in list(browser.contexts):
            pages = list(context.pages)
            if not any(str(page.url or "") == marker_url for page in pages):
                continue
            work_page = next(
                (page for page in pages if str(page.url or "") != marker_url),
                None,
            )
            if work_page is not None:
                return context, work_page
        return None

    def _allocate_context(
        self,
        state: _ServerState,
        *,
        context_options: dict[str, Any],
    ) -> str:
        from playwright.sync_api import sync_playwright

        token = uuid.uuid4().hex
        context = None
        with sync_playwright() as playwright:
            browser = playwright.firefox.connect(
                state.endpoint,
                timeout=_CONNECT_TIMEOUT_MS,
            )
            try:
                context = browser.new_context(**dict(context_options or {}))
                marker_page = context.new_page()
                marker_page.goto(self._marker_url(token), timeout=5000)
                context.new_page()
            except BaseException:
                if context is not None:
                    try:
                        context.close()
                    except Exception:
                        pass
                raise
            finally:
                browser.close()
        return token

    def _cleanup_context(self, endpoint: str, token: str) -> bool:
        from playwright.sync_api import sync_playwright

        for attempt in range(2):
            try:
                with sync_playwright() as playwright:
                    browser = playwright.firefox.connect(
                        endpoint,
                        timeout=min(_CONNECT_TIMEOUT_MS, 5000),
                    )
                    try:
                        found = self._find_context(browser, token)
                        if found is not None:
                            context, _page = found
                            context.close()
                    finally:
                        browser.close()
                return True
            except Exception:
                if attempt == 0:
                    time.sleep(0.1)
        # A crashed shared browser has already discarded every context. If the
        # server is still alive, idle process cleanup remains the final guard.
        return False

    @contextmanager
    def context_lease(
        self,
        headless: bool,
        *,
        context_options: Optional[dict[str, Any]] = None,
        logger: Optional[Callable[[str], None]] = None,
    ) -> Iterator[SharedCamoufoxContextAllocation]:
        log = logger or (lambda _message: None)
        with self.lease(bool(headless), logger=log) as endpoint:
            with self._lock:
                state = self._states.get(bool(headless))
                if state is None or state.endpoint != endpoint:
                    raise RuntimeError("shared Camoufox server changed before allocation")
                allocation_lock = state.allocation_lock
            with allocation_lock:
                token = self._allocate_context(
                    state,
                    context_options=dict(context_options or {}),
                )
            log(
                "[control] shared_camoufox_context=allocated "
                f"mode={_mode_name(headless)} token={token[:8]}"
            )
            try:
                yield SharedCamoufoxContextAllocation(
                    endpoint=endpoint,
                    token=token,
                    headless=bool(headless),
                )
            finally:
                with allocation_lock:
                    cleaned = self._cleanup_context(endpoint, token)
                if not cleaned and self.is_running(bool(headless)):
                    log(
                        "[control] shared_camoufox_context=cleanup_deferred "
                        f"mode={_mode_name(headless)} token={token[:8]}"
                    )

    def is_running(self, headless: bool) -> bool:
        with self._lock:
            return self._is_alive(self._states.get(bool(headless)))

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            servers: dict[str, Any] = {}
            for headless in (True, False):
                state = self._states.get(headless)
                running = self._is_alive(state)
                servers[_mode_name(headless)] = {
                    "running": running,
                    "pid": state.process.pid if running and state is not None else None,
                    "active_contexts": state.leases if state is not None else 0,
                    "generation": state.generation if state is not None else 0,
                    "uptime_seconds": (
                        max(0.0, now - state.started_at)
                        if running and state is not None
                        else 0.0
                    ),
                }
        return {
            "mode": "single_process_multi_context",
            "storage_scope": "browser_context",
            "proxy_scope": "browser_context",
            "fingerprint_scope": "browser_process",
            "webrtc": "blocked",
            "idle_timeout_seconds": _idle_timeout_seconds(),
            "servers": servers,
        }

    def close(self) -> None:
        with self._lock:
            for headless in list(self._states):
                self._stop_state_locked(headless)


_MANAGER = SharedCamoufoxServerManager()
atexit.register(_MANAGER.close)


def shared_camoufox_server_running(headless: bool) -> bool:
    return _MANAGER.is_running(bool(headless))


def shared_camoufox_runtime_snapshot() -> dict[str, Any]:
    return _MANAGER.snapshot()


@contextmanager
def shared_camoufox_server_lease(
    headless: bool,
    *,
    logger: Optional[Callable[[str], None]] = None,
) -> Iterator[str]:
    with _MANAGER.lease(bool(headless), logger=logger) as endpoint:
        yield endpoint


@contextmanager
def shared_camoufox_preallocated_context_lease(
    headless: bool,
    *,
    context_options: Optional[dict[str, Any]] = None,
    logger: Optional[Callable[[str], None]] = None,
) -> Iterator[SharedCamoufoxContextAllocation]:
    with _MANAGER.context_lease(
        bool(headless),
        context_options=context_options,
        logger=logger,
    ) as allocation:
        yield allocation


def bind_shared_camoufox_worker_environment(
    environment: dict[str, str],
    *,
    endpoint: str,
    headless: bool,
    context_token: str,
) -> None:
    environment[SHARED_CAMOUFOX_ENDPOINT_ENV] = str(endpoint)
    environment[SHARED_CAMOUFOX_MODE_ENV] = _mode_name(bool(headless))
    environment[SHARED_CAMOUFOX_CONTEXT_TOKEN_ENV] = str(context_token)


@contextmanager
def shared_camoufox_context_session(
    *,
    headless: bool,
    context_options: Optional[dict[str, Any]] = None,
    logger: Optional[Callable[[str], None]] = None,
) -> Iterator[SharedCamoufoxContextSession]:
    """Yield the context/page reserved for exactly one registration worker."""

    from playwright.sync_api import sync_playwright

    log = logger or (lambda _message: None)
    inherited_endpoint = str(
        os.environ.get(SHARED_CAMOUFOX_ENDPOINT_ENV) or ""
    ).strip()
    inherited_token = str(
        os.environ.get(SHARED_CAMOUFOX_CONTEXT_TOKEN_ENV) or ""
    ).strip()
    inherited_mode = str(os.environ.get(SHARED_CAMOUFOX_MODE_ENV) or "").strip()
    expected_mode = _mode_name(bool(headless))
    if bool(inherited_endpoint) != bool(inherited_token):
        raise RuntimeError("incomplete shared Camoufox worker allocation")
    if inherited_endpoint and inherited_mode and inherited_mode != expected_mode:
        raise RuntimeError(
            "shared Camoufox worker mode mismatch: "
            f"expected={expected_mode} inherited={inherited_mode}"
        )

    if inherited_endpoint:
        allocation_cm: Any = _static_context_allocation(
            SharedCamoufoxContextAllocation(
                endpoint=inherited_endpoint,
                token=inherited_token,
                headless=bool(headless),
            )
        )
    else:
        allocation_cm = _MANAGER.context_lease(
            bool(headless),
            context_options=dict(context_options or {}),
            logger=log,
        )

    with allocation_cm as allocation:
        playwright = sync_playwright().start()
        browser = None
        context = None
        try:
            browser = playwright.firefox.connect(
                allocation.endpoint,
                timeout=_CONNECT_TIMEOUT_MS,
            )
            found = _MANAGER._find_context(browser, allocation.token)
            if found is None:
                raise RuntimeError(
                    "preallocated shared Camoufox context was not found"
                )
            context, page = found
            log(
                "[control] shared_camoufox=connected "
                f"mode={expected_mode} isolation=browser_context "
                f"token={allocation.token[:8]}"
            )
            yield SharedCamoufoxContextSession(
                browser=browser,
                context=context,
                page=page,
                token=allocation.token,
            )
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
            if browser is not None:
                try:
                    browser.close()
                except Exception as exc:
                    log(
                        "[control] shared_camoufox=disconnect_error "
                        f"error={type(exc).__name__}"
                    )
            try:
                playwright.stop()
            except Exception:
                pass


@contextmanager
def shared_camoufox_registration_session(
    *,
    headless: bool,
    proxy: Optional[str] = None,
    extra_context_options: Optional[dict[str, Any]] = None,
    logger: Optional[Callable[[str], None]] = None,
) -> Iterator[SharedCamoufoxContextSession]:
    """Allocate or claim one proxy-isolated registration context."""

    inherited = bool(
        str(os.environ.get(SHARED_CAMOUFOX_ENDPOINT_ENV) or "").strip()
        and str(os.environ.get(SHARED_CAMOUFOX_CONTEXT_TOKEN_ENV) or "").strip()
    )
    with ExitStack() as stack:
        context_options: dict[str, Any] = {}
        if not inherited:
            context_options.update(
                stack.enter_context(
                    shared_camoufox_context_options(proxy, logger=logger)
                )
            )
        context_options.update(dict(extra_context_options or {}))
        session = stack.enter_context(
            shared_camoufox_context_session(
                headless=bool(headless),
                context_options=context_options,
                logger=logger,
            )
        )
        yield session


@contextmanager
def _static_context_allocation(
    allocation: SharedCamoufoxContextAllocation,
) -> Iterator[SharedCamoufoxContextAllocation]:
    yield allocation


def _resolve_proxy_exit_ip(proxy_url: str) -> str:
    import requests

    proxies = {"http": proxy_url, "https": proxy_url}
    endpoints = (
        "https://api.ipify.org",
        "https://checkip.amazonaws.com",
        "https://ipinfo.io/ip",
        "https://icanhazip.com",
    )
    last_error: Optional[BaseException] = None
    for endpoint in endpoints:
        try:
            response = requests.get(
                endpoint,
                proxies=proxies,
                timeout=5,
            )
            response.raise_for_status()
            value = str(response.text or "").strip()
            ipaddress.ip_address(value)
            return value
        except Exception as exc:
            last_error = exc
    raise RuntimeError(
        "unable to resolve proxy exit IP"
        + (f": {type(last_error).__name__}" if last_error is not None else "")
    )


def _proxy_geo_context_options(proxy_url: str) -> dict[str, Any]:
    from camoufox.geolocation import geoip_allowed, get_geolocation

    geoip_allowed()
    exit_ip = _resolve_proxy_exit_ip(proxy_url)
    geolocation = get_geolocation(exit_ip)
    coordinates: dict[str, float] = {
        "longitude": float(geolocation.longitude),
        "latitude": float(geolocation.latitude),
    }
    if geolocation.accuracy is not None:
        coordinates["accuracy"] = float(geolocation.accuracy)
    return {
        "locale": str(geolocation.locale.as_string),
        "timezone_id": str(geolocation.timezone),
        "geolocation": coordinates,
        "permissions": ["geolocation"],
    }


@contextmanager
def shared_camoufox_context_options(
    proxy: Optional[str],
    *,
    logger: Optional[Callable[[str], None]] = None,
) -> Iterator[dict[str, Any]]:
    """Keep a per-context proxy bridge alive and align context GeoIP settings."""

    log = logger or (lambda _message: None)
    raw_proxy = str(proxy or "").strip()
    with playwright_proxy_context(raw_proxy or None, logger=log) as proxy_config:
        options: dict[str, Any] = {}
        if proxy_config:
            options["proxy"] = dict(proxy_config)
        if raw_proxy:
            try:
                options.update(_proxy_geo_context_options(raw_proxy))
                log("[control] shared_camoufox_context geoip=aligned")
            except Exception as exc:
                log(
                    "[control] shared_camoufox_context geoip=unavailable "
                    f"error={type(exc).__name__}"
                )
        yield options
