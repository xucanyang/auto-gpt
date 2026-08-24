from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from services.chatgpt_core import payment_eligibility as probe
from services.chatgpt_core.browser_checkout import (
    _FETCH_SCRIPT,
    _SENTINEL_LOADER_URL,
    _origin_client_metadata,
    BrowserCheckoutClient,
)
from services.chatgpt_core.any_auto.transport import _normalize_result


def _account():
    return SimpleNamespace(
        id=1,
        user_id="acct-1",
        email="browser@example.com",
        token="",
        cookies="session=legacy",
        get_extra=lambda: {
            "access_token": "at-test",
            "account_id": "acct-1",
            "chatgpt_browser_fingerprint": {
                "device_id": "device-1",
                "user_agent": "Mozilla/5.0 Firefox/147.0",
                "browser_family": "firefox",
            },
        },
    )


class _FakePage:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def evaluate(self, script, payload):
        self.calls.append((script, payload))
        return self.result


def _fake_context(client, page):
    client._session = SimpleNamespace(
        page=page,
        context=SimpleNamespace(add_cookies=lambda cookies: None),
    )
    client._page = page
    client._proxy = ""


def test_browser_checkout_fetch_maps_success_and_keeps_headers_outside_cookie_header(monkeypatch):
    page = _FakePage({"status": 200, "payload": {"checkout_session_id": "oaics_demo"}, "text": "{}"})
    client = BrowserCheckoutClient(_account(), {"device_id": "device-1"})
    _fake_context(client, page)

    result = client.post(
        "/backend-api/payments/checkout",
        {"plan_name": "chatgptplusplan"},
        "",
        "checkout 创建",
    )

    assert result["checkout_session_id"] == "oaics_demo"
    payload = page.calls[0][1]
    assert payload["headers"]["Authorization"] == "Bearer at-test"
    assert payload["headers"]["oai-device-id"] == "device-1"
    assert payload["headers"]["chatgpt-account-id"] == "acct-1"
    assert "Cookie" not in payload["headers"]
    assert payload["path"] == "/backend-api/payments/checkout"
    assert payload["requireSentinel"] is True
    assert payload["clientMetadata"]["buildNumber"]
    assert payload["clientMetadata"]["version"]


def test_browser_checkout_fetch_requires_official_sentinel_and_session_warmup():
    assert "SentinelSDK.token('chatgpt_checkout')" in _FETCH_SCRIPT
    assert "OpenAI-Sentinel-Token" in _FETCH_SCRIPT
    assert "OAI-Telemetry" in _FETCH_SCRIPT
    assert "/backend-api/accounts/optimized/check" in _FETCH_SCRIPT
    assert "/backend-api/accounts/check/v4-2023-04-27" in _FETCH_SCRIPT
    assert "/backend-api/sentinel/ping" in _FETCH_SCRIPT


def test_browser_checkout_loads_sentinel_from_current_chatgpt_origin():
    assert _SENTINEL_LOADER_URL == "https://chatgpt.com/backend-api/sentinel/sdk.js"

    page = SimpleNamespace(
        url="https://chatgpt.com/",
        goto=Mock(return_value=SimpleNamespace(status=200)),
        evaluate=Mock(
            side_effect=[
                {"build": "prod-live", "sequence": "9876543"},
                False,
            ]
        ),
        add_script_tag=Mock(),
        wait_for_function=Mock(),
    )
    client = BrowserCheckoutClient(_account(), {"device_id": "device-1"})
    client._page = page

    client._prepare_page()

    page.add_script_tag.assert_called_once_with(url=_SENTINEL_LOADER_URL)
    page.wait_for_function.assert_called_once_with(
        "() => Boolean(window.SentinelSDK && typeof window.SentinelSDK.token === 'function')",
        timeout=15_000,
    )
    assert client._page_source == "origin"


def test_browser_checkout_update_reuses_context_without_new_sentinel_token():
    page = _FakePage({"status": 200, "payload": {"checkout_state": {}}, "text": "{}"})
    client = BrowserCheckoutClient(_account(), {"device_id": "device-1"})
    _fake_context(client, page)

    client.post(
        "/backend-api/payments/checkout/update",
        {"checkout_session_id": "oaics_demo"},
        "",
        "promotion 更新",
    )

    assert page.calls[0][1]["requireSentinel"] is False


def test_browser_checkout_origin_metadata_wins_over_configured_fallback():
    metadata, source = _origin_client_metadata(
        {"buildNumber": "9758774", "version": "prod-configured"},
        {"sequence": "9876543", "build": "prod-live"},
    )

    assert metadata == {"buildNumber": "9876543", "version": "prod-live"}
    assert source == "origin"


def test_browser_checkout_cookie_payload_binds_oai_did_to_frozen_profile():
    account = _account()
    account.cookies = "oai-did=stale-device; session=legacy"
    client = BrowserCheckoutClient(account, {"device_id": "device-1"})

    cookies = client._context_cookie_payload()
    device_cookies = [item for item in cookies if item["name"] == "oai-did"]

    assert device_cookies == [
        {
            "name": "oai-did",
            "value": "device-1",
            "domain": "chatgpt.com",
            "path": "/",
            "secure": True,
            "sameSite": "Lax",
        }
    ]


def test_browser_checkout_http_error_uses_json_detail():
    page = _FakePage(
        {
            "status": 400,
            "payload": {
                "error": {
                    "message": "Our systems have detected unusual activity."
                }
            },
            "text": "ignored",
        }
    )
    client = BrowserCheckoutClient(_account(), {"device_id": "device-1"})
    _fake_context(client, page)

    with pytest.raises(
        probe.PaymentEligibilityHttpError,
        match="checkout 创建 HTTP 400: Our systems have detected unusual activity",
    ):
        client.post(
            "/backend-api/payments/checkout",
            {},
            "",
            "checkout 创建",
        )


def test_browser_checkout_rejects_unapproved_paths_without_page_call():
    page = _FakePage({"status": 200, "payload": {}, "text": "{}"})
    client = BrowserCheckoutClient(_account(), {"device_id": "device-1"})
    _fake_context(client, page)

    with pytest.raises(probe.PaymentEligibilityProtocolError, match="禁止访问"):
        client.post("/backend-api/payments/checkout/confirm", {}, "", "confirm")
    assert page.calls == []


def test_browser_stripe_amount_reader_uses_frozen_proxy_http_session(monkeypatch):
    class FakeStripeResponse:
        status_code = 200
        text = "{}"

        @staticmethod
        def json():
            return {
                "currency": "jpy",
                "total_summary": {"due": 2200},
                "payment_method_types": ["card", "konbini"],
            }

    class FakeStripeSession:
        def __init__(self):
            self.headers = {}
            self.proxies = {}
            self.calls = []
            self.closed = False

        def post(self, url, *, data, timeout):
            self.calls.append((url, data, timeout))
            return FakeStripeResponse()

        def close(self):
            self.closed = True

    fake_session = FakeStripeSession()
    session_options = {}

    def session_factory(**kwargs):
        session_options.update(kwargs)
        return fake_session

    from curl_cffi import requests as cffi_requests

    monkeypatch.setattr(cffi_requests, "Session", session_factory)
    page = _FakePage({})
    client = BrowserCheckoutClient(
        _account(),
        {
            "device_id": "device-1",
            "locale": "ja-JP",
            "stripe_locale": "ja",
            "timezone": "Asia/Tokyo",
            "accept_language": "ja-JP",
            "impersonate": "firefox147",
            "ua": "Mozilla/5.0 Firefox/147.0",
        },
    )
    _fake_context(client, page)
    client._proxy = "http://user:pass@proxy.test:8080"

    result = client.stripe_payment_page_init(
        "cs_demo",
        {
            "currency": "JPY",
            "billing_country": "JP",
            "locale": "ja-JP",
            "timezone": "Asia/Tokyo",
            "stripe_locale": "ja",
            "publishable_key": "pk_live_checkout_response",
        },
    )

    assert result["amount"] == 2200
    assert result["currency"] == "jpy"
    assert result["payment_method_types"] == ["card", "konbini"]
    assert result["stripe_publishable_key_prefix"].startswith(
        "pk_live_checkout_response"
    )
    assert session_options == {"impersonate": "firefox147"}
    assert fake_session.proxies == {
        "http": "http://user:pass@proxy.test:8080",
        "https": "http://user:pass@proxy.test:8080",
    }
    assert fake_session.headers["Origin"] == "https://js.stripe.com"
    assert fake_session.headers["Referer"] == "https://js.stripe.com/"
    assert fake_session.closed is True
    url, body, timeout = fake_session.calls[0]
    assert url.endswith("/cs_demo/init")
    assert timeout == 30
    assert body["key"] == "pk_live_checkout_response"
    assert body["browser_locale"] == "ja-JP"
    assert body["browser_timezone"] == "Asia/Tokyo"
    assert body["elements_session_client[elements_init_source]"] == (
        "custom_checkout"
    )
    assert body["elements_session_client[client_betas][0]"] == (
        "custom_checkout_server_updates_1"
    )
    assert body["elements_session_client[client_betas][1]"] == (
        "custom_checkout_manual_approval_1"
    )


def test_stripe_amount_forwards_current_checkout_key_and_browser_identity():
    captured = {}

    class Reader:
        def stripe_payment_page_init(self, session_id, checkout_profile):
            captured["session_id"] = session_id
            captured["profile"] = dict(checkout_profile)
            return {"amount": 0, "currency": "idr"}

    amount, currency, _result = probe._stripe_amount(
        SimpleNamespace(),
        {
            "session_id": "cs_live_demo",
            "publishable_key": "pk_live_from_checkout",
        },
        "http://proxy.test:8080",
        {
            "locale": "id-ID",
            "stripe_locale": "id",
            "timezone": "Asia/Jakarta",
        },
        {"billing_country": "ID", "currency": "IDR"},
        Reader(),
    )

    assert amount == 0
    assert currency == "IDR"
    assert captured == {
        "session_id": "cs_live_demo",
        "profile": {
            "billing_country": "ID",
            "currency": "IDR",
            "locale": "id-ID",
            "stripe_locale": "id",
            "timezone": "Asia/Jakarta",
            "publishable_key": "pk_live_from_checkout",
        },
    }


def test_browser_transport_selection_does_not_fallback_to_protocol(monkeypatch):
    calls = {"browser": 0, "protocol": 0}

    class FailingBrowser:
        def __init__(self, *args, **kwargs):
            calls["browser"] += 1

        def post(self, *args, **kwargs):
            raise probe.PaymentEligibilityHttpError(
                "checkout 创建", 400, "Our systems have detected unusual activity"
            )

        def close(self):
            pass

    monkeypatch.setattr(
        "services.chatgpt_core.browser_checkout.BrowserCheckoutClient",
        FailingBrowser,
    )

    def protocol_post(*args, **kwargs):
        calls["protocol"] += 1
        raise AssertionError("protocol fallback must not run")

    monkeypatch.setattr(probe._CheckoutClient, "post", protocol_post)
    monkeypatch.setattr(
        probe,
        "_resolve_proxy_chain",
        lambda *_args, **_kwargs: {"checkout": "", "promotion": "", "taxes": ""},
    )
    result = probe.probe_zero_amount_eligibility(
        _account(),
        settings={"checkout_transport": "browser"},
        max_attempts=1,
    )

    assert result["state"] == "probe_failed"
    assert result["failure_category"] == "checkout_create_failed"
    assert calls == {"browser": 1, "protocol": 0}


def test_browser_registration_normalization_keeps_structured_cookie_material():
    result = _normalize_result(
        email="browser@example.com",
        password="pw",
        payload={
            "success": True,
            "access_token": "at",
            "session_token": "st",
            "cookies": [
                {
                    "name": "session",
                    "value": "cookie",
                    "domain": ".chatgpt.com",
                    "path": "/",
                    "secure": True,
                }
            ],
        },
        executor="headless",
        transport="any_auto_browser",
    )

    assert result.ok is True
    assert result.metadata["chatgpt_browser_cookies"][0]["domain"] == ".chatgpt.com"
