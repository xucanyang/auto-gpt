from __future__ import annotations

import ipaddress
from typing import Any, Optional
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


def resolve_probe_candidate_proxies(
    params: Optional[dict] = None,
    fallback_proxy: Optional[str] = None,
    default_mode: str = "direct",
) -> list[tuple[str, Any, str]]:
    """为批量本地状态探测（或其他 Action）解析候选代理列表，支持参考注册的代理配置语义。"""
    if params is None or not isinstance(params, dict):
        params = {}

    explicit_proxy = normalize_proxy_url(
        params.get("proxy")
        or params.get("proxy_url")
        or params.get("register_proxy")
        or params.get("probe_proxy")
        or fallback_proxy
    ) or ""
    mode = str(
        params.get("proxy_mode")
        or params.get("register_proxy_mode")
        or params.get("probe_proxy_mode")
        or ""
    ).strip().lower()

    if not mode:
        mode = "specified" if params.get("proxy") or params.get("proxy_url") or params.get("register_proxy") or params.get("probe_proxy") else default_mode

    if mode in {"none", "no_proxy", "direct", "直连"}:
        return [("", None, "direct")]

    if mode in {"manual", "explicit"}:
        mode = "specified"
    if mode not in {"specified", "pool"}:
        mode = "specified" if explicit_proxy else default_mode
    if mode == "direct":
        return [("", None, "direct")]

    country_code = str(
        params.get("proxy_country_code")
        or params.get("register_proxy_country_code")
        or params.get("probe_proxy_country_code")
        or ""
    ).strip().upper()

    raw_failover = params.get("proxy_failover")
    if raw_failover is None:
        raw_failover = params.get("register_proxy_failover")
    if raw_failover is None:
        raw_failover = params.get("probe_proxy_failover")

    failover = False
    if raw_failover is not None:
        if isinstance(raw_failover, bool):
            failover = raw_failover
        else:
            text = str(raw_failover).strip().lower()
            failover = text in {"1", "true", "yes", "on"}

    try:
        max_candidates = int(
            float(
                params.get("proxy_max_candidates")
                or params.get("register_proxy_max_candidates")
                or params.get("probe_proxy_max_candidates")
                or 5
            )
        )
    except Exception:
        max_candidates = 5
    max_candidates = max(1, min(100, max_candidates))

    try:
        min_score = float(
            params.get("proxy_min_score")
            or params.get("register_proxy_min_score")
            or params.get("probe_proxy_min_score")
            or 50
        )
    except Exception:
        min_score = 50.0
    min_score = max(0.0, min(100.0, min_score))

    candidates: list[tuple[str, Any, str]] = []
    if mode == "specified":
        if not explicit_proxy:
            raise RuntimeError("已选择指定代理模式，但代理地址为空")
        
        source = "specified"
        try:
            from sqlmodel import Session, select
            from .db import engine, ProxyModel
            with Session(engine) as session:
                proxy_record = session.exec(select(ProxyModel).where(ProxyModel.url == explicit_proxy)).first()
                if proxy_record:
                    country = str(getattr(proxy_record, "exit_country_code", "") or "unknown").strip() or "unknown"
                    exit_ip = str(getattr(proxy_record, "exit_ip", "") or "").strip()
                    exit_ip_str = f" exit_ip={exit_ip}" if exit_ip else ""
                    source = f"specified country={country}{exit_ip_str}"
        except Exception:
            pass
            
        candidates.append((explicit_proxy, None, source))
        if not failover:
            return candidates

    try:
        from .proxy_pool import proxy_pool

        pool_candidates = proxy_pool.get_candidate_records(
            target="chatgpt",
            country_code=country_code,
            limit=max_candidates,
            min_score=min_score,
        )
        for candidate in pool_candidates:
            url = normalize_proxy_url(candidate.get("url") if isinstance(candidate, dict) else getattr(candidate, "url", "")) or ""
            if not url or any(existing[0] == url for existing in candidates):
                continue
            country = str((candidate.get("exit_country_code") if isinstance(candidate, dict) else getattr(candidate, "exit_country_code", "")) or "unknown").strip() or "unknown"
            exit_ip = str((candidate.get("exit_ip") if isinstance(candidate, dict) else getattr(candidate, "exit_ip", "")) or "").strip()
            score = candidate.get("health_score") if isinstance(candidate, dict) else getattr(candidate, "health_score", None)
            latency = int((candidate.get("latency_ms") if isinstance(candidate, dict) else getattr(candidate, "latency_ms", 0)) or 0)
            exit_ip_str = f" exit_ip={exit_ip}" if exit_ip else ""
            source = f"pool country={country}{exit_ip_str} score={score} latency={latency}ms"
            candidates.append((url, proxy_pool, source))
        if mode == "pool" and not candidates:
            country_text = country_code or "不限"
            raise RuntimeError(f"代理池没有可用候选：target=chatgpt country={country_text} min_score={min_score:g}")
    except Exception as exc:
        if mode == "pool" and not candidates:
            raise
    return candidates or [("", None, "direct")]



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
