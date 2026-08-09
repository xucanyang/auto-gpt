from __future__ import annotations

from types import SimpleNamespace

import pytest

from services.chatgpt_core import payment_eligibility as probe


def _account():
    extra = {"access_token": "at-test", "chatgpt_browser_fingerprint": {"user_agent": "Mozilla/5.0 Chrome/146.0.0.0"}}
    return SimpleNamespace(
        id=1,
        user_id="acct-1",
        email="probe@example.com",
        token="",
        get_extra=lambda: dict(extra),
    )


def _state(amount: int) -> dict:
    return {
        "checkout_state": {
            "id": "oaics_demo",
            "currency": "PHP",
            "total": {"total": {"minorUnitsAmount": amount}},
        }
    }


def _checkout_payload(session_id: str = "oaics_demo", *, methods=None, amount: int = 0) -> dict:
    payload = {
        "checkout_session_id": session_id,
        "checkout_provider": "open_ai" if session_id.startswith("oaics_") else "stripe",
        "processor_entity": "openai_llc",
        "payment_method_types": ["card"],
        "custom_payment_methods": list(methods or []),
    }
    payload.update(_state(amount) if session_id.startswith("oaics_") else {})
    return payload


def _patch_common(monkeypatch, responses):
    monkeypatch.setattr(probe, "_resolve_proxy_chain", lambda _settings: {"checkout": "us", "promotion": "vn", "taxes": "us"})
    monkeypatch.setattr(probe, "_browser_profile", lambda _account: {
        "device_id": "device-1",
        "ua": "Mozilla/5.0 Chrome/146.0.0.0",
        "accept_language": "en-US,en;q=0.9",
        "locale": "en-US",
        "impersonate": "chrome146",
        "timezone": "America/New_York",
    })
    queue = list(responses)

    def fake_post(self, path, body, proxy, stage, **kwargs):
        assert path not in {
            "/backend-api/payments/checkout/confirm",
            "/backend-api/payments/checkout/approve",
            "/backend-api/payments/checkout/custom_payment_method/start",
        }
        if path in {"/backend-api/payments/checkout/update", "/backend-api/payments/checkout/taxes"}:
            assert kwargs.get("referer") == f"https://chatgpt.com/checkout/openai_llc/{body['checkout_session_id']}"
        if not queue:
            raise AssertionError(f"unexpected {path}")
        response = queue.pop(0)
        return response() if callable(response) else response

    monkeypatch.setattr(probe._CheckoutClient, "post", fake_post)


def test_oaics_zero_amount_does_not_require_gcash(monkeypatch):
    _patch_common(monkeypatch, [_checkout_payload(), _state(0), _state(0)])
    result = probe.probe_zero_amount_eligibility(_account(), settings={})
    assert result["state"] == "eligible"
    assert result["evidence"]["amount_minor"] == 0
    assert result["evidence"]["custom_payment_method_count"] == 0


def test_oaics_nonzero_amount_is_independent_from_gcash_availability(monkeypatch):
    methods = [{"id": "cpmt_gcash1", "options": {"type": "static"}}]
    _patch_common(monkeypatch, [_checkout_payload(methods=methods, amount=110000), _state(110000), _state(110000)])
    zero = probe.probe_zero_amount_eligibility(_account(), settings={})
    assert zero["state"] == "ineligible"

    _patch_common(monkeypatch, [_checkout_payload(methods=methods, amount=110000), {**_state(110000), "custom_payment_methods": methods}, {**_state(110000), "custom_payment_methods": methods}])
    gcash = probe.probe_gcash_payment_method(_account(), settings={})
    assert gcash["state"] == "available"


def test_stripe_zero_amount_path_uses_structured_amount_reader(monkeypatch):
    _patch_common(monkeypatch, [_checkout_payload("cs_demo"), {}, {}])
    monkeypatch.setattr(probe, "_stripe_amount", lambda *args, **kwargs: (0, "PHP", {"amount_source": "stripe.init"}))
    result = probe.probe_zero_amount_eligibility(_account(), settings={})
    assert result["state"] == "eligible"
    assert result["evidence"]["amount_source"] == "stripe.init"


def test_stripe_checkout_is_gcash_unavailable_without_provider_actions(monkeypatch):
    _patch_common(monkeypatch, [_checkout_payload("cs_demo"), {}, {}])
    result = probe.probe_gcash_payment_method(_account(), settings={})
    assert result["state"] == "unavailable"
    assert result["reason_code"] == "stripe_checkout"


def test_cpmt_disappearing_after_refresh_is_unavailable(monkeypatch):
    methods = [{"id": "cpmt_gcash1"}]
    _patch_common(monkeypatch, [
        _checkout_payload(methods=methods),
        {**_state(0), "custom_payment_methods": []},
        {**_state(0), "custom_payment_methods": []},
    ])
    result = probe.probe_gcash_payment_method(_account(), settings={})
    assert result["state"] == "unavailable"
    assert result["evidence"]["stable"] is False


def test_cpmt_requires_a_real_custom_method_id(monkeypatch):
    fake_methods = [{"type": "cpmt_not_an_id"}]
    _patch_common(monkeypatch, [
        _checkout_payload(methods=fake_methods),
        {**_state(0), "custom_payment_methods": fake_methods},
        {**_state(0), "custom_payment_methods": fake_methods},
    ])
    result = probe.probe_gcash_payment_method(_account(), settings={})
    assert result["state"] == "unavailable"
    assert result["evidence"]["final_custom_payment_method_count"] == 0


def test_technical_failure_is_probe_failed_and_retries(monkeypatch):
    calls = {"count": 0}
    monkeypatch.setattr(probe, "_resolve_proxy_chain", lambda _settings: {"checkout": "us", "promotion": "vn", "taxes": "us"})
    monkeypatch.setattr(probe, "_browser_profile", lambda _account: {"device_id": "d", "ua": "Mozilla/5.0 Chrome/146.0.0.0", "accept_language": "en-US", "locale": "en-US", "impersonate": "chrome146", "timezone": "America/New_York"})

    def failing_post(self, path, body, proxy, stage):
        calls["count"] += 1
        raise probe.PaymentEligibilityProbeError("upstream unavailable")

    monkeypatch.setattr(probe._CheckoutClient, "post", failing_post)
    result = probe.probe_zero_amount_eligibility(_account(), settings={}, max_attempts=3)
    assert result["state"] == "probe_failed"
    assert result["attempt_count"] == 3
    assert calls["count"] == 3


def test_task_interruption_is_not_swallowed(monkeypatch):
    from core.task_runtime import TaskInterruption

    monkeypatch.setattr(probe, "_resolve_proxy_chain", lambda _settings: {"checkout": "us", "promotion": "vn", "taxes": "us"})

    def stop_post(self, path, body, proxy, stage):
        raise TaskInterruption("stop")

    monkeypatch.setattr(probe._CheckoutClient, "post", stop_post)
    with pytest.raises(TaskInterruption):
        probe.probe_zero_amount_eligibility(_account(), settings={})


def test_dynamic_mode_rejects_a_fixed_proxy_disguised_as_a_template():
    with pytest.raises(probe.PaymentEligibilityProbeError, match="region-XX"):
        probe._resolve_proxy_chain(
            {
                "proxy_mode": "dynamic",
                "proxy": "http://user:pass@127.0.0.1:8080",
            }
        )
