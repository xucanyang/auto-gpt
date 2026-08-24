from __future__ import annotations

from types import SimpleNamespace

from services.chatgpt_core.browser_cookies import (
    STRUCTURED_COOKIE_FIELD,
    browser_cookie_items,
    cookie_header_to_host_cookies,
    normalize_structured_cookies,
)


def test_structured_cookie_normalization_preserves_scope_and_security_fields():
    items = normalize_structured_cookies(
        [
            {
                "name": "session",
                "value": "value",
                "domain": ".chatgpt.com",
                "path": "/",
                "secure": True,
                "httpOnly": True,
                "sameSite": "lax",
                "expires": 1_725_000_000,
            },
            {"value": "missing-name"},
            "not-a-cookie",
        ]
    )

    assert items == [
        {
            "name": "session",
            "value": "value",
            "domain": ".chatgpt.com",
            "path": "/",
            "secure": True,
            "httpOnly": True,
            "sameSite": "Lax",
            "expires": 1_725_000_000.0,
        }
    ]


def test_legacy_cookie_header_is_host_only_and_does_not_invent_domain_scope():
    cookies = cookie_header_to_host_cookies(
        "__Secure-next-auth.session-token=abc; oai-did=device-1"
    )

    assert cookies == [
        {
            "name": "__Secure-next-auth.session-token",
            "value": "abc",
            "url": "https://chatgpt.com/",
        },
        {"name": "oai-did", "value": "device-1", "url": "https://chatgpt.com/"},
    ]
    assert all("domain" not in item and "path" not in item for item in cookies)


def test_account_cookie_resolution_prefers_structured_material():
    structured = [
        {
            "name": "session",
            "value": "structured",
            "domain": ".chatgpt.com",
            "path": "/",
        }
    ]
    account = SimpleNamespace(cookies="session=legacy")
    items, is_structured = browser_cookie_items(
        account,
        {STRUCTURED_COOKIE_FIELD: structured, "cookie_header": "session=legacy"},
    )

    assert is_structured is True
    assert items == structured
