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


def _param_first(params: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = params.get(key)
        if value not in (None, ""):
            return value
    return default


def _truthy(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "y", "是", "开启", "启用"}:
        return True
    if text in {"0", "false", "no", "off", "n", "否", "关闭", "禁用"}:
        return False
    return default


def _positive_int(value: Any, default: int, minimum: int = 1, maximum: int = 100) -> int:
    try:
        parsed = int(float(str(value or "").strip()))
    except Exception:
        parsed = default
    return max(minimum, min(maximum, parsed))


def _positive_float(value: Any, default: float, minimum: float = 0, maximum: float = 100) -> float:
    try:
        parsed = float(str(value or "").strip())
    except Exception:
        parsed = default
    return max(minimum, min(maximum, parsed))


def _configured_value(key: str, default: Any = "") -> Any:
    try:
        from .config_store import config_store

        value = config_store.get(key, default)
        return default if value is None else value
    except Exception:
        return default


def _safe_source_text(value: Any, limit: int = 240) -> str:
    text = str(value or "")
    try:
        from services.chatgpt_core.task_logging import redact_log_text

        text = redact_log_text(text)
    except Exception:
        if "://" in text and "@" in text:
            import re

            text = re.sub(r"([a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@", r"\1***:***@", text, flags=re.I)
    if len(text) > limit:
        return f"{text[: max(0, limit - 40)]}...{text[-30:]}"
    return text


def _pool_candidate_tuples(
    *,
    target: str,
    country_code: str,
    limit: int,
    min_score: float,
    existing: list[tuple[str, Any, str]] | None = None,
) -> list[tuple[str, Any, str]]:
    from .proxy_pool import proxy_pool

    candidates: list[tuple[str, Any, str]] = []
    existing = existing or []
    pool_candidates = proxy_pool.get_candidate_records(
        target=target,
        country_code=country_code,
        limit=limit,
        min_score=min_score,
    )
    for candidate in pool_candidates:
        url = normalize_proxy_url(candidate.get("url") if isinstance(candidate, dict) else getattr(candidate, "url", "")) or ""
        if not url or any(item[0] == url for item in existing) or any(item[0] == url for item in candidates):
            continue
        country = str((candidate.get("exit_country_code") if isinstance(candidate, dict) else getattr(candidate, "exit_country_code", "")) or "unknown").strip() or "unknown"
        exit_ip = str((candidate.get("exit_ip") if isinstance(candidate, dict) else getattr(candidate, "exit_ip", "")) or "").strip()
        score = candidate.get("health_score") if isinstance(candidate, dict) else getattr(candidate, "health_score", None)
        latency = int((candidate.get("latency_ms") if isinstance(candidate, dict) else getattr(candidate, "latency_ms", 0)) or 0)
        exit_ip_str = f" exit_ip={exit_ip}" if exit_ip else ""
        source = f"pool country={country}{exit_ip_str} score={score} latency={latency}ms"
        candidates.append((url, proxy_pool, source))
    return candidates


def _dynamic_probe_source(
    proxy_url: str,
    *,
    expected_country: str,
    provider: str,
    sid_refreshed: bool,
    timeout_seconds: int,
    require_country_match: bool,
) -> tuple[bool, str]:
    from services.proxy_scanner import scan_proxy_url

    summary = scan_proxy_url(
        proxy_url,
        targets=["basic", "geo"],
        timeout_seconds=timeout_seconds,
        refresh_geo=True,
    )
    basic = summary.get("basic") if isinstance(summary.get("basic"), dict) else {}
    geo = summary.get("geo") if isinstance(summary.get("geo"), dict) else {}
    exit_ip = str((basic or {}).get("exit_ip") or "").strip()
    actual_country = str((geo or {}).get("country_code") or "").strip().upper()
    sid_text = "refreshed" if sid_refreshed else "unchanged"
    if not (basic or {}).get("ok"):
        error = _safe_source_text((basic or {}).get("error") or (basic or {}).get("error_code") or "基础连通性检测失败", limit=200)
        return (not require_country_match), f"dynamic country={expected_country} actual=unknown provider={provider} sid={sid_text} probe=failed error={error}"
    if require_country_match and actual_country != expected_country:
        actual_text = actual_country or "unknown"
        ip_text = f" exit_ip={exit_ip}" if exit_ip else ""
        return False, f"dynamic country={expected_country} actual={actual_text}{ip_text} provider={provider} sid={sid_text} country_mismatch"
    actual_text = actual_country or "unknown"
    ip_text = f" exit_ip={exit_ip}" if exit_ip else ""
    return True, f"dynamic country={expected_country} actual={actual_text}{ip_text} provider={provider} sid={sid_text} probe=ok"


def _dynamic_candidate_tuples(
    *,
    template: str,
    country_code: str,
    max_candidates: int,
    failover: bool,
    probe_enabled: bool,
    require_country_match: bool,
    timeout_seconds: int,
) -> list[tuple[str, Any, str]]:
    from .dynamic_proxy import resolve_dynamic_proxy_template

    if not template:
        raise RuntimeError("已选择动态代理模式，但代理模板为空")
    if not country_code:
        raise RuntimeError("动态代理模式必须填写出口国家")

    desired_count = max_candidates if failover else 1
    desired_count = max(1, min(100, int(desired_count or 1)))
    candidates: list[tuple[str, Any, str]] = []
    errors: list[str] = []
    seen: set[str] = set()

    for _ in range(desired_count):
        try:
            resolved = resolve_dynamic_proxy_template(template, country_code, refresh_sid=True)
            runtime_proxy = normalize_proxy_url(resolved.proxy_url) or ""
            if not runtime_proxy:
                raise RuntimeError("动态代理模板解析后为空")
            if runtime_proxy in seen:
                if not resolved.sid_refreshed:
                    break
                continue
            seen.add(runtime_proxy)
            if probe_enabled:
                ok, source = _dynamic_probe_source(
                    runtime_proxy,
                    expected_country=resolved.requested_country_code,
                    provider=resolved.provider,
                    sid_refreshed=resolved.sid_refreshed,
                    timeout_seconds=timeout_seconds,
                    require_country_match=require_country_match,
                )
                if not ok:
                    errors.append(source)
                    continue
            else:
                sid_text = "refreshed" if resolved.sid_refreshed else "unchanged"
                source = f"dynamic country={resolved.requested_country_code} actual=unverified provider={resolved.provider} sid={sid_text} probe=disabled"
            candidates.append((runtime_proxy, None, source))
        except Exception as exc:
            errors.append(_safe_source_text(exc))
            if not failover:
                break

    if not candidates:
        detail = "; ".join(err for err in errors if err)[:500]
        raise RuntimeError(f"动态代理没有可用候选：country={country_code}{' ' + detail if detail else ''}")
    return candidates


def resolve_task_proxy_candidates(
    params: Optional[dict] = None,
    fallback_proxy: Optional[str] = None,
    default_mode: str = "direct",
    *,
    target: str = "chatgpt",
) -> list[tuple[str, Any, str]]:
    """统一解析任务级代理候选，支持 direct / specified / pool / dynamic。"""
    if params is None or not isinstance(params, dict):
        params = {}

    raw_proxy_value = _param_first(
        params,
        "proxy",
        "proxy_url",
        "register_proxy",
        "probe_proxy",
        "dynamic_proxy_template",
        default=fallback_proxy,
    )
    raw_proxy = str(raw_proxy_value or "").strip()
    explicit_proxy = normalize_proxy_url(raw_proxy) or ""
    mode = str(
        _param_first(
            params,
            "proxy_mode",
            "register_proxy_mode",
            "probe_proxy_mode",
            default="",
        )
        or ""
    ).strip().lower()

    if not mode:
        mode = "specified" if raw_proxy else default_mode

    if mode in {"none", "no_proxy", "direct", "直连"}:
        return [("", None, "direct")]
    if mode in {"manual", "explicit"}:
        mode = "specified"
    if mode not in {"specified", "pool", "dynamic"}:
        mode = "specified" if explicit_proxy else default_mode
    if mode == "direct":
        return [("", None, "direct")]

    country_code = str(
        _param_first(
            params,
            "proxy_country_code",
            "register_proxy_country_code",
            "probe_proxy_country_code",
            default="",
        )
        or ""
    ).strip().upper()
    if mode == "dynamic" and not country_code:
        country_code = str(_configured_value("dynamic_proxy_default_country", "JP") or "JP").strip().upper()

    raw_failover = _param_first(
        params,
        "proxy_failover",
        "register_proxy_failover",
        "probe_proxy_failover",
        default=None,
    )
    failover = _truthy(raw_failover, default=False)

    pool_max_candidates = _positive_int(
        _param_first(
            params,
            "proxy_max_candidates",
            "register_proxy_max_candidates",
            "probe_proxy_max_candidates",
            default=_configured_value("proxy_pool_max_candidates", "5"),
        ),
        default=5,
        minimum=1,
        maximum=100,
    )
    min_score = _positive_float(
        _param_first(
            params,
            "proxy_min_score",
            "register_proxy_min_score",
            "probe_proxy_min_score",
            default=_configured_value("proxy_scan_min_score", "50"),
        ),
        default=50,
        minimum=0,
        maximum=100,
    )

    candidates: list[tuple[str, Any, str]] = []
    if mode == "specified":
        if not explicit_proxy:
            raise RuntimeError("已选择指定代理模式，但代理地址为空")
        candidates.append((explicit_proxy, None, "specified"))
        if not failover:
            return candidates

    if mode == "dynamic":
        template = raw_proxy or str(_configured_value("dynamic_proxy_template", "") or "").strip()
        dynamic_max_attempts = _positive_int(
            params.get("dynamic_proxy_max_attempts") or _configured_value("dynamic_proxy_max_attempts", "5"),
            default=5,
            minimum=1,
            maximum=100,
        )
        probe_enabled = _truthy(
            params.get("dynamic_proxy_probe_enabled"),
            default=_truthy(_configured_value("dynamic_proxy_probe_enabled", "true"), default=True),
        )
        require_country_match = _truthy(
            params.get("dynamic_proxy_require_country_match"),
            default=_truthy(_configured_value("dynamic_proxy_require_country_match", "true"), default=True),
        )
        timeout_seconds = _positive_int(
            params.get("dynamic_proxy_probe_timeout_seconds") or _configured_value("dynamic_proxy_probe_timeout_seconds", "8"),
            default=8,
            minimum=2,
            maximum=60,
        )
        return _dynamic_candidate_tuples(
            template=template,
            country_code=country_code,
            max_candidates=dynamic_max_attempts,
            failover=failover,
            probe_enabled=probe_enabled,
            require_country_match=require_country_match,
            timeout_seconds=timeout_seconds,
        )

    try:
        candidates.extend(
            _pool_candidate_tuples(
                target=target,
                country_code=country_code,
                limit=pool_max_candidates,
                min_score=min_score,
                existing=candidates,
            )
        )
        if mode == "pool" and not candidates:
            country_text = country_code or "不限"
            raise RuntimeError(f"代理池没有可用候选：target={target} country={country_text} min_score={min_score:g}")
    except Exception:
        if mode == "pool" and not candidates:
            raise
    return candidates or [("", None, "direct")]


def resolve_probe_candidate_proxies(
    params: Optional[dict] = None,
    fallback_proxy: Optional[str] = None,
    default_mode: str = "direct",
) -> list[tuple[str, Any, str]]:
    """为批量本地状态探测（或其他 Action）解析候选代理列表，支持参考注册的代理配置语义。"""
    return resolve_task_proxy_candidates(
        params,
        fallback_proxy=fallback_proxy,
        default_mode=default_mode,
        target="chatgpt",
    )


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
