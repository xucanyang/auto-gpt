"""Versioned browser identities shared by HTTP, Sentinel, and Camoufox.

The transport identity is intentionally explicit.  curl_cffi aliases such as
``chrome`` move when the dependency changes, so persisted accounts always keep
the concrete impersonation target that created their session.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import random
import re
import secrets
import uuid
from dataclasses import asdict, dataclass, field, fields, is_dataclass, replace
from typing import Any, Mapping


FINGERPRINT_SCHEMA_VERSION = 2
CAMOUFOX_ENGINE_VERSION = "152.0.4"
CAMOUFOX_ENGINE_RELEASE = "beta.28"
CAMOUFOX_VISIBLE_FIREFOX_VERSION = "147.0"
CAMOUFOX_DEEP_ISOLATION_MODE = "process_isolated_context_deep_native"
CHROMIUM_ENGINE_VERSION = "148.0.7778.96"
CHROMIUM_VISIBLE_VERSION = "148.0.0.0"
CHROMIUM_DEEP_ISOLATION_MODE = "process_isolated_context_patchright_chromium"
DEEP_BROWSER_OPERATING_SYSTEM = "macos"
DEEP_BROWSER_FAMILIES = ("chrome", "firefox")

# These are the newest concrete targets that curl_cffi 0.16.0 actually ships.
# Do not replace them with moving aliases (chrome/firefox/safari).
LATEST_CURL_IMPERSONATE = {
    "chrome": "chrome146",
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
    "cdp_user_agent_metadata",
    "navigator_platform",
    "navigator_hardware",
    "screen_geometry",
    "timezone_locale",
    "geolocation",
    "webrtc_proxy_only",
    "webgl_identity",
    "canvas_seed",
    "audio_seed",
    "font_profile",
    "speech_voices",
    "media_devices",
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

_PROFILE_TEMPLATES: dict[str, dict[str, Any]] = {
    "chrome": {
        "browser_version": "146.0.0.0",
        "browser_major": 146,
        "engine_family": "chromium",
        "engine_version": "146.0.0.0",
        "user_agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/146.0.0.0 Safari/537.36"
        ),
        "sec_ch_ua": (
            '"Chromium";v="146", "Not-A.Brand";v="24", '
            '"Google Chrome";v="146"'
        ),
        "platform_version": "15.7.0",
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
    transport_library_version: str = "0.16.0"
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
) -> BrowserFingerprint:
    template = _PROFILE_TEMPLATES[family]
    sw, sh, aw, ah, vw, vh, dpr = random.choice(_SCREENS)
    language_header = str(accept_language or random.choice(_ACCEPT_LANGUAGES))
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
        preset_id=f"curl-cffi-0.16.0:{LATEST_CURL_IMPERSONATE[family]}",
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
                "sec_ch_ua_platform": '"macOS"',
                "sec_ch_ua_platform_version": str(template["platform_version"]),
            }
            if family == "chrome"
            else {}
        ),
        locale=_language_parts(language_header)[0],
        languages=_language_parts(language_header),
        timezone=str(timezone or "America/New_York"),
        screen_width=sw,
        screen_height=sh,
        screen_avail_width=aw,
        screen_avail_height=ah,
        outer_width=min(sw, vw + 16),
        outer_height=min(ah, vh + 88),
        device_scale_factor=dpr,
        hardware_concurrency=secrets.choice((8, 10, 12, 16)),
        webgl_vendor="Apple Inc.",
        webgl_renderer="Apple M1",
        canvas_seed=seeds[0],
        audio_seed=seeds[1],
        font_spacing_seed=seeds[2],
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
    context_options = dict(generated.get("context_options") or {})
    screen = context_options.get("viewport") or {}
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

    screen_width = int(config.get("screen.width") or base.screen_width)
    screen_height = int(config.get("screen.height") or base.screen_height)
    screen_avail_width = int(config.get("screen.availWidth") or screen_width)
    screen_avail_height = int(config.get("screen.availHeight") or screen_height)
    if screen_avail_width == screen_width and screen_avail_height == screen_height:
        taskbar_height = {"linux": 27, "macos": 25, "windows": 40}[
            target_profile_os
        ]
        screen_avail_height = max(1, screen_height - taskbar_height)
    viewport_width = min(
        int(screen.get("width") or screen_avail_width),
        screen_avail_width,
    )
    viewport_height = min(
        int(screen.get("height") or screen_avail_height),
        screen_avail_height,
    )
    outer_width = max(viewport_width, min(screen_avail_width, viewport_width + 16))
    outer_height = max(viewport_height, min(screen_avail_height, viewport_height + 1))
    device_scale_factor = float(
        context_options.get("device_scale_factor")
        or config.get("window.devicePixelRatio")
        or base.device_scale_factor
    )
    language_parts = _language_parts(base.accept_language)
    config.update(
        {
            "navigator.languages": list(language_parts),
            "screen.availWidth": screen_avail_width,
            "screen.availHeight": screen_avail_height,
            "screen.availTop": 0,
            "screen.availLeft": 0,
            "window.outerWidth": outer_width,
            "window.outerHeight": outer_height,
            "window.innerWidth": viewport_width,
            "window.innerHeight": viewport_height,
            "window.screenX": 0,
            "window.screenY": 0,
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


def _chromium_fingerprint(base: BrowserFingerprint) -> BrowserFingerprint:
    try:
        from browserforge.fingerprints import FingerprintGenerator
    except Exception as exc:  # pragma: no cover - exercised by image preflight
        raise RuntimeError(f"BrowserForge fingerprint API unavailable: {exc}") from exc

    try:
        generated = FingerprintGenerator(
            browser=["chrome"],
            os=["macos"],
            device="desktop",
            locale=[base.locale],
            strict=True,
        ).generate()
    except Exception as exc:
        raise RuntimeError(f"BrowserForge macOS Chrome profile unavailable: {exc}") from exc

    if is_dataclass(generated):
        raw_profile = asdict(generated)
    elif isinstance(generated, Mapping):
        raw_profile = dict(generated)
    else:
        raise RuntimeError("BrowserForge returned an unsupported fingerprint payload")

    navigator = dict(raw_profile.get("navigator") or {})
    video_card = dict(raw_profile.get("videoCard") or {})
    multimedia_devices = dict(raw_profile.get("multimediaDevices") or {})
    generated_fonts = [str(item) for item in list(raw_profile.get("fonts") or []) if item]
    font_list = tuple(
        dict.fromkeys(
            generated_fonts
            + [
                "Arial",
                "Arial Black",
                "Arial Narrow",
                "Arial Rounded MT Bold",
                "Avenir",
                "Avenir Next",
                "Courier New",
                "Geneva",
                "Georgia",
                "Helvetica",
                "Helvetica Neue",
                "Menlo",
                "Monaco",
                "SF Pro Display",
                "SF Pro Text",
                "Times New Roman",
                "Trebuchet MS",
                "Verdana",
            ]
        )
    )
    speech_voices = (
        {"name": "Samantha", "lang": "en-US", "localService": True, "default": True},
        {"name": "Alex", "lang": "en-US", "localService": True, "default": False},
        {"name": "Daniel", "lang": "en-GB", "localService": True, "default": False},
        {"name": "Karen", "lang": "en-AU", "localService": True, "default": False},
    )

    browser_major = int(CHROMIUM_ENGINE_VERSION.split(".", 1)[0])
    user_agent = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{CHROMIUM_VISIBLE_VERSION} Safari/537.36"
    )
    brands = (
        {"brand": "Chromium", "version": str(browser_major)},
        {"brand": "Not_A Brand", "version": "24"},
        {"brand": "Google Chrome", "version": str(browser_major)},
    )
    full_version_list = tuple(
        {
            "brand": item["brand"],
            "version": (
                CHROMIUM_ENGINE_VERSION
                if item["brand"] != "Not_A Brand"
                else "24.0.0.0"
            ),
        }
        for item in brands
    )
    sec_ch_ua = ", ".join(
        f'"{item["brand"]}";v="{item["version"]}"' for item in brands
    )
    architecture = str(
        (navigator.get("userAgentData") or {}).get("architecture") or "arm"
    ).strip().lower()
    if architecture not in {"arm", "x86"}:
        architecture = "arm"
    user_agent_metadata = {
        "brands": [dict(item) for item in brands],
        "fullVersionList": [dict(item) for item in full_version_list],
        "fullVersion": CHROMIUM_ENGINE_VERSION,
        "platform": "macOS",
        "platformVersion": "15.7.0",
        "architecture": architecture,
        "model": "",
        "mobile": False,
        "bitness": "64",
        "wow64": False,
    }
    raw_profile["navigator"] = {
        **navigator,
        "userAgent": user_agent,
        "appVersion": user_agent.removeprefix("Mozilla/"),
        "platform": "MacIntel",
        "oscpu": None,
        "userAgentData": user_agent_metadata,
    }
    raw_profile["headers"] = {
        **dict(raw_profile.get("headers") or {}),
        "User-Agent": user_agent,
        "Accept-Language": base.accept_language,
        "sec-ch-ua": sec_ch_ua,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
    }
    raw_profile["fonts"] = list(font_list)
    raw_profile["speechVoices"] = [dict(item) for item in speech_voices]
    raw_profile["userAgentMetadata"] = user_agent_metadata

    webgl_vendor = str(video_card.get("vendor") or "Google Inc. (Apple)")
    webgl_renderer = str(
        video_card.get("renderer")
        or "ANGLE (Apple, ANGLE Metal Renderer: Apple M2, Unspecified Version)"
    )
    profile_id = str(uuid.uuid4())
    return replace(
        base,
        profile_id=profile_id,
        preset_id=f"browserforge-1.2.4:{_profile_hash(raw_profile)}",
        browser_version=CHROMIUM_ENGINE_VERSION,
        browser_major=browser_major,
        engine_family="chromium",
        engine_version=CHROMIUM_ENGINE_VERSION,
        chrome_major=browser_major,
        chrome_full_version=CHROMIUM_ENGINE_VERSION,
        user_agent=user_agent,
        sec_ch_ua=sec_ch_ua,
        platform_version="15.7.0",
        client_hints={
            "sec_ch_ua": sec_ch_ua,
            "sec_ch_ua_mobile": "?0",
            "sec_ch_ua_platform": '"macOS"',
            "sec_ch_ua_platform_version": '"15.7.0"',
            "sec_ch_ua_arch": f'"{architecture}"',
            "sec_ch_ua_bitness": '"64"',
            "sec_ch_ua_full_version": f'"{CHROMIUM_ENGINE_VERSION}"',
        },
        operating_system="macos",
        architecture="arm64" if architecture == "arm" else "x86_64",
        navigator_platform="MacIntel",
        navigator_oscpu="",
        # Patchright's Chromium 148 surface reports 8 consistently in this
        # image. Keep the persisted value aligned with the real engine rather
        # than accepting BrowserForge samples that Chromium later clamps.
        hardware_concurrency=8,
        device_memory=max(min(int(navigator.get("deviceMemory") or 8), 8), 4),
        max_touch_points=0,
        webgl_vendor=webgl_vendor,
        webgl_renderer=webgl_renderer,
        font_list=font_list,
        speech_voices=speech_voices,
        media_devices=_json_safe(multimedia_devices),
        chromium_config=_json_safe(raw_profile),
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
    timezone: str = "America/New_York",
) -> BrowserFingerprint:
    family = normalize_browser_family(browser_family)
    if deep_context and family not in DEEP_BROWSER_FAMILIES:
        raise ValueError("deep browser contexts support only Chrome or Firefox")
    base = _generic_fingerprint(
        family=family,
        device_id=device_id,
        accept_language=accept_language,
        timezone=timezone,
    )
    if not deep_context:
        return base
    return _camoufox_fingerprint(base) if family == "firefox" else _chromium_fingerprint(base)


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
        return fingerprint

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
    return BrowserFingerprint(**values)


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
    options.setdefault("locale", profile.locale)
    options.setdefault("timezone_id", profile.timezone)
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
    """Build one coherent macOS Chrome context and CDP identity override."""

    profile = _resolve_chromium_deep_profile(fingerprint)
    options = dict(context_options or {})
    options.pop("_auto_gpt_webrtc_ipv4", None)
    options.pop("_auto_gpt_webrtc_ipv6", None)
    options["user_agent"] = profile.user_agent
    options["viewport"] = {
        "width": int(profile.viewport_width),
        "height": int(profile.viewport_height),
    }
    options["screen"] = {
        "width": int(profile.screen_width or profile.viewport_width),
        "height": int(profile.screen_height or profile.viewport_height),
    }
    options["device_scale_factor"] = float(profile.device_scale_factor or 1.0)
    options.setdefault("locale", profile.locale)
    options.setdefault("timezone_id", profile.timezone)
    options.setdefault("ignore_https_errors", True)
    extra_headers = dict(options.get("extra_http_headers") or {})
    extra_headers["Accept-Language"] = profile.accept_language
    for key in list(extra_headers):
        if str(key).lower().startswith("sec-ch-"):
            extra_headers.pop(key, None)
    options["extra_http_headers"] = extra_headers

    metadata = dict(profile.chromium_config.get("userAgentMetadata") or {})
    if not metadata:
        raise RuntimeError("Chromium fingerprint is missing userAgentMetadata")
    cdp_override = {
        "userAgent": profile.user_agent,
        "acceptLanguage": ",".join(profile.languages),
        "platform": profile.navigator_platform,
        "userAgentMetadata": metadata,
    }

    script_profile = {
        "language": profile.locale,
        "languages": list(profile.languages),
        "platform": profile.navigator_platform,
        "hardwareConcurrency": int(profile.hardware_concurrency),
        "deviceMemory": int(profile.device_memory),
        "maxTouchPoints": int(profile.max_touch_points),
        "screen": {
            "width": int(profile.screen_width),
            "height": int(profile.screen_height),
            "availWidth": int(profile.screen_avail_width),
            "availHeight": int(profile.screen_avail_height),
            "colorDepth": int(profile.color_depth),
            "pixelDepth": int(profile.pixel_depth),
        },
        "webglVendor": profile.webgl_vendor,
        "webglRenderer": profile.webgl_renderer,
        "canvasSeed": int(profile.canvas_seed),
        "audioSeed": int(profile.audio_seed),
        "fonts": list(profile.font_list),
        "voices": [dict(item) for item in profile.speech_voices],
        "mediaDevices": dict(profile.media_devices or {}),
    }
    encoded_profile = json.dumps(
        script_profile,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    init_script = f"""
(() => {{
  const profile = {encoded_profile};
  const define = (target, key, getter) => {{
    try {{ Object.defineProperty(target, key, {{ get: getter, configurable: true }}); }} catch (_) {{}}
  }};
  const navigatorProto = globalThis.Navigator && Navigator.prototype;
  if (navigatorProto) {{
    define(navigatorProto, 'platform', () => profile.platform);
    define(navigatorProto, 'language', () => profile.language);
    define(navigatorProto, 'languages', () => Object.freeze([...profile.languages]));
    define(navigatorProto, 'hardwareConcurrency', () => profile.hardwareConcurrency);
    define(navigatorProto, 'deviceMemory', () => profile.deviceMemory);
    define(navigatorProto, 'maxTouchPoints', () => profile.maxTouchPoints);
  }}
  const screenProto = globalThis.Screen && Screen.prototype;
  if (screenProto) {{
    for (const [key, value] of Object.entries(profile.screen)) {{
      if (value > 0) define(screenProto, key, () => value);
    }}
  }}
  const patchWebGL = (prototype) => {{
    if (!prototype || typeof prototype.getParameter !== 'function') return;
    const nativeGetParameter = prototype.getParameter;
    Object.defineProperty(prototype, 'getParameter', {{
      configurable: true,
      value(parameter) {{
        if (parameter === 37445) return profile.webglVendor;
        if (parameter === 37446) return profile.webglRenderer;
        return nativeGetParameter.call(this, parameter);
      }},
    }});
  }};
  patchWebGL(globalThis.WebGLRenderingContext && WebGLRenderingContext.prototype);
  patchWebGL(globalThis.WebGL2RenderingContext && WebGL2RenderingContext.prototype);

  const canvasProto = globalThis.HTMLCanvasElement && HTMLCanvasElement.prototype;
  const context2dProto = globalThis.CanvasRenderingContext2D && CanvasRenderingContext2D.prototype;
  if (canvasProto && context2dProto && typeof canvasProto.toDataURL === 'function') {{
    const nativeToDataURL = canvasProto.toDataURL;
    const nativeGetImageData = context2dProto.getImageData;
    const nativePutImageData = context2dProto.putImageData;
    const nativeDrawImage = context2dProto.drawImage;
    Object.defineProperty(canvasProto, 'toDataURL', {{
      configurable: true,
      value(...args) {{
        if (!this.width || !this.height || this.width > 512 || this.height > 512) {{
          return nativeToDataURL.apply(this, args);
        }}
        try {{
          const clone = document.createElement('canvas');
          clone.width = this.width;
          clone.height = this.height;
          const ctx = clone.getContext('2d');
          nativeDrawImage.call(ctx, this, 0, 0);
          const x = Math.abs(profile.canvasSeed) % this.width;
          const y = Math.abs(profile.canvasSeed >>> 8) % this.height;
          const pixel = nativeGetImageData.call(ctx, x, y, 1, 1);
          pixel.data[0] = (pixel.data[0] + (profile.canvasSeed % 3) + 1) & 255;
          nativePutImageData.call(ctx, pixel, x, y);
          return nativeToDataURL.apply(clone, args);
        }} catch (_) {{
          return nativeToDataURL.apply(this, args);
        }}
      }},
    }});
  }}

  const audioProto = globalThis.AudioBuffer && AudioBuffer.prototype;
  if (audioProto && typeof audioProto.getChannelData === 'function') {{
    const nativeGetChannelData = audioProto.getChannelData;
    const adjusted = new WeakSet();
    Object.defineProperty(audioProto, 'getChannelData', {{
      configurable: true,
      value(channel) {{
        const data = nativeGetChannelData.call(this, channel);
        if (data && data.length && !adjusted.has(data)) {{
          adjusted.add(data);
          const index = Math.abs(profile.audioSeed) % data.length;
          data[index] += ((profile.audioSeed % 97) + 1) * 1e-10;
        }}
        return data;
      }},
    }});
  }}

  if (document.fonts && typeof document.fonts.check === 'function') {{
    const nativeFontCheck = document.fonts.check.bind(document.fonts);
    const knownFonts = new Set(profile.fonts.map((font) => font.toLowerCase()));
    document.fonts.check = (font, text) => {{
      const family = String(font || '').replace(/^\\s*[^ ]+\\s+/, '').replace(/["']/g, '').trim().toLowerCase();
      return knownFonts.has(family) || nativeFontCheck(font, text);
    }};
  }}
  if (globalThis.speechSynthesis && typeof speechSynthesis.getVoices === 'function') {{
    const nativeGetVoices = speechSynthesis.getVoices.bind(speechSynthesis);
    speechSynthesis.getVoices = () => {{
      const voices = nativeGetVoices();
      return voices.length ? voices : profile.voices.map((voice) => Object.freeze({{...voice, voiceURI: voice.name}}));
    }};
  }}
  if (navigator.mediaDevices && typeof navigator.mediaDevices.enumerateDevices === 'function') {{
    const nativeEnumerateDevices = navigator.mediaDevices.enumerateDevices.bind(navigator.mediaDevices);
    navigator.mediaDevices.enumerateDevices = async () => {{
      const devices = await nativeEnumerateDevices();
      if (devices.length) return devices;
      const configured = [
        ...(profile.mediaDevices.speakers || []),
        ...(profile.mediaDevices.micros || []),
        ...(profile.mediaDevices.webcams || []),
      ];
      return configured.map((device) => Object.freeze({{...device, toJSON: () => ({{...device}})}}));
    }};
  }}
}})();
""".strip()
    return (
        options,
        init_script,
        _json_safe(cdp_override),
        browser_fingerprint_to_dict(profile),
    )


__all__ = [
    "BrowserFingerprint",
    "CAMOUFOX_DEEP_ISOLATION_MODE",
    "CAMOUFOX_CONTEXT_SETTERS",
    "CAMOUFOX_ENGINE_RELEASE",
    "CAMOUFOX_ENGINE_VERSION",
    "CHROMIUM_CONTEXT_CAPABILITIES",
    "CHROMIUM_DEEP_ISOLATION_MODE",
    "CHROMIUM_ENGINE_VERSION",
    "CHROMIUM_VISIBLE_VERSION",
    "DEEP_BROWSER_FAMILIES",
    "DEEP_BROWSER_OPERATING_SYSTEM",
    "FINGERPRINT_SCHEMA_VERSION",
    "LATEST_CURL_IMPERSONATE",
    "PROTOCOL_BROWSER_FAMILIES",
    "REGISTER_BROWSER_FAMILY_OPTIONS",
    "browser_fingerprint_to_dict",
    "browser_backend_for_family",
    "build_camoufox_context_spec",
    "build_camoufox_process_config",
    "build_chromium_context_spec",
    "coerce_browser_fingerprint",
    "generate_browser_fingerprint",
    "infer_browser_family",
    "merge_observed_browser_fingerprint",
    "normalize_browser_family",
    "normalize_protocol_browser_family",
    "select_protocol_browser_family",
]
