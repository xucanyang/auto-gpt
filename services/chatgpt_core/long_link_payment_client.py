"""Internal client for admin-configured payment links from openai-pay-long-link.

The long-link service owns every payment setting.  Auto-GPT sends only an
account access token plus a stable request id, then persists the redacted batch
result returned by the internal API.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

import requests

from services.chatgpt_core.payment_link_cache import (
    PAYMENT_LINK_GENERATION_TEAM,
    PAYMENT_LINK_PLAN_TEAM,
    normalize_payment_link_expires_at,
    payment_link_variant_key,
)


DEFAULT_BASE_URL = "http://openai-pay-long-link:8788"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 20.0
DEFAULT_PROFILE_CACHE_SECONDS = 30.0

_PROFILE_PATH = "/api/internal/payment-links/profile"
_BATCH_PATH = "/api/internal/payment-links/batches"
_BATCH_STATUS_PATH = "/api/internal/payment-links/batches/{batch_id}"
_RUNNING_STATUSES = {"queued", "running"}
_TERMINAL_STATUSES = {"done", "error", "interrupted"}
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)?\b")
_profile_cache_lock = threading.Lock()
_profile_cache: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}


class LongLinkPaymentError(RuntimeError):
    """Raised when the generic internal payment-link API cannot be used."""


def _env_float(name: str, default: float, *, minimum: float) -> float:
    try:
        value = float(str(os.getenv(name) or default).strip())
    except (TypeError, ValueError):
        value = default
    return max(value, minimum)


def _safe_error_text(value: Any, *, secrets: Iterable[str] = ()) -> str:
    text = str(value or "").strip()
    for secret in secrets:
        normalized = str(secret or "")
        if normalized:
            text = text.replace(normalized, "***")
    text = _JWT_RE.sub("***", text)
    return text[:500]


def _http_url(value: Any) -> str:
    url = str(value or "").strip()
    if not url or len(url) > 10_000 or any(ord(character) < 32 or ord(character) == 127 for character in url):
        return ""
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return ""
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return ""
    return url


def _iso_timestamp(value: Any) -> str:
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return ""
    if timestamp <= 0:
        return ""
    try:
        return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


def normalize_payment_profile(profile_response: dict[str, Any] | None) -> dict[str, Any]:
    response = profile_response if isinstance(profile_response, dict) else {}
    profile = response.get("profile") if isinstance(response.get("profile"), dict) else {}
    profile_hash = str(response.get("profile_hash") or profile.get("profile_hash") or "").strip()
    link_type = str(response.get("link_type") or profile.get("link_type") or "hosted").strip().lower() or "hosted"
    country = str(profile.get("billing_country") or profile.get("country") or "").strip().upper()
    currency = str(profile.get("currency") or "").strip().upper()
    try:
        effective_concurrency = int(response.get("effective_concurrency") or profile.get("effective_concurrency") or 0)
    except (TypeError, ValueError):
        effective_concurrency = 0
    normalized = {
        "profile_hash": profile_hash,
        "link_type": link_type,
        "country": country,
        "currency": currency,
        "effective_concurrency": max(effective_concurrency, 0),
        "profile": dict(profile),
    }
    # Keep the redacted Team business summary returned by the long-link
    # profile endpoint.  Raw promo codes are never accepted here.
    team = profile.get("team") if isinstance(profile.get("team"), dict) else profile.get("team_plan_data")
    if isinstance(team, dict):
        normalized["team"] = dict(team)
        normalized["team_plan_data"] = dict(team)
    for key in ("plan", "plan_name", "generation_kind", "promo_code_digest", "variant_key"):
        value = response.get(key)
        if value in (None, ""):
            value = profile.get(key)
        if value not in (None, ""):
            normalized[key] = value
    if "team" in normalized:
        normalized.setdefault("plan", PAYMENT_LINK_PLAN_TEAM)
        normalized.setdefault("generation_kind", PAYMENT_LINK_GENERATION_TEAM)
        normalized.setdefault("plan_name", "chatgptteamplan")
    return normalized


def payment_link_from_remote_job(
    job: dict[str, Any],
    *,
    profile: dict[str, Any],
) -> dict[str, Any]:
    """Validate and normalize a completed generic long-link response."""

    payload = job if isinstance(job, dict) else {}
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    url = ""
    for key in ("url", "provider_redirect_url", "long_url", "stripe_redirect_url", "stripe_hosted_url"):
        url = _http_url(result.get(key))
        if url:
            break
    if not url:
        raise LongLinkPaymentError("支付链接任务成功但未返回有效 HTTP(S) 链接")

    link_type = str(result.get("link_type") or profile.get("link_type") or "hosted").strip().lower() or "hosted"
    raw_generation_kind = str(
        result.get("generation_kind") or profile.get("generation_kind") or ""
    ).strip().lower()
    raw_plan = str(result.get("plan") or profile.get("plan") or "").strip().lower()
    raw_plan_name = str(result.get("plan_name") or profile.get("plan_name") or "").strip().lower()
    is_team = (
        link_type in {"team", "team_checkout"}
        or raw_plan in {"team", "team_checkout", "chatgptteamplan"}
        or raw_generation_kind == PAYMENT_LINK_GENERATION_TEAM
        or raw_plan_name == "chatgptteamplan"
    )
    if is_team:
        link_type = "team"
    team_source = result.get("team_plan_data") if isinstance(result.get("team_plan_data"), dict) else {}
    if not team_source and isinstance(result.get("team"), dict):
        team_source = result.get("team")
    if not team_source and isinstance(profile.get("team"), dict):
        team_source = profile.get("team")
    if not team_source and isinstance(profile.get("team_plan_data"), dict):
        team_source = profile.get("team_plan_data")
    team_plan_data = {
        "workspace_name": str(team_source.get("workspace_name") or team_source.get("workspaceName") or "").strip(),
        "price_interval": str(team_source.get("price_interval") or team_source.get("priceInterval") or "month").strip().lower(),
        "seat_quantity": team_source.get("seat_quantity") or team_source.get("seatQuantity") or 2,
        "cancel_url": str(team_source.get("cancel_url") or team_source.get("cancelUrl") or "").strip(),
    } if is_team else {}
    if is_team:
        try:
            team_plan_data["seat_quantity"] = int(team_plan_data["seat_quantity"])
        except (TypeError, ValueError):
            team_plan_data["seat_quantity"] = 2
    profile_hash = str(payload.get("profile_hash") or profile.get("profile_hash") or "").strip()
    promo_code_digest = str(
        result.get("promo_code_digest") or profile.get("promo_code_digest") or team_source.get("promo_code_digest") or ""
    ).strip()
    if is_team and promo_code_digest:
        team_plan_data["promo_code_digest"] = promo_code_digest
    plan = PAYMENT_LINK_PLAN_TEAM if is_team else "plus"
    generation_kind = PAYMENT_LINK_GENERATION_TEAM if is_team else "plus_checkout"
    completed_at = _iso_timestamp(payload.get("completed_at"))
    output: dict[str, Any] = {
        "url": url,
        "plan": plan,
        "generation_kind": generation_kind,
        "plan_name": "chatgptteamplan" if is_team else "chatgptplusplan",
        "country": str(result.get("billing_country") or profile.get("country") or "").strip().upper(),
        "currency": str(result.get("currency") or profile.get("currency") or "").strip().upper(),
        "payment_link_format": "long_link",
        "payment_source": "long_link",
        "link_type": link_type,
        "profile_hash": profile_hash,
        "remote_batch_id": str(payload.get("batch_id") or "").strip(),
        "remote_job_id": str(payload.get("job_id") or "").strip(),
        "remote_request_id": str(payload.get("request_id") or "").strip(),
        "generated_at": completed_at,
        "created_at": completed_at or datetime.now(timezone.utc).isoformat(),
    }
    link_expires_at = normalize_payment_link_expires_at(result.get("link_expires_at"))
    if link_expires_at is not None:
        output["link_expires_at"] = link_expires_at
    if link_type == "paypal":
        output["paypal_url"] = _http_url(result.get("paypal_url")) or url
    if is_team:
        output["team_plan_data"] = team_plan_data
        output["workspace_name"] = team_plan_data.get("workspace_name") or ""
        output["price_interval"] = team_plan_data.get("price_interval") or "month"
        output["seat_quantity"] = team_plan_data.get("seat_quantity") or 2
        output["cancel_url"] = team_plan_data.get("cancel_url") or ""
        output["promo_code_digest"] = promo_code_digest
    for key in (
        "provider_redirect_url",
        "long_url",
        "stripe_redirect_url",
        "stripe_hosted_url",
        "cs_id",
        "payment_method_id",
        "payment_method_type",
        "processor_entity",
        "billing_country",
        "payment_locale",
        "amount",
        "amount_display",
        "cs_count",
    ):
        value = result.get(key)
        if value is not None and value != "":
            output[key] = value
    output["variant_key"] = (
        payment_link_variant_key(output)
        if is_team
        else str(result.get("variant_key") or "").strip() or payment_link_variant_key(output)
    )
    return output


class LongLinkPaymentClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        profile_cache_seconds: float = DEFAULT_PROFILE_CACHE_SECONDS,
        session: requests.Session | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.base_url = str(base_url or "").strip().rstrip("/")
        self.api_key = str(api_key or "").strip()
        self.request_timeout = max(float(request_timeout or DEFAULT_REQUEST_TIMEOUT_SECONDS), 1.0)
        self.profile_cache_seconds = max(float(profile_cache_seconds or 0.0), 0.0)
        self.session = session or requests.Session()
        self._monotonic = monotonic
        if not self.base_url:
            raise LongLinkPaymentError("未配置支付链接生成服务地址")
        if not self.api_key:
            raise LongLinkPaymentError("未配置支付链接生成服务密钥")

    @classmethod
    def from_env(cls) -> "LongLinkPaymentClient":
        return cls(
            base_url=os.getenv("OPENAI_PAY_LONG_LINK_BASE_URL", DEFAULT_BASE_URL),
            api_key=os.getenv("OPENAI_PAY_LONG_LINK_API_KEY", ""),
            request_timeout=_env_float(
                "OPENAI_PAY_LONG_LINK_REQUEST_TIMEOUT_SECONDS",
                DEFAULT_REQUEST_TIMEOUT_SECONDS,
                minimum=1.0,
            ),
            profile_cache_seconds=_env_float(
                "OPENAI_PAY_LONG_LINK_PROFILE_CACHE_SECONDS",
                DEFAULT_PROFILE_CACHE_SECONDS,
                minimum=0.0,
            ),
        )

    def _cache_key(self, overrides: dict[str, Any] | None = None) -> tuple[str, str, str]:
        override_digest = ""
        if isinstance(overrides, dict) and overrides:
            try:
                import json

                override_digest = hashlib.sha256(
                    json.dumps(overrides, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
                ).hexdigest()
            except (TypeError, ValueError):
                override_digest = hashlib.sha256(repr(sorted(overrides.items())).encode("utf-8")).hexdigest()
        return self.base_url, hashlib.sha256(self.api_key.encode("utf-8")).hexdigest(), override_digest

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        secrets: Iterable[str] = (),
    ) -> dict[str, Any]:
        try:
            response = self.session.request(
                method,
                f"{self.base_url}{path}",
                json=payload,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "X-Internal-API-Key": self.api_key,
                },
                timeout=self.request_timeout,
            )
        except requests.RequestException as exc:
            raise LongLinkPaymentError(f"支付链接生成服务不可用: {_safe_error_text(exc, secrets=secrets)}") from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise LongLinkPaymentError(f"支付链接生成服务返回无效 JSON (HTTP {response.status_code})") from exc
        if not isinstance(data, dict):
            raise LongLinkPaymentError(f"支付链接生成服务返回格式错误 (HTTP {response.status_code})")
        if not 200 <= int(response.status_code or 0) < 300:
            detail = data.get("detail") or data.get("error") or data.get("message") or "上游请求失败"
            raise LongLinkPaymentError(
                f"支付链接生成服务请求失败 (HTTP {response.status_code}): "
                f"{_safe_error_text(detail, secrets=secrets)}"
            )
        return data

    def get_profile(
        self,
        *,
        overrides: dict[str, Any] | None = None,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        safe_overrides = dict(overrides) if isinstance(overrides, dict) else {}
        cache_key = self._cache_key(safe_overrides)
        now = self._monotonic()
        if not force_refresh and self.profile_cache_seconds > 0:
            with _profile_cache_lock:
                cached = _profile_cache.get(cache_key)
                if cached and cached[0] > now:
                    return dict(cached[1])
        if safe_overrides:
            normalized = normalize_payment_profile(
                self._request("POST", _PROFILE_PATH, payload={"profileOverrides": safe_overrides})
            )
        else:
            normalized = normalize_payment_profile(self._request("GET", _PROFILE_PATH))
        if not normalized["profile_hash"]:
            raise LongLinkPaymentError("支付链接生成服务未返回 profile_hash")
        if self.profile_cache_seconds > 0:
            with _profile_cache_lock:
                _profile_cache[cache_key] = (now + self.profile_cache_seconds, dict(normalized))
        return normalized

    def submit_batch(
        self,
        *,
        items: list[dict[str, Any]],
        expected_profile_hash: str,
        profile_overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not items:
            raise LongLinkPaymentError("支付链接批次不能为空")
        if len(items) > 1000:
            raise LongLinkPaymentError("单次最多提交 1000 个支付链接账号")
        serialized_items: list[dict[str, str]] = []
        tokens: list[str] = []
        seen_ids: set[str] = set()
        for item in items:
            raw = item if isinstance(item, dict) else {}
            token = str(raw.get("access_token") or raw.get("accessToken") or "").strip()
            request_id = str(raw.get("request_id") or raw.get("requestId") or "").strip()
            if not token:
                raise LongLinkPaymentError("账号缺少 Access Token")
            if not request_id:
                raise LongLinkPaymentError("支付链接请求缺少 request_id")
            if request_id in seen_ids:
                raise LongLinkPaymentError("同一批次存在重复 request_id")
            seen_ids.add(request_id)
            tokens.append(token)
            serialized_items.append({"accessToken": token, "requestId": request_id})
        request_payload: dict[str, Any] = {
            "expectedProfileHash": str(expected_profile_hash or "").strip(),
            "items": serialized_items,
        }
        if isinstance(profile_overrides, dict) and profile_overrides:
            request_payload["profileOverrides"] = dict(profile_overrides)
        response = self._request(
            "POST",
            _BATCH_PATH,
            payload=request_payload,
            secrets=tokens,
        )
        returned_items = response.get("items") if isinstance(response.get("items"), list) else []
        if len(returned_items) != len(serialized_items):
            raise LongLinkPaymentError("支付链接生成服务未返回完整批次任务")
        returned_request_ids: set[str] = set()
        for item in returned_items:
            request_id = str(item.get("request_id") or "").strip() if isinstance(item, dict) else ""
            if not request_id or request_id in returned_request_ids:
                raise LongLinkPaymentError("支付链接生成服务返回了无效批次任务")
            returned_request_ids.add(request_id)
        expected_request_ids = {item["requestId"] for item in serialized_items}
        if returned_request_ids != expected_request_ids:
            raise LongLinkPaymentError("支付链接生成服务返回的批次任务与请求不一致")
        return response

    def get_batch(self, batch_id: str) -> dict[str, Any]:
        normalized_id = str(batch_id or "").strip()
        if not normalized_id:
            raise LongLinkPaymentError("支付链接远端批次 ID 为空")
        response = self._request(
            "GET",
            _BATCH_STATUS_PATH.format(batch_id=urllib.parse.quote(normalized_id, safe="")),
        )
        status = str(response.get("status") or "").strip().lower()
        if status not in _RUNNING_STATUSES | _TERMINAL_STATUSES | {"partial"}:
            raise LongLinkPaymentError(f"支付链接生成服务返回未知批次状态: {_safe_error_text(status)}")
        if not isinstance(response.get("items"), list):
            raise LongLinkPaymentError("支付链接生成服务未返回批次项目")
        return response


def clear_profile_cache() -> None:
    with _profile_cache_lock:
        _profile_cache.clear()
