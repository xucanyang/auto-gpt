"""Proxy-failure classification shared by ChatGPT local-status probes."""

from __future__ import annotations

import re
from typing import Any, Callable


_HTTP_STATUS_PATTERN = re.compile(
    r"\b(?:"
    r"http(?:/\d+(?:\.\d+)?)?(?:[\s_-]+(?:status|code))?"
    r"|status(?:[\s_-]+code)?"
    r")\s*[:=_-]?\s*(\d{3})\b",
    re.IGNORECASE,
)


def _http_status_codes(error_text: Any) -> set[int]:
    text = str(error_text or "")
    return {int(match.group(1)) for match in _HTTP_STATUS_PATTERN.finditer(text)}


def is_local_status_rate_limit_error(
    error_text: Any,
    *,
    http_status: Any = 0,
) -> bool:
    """Return whether a probe failure is an upstream rate-limit response."""

    try:
        status = int(http_status or 0)
    except (TypeError, ValueError):
        status = 0
    text = str(error_text or "").strip().lower()
    return bool(
        status == 429
        or 429 in _http_status_codes(text)
        or any(
            marker in text
            for marker in (
                "too many requests",
                "rate limit",
                "rate-limit",
                "rate_limited",
                "ratelimit",
                "请求过于频繁",
                "请求频率过高",
                "限流",
            )
        )
    )


def is_local_status_proxy_transport_error(
    error_text: Any,
    *,
    http_status: Any = 0,
) -> bool:
    """Match only failures that justify replacing the current proxy candidate."""

    text = str(error_text or "").strip().lower()
    try:
        status = int(http_status or 0)
    except (TypeError, ValueError):
        status = 0
    text_statuses = _http_status_codes(text)
    # 407 is emitted by the proxy hop itself, not by ChatGPT account policy.
    # It must replace the candidate even though it belongs to the 4xx family.
    if status == 407 or 407 in text_statuses:
        return True
    if not text or is_local_status_rate_limit_error(text, http_status=status):
        return False
    if 400 <= status < 500 or any(400 <= code < 500 for code in text_statuses):
        return False
    return any(
        marker in text
        for marker in (
            "curl: (5)",
            "curl: (6)",
            "curl: (7)",
            "curl: (28)",
            "curl: (35)",
            "curl: (52)",
            "curl: (55)",
            "curl: (56)",
            "curl: (97)",
            "proxy error",
            "proxyerror",
            "proxy connect",
            "proxy tunnel",
            "cannot connect to proxy",
            "failed to connect to proxy",
            "could not resolve proxy",
            "socks5",
            "socksconnection(",
            "sockshttpconnectionpool",
            "network is unreachable",
            "no route to host",
            "could not connect to server",
            "could not resolve host",
            "name resolution",
            "connection timed out",
            "connection timeout",
            "connect timeout",
            "connect timed out",
            "read timeout",
            "read timed out",
            "connection reset",
            "connection aborted",
            "connection refused",
            "failed to connect",
            "remote disconnected",
            "server disconnected",
            "tls handshake",
            "ssl handshake",
            "address type not supported",
        )
    )


def local_status_probe_proxy_failure(probe: Any) -> str:
    """Return the explicit proxy transport error from a structured probe."""

    result = probe if isinstance(probe, dict) else {}
    auth = result.get("auth") if isinstance(result.get("auth"), dict) else {}
    if str(auth.get("state") or "").strip().lower() != "probe_failed":
        return ""
    http_status = auth.get("http_status") or 0
    message = str(auth.get("message") or auth.get("error_code") or "").strip()
    return (
        message
        if is_local_status_proxy_transport_error(message, http_status=http_status)
        else ""
    )


def is_local_status_proxy_configuration_error(error_text: Any) -> bool:
    """Return whether retrying cannot repair the proxy configuration."""

    text = str(error_text or "").strip().lower()
    return any(
        marker in text
        for marker in (
            "动态节点地址为空",
            "动态节点地址解析后为空",
            "必须填写出口国家",
            "代理地址为空",
            "region-xx",
            "country=xx",
            "unsupported country",
            "miyaip crc",
            "miyaip keyname",
            "miyaip 套餐 pool",
            "miyaip 网关区域",
            "miyaip 代理协议",
            "动态代理渠道",
            "不接受 cliproxy 模板",
        )
    )


def _first_value(source: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = source.get(key)
        if value not in (None, ""):
            return value
    return default


def _truthy(value: Any, *, default: bool = False) -> bool:
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


def _configured_value(config_values: dict[str, Any], key: str, default: Any = None) -> Any:
    if key in config_values and config_values.get(key) not in (None, ""):
        return config_values.get(key)
    try:
        from core.config_store import config_store

        value = config_store.get(key, default)
        return default if value is None else value
    except Exception:
        return default


def _effective_local_status_proxy_params(
    params: dict[str, Any],
    config_values: dict[str, Any],
    *,
    default_mode: str,
) -> dict[str, Any]:
    """Freeze one effective proxy configuration for policy and resolution.

    ``core.proxy_utils`` normally resolves omitted fields from the live global
    store. Local-status callers may also provide a platform config snapshot.
    Canonicalizing every field here prevents policy from using the snapshot
    while the resolver silently observes a different global revision.
    """

    frozen = dict(params)
    configured_cache: dict[str, Any] = {}

    def configured(key: str, default: Any = None) -> Any:
        if key not in configured_cache:
            configured_cache[key] = _configured_value(config_values, key, default)
        return configured_cache[key]

    raw_proxy = _first_value(
        frozen,
        "proxy",
        "proxy_url",
        "register_proxy",
        "probe_proxy",
        "dynamic_proxy_template",
        default="",
    )
    raw_mode_text = str(
        _first_value(
            frozen,
            "proxy_mode",
            "register_proxy_mode",
            "probe_proxy_mode",
            default="",
        )
        or ""
    ).strip().lower()
    mode = raw_mode_text
    global_mode_aliases = {
        "global",
        "config",
        "task",
        "task_proxy",
        "default",
        "inherit",
    }
    inherits_global_proxy = raw_mode_text in global_mode_aliases and not raw_proxy
    use_global_default = str(default_mode or "").strip().lower() in {
        "global",
        "config",
        "task",
        "task_proxy",
        "default",
    }
    global_mode = str(configured("task_proxy_mode", "dynamic") or "dynamic").strip().lower()
    if not mode:
        if raw_proxy:
            mode = "specified"
        else:
            mode = global_mode if use_global_default else str(default_mode or "direct").strip().lower()
    if raw_mode_text in global_mode_aliases:
        mode = global_mode if inherits_global_proxy else "specified"
    if mode in {"manual", "explicit"}:
        mode = "specified"
    if mode not in {"direct", "none", "no_proxy", "直连", "specified", "pool", "dynamic"}:
        mode = "specified" if raw_proxy else (global_mode if use_global_default else str(default_mode or "direct"))
    if mode in {"none", "no_proxy", "直连"}:
        mode = "direct"
    if mode not in {"direct", "specified", "pool", "dynamic"}:
        mode = "direct"
    frozen["proxy_mode"] = mode

    effective_global_default = use_global_default or inherits_global_proxy
    if mode == "specified" and not raw_proxy and effective_global_default and global_mode == "specified":
        frozen["proxy_url"] = configured("task_proxy_url", "")

    country_keys = (
        "proxy_country_code",
        "register_proxy_country_code",
        "probe_proxy_country_code",
    )
    if any(key in frozen for key in country_keys):
        for key in country_keys:
            if key in frozen:
                frozen["proxy_country_code"] = frozen.get(key)
                break
    elif mode == "dynamic":
        frozen["proxy_country_code"] = (
            configured("dynamic_proxy_default_country", "")
            or configured("task_proxy_country_code", "")
        )
    elif mode == "pool":
        frozen["proxy_country_code"] = (
            configured("task_proxy_country_code", "") if effective_global_default else ""
        )

    raw_failover = _first_value(
        frozen,
        "proxy_failover",
        "register_proxy_failover",
        "probe_proxy_failover",
        default=(
            configured("task_proxy_failover", False)
            if effective_global_default
            else False
        ),
    )
    frozen["proxy_failover"] = raw_failover

    if mode == "pool":
        frozen["proxy_max_candidates"] = _first_value(
            frozen,
            "proxy_max_candidates",
            "register_proxy_max_candidates",
            "probe_proxy_max_candidates",
            default=(
                configured(
                    "task_proxy_max_candidates",
                    configured("proxy_pool_max_candidates", 5),
                )
                if effective_global_default
                else configured("proxy_pool_max_candidates", 5)
            ),
        )
        frozen["proxy_min_score"] = _first_value(
            frozen,
            "proxy_min_score",
            "register_proxy_min_score",
            "probe_proxy_min_score",
            default=(
                configured(
                    "task_proxy_min_score",
                    configured("proxy_scan_min_score", 50),
                )
                if effective_global_default
                else configured("proxy_scan_min_score", 50)
            ),
        )

    if mode == "dynamic":
        from core.dynamic_proxy import dynamic_proxy_supported
        from core.task_proxy_config import normalize_dynamic_proxy_provider

        raw_provider = frozen.get("dynamic_proxy_provider")
        if raw_provider not in (None, ""):
            provider = normalize_dynamic_proxy_provider(raw_provider)
        elif raw_proxy and dynamic_proxy_supported(raw_proxy):
            provider = "cliproxy"
        elif raw_mode_text and raw_mode_text not in global_mode_aliases:
            provider = "cliproxy"
        else:
            provider = normalize_dynamic_proxy_provider(
                configured("dynamic_proxy_provider", "cliproxy")
            )
        frozen["dynamic_proxy_provider"] = provider

        if provider == "miyaip" and raw_proxy:
            raise RuntimeError("MiyaIP 动态代理不接受 Cliproxy 模板覆盖")
        if provider == "cliproxy" and not raw_proxy:
            template = configured("dynamic_proxy_template", "") or configured(
                "task_proxy_url",
                "",
            )
            if not str(template or "").strip():
                raise RuntimeError("已选择动态代理模式，但动态节点地址为空")
            frozen["dynamic_proxy_template"] = template
        if provider == "miyaip":
            for key, default in (
                ("miyaip_crc", ""),
                ("miyaip_key_name", ""),
                ("miyaip_pool", 1),
                ("miyaip_gateway_server", "us"),
                ("miyaip_protocol", "http"),
                ("miyaip_request_timeout_seconds", 15),
            ):
                if frozen.get(key) in (None, ""):
                    frozen[key] = configured(key, default)
        frozen["dynamic_proxy_max_attempts"] = (
            frozen.get("dynamic_proxy_max_attempts")
            or configured("dynamic_proxy_max_attempts", 5)
        )
        if frozen.get("dynamic_proxy_probe_enabled") in (None, ""):
            frozen["dynamic_proxy_probe_enabled"] = configured(
                "dynamic_proxy_probe_enabled",
                True,
            )
        if frozen.get("dynamic_proxy_require_country_match") in (None, ""):
            frozen["dynamic_proxy_require_country_match"] = configured(
                "dynamic_proxy_require_country_match",
                True,
            )
        if frozen.get("dynamic_proxy_probe_timeout_seconds") in (None, ""):
            frozen["dynamic_proxy_probe_timeout_seconds"] = configured(
                "dynamic_proxy_probe_timeout_seconds",
                8,
            )
        if provider == "cliproxy" and frozen.get("dynamic_proxy_ip_retention_minutes") in (None, ""):
            frozen["dynamic_proxy_ip_retention_minutes"] = configured(
                "dynamic_proxy_ip_retention_minutes",
                5,
            )

    return frozen


def _local_status_dynamic_proxy_policy(params: dict[str, Any]) -> tuple[bool, int]:
    if str(params.get("proxy_mode") or "").strip().lower() != "dynamic":
        return False, 1

    raw_failover = _first_value(
        params,
        "proxy_failover",
        "register_proxy_failover",
        "probe_proxy_failover",
        default=False,
    )
    if not _truthy(raw_failover, default=False):
        return True, 1
    raw_attempts = params.get("dynamic_proxy_max_attempts") or 5
    try:
        attempts = int(float(str(raw_attempts).strip()))
    except (TypeError, ValueError):
        attempts = 5
    return True, max(1, min(attempts, 100))


def run_local_status_probe_with_candidates(
    probe_account: Any,
    params: dict[str, Any] | None,
    probe_fn: Callable[..., dict[str, Any]],
    *,
    default_mode: str = "global",
    candidate_state: dict[str, Any] | None = None,
    config_values: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Probe through static candidates or lazily allocated dynamic lines.

    ``candidate_state`` is intentionally caller-owned so an auth-material retry
    can reuse the last candidate that reached the upstream. The saved candidate
    is discarded only after an explicit proxy transport failure.
    """

    from core.proxy_utils import resolve_probe_candidate_proxies

    resolved_config = dict(config_values or {})
    resolved_params = _effective_local_status_proxy_params(
        dict(params or {}),
        resolved_config,
        default_mode=default_mode,
    )
    state = candidate_state if isinstance(candidate_state, dict) else {}

    def report_candidate(proxy_pool: Any, method: str, proxy_url: str) -> None:
        if proxy_pool is None or not proxy_url:
            return
        reporter = getattr(proxy_pool, method, None)
        if not callable(reporter):
            return
        try:
            reporter(proxy_url)
        except Exception:
            pass

    def candidate_key(candidate: Any) -> str:
        return str((candidate or ("",))[0] or "").strip()

    def clear_reusable_candidate(candidate: Any) -> None:
        reusable = state.get("candidate")
        if reusable is not None and candidate_key(reusable) == candidate_key(candidate):
            state.pop("candidate", None)

    def run_candidate(candidate: tuple[str, Any, str]) -> tuple[dict[str, Any] | None, Exception | None]:
        proxy_url, proxy_pool, _source = candidate
        try:
            probe_result = probe_fn(
                probe_account,
                proxy=proxy_url,
                use_default_proxy=False,
            )
        except Exception as exc:
            if not is_local_status_proxy_transport_error(str(exc)):
                raise
            report_candidate(proxy_pool, "report_fail", proxy_url)
            clear_reusable_candidate(candidate)
            return None, exc

        proxy_error = local_status_probe_proxy_failure(probe_result)
        if proxy_error:
            report_candidate(proxy_pool, "report_fail", proxy_url)
            clear_reusable_candidate(candidate)
            return None, RuntimeError(proxy_error)

        # A structured business/auth/HTTP response proves the selected proxy
        # reached the upstream, even if the account probe itself was rejected.
        report_candidate(proxy_pool, "report_success", proxy_url)
        state["candidate"] = candidate
        return probe_result, None

    is_dynamic, dynamic_attempt_budget = _local_status_dynamic_proxy_policy(resolved_params)
    reusable_candidate = state.get("candidate")
    if is_dynamic:
        attempted_keys: set[str] = set()
        last_error: Exception | None = None
        for candidate_index in range(dynamic_attempt_budget):
            candidate = reusable_candidate if candidate_index == 0 else None
            if candidate is None:
                single_candidate_params = dict(resolved_params)
                single_candidate_params["proxy_failover"] = False
                single_candidate_params["dynamic_proxy_max_attempts"] = 1
                try:
                    candidates = resolve_probe_candidate_proxies(
                        single_candidate_params,
                        fallback_proxy=None,
                        default_mode="direct",
                    )
                except Exception as exc:
                    last_error = exc
                    if is_local_status_proxy_configuration_error(exc):
                        raise
                    continue
                if not candidates:
                    last_error = RuntimeError("本地状态探测没有可用代理候选")
                    continue
                candidate = candidates[0]

            key = candidate_key(candidate)
            if key and key in attempted_keys:
                last_error = RuntimeError("动态代理返回了本账号已尝试的线路")
                continue
            if key:
                attempted_keys.add(key)
            probe_result, transport_error = run_candidate(candidate)
            if probe_result is not None:
                return probe_result
            last_error = transport_error or RuntimeError("代理探测失败")

        if last_error is not None:
            raise last_error
        raise RuntimeError("本地状态探测没有可用代理候选")

    candidates = resolve_probe_candidate_proxies(
        resolved_params,
        fallback_proxy=None,
        default_mode="direct",
    )
    if reusable_candidate is not None:
        reusable_key = candidate_key(reusable_candidate)
        candidates = [reusable_candidate] + [
            candidate
            for candidate in candidates
            if candidate_key(candidate) != reusable_key
        ]
    if not candidates:
        raise RuntimeError("本地状态探测没有可用代理候选")

    last_error: Exception | None = None
    for candidate in candidates:
        probe_result, transport_error = run_candidate(candidate)
        if probe_result is not None:
            return probe_result
        last_error = transport_error or RuntimeError("代理探测失败")
    if last_error is not None:
        raise last_error
    raise RuntimeError("本地状态探测没有可用代理候选")
