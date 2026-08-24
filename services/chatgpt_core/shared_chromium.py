"""Patchright Chromium contexts with one persisted macOS Chrome identity."""

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


def _attach_cdp_identity(
    context: Any,
    page: Any,
    cdp_override: dict[str, Any],
    init_script: str,
    *,
    logger: Callable[[str], None],
) -> Any:
    session = context.new_cdp_session(page)
    session.send("Page.enable")
    script_result = session.send(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": init_script},
    )
    script_identifier = str((script_result or {}).get("identifier") or "").strip()
    if not script_identifier:
        raise RuntimeError("Chromium main-world fingerprint script was not registered")
    session.send("Network.setUserAgentOverride", dict(cdp_override))
    logger(
        "[control] chromium_cdp_identity=attached "
        f"script={script_identifier} "
        f"page={str(getattr(page, 'url', '') or 'about:blank')[:120]}"
    )
    return session


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
        context_options, init_script, cdp_override, payload = (
            build_chromium_context_spec(
                browser_fingerprint,
                context_options=context_seed,
            )
        )

        playwright = sync_playwright().start()
        browser = None
        context = None
        cdp_sessions: list[Any] = []
        active_counted = False
        try:
            executable = chromium_executable_path()
            launch_args = [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                "--webrtc-ip-handling-policy=disable_non_proxied_udp",
                f"--lang={str(context_options.get('locale') or 'en-US')}",
            ]
            log(
                "[control] chromium_process=launching "
                f"mode={'headless' if headless else 'headed'} "
                f"binary={Path(executable).name}"
            )
            browser = playwright.chromium.launch(
                executable_path=executable,
                headless=bool(headless),
                chromium_sandbox=False,
                args=launch_args,
                timeout=_LAUNCH_TIMEOUT_MS,
            )
            context = browser.new_context(**context_options)
            attached_page_ids: set[int] = set()
            creating_controlled_page = False

            def _attach_page(new_page: Any) -> None:
                page_id = id(new_page)
                if page_id in attached_page_ids:
                    return
                attached_page_ids.add(page_id)
                try:
                    cdp_sessions.append(
                        _attach_cdp_identity(
                            context,
                            new_page,
                            cdp_override,
                            init_script,
                            logger=log,
                        )
                    )
                except Exception as exc:
                    attached_page_ids.discard(page_id)
                    log(
                        "[control] chromium_cdp_identity=failed "
                        f"error={type(exc).__name__}"
                    )
                    try:
                        new_page.close()
                    except Exception:
                        pass
                    raise

            def _attach_popup_page(new_page: Any) -> None:
                if not creating_controlled_page:
                    _attach_page(new_page)

            context.on("page", _attach_popup_page)
            native_new_page = context.new_page

            def _new_page_with_identity(*args: Any, **kwargs: Any) -> Any:
                nonlocal creating_controlled_page
                creating_controlled_page = True
                try:
                    new_page = native_new_page(*args, **kwargs)
                finally:
                    creating_controlled_page = False
                _attach_page(new_page)
                return new_page

            context.new_page = _new_page_with_identity
            page = context.new_page()

            with _RUNTIME_LOCK:
                _ACTIVE_CONTEXTS += 1
                _TOTAL_LAUNCHES += 1
                active_counted = True
            log(
                "[control] chromium_context=ready "
                "backend=patchright_chromium target_os=macos "
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
            for cdp_session in reversed(cdp_sessions):
                try:
                    cdp_session.detach()
                except Exception:
                    pass
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
    "patchright_chromium_registration_session",
    "patchright_chromium_runtime_snapshot",
]
