"""Restricted urllib opener for configured HTTP(S) integrations."""

from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class UnsafeHttpUrlError(ValueError):
    """Raised when a configured URL is outside the supported HTTP boundary."""


def parse_http_url(value: Any) -> urllib.parse.SplitResult:
    raw = str(value or "").strip()
    if not raw or any(ord(char) < 32 or ord(char) == 127 for char in raw):
        raise UnsafeHttpUrlError("HTTP(S) URL 格式无效")
    try:
        parsed = urllib.parse.urlsplit(raw)
        hostname = parsed.hostname
        username = parsed.username
        password = parsed.password
        parsed.port
    except ValueError as exc:
        raise UnsafeHttpUrlError("HTTP(S) URL 格式无效") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not hostname
        or username is not None
        or password is not None
        or parsed.fragment
    ):
        raise UnsafeHttpUrlError("URL 必须是无凭据、无片段的 HTTP(S) 地址")
    return parsed


def _effective_port(parsed: urllib.parse.SplitResult) -> int:
    if parsed.port is not None:
        return parsed.port
    return 443 if parsed.scheme.lower() == "https" else 80


def _redirect_is_allowed(
    source: urllib.parse.SplitResult,
    target: urllib.parse.SplitResult,
) -> bool:
    source_scheme = source.scheme.lower()
    target_scheme = target.scheme.lower()
    source_host = str(source.hostname or "").lower().rstrip(".")
    target_host = str(target.hostname or "").lower().rstrip(".")
    if not source_host or source_host != target_host:
        return False
    if source_scheme == target_scheme:
        return _effective_port(source) == _effective_port(target)
    return (
        source_scheme == "http"
        and target_scheme == "https"
        and _effective_port(source) == 80
        and _effective_port(target) == 443
    )


class RestrictedHttpRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject redirects that could leak credentials or escape HTTP(S)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            source = parse_http_url(req.full_url)
            target = parse_http_url(urllib.parse.urljoin(req.full_url, newurl))
        except UnsafeHttpUrlError as exc:
            raise urllib.error.HTTPError(
                newurl,
                code,
                "unsafe HTTP redirect blocked",
                headers,
                fp,
            ) from exc
        if not _redirect_is_allowed(source, target):
            raise urllib.error.HTTPError(
                newurl,
                code,
                "cross-origin or downgraded HTTP redirect blocked",
                headers,
                fp,
            )
        return super().redirect_request(req, fp, code, msg, headers, target.geturl())


def open_http_url(request: urllib.request.Request, *, timeout: float):
    """Open an HTTP(S) request with redirect validation on every hop."""

    parse_http_url(request.full_url)
    opener = urllib.request.build_opener(RestrictedHttpRedirectHandler())
    return opener.open(request, timeout=timeout)
