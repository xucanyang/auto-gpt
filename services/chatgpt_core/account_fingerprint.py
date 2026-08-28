"""Account-level ChatGPT browser fingerprint helpers.

The registration flow uses an attempt-scoped BrowserFingerprint.  Once an
account is saved, the same profile must become account-scoped: different
accounts stay isolated, while later tasks for the same account reuse the
registration-time browser identity.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import fields
from datetime import datetime, timezone
from typing import Any, MutableMapping

from .browser_identity import (
    BrowserFingerprint,
    FINGERPRINT_SCHEMA_VERSION,
    browser_fingerprint_to_dict,
    infer_browser_family,
)


LEGACY_FINGERPRINT_KEYS: tuple[str, ...] = (
    "device_id",
    "accept_language",
    "impersonate",
    "chrome_major",
    "chrome_full_version",
    "user_agent",
    "sec_ch_ua",
    "platform_version",
    "viewport_width",
    "viewport_height",
)
FINGERPRINT_KEYS: tuple[str, ...] = tuple(
    item.name for item in fields(BrowserFingerprint)
)

_FINGERPRINT_META_KEYS: tuple[str, ...] = (
    "chatgpt_browser_fingerprint",
    "chatgpt_browser_fingerprint_signature",
    "chatgpt_browser_fingerprint_source",
    "chatgpt_browser_fingerprint_saved_at",
    "chatgpt_browser_fingerprint_isolated",
    "chatgpt_browser_fingerprint_isolation_mode",
)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_field(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)


def _int_or_zero(value: Any) -> int:
    try:
        return int(float(str(value).strip()))
    except Exception:
        return 0


def _chrome_major_from_version(value: Any) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        return int(text.split(".", 1)[0])
    except Exception:
        return 0


def _chrome_version_from_user_agent(user_agent: str) -> str:
    marker = "Chrome/"
    if marker not in str(user_agent or ""):
        return ""
    tail = str(user_agent).split(marker, 1)[1]
    return tail.split(" ", 1)[0].strip()


def build_browser_fingerprint_payload(fingerprint: Any) -> dict[str, Any]:
    """Return a JSON-safe canonical fingerprint payload without generating new data.

    ``coerce_browser_fingerprint`` intentionally fills missing fields with fresh
    random values for live clients.  Account persistence must not do that, or an
    old partial payload would silently become a new identity.  This helper only
    normalizes fields that are already present.
    """
    if fingerprint is None:
        return {}

    raw = browser_fingerprint_to_dict(fingerprint)
    is_v2 = bool(
        _int_or_zero(raw.get("schema_version")) >= FINGERPRINT_SCHEMA_VERSION
        or raw.get("profile_id")
        or raw.get("browser_family") not in (None, "", "chrome")
        or raw.get("camoufox_config")
        or raw.get("chromium_config")
        or raw.get("browser_backend") not in (None, "", "protocol")
    )
    if is_v2:
        user_agent = str(raw.get("user_agent") or "").strip()
        if not user_agent:
            return {}
        payload = dict(raw)
        payload["schema_version"] = FINGERPRINT_SCHEMA_VERSION
        payload["device_id"] = str(payload.get("device_id") or "").strip()
        payload["accept_language"] = str(
            payload.get("accept_language") or ""
        ).strip()
        payload["impersonate"] = str(payload.get("impersonate") or "").strip()
        payload["user_agent"] = user_agent
        payload["browser_family"] = infer_browser_family(
            user_agent,
            payload.get("impersonate"),
        )
        isolation_mode = str(payload.get("isolation_mode") or "")
        if not str(payload.get("browser_backend") or ""):
            if isolation_mode == "process_isolated_context_deep_native":
                payload["browser_backend"] = "camoufox_firefox"
            elif isolation_mode in {
                "process_isolated_context_patchright_chromium",
                "process_isolated_context_patchright_native_chromium",
            }:
                payload["browser_backend"] = "patchright_chromium"
            else:
                payload["browser_backend"] = "protocol"
        for key in (
            "chrome_major",
            "browser_major",
            "viewport_width",
            "viewport_height",
            "screen_width",
            "screen_height",
            "screen_avail_width",
            "screen_avail_height",
            "outer_width",
            "outer_height",
            "color_depth",
            "pixel_depth",
            "hardware_concurrency",
            "device_memory",
            "max_touch_points",
            "canvas_seed",
            "audio_seed",
            "font_spacing_seed",
        ):
            if key in payload:
                payload[key] = _int_or_zero(payload.get(key))
        try:
            payload["device_scale_factor"] = float(
                payload.get("device_scale_factor") or 1.0
            )
        except (TypeError, ValueError):
            payload["device_scale_factor"] = 1.0
        for key in ("languages", "font_list", "speech_voices", "context_capabilities"):
            if key in payload:
                payload[key] = list(payload.get(key) or [])
        for key in (
            "client_hints",
            "media_devices",
            "geolocation",
            "camoufox_config",
            "chromium_config",
        ):
            if key in payload and not isinstance(payload.get(key), dict):
                payload[key] = {}
        if payload["browser_family"] != "chrome":
            payload["chrome_major"] = 0
            payload["chrome_full_version"] = ""
            payload["sec_ch_ua"] = ""
            payload["platform_version"] = ""
        return payload

    chrome_full_version = str(_get_field(fingerprint, "chrome_full_version") or "").strip()
    user_agent = str(_get_field(fingerprint, "user_agent") or "").strip()
    if not chrome_full_version and user_agent:
        chrome_full_version = _chrome_version_from_user_agent(user_agent)
    chrome_major = _int_or_zero(_get_field(fingerprint, "chrome_major")) or _chrome_major_from_version(chrome_full_version)

    payload: dict[str, Any] = {
        "device_id": str(_get_field(fingerprint, "device_id") or "").strip(),
        "accept_language": str(_get_field(fingerprint, "accept_language") or "").strip(),
        "impersonate": str(_get_field(fingerprint, "impersonate") or "").strip(),
        "chrome_major": chrome_major,
        "chrome_full_version": chrome_full_version,
        "user_agent": user_agent,
        "sec_ch_ua": str(_get_field(fingerprint, "sec_ch_ua") or "").strip(),
        "platform_version": str(_get_field(fingerprint, "platform_version") or "").strip(),
        "viewport_width": _int_or_zero(_get_field(fingerprint, "viewport_width")),
        "viewport_height": _int_or_zero(_get_field(fingerprint, "viewport_height")),
    }

    # Without a UA/sec-ch-ua/chrome profile this is not a stable browser identity;
    # passing such a partial dict downstream would cause random completion later.
    if not (payload.get("user_agent") or payload.get("sec_ch_ua") or payload.get("chrome_full_version")):
        return {}
    return payload


def fingerprint_signature(payload: Any, *, include_device: bool = False) -> str:
    canonical = build_browser_fingerprint_payload(payload)
    if not canonical:
        return ""
    if _int_or_zero(canonical.get("schema_version")) >= FINGERPRINT_SCHEMA_VERSION:
        material_payload = dict(canonical)
        if not include_device:
            material_payload.pop("device_id", None)
        material = json.dumps(
            material_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        return hashlib.sha256(material.encode("ascii")).hexdigest()[:16]
    fields: list[Any] = [
        canonical.get("user_agent"),
        canonical.get("sec_ch_ua"),
        canonical.get("accept_language"),
        canonical.get("impersonate"),
        canonical.get("platform_version"),
        canonical.get("viewport_width"),
        canonical.get("viewport_height"),
    ]
    if include_device:
        fields.append(canonical.get("device_id"))
    material = "|".join(str(item or "") for item in fields)
    return hashlib.sha256(material.encode("utf-8", errors="ignore")).hexdigest()[:16]


def _registration_context(extra: dict[str, Any]) -> dict[str, Any]:
    value = extra.get("chatgpt_registration_context")
    return value if isinstance(value, dict) else {}


def _loose_fingerprint_from_context(context: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(context, dict):
        return {}
    payload = {
        "device_id": context.get("device_id"),
        "accept_language": context.get("accept_language"),
        "impersonate": context.get("impersonate"),
        "user_agent": context.get("user_agent"),
        "sec_ch_ua": context.get("sec_ch_ua"),
        "viewport_width": context.get("viewport_width"),
        "viewport_height": context.get("viewport_height"),
        "platform_version": context.get("platform_version"),
        "chrome_full_version": context.get("chrome_full_version"),
        "chrome_major": context.get("chrome_major"),
    }
    return build_browser_fingerprint_payload(payload)


def resolve_account_browser_fingerprint(extra: Any) -> dict[str, Any]:
    """Resolve the account-level browser fingerprint from new or legacy fields."""
    if not isinstance(extra, dict):
        return {}

    for candidate in (
        extra.get("chatgpt_browser_fingerprint"),
        extra.get("browser_fingerprint"),
    ):
        payload = build_browser_fingerprint_payload(candidate)
        if payload:
            return payload

    context = _registration_context(extra)
    for candidate in (
        context.get("browser_fingerprint"),
        context.get("chatgpt_browser_fingerprint"),
    ):
        payload = build_browser_fingerprint_payload(candidate)
        if payload:
            return payload

    payload = _loose_fingerprint_from_context(context)
    if payload:
        return payload

    loose_top_level = {
        key: extra.get(key)
        for key in LEGACY_FINGERPRINT_KEYS
        if key in extra
    }
    return build_browser_fingerprint_payload(loose_top_level)


def persist_account_browser_fingerprint(
    extra: Any,
    fingerprint: Any = None,
    *,
    source: str = "registration",
    overwrite: bool = False,
) -> dict[str, Any]:
    """Store a canonical account-level fingerprint in ``extra``.

    Existing top-level account fingerprints are kept by default.  This enforces
    the target semantic: account-to-account isolation, account-internal stability.
    """
    target = dict(extra or {}) if isinstance(extra, dict) else {}
    existing = resolve_account_browser_fingerprint(target)
    if existing and not overwrite:
        payload = existing
    else:
        payload = build_browser_fingerprint_payload(fingerprint) or existing or resolve_account_browser_fingerprint(target)
    if not payload:
        return target

    target["chatgpt_browser_fingerprint"] = dict(payload)
    signature = fingerprint_signature(payload)
    if signature:
        target["chatgpt_browser_fingerprint_signature"] = signature
    if source and (overwrite or not target.get("chatgpt_browser_fingerprint_source")):
        target["chatgpt_browser_fingerprint_source"] = str(source)
    if overwrite or not target.get("chatgpt_browser_fingerprint_saved_at"):
        target["chatgpt_browser_fingerprint_saved_at"] = _utcnow_iso()
    target.setdefault("chatgpt_browser_fingerprint_isolated", True)
    isolation_mode = str(payload.get("isolation_mode") or "").strip()
    if isolation_mode and (
        overwrite or not target.get("chatgpt_browser_fingerprint_isolation_mode")
    ):
        target["chatgpt_browser_fingerprint_isolation_mode"] = isolation_mode

    context = _registration_context(target)
    if context:
        if overwrite or not context.get("browser_fingerprint"):
            context["browser_fingerprint"] = dict(payload)
        target["chatgpt_registration_context"] = context
    return target


def merge_preserving_account_browser_fingerprint(
    incoming_extra: Any,
    existing_extra: Any,
    *,
    source: str = "save_account",
) -> dict[str, Any]:
    """Merge/update helper used when a DB save would otherwise replace extra_json."""
    incoming = dict(incoming_extra or {}) if isinstance(incoming_extra, dict) else {}
    existing = dict(existing_extra or {}) if isinstance(existing_extra, dict) else {}

    incoming_payload = resolve_account_browser_fingerprint(incoming)
    existing_payload = resolve_account_browser_fingerprint(existing)

    if existing_payload:
        existing_source = str(existing.get("chatgpt_browser_fingerprint_source") or source or "save_account")
        existing_saved_at = existing.get("chatgpt_browser_fingerprint_saved_at")
        for key in _FINGERPRINT_META_KEYS:
            incoming.pop(key, None)
        for key in _FINGERPRINT_META_KEYS:
            if key in existing and key not in incoming:
                incoming[key] = existing.get(key)
        merged = persist_account_browser_fingerprint(
            incoming,
            existing_payload,
            source=existing_source,
            overwrite=True,
        )
        if existing_saved_at:
            merged["chatgpt_browser_fingerprint_saved_at"] = existing_saved_at
        return merged
    if incoming_payload:
        return persist_account_browser_fingerprint(incoming, incoming_payload, source=source, overwrite=False)
    return incoming


def inject_account_browser_fingerprint(
    config: MutableMapping[str, Any],
    extra: Any,
    *,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Inject an account fingerprint into a runtime config dict if available."""
    if config is None:
        config = {}
    payload = resolve_account_browser_fingerprint(extra)
    if not payload:
        return dict(config)
    if overwrite or not config.get("chatgpt_browser_fingerprint"):
        config["chatgpt_browser_fingerprint"] = dict(payload)
    signature = fingerprint_signature(payload)
    if signature and (overwrite or not config.get("chatgpt_browser_fingerprint_signature")):
        config["chatgpt_browser_fingerprint_signature"] = signature
    config.setdefault("chatgpt_browser_fingerprint_source", "account")
    return dict(config)


def browser_fingerprint_summary(payload: Any) -> str:
    fp = build_browser_fingerprint_payload(payload)
    if not fp:
        return ""
    device = str(fp.get("device_id") or "")
    chrome = str(fp.get("chrome_full_version") or fp.get("chrome_major") or "")
    viewport = ""
    if fp.get("viewport_width") and fp.get("viewport_height"):
        viewport = f"{fp.get('viewport_width')}x{fp.get('viewport_height')}"
    parts = [
        f"device=*{device[-8:]}" if device else "",
        f"browser={fp.get('browser_family') or '-'}",
        f"backend={fp.get('browser_backend') or 'protocol'}",
        f"version={fp.get('browser_version') or chrome}" if (fp.get("browser_version") or chrome) else "",
        f"viewport={viewport}" if viewport else "",
        f"lang={fp.get('accept_language')}" if fp.get("accept_language") else "",
        f"sig={fingerprint_signature(fp)}" if fingerprint_signature(fp) else "",
    ]
    return " ".join(part for part in parts if part)
