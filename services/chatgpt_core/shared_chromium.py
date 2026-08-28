"""Patchright Chromium contexts using the browser's native Linux surface."""

from __future__ import annotations

import glob
import os
import subprocess
import threading
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from .browser_identity import (
    CHROMIUM_ENGINE_VERSION,
    browser_fingerprint_to_dict,
    build_chromium_context_spec,
)


_LAUNCH_TIMEOUT_MS = 30_000
_RUNTIME_LOCK = threading.RLock()
_ACTIVE_CONTEXTS = 0
_TOTAL_LAUNCHES = 0
_LAUNCH_FAILURES = 0


@dataclass
class PatchrightChromiumContextSession:
    browser: Any
    context: Any
    page: Any
    browser_fingerprint: dict[str, Any] = field(default_factory=dict)
    browser_backend: str = "patchright_chromium"


def patchright_headless_for_environment(requested_headless: bool) -> bool:
    """Use a real headed surface whenever the container supplies Xvfb."""

    xvfb_headful = bool(
        str(os.environ.get("AUTO_GPT_XVFB") or "").strip() == "1"
        and str(os.environ.get("DISPLAY") or "").strip()
    )
    return bool(requested_headless) and not xvfb_headful


@lru_cache(maxsize=1)
def chromium_executable_path() -> str:
    configured = str(
        os.environ.get("CHATGPT_CHROMIUM_EXECUTABLE_PATH") or ""
    ).strip()
    candidates = [
        configured,
        "/usr/local/bin/auto-gpt-chromium",
    ]
    candidates.extend(
        sorted(
            glob.glob(
                "/root/.cache/ms-playwright/chromium-*/chrome-linux*/chrome"
            ),
            reverse=True,
        )
    )
    mismatched_versions: list[str] = []
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            resolved = str(Path(candidate).resolve())
            try:
                version_output = subprocess.check_output(
                    [resolved, "--version"],
                    text=True,
                    stderr=subprocess.STDOUT,
                    timeout=5,
                ).strip()
            except Exception as exc:
                mismatched_versions.append(f"{Path(resolved).name}:{type(exc).__name__}")
                continue
            if CHROMIUM_ENGINE_VERSION in version_output:
                return resolved
            mismatched_versions.append(version_output[:100])
    raise RuntimeError(
        "Chrome for Testing executable/version is unavailable; "
        f"expected={CHROMIUM_ENGINE_VERSION}, "
        f"observed={'; '.join(mismatched_versions) or '<none>'}"
    )


def patchright_chromium_runtime_snapshot() -> dict[str, Any]:
    with _RUNTIME_LOCK:
        return {
            "backend": "patchright_chromium",
            "browser_runtime": "patchright",
            "runtime_package_version": "1.62.1",
            "identity_surface": "native_linux",
            "mode": "isolated_process_per_transaction",
            "executable_path": (
                chromium_executable_path()
                if any(
                    Path(path).exists()
                    for path in (
                        str(os.environ.get("CHATGPT_CHROMIUM_EXECUTABLE_PATH") or ""),
                        "/usr/local/bin/auto-gpt-chromium",
                    )
                    if path
                )
                else "auto-discovery"
            ),
            "active_contexts": _ACTIVE_CONTEXTS,
            "total_launches": _TOTAL_LAUNCHES,
            "launch_failures": _LAUNCH_FAILURES,
        }


@contextmanager
def patchright_chromium_registration_session(
    *,
    headless: bool,
    proxy: Optional[str] = None,
    extra_context_options: Optional[dict[str, Any]] = None,
    browser_fingerprint: Any = None,
    logger: Optional[Callable[[str], None]] = None,
) -> Iterator[PatchrightChromiumContextSession]:
    """Launch one isolated Chromium process; never fall back to Firefox."""

    from patchright.sync_api import sync_playwright

    from .shared_camoufox import shared_camoufox_context_options

    global _ACTIVE_CONTEXTS, _TOTAL_LAUNCHES, _LAUNCH_FAILURES

    log = logger or (lambda _message: None)
    with ExitStack() as stack:
        geo_options = stack.enter_context(
            shared_camoufox_context_options(
                proxy,
                browser_fingerprint=browser_fingerprint,
                logger=log,
            )
        )
        context_seed = dict(geo_options)
        context_seed.update(dict(extra_context_options or {}))
        context_options, _init_script, _cdp_override, payload = (
            build_chromium_context_spec(
                browser_fingerprint,
                context_options=context_seed,
            )
        )

        playwright = sync_playwright().start()
        browser = None
        context = None
        active_counted = False
        try:
            executable = chromium_executable_path()
            launch_headless = patchright_headless_for_environment(bool(headless))
            log(
                "[control] chromium_process=launching "
                f"mode={'headless' if launch_headless else 'headed'} "
                f"requested={'headless' if headless else 'headed'} "
                f"binary={Path(executable).name} surface=native_linux"
            )
            browser = playwright.chromium.launch(
                executable_path=executable,
                headless=launch_headless,
                timeout=_LAUNCH_TIMEOUT_MS,
            )
            context = browser.new_context(**context_options)
            page = context.new_page()

            with _RUNTIME_LOCK:
                _ACTIVE_CONTEXTS += 1
                _TOTAL_LAUNCHES += 1
                active_counted = True
            log(
                "[control] chromium_context=ready "
                "backend=patchright_chromium target_os=linux surface=native "
                f"version={payload.get('browser_version') or '-'}"
            )
            yield PatchrightChromiumContextSession(
                browser=browser,
                context=context,
                page=page,
                browser_fingerprint=browser_fingerprint_to_dict(payload),
            )
        except Exception:
            if not active_counted:
                with _RUNTIME_LOCK:
                    _LAUNCH_FAILURES += 1
            raise
        finally:
            if active_counted:
                with _RUNTIME_LOCK:
                    _ACTIVE_CONTEXTS = max(_ACTIVE_CONTEXTS - 1, 0)
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
                        "[control] chromium_process=close_error "
                        f"error={type(exc).__name__}"
                    )
            try:
                playwright.stop()
            except Exception:
                pass


__all__ = [
    "PatchrightChromiumContextSession",
    "chromium_executable_path",
    "patchright_headless_for_environment",
    "patchright_chromium_registration_session",
    "patchright_chromium_runtime_snapshot",
]
