"""MiyaIP dynamic proxy provider.

The provider returns proxy credentials as plain text even though business
failures may still use HTTP 200 with a JSON body.  Keep transport handling and
response parsing in this module so callers never need to construct or log a
credential-bearing Generate URL.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import ipaddress
import re
from typing import Any, Iterable
from urllib.parse import quote, unquote, urlsplit

import requests

from .dynamic_proxy import normalize_country_code, redact_proxy_url


MIYAIP_GENERATE_URL = "https://miyaip.com/api/ProxyLogic/Generate"
MIYAIP_GATEWAY_SERVERS = {"us", "as", "eu"}
MIYAIP_PROTOCOLS = {"http", "socks5"}
MAX_RESPONSE_BYTES = 64 * 1024


class MiyaIPError(RuntimeError):
    """Safe provider error whose message never contains MiyaIP credentials."""


@dataclass(frozen=True)
class MiyaIPProxyResolution:
    proxy_url: str = field(repr=False)
    requested_country_code: str
    resolved_country_code: str
    provider: str
    protocol: str
    gateway_server: str
    username: str = field(repr=False)
    password: str = field(repr=False)
    redacted_proxy_url: str


@dataclass(frozen=True)
class MiyaIPConfig:
    crc: str = field(repr=False)
    key_name: str = field(repr=False)
    pool: int
    gateway_server: str
    protocol: str
    request_timeout_seconds: int


def normalize_miyaip_gateway_server(value: Any, default: str = "us") -> str:
    server = str(value or default).strip().lower()
    if server not in MIYAIP_GATEWAY_SERVERS:
        raise ValueError("MiyaIP 网关区域必须是 us / as / eu")
    return server


def normalize_miyaip_protocol(value: Any, default: str = "http") -> str:
    protocol = str(value or default).strip().lower()
    if protocol not in MIYAIP_PROTOCOLS:
        raise ValueError("MiyaIP 代理协议必须是 http / socks5")
    return protocol


def normalize_miyaip_pool(value: Any, default: int = 1) -> int:
    try:
        pool = int(str(value if value not in (None, "") else default).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("MiyaIP 套餐 Pool 必须是正整数") from exc
    if pool < 1 or pool > 999999:
        raise ValueError("MiyaIP 套餐 Pool 必须是 1-999999 的整数")
    return pool


def normalize_miyaip_timeout(value: Any, default: int = 15) -> int:
    try:
        timeout = int(float(str(value if value not in (None, "") else default).strip()))
    except (TypeError, ValueError) as exc:
        raise ValueError("MiyaIP 请求超时必须是 2-60 秒整数") from exc
    if timeout < 2 or timeout > 60:
        raise ValueError("MiyaIP 请求超时必须是 2-60 秒整数")
    return timeout


def normalize_miyaip_credential(
    value: Any,
    label: str,
    *,
    required: bool = True,
) -> str:
    secret = str(value or "").strip()
    if not secret and required:
        raise ValueError(f"MiyaIP {label} 不能为空")
    if not secret:
        return ""
    if len(secret) > 512 or any(ord(char) < 32 for char in secret):
        raise ValueError(f"MiyaIP {label} 格式无效")
    return secret


def normalize_miyaip_config(
    *,
    crc: Any,
    key_name: Any,
    pool: Any = 1,
    gateway_server: Any = "us",
    protocol: Any = "http",
    timeout_seconds: Any = 15,
) -> MiyaIPConfig:
    """Validate a complete provider configuration without making a request."""

    return MiyaIPConfig(
        crc=normalize_miyaip_credential(crc, "Crc"),
        key_name=normalize_miyaip_credential(key_name, "KeyName"),
        pool=normalize_miyaip_pool(pool),
        gateway_server=normalize_miyaip_gateway_server(gateway_server),
        protocol=normalize_miyaip_protocol(protocol),
        request_timeout_seconds=normalize_miyaip_timeout(timeout_seconds),
    )


def _safe_provider_message(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "未知错误"
    text = re.sub(
        r"(?i)(\b(?:crc|keyname)\b\s*[=:]\s*)[^\s,;&}\]]+",
        r"\1[REDACTED]",
        text,
    )
    text = re.sub(r"https?://[^\s'\"<>]+", "[MiyaIP endpoint]", text)
    return text[:240]


def _redact_known_values(value: Any, *secrets: str) -> str:
    text = _safe_provider_message(value)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


def _response_text(response: Any) -> str:
    raw_stream = getattr(response, "raw", None)
    if raw_stream is not None and callable(getattr(raw_stream, "read", None)):
        try:
            raw = raw_stream.read(MAX_RESPONSE_BYTES + 1, decode_content=True)
        except TypeError:
            raw = raw_stream.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise MiyaIPError("MiyaIP 生成接口响应过大")
        encoding = str(getattr(response, "encoding", "") or "utf-8")
        try:
            return raw.decode(encoding, errors="replace").strip()
        except LookupError:
            return raw.decode("utf-8", errors="replace").strip()

    raw = getattr(response, "content", None)
    if isinstance(raw, bytes):
        if len(raw) > MAX_RESPONSE_BYTES:
            raise MiyaIPError("MiyaIP 生成接口响应过大")
        encoding = str(getattr(response, "encoding", "") or "utf-8")
        try:
            return raw.decode(encoding, errors="replace").strip()
        except LookupError:
            return raw.decode("utf-8", errors="replace").strip()
    text = str(getattr(response, "text", "") or "")
    if len(text.encode("utf-8", errors="replace")) > MAX_RESPONSE_BYTES:
        raise MiyaIPError("MiyaIP 生成接口响应过大")
    return text.strip()


def _proxy_lines_from_json(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        raise MiyaIPError("MiyaIP 生成接口返回了无效 JSON")

    code = payload.get("code")
    success = payload.get("success")
    body = payload.get("body", payload.get("data"))
    code_text = str(code if code is not None else "").strip().lower()
    is_success_code = code is None or code_text in {"0", "200", "ok", "success"}
    if success is False or not is_success_code:
        message = payload.get("message") or payload.get("msg") or "生成失败"
        raise MiyaIPError(f"MiyaIP 生成失败: {_safe_provider_message(message)}")

    if isinstance(body, str):
        return [line.strip() for line in body.splitlines() if line.strip()]
    if isinstance(body, list):
        return [str(line or "").strip() for line in body if str(line or "").strip()]
    if isinstance(body, dict):
        for key in ("proxy", "proxies", "list", "items", "result"):
            value = body.get(key)
            if isinstance(value, str):
                return [line.strip() for line in value.splitlines() if line.strip()]
            if isinstance(value, list):
                return [str(line or "").strip() for line in value if str(line or "").strip()]
    raise MiyaIPError("MiyaIP 生成成功响应中没有代理地址")


def parse_miyaip_generate_response(value: Any) -> list[str]:
    """Parse a Generate response and reject HTTP-200 business errors."""

    text = str(value or "").strip()
    if not text:
        raise MiyaIPError("MiyaIP 生成接口返回空响应")
    if text[:1] in {"{", "["}:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise MiyaIPError("MiyaIP 生成接口返回了无效 JSON") from exc
        return _proxy_lines_from_json(payload)
    return [line.strip() for line in text.splitlines() if line.strip()]


def _valid_host_port(host: str, port: Any) -> tuple[str, int]:
    normalized_host = str(host or "").strip().strip("[]")
    if not normalized_host or len(normalized_host) > 253 or any(char.isspace() for char in normalized_host):
        raise MiyaIPError("MiyaIP 返回的代理主机无效")
    try:
        ipaddress.ip_address(normalized_host)
    except ValueError:
        labels = normalized_host.rstrip(".").split(".")
        if not labels or any(
            not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
            for label in labels
        ):
            raise MiyaIPError("MiyaIP 返回的代理主机无效")
    try:
        normalized_port = int(port)
    except (TypeError, ValueError) as exc:
        raise MiyaIPError("MiyaIP 返回的代理端口无效") from exc
    if normalized_port < 1 or normalized_port > 65535:
        raise MiyaIPError("MiyaIP 返回的代理端口无效")
    display_host = f"[{normalized_host}]" if ":" in normalized_host else normalized_host
    return display_host, normalized_port


def _build_proxy_url(protocol: str, username: str, password: str, host: str, port: Any) -> str:
    if (
        not username
        or not password
        or len(username) > 512
        or len(password) > 512
        or any(ord(char) < 32 for char in f"{username}{password}")
    ):
        raise MiyaIPError("MiyaIP 返回的代理凭据不完整")
    display_host, normalized_port = _valid_host_port(host, port)
    scheme = "socks5h" if protocol == "socks5" else "http"
    return (
        f"{scheme}://{quote(username, safe='')}:{quote(password, safe='')}"
        f"@{display_host}:{normalized_port}"
    )


def parse_miyaip_proxy_line(line: Any, protocol: Any = "http") -> tuple[str, str, str]:
    """Return ``(proxy_url, username, password)`` for supported provider rows.

    MiyaIP Format=1 is ``username:password@host:port``.  Full proxy URLs and
    ``host:port:username:password`` are accepted as defensive compatibility
    formats, while malformed or unauthenticated endpoints are rejected.
    """

    value = str(line or "").strip()
    normalized_protocol = normalize_miyaip_protocol(protocol)
    if not value or any(char in value for char in "\r\n\t"):
        raise MiyaIPError("MiyaIP 返回的代理地址为空或格式无效")

    if "://" in value:
        try:
            parts = urlsplit(value)
            source_scheme = str(parts.scheme or "").lower()
            if source_scheme not in {"http", "https", "socks5", "socks5h"}:
                raise MiyaIPError("MiyaIP 返回了不支持的代理协议")
            if parts.path not in {"", "/"} or parts.query or parts.fragment:
                raise MiyaIPError("MiyaIP 返回的代理 URL 格式无效")
            username = unquote(parts.username or "")
            password = unquote(parts.password or "")
            return (
                _build_proxy_url(normalized_protocol, username, password, parts.hostname or "", parts.port),
                username,
                password,
            )
        except MiyaIPError:
            raise
        except Exception as exc:
            raise MiyaIPError("MiyaIP 返回的代理 URL 格式无效") from exc

    if "@" in value:
        userinfo, endpoint = value.rsplit("@", 1)
        if ":" not in userinfo or ":" not in endpoint:
            raise MiyaIPError("MiyaIP 返回的 Format=1 代理格式无效")
        username, password = userinfo.split(":", 1)
        host, port = endpoint.rsplit(":", 1)
        return _build_proxy_url(normalized_protocol, username, password, host, port), username, password

    parts = value.split(":")
    if len(parts) == 4:
        host, port, username, password = parts
        return _build_proxy_url(normalized_protocol, username, password, host, port), username, password
    raise MiyaIPError("MiyaIP 返回的代理格式无效")


def _first_valid_proxy(lines: Iterable[str], protocol: str) -> tuple[str, str, str]:
    errors: list[str] = []
    for line in lines:
        try:
            return parse_miyaip_proxy_line(line, protocol)
        except MiyaIPError as exc:
            errors.append(str(exc))
    detail = errors[0] if errors else "MiyaIP 响应中没有代理地址"
    raise MiyaIPError(detail)


def generate_miyaip_proxy(
    country_code: Any,
    *,
    crc: Any,
    key_name: Any,
    pool: Any = 1,
    gateway_server: Any = "us",
    protocol: Any = "http",
    timeout_seconds: Any = 15,
) -> MiyaIPProxyResolution:
    country = normalize_country_code(country_code)
    if not country:
        raise ValueError("动态代理模式必须填写出口国家")
    config = normalize_miyaip_config(
        crc=crc,
        key_name=key_name,
        pool=pool,
        gateway_server=gateway_server,
        protocol=protocol,
        timeout_seconds=timeout_seconds,
    )

    params = {
        "Num": 1,
        "Country": country,
        "SessionTime": -1,
        "Server": config.gateway_server,
        "Format": 1,
        "Crc": config.crc,
        "Pool": config.pool,
        "KeyName": config.key_name,
        "GenType": config.protocol,
    }
    response = None
    try:
        response = requests.get(
            MIYAIP_GENERATE_URL,
            params=params,
            timeout=config.request_timeout_seconds,
            allow_redirects=False,
            stream=True,
        )
    except requests.RequestException as exc:
        raise MiyaIPError(f"MiyaIP 生成请求失败: {exc.__class__.__name__}") from exc
    try:
        status_code = int(getattr(response, "status_code", 0) or 0)
        if status_code < 200 or status_code >= 300:
            raise MiyaIPError(f"MiyaIP 生成接口 HTTP {status_code or 'unknown'}")
        lines = parse_miyaip_generate_response(_response_text(response))
    except MiyaIPError as exc:
        raise MiyaIPError(_redact_known_values(exc, config.crc, config.key_name)) from exc
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()
    proxy_url, username, password = _first_valid_proxy(lines, config.protocol)
    return MiyaIPProxyResolution(
        proxy_url=proxy_url,
        requested_country_code=country,
        resolved_country_code=country,
        provider="miyaip",
        protocol=config.protocol,
        gateway_server=config.gateway_server,
        username=username,
        password=password,
        redacted_proxy_url=redact_proxy_url(proxy_url),
    )
