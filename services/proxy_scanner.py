"""Proxy scanning, exit-IP discovery, target reachability and in-memory scan jobs."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import ipaddress
import json
import threading
import time
import uuid
from typing import Any, Callable, Iterable
from urllib.parse import unquote, urlsplit

import requests
try:
    from curl_cffi import requests as cffi_requests
except Exception:  # pragma: no cover - curl_cffi is optional for unit tests/local tooling
    cffi_requests = None
from requests import Response
from requests import exceptions as requests_exc
from sqlmodel import Session, select

from core.config_store import config_store
from core.db import ProxyModel, engine
from core.proxy_utils import build_requests_proxy_config, normalize_proxy_url
from core.task_runtime import TaskInterruption

BASIC_TARGETS = (
    "https://api.ipify.org?format=json",
    "https://ifconfig.co/json",
    "https://httpbin.org/ip",
)
GEO_TARGETS = (
    "https://ipapi.co/{ip}/json/",
    "https://ipinfo.io/{ip}/json",
)
CLOUDFLARE_TRACE_TARGET = "https://www.cloudflare.com/cdn-cgi/trace"
CHATGPT_TARGET = "https://chatgpt.com/"
AUTH_OPENAI_TARGET = "https://auth.openai.com/"
CFFI_CHATGPT_TARGETS = (CHATGPT_TARGET, AUTH_OPENAI_TARGET)
CFFI_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
DEFAULT_TIMEOUT_SECONDS = 8
DEFAULT_CONCURRENCY = 8
DEFAULT_MAX_RECENT_RESULTS = 200
REGISTRATION_PROBE_ATTEMPTS = 3
BASIC_FAILURE_COOLDOWN_SECONDS = 5 * 60
CHATGPT_FAILURE_COOLDOWN_SECONDS = 15 * 60
PROXY_LEVEL_ERROR_CODES = {
    "invalid_url",
    "proxy_auth_failed",
    "proxy_error",
    "connection_refused",
    "connection_error",
    "connect_timeout",
    "timeout",
    "tls_error",
}


def _run_stop_checker(stop_checker: Callable[[], None] | None) -> None:
    if callable(stop_checker):
        stop_checker()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None) -> str:
    if not value:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_bool(value: Any, default: bool = False) -> bool:
    text = str(value if value is not None else "").strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def parse_int(value: Any, default: int, minimum: int = 0, maximum: int | None = None) -> int:
    try:
        parsed = int(float(str(value or "").strip()))
    except Exception:
        parsed = default
    parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


def normalize_targets(targets: Iterable[str] | None) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    aliases = {
        "quick": ["basic", "geo"],
        "full": ["basic", "geo", "chatgpt"],
        "exit": ["basic", "geo"],
        "homepage": ["chatgpt"],
        "register": ["basic", "geo", "chatgpt"],
        "cffi": ["chatgpt"],
    }
    raw_targets = list(targets or ["basic", "geo"])
    for raw in raw_targets:
        key = str(raw or "").strip().lower()
        expanded = aliases.get(key, [key])
        for item in expanded:
            if item not in {"basic", "geo", "chatgpt"}:
                continue
            if item == "geo" and "basic" not in seen:
                normalized.append("basic")
                seen.add("basic")
            if item not in seen:
                normalized.append(item)
                seen.add(item)
    if not normalized:
        normalized = ["basic", "geo"]
    return normalized


def mask_proxy_url(proxy_url: str | None) -> str:
    value = normalize_proxy_url(proxy_url) or ""
    if not value:
        return "direct"
    try:
        parts = urlsplit(value)
    except Exception:
        return value[:80] + ("..." if len(value) > 80 else "")
    if not parts.scheme or not parts.netloc:
        return value[:80] + ("..." if len(value) > 80 else "")
    host = parts.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = f":{parts.port}" if parts.port else ""
    auth = "***:***@" if parts.username or parts.password else ""
    return f"{parts.scheme}://{auth}{host}{port}"


def parse_proxy_endpoint(proxy_url: str | None) -> dict[str, Any]:
    value = normalize_proxy_url(proxy_url) or ""
    if not value:
        return {"scheme": "", "host": "", "port": 0}
    try:
        parts = urlsplit(value)
    except Exception:
        return {"scheme": "", "host": "", "port": 0}
    return {
        "scheme": str(parts.scheme or ""),
        "host": str(parts.hostname or ""),
        "port": int(parts.port or 0),
    }


def _validate_proxy_url(proxy_url: str | None) -> tuple[bool, str]:
    value = normalize_proxy_url(proxy_url) or ""
    if not value:
        return False, "代理地址为空"
    try:
        parts = urlsplit(value)
    except Exception as exc:
        return False, f"代理地址解析失败: {exc}"
    if not parts.scheme:
        return False, "代理地址缺少协议，例如 http:// 或 socks5://"
    if parts.scheme.lower() not in {"http", "https", "socks4", "socks4a", "socks5", "socks5h"}:
        return False, f"不支持的代理协议: {parts.scheme}"
    if not parts.hostname or not parts.port:
        return False, "代理地址缺少 host 或端口"
    return True, ""


def classify_error(exc: BaseException | str | None = None, *, status_code: int = 0) -> str:
    if status_code:
        if status_code == 403:
            return "http_403"
        if status_code == 429:
            return "http_429"
        if status_code >= 500:
            return f"http_{status_code}"
        if status_code >= 400:
            return f"http_{status_code}"
    if exc is None:
        return "unknown"
    if isinstance(exc, requests_exc.ConnectTimeout):
        return "connect_timeout"
    if isinstance(exc, requests_exc.ReadTimeout):
        return "read_timeout"
    if isinstance(exc, requests_exc.Timeout):
        return "timeout"
    if isinstance(exc, requests_exc.ProxyError):
        text = str(exc).lower()
        if "authentication" in text or "407" in text or "auth" in text:
            return "proxy_auth_failed"
        return "proxy_error"
    if isinstance(exc, requests_exc.SSLError):
        return "tls_error"
    if isinstance(exc, requests_exc.ConnectionError):
        text = str(exc).lower()
        if "name resolution" in text or "dns" in text or "temporary failure in name resolution" in text:
            return "dns_error"
        if "refused" in text:
            return "connection_refused"
        return "connection_error"
    text = str(exc or "").strip().lower()
    if not text:
        return "unknown"
    if "invalid" in text and "proxy" in text:
        return "invalid_url"
    if "timed out" in text or "timeout" in text:
        return "timeout"
    if "407" in text or "authentication" in text or "proxy auth" in text:
        return "proxy_auth_failed"
    if "ssl" in text or "tls" in text or "handshake" in text:
        return "tls_error"
    if "name resolution" in text or "dns" in text:
        return "dns_error"
    if "refused" in text:
        return "connection_refused"
    if "proxy" in text or "socks" in text:
        return "proxy_error"
    return "unknown"


def _elapsed_ms(start: float) -> int:
    return max(0, int((time.perf_counter() - start) * 1000))


def _safe_json(response: Response) -> dict[str, Any]:
    try:
        data = response.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _extract_ip(data: dict[str, Any], text: str = "") -> str:
    candidates = [data.get("ip"), data.get("origin"), data.get("query"), data.get("remote_addr")]
    for raw in candidates:
        value = str(raw or "").split(",", 1)[0].strip()
        if not value:
            continue
        try:
            ipaddress.ip_address(value)
            return value
        except ValueError:
            continue
    for raw in str(text or "").replace("\n", " ").split():
        value = raw.strip().strip('"{},')
        try:
            ipaddress.ip_address(value)
            return value
        except ValueError:
            continue
    return ""


def _request_via_proxy(url: str, proxy_url: str, *, timeout_seconds: int) -> dict[str, Any]:
    start = time.perf_counter()
    try:
        response = requests.get(
            url,
            proxies=build_requests_proxy_config(proxy_url),
            timeout=timeout_seconds,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                "Accept": "application/json,text/html,*/*",
            },
            allow_redirects=True,
        )
        latency_ms = _elapsed_ms(start)
        status_code = int(response.status_code or 0)
        if status_code >= 400:
            return {
                "ok": False,
                "status_code": status_code,
                "latency_ms": latency_ms,
                "error_code": classify_error(status_code=status_code),
                "error": f"HTTP {status_code}",
                "body": response.text[:300],
            }
        return {
            "ok": True,
            "status_code": status_code,
            "latency_ms": latency_ms,
            "json": _safe_json(response),
            "text": response.text[:1000],
        }
    except Exception as exc:
        return {
            "ok": False,
            "status_code": 0,
            "latency_ms": _elapsed_ms(start),
            "error_code": classify_error(exc),
            "error": str(exc)[:500],
        }


def probe_basic(
    proxy_url: str,
    *,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    stop_checker: Callable[[], None] | None = None,
) -> dict[str, Any]:
    _run_stop_checker(stop_checker)
    valid, message = _validate_proxy_url(proxy_url)
    if not valid:
        return {
            "ok": False,
            "status": "failed",
            "error_code": "invalid_url",
            "error": message,
            "latency_ms": 0,
            "exit_ip": "",
            "target": "",
        }

    failures: list[dict[str, Any]] = []
    for target in BASIC_TARGETS:
        _run_stop_checker(stop_checker)
        result = _request_via_proxy(target, proxy_url, timeout_seconds=timeout_seconds)
        _run_stop_checker(stop_checker)
        if result.get("ok"):
            exit_ip = _extract_ip(result.get("json") or {}, str(result.get("text") or ""))
            if exit_ip:
                return {
                    "ok": True,
                    "status": "ok",
                    "target": target,
                    "status_code": int(result.get("status_code") or 0),
                    "latency_ms": int(result.get("latency_ms") or 0),
                    "exit_ip": exit_ip,
                    "error_code": "",
                    "error": "",
                }
            failures.append(
                {
                    "target": target,
                    "error_code": "exit_ip_missing",
                    "error": "响应成功但未解析到出口 IP",
                    "latency_ms": int(result.get("latency_ms") or 0),
                }
            )
            continue
        failures.append({"target": target, **result})
        if str(result.get("error_code") or "") in PROXY_LEVEL_ERROR_CODES:
            break

    last = failures[-1] if failures else {}
    return {
        "ok": False,
        "status": "failed",
        "target": str(last.get("target") or ""),
        "status_code": int(last.get("status_code") or 0),
        "latency_ms": int(last.get("latency_ms") or 0),
        "exit_ip": "",
        "error_code": str(last.get("error_code") or "unknown"),
        "error": str(last.get("error") or "代理基础连通性检测失败")[:500],
        "failures": failures[-3:],
    }


def _parse_geo_payload(payload: dict[str, Any], *, source: str) -> dict[str, Any]:
    if source == "ipapi":
        country_code = str(payload.get("country_code") or payload.get("country") or "").upper()
        org = str(payload.get("org") or "").strip()
        asn = str(payload.get("asn") or "").strip()
        return {
            "country_code": country_code,
            "country_name": str(payload.get("country_name") or "").strip(),
            "region_name": str(payload.get("region") or payload.get("region_name") or "").strip(),
            "city": str(payload.get("city") or "").strip(),
            "asn": asn,
            "isp": org,
            "source": source,
        }
    if source == "ipinfo":
        org = str(payload.get("org") or "").strip()
        asn = org.split(" ", 1)[0] if org.upper().startswith("AS") else ""
        return {
            "country_code": str(payload.get("country") or "").upper(),
            "country_name": str(payload.get("country") or "").upper(),
            "region_name": str(payload.get("region") or "").strip(),
            "city": str(payload.get("city") or "").strip(),
            "asn": asn,
            "isp": org,
            "source": source,
        }
    return {}


def lookup_geo(
    exit_ip: str,
    *,
    timeout_seconds: int = 6,
    stop_checker: Callable[[], None] | None = None,
) -> dict[str, Any]:
    _run_stop_checker(stop_checker)
    ip = str(exit_ip or "").strip()
    if not ip:
        return {"ok": False, "error_code": "exit_ip_missing", "error": "缺少出口 IP"}
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        return {"ok": False, "error_code": "invalid_exit_ip", "error": f"无效出口 IP: {ip}"}

    failures: list[dict[str, Any]] = []
    for template in GEO_TARGETS:
        _run_stop_checker(stop_checker)
        source = "ipapi" if "ipapi.co" in template else "ipinfo"
        url = template.format(ip=ip)
        start = time.perf_counter()
        try:
            response = requests.get(url, timeout=timeout_seconds, headers={"Accept": "application/json"})
            _run_stop_checker(stop_checker)
            latency_ms = _elapsed_ms(start)
            if response.status_code >= 400:
                failures.append(
                    {
                        "source": source,
                        "status_code": response.status_code,
                        "error_code": classify_error(status_code=response.status_code),
                        "error": f"HTTP {response.status_code}",
                        "latency_ms": latency_ms,
                    }
                )
                continue
            payload = _safe_json(response)
            if not payload:
                failures.append({"source": source, "error_code": "geo_parse_failed", "error": "GeoIP 响应为空", "latency_ms": latency_ms})
                continue
            parsed = _parse_geo_payload(payload, source=source)
            if parsed.get("country_code"):
                return {"ok": True, "latency_ms": latency_ms, **parsed}
            failures.append({"source": source, "error_code": "geo_country_missing", "error": "GeoIP 未返回国家", "latency_ms": latency_ms})
        except TaskInterruption:
            raise
        except Exception as exc:
            failures.append({"source": source, "error_code": classify_error(exc), "error": str(exc)[:500], "latency_ms": _elapsed_ms(start)})
    last = failures[-1] if failures else {}
    return {
        "ok": False,
        "error_code": str(last.get("error_code") or "geo_lookup_failed"),
        "error": str(last.get("error") or "GeoIP 查询失败")[:500],
        "failures": failures[-3:],
    }


def _parse_cloudflare_trace(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip().lower()] = value.strip()

    ip = values.get("ip", "")
    if ip:
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            ip = ""
    country_code = str(values.get("loc") or "").strip().upper()
    if len(country_code) != 2 or not country_code.isalpha():
        country_code = ""
    return {
        "ip": ip,
        "country_code": country_code,
        "colo": str(values.get("colo") or "").strip().upper(),
        "raw_loc": str(values.get("loc") or "").strip(),
    }


def lookup_geo_via_proxy_trace(
    proxy_url: str,
    *,
    timeout_seconds: int = 6,
    stop_checker: Callable[[], None] | None = None,
) -> dict[str, Any]:
    _run_stop_checker(stop_checker)
    valid, message = _validate_proxy_url(proxy_url)
    if not valid:
        return {"ok": False, "error_code": "invalid_url", "error": message}

    result = _request_via_proxy(CLOUDFLARE_TRACE_TARGET, proxy_url, timeout_seconds=timeout_seconds)
    _run_stop_checker(stop_checker)
    if not result.get("ok"):
        return {
            "ok": False,
            "error_code": str(result.get("error_code") or "cloudflare_trace_failed"),
            "error": str(result.get("error") or "Cloudflare trace 查询失败")[:500],
            "latency_ms": int(result.get("latency_ms") or 0),
            "source": "cloudflare_trace",
        }

    parsed = _parse_cloudflare_trace(str(result.get("text") or ""))
    if parsed.get("country_code"):
        return {
            "ok": True,
            "latency_ms": int(result.get("latency_ms") or 0),
            "country_code": parsed["country_code"],
            "country_name": parsed["country_code"],
            "region_name": "",
            "city": "",
            "asn": "",
            "isp": "",
            "source": "cloudflare_trace",
            "exit_ip": parsed.get("ip") or "",
            "colo": parsed.get("colo") or "",
        }

    return {
        "ok": False,
        "error_code": "cloudflare_trace_country_missing",
        "error": "Cloudflare trace 未返回 loc 国家码",
        "latency_ms": int(result.get("latency_ms") or 0),
        "source": "cloudflare_trace",
        "exit_ip": parsed.get("ip") or "",
        "raw_loc": parsed.get("raw_loc") or "",
    }



def _request_via_cffi(url: str, proxy_url: str, *, timeout_seconds: int) -> dict[str, Any]:
    start = time.perf_counter()
    if cffi_requests is None:
        return {
            "ok": False,
            "status_code": 0,
            "latency_ms": 0,
            "error_code": "curl_cffi_missing",
            "error": "curl_cffi 未安装，无法执行注册链路指纹检测",
        }
    try:
        session = cffi_requests.Session(impersonate="chrome")
        proxy_config = build_requests_proxy_config(proxy_url)
        if proxy_config:
            session.proxies = proxy_config
        response = session.get(
            url,
            headers=CFFI_BROWSER_HEADERS,
            timeout=timeout_seconds,
            allow_redirects=True,
        )
        latency_ms = _elapsed_ms(start)
        status_code = int(response.status_code or 0)
        body = str(getattr(response, "text", "") or "")[:300]
        result = {
            "ok": status_code < 400,
            "status_code": status_code,
            "latency_ms": latency_ms,
            "final_url": str(getattr(response, "url", "") or url).split("?", 1)[0],
            "body": body,
        }
        if status_code >= 400:
            result.update({"error_code": classify_error(status_code=status_code), "error": f"HTTP {status_code}"})
        else:
            result.update({"error_code": "", "error": ""})
        return result
    except Exception as exc:
        return {
            "ok": False,
            "status_code": 0,
            "latency_ms": _elapsed_ms(start),
            "error_code": classify_error(exc),
            "error": str(exc)[:500],
        }


def _chatgpt_status_from_error(error_code: str, status_code: int = 0) -> str:
    if error_code == "http_403":
        return "blocked_403"
    if error_code == "http_429":
        return "rate_limited_429"
    if error_code in {"connect_timeout", "read_timeout", "timeout"}:
        return "timeout"
    if error_code in {"tls_error", "dns_error", "proxy_error", "proxy_auth_failed", "connection_error", "connection_refused"}:
        return error_code
    if error_code == "curl_cffi_missing":
        return "unchecked"
    return "failed" if status_code or error_code else "unknown"


def probe_chatgpt_cffi(proxy_url: str, *, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS) -> dict[str, Any]:
    valid, message = _validate_proxy_url(proxy_url)
    if not valid:
        return {
            "ok": False,
            "status": "failed",
            "error_code": "invalid_url",
            "error": message,
            "latency_ms": 0,
            "targets": {},
            "target": "chatgpt_cffi",
        }

    targets: dict[str, Any] = {}
    total_latency = 0
    failures: list[dict[str, Any]] = []
    for url in CFFI_CHATGPT_TARGETS:
        result = _request_via_cffi(url, proxy_url, timeout_seconds=timeout_seconds)
        key = "auth" if "auth.openai.com" in url else "chatgpt"
        targets[key] = {"target": url, **result}
        total_latency += int(result.get("latency_ms") or 0)
        if not result.get("ok"):
            failures.append({"target_key": key, "target": url, **result})

    if not failures:
        return {
            "ok": True,
            "status": "ok",
            "target": "chatgpt_cffi",
            "status_code": 200,
            "latency_ms": total_latency,
            "error_code": "",
            "error": "",
            "targets": targets,
        }

    first = failures[0]
    error_code = str(first.get("error_code") or classify_error(status_code=int(first.get("status_code") or 0)))
    status = _chatgpt_status_from_error(error_code, int(first.get("status_code") or 0))
    return {
        "ok": False,
        "status": status,
        "target": "chatgpt_cffi",
        "status_code": int(first.get("status_code") or 0),
        "latency_ms": total_latency or int(first.get("latency_ms") or 0),
        "error_code": error_code,
        "error": f"{first.get('target_key') or first.get('target')}: {first.get('error') or status}"[:500],
        "targets": targets,
        "failures": failures,
    }


def probe_chatgpt_registration_flow(proxy_url: str, *, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Probe the same homepage + CSRF path used by the registration state machine.

    Plain requests against chatgpt.com often return Cloudflare 403 even when the
    real registration client can proceed. Conversely, a generic curl_cffi probe
    can be too optimistic because the registration engine uses a task-level
    browser/TLS fingerprint and reuses its session. This probe intentionally
    follows the real ChatGPTClient preflight: visit homepage, then fetch CSRF,
    retrying by resetting the same session/fingerprint just like registration.
    """

    valid, message = _validate_proxy_url(proxy_url)
    if not valid:
        return {
            "ok": False,
            "status": "failed",
            "error_code": "invalid_url",
            "error": message,
            "latency_ms": 0,
            "target": "registration_homepage_csrf",
            "attempts": [],
        }

    start = time.perf_counter()
    attempts: list[dict[str, Any]] = []
    try:
        from services.chatgpt_core.chatgpt_client import ChatGPTClient

        probe_browser_mode = str(config_store.get("default_executor", "protocol") or "protocol").strip() or "protocol"
        client = ChatGPTClient(proxy=proxy_url, verbose=False, browser_mode=probe_browser_mode)
        try:
            for attempt in range(REGISTRATION_PROBE_ATTEMPTS):
                if attempt > 0:
                    client._reset_session()
                homepage_ok = bool(client.visit_homepage())
                homepage_probe = dict(getattr(client, "last_homepage_probe", {}) or {})
                status_code = int(homepage_probe.get("status_code") or 0)
                detail = str(homepage_probe.get("detail") or homepage_probe.get("reason") or "").strip()
                attempt_payload: dict[str, Any] = {
                    "attempt": attempt + 1,
                    "homepage_ok": homepage_ok,
                    "status_code": status_code,
                    "reason": str(homepage_probe.get("reason") or ""),
                    "detail": detail[:300],
                    "url": str(homepage_probe.get("url") or CHATGPT_TARGET).split("?", 1)[0],
                    "impersonate": str(getattr(client, "impersonate", "") or ""),
                    "chrome_major": int(getattr(client, "chrome_major", 0) or 0),
                    "browser_mode": probe_browser_mode,
                }
                if not homepage_ok:
                    attempts.append(attempt_payload)
                    continue

                csrf_token = client.get_csrf_token()
                attempt_payload["csrf_ok"] = bool(csrf_token)
                attempts.append(attempt_payload)
                if csrf_token:
                    return {
                        "ok": True,
                        "status": "ok",
                        "target": "registration_homepage_csrf",
                        "status_code": 200,
                        "latency_ms": _elapsed_ms(start),
                        "error_code": "",
                        "error": "",
                        "attempts": attempts,
                    }
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
    except BaseException as exc:
        return {
            "ok": False,
            "status": _chatgpt_status_from_error(classify_error(exc), 0),
            "target": "registration_homepage_csrf",
            "status_code": 0,
            "latency_ms": _elapsed_ms(start),
            "error_code": classify_error(exc),
            "error": str(exc)[:500],
            "attempts": attempts,
        }

    last = attempts[-1] if attempts else {}
    status_code = int(last.get("status_code") or 0)
    reason = str(last.get("reason") or "").strip()
    error_code = reason if reason in PROXY_LEVEL_ERROR_CODES else classify_error(status_code=status_code)
    status = _chatgpt_status_from_error(error_code, status_code)
    error = str(last.get("detail") or reason or "注册链路首页/CSRF 检测失败").strip()
    if last.get("homepage_ok") and not last.get("csrf_ok"):
        error_code = "csrf_failed"
        status = "failed"
        error = "获取 CSRF token 失败"
    return {
        "ok": False,
        "status": status,
        "target": "registration_homepage_csrf",
        "status_code": status_code,
        "latency_ms": _elapsed_ms(start),
        "error_code": error_code or "unknown",
        "error": error[:500],
        "attempts": attempts,
    }

def probe_chatgpt(proxy_url: str, *, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS) -> dict[str, Any]:
    registration_result = probe_chatgpt_registration_flow(proxy_url, timeout_seconds=timeout_seconds)
    cffi_result = probe_chatgpt_cffi(proxy_url, timeout_seconds=timeout_seconds)
    result = _request_via_proxy(CHATGPT_TARGET, proxy_url, timeout_seconds=timeout_seconds)
    status_code = int(result.get("status_code") or 0)
    latency_ms = int(result.get("latency_ms") or 0)
    legacy = {
        "ok": bool(result.get("ok")),
        "status_code": status_code,
        "latency_ms": latency_ms,
        "error_code": str(result.get("error_code") or classify_error(status_code=status_code)),
        "error": str(result.get("error") or "")[:500],
        "target": CHATGPT_TARGET,
    }
    if result.get("ok"):
        legacy["status"] = "ok"
    else:
        legacy["status"] = _chatgpt_status_from_error(str(legacy.get("error_code") or ""), status_code)

    # 注册链路以真实 ChatGPTClient 首页+CSRF 预检为准。普通 requests 403
    # 只作为诊断；通用 curl_cffi 能 200 也不能替代真实注册状态机预检。
    merged = dict(registration_result)
    merged["legacy_requests"] = legacy
    merged["cffi_probe"] = cffi_result
    if isinstance(cffi_result.get("targets"), dict):
        # 兼容旧诊断面板读取 chatgpt.targets.auth/chatgpt。
        merged["targets"] = cffi_result.get("targets")
    if registration_result.get("ok"):
        merged["status"] = "ok"
        merged["status_code"] = int(registration_result.get("status_code") or 200)
        merged["error_code"] = ""
        merged["error"] = ""
        return merged
    if str(registration_result.get("error_code") or "") == "curl_cffi_missing" and result.get("ok"):
        return {
            "ok": True,
            "status": "ok",
            "target": CHATGPT_TARGET,
            "status_code": status_code,
            "latency_ms": latency_ms,
            "error_code": "",
            "error": "",
            "legacy_requests": legacy,
            "cffi_unavailable": True,
        }
    return merged


def calculate_health_score(proxy: ProxyModel) -> float:
    now = utcnow()
    if not bool(getattr(proxy, "is_active", False)):
        return 0.0
    score = 100.0
    cooldown_until = getattr(proxy, "cooldown_until", None) or getattr(proxy, "homepage_circuit_open_until", None)
    if isinstance(cooldown_until, datetime) and cooldown_until.tzinfo is None:
        cooldown_until = cooldown_until.replace(tzinfo=timezone.utc)
    if cooldown_until and cooldown_until > now:
        score -= 55
    scan_status = str(getattr(proxy, "scan_status", "") or "unchecked").lower()
    if scan_status == "failed":
        score -= 55
    elif scan_status == "degraded":
        score -= 25
    elif scan_status == "unchecked":
        score -= 20
    chatgpt_status = str(getattr(proxy, "chatgpt_status", "") or "unchecked").lower()
    if chatgpt_status in {"blocked_403", "rate_limited_429"}:
        score -= 45
    elif chatgpt_status in {"timeout", "tls_error", "dns_error", "proxy_error", "proxy_auth_failed", "connection_error", "connection_refused", "failed"}:
        score -= 35
    elif chatgpt_status == "unchecked":
        score -= 5
    latency_ms = int(getattr(proxy, "last_latency_ms", 0) or 0)
    if latency_ms >= 8000:
        score -= 30
    elif latency_ms >= 3000:
        score -= 15
    elif latency_ms and latency_ms <= 1200:
        score += 5
    failures = int(getattr(proxy, "consecutive_failures", 0) or 0)
    homepage_failures = int(getattr(proxy, "homepage_consecutive_failures", 0) or 0)
    score -= min(30, max(failures, homepage_failures) * 10)
    desired_country = str(getattr(proxy, "desired_country_code", "") or getattr(proxy, "region", "") or "").strip().upper()
    exit_country = str(getattr(proxy, "exit_country_code", "") or "").strip().upper()
    if desired_country and len(desired_country) <= 3 and exit_country and desired_country != exit_country:
        score -= 10
    return max(0.0, min(100.0, round(score, 1)))


def _scan_status_from_results(basic: dict[str, Any] | None, chatgpt: dict[str, Any] | None) -> str:
    if basic and not basic.get("ok"):
        return "failed"
    if basic and basic.get("ok") and chatgpt and not chatgpt.get("ok"):
        return "degraded"
    if basic and basic.get("ok"):
        return "ok"
    return "unchecked"


def scan_proxy_url(
    proxy_url: str,
    *,
    targets: Iterable[str] | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    refresh_geo: bool = True,
    existing_geo: dict[str, Any] | None = None,
    stop_checker: Callable[[], None] | None = None,
) -> dict[str, Any]:
    _run_stop_checker(stop_checker)
    normalized_targets = normalize_targets(targets)
    timeout = parse_int(timeout_seconds, DEFAULT_TIMEOUT_SECONDS, minimum=2, maximum=60)
    started = utcnow()
    started_perf = time.perf_counter()
    summary: dict[str, Any] = {
        "url_masked": mask_proxy_url(proxy_url),
        "targets": normalized_targets,
        "started_at": iso(started),
        "basic": None,
        "geo": None,
        "chatgpt": None,
    }

    basic_result: dict[str, Any] | None = None
    if "basic" in normalized_targets:
        basic_result = probe_basic(
            proxy_url,
            timeout_seconds=timeout,
            stop_checker=stop_checker,
        )
        _run_stop_checker(stop_checker)
        summary["basic"] = basic_result

    if "geo" in normalized_targets:
        exit_ip = str((basic_result or {}).get("exit_ip") or "").strip()
        existing = existing_geo or {}
        if (
            exit_ip
            and not refresh_geo
            and str(existing.get("exit_ip") or "").strip() == exit_ip
            and str(existing.get("country_code") or "").strip()
        ):
            summary["geo"] = {"ok": True, "source": existing.get("source") or "cached", **existing}
        elif exit_ip:
            proxy_trace_geo = lookup_geo_via_proxy_trace(
                proxy_url,
                timeout_seconds=min(timeout, 8),
                stop_checker=stop_checker,
            )
            if proxy_trace_geo.get("ok"):
                trace_exit_ip = str(proxy_trace_geo.get("exit_ip") or "").strip()
                if trace_exit_ip:
                    proxy_trace_geo["exit_ip"] = trace_exit_ip
                summary["geo"] = proxy_trace_geo
            else:
                ip_geo = lookup_geo(
                    exit_ip,
                    timeout_seconds=min(timeout, 8),
                    stop_checker=stop_checker,
                )
                if not ip_geo.get("ok"):
                    failures: list[dict[str, Any]] = []
                    trace_failure = {
                        "source": "cloudflare_trace",
                        "error_code": str(proxy_trace_geo.get("error_code") or ""),
                        "error": str(proxy_trace_geo.get("error") or "")[:500],
                        "latency_ms": int(proxy_trace_geo.get("latency_ms") or 0),
                    }
                    if trace_failure["error_code"] or trace_failure["error"]:
                        failures.append(trace_failure)
                    failures.extend(list(ip_geo.get("failures") or [])[-3:])
                    ip_geo = {**ip_geo, "failures": failures[-4:]}
                summary["geo"] = ip_geo
        else:
            summary["geo"] = {"ok": False, "error_code": "exit_ip_missing", "error": "基础扫描未得到出口 IP"}

    chatgpt_result: dict[str, Any] | None = None
    if "chatgpt" in normalized_targets:
        _run_stop_checker(stop_checker)
        basic_error_code = str((basic_result or {}).get("error_code") or "")
        if basic_result is not None and not basic_result.get("ok") and basic_error_code in PROXY_LEVEL_ERROR_CODES:
            chatgpt_result = {
                "ok": False,
                "status": basic_error_code,
                "target": CHATGPT_TARGET,
                "status_code": 0,
                "latency_ms": 0,
                "error_code": basic_error_code,
                "error": str((basic_result or {}).get("error") or "代理基础连通性失败，跳过 ChatGPT 检测")[:500],
                "skipped_due_to_basic_failure": True,
            }
        else:
            chatgpt_result = probe_chatgpt(proxy_url, timeout_seconds=timeout)
        _run_stop_checker(stop_checker)
        summary["chatgpt"] = chatgpt_result

    summary["scan_status"] = _scan_status_from_results(basic_result, chatgpt_result)
    summary["duration_ms"] = _elapsed_ms(started_perf)
    summary["finished_at"] = iso(utcnow())
    return summary


def _cooldown_enabled() -> bool:
    return parse_bool(config_store.get("proxy_pool_cooldown_enabled", "true"), default=True)


def _apply_scan_summary(proxy: ProxyModel, summary: dict[str, Any], *, targets: list[str]) -> None:
    now = utcnow()
    endpoint = parse_proxy_endpoint(proxy.url)
    proxy.scheme = endpoint["scheme"]
    proxy.host = endpoint["host"]
    proxy.port = endpoint["port"]

    basic = summary.get("basic") if isinstance(summary.get("basic"), dict) else None
    geo = summary.get("geo") if isinstance(summary.get("geo"), dict) else None
    chatgpt = summary.get("chatgpt") if isinstance(summary.get("chatgpt"), dict) else None

    proxy.last_scan_at = now
    proxy.last_scan_duration_ms = int(summary.get("duration_ms") or 0)
    proxy.scan_status = str(summary.get("scan_status") or "unchecked")

    if basic is not None:
        proxy.last_latency_ms = int(basic.get("latency_ms") or 0)
        proxy.last_checked = now
        if basic.get("ok"):
            proxy.success_count = int(proxy.success_count or 0) + 1
            proxy.consecutive_failures = 0
            proxy.last_error_code = ""
            proxy.last_error = ""
            proxy.exit_ip = str(basic.get("exit_ip") or "").strip()
            proxy.cooldown_until = None
        else:
            proxy.fail_count = int(proxy.fail_count or 0) + 1
            proxy.consecutive_failures = int(proxy.consecutive_failures or 0) + 1
            proxy.last_error_code = str(basic.get("error_code") or "unknown")[:120]
            proxy.last_error = str(basic.get("error") or "代理基础连通性检测失败")[:500]
            if _cooldown_enabled():
                proxy.cooldown_until = now + timedelta(seconds=BASIC_FAILURE_COOLDOWN_SECONDS)
                proxy.homepage_circuit_open_until = proxy.cooldown_until

    if geo is not None:
        if geo.get("ok"):
            proxy.exit_country_code = str(geo.get("country_code") or "").upper()
            proxy.exit_country_name = str(geo.get("country_name") or "")[:120]
            proxy.exit_region_name = str(geo.get("region_name") or "")[:120]
            proxy.exit_city = str(geo.get("city") or "")[:120]
            proxy.exit_asn = str(geo.get("asn") or "")[:80]
            proxy.exit_isp = str(geo.get("isp") or "")[:180]
            proxy.geo_source = str(geo.get("source") or "")[:80]
            proxy.geo_checked_at = now
        elif basic and basic.get("ok"):
            # 出口 IP 已经拿到，GeoIP 失败不要把代理直接判死，只把错误留在调试摘要。
            proxy.geo_checked_at = now

    if chatgpt is not None:
        proxy.chatgpt_status = str(chatgpt.get("status") or ("ok" if chatgpt.get("ok") else "failed"))[:120]
        proxy.chatgpt_status_code = int(chatgpt.get("status_code") or 0)
        proxy.chatgpt_latency_ms = int(chatgpt.get("latency_ms") or 0)
        proxy.chatgpt_last_checked_at = now
        if chatgpt.get("ok"):
            proxy.chatgpt_last_error = ""
            proxy.homepage_success_count = int(proxy.homepage_success_count or 0) + 1
            proxy.homepage_consecutive_failures = 0
            proxy.homepage_last_error = ""
            proxy.homepage_last_status_code = int(chatgpt.get("status_code") or 200)
            proxy.homepage_last_checked = now
            if _cooldown_enabled() and (not proxy.cooldown_until or proxy.cooldown_until <= now):
                proxy.homepage_circuit_open_until = None
        else:
            proxy.chatgpt_last_error = str(chatgpt.get("error") or chatgpt.get("error_code") or "ChatGPT 检测失败")[:500]
            proxy.homepage_fail_count = int(proxy.homepage_fail_count or 0) + 1
            proxy.homepage_consecutive_failures = int(proxy.homepage_consecutive_failures or 0) + 1
            proxy.homepage_last_error = proxy.chatgpt_last_error
            proxy.homepage_last_status_code = int(chatgpt.get("status_code") or 0)
            proxy.homepage_last_checked = now
            if _cooldown_enabled() and int(proxy.homepage_consecutive_failures or 0) >= 3:
                proxy.homepage_circuit_open_until = now + timedelta(seconds=CHATGPT_FAILURE_COOLDOWN_SECONDS)

    proxy.health_score = calculate_health_score(proxy)
    summary["health_score"] = proxy.health_score
    try:
        proxy.last_probe_json = json.dumps(summary, ensure_ascii=False, sort_keys=True)[:12000]
    except Exception:
        proxy.last_probe_json = json.dumps({"error": "probe_json_encode_failed"}, ensure_ascii=False)


def scan_proxy_id(
    proxy_id: int,
    *,
    targets: Iterable[str] | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    refresh_geo: bool = True,
) -> dict[str, Any]:
    normalized_targets = normalize_targets(targets)
    with Session(engine) as session:
        proxy = session.get(ProxyModel, int(proxy_id))
        if not proxy:
            return {"proxy_id": int(proxy_id), "status": "missing", "ok": False, "error_code": "proxy_missing", "error": "代理不存在"}
        proxy_url = str(proxy.url or "").strip()
        existing_geo = {
            "exit_ip": str(proxy.exit_ip or ""),
            "country_code": str(proxy.exit_country_code or ""),
            "country_name": str(proxy.exit_country_name or ""),
            "region_name": str(proxy.exit_region_name or ""),
            "city": str(proxy.exit_city or ""),
            "asn": str(proxy.exit_asn or ""),
            "isp": str(proxy.exit_isp or ""),
            "source": str(proxy.geo_source or ""),
        }

    summary = scan_proxy_url(
        proxy_url,
        targets=normalized_targets,
        timeout_seconds=timeout_seconds,
        refresh_geo=refresh_geo,
        existing_geo=existing_geo,
    )

    with Session(engine) as session:
        proxy = session.get(ProxyModel, int(proxy_id))
        if not proxy:
            return {"proxy_id": int(proxy_id), "status": "missing", "ok": False, "error_code": "proxy_missing", "error": "代理已被删除"}
        _apply_scan_summary(proxy, summary, targets=normalized_targets)
        session.add(proxy)
        session.commit()
        return {
            "proxy_id": int(proxy.id or 0),
            "url_masked": mask_proxy_url(proxy.url),
            "ok": proxy.scan_status in {"ok", "degraded"},
            "status": proxy.scan_status,
            "health_score": float(proxy.health_score or 0),
            "exit_ip": proxy.exit_ip,
            "exit_country_code": proxy.exit_country_code,
            "latency_ms": int(proxy.last_latency_ms or 0),
            "chatgpt_status": proxy.chatgpt_status,
            "error_code": proxy.last_error_code,
            "error": proxy.last_error,
            "summary": summary,
        }


@dataclass
class ProxyScanJob:
    job_id: str
    proxy_ids: list[int]
    targets: list[str]
    concurrency: int
    timeout_seconds: int
    refresh_geo: bool
    status: str = "pending"
    total: int = 0
    done: int = 0
    ok: int = 0
    failed: int = 0
    degraded: int = 0
    started_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    finished_at: datetime | None = None
    current: dict[int, str] = field(default_factory=dict)
    recent_results: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    cancel_requested: bool = False

    def snapshot(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "targets": self.targets,
            "concurrency": self.concurrency,
            "timeout_seconds": self.timeout_seconds,
            "refresh_geo": self.refresh_geo,
            "total": self.total,
            "done": self.done,
            "ok": self.ok,
            "failed": self.failed,
            "degraded": self.degraded,
            "started_at": iso(self.started_at),
            "updated_at": iso(self.updated_at),
            "finished_at": iso(self.finished_at),
            "current": [{"proxy_id": key, "url_masked": value} for key, value in self.current.items()],
            "recent_results": list(self.recent_results[-DEFAULT_MAX_RECENT_RESULTS:]),
            "error": self.error,
        }


class ProxyScanManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, ProxyScanJob] = {}

    def _cleanup_locked(self) -> None:
        if len(self._jobs) <= 30:
            return
        finished = [job for job in self._jobs.values() if job.status in {"done", "failed", "cancelled"}]
        finished.sort(key=lambda item: item.updated_at)
        for job in finished[: max(0, len(self._jobs) - 30)]:
            self._jobs.pop(job.job_id, None)

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(str(job_id or ""))
            return job.snapshot() if job else None

    def cancel_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(str(job_id or ""))
            if not job:
                return None
            job.cancel_requested = True
            job.updated_at = utcnow()
            return job.snapshot()

    def start_job(
        self,
        proxy_ids: Iterable[int],
        *,
        targets: Iterable[str] | None = None,
        concurrency: int = DEFAULT_CONCURRENCY,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        refresh_geo: bool = True,
    ) -> dict[str, Any]:
        ids = []
        seen: set[int] = set()
        for raw in proxy_ids:
            try:
                proxy_id = int(raw)
            except Exception:
                continue
            if proxy_id <= 0 or proxy_id in seen:
                continue
            seen.add(proxy_id)
            ids.append(proxy_id)
        normalized_targets = normalize_targets(targets)
        job = ProxyScanJob(
            job_id=f"proxy_scan_{utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}",
            proxy_ids=ids,
            targets=normalized_targets,
            concurrency=parse_int(concurrency, DEFAULT_CONCURRENCY, minimum=1, maximum=32),
            timeout_seconds=parse_int(timeout_seconds, DEFAULT_TIMEOUT_SECONDS, minimum=2, maximum=60),
            refresh_geo=bool(refresh_geo),
            total=len(ids),
        )
        with self._lock:
            self._cleanup_locked()
            self._jobs[job.job_id] = job
        thread = threading.Thread(target=self._run_job, args=(job.job_id,), daemon=True)
        thread.start()
        return job.snapshot()

    def start_job_from_query(
        self,
        *,
        ids: Iterable[int] | None = None,
        only_active: bool = False,
        targets: Iterable[str] | None = None,
        concurrency: int = DEFAULT_CONCURRENCY,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        refresh_geo: bool = True,
    ) -> dict[str, Any]:
        if ids is not None:
            proxy_ids = list(ids)
        else:
            with Session(engine) as session:
                query = select(ProxyModel)
                if only_active:
                    query = query.where(ProxyModel.is_active == True)
                proxy_ids = [int(item.id or 0) for item in session.exec(query).all() if item.id]
        return self.start_job(
            proxy_ids,
            targets=targets,
            concurrency=concurrency,
            timeout_seconds=timeout_seconds,
            refresh_geo=refresh_geo,
        )

    def _mark_job(self, job_id: str, **updates: Any) -> ProxyScanJob | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            for key, value in updates.items():
                setattr(job, key, value)
            job.updated_at = utcnow()
            return job

    def _append_result(self, job_id: str, result: dict[str, Any]) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job.done += 1
            status = str(result.get("status") or "").lower()
            if status == "ok":
                job.ok += 1
            elif status == "degraded":
                job.degraded += 1
            else:
                job.failed += 1
            compact = {
                "proxy_id": result.get("proxy_id"),
                "url_masked": result.get("url_masked"),
                "status": result.get("status"),
                "health_score": result.get("health_score"),
                "exit_country_code": result.get("exit_country_code"),
                "exit_ip": result.get("exit_ip"),
                "latency_ms": result.get("latency_ms"),
                "chatgpt_status": result.get("chatgpt_status"),
                "error_code": result.get("error_code"),
                "error": result.get("error"),
            }
            job.recent_results.append(compact)
            if len(job.recent_results) > DEFAULT_MAX_RECENT_RESULTS:
                job.recent_results = job.recent_results[-DEFAULT_MAX_RECENT_RESULTS:]
            job.current.pop(int(result.get("proxy_id") or 0), None)
            job.updated_at = utcnow()

    def _run_job(self, job_id: str) -> None:
        job = self._mark_job(job_id, status="running")
        if not job:
            return
        if job.total == 0:
            self._mark_job(job_id, status="done", finished_at=utcnow())
            return
        try:
            with ThreadPoolExecutor(max_workers=job.concurrency) as executor:
                futures = {}
                for proxy_id in job.proxy_ids:
                    with self._lock:
                        active_job = self._jobs.get(job_id)
                        if active_job and active_job.cancel_requested:
                            break
                    futures[
                        executor.submit(
                            scan_proxy_id,
                            proxy_id,
                            targets=job.targets,
                            timeout_seconds=job.timeout_seconds,
                            refresh_geo=job.refresh_geo,
                        )
                    ] = proxy_id
                    with self._lock:
                        active_job = self._jobs.get(job_id)
                        if active_job:
                            active_job.current[proxy_id] = f"#{proxy_id}"
                            active_job.updated_at = utcnow()
                for future in as_completed(futures):
                    proxy_id = futures[future]
                    with self._lock:
                        active_job = self._jobs.get(job_id)
                        cancel_requested = bool(active_job and active_job.cancel_requested)
                    if cancel_requested:
                        self._append_result(
                            job_id,
                            {
                                "proxy_id": proxy_id,
                                "url_masked": f"#{proxy_id}",
                                "status": "cancelled",
                                "error_code": "cancelled",
                                "error": "扫描已取消",
                            },
                        )
                        continue
                    try:
                        result = future.result()
                    except Exception as exc:
                        result = {
                            "proxy_id": proxy_id,
                            "url_masked": f"#{proxy_id}",
                            "status": "failed",
                            "ok": False,
                            "error_code": classify_error(exc),
                            "error": str(exc)[:500],
                        }
                    self._append_result(job_id, result)
            with self._lock:
                job = self._jobs.get(job_id)
                if not job:
                    return
                job.status = "cancelled" if job.cancel_requested else "done"
                job.finished_at = utcnow()
                job.updated_at = utcnow()
                job.current.clear()
        except Exception as exc:
            self._mark_job(job_id, status="failed", error=str(exc)[:500], finished_at=utcnow())


proxy_scan_manager = ProxyScanManager()
