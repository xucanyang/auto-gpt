"""Shared helpers for ChatGPT checkout/payment-link cache payloads."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from core.proxy_utils import normalize_proxy_url
from services.chatgpt_core.payment import (
    DEFAULT_PAYMENT_LINK_FORMAT,
    PAYMENT_LINK_FORMAT_LONG,
    is_default_hosted_checkout_fragment,
    normalize_checkout_url_for_link_format,
    normalize_checkout_country,
    normalize_checkout_currency,
    normalize_payment_link_format,
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
PAYMENT_LINK_FORMAT_PAYPAL = "paypal_url"
PAYMENT_LINK_FORMAT_LONG_LINK = "long_link"
PAYMENT_SOURCE_CHATGPT_HOSTED = "chatgpt_hosted"
PAYMENT_SOURCE_LONG_LINK_PAYPAL = "long_link_paypal"
PAYMENT_SOURCE_LONG_LINK = "long_link"
RETIRED_PAYMENT_REQUEST_KEYS = frozenset({
    "promo_code",
    "workspace_name",
    "seat_quantity",
    "price_interval",
})


def normalize_payment_link_plan(value: Any) -> str:
    return "plus"


def _is_supported_raw_payment_link_plan(value: Any) -> bool:
    raw_plan = str(value or "").strip().lower()
    return not raw_plan or raw_plan == "plus"


def validate_plus_payment_request_params(params: dict[str, Any] | None) -> None:
    """Reject retired product inputs before any account, task, or cache work."""
    if params is None:
        return
    if not isinstance(params, dict):
        raise ValueError("支付参数必须是对象")
    raw_plan = str(params.get("plan") or "").strip().lower()
    if raw_plan and raw_plan != "plus":
        raise ValueError("当前仅支持 Plus 支付计划")
    retired_keys = sorted(RETIRED_PAYMENT_REQUEST_KEYS.intersection(params))
    if retired_keys:
        raise ValueError(f"已下线的 Team 支付参数: {', '.join(retired_keys)}")


def normalize_payment_link_status(value: Any) -> str:
    return str(value or "").strip().lower()


def normalize_payment_link_output_format(value: Any) -> str:
    text = str(value or "").strip().lower().replace("-", "_")
    if text in {"long_link", "longlink", "admin_long_link", "configured_long_link"}:
        return PAYMENT_LINK_FORMAT_LONG_LINK
    if text in {"paypal", "paypal_url", "paypal_approval", "provider_url"}:
        return PAYMENT_LINK_FORMAT_PAYPAL
    return normalize_payment_link_format(value)


def normalize_payment_link_source(value: Any) -> str:
    text = str(value or "").strip().lower().replace("-", "_")
    if text in {"long_link", "longlink_config", "admin_long_link", "configured_long_link"}:
        return PAYMENT_SOURCE_LONG_LINK
    if text in {
        "long_link_paypal",
        "longlink_paypal",
        "openai_pay_long_link",
        "openai_pay_long_link_api",
        "paypal",
    }:
        return PAYMENT_SOURCE_LONG_LINK_PAYPAL
    return PAYMENT_SOURCE_CHATGPT_HOSTED


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


def payment_link_url_requires_regeneration(value: Any, link_format: Any = None) -> bool:
    normalized_format = normalize_payment_link_output_format(link_format or DEFAULT_PAYMENT_LINK_FORMAT)
    if normalized_format == PAYMENT_LINK_FORMAT_PAYPAL:
        return False
    if normalized_format != PAYMENT_LINK_FORMAT_LONG:
        return False
    return is_default_hosted_checkout_fragment(value)


def normalize_payment_link_params(params: dict[str, Any] | None) -> dict[str, Any]:
    source = params if isinstance(params, dict) else {}
    plan = normalize_payment_link_plan(source.get("plan"))
    country = normalize_checkout_country(source.get("country"))
    currency = normalize_checkout_currency(source.get("currency"), country)
    payment_source = normalize_payment_link_source(source.get("payment_source"))
    payment_link_format = normalize_payment_link_output_format(source.get("payment_link_format"))
    if payment_source in {PAYMENT_SOURCE_LONG_LINK, PAYMENT_SOURCE_LONG_LINK_PAYPAL}:
        plan = "plus"
        payment_link_format = (
            PAYMENT_LINK_FORMAT_LONG_LINK
            if payment_source == PAYMENT_SOURCE_LONG_LINK
            else PAYMENT_LINK_FORMAT_PAYPAL
        )
    return {
        "plan": plan,
        "country": country,
        "currency": currency,
        "proxy": (
            ""
            if payment_source in {PAYMENT_SOURCE_LONG_LINK, PAYMENT_SOURCE_LONG_LINK_PAYPAL}
            else normalize_proxy_url(source.get("proxy")) or ""
        ),
        "payment_link_format": payment_link_format,
        "payment_source": payment_source,
        "profile_hash": str(source.get("profile_hash") or source.get("payment_profile_hash") or "").strip(),
    }


def payment_link_cache_matches(
    cached: dict[str, Any] | None,
    params: dict[str, Any] | None,
) -> bool:
    if not isinstance(cached, dict) or not normalize_payment_link_url(cached.get("url")):
        return False
    if not _is_supported_raw_payment_link_plan(cached.get("plan")):
        return False
    if isinstance(params, dict) and not _is_supported_raw_payment_link_plan(params.get("plan")):
        return False
    expected = normalize_payment_link_params(params)
    cached_plan = normalize_payment_link_plan(cached.get("plan"))
    cached_country = normalize_checkout_country(cached.get("country"))
    cached_currency = normalize_checkout_currency(cached.get("currency"), cached_country)
    cached_proxy = normalize_proxy_url(cached.get("proxy")) or ""
    cached_format = normalize_payment_link_output_format(cached.get("payment_link_format") or PAYMENT_LINK_FORMAT_LONG)
    cached_source = normalize_payment_link_source(
        cached.get("payment_source")
        or (PAYMENT_SOURCE_LONG_LINK_PAYPAL if cached_format == PAYMENT_LINK_FORMAT_PAYPAL else "")
    )
    cached_profile_hash = str(cached.get("profile_hash") or cached.get("payment_profile_hash") or "").strip()
    if payment_link_url_requires_regeneration(cached.get("url"), cached_format):
        return False
    matches = (
        cached_plan == expected["plan"]
        and cached_country == expected["country"]
        and cached_currency == expected["currency"]
        and cached_proxy == expected["proxy"]
        and cached_format == expected["payment_link_format"]
        and cached_source == expected["payment_source"]
    )
    if not matches:
        return False
    if expected["payment_source"] in {PAYMENT_SOURCE_LONG_LINK, PAYMENT_SOURCE_LONG_LINK_PAYPAL}:
        return bool(expected["profile_hash"]) and cached_profile_hash == expected["profile_hash"]
    return True


def normalize_payment_link_url(value: Any, link_format: Any = None) -> str:
    normalized_format = normalize_payment_link_output_format(link_format or DEFAULT_PAYMENT_LINK_FORMAT)
    if normalized_format in {PAYMENT_LINK_FORMAT_PAYPAL, PAYMENT_LINK_FORMAT_LONG_LINK}:
        return str(value or "").strip()
    return normalize_checkout_url_for_link_format(value, normalized_format)


def build_payment_link_cache_payload(
    data: dict[str, Any] | None,
    *,
    source: str,
    fallback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload_source = data if isinstance(data, dict) else {}
    fallback_source = fallback if isinstance(fallback, dict) else {}
    link_format = normalize_payment_link_output_format(
        payload_source.get("payment_link_format")
        or fallback_source.get("payment_link_format")
        or DEFAULT_PAYMENT_LINK_FORMAT
    )
    raw_payment_source = payload_source.get("payment_source")
    if not str(raw_payment_source or "").strip() and link_format == PAYMENT_LINK_FORMAT_PAYPAL:
        raw_payment_source = PAYMENT_SOURCE_LONG_LINK_PAYPAL
    if not str(raw_payment_source or "").strip() and link_format == PAYMENT_LINK_FORMAT_LONG_LINK:
        raw_payment_source = PAYMENT_SOURCE_LONG_LINK
    payment_source = normalize_payment_link_source(raw_payment_source or fallback_source.get("payment_source"))
    fallback_format = normalize_payment_link_output_format(
        fallback_source.get("payment_link_format") or DEFAULT_PAYMENT_LINK_FORMAT
    )
    fallback_payment_source = normalize_payment_link_source(
        fallback_source.get("payment_source")
        or (PAYMENT_SOURCE_LONG_LINK_PAYPAL if fallback_format == PAYMENT_LINK_FORMAT_PAYPAL else "")
    )
    metadata_fallback = (
        fallback_source
        if fallback_format == link_format and fallback_payment_source == payment_source
        else {}
    )
    payload_raw_plan_values = (
        payload_source.get("plan"),
        payload_source.get("chatgpt_checkout_plan"),
    )
    fallback_raw_plan_values = (
        metadata_fallback.get("plan"),
        metadata_fallback.get("chatgpt_checkout_plan"),
    )
    explicit_payload_plans = [value for value in payload_raw_plan_values if str(value or "").strip()]
    plans_to_validate = explicit_payload_plans or [
        value for value in fallback_raw_plan_values if str(value or "").strip()
    ]
    if any(not _is_supported_raw_payment_link_plan(value) for value in plans_to_validate):
        return {}
    url = str(
        payload_source.get("url")
        or payload_source.get("paypal_url")
        or payload_source.get("provider_redirect_url")
        or payload_source.get("checkout_url")
        or payload_source.get("cashier_url")
        or payload_source.get("chatgpt_checkout_url")
        or fallback_source.get("url")
        or fallback_source.get("paypal_url")
        or fallback_source.get("provider_redirect_url")
        or fallback_source.get("checkout_url")
        or fallback_source.get("cashier_url")
        or fallback_source.get("chatgpt_checkout_url")
        or ""
    ).strip()
    url = normalize_payment_link_url(url, link_format)
    if not url:
        return {}

    plan = normalize_payment_link_plan(
        payload_source.get("plan")
        or payload_source.get("chatgpt_checkout_plan")
        or metadata_fallback.get("plan")
        or metadata_fallback.get("chatgpt_checkout_plan")
    )
    country = normalize_checkout_country(
        payload_source.get("country")
        or payload_source.get("chatgpt_checkout_country")
        or metadata_fallback.get("country")
        or metadata_fallback.get("chatgpt_checkout_country")
    )
    currency = normalize_checkout_currency(
        payload_source.get("currency")
        or payload_source.get("chatgpt_checkout_currency")
        or metadata_fallback.get("currency")
        or metadata_fallback.get("chatgpt_checkout_currency"),
        country,
    )

    payload: dict[str, Any] = {
        "url": url,
        "plan": plan,
        "country": country,
        "currency": currency,
        "link_type": str(
            payload_source.get("link_type")
            or metadata_fallback.get("link_type")
            or ""
        ).strip().lower(),
        "proxy": (
            ""
            if payment_source in {PAYMENT_SOURCE_LONG_LINK, PAYMENT_SOURCE_LONG_LINK_PAYPAL}
            else normalize_proxy_url(payload_source.get("proxy") or metadata_fallback.get("proxy")) or ""
        ),
        "payment_link_format": link_format,
        "payment_source": payment_source,
        "profile_hash": str(
            payload_source.get("profile_hash")
            or payload_source.get("payment_profile_hash")
            or metadata_fallback.get("profile_hash")
            or metadata_fallback.get("payment_profile_hash")
            or ""
        ).strip(),
        "source": str(source or payload_source.get("source") or metadata_fallback.get("source") or "").strip(),
        "created_at": str(
            payload_source.get("created_at")
            or metadata_fallback.get("created_at")
            or datetime.now(timezone.utc).isoformat()
        ),
        "generated_at": str(
            payload_source.get("generated_at")
            or metadata_fallback.get("generated_at")
            or payload_source.get("created_at")
            or metadata_fallback.get("created_at")
            or datetime.now(timezone.utc).isoformat()
        ),
    }

    billing = payload_source.get("billing") if isinstance(payload_source.get("billing"), dict) else metadata_fallback.get("billing")
    if isinstance(billing, dict):
        payload["billing"] = billing

    amount = (
        payload_source.get("checkout_amount")
        if "checkout_amount" in payload_source
        else payload_source.get("chatgpt_checkout_amount")
    )
    if amount is None:
        amount = metadata_fallback.get("checkout_amount")
    if amount is not None:
        payload["checkout_amount"] = amount

    amount_is_zero = (
        payload_source.get("checkout_amount_is_zero")
        if "checkout_amount_is_zero" in payload_source
        else payload_source.get("chatgpt_checkout_amount_is_zero")
    )
    if amount_is_zero is None:
        amount_is_zero = metadata_fallback.get("checkout_amount_is_zero")
    if amount_is_zero is not None:
        payload["checkout_amount_is_zero"] = bool(amount_is_zero)

    probe = (
        payload_source.get("checkout_probe")
        if isinstance(payload_source.get("checkout_probe"), dict)
        else payload_source.get("chatgpt_checkout_probe")
    )
    if not isinstance(probe, dict):
        probe = metadata_fallback.get("checkout_probe")
    if isinstance(probe, dict):
        payload["checkout_probe"] = probe

    if link_format == PAYMENT_LINK_FORMAT_PAYPAL:
        payload["paypal_url"] = str(payload_source.get("paypal_url") or metadata_fallback.get("paypal_url") or url).strip()

    for key in (
        "link_type",
        "provider_redirect_url",
        "long_url",
        "stripe_redirect_url",
        "stripe_hosted_url",
        "cs_id",
        "payment_method_id",
        "payment_method_type",
        "processor_entity",
        "remote_job_id",
        "remote_request_id",
        "generated_at",
        "billing_country",
        "payment_locale",
        "amount_display",
        "cs_count",
    ):
        value = payload_source.get(key)
        if value is None or value == "":
            value = metadata_fallback.get(key)
        if value is not None and value != "":
            payload[key] = value

    return payload


def cache_checkout_link_in_extra(extra: dict[str, Any], *, source: str) -> dict[str, Any]:
    if not isinstance(extra, dict):
        return {}
    existing = extra.get("chatgpt_last_payment_link") if isinstance(extra.get("chatgpt_last_payment_link"), dict) else {}
    payload = build_payment_link_cache_payload(extra, source=source, fallback=existing)
    if payload:
        extra["chatgpt_last_payment_link"] = payload
    return extra
