from unittest import mock

from core.playwright_proxy import (
    _AuthenticatedSocks5ConnectBridge,
    _parse_connect_target,
    playwright_proxy_context,
)


def test_parse_connect_target_supports_host_and_ipv6():
    assert _parse_connect_target("example.com:443") == ("example.com", 443)
    assert _parse_connect_target("[2001:db8::1]:8443") == ("2001:db8::1", 8443)


def test_authenticated_socks5_uses_loopback_connect_bridge_without_credentials():
    logs = []
    bridge = mock.Mock()
    bridge.server_url = "http://127.0.0.1:32123"

    with mock.patch(
        "core.playwright_proxy._AuthenticatedSocks5ConnectBridge",
        return_value=bridge,
    ) as bridge_factory:
        with playwright_proxy_context(
            "socks5h://user:password@proxy.example:443",
            logger=logs.append,
        ) as config:
            assert config == {"server": "http://127.0.0.1:32123"}

    bridge_factory.assert_called_once_with(
        "socks5h://user:password@proxy.example:443"
    )
    bridge.start.assert_called_once_with()
    bridge.close.assert_called_once_with()
    assert all("user" not in item and "password" not in item for item in logs)
    assert any("loopback HTTP CONNECT" in item for item in logs)


def test_authenticated_socks5_bridge_always_uses_remote_dns():
    bridge = _AuthenticatedSocks5ConnectBridge(
        "socks5://user:password@proxy.example:443"
    )

    assert bridge._proxy_rdns is True


def test_unauthenticated_socks5h_is_mapped_to_playwright_socks5():
    with playwright_proxy_context("socks5h://proxy.example:1080") as config:
        assert config == {"server": "socks5://proxy.example:1080"}


def test_http_proxy_keeps_native_playwright_auth_shape():
    with playwright_proxy_context(
        "http://user:password@proxy.example:8080"
    ) as config:
        assert config == {
            "server": "http://proxy.example:8080",
            "username": "user",
            "password": "password",
        }
