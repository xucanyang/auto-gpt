"""Backend-neutral deep browser session dispatch for ChatGPT flows."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Callable, Iterator, Optional

from .browser_identity import (
    BrowserGeoIdentity,
    CHROMIUM_DEEP_ISOLATION_MODE,
    CAMOUFOX_DEEP_ISOLATION_MODE,
    browser_fingerprint_to_dict,
    coerce_browser_fingerprint,
    configured_deep_browser_family,
    generate_browser_fingerprint,
    normalize_browser_family,
)


def ensure_deep_browser_fingerprint(
    fingerprint: Any = None,
    *,
    default_family: str = "chrome",
) -> Any:
    """Return a profile for the configured deep-browser runtime.

    The runtime is deployment-owned, matching long-link Plus3. Persisted
    Camoufox/macOS profiles are converted once to the native Patchright/Linux
    contract instead of making each caller choose a browser independently.
    """

    existing = coerce_browser_fingerprint(fingerprint) if fingerprint else None
    del default_family
    family = normalize_browser_family(configured_deep_browser_family(), default="")
    if family not in {"chrome", "firefox"}:
        raise ValueError("deep browser sessions support only Chrome or Firefox")
    expected_mode = (
        CHROMIUM_DEEP_ISOLATION_MODE
        if family == "chrome"
        else CAMOUFOX_DEEP_ISOLATION_MODE
    )
    required_config = (
        getattr(existing, "chromium_config", None)
        if family == "chrome"
        else getattr(existing, "camoufox_config", None)
    )
    if (
        existing is not None
        and str(getattr(existing, "isolation_mode", "") or "") == expected_mode
        and required_config
    ):
        return existing
    return generate_browser_fingerprint(
        device_id=getattr(existing, "device_id", None) if existing else None,
        accept_language=(
            getattr(existing, "accept_language", None) if existing else None
        ),
        browser_family=family,
        deep_context=True,
        timezone=(
            str(getattr(existing, "timezone", "") or "America/New_York")
            if existing
            else "America/New_York"
        ),
        geo_identity=(
            BrowserGeoIdentity(
                exit_ip=str(
                    getattr(existing, "webrtc_ipv4", "")
                    or getattr(existing, "webrtc_ipv6", "")
                    or ""
                ),
                timezone=str(
                    getattr(existing, "timezone", "") or "America/New_York"
                ),
                locale=str(getattr(existing, "locale", "") or "en-US"),
                languages=tuple(
                    getattr(existing, "languages", ()) or ("en-US", "en")
                ),
                accept_language=str(
                    getattr(existing, "accept_language", "")
                    or "en-US,en;q=0.9"
                ),
                geolocation=dict(
                    getattr(existing, "geolocation", {}) or {}
                ),
                webrtc_ipv4=str(
                    getattr(existing, "webrtc_ipv4", "") or ""
                ),
                webrtc_ipv6=str(
                    getattr(existing, "webrtc_ipv6", "") or ""
                ),
                source="persisted_profile",
            )
            if existing
            else None
        ),
    )


@contextmanager
def shared_browser_registration_session(
    *,
    headless: bool,
    proxy: Optional[str] = None,
    extra_context_options: Optional[dict[str, Any]] = None,
    browser_fingerprint: Any = None,
    logger: Optional[Callable[[str], None]] = None,
) -> Iterator[Any]:
    profile = ensure_deep_browser_fingerprint(browser_fingerprint)
    family = str(getattr(profile, "browser_family", "") or "")
    log = logger or (lambda _message: None)
    if family == "firefox":
        from .shared_camoufox import shared_camoufox_registration_session

        target_os = str(getattr(profile, "operating_system", "") or "unknown")
        log(f"[control] browser_backend=camoufox_firefox target_os={target_os}")
        with shared_camoufox_registration_session(
            headless=bool(headless),
            proxy=proxy,
            extra_context_options=extra_context_options,
            browser_fingerprint=profile,
            logger=log,
        ) as session:
            yield session
        return
    if family == "chrome":
        from .shared_chromium import patchright_chromium_registration_session

        log("[control] browser_backend=patchright_chromium target_os=linux surface=native")
        with patchright_chromium_registration_session(
            headless=bool(headless),
            proxy=proxy,
            extra_context_options=extra_context_options,
            browser_fingerprint=profile,
            logger=log,
        ) as session:
            yield session
        return
    raise RuntimeError(f"unsupported deep browser family: {family or '<empty>'}")


def shared_browser_runtime_snapshot() -> dict[str, Any]:
    from .shared_camoufox import shared_camoufox_runtime_snapshot
    from .shared_chromium import patchright_chromium_runtime_snapshot

    return {
        "camoufox_firefox": shared_camoufox_runtime_snapshot(),
        "patchright_chromium": patchright_chromium_runtime_snapshot(),
    }


def deep_browser_fingerprint_payload(
    fingerprint: Any = None,
    *,
    default_family: str = "chrome",
) -> dict[str, Any]:
    return browser_fingerprint_to_dict(
        ensure_deep_browser_fingerprint(
            fingerprint,
            default_family=default_family,
        )
    )


__all__ = [
    "deep_browser_fingerprint_payload",
    "ensure_deep_browser_fingerprint",
    "shared_browser_registration_session",
    "shared_browser_runtime_snapshot",
]
