"""Structured browser-cookie compatibility helpers.

Registration browsers expose Playwright cookie dictionaries, while older
accounts only persisted a semicolon-delimited Cookie header.  Keep the two
formats explicit: structured cookies retain their original scope attributes;
header-only values are restored as host-only cookies for chatgpt.com only.
"""

from __future__ import annotations

from http.cookies import SimpleCookie
from typing import Any, Iterable, Mapping


STRUCTURED_COOKIE_FIELD = "chatgpt_browser_cookies"
_COOKIE_ATTRIBUTE_KEYS = (
    "name",
    "value",
    "domain",
    "path",
    "secure",
    "httpOnly",
    "sameSite",
    "expires",
)
_SAME_SITE_VALUES = {"Strict", "Lax", "None"}


def _coerce_cookie_expires(value: Any) -> float | None:
    if value in (None, "", -1, "-1"):
        return None
    try:
        expires = float(value)
    except (TypeError, ValueError):
        return None
    return expires if expires > 0 else None


def _coerce_same_site(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    normalized = {item.lower(): item for item in _SAME_SITE_VALUES}.get(text)
    return normalized


def normalize_structured_cookies(value: Any) -> list[dict[str, Any]]:
    """Return JSON-safe Playwright cookie dictionaries.

    Invalid entries are ignored rather than guessed.  In particular, no
    domain/path is invented for a structured cookie that does not provide one;
    callers can decide whether a URL-scoped compatibility cookie is acceptable.
    """

    if not isinstance(value, (list, tuple)):
        return []
    normalized: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            continue
        name = str(raw.get("name") or "").strip()
        if not name:
            continue
        item: dict[str, Any] = {
            "name": name,
            "value": str(raw.get("value") or ""),
        }
        domain = str(raw.get("domain") or "").strip()
        path = str(raw.get("path") or "").strip()
        if domain:
            item["domain"] = domain
        if path:
            item["path"] = path
        if "secure" in raw:
            item["secure"] = bool(raw.get("secure"))
        if "httpOnly" in raw or "httponly" in raw:
            item["httpOnly"] = bool(raw.get("httpOnly", raw.get("httponly")))
        same_site = _coerce_same_site(raw.get("sameSite", raw.get("samesite")))
        if same_site:
            item["sameSite"] = same_site
        expires = _coerce_cookie_expires(raw.get("expires"))
        if expires is not None:
            item["expires"] = expires
        # Playwright accepts either a domain/path pair or a URL.  Preserve the
        # original scope here and let the browser transport reject incomplete
        # entries instead of silently broadening them.
        normalized.append(item)
    return normalized


def cookie_header_to_host_cookies(
    value: Any,
    *,
    url: str = "https://chatgpt.com/",
) -> list[dict[str, Any]]:
    """Convert a legacy Cookie header to URL-scoped host-only cookies."""

    header = str(value or "").strip()
    if not header:
        return []
    parsed = SimpleCookie()
    try:
        parsed.load(header)
    except Exception:
        parsed = SimpleCookie()
    pairs: list[tuple[str, str]] = []
    if parsed:
        pairs = [(str(key), str(morsel.value)) for key, morsel in parsed.items()]
    if not pairs:
        # SimpleCookie rejects some legal values containing unquoted commas;
        # retain a conservative name/value fallback without interpreting any
        # domain attributes from the header.
        for chunk in header.split(";"):
            name, separator, raw_value = chunk.partition("=")
            name = name.strip()
            if separator and name:
                pairs.append((name, raw_value.strip()))
    return [
        {"name": name, "value": value, "url": url}
        for name, value in pairs
        if name
    ]


def browser_cookie_items(
    account: Any,
    extra: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Resolve account cookies and indicate whether they were structured."""

    values: list[Any] = []
    source = dict(extra or {})
    for key in (STRUCTURED_COOKIE_FIELD, "browser_cookies"):
        if source.get(key):
            values.append(source.get(key))
    account_cookies = getattr(account, "cookies", None)
    if isinstance(account_cookies, (list, tuple)):
        values.append(account_cookies)
    for key in ("cookies", "cookie_header", "cookie"):
        if isinstance(source.get(key), (list, tuple)):
            values.append(source.get(key))
    for candidate in values:
        normalized = normalize_structured_cookies(candidate)
        if normalized:
            return normalized, True

    header_values: list[Any] = []
    if isinstance(account_cookies, str):
        header_values.append(account_cookies)
    for key in ("cookie_header", "cookies", "cookie"):
        if isinstance(source.get(key), str):
            header_values.append(source.get(key))
    for header in header_values:
        converted = cookie_header_to_host_cookies(header)
        if converted:
            return converted, False
    return [], False


def cookie_header_from_items(items: Iterable[Mapping[str, Any]] | None) -> str:
    """Serialize only name/value pairs for legacy protocol compatibility."""

    pairs: list[str] = []
    for item in items or ():
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or "").strip()
        if name:
            pairs.append(f"{name}={item.get('value') or ''}")
    return "; ".join(pairs)


__all__ = [
    "STRUCTURED_COOKIE_FIELD",
    "browser_cookie_items",
    "cookie_header_from_items",
    "cookie_header_to_host_cookies",
    "normalize_structured_cookies",
]
