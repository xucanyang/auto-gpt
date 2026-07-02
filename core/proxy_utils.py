from __future__ import annotations

import ipaddress
from typing import Optional
from urllib.parse import unquote, urlsplit


def normalize_proxy_url(proxy_url: Optional[str]) -> Optional[str]:
    """仅做基础清洗，保留原始代理 scheme。"""
    if proxy_url is None:
        return None

    value = str(proxy_url).strip()
    if not value:
        return None
    if value.startswith("socks5://") and "cliproxy.io" in value:
        value = value.replace("socks5://", "http://", 1)
    return value


def resolve_runtime_proxy(proxy_url: Optional[str] = None) -> str:
    resolved, _, _ = resolve_runtime_proxy_with_metadata(proxy_url)
    return resolved


def iter_enabled_runtime_proxies(proxy_url: Optional[str] = None) -> list[str]:
    try:
        from .proxy_pool import proxy_pool

        filtered = proxy_pool.get_candidate_records()
        results: list[str] = []
        seen: set[str] = set()
        for item in filtered:
            value = normalize_proxy_url(item.get("url", "") if isinstance(item, dict) else getattr(item, "url", ""))
            if not value or value in seen:
                continue
            seen.add(value)
            results.append(value)
        if results:
            return results
    except Exception:
        pass
    return []


def resolve_runtime_proxy_with_metadata(proxy_url: Optional[str] = None) -> tuple[str, object | None, str]:
    direct = normalize_proxy_url(proxy_url)
    if direct:
        return direct, None, "explicit"
    try:
        from .proxy_pool import proxy_pool

        pooled = normalize_proxy_url(proxy_pool.get_next())
        if pooled:
            return pooled, proxy_pool, "pool"
    except Exception:
        pass
    return "", None, "direct"


def build_requests_proxy_config(proxy_url: Optional[str]) -> Optional[dict[str, str]]:
    if not proxy_url:
        return None
    return {"http": proxy_url, "https": proxy_url}


def build_playwright_proxy_config(proxy_url: Optional[str]) -> Optional[dict[str, str]]:
    if not proxy_url:
        return None

    parts = urlsplit(proxy_url)
    if not parts.scheme or not parts.hostname or parts.port is None:
        return {"server": proxy_url}

    host = parts.hostname
    try:
        if host and ipaddress.ip_address(host).version == 6:
            host = f"[{host}]"
    except ValueError:
        pass

    config = {"server": f"{parts.scheme}://{host}:{parts.port}"}
    if parts.username:
        config["username"] = unquote(parts.username)
    if parts.password:
        config["password"] = unquote(parts.password)
    return config


def is_proxy_error_text(error_text: Optional[str]) -> bool:
    text = str(error_text or "").strip().lower()
    if not text:
        return False
    account_state_markers = (
        "account_deactivated",
        "account_deleted",
        "account has been deleted or deactivated",
        "you do not have an account because it has been deleted or deactivated",
        "deleted or deactivated",
    )
    if any(marker in text for marker in account_state_markers):
        return False
    markers = (
        "curl: (7)",
        "curl: (28)",
        "curl: (35)",
        "curl: (52)",
        "curl: (56)",
        "curl: (97)",
        "network is unreachable",
        "could not connect to server",
        "connection timed out",
        "timed out",
        "timeout",
        "tls",
        "ssl",
        "handshake",
        "proxy",
        "socks5",
        "connection reset",
        "connection aborted",
        "connection refused",
        "failed to connect",
        "sockshttpconnectionpool",
        "socksconnection(",
        "address type not supported",
        "status=403 final_url=https://chatgpt.com/",
        "http 403",
        "http 429",
        "authorize 失败",
        "预授权被拦截",
        "访问首页失败",
        "获取 csrf token 失败",
    )
    return any(marker in text for marker in markers)
