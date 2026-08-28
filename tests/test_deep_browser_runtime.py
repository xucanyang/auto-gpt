from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from services.chatgpt_core.browser_identity import (
    BrowserGeoIdentity,
    generate_browser_fingerprint,
)
from services.chatgpt_core.browser_checkout import BrowserCheckoutClient
from services.chatgpt_core.sentinel_batch import FlowSpec, PlaywrightSentinelProvider
from services.chatgpt_core.sentinel_browser import _evaluate_complete_sentinel_token
from services.chatgpt_core.shared_browser import shared_browser_registration_session
from services.turnstile_solver.api_solver import TurnstileAPIServer


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - stdlib callback name
        body = b"""<!doctype html>
<html>
  <head>
    <script>
      (async () => {
        const canvas = document.createElement('canvas');
        const gl = canvas.getContext('webgl2') || canvas.getContext('webgl');
        const position = await new Promise((resolve, reject) => {
          navigator.geolocation.getCurrentPosition(resolve, reject, { timeout: 5000 });
        });
        const identity = {
          ua: navigator.userAgent,
          platform: navigator.platform,
          oscpu: navigator.oscpu || '',
          webdriver: navigator.webdriver,
          language: navigator.language,
          languages: navigator.languages,
          timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
          geolocation: [
            position.coords.latitude,
            position.coords.longitude,
            position.coords.accuracy,
          ],
          hardwareConcurrency: navigator.hardwareConcurrency,
          deviceMemory: navigator.deviceMemory || 0,
          screen: [screen.width, screen.height, screen.availWidth, screen.availHeight],
          webgl: gl ? [gl.getParameter(37445), gl.getParameter(37446)] : [],
          userAgentData: navigator.userAgentData ? {
            brands: navigator.userAgentData.brands,
            platform: navigator.userAgentData.platform,
            high: await navigator.userAgentData.getHighEntropyValues([
              'architecture', 'bitness', 'platformVersion',
              'uaFullVersion', 'fullVersionList'
            ]),
          } : null,
        };
        document.documentElement.dataset.browserIdentity = JSON.stringify(identity);
      })().catch((error) => {
        document.documentElement.dataset.browserIdentityError = String(error);
      });
    </script>
  </head>
  <body>browser-runtime</body>
</html>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        return


@contextmanager
def _local_page_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _observed_identity(page):
    page.wait_for_function(
        """() => document.documentElement.dataset.browserIdentity
          || document.documentElement.dataset.browserIdentityError""",
        timeout=10_000,
    )
    root = page.locator("html")
    error = root.get_attribute("data-browser-identity-error")
    assert not error, error
    return json.loads(root.get_attribute("data-browser-identity") or "{}")


@pytest.mark.browser
@pytest.mark.parametrize(
    ("family", "ua_marker", "backend"),
    (
        ("firefox", "Firefox/147.0", "camoufox_firefox"),
        ("chrome", "Chrome/151.0.0.0", "patchright_chromium"),
    ),
)
def test_deep_browser_runtime_exposes_configured_native_identity(
    family,
    ua_marker,
    backend,
    monkeypatch,
):
    monkeypatch.setenv(
        "CHATGPT_BROWSER_ENGINE",
        "camoufox" if family == "firefox" else "patchright",
    )
    monkeypatch.setenv("AUTO_GPT_XVFB", "1")
    secondary_observed = None
    isolated_storage = None
    geo_identity = BrowserGeoIdentity(
        exit_ip="103.189.207.248",
        country_code="ID",
        timezone="Asia/Pontianak",
        locale="id-ID",
        languages=("id-ID", "id", "en-US", "en"),
        accept_language="id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
        geolocation={
            "latitude": -2.3406,
            "longitude": 106.1922,
            "accuracy": 25.0,
        },
        webrtc_ipv4="103.189.207.248",
        source="maxmind_geoip",
    )
    fingerprint = generate_browser_fingerprint(
        browser_family=family,
        deep_context=True,
        geo_identity=geo_identity,
    )
    with _local_page_url() as page_url:
        with shared_browser_registration_session(
            headless=True,
            browser_fingerprint=fingerprint,
        ) as session:
            session.page.goto(
                page_url,
                wait_until="domcontentloaded",
                timeout=20_000,
            )
            observed = _observed_identity(session.page)
            if family == "chrome":
                assert session.page.viewport_size is None
                session.page.evaluate(
                    """() => {
                      localStorage.setItem('runtime-isolation', 'primary');
                      document.cookie = 'runtime-isolation=primary; SameSite=Lax';
                    }"""
                )
                second_page = session.context.new_page()
                try:
                    second_page.goto(
                        page_url,
                        wait_until="domcontentloaded",
                        timeout=20_000,
                    )
                    secondary_observed = _observed_identity(second_page)
                finally:
                    second_page.close()
                isolated_context = session.browser.new_context(no_viewport=True)
                try:
                    isolated_page = isolated_context.new_page()
                    isolated_page.goto(
                        page_url,
                        wait_until="domcontentloaded",
                        timeout=20_000,
                    )
                    isolated_storage = isolated_page.evaluate(
                        """() => ({
                          local: localStorage.getItem('runtime-isolation'),
                          cookie: document.cookie,
                        })"""
                    )
                finally:
                    isolated_context.close()

    assert fingerprint.browser_backend == backend
    assert ua_marker in observed["ua"]
    assert "HeadlessChrome" not in observed["ua"]
    assert observed["webdriver"] is False
    assert observed["language"] == geo_identity.locale
    assert observed["languages"] == list(fingerprint.languages)
    assert observed["timezone"] == geo_identity.timezone
    assert observed["geolocation"] == pytest.approx(
        [
            geo_identity.geolocation["latitude"],
            geo_identity.geolocation["longitude"],
            geo_identity.geolocation["accuracy"],
        ]
    )
    assert observed["hardwareConcurrency"] == fingerprint.hardware_concurrency
    assert all(int(value) > 0 for value in observed["screen"])
    if family == "firefox":
        assert fingerprint.operating_system == "macos"
        assert observed["platform"] == "MacIntel"
        assert "Mac OS X" in observed["oscpu"]
        assert observed["userAgentData"] is None
    else:
        assert fingerprint.operating_system == "linux"
        assert observed["platform"] == "Linux x86_64"
        assert observed["oscpu"] == ""
        assert observed["deviceMemory"] == fingerprint.device_memory
        assert all(str(value) for value in observed["webgl"])
        assert observed["userAgentData"]["platform"] == "Linux"
        high = observed["userAgentData"]["high"]
        assert high["uaFullVersion"] == fingerprint.chrome_full_version
        assert high["architecture"] == "x86"
        assert secondary_observed is not None
        assert secondary_observed["ua"] == observed["ua"]
        assert secondary_observed["platform"] == observed["platform"]
        assert secondary_observed["deviceMemory"] == observed["deviceMemory"]
        assert secondary_observed["webgl"] == observed["webgl"]
        assert secondary_observed["userAgentData"] == observed["userAgentData"]
        assert isolated_storage == {"local": None, "cookie": ""}


@pytest.mark.browser
def test_patchright_sdk_helpers_read_page_globals_from_main_world(monkeypatch):
    monkeypatch.setenv("CHATGPT_BROWSER_ENGINE", "patchright")
    monkeypatch.setenv("AUTO_GPT_XVFB", "1")
    fingerprint = generate_browser_fingerprint(
        browser_family="chrome",
        deep_context=True,
    )

    with shared_browser_registration_session(
        headless=True,
        browser_fingerprint=fingerprint,
    ) as session:
        session.page.set_content(
            """
            <script>
              window.SentinelSDK = {
                init: async () => true,
                token: async (flow) => flow === 'chatgpt_checkout'
                  ? 'main-world-token'
                  : JSON.stringify({p: 'pow', t: 'telemetry', c: 'challenge'})
              };
              window.turnstile = {render: () => true};
            </script>
            """
        )
        assert session.page.evaluate("""() => ({
            sentinel: typeof window.SentinelSDK,
            turnstile: typeof window.turnstile
        })""") == {"sentinel": "undefined", "turnstile": "undefined"}

        client = object.__new__(BrowserCheckoutClient)
        client._session = session
        client._page = session.page
        assert client._page_evaluate(
            "async () => await window.SentinelSDK.token('chatgpt_checkout')"
        ) == "main-world-token"

        token = _evaluate_complete_sentinel_token(
            session.page,
            flow="oauth_create_account",
            sdk_wait_timeout_ms=1000,
            token_eval_timeout_ms=1000,
            require_complete_signals=True,
            logger=lambda _message: None,
        )
        assert json.loads(token or "{}") == {
            "p": "pow",
            "t": "telemetry",
            "c": "challenge",
        }

        provider = object.__new__(PlaywrightSentinelProvider)
        provider._page = session.page
        assert json.loads(
            provider.get_flow_token(
                FlowSpec(
                    internal_name="oauth_create_account",
                    alias="oauth-create-account",
                    page_url="https://auth.openai.com/create-account",
                )
            )
        ) == {"p": "pow", "t": "telemetry", "c": "challenge"}

        solver = object.__new__(TurnstileAPIServer)
        solver.browser_type = "chromium"
        assert solver._evaluate_page_world(
            session.page,
            "() => typeof window.turnstile",
        ) == "object"
