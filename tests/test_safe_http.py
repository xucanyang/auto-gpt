from __future__ import annotations

import urllib.error
import urllib.request

import pytest

from core.safe_http import (
    RestrictedHttpRedirectHandler,
    UnsafeHttpUrlError,
    parse_http_url,
)


def _redirect(source: str, target: str):
    return RestrictedHttpRedirectHandler().redirect_request(
        urllib.request.Request(source),
        None,
        302,
        "Found",
        {},
        target,
    )


@pytest.mark.parametrize(
    "value",
    (
        "file:///tmp/secret",
        "ftp://example.test/archive",
        "https://user:password@example.test/api",
        "https://example.test/api#fragment",
        "https://example.test/api\nX-Test: injected",
    ),
)
def test_http_url_parser_rejects_unsafe_schemes_credentials_and_controls(value):
    with pytest.raises(UnsafeHttpUrlError):
        parse_http_url(value)


def test_http_redirect_policy_allows_same_origin_and_safe_https_upgrade():
    same_origin = _redirect(
        "https://api.example.test/v1",
        "/v2?cursor=1",
    )
    assert same_origin.full_url == "https://api.example.test/v2?cursor=1"

    upgraded = _redirect(
        "http://api.example.test/v1",
        "https://api.example.test/v2",
    )
    assert upgraded.full_url == "https://api.example.test/v2"


@pytest.mark.parametrize(
    "target",
    (
        "https://other.example.test/v2",
        "http://api.example.test/v2",
        "ftp://api.example.test/v2",
        "https://user:password@api.example.test/v2",
        "https://api.example.test:8443/v2",
    ),
)
def test_http_redirect_policy_blocks_cross_origin_downgrade_and_credentials(target):
    with pytest.raises(urllib.error.HTTPError):
        _redirect("https://api.example.test/v1", target)
