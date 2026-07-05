from __future__ import annotations

from dataclasses import dataclass
import re
import secrets
import string
from typing import Any
from urllib.parse import urlsplit, urlunsplit

REGION_RE = re.compile(r"(?i)(region-)([^-:@/]+)")
SID_RE = re.compile(r"(?i)(sid-)([^-:@/]+)")
RETENTION_RE = re.compile(r"(?i)(-t-)([^-:@/]+)")
COUNTRY_RE = re.compile(r"^[A-Z]{2}$")
MIN_RETENTION_MINUTES = 1
MAX_RETENTION_MINUTES = 1440


@dataclass(frozen=True)
class DynamicProxyResolution:
    template: str
    proxy_url: str
    requested_country_code: str
    template_country_code: str
    resolved_country_code: str
    provider: str
    sid_refreshed: bool
    sid: str
    redacted_template: str
    redacted_proxy_url: str
    retention_minutes: int | None = None
    retention_applied: bool = False


def normalize_country_code(value: Any) -> str:
    country = str(value or "").strip().upper()
    if not country:
        return ""
    if not COUNTRY_RE.fullmatch(country):
        raise ValueError("动态代理出口国家必须是两位 ISO 国家码，例如 US / JP / SG")
    return country


def declared_proxy_region(proxy_url: Any) -> str:
    match = REGION_RE.search(str(proxy_url or ""))
    return match.group(2).upper() if match else ""


def dynamic_proxy_supported(proxy_url: Any) -> bool:
    return bool(declared_proxy_region(proxy_url))


def detect_provider(proxy_url: Any) -> str:
    text = str(proxy_url or "")
    try:
        parts = urlsplit(text)
        host = str(parts.hostname or "").lower()
    except Exception:
        host = ""
    if "cliproxy.io" in host or "cliproxy" in text.lower():
        return "cliproxy"
    return "dynamic"


def _new_sid(length: int = 10) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(max(6, min(32, int(length or 10)))))


def proxy_with_region(proxy_url: Any, country_code: Any) -> str:
    value = str(proxy_url or "").strip()
    if not value:
        raise ValueError("动态代理模板为空")
    country = normalize_country_code(country_code)
    if not country:
        raise ValueError("动态代理模式必须填写出口国家")
    if not REGION_RE.search(value):
        raise ValueError("动态代理模板缺少 region-XX/region-Rand 标记，无法按需改写出口国家")
    return REGION_RE.sub(lambda match: f"{match.group(1)}{country}", value, count=1)


def proxy_with_fresh_sid(proxy_url: Any) -> tuple[str, bool, str]:
    value = str(proxy_url or "").strip()
    if not value:
        raise ValueError("动态代理模板为空")
    sid = _new_sid()
    if not SID_RE.search(value):
        return value, False, ""
    return SID_RE.sub(lambda match: f"{match.group(1)}{sid}", value, count=1), True, sid


def normalize_retention_minutes(
    value: Any,
    *,
    default: int | None = None,
    minimum: int = MIN_RETENTION_MINUTES,
    maximum: int = MAX_RETENTION_MINUTES,
) -> int | None:
    if value is None or value == "":
        return default
    try:
        text = str(value).strip()
        if not re.fullmatch(r"\d+", text):
            raise ValueError
        parsed = int(text)
    except Exception as exc:
        raise ValueError(f"动态代理 IP 保留时长必须是 {minimum}-{maximum} 分钟整数") from exc
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"动态代理 IP 保留时长必须是 {minimum}-{maximum} 分钟整数")
    return parsed


def proxy_with_retention(proxy_url: Any, retention_minutes: Any = None) -> tuple[str, bool, int | None]:
    value = str(proxy_url or "").strip()
    if not value:
        raise ValueError("动态代理模板为空")
    minutes = normalize_retention_minutes(retention_minutes, default=None)
    if minutes is None:
        return value, False, None
    if RETENTION_RE.search(value):
        return RETENTION_RE.sub(lambda match: f"{match.group(1)}{minutes}", value, count=1), True, minutes
    if SID_RE.search(value):
        return SID_RE.sub(lambda match: f"{match.group(0)}-t-{minutes}", value, count=1), True, minutes
    return value, False, minutes


def redact_proxy_url(proxy_url: Any) -> str:
    value = str(proxy_url or "").strip()
    if not value:
        return ""
    try:
        parts = urlsplit(value)
        if parts.scheme and parts.netloc:
            host = parts.hostname or ""
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            port = f":{parts.port}" if parts.port else ""
            auth = "***:***@" if parts.username or parts.password else ""
            return urlunsplit((parts.scheme, f"{auth}{host}{port}", parts.path or "", "", ""))[:180]
    except Exception:
        pass
    if "@" in value and ":" in value.rsplit("@", 1)[0]:
        head, tail = value.rsplit("@", 1)
        scheme = f"{head.split('://', 1)[0]}://" if "://" in head else ""
        return f"{scheme}***:***@{tail}"[:180]
    return value[:180]


def resolve_dynamic_proxy_template(
    proxy_url: Any,
    country_code: Any,
    *,
    refresh_sid: bool = True,
    retention_minutes: Any = None,
) -> DynamicProxyResolution:
    template = str(proxy_url or "").strip()
    if not template:
        raise ValueError("动态代理模板为空")
    requested = normalize_country_code(country_code)
    if not requested:
        raise ValueError("动态代理模式必须填写出口国家")
    template_country = declared_proxy_region(template)
    if not template_country:
        raise ValueError("动态代理模板缺少 region-XX/region-Rand 标记，无法按需改写出口国家")

    resolved = proxy_with_region(template, requested)
    sid_refreshed = False
    sid = ""
    if refresh_sid:
        resolved, sid_refreshed, sid = proxy_with_fresh_sid(resolved)
    resolved, retention_applied, retention = proxy_with_retention(resolved, retention_minutes)

    return DynamicProxyResolution(
        template=template,
        proxy_url=resolved,
        requested_country_code=requested,
        template_country_code=template_country,
        resolved_country_code=declared_proxy_region(resolved) or requested,
        provider=detect_provider(template),
        sid_refreshed=sid_refreshed,
        sid=sid,
        redacted_template=redact_proxy_url(template),
        redacted_proxy_url=redact_proxy_url(resolved),
        retention_minutes=retention,
        retention_applied=retention_applied,
    )
