"""Shared helpers for ChatGPT checkout/payment-link cache payloads."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from core.proxy_utils import normalize_proxy_url
from services.chatgpt_core.payment import (
    normalize_checkout_country,
    normalize_checkout_currency,
)

PAYMENT_LINK_STATUS_LABELS = {
    "invalid": "无效",
    "already_paid": "已经支付过",
    "amount_not_zero": "非0元订单",
    "not_usd": "非指定区域订单",
    "precheck_failed": "支付链接核验失败",
}
PAYMENT_LINK_REGENERATE_STATUSES = {
    "invalid",
    "amount_not_zero",
    "not_usd",
    "precheck_failed",
}
PAYMENT_LINK_STATUS_SYNC_STATUSES = {"already_paid"}


def normalize_payment_link_plan(value: Any) -> str:
    plan = str(value or "plus").strip().lower()
    return plan if plan in {"plus", "team"} else "plus"


def normalize_payment_link_status(value: Any) -> str:
    return str(value or "").strip().lower()


def payment_link_status_label(value: Any) -> str:
    status = normalize_payment_link_status(value)
    return PAYMENT_LINK_STATUS_LABELS.get(status, status)


def payment_link_requires_regeneration(cached: dict[str, Any] | None) -> bool:
    if not isinstance(cached, dict):
        return False
    return normalize_payment_link_status(cached.get("link_status")) in PAYMENT_LINK_REGENERATE_STATUSES


def payment_link_requires_status_sync(cached: dict[str, Any] | None) -> bool:
    if not isinstance(cached, dict):
        return False
    return normalize_payment_link_status(cached.get("link_status")) in PAYMENT_LINK_STATUS_SYNC_STATUSES


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(1, parsed)


def normalize_payment_link_params(params: dict[str, Any] | None) -> dict[str, Any]:
    source = params if isinstance(params, dict) else {}
    plan = normalize_payment_link_plan(source.get("plan"))
    country = normalize_checkout_country(source.get("country"))
    currency = normalize_checkout_currency(source.get("currency"), country)
    return {
        "plan": plan,
        "country": country,
        "currency": currency,
        "proxy": normalize_proxy_url(source.get("proxy")) or "",
        "promo_code": str(source.get("promo_code") or "").strip(),
        "workspace_name": str(source.get("workspace_name") or "MyTeam").strip() or "MyTeam",
        "seat_quantity": max(2, _positive_int(source.get("seat_quantity", 5), 5)),
        "price_interval": str(source.get("price_interval") or "month").strip().lower() or "month",
    }


def payment_link_cache_matches(
    cached: dict[str, Any] | None,
    params: dict[str, Any] | None,
) -> bool:
    if not isinstance(cached, dict) or not str(cached.get("url") or "").strip():
        return False
    expected = normalize_payment_link_params(params)
    cached_plan = normalize_payment_link_plan(cached.get("plan"))
    cached_country = normalize_checkout_country(cached.get("country"))
    cached_currency = normalize_checkout_currency(cached.get("currency"), cached_country)
    cached_proxy = normalize_proxy_url(cached.get("proxy")) or ""
    return (
        cached_plan == expected["plan"]
        and cached_country == expected["country"]
        and cached_currency == expected["currency"]
        and cached_proxy == expected["proxy"]
    )


def build_payment_link_cache_payload(
    data: dict[str, Any] | None,
    *,
    source: str,
    fallback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload_source = data if isinstance(data, dict) else {}
    fallback_source = fallback if isinstance(fallback, dict) else {}
    url = str(
        payload_source.get("url")
        or payload_source.get("checkout_url")
        or payload_source.get("cashier_url")
        or payload_source.get("chatgpt_checkout_url")
        or fallback_source.get("url")
        or fallback_source.get("checkout_url")
        or fallback_source.get("cashier_url")
        or fallback_source.get("chatgpt_checkout_url")
        or ""
    ).strip()
    if not url:
        return {}

    plan = normalize_payment_link_plan(
        payload_source.get("plan")
        or payload_source.get("chatgpt_checkout_plan")
        or fallback_source.get("plan")
        or fallback_source.get("chatgpt_checkout_plan")
    )
    country = normalize_checkout_country(
        payload_source.get("country")
        or payload_source.get("chatgpt_checkout_country")
        or fallback_source.get("country")
        or fallback_source.get("chatgpt_checkout_country")
    )
    currency = normalize_checkout_currency(
        payload_source.get("currency")
        or payload_source.get("chatgpt_checkout_currency")
        or fallback_source.get("currency")
        or fallback_source.get("chatgpt_checkout_currency"),
        country,
    )

    payload: dict[str, Any] = {
        "url": url,
        "plan": plan,
        "country": country,
        "currency": currency,
        "proxy": normalize_proxy_url(payload_source.get("proxy") or fallback_source.get("proxy")) or "",
        "promo_code": str(payload_source.get("promo_code") or fallback_source.get("promo_code") or "").strip(),
        "source": str(source or payload_source.get("source") or fallback_source.get("source") or "").strip(),
        "created_at": str(
            payload_source.get("created_at")
            or fallback_source.get("created_at")
            or datetime.now(timezone.utc).isoformat()
        ),
    }

    billing = payload_source.get("billing") if isinstance(payload_source.get("billing"), dict) else fallback_source.get("billing")
    if isinstance(billing, dict):
        payload["billing"] = billing

    amount = (
        payload_source.get("checkout_amount")
        if "checkout_amount" in payload_source
        else payload_source.get("chatgpt_checkout_amount")
    )
    if amount is None:
        amount = fallback_source.get("checkout_amount")
    if amount is not None:
        payload["checkout_amount"] = amount

    amount_is_zero = (
        payload_source.get("checkout_amount_is_zero")
        if "checkout_amount_is_zero" in payload_source
        else payload_source.get("chatgpt_checkout_amount_is_zero")
    )
    if amount_is_zero is None:
        amount_is_zero = fallback_source.get("checkout_amount_is_zero")
    if amount_is_zero is not None:
        payload["checkout_amount_is_zero"] = bool(amount_is_zero)

    probe = (
        payload_source.get("checkout_probe")
        if isinstance(payload_source.get("checkout_probe"), dict)
        else payload_source.get("chatgpt_checkout_probe")
    )
    if not isinstance(probe, dict):
        probe = fallback_source.get("checkout_probe")
    if isinstance(probe, dict):
        payload["checkout_probe"] = probe

    return payload


def cache_checkout_link_in_extra(extra: dict[str, Any], *, source: str) -> dict[str, Any]:
    if not isinstance(extra, dict):
        return {}
    existing = extra.get("chatgpt_last_payment_link") if isinstance(extra.get("chatgpt_last_payment_link"), dict) else {}
    payload = build_payment_link_cache_payload(extra, source=source, fallback=existing)
    if payload:
        extra["chatgpt_last_payment_link"] = payload
    return extra
