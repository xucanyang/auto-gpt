"""Versioned browser identities shared by HTTP and real-browser runtimes.

The transport identity is intentionally explicit.  curl_cffi aliases such as
``chrome`` move when the dependency changes, so persisted accounts always keep
the concrete impersonation target that created their session.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import platform
import random
import re
import secrets
import uuid
from dataclasses import asdict, dataclass, field, fields, is_dataclass, replace
from functools import lru_cache
from typing import Any, Mapping


FINGERPRINT_SCHEMA_VERSION = 2
BROWSER_ENGINE_ENV = "CHATGPT_BROWSER_ENGINE"
CAMOUFOX_BROWSER_RUNTIME = "camoufox"
PATCHRIGHT_BROWSER_RUNTIME = "patchright"
NATIVE_CHROMIUM_GENERATOR = "native_chromium"
CAMOUFOX_ENGINE_VERSION = "152.0.4"
CAMOUFOX_ENGINE_RELEASE = "beta.28"
CAMOUFOX_VISIBLE_FIREFOX_VERSION = "147.0"
CAMOUFOX_DEEP_ISOLATION_MODE = "process_isolated_context_deep_native"
PATCHRIGHT_PACKAGE_VERSION = "1.62.1"
CHROMIUM_ENGINE_VERSION = "151.0.7922.34"
CHROMIUM_VISIBLE_VERSION = "151.0.0.0"
CHROMIUM_DEEP_ISOLATION_MODE = "process_isolated_context_patchright_native_chromium"
DEEP_BROWSER_OPERATING_SYSTEM = "macos"
DEEP_BROWSER_FAMILIES = ("chrome", "firefox")
CAMOUFOX_NATIVE_DEVICE_SCALE_FACTOR = 1.0

# These are the newest concrete targets that curl_cffi 0.16.2 actually ships.
# Do not replace them with moving aliases (chrome/firefox/safari).
LATEST_CURL_IMPERSONATE = {
    "chrome": "chrome150",
    "firefox": "firefox147",
    "safari": "safari2601",
}

CAMOUFOX_CONTEXT_SETTERS: tuple[str, ...] = (
    "setFontSpacingSeed",
    "setAudioFingerprintSeed",
    "setTimezone",
    "setNavigatorPlatform",
    "setNavigatorOscpu",
    "setNavigatorHardwareConcurrency",
    "setNavigatorUserAgent",
    "setWebRTCIPv4",
    "setWebRTCIPv6",
    "setWebGLVendor",
    "setWebGLRenderer",
    "setFontList",
    "setSpeechVoices",
)

CHROMIUM_CONTEXT_CAPABILITIES: tuple[str, ...] = (
    "native_user_agent",
    "native_client_hints",
    "native_screen_geometry",
    "native_webgl",
    "native_canvas_audio",
    "context_locale_timezone",
    "context_proxy",
    "context_storage_isolation",
)

_BROWSER_FAMILIES = tuple(LATEST_CURL_IMPERSONATE)
PROTOCOL_BROWSER_FAMILIES = ("chrome", "firefox", "safari")
REGISTER_BROWSER_FAMILY_OPTIONS = ("random",) + PROTOCOL_BROWSER_FAMILIES
_ACCEPT_LANGUAGES = (
    "en-US,en;q=0.9",
    "en-US,en;q=0.8",
)
_SCREENS = (
    (1440, 900, 1440, 875, 1280, 720, 2.0),
    (1512, 982, 1512, 957, 1365, 768, 2.0),
    (1728, 1117, 1728, 1092, 1440, 900, 2.0),
    (1920, 1080, 1920, 1055, 1536, 864, 1.0),
)

# Camoufox's statistical presets include phone-sized and ultra-wide viewport
# combinations.  Those are valid generator output, but not coherent with this
# executor's fixed macOS desktop and native DPR=1 runtime contract.
_CAMOUFOX_MACOS_DESKTOP_GEOMETRIES = (
    (1440, 900, 1440, 875, 1280, 720),
    (1512, 982, 1512, 957, 1365, 768),
    (1680, 1050, 1680, 1025, 1440, 900),
    (1920, 1080, 1920, 1055, 1536, 864),
)
_CAMOUFOX_MACOS_MENU_BAR_HEIGHT = 25
_CAMOUFOX_BROWSER_CHROME_WIDTH = 16
_CAMOUFOX_BROWSER_CHROME_HEIGHT = 88

_PROFILE_TEMPLATES: dict[str, dict[str, Any]] = {
    "chrome": {
        "browser_version": CHROMIUM_ENGINE_VERSION,
        "browser_major": 151,
        "engine_family": "chromium",
        "engine_version": CHROMIUM_ENGINE_VERSION,
        "user_agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{CHROMIUM_VISIBLE_VERSION} Safari/537.36"
        ),
        "sec_ch_ua": (
            '"Chromium";v="151", "Not=A?Brand";v="99"'
        ),
        "platform_version": "",
    },
    "firefox": {
        "browser_version": CAMOUFOX_VISIBLE_FIREFOX_VERSION,
        "browser_major": 147,
        "engine_family": "firefox",
        "engine_version": CAMOUFOX_VISIBLE_FIREFOX_VERSION,
        "user_agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:147.0) "
            "Gecko/20100101 Firefox/147.0"
        ),
        "sec_ch_ua": "",
        "platform_version": "",
    },
    "safari": {
        "browser_version": "26.0.1",
        "browser_major": 26,
        "engine_family": "webkit",
        "engine_version": "605.1.15",
        "user_agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/26.0.1 Safari/605.1.15"
        ),
        "sec_ch_ua": "",
        "platform_version": "",
    },
}


@dataclass(frozen=True)
class BrowserFingerprint:
    """One stable browser identity.

    The first ten fields retain the legacy constructor contract.  New fields
    describe the correlated profile used by new registrations only.
    """

    device_id: str
    accept_language: str
    impersonate: str
    chrome_major: int
    chrome_full_version: str
    user_agent: str
    sec_ch_ua: str
    platform_version: str
    viewport_width: int
    viewport_height: int

    schema_version: int = FINGERPRINT_SCHEMA_VERSION
    profile_id: str = ""
    preset_id: str = ""
    browser_family: str = "chrome"
    browser_version: str = ""
    browser_major: int = 0
    engine_family: str = ""
    engine_version: str = ""
    transport_profile: str = ""
    transport_library: str = "curl_cffi"
    transport_library_version: str = "0.16.2"
    tls_profile: str = ""
    http2_profile: str = ""
    http3_profile: str = ""
    header_profile: str = ""
    client_hints: dict[str, str] = field(default_factory=dict)
    operating_system: str = "macos"
    architecture: str = "x86_64"
    navigator_platform: str = "MacIntel"
    navigator_oscpu: str = "Intel Mac OS X 10.15"
    locale: str = "en-US"
    languages: tuple[str, ...] = ("en-US", "en")
    timezone: str = "America/New_York"
    screen_width: int = 0
    screen_height: int = 0
    screen_avail_width: int = 0
    screen_avail_height: int = 0
    outer_width: int = 0
    outer_height: int = 0
    device_scale_factor: float = 1.0
    color_depth: int = 24
    pixel_depth: int = 24
    hardware_concurrency: int = 8
    device_memory: int = 8
    max_touch_points: int = 0
    webgl_vendor: str = ""
    webgl_renderer: str = ""
    canvas_seed: int = 0
    audio_seed: int = 0
    font_spacing_seed: int = 0
    font_list: tuple[str, ...] = ()
    speech_voices: tuple[dict[str, Any], ...] = ()
    media_devices: dict[str, Any] = field(default_factory=dict)
    webrtc_ipv4: str = ""
    webrtc_ipv6: str = ""
    geolocation: dict[str, float] = field(default_factory=dict)
    camoufox_config: dict[str, Any] = field(default_factory=dict)
    chromium_config: dict[str, Any] = field(default_factory=dict)
    browser_backend: str = "protocol"
    camoufox_binary_version: str = ""
    camoufox_release: str = ""
    isolation_mode: str = "protocol_transport"
    context_capabilities: tuple[str, ...] = ()


@dataclass(frozen=True)
class BrowserGeoIdentity:
    """One frozen network geography shared by every browser identity layer."""

    exit_ip: str = ""
    country_code: str = ""
    timezone: str = "America/New_York"
    locale: str = "en-US"
    languages: tuple[str, ...] = ("en-US", "en")
    accept_language: str = "en-US,en;q=0.9"
    geolocation: dict[str, float] = field(default_factory=dict)
    webrtc_ipv4: str = ""
    webrtc_ipv6: str = ""
    source: str = "legacy_default"


_FINGERPRINT_FIELD_NAMES = {item.name for item in fields(BrowserFingerprint)}


def normalize_browser_family(value: Any, *, default: str = "chrome") -> str:
    family = str(value or "").strip().lower()
    aliases = {
        "chromium": "chrome",
        "google_chrome": "chrome",
        "camoufox": "firefox",
        "mozilla": "firefox",
        "webkit": "safari",
    }
    family = aliases.get(family, family)
    if family in _BROWSER_FAMILIES:
        return family
    return default


def normalize_browser_runtime(
    value: Any,
    *,
    default: str = PATCHRIGHT_BROWSER_RUNTIME,
) -> str:
    normalized = str(value or default or PATCHRIGHT_BROWSER_RUNTIME).strip().lower()
    aliases = {
        "patchright": PATCHRIGHT_BROWSER_RUNTIME,
        "chrome": PATCHRIGHT_BROWSER_RUNTIME,
        "chromium": PATCHRIGHT_BROWSER_RUNTIME,
        "camoufox": CAMOUFOX_BROWSER_RUNTIME,
        "firefox": CAMOUFOX_BROWSER_RUNTIME,
    }
    runtime = aliases.get(normalized)
    if runtime is None:
        raise ValueError(
            f"unsupported browser runtime: {normalized}; "
            f"expected {PATCHRIGHT_BROWSER_RUNTIME} or {CAMOUFOX_BROWSER_RUNTIME}"
        )
    return runtime


def configured_browser_runtime() -> str:
    return normalize_browser_runtime(
        os.getenv(BROWSER_ENGINE_ENV, PATCHRIGHT_BROWSER_RUNTIME)
    )


def configured_deep_browser_family() -> str:
    return (
        "chrome"
        if configured_browser_runtime() == PATCHRIGHT_BROWSER_RUNTIME
        else "firefox"
    )


def normalize_protocol_browser_family(value: Any, *, default: str = "random") -> str:
    """Normalize the user-facing protocol browser selection.

    ``random`` is deliberately separate from a concrete family.  This keeps
    task configuration deterministic: only a task explicitly configured as
    random may choose a different protocol identity per attempt.
    """

    family = str(value or "").strip().lower()
    if family in {"", "auto", "any", "random"}:
        return "random"
    normalized = normalize_browser_family(family, default="")
    if normalized in PROTOCOL_BROWSER_FAMILIES:
        return normalized
    return default


def infer_browser_family(user_agent: Any, impersonate: Any = "") -> str:
    target = str(impersonate or "").strip().lower()
    if target.startswith("firefox") or target.startswith("tor"):
        return "firefox"
    if target.startswith("safari"):
        return "safari"
    if target.startswith(("chrome", "edge")):
        return "chrome"
    ua = str(user_agent or "")
    if "Firefox/" in ua:
        return "firefox"
    if "Safari/" in ua and "Chrome/" not in ua and "Chromium/" not in ua:
        return "safari"
    return "chrome"


def select_protocol_browser_family(requested: Any = None) -> str:
    requested_family = normalize_protocol_browser_family(requested, default="")
    if requested_family in PROTOCOL_BROWSER_FAMILIES:
        return requested_family

    configured = str(
        os.environ.get("CHATGPT_PROTOCOL_BROWSER_FAMILIES")
        or "chrome,firefox,safari"
    )
    candidates = [
        normalize_browser_family(item, default="")
        for item in configured.split(",")
    ]
    candidates = [item for item in candidates if item in _BROWSER_FAMILIES]
    return secrets.choice(candidates or list(_BROWSER_FAMILIES))


def _language_parts(accept_language: str) -> tuple[str, ...]:
    values: list[str] = []
    for item in str(accept_language or "").split(","):
        language = item.split(";", 1)[0].strip()
        if language and language not in values:
            values.append(language)
    return tuple(values or ("en-US", "en"))


def _locale_languages(locale: Any) -> tuple[str, ...]:
    normalized = str(locale or "").strip().replace("_", "-") or "en-US"
    primary_language = normalized.split("-", 1)[0].lower()
    values: list[str] = [normalized]
    if primary_language and primary_language != normalized.lower():
        values.append(primary_language)
    if primary_language != "en":
        values.extend(("en-US", "en"))
    return tuple(dict.fromkeys(item for item in values if item))


def _accept_language_for_languages(languages: tuple[str, ...]) -> str:
    values = tuple(item for item in languages if str(item or "").strip())
    if not values:
        return "en-US,en;q=0.9"
    rendered = [str(values[0])]
    for index, language in enumerate(values[1:], start=1):
        quality = max(0.5, 1.0 - (index * 0.1))
        rendered.append(f"{language};q={quality:.1f}")
    return ",".join(rendered)


def _normalize_country_code(value: Any) -> str:
    country = str(value or "").strip().upper()
    if len(country) == 2 and country.isascii() and country.isalpha():
        return country
    return ""


@lru_cache(maxsize=256)
def _primary_locale_for_country(country_code: str) -> str:
    """Return a stable mainstream locale instead of a statistical sample."""

    country = _normalize_country_code(country_code)
    if not country:
        return "en-US"
    try:
        from camoufox.locales import SELECTOR, normalize_locale

        languages, probabilities = SELECTOR._load_territory_data(country)
        if len(languages) and len(probabilities):
            index = max(
                range(min(len(languages), len(probabilities))),
                key=lambda item: float(probabilities[item]),
            )
            language = str(languages[index] or "").replace("_", "-")
            if language:
                return str(normalize_locale(f"{language}-{country}").as_string)
    except Exception:
        pass
    return "en-US"


def _country_geo_fallback(country_code: str) -> tuple[str, str]:
    country = _normalize_country_code(country_code)
    if not country:
        return "America/New_York", "en-US"

    timezone = "America/New_York"
    locale = _primary_locale_for_country(country)
    try:
        import pytz

        timezones = tuple(pytz.country_timezones.get(country) or ())
        if timezones:
            timezone = str(timezones[0])
    except Exception:
        pass
    return timezone, locale


def resolve_browser_geo_identity(
    exit_ip: Any = "",
    *,
    country_code: Any = "",
) -> BrowserGeoIdentity:
    """Resolve a coherent locale/timezone/location from the actual exit IP.

    Camoufox's GeoLite2 database is installed into the production image at
    build time.  Country-only fallback keeps old/direct/probe-degraded tasks
    usable without introducing a runtime dependency on a public GeoIP API.
    """

    raw_ip = str(exit_ip or "").strip()
    validated_ip = ""
    if raw_ip:
        try:
            validated_ip = str(ipaddress.ip_address(raw_ip))
        except ValueError:
            validated_ip = ""

    fallback_country = _normalize_country_code(country_code)
    if validated_ip:
        try:
            from camoufox.geolocation import geoip_allowed, get_geolocation

            geoip_allowed()
            resolved = get_geolocation(validated_ip)
            resolved_country = _normalize_country_code(
                getattr(resolved.locale, "region", "")
            )
            effective_country = resolved_country or fallback_country
            locale = _primary_locale_for_country(effective_country)
            languages = _locale_languages(locale)
            coordinates: dict[str, float] = {
                "latitude": float(resolved.latitude),
                "longitude": float(resolved.longitude),
            }
            if resolved.accuracy is not None:
                coordinates["accuracy"] = float(resolved.accuracy)
            is_ipv4 = ipaddress.ip_address(validated_ip).version == 4
            return BrowserGeoIdentity(
                exit_ip=validated_ip,
                country_code=effective_country,
                timezone=str(resolved.timezone or "America/New_York"),
                locale=locale,
                languages=languages,
                accept_language=_accept_language_for_languages(languages),
                geolocation=coordinates,
                webrtc_ipv4=validated_ip if is_ipv4 else "",
                webrtc_ipv6=validated_ip if not is_ipv4 else "",
                source="maxmind_geoip",
            )
        except Exception:
            pass

    timezone, locale = _country_geo_fallback(fallback_country)
    languages = _locale_languages(locale)
    is_ipv4 = bool(
        validated_ip and ipaddress.ip_address(validated_ip).version == 4
    )
    return BrowserGeoIdentity(
        exit_ip=validated_ip,
        country_code=fallback_country,
        timezone=timezone,
        locale=locale,
        languages=languages,
        accept_language=_accept_language_for_languages(languages),
        geolocation={},
        webrtc_ipv4=validated_ip if is_ipv4 else "",
        webrtc_ipv6=validated_ip if validated_ip and not is_ipv4 else "",
        source="country_fallback" if fallback_country else "legacy_default",
    )


def rebind_browser_fingerprint_geo(
    fingerprint: Any,
    geo_identity: BrowserGeoIdentity,
) -> BrowserFingerprint:
    """Return an ephemeral copy with only network geography rebound.

    Checkout may intentionally use a country-specific exit that differs from
    the account's registration route. Preserve the account device, browser
    preset and entropy seeds while making locale, timezone, coordinates and
    WebRTC describe the verified Checkout exit.
    """

    profile = coerce_browser_fingerprint(fingerprint)
    geo = (
        geo_identity
        if isinstance(geo_identity, BrowserGeoIdentity)
        else BrowserGeoIdentity()
    )
    camoufox_config = dict(profile.camoufox_config or {})
    if camoufox_config:
        camoufox_config["timezone"] = str(geo.timezone or profile.timezone)
        _apply_locale_to_camoufox_config(
            camoufox_config,
            str(geo.locale or profile.locale),
            tuple(geo.languages or profile.languages),
        )
        coordinates = dict(geo.geolocation or {})
        for key in ("latitude", "longitude", "accuracy"):
            config_key = f"geolocation:{key}"
            if coordinates.get(key) is None:
                camoufox_config.pop(config_key, None)
            else:
                camoufox_config[config_key] = float(coordinates[key])

    return replace(
        profile,
        accept_language=str(geo.accept_language or profile.accept_language),
        locale=str(geo.locale or profile.locale),
        languages=tuple(geo.languages or profile.languages),
        timezone=str(geo.timezone or profile.timezone),
        geolocation=dict(geo.geolocation or {}),
        webrtc_ipv4=str(geo.webrtc_ipv4 or ""),
        webrtc_ipv6=str(geo.webrtc_ipv6 or ""),
        camoufox_config=camoufox_config,
    )


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)


def browser_fingerprint_to_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, BrowserFingerprint):
        return _json_safe(asdict(value))
    if isinstance(value, Mapping):
        return {
            str(key): _json_safe(item)
            for key, item in value.items()
            if str(key) in _FINGERPRINT_FIELD_NAMES
        }
    return {}


def _version_from_user_agent(family: str, user_agent: str) -> str:
    marker = {
        "chrome": r"(?:Chrome|Chromium)/([0-9.]+)",
        "firefox": r"Firefox/([0-9.]+)",
        "safari": r"Version/([0-9.]+)",
    }[family]
    match = re.search(marker, str(user_agent or ""))
    return str(match.group(1) if match else "")


def _profile_hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        _json_safe(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()[:24]


def _runtime_camoufox_os() -> str:
    system = platform.system().strip().lower()
    target = {
        "darwin": "macos",
        "linux": "linux",
        "windows": "windows",
    }.get(system)
    if not target:
        raise RuntimeError(f"unsupported Camoufox runtime OS: {system or 'unknown'}")
    return target


def browser_backend_for_family(browser_family: Any, *, deep_context: bool) -> str:
    if not deep_context:
        return "protocol"
    family = normalize_browser_family(browser_family, default="")
    if family == "firefox":
        return "camoufox_firefox"
    if family == "chrome":
        return "patchright_chromium"
    raise ValueError("deep browser contexts support only Chrome or Firefox")


def _architecture_from_navigator(platform_value: str, oscpu: str) -> str:
    material = f"{platform_value} {oscpu}".lower()
    if "aarch64" in material or "arm64" in material:
        return "arm64"
    if "i686" in material or "i386" in material:
        return "x86"
    return "x86_64"


def _navigator_defaults_for_os(target_os: str) -> tuple[str, str]:
    return {
        "linux": ("Linux x86_64", "Linux x86_64"),
        "macos": ("MacIntel", "Intel Mac OS X 10.15"),
        "windows": ("Win32", "Windows NT 10.0; Win64; x64"),
    }[target_os]


def _generic_fingerprint(
    *,
    family: str,
    device_id: Any = None,
    accept_language: Any = None,
    timezone: str = "America/New_York",
    geo_identity: BrowserGeoIdentity | None = None,
) -> BrowserFingerprint:
    template = _PROFILE_TEMPLATES[family]
    sw, sh, aw, ah, vw, vh, dpr = random.choice(_SCREENS)
    geo = geo_identity or BrowserGeoIdentity()
    language_header = str(
        accept_language
        or (geo.accept_language if geo.source != "legacy_default" else "")
        or random.choice(_ACCEPT_LANGUAGES)
    )
    languages = (
        tuple(geo.languages)
        if geo.source != "legacy_default" and not accept_language
        else _language_parts(language_header)
    )
    locale = (
        str(geo.locale or "")
        if geo.source != "legacy_default" and not accept_language
        else str(languages[0] if languages else "en-US")
    )
    if family == "chrome":
        # Match the live long-link Patchright contract. Chromium owns its UA,
        # Client Hints, screen, WebGL, Canvas and Audio surfaces; only the
        # task's locale/timezone/proxy are varied per BrowserContext.
        sw, sh, aw, ah, vw, vh, dpr = (1920, 1080, 1920, 1040, 1920, 1080, 1.0)
        locale = str(locale or "en-US")
        languages = (locale,)
        language_header = locale
    browser_version = str(template["browser_version"])
    chrome_version = browser_version if family == "chrome" else ""
    chrome_major = int(template["browser_major"]) if family == "chrome" else 0
    seeds = [secrets.randbelow(4_294_967_295) + 1 for _ in range(3)]
    profile_id = str(uuid.uuid4())
    return BrowserFingerprint(
        device_id=str(device_id or uuid.uuid4()),
        accept_language=language_header,
        impersonate=LATEST_CURL_IMPERSONATE[family],
        chrome_major=chrome_major,
        chrome_full_version=chrome_version,
        user_agent=str(template["user_agent"]),
        sec_ch_ua=str(template["sec_ch_ua"]),
        platform_version=str(template["platform_version"]),
        viewport_width=vw,
        viewport_height=vh,
        profile_id=profile_id,
        preset_id=f"curl-cffi-0.16.2:{LATEST_CURL_IMPERSONATE[family]}",
        browser_family=family,
        browser_version=browser_version,
        browser_major=int(template["browser_major"]),
        engine_family=str(template["engine_family"]),
        engine_version=str(template["engine_version"]),
        transport_profile=LATEST_CURL_IMPERSONATE[family],
        tls_profile=LATEST_CURL_IMPERSONATE[family],
        http2_profile=LATEST_CURL_IMPERSONATE[family],
        header_profile=LATEST_CURL_IMPERSONATE[family],
        client_hints=(
            {
                "sec_ch_ua": str(template["sec_ch_ua"]),
                "sec_ch_ua_mobile": "?0",
                "sec_ch_ua_platform": '"Linux"',
                "sec_ch_ua_platform_version": str(template["platform_version"]),
            }
            if family == "chrome"
            else {}
        ),
        locale=locale,
        languages=languages,
        timezone=str(timezone or "America/New_York"),
        screen_width=sw,
        screen_height=sh,
        screen_avail_width=aw,
        screen_avail_height=ah,
        outer_width=min(sw, vw + 16),
        outer_height=min(ah, vh + 88),
        device_scale_factor=dpr,
        hardware_concurrency=(
            max(1, min(16, int(os.cpu_count() or 8)))
            if family == "chrome"
            else secrets.choice((8, 10, 12, 16))
        ),
        device_memory=32 if family == "chrome" else 8,
        webgl_vendor="" if family == "chrome" else "Apple Inc.",
        webgl_renderer="" if family == "chrome" else "Apple M1",
        canvas_seed=seeds[0],
        audio_seed=seeds[1],
        font_spacing_seed=seeds[2],
        webrtc_ipv4=str(geo.webrtc_ipv4 or ""),
        webrtc_ipv6=str(geo.webrtc_ipv6 or ""),
        geolocation=dict(geo.geolocation or {}),
        operating_system="linux" if family == "chrome" else "macos",
        navigator_platform="Linux x86_64" if family == "chrome" else "MacIntel",
        navigator_oscpu="" if family == "chrome" else "Intel Mac OS X 10.15",
        isolation_mode="protocol_transport",
    )


def _camoufox_fingerprint(base: BrowserFingerprint) -> BrowserFingerprint:
    try:
        from camoufox.fingerprints import (
            generate_context_fingerprint,
            get_random_preset,
        )
    except Exception as exc:  # pragma: no cover - exercised by image preflight
        raise RuntimeError(f"Camoufox context fingerprint API unavailable: {exc}") from exc

    # Camoufox is specifically built to apply a complete cross-platform native
    # profile. Keep the target profile independent from the Linux container so
    # UA, navigator, fonts, WebGL and screen all describe the same macOS device.
    _runtime_camoufox_os()  # Fail early on unsupported runtime hosts.
    target_profile_os = DEEP_BROWSER_OPERATING_SYSTEM
    preset = get_random_preset(
        os=target_profile_os,
        ff_version=CAMOUFOX_ENGINE_VERSION,
    )
    if not isinstance(preset, dict):
        raise RuntimeError("Camoufox v152 fingerprint presets are unavailable")
    generated = generate_context_fingerprint(
        preset=preset,
        ff_version=str(CAMOUFOX_VISIBLE_FIREFOX_VERSION.split(".", 1)[0]),
        timezone=base.timezone,
        locale=base.locale,
    )
    config = dict(generated.get("config") or {})
    voices = config.get("voices") or []
    fonts = config.get("fonts") or []
    target_os = {"linux": "lin", "macos": "mac", "windows": "win"}[
        target_profile_os
    ]
    try:
        from camoufox.webgl.sample import sample_webgl

        try:
            webgl_config = sample_webgl(
                target_os,
                str(config.get("webGl:vendor") or "") or None,
                str(config.get("webGl:renderer") or "") or None,
            )
        except ValueError:
            # Some bundled v152 presets reference a vendor/renderer pair that
            # is absent from the same release's WebGL database. Replace only
            # that inconsistent pair with a valid same-OS official sample.
            webgl_config = sample_webgl(target_os)
        webgl_config.pop("webGl2Enabled", None)
        config.update({str(key): value for key, value in webgl_config.items()})
    except Exception as exc:
        raise RuntimeError(f"Camoufox v152 WebGL profile unavailable: {exc}") from exc

    (
        screen_width,
        screen_height,
        screen_avail_width,
        screen_avail_height,
        viewport_width,
        viewport_height,
    ) = secrets.choice(_CAMOUFOX_MACOS_DESKTOP_GEOMETRIES)
    outer_width = min(
        screen_avail_width,
        viewport_width + _CAMOUFOX_BROWSER_CHROME_WIDTH,
    )
    outer_height = min(
        screen_avail_height,
        viewport_height + _CAMOUFOX_BROWSER_CHROME_HEIGHT,
    )
    # Camoufox cannot coherently expose a synthetic Retina DPR across Gecko,
    # WebGL and the Xvfb display. Keep the frozen profile at the native value
    # the runtime can implement on every browser-visible surface.
    device_scale_factor = CAMOUFOX_NATIVE_DEVICE_SCALE_FACTOR
    language_parts = _language_parts(base.accept_language)
    config.update(
        {
            "navigator.languages": list(language_parts),
            "screen.width": screen_width,
            "screen.height": screen_height,
            "screen.availWidth": screen_avail_width,
            "screen.availHeight": screen_avail_height,
            "screen.availTop": _CAMOUFOX_MACOS_MENU_BAR_HEIGHT,
            "screen.availLeft": 0,
            "screen.colorDepth": 24,
            "screen.pixelDepth": 24,
            "window.outerWidth": outer_width,
            "window.outerHeight": outer_height,
            "window.innerWidth": viewport_width,
            "window.innerHeight": viewport_height,
            "window.screenX": 0,
            "window.screenY": _CAMOUFOX_MACOS_MENU_BAR_HEIGHT,
            "window.devicePixelRatio": device_scale_factor,
            "window.history.length": secrets.choice((2, 3, 4, 5)),
            "headers.User-Agent": str(config.get("navigator.userAgent") or base.user_agent),
            "headers.Accept-Language": base.accept_language,
            "mediaDevices:enabled": True,
            "mediaDevices:micros": 1,
            "mediaDevices:webcams": 1,
            "mediaDevices:speakers": 0,
        }
    )
    media_devices = {
        key.split(":", 1)[1]: value
        for key, value in config.items()
        if str(key).startswith("mediaDevices:")
    }
    user_agent = str(config.get("navigator.userAgent") or base.user_agent)
    version = _version_from_user_agent("firefox", user_agent) or base.browser_version
    default_platform, default_oscpu = _navigator_defaults_for_os(target_profile_os)
    navigator_platform = str(config.get("navigator.platform") or default_platform)
    navigator_oscpu = str(config.get("navigator.oscpu") or default_oscpu)
    preset_id = f"camoufox-v152:{_profile_hash({'preset': preset, 'webgl': webgl_config})}"
    return replace(
        base,
        preset_id=preset_id,
        browser_version=version,
        browser_major=int(version.split(".", 1)[0]),
        engine_family="firefox",
        engine_version=CAMOUFOX_ENGINE_VERSION,
        user_agent=user_agent,
        operating_system=target_profile_os,
        architecture=_architecture_from_navigator(
            navigator_platform,
            navigator_oscpu,
        ),
        viewport_width=viewport_width,
        viewport_height=viewport_height,
        navigator_platform=navigator_platform,
        navigator_oscpu=navigator_oscpu,
        locale=str(config.get("navigator.language") or base.locale),
        screen_width=screen_width,
        screen_height=screen_height,
        screen_avail_width=screen_avail_width,
        screen_avail_height=screen_avail_height,
        outer_width=outer_width,
        outer_height=outer_height,
        device_scale_factor=device_scale_factor,
        color_depth=int(config.get("screen.colorDepth") or 24),
        pixel_depth=int(config.get("screen.pixelDepth") or 24),
        hardware_concurrency=int(
            config.get("navigator.hardwareConcurrency") or base.hardware_concurrency
        ),
        max_touch_points=int(config.get("navigator.maxTouchPoints") or 0),
        webgl_vendor=str(config.get("webGl:vendor") or ""),
        webgl_renderer=str(config.get("webGl:renderer") or ""),
        canvas_seed=int(config.get("canvas:seed") or base.canvas_seed),
        audio_seed=int(config.get("audio:seed") or base.audio_seed),
        font_spacing_seed=int(
            config.get("fonts:spacing_seed") or base.font_spacing_seed
        ),
        font_list=tuple(str(item) for item in fonts),
        speech_voices=tuple(
            dict(item) if isinstance(item, Mapping) else {"name": str(item)}
            for item in voices
        ),
        media_devices=_json_safe(media_devices),
        camoufox_config=_json_safe(
            {
                key: value
                for key, value in config.items()
                if key not in {"fonts", "voices"}
            }
        ),
        camoufox_binary_version=CAMOUFOX_ENGINE_VERSION,
        camoufox_release=CAMOUFOX_ENGINE_RELEASE,
        browser_backend="camoufox_firefox",
        isolation_mode=CAMOUFOX_DEEP_ISOLATION_MODE,
        context_capabilities=CAMOUFOX_CONTEXT_SETTERS,
    )


def _native_patchright_chromium_fingerprint(
    base: BrowserFingerprint,
) -> BrowserFingerprint:
    """Freeze the native Chromium contract used by long-link Plus3.

    Patchright owns every browser-visible surface. The persisted payload keeps
    only task identity and Context-level geography; it deliberately contains
    no BrowserForge material or JavaScript/CDP spoofing seeds.
    """

    browser_major = int(CHROMIUM_ENGINE_VERSION.split(".", 1)[0])
    user_agent = (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{CHROMIUM_VISIBLE_VERSION} Safari/537.36"
    )
    sec_ch_ua = '"Chromium";v="151", "Not=A?Brand";v="99"'
    locale = str(base.locale or "en-US")
    return replace(
        base,
        preset_id=f"patchright-{PATCHRIGHT_PACKAGE_VERSION}:native-chromium",
        browser_version=CHROMIUM_ENGINE_VERSION,
        browser_major=browser_major,
        engine_family="chromium",
        engine_version=CHROMIUM_ENGINE_VERSION,
        chrome_major=browser_major,
        chrome_full_version=CHROMIUM_ENGINE_VERSION,
        user_agent=user_agent,
        sec_ch_ua=sec_ch_ua,
        platform_version="",
        accept_language=locale,
        locale=locale,
        languages=(locale,),
        client_hints={
            "sec_ch_ua": sec_ch_ua,
            "sec_ch_ua_mobile": "?0",
            "sec_ch_ua_platform": '"Linux"',
            "sec_ch_ua_arch": '"x86"',
            "sec_ch_ua_bitness": '"64"',
            "sec_ch_ua_full_version": f'"{CHROMIUM_ENGINE_VERSION}"',
        },
        operating_system="linux",
        architecture="x86_64",
        navigator_platform="Linux x86_64",
        navigator_oscpu="",
        viewport_width=1920,
        viewport_height=1080,
        screen_width=1920,
        screen_height=1080,
        screen_avail_width=1920,
        screen_avail_height=1040,
        outer_width=1920,
        outer_height=1040,
        device_scale_factor=1.0,
        hardware_concurrency=max(1, min(16, int(os.cpu_count() or 8))),
        device_memory=32,
        max_touch_points=0,
        webgl_vendor="",
        webgl_renderer="",
        canvas_seed=0,
        audio_seed=0,
        font_spacing_seed=0,
        font_list=(),
        speech_voices=(),
        media_devices={},
        chromium_config={
            "generator": NATIVE_CHROMIUM_GENERATOR,
            "browser_runtime": PATCHRIGHT_BROWSER_RUNTIME,
            "runtime_package_version": PATCHRIGHT_PACKAGE_VERSION,
            "engine_version": CHROMIUM_ENGINE_VERSION,
            "native_browser_surface": True,
        },
        browser_backend="patchright_chromium",
        isolation_mode=CHROMIUM_DEEP_ISOLATION_MODE,
        context_capabilities=CHROMIUM_CONTEXT_CAPABILITIES,
    )


def generate_browser_fingerprint(
    device_id: Any = None,
    accept_language: Any = None,
    *,
    browser_family: str = "chrome",
    deep_context: bool = False,
    timezone: str | None = None,
    geo_identity: BrowserGeoIdentity | None = None,
) -> BrowserFingerprint:
    family = normalize_browser_family(browser_family)
    if deep_context and family not in DEEP_BROWSER_FAMILIES:
        raise ValueError("deep browser contexts support only Chrome or Firefox")
    effective_geo = geo_identity or BrowserGeoIdentity()
    base = _generic_fingerprint(
        family=family,
        device_id=device_id,
        accept_language=accept_language,
        timezone=str(
            timezone
            or (
                effective_geo.timezone
                if effective_geo.source != "legacy_default"
                else "America/New_York"
            )
        ),
        geo_identity=effective_geo,
    )
    if not deep_context:
        return base
    return (
        _camoufox_fingerprint(base)
        if family == "firefox"
        else _native_patchright_chromium_fingerprint(base)
    )


def _normalize_camoufox_runtime_profile(
    fingerprint: BrowserFingerprint,
) -> BrowserFingerprint:
    if (
        fingerprint.browser_family != "firefox"
        or fingerprint.isolation_mode != CAMOUFOX_DEEP_ISOLATION_MODE
        or not fingerprint.camoufox_config
    ):
        return fingerprint
    config = dict(fingerprint.camoufox_config)
    configured_dpr = config.get("window.devicePixelRatio")
    try:
        configured_dpr = float(configured_dpr)
    except (TypeError, ValueError):
        configured_dpr = 0.0
    if (
        float(fingerprint.device_scale_factor or 0.0)
        == CAMOUFOX_NATIVE_DEVICE_SCALE_FACTOR
        and configured_dpr == CAMOUFOX_NATIVE_DEVICE_SCALE_FACTOR
    ):
        return fingerprint
    config["window.devicePixelRatio"] = CAMOUFOX_NATIVE_DEVICE_SCALE_FACTOR
    return replace(
        fingerprint,
        device_scale_factor=CAMOUFOX_NATIVE_DEVICE_SCALE_FACTOR,
        camoufox_config=_json_safe(config),
    )


def coerce_browser_fingerprint(
    fingerprint: Any = None,
    *,
    device_id: Any = None,
    user_agent: Any = None,
    sec_ch_ua: Any = None,
    impersonate: Any = None,
    accept_language: Any = None,
    platform_version: Any = None,
    viewport_width: Any = None,
    viewport_height: Any = None,
    browser_family: Any = None,
) -> BrowserFingerprint:
    if isinstance(fingerprint, BrowserFingerprint):
        return _normalize_camoufox_runtime_profile(fingerprint)

    source = browser_fingerprint_to_dict(fingerprint)
    source_user_agent = str(user_agent or source.get("user_agent") or "")
    source_impersonate = str(impersonate or source.get("impersonate") or "")
    family = normalize_browser_family(
        browser_family
        or source.get("browser_family")
        or infer_browser_family(source_user_agent, source_impersonate)
    )
    base = generate_browser_fingerprint(
        device_id=device_id or source.get("device_id"),
        accept_language=accept_language or source.get("accept_language"),
        browser_family=family,
        deep_context=False,
        timezone=str(source.get("timezone") or "America/New_York"),
    )
    values = browser_fingerprint_to_dict(base)
    values.update({key: value for key, value in source.items() if value not in (None, "")})
    explicit = {
        "device_id": device_id,
        "user_agent": user_agent,
        "sec_ch_ua": sec_ch_ua,
        "impersonate": impersonate,
        "accept_language": accept_language,
        "platform_version": platform_version,
        "viewport_width": viewport_width,
        "viewport_height": viewport_height,
        "browser_family": browser_family,
    }
    values.update({key: value for key, value in explicit.items() if value not in (None, "")})
    family = normalize_browser_family(
        values.get("browser_family")
        or infer_browser_family(values.get("user_agent"), values.get("impersonate"))
    )
    values["browser_family"] = family
    isolation_mode = str(values.get("isolation_mode") or "")
    if isolation_mode == CAMOUFOX_DEEP_ISOLATION_MODE and family == "firefox":
        values["browser_backend"] = "camoufox_firefox"
    elif isolation_mode == CHROMIUM_DEEP_ISOLATION_MODE and family == "chrome":
        values["browser_backend"] = "patchright_chromium"
    else:
        values["browser_backend"] = str(values.get("browser_backend") or "protocol")
    version = str(
        values.get("browser_version")
        or _version_from_user_agent(family, str(values.get("user_agent") or ""))
    )
    values["browser_version"] = version
    if not values.get("browser_major") and version:
        values["browser_major"] = int(version.split(".", 1)[0])
    if family == "chrome":
        chrome_version = str(
            values.get("chrome_full_version")
            or _version_from_user_agent("chrome", str(values.get("user_agent") or ""))
        )
        values["chrome_full_version"] = chrome_version
        values["chrome_major"] = int(chrome_version.split(".", 1)[0]) if chrome_version else 0
    else:
        values["chrome_full_version"] = ""
        values["chrome_major"] = 0
        values["sec_ch_ua"] = ""
        values["platform_version"] = ""
    values["languages"] = tuple(values.get("languages") or _language_parts(str(values.get("accept_language") or "")))
    values["font_list"] = tuple(values.get("font_list") or ())
    values["speech_voices"] = tuple(values.get("speech_voices") or ())
    values["context_capabilities"] = tuple(values.get("context_capabilities") or ())
    values = {key: values.get(key) for key in _FINGERPRINT_FIELD_NAMES}
    return _normalize_camoufox_runtime_profile(BrowserFingerprint(**values))


def merge_observed_browser_fingerprint(
    planned: Any,
    observed: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Merge runtime observations without inventing or rotating deep seeds."""

    payload = browser_fingerprint_to_dict(planned)
    for key, value in dict(observed or {}).items():
        if key in _FINGERPRINT_FIELD_NAMES and value not in (None, "", [], {}):
            payload[key] = _json_safe(value)
    family = infer_browser_family(payload.get("user_agent"), payload.get("impersonate"))
    payload["browser_family"] = family
    version = _version_from_user_agent(family, str(payload.get("user_agent") or ""))
    if version:
        payload["browser_version"] = version
        payload["browser_major"] = int(version.split(".", 1)[0])
    if family != "chrome":
        payload["chrome_major"] = 0
        payload["chrome_full_version"] = ""
        payload["sec_ch_ua"] = ""
        payload["platform_version"] = ""
    return payload


def _resolve_camoufox_deep_profile(fingerprint: Any = None) -> BrowserFingerprint:
    if fingerprint:
        profile = coerce_browser_fingerprint(fingerprint)
    else:
        profile = generate_browser_fingerprint(
            browser_family="firefox",
            deep_context=True,
        )
    if (
        profile.schema_version < FINGERPRINT_SCHEMA_VERSION
        or profile.browser_family != "firefox"
        or profile.isolation_mode != CAMOUFOX_DEEP_ISOLATION_MODE
        or not profile.camoufox_config
    ):
        raise RuntimeError(
            "Camoufox process requires a persisted Firefox v2 deep fingerprint"
        )
    if tuple(profile.context_capabilities) != CAMOUFOX_CONTEXT_SETTERS:
        raise RuntimeError("Camoufox context fingerprint capability contract mismatch")
    return profile


def _apply_locale_to_camoufox_config(
    config: dict[str, Any],
    locale: str,
    languages: tuple[str, ...],
) -> None:
    try:
        from camoufox.locales import normalize_locale

        parsed = normalize_locale(locale)
        config["locale:language"] = parsed.language
        config["locale:region"] = parsed.region
        if parsed.script:
            config["locale:script"] = parsed.script
        else:
            config.pop("locale:script", None)
        config["navigator.language"] = parsed.as_string
    except Exception as exc:
        raise RuntimeError(f"invalid Camoufox locale {locale!r}: {exc}") from exc
    config["navigator.languages"] = list(languages or (locale,))
    if len(languages) > 1:
        config["locale:all"] = ",".join(languages)
    else:
        config.pop("locale:all", None)


def build_camoufox_process_config(
    fingerprint: Any = None,
    *,
    context_options: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Rebuild the complete launch-time CAMOU_CONFIG for one isolated process."""

    profile = _resolve_camoufox_deep_profile(fingerprint)
    options = dict(context_options or {})
    config = dict(profile.camoufox_config)
    config.update(
        {
            "navigator.userAgent": profile.user_agent,
            "navigator.platform": profile.navigator_platform,
            "navigator.oscpu": profile.navigator_oscpu,
            "navigator.hardwareConcurrency": int(profile.hardware_concurrency),
            "navigator.maxTouchPoints": int(profile.max_touch_points),
            "screen.width": int(profile.screen_width),
            "screen.height": int(profile.screen_height),
            "screen.availWidth": int(profile.screen_avail_width),
            "screen.availHeight": int(profile.screen_avail_height),
            "screen.colorDepth": int(profile.color_depth),
            "screen.pixelDepth": int(profile.pixel_depth),
            "window.outerWidth": int(profile.outer_width),
            "window.outerHeight": int(profile.outer_height),
            "window.innerWidth": int(profile.viewport_width),
            "window.innerHeight": int(profile.viewport_height),
            "window.devicePixelRatio": float(profile.device_scale_factor or 1.0),
            "webGl:vendor": profile.webgl_vendor,
            "webGl:renderer": profile.webgl_renderer,
            "canvas:seed": int(profile.canvas_seed),
            "audio:seed": int(profile.audio_seed),
            "fonts:spacing_seed": int(profile.font_spacing_seed),
            "fonts": list(profile.font_list),
            "voices": [dict(item) for item in profile.speech_voices],
            "headers.User-Agent": profile.user_agent,
            "headers.Accept-Language": profile.accept_language,
        }
    )
    for key, value in dict(profile.media_devices or {}).items():
        config[f"mediaDevices:{key}"] = value
    if not profile.media_devices:
        config.update(
            {
                "mediaDevices:enabled": True,
                "mediaDevices:micros": 1,
                "mediaDevices:webcams": 1,
                "mediaDevices:speakers": 0,
            }
        )

    effective_timezone = str(options.get("timezone_id") or profile.timezone)
    effective_locale = str(options.get("locale") or profile.locale)
    effective_languages = (
        (effective_locale,) if effective_locale != profile.locale else profile.languages
    )
    config["timezone"] = effective_timezone
    _apply_locale_to_camoufox_config(config, effective_locale, effective_languages)

    geolocation = dict(options.get("geolocation") or profile.geolocation or {})
    for key in ("latitude", "longitude", "accuracy"):
        if geolocation.get(key) is not None:
            config[f"geolocation:{key}"] = float(geolocation[key])
        else:
            config.pop(f"geolocation:{key}", None)
    effective_webrtc_ipv4 = str(
        options.get("_auto_gpt_webrtc_ipv4") or profile.webrtc_ipv4
    )
    effective_webrtc_ipv6 = str(
        options.get("_auto_gpt_webrtc_ipv6") or profile.webrtc_ipv6
    )
    config["webrtc:ipv4"] = effective_webrtc_ipv4
    config["webrtc:ipv6"] = effective_webrtc_ipv6

    required_values = {
        "navigator.userAgent": config.get("navigator.userAgent"),
        "navigator.platform": config.get("navigator.platform"),
        "screen.width": config.get("screen.width"),
        "screen.height": config.get("screen.height"),
        "webGl:vendor": config.get("webGl:vendor"),
        "webGl:renderer": config.get("webGl:renderer"),
        "canvas:seed": config.get("canvas:seed"),
        "audio:seed": config.get("audio:seed"),
        "fonts:spacing_seed": config.get("fonts:spacing_seed"),
        "fonts": config.get("fonts"),
        "voices": config.get("voices"),
    }
    missing = [key for key, value in required_values.items() if value in (None, "", [], {})]
    if missing:
        raise RuntimeError(
            "Camoufox process fingerprint is incomplete: " + ",".join(missing)
        )
    return _json_safe(config)


def build_camoufox_context_spec(
    fingerprint: Any = None,
    *,
    context_options: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    """Build Playwright options and the 13 native v152 context setter calls."""

    profile = _resolve_camoufox_deep_profile(fingerprint)

    options = dict(context_options or {})
    effective_webrtc_ipv4 = str(
        options.pop("_auto_gpt_webrtc_ipv4", "") or profile.webrtc_ipv4
    )
    effective_webrtc_ipv6 = str(
        options.pop("_auto_gpt_webrtc_ipv6", "") or profile.webrtc_ipv6
    )
    options["user_agent"] = profile.user_agent
    options["viewport"] = {
        "width": int(profile.viewport_width),
        "height": int(profile.viewport_height),
    }
    options["device_scale_factor"] = float(profile.device_scale_factor or 1.0)
    # Camoufox applies locale and the complete language list at process level.
    # Playwright's Firefox context locale collapses navigator.languages to one
    # item after CAMOU_CONFIG has already installed the correlated profile.
    options.pop("locale", None)
    options.setdefault("timezone_id", profile.timezone)
    if profile.geolocation:
        options.setdefault("geolocation", dict(profile.geolocation))
        permissions = list(options.get("permissions") or [])
        if "geolocation" not in permissions:
            permissions.append("geolocation")
        options["permissions"] = permissions
    extra_headers = dict(options.get("extra_http_headers") or {})
    extra_headers["Accept-Language"] = profile.accept_language
    for key in list(extra_headers):
        if str(key).lower().startswith("sec-ch-"):
            extra_headers.pop(key, None)
    options["extra_http_headers"] = extra_headers

    effective_timezone = str(options.get("timezone_id") or profile.timezone)
    values = {
        "font_spacing_seed": int(profile.font_spacing_seed),
        "audio_seed": int(profile.audio_seed),
        "timezone": effective_timezone,
        "navigator_platform": profile.navigator_platform,
        "navigator_oscpu": profile.navigator_oscpu,
        "hardware_concurrency": int(profile.hardware_concurrency),
        "user_agent": profile.user_agent,
        "webrtc_ipv4": effective_webrtc_ipv4,
        "webrtc_ipv6": effective_webrtc_ipv6,
        "webgl_vendor": profile.webgl_vendor,
        "webgl_renderer": profile.webgl_renderer,
        "font_list": list(profile.font_list),
        "speech_voices": [
            str(item.get("name") or "") if isinstance(item, Mapping) else str(item)
            for item in profile.speech_voices
        ],
    }
    calls = (
        ("setFontSpacingSeed", [values["font_spacing_seed"]]),
        ("setAudioFingerprintSeed", [values["audio_seed"]]),
        ("setTimezone", [values["timezone"]]),
        ("setNavigatorPlatform", [values["navigator_platform"]]),
        ("setNavigatorOscpu", [values["navigator_oscpu"]]),
        (
            "setNavigatorHardwareConcurrency",
            [values["hardware_concurrency"]],
        ),
        ("setNavigatorUserAgent", [values["user_agent"]]),
        ("setWebRTCIPv4", [values["webrtc_ipv4"]]),
        ("setWebRTCIPv6", [values["webrtc_ipv6"]]),
        ("setWebGLVendor", [values["webgl_vendor"]]),
        ("setWebGLRenderer", [values["webgl_renderer"]]),
        ("setFontList", [",".join(values["font_list"])]),
        ("setSpeechVoices", [",".join(values["speech_voices"])]),
    )
    lines = ["(() => {", "  const w = window;"]
    for function_name, arguments in calls:
        encoded_arguments = ", ".join(
            json.dumps(item, ensure_ascii=True, separators=(",", ":"))
            for item in arguments
        )
        lines.append(
            f'  if (typeof w.{function_name} === "function") '
            f"w.{function_name}({encoded_arguments});"
        )
    lines.append("})();")
    return options, "\n".join(lines), browser_fingerprint_to_dict(profile)


def _resolve_chromium_deep_profile(fingerprint: Any = None) -> BrowserFingerprint:
    if fingerprint:
        profile = coerce_browser_fingerprint(fingerprint)
    else:
        profile = generate_browser_fingerprint(
            browser_family="chrome",
            deep_context=True,
        )
    if (
        profile.browser_family != "chrome"
        or profile.isolation_mode != CHROMIUM_DEEP_ISOLATION_MODE
        or not profile.chromium_config
    ):
        raise RuntimeError(
            "Patchright Chromium requires a persisted Chrome deep fingerprint"
        )
    if tuple(profile.context_capabilities) != CHROMIUM_CONTEXT_CAPABILITIES:
        raise RuntimeError("Chromium context fingerprint capability contract mismatch")
    return profile


def build_chromium_context_spec(
    fingerprint: Any = None,
    *,
    context_options: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], str, dict[str, Any], dict[str, Any]]:
    """Build one native Linux Chromium context without identity injection."""

    profile = _resolve_chromium_deep_profile(fingerprint)
    options = dict(context_options or {})
    options.pop("_auto_gpt_webrtc_ipv4", None)
    options.pop("_auto_gpt_webrtc_ipv6", None)
    for key in (
        "user_agent",
        "viewport",
        "screen",
        "device_scale_factor",
        "is_mobile",
        "has_touch",
    ):
        options.pop(key, None)
    options["no_viewport"] = True
    options.setdefault("locale", profile.locale)
    options.setdefault("timezone_id", profile.timezone)
    if profile.geolocation:
        options.setdefault("geolocation", dict(profile.geolocation))
        permissions = list(options.get("permissions") or [])
        if "geolocation" not in permissions:
            permissions.append("geolocation")
        options["permissions"] = permissions
    options.setdefault("ignore_https_errors", True)
    extra_headers = dict(options.get("extra_http_headers") or {})
    for key in list(extra_headers):
        if str(key).lower() in {"user-agent", "accept-language"} or str(
            key
        ).lower().startswith("sec-ch-"):
            extra_headers.pop(key, None)
    if extra_headers:
        options["extra_http_headers"] = extra_headers
    else:
        options.pop("extra_http_headers", None)
    return (
        options,
        "",
        {},
        browser_fingerprint_to_dict(profile),
    )


__all__ = [
    "BrowserFingerprint",
    "BrowserGeoIdentity",
    "BROWSER_ENGINE_ENV",
    "CAMOUFOX_BROWSER_RUNTIME",
    "CAMOUFOX_DEEP_ISOLATION_MODE",
    "CAMOUFOX_CONTEXT_SETTERS",
    "CAMOUFOX_ENGINE_RELEASE",
    "CAMOUFOX_ENGINE_VERSION",
    "CAMOUFOX_NATIVE_DEVICE_SCALE_FACTOR",
    "CHROMIUM_CONTEXT_CAPABILITIES",
    "CHROMIUM_DEEP_ISOLATION_MODE",
    "CHROMIUM_ENGINE_VERSION",
    "CHROMIUM_VISIBLE_VERSION",
    "DEEP_BROWSER_FAMILIES",
    "DEEP_BROWSER_OPERATING_SYSTEM",
    "FINGERPRINT_SCHEMA_VERSION",
    "LATEST_CURL_IMPERSONATE",
    "PROTOCOL_BROWSER_FAMILIES",
    "PATCHRIGHT_BROWSER_RUNTIME",
    "PATCHRIGHT_PACKAGE_VERSION",
    "REGISTER_BROWSER_FAMILY_OPTIONS",
    "browser_fingerprint_to_dict",
    "browser_backend_for_family",
    "build_camoufox_context_spec",
    "build_camoufox_process_config",
    "build_chromium_context_spec",
    "coerce_browser_fingerprint",
    "configured_browser_runtime",
    "configured_deep_browser_family",
    "generate_browser_fingerprint",
    "infer_browser_family",
    "merge_observed_browser_fingerprint",
    "normalize_browser_family",
    "normalize_browser_runtime",
    "normalize_protocol_browser_family",
    "rebind_browser_fingerprint_geo",
    "resolve_browser_geo_identity",
    "select_protocol_browser_family",
]
