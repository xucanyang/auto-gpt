from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from services.chatgpt_core.browser_identity import generate_browser_fingerprint
from services.chatgpt_core.shared_browser import shared_browser_registration_session


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - stdlib callback name
        body = b"""<!doctype html>
<html>
  <head>
    <script>
      (async () => {
        const canvas = document.createElement('canvas');
        const gl = canvas.getContext('webgl2') || canvas.getContext('webgl');
        const identity = {
          ua: navigator.userAgent,
          platform: navigator.platform,
          oscpu: navigator.oscpu || '',
          webdriver: navigator.webdriver,
          language: navigator.language,
          timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
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
        ("chrome", "Chrome/148.0.0.0", "patchright_chromium"),
    ),
)
def test_deep_browser_runtime_exposes_coherent_macos_identity(
    family,
    ua_marker,
    backend,
):
    secondary_observed = None
    fingerprint = generate_browser_fingerprint(
        browser_family=family,
        deep_context=True,
        timezone="Asia/Jakarta",
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

    assert fingerprint.browser_backend == backend
    assert fingerprint.operating_system == "macos"
    assert ua_marker in observed["ua"]
    assert observed["platform"] == "MacIntel"
    assert observed["webdriver"] is False
    assert observed["timezone"] == "Asia/Jakarta"
    assert observed["hardwareConcurrency"] == fingerprint.hardware_concurrency
    assert all(int(value) > 0 for value in observed["screen"])
    if family == "firefox":
        assert "Mac OS X" in observed["oscpu"]
        assert observed["userAgentData"] is None
    else:
        assert observed["oscpu"] == ""
        assert observed["deviceMemory"] == fingerprint.device_memory
        assert observed["webgl"] == [
            fingerprint.webgl_vendor,
            fingerprint.webgl_renderer,
        ]
        assert observed["userAgentData"]["platform"] == "macOS"
        high = observed["userAgentData"]["high"]
        assert high["platformVersion"] == fingerprint.platform_version
        assert high["uaFullVersion"] == fingerprint.chrome_full_version
        assert high["architecture"] in {"arm", "x86"}
        assert secondary_observed is not None
        assert secondary_observed["ua"] == observed["ua"]
        assert secondary_observed["platform"] == observed["platform"]
        assert secondary_observed["deviceMemory"] == observed["deviceMemory"]
        assert secondary_observed["webgl"] == observed["webgl"]
        assert secondary_observed["userAgentData"] == observed["userAgentData"]
