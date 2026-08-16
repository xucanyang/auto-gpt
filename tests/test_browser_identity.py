from types import SimpleNamespace
from unittest import mock

from curl_cffi import requests as cffi_requests

from services.chatgpt_core.account_fingerprint import (
    build_browser_fingerprint_payload,
    persist_account_browser_fingerprint,
)
from services.chatgpt_core.browser_identity import (
    CAMOUFOX_CONTEXT_SETTERS,
    CAMOUFOX_DEEP_ISOLATION_MODE,
    LATEST_CURL_IMPERSONATE,
    build_camoufox_context_spec,
    build_camoufox_process_config,
    generate_browser_fingerprint,
    select_protocol_browser_family,
)
from services.chatgpt_core.sentinel_token import SentinelTokenGenerator
from services.chatgpt_core.utils import apply_browser_fingerprint
from services.chatgpt_core.any_auto import transport
from services.chatgpt_core.any_auto.http_client import OpenAIHTTPClient
from platforms.chatgpt import utils as platform_utils


def _lower_headers(session) -> dict[str, str]:
    return {str(key).lower(): str(value) for key, value in session.headers.items()}


def test_latest_concrete_transport_profiles_are_family_consistent():
    assert LATEST_CURL_IMPERSONATE == {
        "chrome": "chrome146",
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
    assert fingerprint.operating_system == "linux"
    assert "Linux" in fingerprint.navigator_platform
    assert "Linux" in fingerprint.navigator_oscpu
    assert tuple(payload["context_capabilities"]) == CAMOUFOX_CONTEXT_SETTERS
    assert len(payload["font_list"]) >= 30
    assert {"Arimo", "Cousine", "Tinos", "Twemoji Mozilla"}.issubset(
        payload["font_list"]
    )
    assert len(payload["speech_voices"]) > 10
    assert len(CAMOUFOX_CONTEXT_SETTERS) == 13
    assert options["timezone_id"] == "Asia/Tokyo"
    assert options["locale"] == "ja-JP"
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
    assert process_config["timezone"] == "Asia/Tokyo"
    assert process_config["navigator.language"] == "ja-JP"
    assert process_config["webrtc:ipv4"] == "203.0.113.10"
    assert process_config["geolocation:latitude"] == 35.6762
    assert process_config["fonts"] == list(fingerprint.font_list)
    assert process_config["voices"] == list(fingerprint.speech_voices)
    assert process_config["mediaDevices:enabled"] is True
    assert process_config["mediaDevices:micros"] == 1
    assert process_config["webGl:parameters"]


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
