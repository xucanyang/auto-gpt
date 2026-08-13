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


def _state(amount: int, currency: str = "VND") -> dict:
    return {
        "checkout_state": {
            "id": "oaics_demo",
            "currency": currency,
            "total": {"total": {"minorUnitsAmount": amount}},
        }
    }


def _checkout_payload(
    session_id: str = "oaics_demo",
    *,
    methods=None,
    amount: int = 0,
    currency: str = "VND",
) -> dict:
    payload = {
        "checkout_session_id": session_id,
        "checkout_provider": "open_ai" if session_id.startswith("oaics_") else "stripe",
        "processor_entity": "openai_llc",
        "payment_method_types": ["card"],
        "custom_payment_methods": list(methods or []),
    }
    payload.update(_state(amount, currency) if session_id.startswith("oaics_") else {})
    return payload


def _resolved_chain(kind, settings):
    return {
        stage: region.lower()
        for stage, region in probe.payment_eligibility_stage_regions(kind, settings).items()
    }


def _patch_common(monkeypatch, responses):
    monkeypatch.setattr(probe, "_resolve_proxy_chain", _resolved_chain)
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
    assert result["reason_code"] == "zero_checkout_amount"
    assert result["evidence"]["amount_minor"] == 0
    assert result["evidence"]["amount_display"] == "0.00 VND"
    assert result["evidence"]["profile"]["billing_country"] == "VN"
    assert result["evidence"]["profile"]["currency"] == "VND"
    assert result["evidence"]["custom_payment_method_count"] == 0


def test_oaics_nonzero_amount_is_independent_from_gcash_availability(monkeypatch):
    methods = [{"id": "cpmt_gcash1", "options": {"type": "static"}}]
    _patch_common(monkeypatch, [_checkout_payload(methods=methods, amount=110000), _state(110000), _state(110000)])
    zero = probe.probe_zero_amount_eligibility(_account(), settings={})
    assert zero["state"] == "ineligible"
    assert zero["reason_code"] == "nonzero_checkout_amount"
    assert zero["evidence"]["amount_display"] == "1,100.00 VND"

    _patch_common(monkeypatch, [
        _checkout_payload(methods=methods, amount=110000, currency="PHP"),
        {**_state(110000, "PHP"), "custom_payment_methods": methods},
        {**_state(110000, "PHP"), "custom_payment_methods": methods},
    ])
    gcash = probe.probe_gcash_payment_method(_account(), settings={})
    assert gcash["state"] == "available"


def test_stripe_zero_amount_path_uses_structured_amount_reader(monkeypatch):
    _patch_common(monkeypatch, [_checkout_payload("cs_demo", currency="PHP"), {}, {}])
    monkeypatch.setattr(probe, "_stripe_amount", lambda *args, **kwargs: (0, "VND", {"amount_source": "stripe.init"}))
    result = probe.probe_zero_amount_eligibility(_account(), settings={})
    assert result["state"] == "eligible"
    assert result["evidence"]["amount_source"] == "stripe.init"


def test_stripe_checkout_is_gcash_unavailable_without_provider_actions(monkeypatch):
    _patch_common(monkeypatch, [_checkout_payload("cs_demo", currency="PHP"), {}, {}])
    result = probe.probe_gcash_payment_method(_account(), settings={})
    assert result["state"] == "unavailable"
    assert result["reason_code"] == "stripe_checkout"


def test_cpmt_disappearing_after_refresh_is_unavailable(monkeypatch):
    methods = [{"id": "cpmt_gcash1"}]
    _patch_common(monkeypatch, [
        _checkout_payload(methods=methods, currency="PHP"),
        {**_state(0, "PHP"), "custom_payment_methods": []},
        {**_state(0, "PHP"), "custom_payment_methods": []},
    ])
    result = probe.probe_gcash_payment_method(_account(), settings={})
    assert result["state"] == "unavailable"
    assert result["evidence"]["stable"] is False


def test_cpmt_requires_a_real_custom_method_id(monkeypatch):
    fake_methods = [{"type": "cpmt_not_an_id"}]
    _patch_common(monkeypatch, [
        _checkout_payload(methods=fake_methods, currency="PHP"),
        {**_state(0, "PHP"), "custom_payment_methods": fake_methods},
        {**_state(0, "PHP"), "custom_payment_methods": fake_methods},
    ])
    result = probe.probe_gcash_payment_method(_account(), settings={})
    assert result["state"] == "unavailable"
    assert result["evidence"]["final_custom_payment_method_count"] == 0


def test_technical_failure_is_probe_failed_and_retries(monkeypatch):
    calls = {"count": 0}
    monkeypatch.setattr(probe, "_resolve_proxy_chain", _resolved_chain)
    monkeypatch.setattr(probe, "_browser_profile", lambda _account: {"device_id": "d", "ua": "Mozilla/5.0 Chrome/146.0.0.0", "accept_language": "en-US", "locale": "en-US", "impersonate": "chrome146", "timezone": "America/New_York"})

    def failing_post(self, path, body, proxy, stage):
        calls["count"] += 1
        raise probe.PaymentEligibilityProbeError("upstream unavailable")

    monkeypatch.setattr(probe._CheckoutClient, "post", failing_post)
    result = probe.probe_zero_amount_eligibility(
        _account(),
        settings={"checkout_country_code": "JP"},
        max_attempts=3,
    )
    assert result["state"] == "probe_failed"
    assert result["attempt_count"] == 3
    assert calls["count"] == 3
    assert result["evidence"]["profile"]["proxy_chain"] == {
        "checkout": "JP",
        "promotion": "JP",
        "taxes": "JP",
    }
    assert result["evidence"]["profile"]["billing_country"] == "JP"
    assert result["evidence"]["profile"]["currency"] == "JPY"
    assert result["evidence"]["network"]["stage_regions"]["promotion"] == "JP"


def test_task_interruption_is_not_swallowed(monkeypatch):
    from core.task_runtime import TaskInterruption

    monkeypatch.setattr(probe, "_resolve_proxy_chain", _resolved_chain)

    def stop_post(self, path, body, proxy, stage):
        raise TaskInterruption("stop")

    monkeypatch.setattr(probe._CheckoutClient, "post", stop_post)
    with pytest.raises(TaskInterruption):
        probe.probe_zero_amount_eligibility(_account(), settings={})


def test_dynamic_mode_rejects_a_fixed_proxy_disguised_as_a_template():
    with pytest.raises(probe.PaymentEligibilityProbeError, match="region-XX"):
        probe._resolve_proxy_chain(
            probe.ZERO_AMOUNT_KIND,
            {
                "proxy_mode": "dynamic",
                "proxy": "http://user:pass@127.0.0.1:8080",
            }
        )


def test_zero_amount_direct_mode_fails_closed_without_selected_country_exit():
    with pytest.raises(
        probe.PaymentEligibilityProbeError,
        match="必须使用与结账国家一致的代理出口",
    ):
        probe._resolve_proxy_chain(
            probe.ZERO_AMOUNT_KIND,
            {"proxy_mode": "direct", "checkout_country_code": "JP"},
        )

    assert probe._resolve_proxy_chain(
        probe.GCASH_KIND,
        {"proxy_mode": "direct"},
    ) == {"checkout": "", "promotion": "", "taxes": ""}


def test_dynamic_proxy_chain_uses_canonical_socks5h_runtime_urls(monkeypatch):
    monkeypatch.setattr(probe, "_verify_zero_amount_proxy_country", lambda *_args: None)
    chain = probe._resolve_proxy_chain(
        probe.ZERO_AMOUNT_KIND,
        {
            "proxy_mode": "dynamic",
            "proxy": "socks5://user-region-Rand-sid-seed-t-5:pass@proxy.example:1080",
            "dynamic_proxy_ip_retention_minutes": 120,
            "dynamic_proxy_probe_enabled": False,
        }
    )

    assert set(chain) == {"checkout", "promotion", "taxes"}
    assert all(proxy_url.startswith("socks5h://") for proxy_url in chain.values())
    assert "region-VN" in chain["checkout"]
    assert chain["checkout"] == chain["promotion"] == chain["taxes"]
    assert all("-t-120" in proxy_url for proxy_url in chain.values())


def test_dynamic_proxy_chain_applies_zero_amount_override_but_not_gcash(monkeypatch):
    monkeypatch.setattr(probe, "_verify_zero_amount_proxy_country", lambda *_args: None)
    settings = {
        "proxy_mode": "dynamic",
        "proxy": "socks5://user-region-Rand-sid-seed-t-5:pass@proxy.example:1080",
        "checkout_country_code": "JP",
        "dynamic_proxy_probe_enabled": False,
    }

    zero_chain = probe._resolve_proxy_chain(probe.ZERO_AMOUNT_KIND, settings)
    gcash_chain = probe._resolve_proxy_chain(probe.GCASH_KIND, settings)

    assert "region-JP" in zero_chain["checkout"]
    assert zero_chain["checkout"] == zero_chain["promotion"] == zero_chain["taxes"]
    assert "region-VN" in gcash_chain["promotion"]
    assert "region-US" in gcash_chain["checkout"]
    assert "region-US" in gcash_chain["taxes"]


def test_fixed_specified_proxy_is_normalized_and_country_verified(monkeypatch):
    monkeypatch.setattr(
        "services.proxy_scanner.scan_proxy_url",
        lambda *_args, **_kwargs: {
            "basic": {"ok": True, "exit_ip": "203.0.113.10"},
            "geo": {"ok": True, "country_code": "VN"},
        },
    )
    chain = probe._resolve_proxy_chain(
        probe.ZERO_AMOUNT_KIND,
        {
            "proxy_mode": "specified",
            "proxy": "socks5://user:pass@proxy.example:1080",
        }
    )

    assert chain == {
        "checkout": "socks5h://user:pass@proxy.example:1080",
        "promotion": "socks5h://user:pass@proxy.example:1080",
        "taxes": "socks5h://user:pass@proxy.example:1080",
    }


def test_fixed_specified_proxy_rejects_country_mismatch(monkeypatch):
    monkeypatch.setattr(
        "services.proxy_scanner.scan_proxy_url",
        lambda *_args, **_kwargs: {
            "basic": {"ok": True, "exit_ip": "203.0.113.10"},
            "geo": {"ok": True, "country_code": "US"},
        },
    )

    with pytest.raises(
        probe.PaymentEligibilityProbeError,
        match="expected=JP, actual=US",
    ):
        probe._resolve_proxy_chain(
            probe.ZERO_AMOUNT_KIND,
            {
                "proxy_mode": "specified",
                "proxy": "socks5://user:pass@proxy.example:1080",
                "checkout_country_code": "JP",
            },
        )


def test_dynamic_proxy_is_strictly_geo_verified_once(monkeypatch):
    calls = []

    def fake_scan(proxy_url, **kwargs):
        calls.append((proxy_url, kwargs))
        return {
            "basic": {"ok": True, "exit_ip": "203.0.113.10"},
            "geo": {"ok": True, "country_code": "JP"},
        }

    monkeypatch.setattr("services.proxy_scanner.scan_proxy_url", fake_scan)
    chain = probe._resolve_proxy_chain(
        probe.ZERO_AMOUNT_KIND,
        {
            "proxy_mode": "dynamic",
            "proxy": "socks5://user-region-Rand-sid-seed-t-5:pass@proxy.example:1080",
            "checkout_country_code": "JP",
            "dynamic_proxy_probe_enabled": True,
        },
    )

    assert chain["checkout"] == chain["promotion"] == chain["taxes"]
    assert len(calls) == 1
    assert calls[0][1]["targets"] == ["basic", "geo"]


def test_zero_amount_reuses_one_http_session_but_gcash_keeps_per_request_sessions(monkeypatch):
    instances = []

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {}

    class Session:
        def __init__(self, *args, **kwargs):
            self.headers = {}
            self.proxies = {}
            self.closed = 0
            instances.append(self)

        def post(self, *args, **kwargs):
            return Response()

        def close(self):
            self.closed += 1

    monkeypatch.setattr(probe.cffi_requests, "Session", Session)
    profile = {
        "device_id": "device-1",
        "ua": "Mozilla/5.0 Chrome/146.0.0.0",
        "accept_language": "en-US",
        "impersonate": "chrome146",
    }

    zero_client = probe._CheckoutClient(_account(), profile, reuse_session=True)
    zero_client.post("/one", {}, "socks5h://proxy.example:1080", "one")
    zero_client.post("/two", {}, "socks5h://proxy.example:1080", "two")
    zero_client.close()
    assert len(instances) == 1
    assert instances[0].closed == 1

    gcash_client = probe._CheckoutClient(_account(), profile)
    gcash_client.post("/one", {}, "", "one")
    gcash_client.post("/two", {}, "", "two")
    gcash_client.close()
    assert len(instances) == 3
    assert [item.closed for item in instances[1:]] == [1, 1]


def test_checkout_network_error_includes_the_failed_stage(monkeypatch):
    class FailingSession:
        def __init__(self, *args, **kwargs):
            self.headers = {}
            self.proxies = {}

        def post(self, *args, **kwargs):
            raise RuntimeError("curl 35")

        def close(self):
            pass

    monkeypatch.setattr(probe.cffi_requests, "Session", FailingSession)
    client = probe._CheckoutClient(
        _account(),
        {
            "device_id": "device-1",
            "ua": "Mozilla/5.0 Chrome/146.0.0.0",
            "accept_language": "en-US",
            "impersonate": "chrome146",
        },
    )

    with pytest.raises(probe.PaymentEligibilityProbeError, match="checkout 创建 网络失败: curl 35"):
        client.post("/backend-api/payments/checkout", {}, "", "checkout 创建")


def test_checkout_http_error_includes_safe_business_detail(monkeypatch):
    class ForbiddenResponse:
        status_code = 403

        @staticmethod
        def json():
            return {"detail": "This promotion is not available."}

    class ForbiddenSession:
        def __init__(self, *args, **kwargs):
            self.headers = {}
            self.proxies = {}

        def post(self, *args, **kwargs):
            return ForbiddenResponse()

        def close(self):
            pass

    monkeypatch.setattr(probe.cffi_requests, "Session", ForbiddenSession)
    client = probe._CheckoutClient(
        _account(),
        {
            "device_id": "device-1",
            "ua": "Mozilla/5.0 Chrome/146.0.0.0",
            "accept_language": "en-US",
            "impersonate": "chrome146",
        },
    )

    with pytest.raises(
        probe.PaymentEligibilityHttpError,
        match=r"promotion 刷新 HTTP 403: This promotion is not available\.",
    ):
        client.post("/backend-api/payments/checkout/update", {}, "", "promotion 刷新")


def test_zero_amount_promotion_unavailable_is_an_ineligible_business_result(monkeypatch):
    calls = []
    monkeypatch.setattr(probe, "_resolve_proxy_chain", _resolved_chain)
    monkeypatch.setattr(probe, "_browser_profile", lambda _account: {"device_id": "d", "ua": "ua", "accept_language": "en-US", "impersonate": "chrome146"})

    def fake_post(self, path, body, proxy, stage, **kwargs):
        calls.append((path, proxy))
        if path == "/backend-api/payments/checkout":
            return _checkout_payload(amount=110000)
        if path == "/backend-api/payments/checkout/update":
            raise probe.PaymentEligibilityHttpError(stage, 403, "This promotion is not available.")
        raise AssertionError(f"unexpected call: {path}")

    monkeypatch.setattr(probe._CheckoutClient, "post", fake_post)
    result = probe.probe_zero_amount_eligibility(
        _account(),
        settings={"checkout_country_code": "JP"},
        max_attempts=4,
    )

    assert result["state"] == "ineligible"
    assert result["business_result"] is True
    assert result["reason_code"] == "promotion_unavailable"
    assert result["attempt_count"] == 1
    assert result["evidence"]["verified_stage"] == "promotion_rejected"
    assert result["evidence"]["upstream_status"] == 403
    assert result["evidence"]["profile"]["proxy_chain"] == {
        "checkout": "JP",
        "promotion": "JP",
        "taxes": "JP",
    }
    assert calls == [
        ("/backend-api/payments/checkout", "jp"),
        ("/backend-api/payments/checkout/update", "jp"),
    ]


@pytest.mark.parametrize("kind", [probe.ZERO_AMOUNT_KIND, probe.GCASH_KIND])
def test_other_promotion_403_responses_remain_technical_failures(monkeypatch, kind):
    calls = []
    monkeypatch.setattr(probe, "_resolve_proxy_chain", _resolved_chain)
    monkeypatch.setattr(probe, "_browser_profile", lambda _account: {"device_id": "d", "ua": "ua", "accept_language": "en-US", "impersonate": "chrome146"})

    def fake_post(self, path, body, proxy, stage, **kwargs):
        calls.append(path)
        if path == "/backend-api/payments/checkout":
            currency = "PHP" if kind == probe.GCASH_KIND else "VND"
            return _checkout_payload(amount=110000, currency=currency)
        raise probe.PaymentEligibilityHttpError(stage, 403, "Access denied")

    monkeypatch.setattr(probe._CheckoutClient, "post", fake_post)
    result = probe.run_payment_eligibility_probe(_account(), kind, settings={}, max_attempts=2)

    assert result["state"] == "probe_failed"
    assert result["business_result"] is False
    assert result["attempt_count"] == 2
    assert result["message"] == "promotion 刷新 HTTP 403: Access denied"
    assert calls.count("/backend-api/payments/checkout") == 2
    assert calls.count("/backend-api/payments/checkout/update") == 2


def test_gcash_does_not_reclassify_promotion_unavailable_as_zero_amount_result(monkeypatch):
    monkeypatch.setattr(probe, "_resolve_proxy_chain", _resolved_chain)
    monkeypatch.setattr(probe, "_browser_profile", lambda _account: {"device_id": "d", "ua": "ua", "accept_language": "en-US", "impersonate": "chrome146"})

    def fake_post(self, path, body, proxy, stage, **kwargs):
        if path == "/backend-api/payments/checkout":
            return _checkout_payload(amount=110000, currency="PHP")
        raise probe.PaymentEligibilityHttpError(stage, 403, "This promotion is not available.")

    monkeypatch.setattr(probe._CheckoutClient, "post", fake_post)
    result = probe.probe_gcash_payment_method(_account(), settings={}, max_attempts=1)

    assert result["state"] == "probe_failed"
    assert result["message"] == "promotion 刷新 HTTP 403: This promotion is not available."


def test_stage_regions_default_override_and_gcash_isolation():
    assert probe.payment_eligibility_stage_regions(probe.ZERO_AMOUNT_KIND, {}) == {
        "checkout": "VN",
        "promotion": "VN",
        "taxes": "VN",
    }
    assert probe.payment_eligibility_stage_regions(
        probe.ZERO_AMOUNT_KIND,
        {"checkout_country_code": "jp"},
    ) == {
        "checkout": "JP",
        "promotion": "JP",
        "taxes": "JP",
    }
    assert probe.payment_eligibility_stage_regions(
        probe.ZERO_AMOUNT_KIND,
        {"promotion_proxy_country_code": "ph"},
    ) == {"checkout": "PH", "promotion": "PH", "taxes": "PH"}
    assert probe.payment_eligibility_stage_regions(
        probe.GCASH_KIND,
        {"promotion_proxy_country_code": "JP"},
    ) == {
        "checkout": "US",
        "promotion": "VN",
        "taxes": "US",
    }


def test_zero_amount_success_and_gcash_success_record_effective_stage_regions(monkeypatch):
    _patch_common(monkeypatch, [
        _checkout_payload(currency="JPY"),
        _state(0, "JPY"),
        _state(0, "JPY"),
    ])
    zero = probe.probe_zero_amount_eligibility(
        _account(),
        settings={"checkout_country_code": "JP"},
    )
    assert zero["state"] == "eligible"
    assert zero["evidence"]["profile"] == {
        "plan": "chatgptplusplan",
        "billing_country": "JP",
        "currency": "JPY",
        "checkout_ui_mode": "custom",
        "proxy_chain": {"checkout": "JP", "promotion": "JP", "taxes": "JP"},
    }
    assert zero["evidence"]["network"]["stage_regions"] == {
        "checkout": "JP",
        "promotion": "JP",
        "taxes": "JP",
    }

    _patch_common(monkeypatch, [_checkout_payload("cs_demo"), {}, {}])
    gcash = probe.probe_gcash_payment_method(
        _account(),
        settings={"promotion_proxy_country_code": "JP"},
    )
    assert gcash["state"] == "unavailable"
    assert gcash["evidence"]["profile"]["proxy_chain"]["promotion"] == "VN"
    assert gcash["evidence"]["network"]["stage_regions"]["promotion"] == "VN"


def test_zero_amount_selected_country_reaches_checkout_and_taxes_bodies(monkeypatch):
    calls = []
    monkeypatch.setattr(probe, "_resolve_proxy_chain", lambda *_args: {
        "checkout": "same-jp-proxy",
        "promotion": "same-jp-proxy",
        "taxes": "same-jp-proxy",
    })
    monkeypatch.setattr(probe, "_browser_profile", lambda _account: {
        "device_id": "device-1",
        "ua": "ua",
        "accept_language": "ja-JP",
        "locale": "ja-JP",
        "impersonate": "chrome146",
        "timezone": "Asia/Tokyo",
    })

    def fake_post(self, path, body, proxy, stage, **kwargs):
        calls.append((path, body, proxy))
        if path == "/backend-api/payments/checkout":
            return _checkout_payload(currency="JPY")
        return _state(0, "JPY")

    monkeypatch.setattr(probe._CheckoutClient, "post", fake_post)
    result = probe.probe_zero_amount_eligibility(
        _account(),
        settings={"checkout_country_code": "JP"},
        max_attempts=1,
    )

    assert result["state"] == "eligible"
    assert [proxy_url for _path, _body, proxy_url in calls] == [
        "same-jp-proxy",
        "same-jp-proxy",
        "same-jp-proxy",
    ]
    assert calls[0][1]["billing_details"] == {"country": "JP", "currency": "JPY"}
    assert calls[2][1]["billing_country"] == "JP"
    assert calls[2][1]["currency"] == "JPY"
    assert calls[2][1]["billing_address"]["country"] == "JP"


def test_checkout_profile_rejects_unsupported_country_and_formats_minor_units():
    with pytest.raises(ValueError, match="不受支持"):
        probe.payment_eligibility_profile(
            probe.ZERO_AMOUNT_KIND,
            {"checkout_country_code": "ZZ"},
        )
    assert probe.format_minor_amount(110000, "PHP") == "1,100.00 PHP"
    assert probe.format_minor_amount(110000, "VND") == "1,100.00 VND"
    assert probe.format_minor_amount(2200, "JPY") == "2,200 JPY"
