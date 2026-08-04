import pytest
from fastapi import HTTPException

from core.dynamic_proxy import declared_proxy_region, resolve_dynamic_proxy_template
from core.proxy_utils import (
    _dynamic_probe_source,
    normalize_proxy_url,
    resolve_default_chatgpt_proxy_with_metadata,
    resolve_probe_candidate_proxies,
)
from services.chatgpt_core.task_logging import sanitize_task_detail


TEMPLATE = "socks5://acct-region-JP-sid-oldsid-t-1:secret@example.cliproxy.io:3010"
RAND_TEMPLATE = "socks5://acct-region-Rand-sid-oldsid-t-5:secret@example.cliproxy.io:3010"


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


def test_dynamic_proxy_rewrites_full_region_rand_token_without_suffix_leak():
    resolved = resolve_dynamic_proxy_template(RAND_TEMPLATE, "jp", refresh_sid=False)

    assert resolved.template_country_code == "RAND"
    assert resolved.resolved_country_code == "JP"
    assert "region-JP-sid-" in resolved.proxy_url
    assert "region-JPnd" not in resolved.proxy_url


def test_dynamic_proxy_can_override_cliproxy_retention_token():
    resolved = resolve_dynamic_proxy_template(TEMPLATE, "US", refresh_sid=False, retention_minutes=15)

    assert "region-US" in resolved.proxy_url
    assert "sid-oldsid-t-15" in resolved.proxy_url
    assert resolved.retention_minutes == 15
    assert resolved.retention_applied is True


def test_dynamic_proxy_inserts_retention_after_sid_when_template_lacks_t_token():
    template = "socks5://acct-region-Rand-sid-oldsid:secret@example.cliproxy.io:3010"
    resolved = resolve_dynamic_proxy_template(template, "SG", refresh_sid=False, retention_minutes=7)

    assert "region-SG" in resolved.proxy_url
    assert "sid-oldsid-t-7:secret@" in resolved.proxy_url


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


def test_dynamic_proxy_candidate_uses_configured_retention_minutes(monkeypatch):
    def fake_configured_value(key, default=""):
        if key == "dynamic_proxy_ip_retention_minutes":
            return "12"
        return default

    monkeypatch.setattr("core.proxy_utils._configured_value", fake_configured_value)
    candidates = resolve_probe_candidate_proxies(
        {
            "proxy_mode": "dynamic",
            "proxy": RAND_TEMPLATE,
            "proxy_country_code": "US",
            "dynamic_proxy_probe_enabled": False,
        }
    )

    assert len(candidates) == 1
    assert "region-US-sid-" in candidates[0][0]
    assert "region-USnd" not in candidates[0][0]
    assert "-t-12" in candidates[0][0]
    assert "retention=t-12" in candidates[0][2]


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
            retention_minutes=9,
            refresh_sid=True,
            probe=False,
        )
    )
    dumped = str(result)
    assert result["ok"] is True
    assert result["expected_country"] == "US"
    assert result["retention_minutes"] == 9
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


def test_default_chatgpt_proxy_uses_global_dynamic_config(monkeypatch):
    def fake_configured_value(key, default=""):
        values = {
            "task_proxy_mode": "dynamic",
            "task_proxy_url": "socks5://legacy-region-CA-sid-oldsid-t-1:secret@legacy.example:3010",
            "task_proxy_country_code": "CA",
            "dynamic_proxy_template": RAND_TEMPLATE,
            "dynamic_proxy_default_country": "US",
            "dynamic_proxy_probe_enabled": "false",
            "dynamic_proxy_ip_retention_minutes": "9",
        }
        return values.get(key, default)

    monkeypatch.setattr("core.proxy_utils._configured_value", fake_configured_value)
    proxy_url, proxy_pool, source = resolve_default_chatgpt_proxy_with_metadata()

    assert proxy_pool is None
    assert proxy_url.startswith("socks5h://")
    assert "region-US-sid-" in proxy_url
    assert "-t-9" in proxy_url
    assert "dynamic country=US" in source


def test_default_dynamic_proxy_stops_after_first_usable_sid(monkeypatch):
    calls = []

    def fake_configured_value(key, default=""):
        values = {
            "task_proxy_mode": "dynamic",
            "task_proxy_failover": "true",
            "dynamic_proxy_max_attempts": "5",
        }
        return values.get(key, default)

    def fake_resolve(params, **_kwargs):
        calls.append(dict(params))
        return [("socks5h://main-sid.example:3010", None, "dynamic probe=ok")]

    monkeypatch.setattr("core.proxy_utils._configured_value", fake_configured_value)
    monkeypatch.setattr("core.proxy_utils.resolve_task_proxy_candidates", fake_resolve)

    proxy_url, proxy_pool, source = resolve_default_chatgpt_proxy_with_metadata()

    assert proxy_url == "socks5h://main-sid.example:3010"
    assert proxy_pool is None
    assert source == "dynamic probe=ok"
    assert calls == [
        {
            "proxy_mode": "dynamic",
            "proxy_failover": False,
            "dynamic_proxy_max_attempts": 1,
        }
    ]


def test_default_dynamic_proxy_uses_more_sid_budget_only_after_prepare_failure(monkeypatch):
    calls = []

    def fake_configured_value(key, default=""):
        values = {
            "task_proxy_mode": "dynamic",
            "task_proxy_failover": "true",
            "dynamic_proxy_max_attempts": "5",
        }
        return values.get(key, default)

    def fake_resolve(params, **_kwargs):
        calls.append(dict(params))
        if len(calls) < 3:
            raise RuntimeError(f"candidate {len(calls)} failed health probe")
        return [("socks5h://replacement-sid.example:3010", None, "dynamic probe=ok")]

    monkeypatch.setattr("core.proxy_utils._configured_value", fake_configured_value)
    monkeypatch.setattr("core.proxy_utils.resolve_task_proxy_candidates", fake_resolve)

    proxy_url, _, _ = resolve_default_chatgpt_proxy_with_metadata()

    assert proxy_url == "socks5h://replacement-sid.example:3010"
    assert len(calls) == 3
    assert all(call["dynamic_proxy_max_attempts"] == 1 for call in calls)
    assert all(call["proxy_failover"] is False for call in calls)


def test_global_dynamic_canonical_template_wins_over_legacy_task_proxy_url(monkeypatch):
    canonical_template = "socks5://canonical-region-Rand-sid-oldsid-t-1:secret@canonical.example:3010"
    legacy_template = "socks5://legacy-region-Rand-sid-oldsid-t-1:secret@legacy.example:3010"

    def fake_configured_value(key, default=""):
        values = {
            "task_proxy_mode": "dynamic",
            "task_proxy_url": legacy_template,
            "task_proxy_country_code": "CA",
            "dynamic_proxy_template": canonical_template,
            "dynamic_proxy_default_country": "US",
            "dynamic_proxy_probe_enabled": "false",
        }
        return values.get(key, default)

    monkeypatch.setattr("core.proxy_utils._configured_value", fake_configured_value)
    proxy_url, proxy_pool, source = resolve_default_chatgpt_proxy_with_metadata()

    assert proxy_pool is None
    assert "canonical.example" in proxy_url
    assert "legacy.example" not in proxy_url
    assert "region-US" in proxy_url
    assert "dynamic country=US" in source


def test_global_dynamic_legacy_task_proxy_fields_remain_a_compatibility_fallback(monkeypatch):
    legacy_template = "socks5://legacy-region-Rand-sid-oldsid-t-1:secret@legacy.example:3010"

    def fake_configured_value(key, default=""):
        values = {
            "task_proxy_mode": "dynamic",
            "task_proxy_url": legacy_template,
            "task_proxy_country_code": "SG",
            "dynamic_proxy_template": "",
            "dynamic_proxy_default_country": "",
            "dynamic_proxy_probe_enabled": "false",
        }
        return values.get(key, default)

    monkeypatch.setattr("core.proxy_utils._configured_value", fake_configured_value)
    proxy_url, proxy_pool, source = resolve_default_chatgpt_proxy_with_metadata()

    assert proxy_pool is None
    assert "legacy.example" in proxy_url
    assert "region-SG" in proxy_url
    assert "dynamic country=SG" in source


def test_global_specified_mode_keeps_task_proxy_url_semantics(monkeypatch):
    specified_proxy = "http://user:pass@specified.example:8080"

    def fake_configured_value(key, default=""):
        values = {
            "task_proxy_mode": "specified",
            "task_proxy_url": specified_proxy,
            "dynamic_proxy_template": RAND_TEMPLATE,
        }
        return values.get(key, default)

    monkeypatch.setattr("core.proxy_utils._configured_value", fake_configured_value)
    proxy_url, proxy_pool, source = resolve_default_chatgpt_proxy_with_metadata()

    assert proxy_url == specified_proxy
    assert proxy_pool is None
    assert source == "specified"


def test_dynamic_preview_uses_legacy_global_dynamic_fields_as_fallback(monkeypatch):
    from api.proxies import DynamicProxyPreviewRequest, dynamic_proxy_preview

    def fake_configured_value(key, default=""):
        values = {
            "task_proxy_url": RAND_TEMPLATE,
            "task_proxy_country_code": "SG",
            "dynamic_proxy_template": "",
            "dynamic_proxy_default_country": "",
        }
        return values.get(key, default)

    monkeypatch.setattr("core.proxy_utils._configured_value", fake_configured_value)
    result = dynamic_proxy_preview(DynamicProxyPreviewRequest(probe=False, refresh_sid=False))

    assert result["ok"] is True
    assert result["expected_country"] == "SG"
    assert "secret" not in str(result)


def test_custom_email_dynamic_country_prefers_canonical_global_default(monkeypatch):
    from api.tasks import CustomEmailRecheckTaskRequest, _custom_email_proxy_settings

    def fake_configured_value(key, default=""):
        values = {
            "dynamic_proxy_default_country": "US",
            "task_proxy_country_code": "JP",
        }
        return values.get(key, default)

    monkeypatch.setattr("core.proxy_utils._configured_value", fake_configured_value)
    settings = _custom_email_proxy_settings(
        CustomEmailRecheckTaskRequest(email="test@example.com", proxy_mode="dynamic")
    )

    assert settings["proxy_country_code"] == "US"


def test_default_chatgpt_proxy_can_be_disabled_by_global_direct(monkeypatch):
    def fake_configured_value(key, default=""):
        if key == "task_proxy_mode":
            return "direct"
        if key == "dynamic_proxy_template":
            return RAND_TEMPLATE
        return default

    monkeypatch.setattr("core.proxy_utils._configured_value", fake_configured_value)
    proxy_url, proxy_pool, source = resolve_default_chatgpt_proxy_with_metadata()

    assert proxy_url == ""
    assert proxy_pool is None
    assert source == "direct"


def test_explicit_task_proxy_still_wins_over_global_dynamic(monkeypatch):
    def fake_configured_value(key, default=""):
        values = {
            "task_proxy_mode": "dynamic",
            "dynamic_proxy_template": RAND_TEMPLATE,
            "dynamic_proxy_probe_enabled": "false",
        }
        return values.get(key, default)

    monkeypatch.setattr("core.proxy_utils._configured_value", fake_configured_value)
    candidates = resolve_probe_candidate_proxies({"proxy": "http://proxy.local:8080"})

    assert candidates == [("http://proxy.local:8080", None, "specified")]


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
