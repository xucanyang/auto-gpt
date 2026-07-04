import pytest
from fastapi import HTTPException

from core.dynamic_proxy import declared_proxy_region, resolve_dynamic_proxy_template
from core.proxy_utils import _dynamic_probe_source, normalize_proxy_url, resolve_probe_candidate_proxies
from services.chatgpt_core.task_logging import sanitize_task_detail


TEMPLATE = "socks5://acct-region-JP-sid-oldsid-t-1:secret@example.cliproxy.io:3010"


def test_dynamic_proxy_rewrites_region_refreshes_sid_and_redacts_credentials():
    resolved = resolve_dynamic_proxy_template(TEMPLATE, "us", refresh_sid=True)

    assert resolved.requested_country_code == "US"
    assert resolved.template_country_code == "JP"
    assert declared_proxy_region(resolved.proxy_url) == "US"
    assert "region-US" in resolved.proxy_url
    assert "sid-oldsid-t-" not in resolved.proxy_url
    assert resolved.sid_refreshed is True
    assert "secret" not in resolved.redacted_proxy_url
    assert "acct-region" not in resolved.redacted_proxy_url


def test_dynamic_proxy_requires_region_marker():
    with pytest.raises(ValueError, match="region-XX"):
        resolve_dynamic_proxy_template("http://user:pass@127.0.0.1:8080", "US")


def test_dynamic_proxy_requires_two_letter_country():
    with pytest.raises(ValueError, match="两位 ISO"):
        resolve_dynamic_proxy_template(TEMPLATE, "USA")


def test_resolve_probe_candidate_proxies_dynamic_generates_fresh_normalized_candidates():
    candidates = resolve_probe_candidate_proxies(
        {
            "proxy_mode": "dynamic",
            "proxy": TEMPLATE,
            "proxy_country_code": "US",
            "proxy_failover": True,
            "dynamic_proxy_max_attempts": 3,
            "dynamic_proxy_probe_enabled": False,
        }
    )

    assert len(candidates) == 3
    urls = [item[0] for item in candidates]
    assert len(set(urls)) == 3
    for url, pool, source in candidates:
        assert pool is None
        assert url.startswith("socks5h://")
        assert "region-US" in url
        assert "sid-oldsid-t-" not in url
        assert "dynamic country=US" in source
        assert "probe=disabled" in source


def test_normalize_proxy_url_uses_socks5h_for_remote_dns():
    assert normalize_proxy_url(TEMPLATE) == TEMPLATE.replace("socks5://", "socks5h://", 1)


def test_dynamic_proxy_uses_dynamic_attempts_not_pool_candidate_count():
    candidates = resolve_probe_candidate_proxies(
        {
            "proxy_mode": "dynamic",
            "proxy": TEMPLATE,
            "proxy_country_code": "US",
            "proxy_failover": True,
            "proxy_max_candidates": 9,
            "dynamic_proxy_max_attempts": 2,
            "dynamic_proxy_probe_enabled": False,
        }
    )

    assert len(candidates) == 2


def test_specified_mode_does_not_rewrite_region_or_sid():
    proxy = "http://acct-region-JP-sid-oldsid-t-1:secret@example.cliproxy.io:3010"
    candidates = resolve_probe_candidate_proxies(
        {
            "proxy_mode": "specified",
            "proxy": proxy,
            "proxy_country_code": "US",
        }
    )

    assert candidates == [(proxy, None, "specified")]


def test_dynamic_proxy_template_key_is_redacted_in_task_detail():
    safe = sanitize_task_detail({"params": {"dynamic_proxy_template": TEMPLATE}})
    dumped = str(safe)
    assert "secret" not in dumped
    assert "acct-region" not in dumped
    assert "***:***@example.cliproxy.io:3010" in dumped


def test_dynamic_preview_response_never_returns_raw_credentials():
    from api.proxies import DynamicProxyPreviewRequest, dynamic_proxy_preview

    result = dynamic_proxy_preview(
        DynamicProxyPreviewRequest(
            proxy=TEMPLATE,
            country_code="US",
            refresh_sid=True,
            probe=False,
        )
    )
    dumped = str(result)
    assert result["ok"] is True
    assert result["expected_country"] == "US"
    assert "runtime_proxy_redacted" in result
    assert "secret" not in dumped
    assert "acct-region" not in dumped


def test_dynamic_preview_rejects_missing_region_marker():
    from api.proxies import DynamicProxyPreviewRequest, dynamic_proxy_preview

    with pytest.raises(HTTPException) as exc:
        dynamic_proxy_preview(
            DynamicProxyPreviewRequest(
                proxy="http://user:pass@127.0.0.1:8080",
                country_code="US",
                probe=False,
            )
        )
    assert exc.value.status_code == 400


def test_dynamic_proxy_uses_config_default_country_when_task_country_empty(monkeypatch):
    def fake_configured_value(key, default=""):
        if key == "dynamic_proxy_default_country":
            return "US"
        return default

    monkeypatch.setattr("core.proxy_utils._configured_value", fake_configured_value)
    candidates = resolve_probe_candidate_proxies(
        {
            "proxy_mode": "dynamic",
            "proxy": TEMPLATE,
            "dynamic_proxy_probe_enabled": False,
        }
    )

    assert len(candidates) == 1
    assert "region-US" in candidates[0][0]
    assert "dynamic country=US" in candidates[0][2]


def test_dynamic_probe_accepts_cliproxy_declared_country_when_geo_is_unavailable(monkeypatch):
    def fake_scan_proxy_url(*_args, **_kwargs):
        return {
            "basic": {"ok": True, "exit_ip": "203.0.113.10", "latency_ms": 12},
            "geo": {"ok": False, "error_code": "http_429", "error": "HTTP 429"},
        }

    monkeypatch.setattr("services.proxy_scanner.scan_proxy_url", fake_scan_proxy_url)

    ok, source = _dynamic_probe_source(
        "socks5://acct-region-US-sid-newsid-t-1:secret@example.cliproxy.io:3010",
        expected_country="US",
        declared_country="US",
        provider="cliproxy",
        sid_refreshed=True,
        timeout_seconds=3,
        require_country_match=True,
    )

    assert ok is True
    assert "actual=unverified" in source
    assert "probe=geo_unavailable" in source


def test_dynamic_probe_rejects_real_country_mismatch(monkeypatch):
    def fake_scan_proxy_url(*_args, **_kwargs):
        return {
            "basic": {"ok": True, "exit_ip": "203.0.113.10", "latency_ms": 12},
            "geo": {"ok": True, "country_code": "JP", "source": "cloudflare_trace"},
        }

    monkeypatch.setattr("services.proxy_scanner.scan_proxy_url", fake_scan_proxy_url)

    ok, source = _dynamic_probe_source(
        "socks5://acct-region-US-sid-newsid-t-1:secret@example.cliproxy.io:3010",
        expected_country="US",
        declared_country="US",
        provider="cliproxy",
        sid_refreshed=True,
        timeout_seconds=3,
        require_country_match=True,
    )

    assert ok is False
    assert "country_mismatch" in source
