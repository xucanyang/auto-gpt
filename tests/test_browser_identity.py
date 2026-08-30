from dataclasses import replace
from types import SimpleNamespace
from unittest import mock

import pytest
from curl_cffi import requests as cffi_requests

from services.chatgpt_core.account_fingerprint import (
    build_browser_fingerprint_payload,
    persist_account_browser_fingerprint,
)
from services.chatgpt_core.browser_identity import (
    BrowserGeoIdentity,
    CAMOUFOX_CONTEXT_SETTERS,
    CAMOUFOX_DEEP_ISOLATION_MODE,
    CAMOUFOX_NATIVE_DEVICE_SCALE_FACTOR,
    CHROMIUM_CONTEXT_CAPABILITIES,
    CHROMIUM_DEEP_ISOLATION_MODE,
    CHROMIUM_ENGINE_VERSION,
    LATEST_CURL_IMPERSONATE,
    build_camoufox_context_spec,
    build_camoufox_process_config,
    build_chromium_context_spec,
    coerce_browser_fingerprint,
    configured_camoufox_target_os,
    configured_deep_browser_operating_system,
    generate_browser_fingerprint,
    normalize_camoufox_target_os,
    normalize_protocol_browser_family,
    rebind_browser_fingerprint_geo,
    resolve_browser_geo_identity,
    select_protocol_browser_family,
)
from services.chatgpt_core.shared_browser import ensure_deep_browser_fingerprint
from services.chatgpt_core.sentinel_token import SentinelTokenGenerator
from services.chatgpt_core.utils import apply_browser_fingerprint
from services.chatgpt_core.any_auto import transport
from services.chatgpt_core.any_auto.browser_register import (
    _build_browser_headers as _build_any_auto_browser_headers,
)
from services.chatgpt_core.browser_registration import (
    _build_browser_headers as _build_registration_browser_headers,
)
from services.chatgpt_core.any_auto.http_client import OpenAIHTTPClient
from platforms.chatgpt import utils as platform_utils


def _lower_headers(session) -> dict[str, str]:
    return {str(key).lower(): str(value) for key, value in session.headers.items()}


def test_latest_concrete_transport_profiles_are_family_consistent():
    assert LATEST_CURL_IMPERSONATE == {
        "chrome": "chrome150",
        "firefox": "firefox147",
        "safari": "safari2601",
    }

    for family, target in LATEST_CURL_IMPERSONATE.items():
        fingerprint = generate_browser_fingerprint(browser_family=family)
        session = cffi_requests.Session(impersonate=target)
        apply_browser_fingerprint(session, fingerprint)
        headers = _lower_headers(session)

        assert fingerprint.browser_family == family
        assert fingerprint.impersonate == target
        assert headers["user-agent"] == fingerprint.user_agent
        assert ("sec-ch-ua" in headers) is (family == "chrome")


def test_protocol_family_selection_honors_explicit_allowlist():
    with mock.patch.dict(
        "os.environ",
        {"CHATGPT_PROTOCOL_BROWSER_FAMILIES": "safari"},
        clear=False,
    ):
        assert select_protocol_browser_family() == "safari"
        platform_fingerprint = platform_utils.coerce_browser_fingerprint()
        assert platform_fingerprint.browser_family == "safari"
        assert platform_fingerprint.impersonate == "safari2601"
        platform_session = cffi_requests.Session(impersonate="safari2601")
        platform_utils.apply_browser_fingerprint(
            platform_session,
            platform_fingerprint,
        )
        assert not any(
            str(key).lower().startswith("sec-ch-")
            for key in platform_session.headers
        )


def test_protocol_family_selection_honors_explicit_family():
    with mock.patch.dict(
        "os.environ",
        {"CHATGPT_PROTOCOL_BROWSER_FAMILIES": "safari"},
        clear=False,
    ):
        for family in ("chrome", "firefox", "safari"):
            assert normalize_protocol_browser_family(family) == family
            assert select_protocol_browser_family(family) == family

        assert normalize_protocol_browser_family("random") == "random"
        assert select_protocol_browser_family("random") == "safari"
        assert normalize_protocol_browser_family("unsupported", default="") == ""


def test_exit_ip_geo_identity_freezes_locale_timezone_coordinates_and_webrtc():
    resolved = SimpleNamespace(
        locale=SimpleNamespace(as_string="rej-ID", region="ID"),
        longitude=106.1922,
        latitude=-2.3406,
        timezone="Asia/Pontianak",
        accuracy=25.0,
    )
    with (
        mock.patch("camoufox.geolocation.geoip_allowed"),
        mock.patch(
            "camoufox.geolocation.get_geolocation",
            return_value=resolved,
        ) as get_geolocation,
        mock.patch(
            "services.chatgpt_core.browser_identity._primary_locale_for_country",
            return_value="id-ID",
        ) as primary_locale,
    ):
        geo = resolve_browser_geo_identity(
            "103.189.207.248",
            country_code="US",
        )

    assert geo.country_code == "ID"
    assert geo.timezone == "Asia/Pontianak"
    assert geo.locale == "id-ID"
    assert geo.languages == ("id-ID", "id", "en-US", "en")
    assert geo.accept_language == "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7"
    assert geo.geolocation == {
        "latitude": -2.3406,
        "longitude": 106.1922,
        "accuracy": 25.0,
    }
    assert geo.webrtc_ipv4 == "103.189.207.248"
    assert geo.webrtc_ipv6 == ""
    assert geo.source == "maxmind_geoip"
    get_geolocation.assert_called_once_with("103.189.207.248")
    primary_locale.assert_called_once_with("ID")


def test_country_primary_locale_is_stable_and_not_a_statistical_sample():
    languages = ["rej", "id", "jv"]
    probabilities = [0.01, 0.70, 0.29]
    with mock.patch(
        "camoufox.locales.SELECTOR._load_territory_data",
        return_value=(languages, probabilities),
    ):
        from services.chatgpt_core.browser_identity import _primary_locale_for_country

        assert _primary_locale_for_country("ID") == "id-ID"
        assert _primary_locale_for_country("id") == "id-ID"


def test_geo_identity_is_shared_by_transport_camoufox_and_browser_context():
    geo = BrowserGeoIdentity(
        exit_ip="103.189.207.248",
        country_code="ID",
        timezone="Asia/Pontianak",
        locale="id-ID",
        languages=("id-ID", "id", "en-US", "en"),
        accept_language="id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
        geolocation={"latitude": -2.3406, "longitude": 106.1922},
        webrtc_ipv4="103.189.207.248",
        source="maxmind_geoip",
    )
    fingerprint = generate_browser_fingerprint(
        browser_family="firefox",
        deep_context=True,
        geo_identity=geo,
    )
    transport_session = cffi_requests.Session(impersonate="firefox147")
    apply_browser_fingerprint(transport_session, fingerprint)
    options, init_script, _payload = build_camoufox_context_spec(fingerprint)
    process_config = build_camoufox_process_config(fingerprint)

    assert fingerprint.timezone == geo.timezone
    assert fingerprint.locale == geo.locale
    assert fingerprint.languages == geo.languages
    assert fingerprint.accept_language == geo.accept_language
    assert fingerprint.geolocation == geo.geolocation
    assert fingerprint.webrtc_ipv4 == geo.exit_ip
    assert (
        _lower_headers(transport_session)["accept-language"]
        == geo.accept_language
    )
    assert options["timezone_id"] == geo.timezone
    assert "locale" not in options
    assert options["geolocation"] == geo.geolocation
    assert options["permissions"] == ["geolocation"]
    assert options["extra_http_headers"]["Accept-Language"] == geo.accept_language
    assert geo.exit_ip in init_script
    assert process_config["timezone"] == geo.timezone
    assert process_config["navigator.language"] == geo.locale
    assert process_config["navigator.languages"] == list(geo.languages)
    assert process_config["headers.Accept-Language"] == geo.accept_language
    assert process_config["geolocation:latitude"] == -2.3406


def test_checkout_geo_rebind_preserves_device_and_non_geo_entropy():
    original = generate_browser_fingerprint(
        browser_family="firefox",
        deep_context=True,
        geo_identity=BrowserGeoIdentity(
            exit_ip="198.51.100.10",
            country_code="US",
            timezone="America/Los_Angeles",
            locale="en-US",
            languages=("en-US", "en"),
            accept_language="en-US,en;q=0.9",
            geolocation={"latitude": 34.05, "longitude": -118.24},
            webrtc_ipv4="198.51.100.10",
            source="maxmind_geoip",
        ),
    )
    checkout_geo = BrowserGeoIdentity(
        exit_ip="203.0.113.20",
        country_code="ID",
        timezone="Asia/Jakarta",
        locale="id-ID",
        languages=("id-ID", "id", "en-US", "en"),
        accept_language="id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
        geolocation={"latitude": -6.2, "longitude": 106.816666},
        webrtc_ipv4="203.0.113.20",
        source="maxmind_geoip",
    )

    rebound = rebind_browser_fingerprint_geo(original, checkout_geo)
    options, _init_script, _payload = build_camoufox_context_spec(rebound)
    process_config = build_camoufox_process_config(rebound)

    assert rebound.device_id == original.device_id
    assert rebound.profile_id == original.profile_id
    assert rebound.preset_id == original.preset_id
    assert rebound.canvas_seed == original.canvas_seed
    assert rebound.audio_seed == original.audio_seed
    assert rebound.font_spacing_seed == original.font_spacing_seed
    assert rebound.webgl_renderer == original.webgl_renderer
    assert rebound.timezone == "Asia/Jakarta"
    assert rebound.locale == "id-ID"
    assert rebound.webrtc_ipv4 == "203.0.113.20"
    assert rebound.geolocation == checkout_geo.geolocation
    assert options["timezone_id"] == "Asia/Jakarta"
    assert options["geolocation"] == checkout_geo.geolocation
    assert process_config["timezone"] == "Asia/Jakarta"
    assert process_config["navigator.language"] == "id-ID"
    assert process_config["webrtc:ipv4"] == "203.0.113.20"


def test_page_fetch_headers_do_not_override_geo_aligned_context_language():
    for builder in (
        _build_any_auto_browser_headers,
        _build_registration_browser_headers,
    ):
        headers = builder(
            user_agent="Mozilla/5.0 Firefox/147.0",
            accept="application/json",
        )
        assert "accept-language" not in {
            str(key).lower(): value for key, value in headers.items()
        }


def test_camoufox_deep_profile_contains_official_context_contract():
    fingerprint = generate_browser_fingerprint(
        browser_family="firefox",
        deep_context=True,
    )
    options, init_script, payload = build_camoufox_context_spec(
        fingerprint,
        context_options={
            "timezone_id": "Asia/Tokyo",
            "locale": "ja-JP",
            "_auto_gpt_webrtc_ipv4": "203.0.113.10",
        },
    )

    assert fingerprint.browser_version == "147.0"
    assert fingerprint.camoufox_binary_version == "152.0.4"
    assert fingerprint.camoufox_release == "beta.28"
    assert fingerprint.isolation_mode == CAMOUFOX_DEEP_ISOLATION_MODE
    assert fingerprint.operating_system == "macos"
    assert fingerprint.browser_backend == "camoufox_firefox"
    assert fingerprint.device_scale_factor == CAMOUFOX_NATIVE_DEVICE_SCALE_FACTOR
    assert (
        fingerprint.camoufox_config["window.devicePixelRatio"]
        == CAMOUFOX_NATIVE_DEVICE_SCALE_FACTOR
    )
    assert fingerprint.navigator_platform == "MacIntel"
    assert "Mac OS X" in fingerprint.navigator_oscpu
    assert 1280 <= fingerprint.viewport_width <= 1536
    assert 720 <= fingerprint.viewport_height <= 900
    assert 1440 <= fingerprint.screen_width <= 1920
    assert 900 <= fingerprint.screen_height <= 1080
    assert fingerprint.outer_width - fingerprint.viewport_width == 16
    assert fingerprint.outer_height - fingerprint.viewport_height == 88
    assert fingerprint.color_depth == 24
    assert fingerprint.pixel_depth == 24
    assert fingerprint.camoufox_config["screen.availTop"] == 25
    assert fingerprint.camoufox_config["window.screenY"] == 25
    assert tuple(payload["context_capabilities"]) == CAMOUFOX_CONTEXT_SETTERS
    assert len(payload["font_list"]) >= 30
    assert {"Arial", "Helvetica", "Menlo", "Monaco"}.issubset(
        payload["font_list"]
    )
    assert len(payload["speech_voices"]) > 10
    assert len(CAMOUFOX_CONTEXT_SETTERS) == 13
    assert options["timezone_id"] == "Asia/Tokyo"
    assert "locale" not in options
    assert "203.0.113.10" in init_script
    assert all(name in init_script for name in CAMOUFOX_CONTEXT_SETTERS)
    assert "setScreenDimensions" not in init_script
    assert "setScreenColorDepth" not in init_script
    assert "setCanvasSeed" not in init_script
    assert not any(
        str(key).lower().startswith("sec-ch-")
        for key in options["extra_http_headers"]
    )

    process_config = build_camoufox_process_config(
        fingerprint,
        context_options={
            "timezone_id": "Asia/Tokyo",
            "locale": "ja-JP",
            "_auto_gpt_webrtc_ipv4": "203.0.113.10",
            "geolocation": {
                "latitude": 35.6762,
                "longitude": 139.6503,
                "accuracy": 20,
            },
        },
    )
    assert process_config["canvas:seed"] == fingerprint.canvas_seed
    assert process_config["audio:seed"] == fingerprint.audio_seed
    assert process_config["fonts:spacing_seed"] == fingerprint.font_spacing_seed
    assert process_config["screen.width"] == fingerprint.screen_width
    assert process_config["screen.height"] == fingerprint.screen_height
    assert (
        process_config["window.devicePixelRatio"]
        == CAMOUFOX_NATIVE_DEVICE_SCALE_FACTOR
    )
    assert process_config["timezone"] == "Asia/Tokyo"
    assert process_config["navigator.language"] == "ja-JP"
    assert process_config["webrtc:ipv4"] == "203.0.113.10"
    assert process_config["geolocation:latitude"] == 35.6762
    assert process_config["fonts"] == list(fingerprint.font_list)
    assert process_config["voices"] == list(fingerprint.speech_voices)
    assert process_config["mediaDevices:enabled"] is True
    assert process_config["mediaDevices:micros"] == 1
    assert process_config["webGl:parameters"]


def test_camoufox_linux_target_restores_coherent_native_profile(monkeypatch):
    monkeypatch.setenv("CHATGPT_CAMOUFOX_TARGET_OS", "linux")

    fingerprint = generate_browser_fingerprint(
        browser_family="firefox",
        deep_context=True,
    )
    process_config = build_camoufox_process_config(fingerprint)

    assert fingerprint.operating_system == "linux"
    assert "X11;" in fingerprint.user_agent
    assert "Linux" in fingerprint.user_agent
    assert fingerprint.navigator_platform == "Linux x86_64"
    assert fingerprint.navigator_oscpu == "Linux x86_64"
    assert fingerprint.screen_width in {1366, 1536, 1600, 1680, 1920}
    assert fingerprint.screen_height - fingerprint.screen_avail_height == 27
    assert fingerprint.screen_width == fingerprint.screen_avail_width
    assert fingerprint.outer_width == fingerprint.screen_avail_width
    assert fingerprint.outer_height == fingerprint.screen_avail_height
    assert fingerprint.outer_width == fingerprint.viewport_width
    assert fingerprint.outer_height - fingerprint.viewport_height == 1
    assert fingerprint.camoufox_config["screen.availTop"] == 0
    assert fingerprint.camoufox_config["window.screenY"] == 0
    assert process_config["navigator.platform"] == "Linux x86_64"
    assert process_config["navigator.oscpu"] == "Linux x86_64"


def test_camoufox_target_os_config_is_instance_scoped(monkeypatch):
    monkeypatch.delenv("CHATGPT_CAMOUFOX_TARGET_OS", raising=False)
    monkeypatch.setenv("CHATGPT_BROWSER_ENGINE", "camoufox")
    assert configured_camoufox_target_os() == "macos"
    assert configured_deep_browser_operating_system() == "macos"

    monkeypatch.setenv("CHATGPT_CAMOUFOX_TARGET_OS", "lin")
    assert configured_camoufox_target_os() == "linux"
    assert configured_deep_browser_operating_system() == "linux"

    monkeypatch.setenv("CHATGPT_BROWSER_ENGINE", "patchright")
    monkeypatch.setenv("CHATGPT_CAMOUFOX_TARGET_OS", "invalid")
    assert configured_deep_browser_operating_system() == "linux"
    with pytest.raises(ValueError, match="expected linux or macos"):
        normalize_camoufox_target_os("windows")


def test_legacy_retina_camoufox_profile_is_normalized_without_rotating_identity():
    current = generate_browser_fingerprint(
        browser_family="firefox",
        deep_context=True,
    )
    legacy_config = dict(current.camoufox_config)
    legacy_config["window.devicePixelRatio"] = 2.0
    legacy = replace(
        current,
        device_scale_factor=2.0,
        camoufox_config=legacy_config,
    )

    normalized = coerce_browser_fingerprint(legacy)

    assert normalized.profile_id == legacy.profile_id
    assert normalized.device_id == legacy.device_id
    assert normalized.canvas_seed == legacy.canvas_seed
    assert normalized.webgl_vendor == legacy.webgl_vendor
    assert normalized.webgl_renderer == legacy.webgl_renderer
    assert normalized.device_scale_factor == CAMOUFOX_NATIVE_DEVICE_SCALE_FACTOR
    assert (
        normalized.camoufox_config["window.devicePixelRatio"]
        == CAMOUFOX_NATIVE_DEVICE_SCALE_FACTOR
    )


def test_patchright_chromium_deep_profile_is_complete_and_version_aligned():
    fingerprint = generate_browser_fingerprint(
        browser_family="chrome",
        deep_context=True,
        timezone="Asia/Jakarta",
    )
    options, init_script, cdp_override, payload = build_chromium_context_spec(
        fingerprint,
        context_options={
            "timezone_id": "Asia/Jakarta",
            "locale": "en-US",
            "geolocation": {
                "latitude": -6.2,
                "longitude": 106.816666,
                "accuracy": 20,
            },
            "_auto_gpt_webrtc_ipv4": "203.0.113.20",
        },
    )

    assert fingerprint.browser_backend == "patchright_chromium"
    assert fingerprint.browser_version == CHROMIUM_ENGINE_VERSION
    assert fingerprint.chrome_full_version == CHROMIUM_ENGINE_VERSION
    assert fingerprint.operating_system == "linux"
    assert fingerprint.navigator_platform == "Linux x86_64"
    assert "X11; Linux x86_64" in fingerprint.user_agent
    assert "Chrome/151.0.0.0" in fingerprint.user_agent
    assert fingerprint.impersonate == "chrome150"
    assert fingerprint.isolation_mode == CHROMIUM_DEEP_ISOLATION_MODE
    assert tuple(payload["context_capabilities"]) == CHROMIUM_CONTEXT_CAPABILITIES
    assert payload["chromium_config"]["generator"] == "native_chromium"
    assert payload["chromium_config"]["native_browser_surface"] is True
    assert payload["font_list"] == []
    assert options["timezone_id"] == "Asia/Jakarta"
    assert options["geolocation"]["latitude"] == -6.2
    assert "_auto_gpt_webrtc_ipv4" not in options
    assert options["no_viewport"] is True
    assert "user_agent" not in options
    assert "viewport" not in options
    assert "screen" not in options
    assert "device_scale_factor" not in options
    assert cdp_override == {}
    assert init_script == ""


def test_configured_patchright_runtime_migrates_persisted_camoufox_profile(
    monkeypatch,
):
    monkeypatch.setenv("CHATGPT_BROWSER_ENGINE", "camoufox")
    legacy = generate_browser_fingerprint(
        browser_family="firefox",
        deep_context=True,
        timezone="Asia/Jakarta",
    )

    monkeypatch.setenv("CHATGPT_BROWSER_ENGINE", "patchright")
    migrated = ensure_deep_browser_fingerprint(legacy)

    assert migrated.device_id == legacy.device_id
    assert migrated.timezone == legacy.timezone
    assert migrated.browser_family == "chrome"
    assert migrated.browser_backend == "patchright_chromium"
    assert migrated.operating_system == "linux"
    assert migrated.chromium_config["native_browser_surface"] is True


def test_deep_browser_rejects_safari_but_supports_both_real_browser_engines():
    assert generate_browser_fingerprint(
        browser_family="firefox",
        deep_context=True,
    ).browser_backend == "camoufox_firefox"
    assert generate_browser_fingerprint(
        browser_family="chrome",
        deep_context=True,
    ).browser_backend == "patchright_chromium"
    with pytest.raises(ValueError, match="Chrome or Firefox"):
        generate_browser_fingerprint(browser_family="safari", deep_context=True)


def test_v2_persistence_keeps_deep_profile_without_mutating_legacy_payload():
    deep = generate_browser_fingerprint(
        browser_family="firefox",
        deep_context=True,
    )
    payload = build_browser_fingerprint_payload(deep)
    stored = persist_account_browser_fingerprint({}, payload, source="registration")

    assert stored["chatgpt_browser_fingerprint"] == payload
    assert stored["chatgpt_browser_fingerprint_isolation_mode"] == (
        CAMOUFOX_DEEP_ISOLATION_MODE
    )

    legacy = {
        "device_id": "legacy-device",
        "accept_language": "en-US,en;q=0.9",
        "impersonate": "chrome136",
        "chrome_major": 136,
        "chrome_full_version": "136.0.7103.92",
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/136.0.7103.92 Safari/537.36"
        ),
        "sec_ch_ua": '"Chromium";v="136"',
        "platform_version": "15.0.0",
        "viewport_width": 1440,
        "viewport_height": 900,
    }
    legacy_stored = persist_account_browser_fingerprint(
        {}, legacy, source="registration"
    )
    assert legacy_stored["chatgpt_browser_fingerprint"] == legacy
    assert "schema_version" not in legacy_stored["chatgpt_browser_fingerprint"]
    assert "chatgpt_browser_fingerprint_isolation_mode" not in legacy_stored


def test_sentinel_environment_uses_same_profile_dimensions_and_cpu():
    fingerprint = generate_browser_fingerprint(browser_family="safari")
    generator = SentinelTokenGenerator(
        device_id=fingerprint.device_id,
        user_agent=fingerprint.user_agent,
        browser_fingerprint=fingerprint,
    )
    config = generator._get_config()

    assert config[0] == f"{fingerprint.screen_width}x{fingerprint.screen_height}"
    assert config[4] == fingerprint.user_agent
    assert config[8] == fingerprint.locale
    assert config[17] == fingerprint.hardware_concurrency
    assert config[2] is None


def test_protocol_transport_passes_exact_profile_into_registration_engine():
    fingerprint = generate_browser_fingerprint(browser_family="firefox")
    captured = {}

    class FakeEngine:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.password = ""
            self.email = ""

        def run(self):
            return SimpleNamespace(
                to_dict=lambda: {},
                email="person@example.com",
                password="secret",
                account_id="account-1",
                workspace_id="workspace-1",
                access_token="access-token",
                refresh_token="",
                id_token="access-token",
                session_token="session-token",
                error_message="",
                success=True,
                source="register",
                metadata={
                    "cookies": {
                        "__Secure-next-auth.session-token": "session-token"
                    }
                },
            )

    with mock.patch(
        "services.chatgpt_core.any_auto.register.RegistrationEngine",
        FakeEngine,
    ):
        result = transport.run_any_auto_protocol_registration(
            email="person@example.com",
            password="secret",
            proxy_url=None,
            wait_code=lambda **_kwargs: "123456",
            browser_fingerprint=fingerprint,
        )

    assert result.ok
    assert captured["browser_fingerprint"] is fingerprint


def test_openai_http_client_uses_family_target_and_omits_chromium_hints():
    fingerprint = generate_browser_fingerprint(browser_family="safari")
    client = OpenAIHTTPClient(browser_fingerprint=fingerprint)
    headers = {str(key).lower(): value for key, value in client.session.headers.items()}

    assert client.config.impersonate == "safari2601"
    assert headers["user-agent"] == fingerprint.user_agent
    assert "sec-ch-ua" not in headers
