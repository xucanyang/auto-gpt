from __future__ import annotations

from dataclasses import replace

import pytest

from core.miyaip_proxy import (
    MIYAIP_GENERATE_URL,
    MiyaIPError,
    MiyaIPProxyResolution,
    generate_miyaip_proxy,
    parse_miyaip_generate_response,
    parse_miyaip_proxy_line,
)
from core.proxy_utils import resolve_probe_candidate_proxies


class FakeResponse:
    def __init__(self, body: str, status_code: int = 200) -> None:
        self.content = body.encode("utf-8")
        self.status_code = status_code
        self.encoding = "utf-8"


def _resolution(proxy_url: str = "http://user:pass@proxy.miya.test:10000") -> MiyaIPProxyResolution:
    return MiyaIPProxyResolution(
        proxy_url=proxy_url,
        requested_country_code="US",
        resolved_country_code="US",
        provider="miyaip",
        protocol="http",
        gateway_server="us",
        username="user",
        password="pass",
        redacted_proxy_url="http://***:***@proxy.miya.test:10000",
    )


def test_resolution_repr_never_contains_proxy_credentials():
    result = _resolution("http://sensitive-user:sensitive-pass@proxy.miya.test:10000")

    dumped = repr(result)
    assert "sensitive-user" not in dumped
    assert "sensitive-pass" not in dumped
    assert "proxy_url='http://sensitive" not in dumped
    assert "redacted_proxy_url='http://***:***@proxy.miya.test:10000'" in dumped


def test_generate_uses_fixed_miyaip_contract_and_percent_encodes_userinfo(monkeypatch):
    captured = {}

    def fake_get(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return FakeResponse("user name:p@ss/word@proxy.miya.test:10000")

    monkeypatch.setattr("core.miyaip_proxy.requests.get", fake_get)
    result = generate_miyaip_proxy(
        "us",
        crc="crc-value",
        key_name="key-value",
        pool=7,
        gateway_server="AS",
        protocol="http",
        timeout_seconds=11,
    )

    assert captured["url"] == MIYAIP_GENERATE_URL
    assert captured["params"] == {
        "Num": 1,
        "Country": "US",
        "SessionTime": -1,
        "Server": "as",
        "Format": 1,
        "Crc": "crc-value",
        "Pool": 7,
        "KeyName": "key-value",
        "GenType": "http",
    }
    assert captured["timeout"] == 11
    assert captured["allow_redirects"] is False
    assert captured["stream"] is True
    assert result.proxy_url == "http://user%20name:p%40ss%2Fword@proxy.miya.test:10000"
    assert result.redacted_proxy_url == "http://***:***@proxy.miya.test:10000"


def test_generate_rejects_http_200_business_error_without_leaking_secrets(monkeypatch):
    crc = "crc-sensitive-value"
    key_name = "key-sensitive-value"
    body = (
        '{"code":207,"message":"Generate failed Crc='
        + crc
        + " KeyName="
        + key_name
        + ' https://miyaip.com/api/ProxyLogic/Generate?Crc=leak","body":null}'
    )
    monkeypatch.setattr(
        "core.miyaip_proxy.requests.get",
        lambda *_args, **_kwargs: FakeResponse(body),
    )

    with pytest.raises(MiyaIPError) as exc:
        generate_miyaip_proxy("US", crc=crc, key_name=key_name)

    message = str(exc.value)
    assert "207" not in message or "生成失败" in message
    assert crc not in message
    assert key_name not in message
    assert "?Crc=" not in message
    assert "MiyaIP" in message


def test_complete_config_validation_rejects_missing_credentials_without_network(monkeypatch):
    from core.miyaip_proxy import normalize_miyaip_config

    request = monkeypatch.setattr(
        "core.miyaip_proxy.requests.get",
        lambda *_args, **_kwargs: pytest.fail("validation must not make a request"),
    )
    assert request is None
    with pytest.raises(ValueError, match="Crc 不能为空"):
        normalize_miyaip_config(crc="", key_name="key-value")
    with pytest.raises(ValueError, match="KeyName 不能为空"):
        normalize_miyaip_config(crc="crc-value", key_name="")


@pytest.mark.parametrize(
    ("line", "protocol", "expected"),
    [
        (
            "user:pass@proxy.miya.test:10000",
            "http",
            "http://user:pass@proxy.miya.test:10000",
        ),
        (
            "socks5://user:p%40ss@proxy.miya.test:1080",
            "socks5",
            "socks5h://user:p%40ss@proxy.miya.test:1080",
        ),
        (
            "proxy.miya.test:8080:user:pass",
            "http",
            "http://user:pass@proxy.miya.test:8080",
        ),
    ],
)
def test_proxy_parser_accepts_supported_provider_rows(line, protocol, expected):
    proxy_url, username, password = parse_miyaip_proxy_line(line, protocol)
    assert proxy_url == expected
    assert username == "user"
    assert password in {"pass", "p@ss"}


@pytest.mark.parametrize(
    "line",
    [
        "proxy.miya.test:8080",
        "user@proxy.miya.test:8080",
        "user:pass@bad host:8080",
        "ftp://user:pass@proxy.miya.test:21",
        "http://user:pass@proxy.miya.test:8080/path?secret=1",
    ],
)
def test_proxy_parser_rejects_malformed_or_unauthenticated_rows(line):
    with pytest.raises(MiyaIPError):
        parse_miyaip_proxy_line(line)


def test_generate_response_parser_rejects_json_arrays_and_empty_success_body():
    with pytest.raises(MiyaIPError, match="无效 JSON"):
        parse_miyaip_generate_response('["user:pass@proxy.miya.test:8080"]')
    with pytest.raises(MiyaIPError, match="没有代理地址"):
        parse_miyaip_generate_response('{"code":200,"message":"ok","body":null}')


def test_resolver_dispatches_to_miyaip_without_reading_cliproxy_template(monkeypatch):
    calls = []

    def fake_generate(country_code, **kwargs):
        calls.append((country_code, dict(kwargs)))
        return _resolution()

    monkeypatch.setattr("core.miyaip_proxy.generate_miyaip_proxy", fake_generate)
    candidates = resolve_probe_candidate_proxies(
        {
            "proxy_mode": "dynamic",
            "dynamic_proxy_provider": "miyaip",
            "proxy_country_code": "US",
            "proxy_failover": False,
            "dynamic_proxy_probe_enabled": False,
            "miyaip_crc": "crc-value",
            "miyaip_key_name": "key-value",
            "miyaip_pool": 3,
            "miyaip_gateway_server": "eu",
            "miyaip_protocol": "http",
            "miyaip_request_timeout_seconds": 12,
        }
    )

    assert candidates == [
        (
            "http://user:pass@proxy.miya.test:10000",
            None,
            "dynamic country=US actual=unverified provider=miyaip "
            "line=generated gateway=us protocol=http probe=disabled",
        )
    ]
    assert calls == [
        (
            "US",
            {
                "crc": "crc-value",
                "key_name": "key-value",
                "pool": 3,
                "gateway_server": "eu",
                "protocol": "http",
                "timeout_seconds": 12,
            },
        )
    ]


def test_explicit_miyaip_never_accepts_or_falls_back_to_cliproxy_template(monkeypatch):
    monkeypatch.setattr(
        "core.proxy_utils.get_global_dynamic_proxy_template",
        lambda: "socks5://user-region-Rand-sid-seed:pass@cliproxy.example:1080",
    )
    with pytest.raises(RuntimeError, match="不接受 Cliproxy 模板"):
        resolve_probe_candidate_proxies(
            {
                "proxy_mode": "dynamic",
                "dynamic_proxy_provider": "miyaip",
                "proxy": "socks5://user-region-Rand-sid-seed:pass@cliproxy.example:1080",
                "proxy_country_code": "US",
                "miyaip_crc": "crc-value",
                "miyaip_key_name": "key-value",
            }
        )

    monkeypatch.setattr(
        "core.miyaip_proxy.generate_miyaip_proxy",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(MiyaIPError("MiyaIP auth failed")),
    )
    with pytest.raises(RuntimeError, match="MiyaIP auth failed"):
        resolve_probe_candidate_proxies(
            {
                "proxy_mode": "dynamic",
                "dynamic_proxy_provider": "miyaip",
                "proxy_country_code": "US",
                "miyaip_crc": "crc-value",
                "miyaip_key_name": "key-value",
                "dynamic_proxy_probe_enabled": False,
            }
        )


def test_miyaip_failover_generates_only_within_selected_provider(monkeypatch):
    calls = []

    def fake_generate(country_code, **_kwargs):
        calls.append(country_code)
        index = len(calls)
        return replace(
            _resolution(f"http://user:pass@proxy-{index}.miya.test:10000"),
            redacted_proxy_url=f"http://***:***@proxy-{index}.miya.test:10000",
        )

    monkeypatch.setattr("core.miyaip_proxy.generate_miyaip_proxy", fake_generate)
    candidates = resolve_probe_candidate_proxies(
        {
            "proxy_mode": "dynamic",
            "dynamic_proxy_provider": "miyaip",
            "proxy_country_code": "JP",
            "proxy_failover": True,
            "dynamic_proxy_max_attempts": 3,
            "dynamic_proxy_probe_enabled": False,
            "miyaip_crc": "crc-value",
            "miyaip_key_name": "key-value",
        }
    )

    assert len(candidates) == 3
    assert len({candidate[0] for candidate in candidates}) == 3
    assert calls == ["JP", "JP", "JP"]


def test_miyaip_duplicate_line_does_not_end_refresh_budget_early(monkeypatch):
    calls = []

    def fake_generate(country_code, **_kwargs):
        calls.append(country_code)
        host_index = 1 if len(calls) < 3 else 2
        return replace(
            _resolution(f"http://user:pass@proxy-{host_index}.miya.test:10000"),
            redacted_proxy_url=f"http://***:***@proxy-{host_index}.miya.test:10000",
        )

    monkeypatch.setattr("core.miyaip_proxy.generate_miyaip_proxy", fake_generate)
    candidates = resolve_probe_candidate_proxies(
        {
            "proxy_mode": "dynamic",
            "dynamic_proxy_provider": "miyaip",
            "proxy_country_code": "JP",
            "proxy_failover": True,
            "dynamic_proxy_max_attempts": 3,
            "dynamic_proxy_probe_enabled": False,
            "miyaip_crc": "crc-value",
            "miyaip_key_name": "key-value",
        }
    )

    assert [candidate[0] for candidate in candidates] == [
        "http://user:pass@proxy-1.miya.test:10000",
        "http://user:pass@proxy-2.miya.test:10000",
    ]
    assert calls == ["JP", "JP", "JP"]


def test_global_alias_uses_selected_miyaip_provider(monkeypatch):
    calls = []

    def fake_configured_value(key, default=""):
        values = {
            "task_proxy_mode": "dynamic",
            "dynamic_proxy_provider": "miyaip",
        }
        return values.get(key, default)

    def fake_generate(country_code, **kwargs):
        calls.append((country_code, dict(kwargs)))
        return _resolution()

    monkeypatch.setattr("core.proxy_utils._configured_value", fake_configured_value)
    monkeypatch.setattr("core.miyaip_proxy.generate_miyaip_proxy", fake_generate)
    candidates = resolve_probe_candidate_proxies(
        {
            "proxy_mode": "inherit",
            "proxy_country_code": "US",
            "dynamic_proxy_probe_enabled": False,
        },
        default_mode="direct",
    )

    assert candidates[0][0] == "http://user:pass@proxy.miya.test:10000"
    assert len(calls) == 1


def test_explicit_legacy_dynamic_without_provider_stays_on_cliproxy(monkeypatch):
    template = (
        "socks5://acct-region-Rand-sid-oldsid-t-5:secret@"
        "example.cliproxy.io:3010"
    )

    def fake_configured_value(key, default=""):
        values = {
            "task_proxy_mode": "dynamic",
            "dynamic_proxy_provider": "miyaip",
            "dynamic_proxy_template": template,
            "dynamic_proxy_probe_enabled": "false",
        }
        return values.get(key, default)

    monkeypatch.setattr("core.proxy_utils._configured_value", fake_configured_value)
    monkeypatch.setattr(
        "core.miyaip_proxy.generate_miyaip_proxy",
        lambda *_args, **_kwargs: pytest.fail("legacy dynamic must not call MiyaIP"),
    )
    candidates = resolve_probe_candidate_proxies(
        {
            "proxy_mode": "dynamic",
            "proxy_country_code": "US",
            "dynamic_proxy_probe_enabled": False,
        }
    )

    assert len(candidates) == 1
    assert "example.cliproxy.io" in candidates[0][0]
    assert "provider=cliproxy" in candidates[0][2]


def test_local_status_global_alias_freezes_miyaip_runtime():
    from services.chatgpt_core.local_status_proxy import (
        _effective_local_status_proxy_params,
    )

    resolved = _effective_local_status_proxy_params(
        {"proxy_mode": "inherit"},
        {
            "task_proxy_mode": "dynamic",
            "dynamic_proxy_provider": "miyaip",
            "dynamic_proxy_default_country": "US",
            "miyaip_crc": "crc-sensitive-value",
            "miyaip_key_name": "key-sensitive-value",
            "miyaip_pool": "2",
            "miyaip_gateway_server": "as",
            "miyaip_protocol": "socks5",
            "miyaip_request_timeout_seconds": "12",
        },
        default_mode="direct",
    )

    assert resolved["proxy_mode"] == "dynamic"
    assert resolved["dynamic_proxy_provider"] == "miyaip"
    assert resolved["miyaip_crc"] == "crc-sensitive-value"
    assert resolved["miyaip_key_name"] == "key-sensitive-value"
    assert resolved["miyaip_pool"] == "2"
    assert resolved["miyaip_gateway_server"] == "as"
    assert resolved["miyaip_protocol"] == "socks5"
    assert resolved["miyaip_request_timeout_seconds"] == "12"


def test_local_status_global_alias_with_explicit_proxy_becomes_specified():
    from services.chatgpt_core.local_status_proxy import (
        _effective_local_status_proxy_params,
    )

    resolved = _effective_local_status_proxy_params(
        {
            "proxy_mode": "global",
            "proxy": "http://manual-proxy.example:18080",
        },
        {
            "task_proxy_mode": "dynamic",
            "dynamic_proxy_provider": "miyaip",
            "miyaip_crc": "crc-sensitive-value",
            "miyaip_key_name": "key-sensitive-value",
        },
        default_mode="direct",
    )

    assert resolved["proxy_mode"] == "specified"
    assert resolved["proxy"] == "http://manual-proxy.example:18080"
    assert "dynamic_proxy_provider" not in resolved
    assert "miyaip_crc" not in resolved
    assert "miyaip_key_name" not in resolved
