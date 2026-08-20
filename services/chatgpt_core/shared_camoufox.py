"""Process-isolated Camoufox contexts for synchronous registration workers.

Camoufox 152.0.4-beta.28 still reads Screen, Canvas, Audio and several other
deep properties from process-level CAMOU_CONFIG. Every lease therefore owns a
dedicated Camoufox process containing exactly one BrowserContext. Legacy
browser-registration and OAuth workers may claim a parent allocation; Any-Auto
workers allocate the browser inside their own process so diagnostics and the
Context share one Playwright lifecycle.
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


def _resolve_deep_profile(browser_fingerprint: Any = None) -> Any:
    from services.chatgpt_core.browser_identity import (
        coerce_browser_fingerprint,
        generate_browser_fingerprint,
    )

    if browser_fingerprint:
        return coerce_browser_fingerprint(browser_fingerprint)
    return generate_browser_fingerprint(
        browser_family="firefox",
        deep_context=True,
    )


def _server_launch_config(
    headless: bool,
    *,
    browser_fingerprint: Any = None,
    context_options: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    from camoufox import DefaultAddons
    from camoufox.utils import launch_options
    from services.chatgpt_core.browser_identity import build_camoufox_process_config

    profile = _resolve_deep_profile(browser_fingerprint)
    process_config = build_camoufox_process_config(
        profile,
        context_options=context_options,
    )

    virtual_display = ""
    if (
        bool(headless)
        and profile.operating_system == "linux"
        and str(os.environ.get("AUTO_GPT_XVFB") or "").strip() == "1"
    ):
        virtual_display = str(os.environ.get("DISPLAY") or "").strip()
        if not virtual_display:
            raise RuntimeError("AUTO_GPT_XVFB=1 requires DISPLAY")
    launch_headless = bool(headless) and not bool(virtual_display)
    executable_options = camoufox_executable_options()
    executable_options.setdefault("i_know_what_im_doing", True)
    options = launch_options(
        config=process_config,
        os=profile.operating_system,
        headless=launch_headless,
        block_webrtc=True,
        exclude_addons=[DefaultAddons.UBO],
        **({"virtual_display": virtual_display} if virtual_display else {}),
        **executable_options,
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
        "headless": bool(options.get("headless", launch_headless)),
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
    state_id: str
    headless: bool
    process: subprocess.Popen[str]
    endpoint: str
    generation: int
    started_at: float
    browser_pid: int = 0
    profile_id: str = ""
    token: str = ""
    stderr_tail: deque[str] = field(default_factory=lambda: deque(maxlen=40))


@dataclass(frozen=True)
class SharedCamoufoxContextAllocation:
    endpoint: str
    token: str
    headless: bool
    process_id: int = 0
    browser_fingerprint: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SharedCamoufoxContextSession:
    browser: Any
    context: Any
    page: Any
    token: str
    process_id: int = 0
    browser_fingerprint: dict[str, Any] = field(default_factory=dict)


class SharedCamoufoxServerManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._states: dict[str, _ServerState] = {}
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

    @staticmethod
    def _browser_process_pid(server_pid: int, executable_path: str) -> int:
        expected_path = str(Path(executable_path).resolve())
        expected_name = Path(expected_path).name
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            descendants = {int(server_pid)}
            candidates: list[tuple[int, str]] = []
            try:
                entries = list(os.scandir("/proc"))
            except OSError:
                entries = []
            changed = True
            while changed:
                changed = False
                for entry in entries:
                    if not entry.name.isdigit():
                        continue
                    pid = int(entry.name)
                    if pid in descendants:
                        continue
                    try:
                        status = Path(entry.path, "status").read_text(
                            encoding="ascii",
                            errors="ignore",
                        )
                        ppid_line = next(
                            line for line in status.splitlines() if line.startswith("PPid:")
                        )
                        ppid = int(ppid_line.split(":", 1)[1].strip())
                    except (OSError, StopIteration, TypeError, ValueError):
                        continue
                    if ppid not in descendants:
                        continue
                    descendants.add(pid)
                    changed = True
                    try:
                        command = Path(entry.path, "cmdline").read_bytes().replace(
                            b"\0", b" "
                        ).decode("utf-8", errors="ignore")
                    except OSError:
                        command = ""
                    candidates.append((pid, command))
            for pid, command in candidates:
                if expected_path in command or expected_name in command:
                    return pid
            time.sleep(0.05)
        raise RuntimeError("Camoufox browser child process was not found")

    def _stop_state(self, state_id: str) -> None:
        with self._lock:
            state = self._states.pop(str(state_id), None)
        if state is not None:
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

    def _start_process(
        self,
        headless: bool,
        *,
        browser_fingerprint: Any = None,
        context_options: Optional[dict[str, Any]] = None,
    ) -> _ServerState:
        from playwright._impl._driver import compute_driver_executable

        profile = _resolve_deep_profile(browser_fingerprint)
        config = _server_launch_config(
            bool(headless),
            browser_fingerprint=profile,
            context_options=context_options,
        )
        node, cli = compute_driver_executable()
        config_path = ""
        process: Optional[subprocess.Popen[str]] = None
        state: Optional[_ServerState] = None
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
            with self._lock:
                self._generation += 1
                generation = self._generation
            state = _ServerState(
                state_id=uuid.uuid4().hex,
                headless=bool(headless),
                process=process,
                endpoint="",
                generation=generation,
                started_at=time.time(),
                profile_id=str(getattr(profile, "profile_id", "") or ""),
            )
            threading.Thread(
                target=self._drain_stderr,
                args=(state,),
                name=f"shared-camoufox-stderr-{_mode_name(headless)}",
                daemon=True,
            ).start()
            state.endpoint = self._read_endpoint(state)
            state.browser_pid = self._browser_process_pid(
                process.pid,
                str(config["executablePath"]),
            )
            with self._lock:
                self._states[state.state_id] = state
            return state
        except BaseException:
            if process is not None:
                self._terminate_process(process)
            if state is not None:
                with self._lock:
                    self._states.pop(state.state_id, None)
            raise
        finally:
            if config_path:
                try:
                    os.unlink(config_path)
                except OSError:
                    pass

    @contextmanager
    def _process_lease(
        self,
        headless: bool,
        *,
        browser_fingerprint: Any = None,
        context_options: Optional[dict[str, Any]] = None,
        logger: Optional[Callable[[str], None]] = None,
    ) -> Iterator[_ServerState]:
        log = logger or (lambda _message: None)
        state = self._start_process(
            bool(headless),
            browser_fingerprint=browser_fingerprint,
            context_options=context_options,
        )
        log(
            "[control] camoufox_process=leased "
            f"mode={_mode_name(headless)} pid={state.browser_pid} "
            f"server_pid={state.process.pid} "
            f"generation={state.generation} isolation=process_per_context"
        )
        try:
            yield state
        finally:
            self._stop_state(state.state_id)
            log(
                "[control] camoufox_process=released "
                f"mode={_mode_name(headless)} pid={state.browser_pid}"
            )

    @contextmanager
    def lease(
        self,
        headless: bool,
        *,
        browser_fingerprint: Any = None,
        context_options: Optional[dict[str, Any]] = None,
        logger: Optional[Callable[[str], None]] = None,
    ) -> Iterator[str]:
        with self._process_lease(
            bool(headless),
            browser_fingerprint=browser_fingerprint,
            context_options=context_options,
            logger=logger,
        ) as state:
            yield state.endpoint

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
        effective_options: dict[str, Any],
        init_script: str,
    ) -> str:
        from services.chatgpt_core.browser_identity import CAMOUFOX_CONTEXT_SETTERS
        from playwright.sync_api import sync_playwright

        token = uuid.uuid4().hex
        context = None
        with sync_playwright() as playwright:
            browser = playwright.firefox.connect(
                state.endpoint,
                timeout=_CONNECT_TIMEOUT_MS,
            )
            try:
                context = browser.new_context(**effective_options)
                capability_page = context.new_page()
                try:
                    capabilities = capability_page.evaluate(
                        """
                        names => Object.fromEntries(
                          names.map(name => [name, typeof window[name] === 'function'])
                        )
                        """,
                        list(CAMOUFOX_CONTEXT_SETTERS),
                    )
                finally:
                    capability_page.close()
                missing = [
                    name
                    for name in CAMOUFOX_CONTEXT_SETTERS
                    if not bool((capabilities or {}).get(name))
                ]
                if missing:
                    raise RuntimeError(
                        "Camoufox v152 native context setters unavailable: "
                        + ",".join(missing)
                    )
                context.add_init_script(init_script)
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
        # A crashed dedicated browser has already discarded its only context.
        return False

    @contextmanager
    def context_lease(
        self,
        headless: bool,
        *,
        context_options: Optional[dict[str, Any]] = None,
        browser_fingerprint: Any = None,
        logger: Optional[Callable[[str], None]] = None,
    ) -> Iterator[SharedCamoufoxContextAllocation]:
        from services.chatgpt_core.browser_identity import build_camoufox_context_spec

        log = logger or (lambda _message: None)
        profile = _resolve_deep_profile(browser_fingerprint)
        raw_context_options = dict(context_options or {})
        effective_options, init_script, effective_profile = build_camoufox_context_spec(
            profile,
            context_options=raw_context_options,
        )
        with self._process_lease(
            bool(headless),
            browser_fingerprint=profile,
            context_options=raw_context_options,
            logger=log,
        ) as state:
            token = self._allocate_context(
                state,
                effective_options=effective_options,
                init_script=init_script,
            )
            state.token = token
            log(
                "[control] camoufox_context=allocated "
                f"mode={_mode_name(headless)} pid={state.browser_pid} "
                f"token={token[:8]} isolation=process"
            )
            try:
                yield SharedCamoufoxContextAllocation(
                    endpoint=state.endpoint,
                    token=token,
                    headless=bool(headless),
                    process_id=state.browser_pid,
                    browser_fingerprint=effective_profile,
                )
            finally:
                cleaned = self._cleanup_context(state.endpoint, token)
                if not cleaned and self._is_alive(state):
                    log(
                        "[control] camoufox_context=cleanup_failed "
                        f"mode={_mode_name(headless)} pid={state.browser_pid} "
                        f"token={token[:8]}"
                    )

    def is_running(self, headless: bool) -> bool:
        with self._lock:
            return any(
                state.headless is bool(headless) and self._is_alive(state)
                for state in self._states.values()
            )

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            servers: dict[str, Any] = {}
            for headless in (True, False):
                active_states = [
                    state
                    for state in self._states.values()
                    if state.headless is headless and self._is_alive(state)
                ]
                pids = [state.browser_pid for state in active_states]
                server_pids = [state.process.pid for state in active_states]
                servers[_mode_name(headless)] = {
                    "running": bool(active_states),
                    "pid": pids[0] if len(pids) == 1 else None,
                    "pids": pids,
                    "server_pids": server_pids,
                    "active_processes": len(active_states),
                    "active_contexts": sum(bool(state.token) for state in active_states),
                    "generation": max(
                        (state.generation for state in active_states),
                        default=0,
                    ),
                    "uptime_seconds": (
                        max(0.0, now - min(state.started_at for state in active_states))
                        if active_states
                        else 0.0
                    ),
                }
        return {
            "mode": "process_per_context",
            "storage_scope": "browser_context",
            "proxy_scope": "dedicated_process_context",
            "fingerprint_scope": "browser_process_deep_native",
            "fingerprint_isolation_mode": "process_isolated_context_deep_native",
            "fingerprint_capability_gate": "camoufox_v152_process_config_plus_13_setters",
            "webrtc": "blocked",
            "servers": servers,
        }

    def close(self) -> None:
        with self._lock:
            states = list(self._states.values())
            self._states.clear()
        for state in states:
            self._terminate_process(state.process)


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
    browser_fingerprint: Any = None,
    logger: Optional[Callable[[str], None]] = None,
) -> Iterator[SharedCamoufoxContextAllocation]:
    with _MANAGER.context_lease(
        bool(headless),
        context_options=context_options,
        browser_fingerprint=browser_fingerprint,
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
    browser_fingerprint: Any = None,
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
        from services.chatgpt_core.browser_identity import browser_fingerprint_to_dict

        allocation_cm: Any = _static_context_allocation(
            SharedCamoufoxContextAllocation(
                endpoint=inherited_endpoint,
                token=inherited_token,
                headless=bool(headless),
                browser_fingerprint=browser_fingerprint_to_dict(
                    browser_fingerprint
                ),
            )
        )
    else:
        allocation_cm = _MANAGER.context_lease(
            bool(headless),
            context_options=dict(context_options or {}),
            browser_fingerprint=browser_fingerprint,
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
                "[control] camoufox_context=connected "
                f"mode={expected_mode} isolation=dedicated_process_context "
                f"pid={allocation.process_id or '-'} "
                f"token={allocation.token[:8]}"
            )
            yield SharedCamoufoxContextSession(
                browser=browser,
                context=context,
                page=page,
                token=allocation.token,
                process_id=allocation.process_id,
                browser_fingerprint=(
                    dict(allocation.browser_fingerprint)
                    if allocation.browser_fingerprint
                    else {}
                ),
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
                        "[control] camoufox_context=disconnect_error "
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
    browser_fingerprint: Any = None,
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
                browser_fingerprint=browser_fingerprint,
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
        "_auto_gpt_webrtc_ipv4": exit_ip,
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
