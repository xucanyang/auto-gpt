from __future__ import annotations

from types import SimpleNamespace

import pytest

from services.chatgpt_core import payment_eligibility as probe
from services.chatgpt_core.browser_checkout import BrowserCheckoutClient
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


def test_browser_stripe_amount_reader_uses_page_fetch_and_existing_session():
    page = _FakePage(
        {
            "status": 200,
            "payload": {
                "currency": "jpy",
                "total_summary": {"due": 2200},
                "payment_method_types": ["card", "konbini"],
            },
            "text": "{}",
        }
    )
    client = BrowserCheckoutClient(
        _account(),
        {"device_id": "device-1", "locale": "ja-JP", "accept_language": "ja-JP"},
    )
    _fake_context(client, page)

    result = client.stripe_payment_page_init(
        "cs_demo",
        {"currency": "JPY", "billing_country": "JP", "locale": "ja-JP"},
    )

    assert result["amount"] == 2200
    assert result["currency"] == "jpy"
    assert result["payment_method_types"] == ["card", "konbini"]
    assert page.calls[0][1]["url"].endswith("/cs_demo/init")


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
