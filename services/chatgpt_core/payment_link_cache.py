"""Shared helpers for ChatGPT checkout/payment-link cache payloads."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
import time
from typing import Any
from urllib.parse import urlsplit

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

PIX_EXPIRED_CLEANED_STATUS = "expired_cleaned"
PIX_PAID_CLEANED_STATUS = "paid_cleaned"
PIX_CANCELLED_CLEANED_STATUS = "cancelled_cleaned"
PIX_CLEANED_STATUSES = frozenset({
    PIX_EXPIRED_CLEANED_STATUS,
    PIX_PAID_CLEANED_STATUS,
    PIX_CANCELLED_CLEANED_STATUS,
})
# UPI links are independent QR instruments.  Keep their tombstones distinct so
# a historical PIX cleanup marker can never be mistaken for an UPI link (and so
# operators can tell which payment rail was cleared from the account metadata).
UPI_EXPIRED_CLEANED_STATUS = "upi_expired_cleaned"
UPI_PAID_CLEANED_STATUS = "upi_paid_cleaned"
UPI_CANCELLED_CLEANED_STATUS = "upi_cancelled_cleaned"
UPI_CLEANED_STATUSES = frozenset({
    UPI_EXPIRED_CLEANED_STATUS,
    UPI_PAID_CLEANED_STATUS,
    UPI_CANCELLED_CLEANED_STATUS,
})
PAYMENT_LINK_CLEANED_STATUSES = frozenset({*PIX_CLEANED_STATUSES, *UPI_CLEANED_STATUSES})

PAYMENT_LINK_STATUS_LABELS = {
    "invalid": "无效",
    "already_paid": "已经支付过",
    "amount_not_zero": "非0元订单",
    "not_usd": "非指定区域订单",
    "precheck_failed": "支付链接核验失败",
    "pix_submitted": "已提交 PIX 管理端",
    PIX_EXPIRED_CLEANED_STATUS: "已过期清理",
    PIX_PAID_CLEANED_STATUS: "已支付清理",
    PIX_CANCELLED_CLEANED_STATUS: "支付已取消清理",
    UPI_EXPIRED_CLEANED_STATUS: "UPI 已过期清理",
    UPI_PAID_CLEANED_STATUS: "UPI 已支付清理",
    UPI_CANCELLED_CLEANED_STATUS: "UPI 支付已取消清理",
}
PAYMENT_LINK_REGENERATE_STATUSES = {
    "invalid",
    "amount_not_zero",
    "not_usd",
    "precheck_failed",
    # A Stripe PIX instruction link is single-use from the management
    # service's perspective, even while its QR deadline has not elapsed.
    "pix_submitted",
    *PIX_CLEANED_STATUSES,
    *UPI_CLEANED_STATUSES,
}
PAYMENT_LINK_STATUS_SYNC_STATUSES = {"already_paid"}
PAYMENT_LINK_FORMAT_PAYPAL = "paypal_url"
PAYMENT_LINK_FORMAT_LONG_LINK = "long_link"
PAYMENT_SOURCE_CHATGPT_HOSTED = "chatgpt_hosted"
PAYMENT_SOURCE_LONG_LINK_PAYPAL = "long_link_paypal"
PAYMENT_SOURCE_LONG_LINK = "long_link"
PAYMENT_LINK_PLAN_PLUS = "plus"
PAYMENT_LINK_PLAN_TEAM = "team"
PAYMENT_LINK_GENERATION_PLUS = "plus_checkout"
PAYMENT_LINK_GENERATION_TEAM = "team_checkout"
TEAM_DEFAULT_CHECKOUT_UI_MODE = "hosted"
TEAM_CHECKOUT_UI_MODES = frozenset({"hosted", "custom"})
PAYMENT_LINK_PLUS_PLAN_ALIASES = frozenset({
    "plus",
    "plus_checkout",
    "chatgptplusplan",
    "chatgpt_plus_plan",
})
PAYMENT_LINK_TEAM_PLAN_ALIASES = frozenset({
    "team",
    "team_checkout",
    "chatgptteamplan",
    "chatgpt_team_plan",
})
MAX_PAYMENT_LINK_EXPIRES_AT_EPOCH = 253_402_300_799
PIX_LINK_REUSE_GUARD_SECONDS = 60
UPI_LINK_REUSE_GUARD_SECONDS = 60
RETIRED_PAYMENT_REQUEST_KEYS = frozenset({
    "promo_code",
    "promoCode",
    "workspace_name",
    "workspaceName",
    "team_workspace_name",
    "teamWorkspaceName",
    "seat_quantity",
    "seatQuantity",
    "team_seat_quantity",
    "teamSeatQuantity",
    "price_interval",
    "priceInterval",
    "team_price_interval",
    "teamPriceInterval",
    "cancel_url",
    "cancelUrl",
    "team_plan_data",
    "teamPlanData",
    "checkout_proxy_region",
    "checkoutProxyRegion",
    "checkout_ui_mode",
    "checkoutUiMode",
    "checkoutMode",
})


def normalize_payment_link_plan(value: Any) -> str:
    raw = str(value or "").strip().lower().replace("-", "_")
    if raw in PAYMENT_LINK_TEAM_PLAN_ALIASES:
        return PAYMENT_LINK_PLAN_TEAM
    return PAYMENT_LINK_PLAN_PLUS


def _is_supported_raw_payment_link_plan(value: Any) -> bool:
    raw_plan = str(value or "").strip().lower().replace("-", "_")
    return not raw_plan or raw_plan in PAYMENT_LINK_PLUS_PLAN_ALIASES | PAYMENT_LINK_TEAM_PLAN_ALIASES


def validate_plus_payment_request_params(params: dict[str, Any] | None) -> None:
    """Reject retired product inputs before any account, task, or cache work."""
    if params is None:
        return
    if not isinstance(params, dict):
        raise ValueError("支付参数必须是对象")
    raw_plan = str(params.get("plan") or "").strip().lower().replace("-", "_")
    if raw_plan and raw_plan not in PAYMENT_LINK_PLUS_PLAN_ALIASES:
        raise ValueError("当前仅支持 Plus 支付计划")
    retired_keys = sorted(RETIRED_PAYMENT_REQUEST_KEYS.intersection(params))
    if retired_keys:
        raise ValueError(f"已下线的 Team 支付参数: {', '.join(retired_keys)}")


def _team_param_source(params: dict[str, Any] | None) -> dict[str, Any]:
    source = params if isinstance(params, dict) else {}
    nested = source.get("team_plan_data") or source.get("teamPlanData")
    nested = dict(nested) if isinstance(nested, dict) else {}
    return {
        "workspace_name": str(
            nested.get("workspace_name")
            or nested.get("workspaceName")
            or source.get("workspace_name")
            or source.get("team_workspace_name")
            or source.get("teamWorkspaceName")
            or ""
        ).strip(),
        "price_interval": str(
            nested.get("price_interval")
            or nested.get("priceInterval")
            or source.get("price_interval")
            or source.get("team_price_interval")
            or source.get("teamPriceInterval")
            or "month"
        ).strip().lower(),
        "seat_quantity": source.get(
            "seat_quantity",
            source.get(
                "team_seat_quantity",
                source.get("teamSeatQuantity", nested.get("seat_quantity", nested.get("seatQuantity", 2))),
            ),
        ),
        "promo_code": str(source.get("promo_code") or source.get("promoCode") or "").strip(),
        "cancel_url": str(source.get("cancel_url") or source.get("cancelUrl") or "").strip(),
        "plan_name": str(source.get("plan_name") or source.get("planName") or "chatgptteamplan").strip(),
    }


def normalize_team_checkout_proxy_region(params: dict[str, Any] | None) -> str:
    source = params if isinstance(params, dict) else {}
    return str(
        source.get("checkout_proxy_region")
        or source.get("checkoutProxyRegion")
        or ""
    ).strip().upper()


def normalize_team_checkout_ui_mode(params: dict[str, Any] | None) -> str:
    source = params if isinstance(params, dict) else {}
    raw_mode = source.get("checkout_ui_mode")
    if raw_mode in (None, ""):
        raw_mode = source.get("checkoutUiMode")
    if raw_mode in (None, ""):
        raw_mode = source.get("checkoutMode")
    mode = str(raw_mode or "").strip().lower()
    if mode:
        return mode

    # Team links created before the mode became task-scoped did not persist the
    # field. The ChatGPT checkout route identifies those custom-mode links so
    # they cannot be reused as the new hosted default.
    raw_url = str(
        source.get("url")
        or source.get("long_url")
        or source.get("checkout_url")
        or ""
    ).strip()
    try:
        parsed = urlsplit(raw_url)
    except ValueError:
        parsed = None
    if (
        parsed is not None
        and str(parsed.hostname or "").lower() in {"chatgpt.com", "www.chatgpt.com"}
        and parsed.path.startswith("/checkout/openai_llc/")
    ):
        return "custom"
    return TEAM_DEFAULT_CHECKOUT_UI_MODE


def _team_param_presence(params: dict[str, Any] | None) -> set[str]:
    """Return Team fields explicitly supplied by the caller.

    Batch requests may intentionally omit a field so the long-link admin
    profile supplies its default.  Matching must distinguish an omitted field
    from an explicit default such as ``seat_quantity=2``.
    """

    source = params if isinstance(params, dict) else {}
    nested = source.get("team_plan_data") or source.get("teamPlanData")
    nested = nested if isinstance(nested, dict) else {}
    aliases = {
        "workspace_name": ("workspace_name", "workspaceName", "team_workspace_name", "teamWorkspaceName"),
        "price_interval": ("price_interval", "priceInterval", "team_price_interval", "teamPriceInterval"),
        "seat_quantity": ("seat_quantity", "seatQuantity", "team_seat_quantity", "teamSeatQuantity"),
    }
    present: set[str] = set()
    for canonical, keys in aliases.items():
        if any(key in source and source.get(key) not in (None, "") for key in keys[:2]) or any(
            key in nested and nested.get(key) not in (None, "") for key in keys[:2]
        ) or any(
            key in source and source.get(key) not in (None, "") for key in keys[2:]
        ):
            present.add(canonical)
    if any(key in source and source.get(key) not in (None, "") for key in ("promo_code", "promoCode")):
        present.add("promo_code_digest")
    if any(key in source and source.get(key) not in (None, "") for key in ("cancel_url", "cancelUrl")):
        present.add("cancel_url")
    if any(key in source and source.get(key) not in (None, "") for key in ("plan_name", "planName")):
        present.add("plan_name")
    if any(key in source and source.get(key) not in (None, "") for key in ("checkout_proxy_region", "checkoutProxyRegion")):
        present.add("checkout_proxy_region")
    if any(key in source and source.get(key) not in (None, "") for key in ("checkout_ui_mode", "checkoutUiMode", "checkoutMode")):
        present.add("checkout_ui_mode")
    return present


def validate_team_payment_request_params(params: dict[str, Any] | None) -> None:
    if not isinstance(params, dict):
        raise ValueError("支付参数必须是对象")
    if normalize_payment_link_plan(params.get("plan")) != PAYMENT_LINK_PLAN_TEAM:
        raise ValueError("Team 支付请求必须指定 plan=team")
    values = _team_param_source(params)
    explicit = _team_param_presence(params)
    checkout_proxy_region = normalize_team_checkout_proxy_region(params)
    if not re.fullmatch(r"[A-Z]{2}", checkout_proxy_region):
        raise ValueError("Team 动态 IP 国家必须显式选择两位国家代码")
    checkout_ui_mode = normalize_team_checkout_ui_mode(params)
    if checkout_ui_mode not in TEAM_CHECKOUT_UI_MODES:
        raise ValueError("Team checkout_ui_mode 必须是 hosted 或 custom")
    # Business fields may inherit the long-link admin profile. Proxy country is
    # mandatory and checkout mode always has a task-scoped hosted default.
    if "workspace_name" in explicit and len(values["workspace_name"]) > 256:
        raise ValueError("Team Workspace 名称不能超过 256 个字符")
    if "price_interval" in explicit and values["price_interval"] not in {"month", "year"}:
        raise ValueError("Team price_interval 必须是 month 或 year")
    if "seat_quantity" in explicit:
        try:
            seats = int(values["seat_quantity"])
        except (TypeError, ValueError) as exc:
            raise ValueError("Team seat_quantity 必须是整数") from exc
        if seats < 2 or seats > 1000:
            raise ValueError("Team seat_quantity 必须在 2 到 1000 之间")
    if len(values["promo_code"]) > 256:
        raise ValueError("Team promo_code 过长")
    if values["cancel_url"] and not re.match(r"^https?://[^\s]+$", values["cancel_url"], re.I):
        raise ValueError("Team cancel_url 必须是 HTTP(S) 地址")


def validate_payment_link_request_params(params: dict[str, Any] | None) -> None:
    source = params if isinstance(params, dict) else {}
    raw_plan = str(source.get("plan") or "").strip().lower().replace("-", "_")
    if raw_plan in PAYMENT_LINK_TEAM_PLAN_ALIASES:
        validate_team_payment_request_params(source)
        return
    validate_plus_payment_request_params(source)


def payment_link_generation_kind(params: dict[str, Any] | None) -> str:
    return (
        PAYMENT_LINK_GENERATION_TEAM
        if normalize_payment_link_plan((params or {}).get("plan")) == PAYMENT_LINK_PLAN_TEAM
        else PAYMENT_LINK_GENERATION_PLUS
    )


def payment_link_variant_key(params: dict[str, Any] | None) -> str:
    source = params if isinstance(params, dict) else {}
    plan = normalize_payment_link_plan(source.get("plan"))
    country = normalize_checkout_country(source.get("country") or source.get("billing_country"))
    currency = normalize_checkout_currency(source.get("currency"), country)
    canonical: dict[str, Any] = {
        "generation_kind": payment_link_generation_kind(source),
        "plan": plan,
        "country": country,
        "currency": currency,
        "profile_hash": str(source.get("profile_hash") or source.get("payment_profile_hash") or "").strip(),
        "checkout_proxy_region": normalize_team_checkout_proxy_region(source) if plan == PAYMENT_LINK_PLAN_TEAM else "",
    }
    if plan == PAYMENT_LINK_PLAN_TEAM:
        team = _team_param_source(source)
        canonical["checkout_ui_mode"] = normalize_team_checkout_ui_mode(source)
        try:
            seats = int(team["seat_quantity"] or 0)
        except (TypeError, ValueError):
            seats = 0
        canonical["team"] = {
            "workspace_name": team["workspace_name"],
            "price_interval": team["price_interval"],
            "seat_quantity": seats,
            "promo_code_digest": str(source.get("promo_code_digest") or "").strip()
            or (hashlib.sha256(team["promo_code"].encode("utf-8")).hexdigest() if team["promo_code"] else ""),
            "cancel_url": team["cancel_url"],
            "plan_name": team["plan_name"] or "chatgptteamplan",
        }
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()


def normalize_payment_link_status(value: Any) -> str:
    return str(value or "").strip().lower()


PAYMENT_LINK_QR_TYPES = frozenset({"pix", "upi"})
_GENERIC_PAYMENT_LINK_TYPES = frozenset({
    "hosted",
    "chatgpt",
    "chatgpt_hosted",
    "stripe_hosted",
    "checkout",
    "payment",
    "pay",
    "long",
})


def normalize_payment_link_type(value: Any) -> str:
    """Normalize provider payment types used by the current-link contract."""

    text = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "qr": "pix",
        "pix_qr": "pix",
        "upi_qr": "upi",
        "upi_qr_code": "upi",
    }
    return aliases.get(text, text)


def payment_link_type_from_payload(payload: dict[str, Any] | None) -> str:
    """Infer a payment type from explicit provider fields before URL shape.

    Long-link responses historically omitted ``link_type`` in a few paths while
    still carrying ``payment_method_type``.  Inferring the type here keeps cache,
    list filters, and cleanup in agreement without guessing from arbitrary URLs.
    """

    source = payload if isinstance(payload, dict) else {}
    explicit_values = [
        normalize_payment_link_type(source.get(key))
        for key in ("link_type", "payment_method_type", "payment_type")
    ]
    # A concrete QR method wins over a generic transport label such as
    # ``hosted``.  This is the automatic classification boundary used by the
    # scanner and account-list filter.
    for value in explicit_values:
        if value in PAYMENT_LINK_QR_TYPES:
            return value
    # The canonical Stripe instruction path is stronger evidence than a
    # generic ``link_type=hosted`` value.  This also covers old rows that did
    # not persist ``payment_method_type`` at all.
    for key in ("url", "long_url", "provider_redirect_url", "stripe_redirect_url"):
        value = str(source.get(key) or "").strip()
        if not value:
            continue
        try:
            path = (urlsplit(value).path or "").lower()
        except (TypeError, ValueError):
            path = ""
        if "/upi/instructions/" in path:
            return "upi"
        if "/qr/instructions/" in path:
            return "pix"
    for value in explicit_values:
        if value and value not in _GENERIC_PAYMENT_LINK_TYPES:
            return value
    for value in explicit_values:
        if value:
            return value
    return ""


def _normalize_qr_expiry_epoch(value: Any) -> int | None:
    return normalize_payment_link_expires_at(value)


def extract_payment_link_qr_expires_at(
    payload: Any,
    *,
    payment_type: Any = "",
    depth: int = 0,
) -> int | None:
    """Extract the concrete QR deadline without borrowing checkout expiry.

    UPI's authoritative value is
    ``next_action.upi_handle_redirect_or_display_qr_code.qr_code.expires_at``.
    Stripe's hosted instructions page also exposes the same value as a
    top-level ``expires_at``.  PIX uses the analogous
    ``pix_display_qr_code`` shape.  We intentionally do not recurse into
    ``checkout_session.expires_at`` because that is a different lifetime.
    """

    if depth > 12:
        return None
    normalized_type = normalize_payment_link_type(payment_type)
    if isinstance(payload, dict):
        qr_code = payload.get("qr_code")
        if isinstance(qr_code, dict):
            direct = _normalize_qr_expiry_epoch(qr_code.get("expires_at"))
            if direct is not None:
                return direct
        next_action = payload.get("next_action")
        if isinstance(next_action, dict):
            if normalized_type == "upi":
                action_keys = ("upi_handle_redirect_or_display_qr_code",)
            elif normalized_type == "pix":
                action_keys = ("pix_display_qr_code",)
            else:
                action_keys = (
                    "upi_handle_redirect_or_display_qr_code",
                    "pix_display_qr_code",
                )
            for action_key in action_keys:
                action = next_action.get(action_key)
                if not isinstance(action, dict):
                    continue
                nested_qr = action.get("qr_code")
                if isinstance(nested_qr, dict):
                    direct = _normalize_qr_expiry_epoch(nested_qr.get("expires_at"))
                    if direct is not None:
                        return direct
                direct = _normalize_qr_expiry_epoch(action.get("expires_at"))
                if direct is not None:
                    return direct
        # Hosted UPI/PIX instruction payloads put the QR deadline at the root.
        if normalized_type in PAYMENT_LINK_QR_TYPES:
            direct = _normalize_qr_expiry_epoch(payload.get("expires_at"))
            if direct is not None:
                return direct
        for key, value in payload.items():
            if key in {"checkout_session", "expires_at", "qr_code", "next_action"}:
                continue
            found = extract_payment_link_qr_expires_at(
                value,
                payment_type=normalized_type,
                depth=depth + 1,
            )
            if found is not None:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = extract_payment_link_qr_expires_at(
                value,
                payment_type=normalized_type,
                depth=depth + 1,
            )
            if found is not None:
                return found
    return None


def normalize_payment_link_expiry_source(value: Any, payment_type: Any = "") -> str:
    source = str(value or "").strip().lower().replace("-", "_")
    normalized_type = normalize_payment_link_type(payment_type)
    if normalized_type == "upi":
        if source in {"upi_qr_code", "qr_code", "qr"}:
            return "upi_qr_code"
        if source == "checkout_session":
            # Kept for reading old upstream rows; new UPI cleanup prefers QR
            # expiry whenever a concrete QR value is available.
            return source
    if normalized_type == "pix" and source in {"pix_qr_code", "qr_code", "qr"}:
        return "pix_qr_code"
    return source[:64]


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


def normalize_payment_link_expires_at(value: Any) -> int | None:
    """Accept only an explicit provider-issued Unix timestamp."""
    if isinstance(value, bool):
        return None
    text = str(value or "").strip()
    if not re.fullmatch(r"\d{1,12}", text):
        return None
    epoch = int(text)
    if epoch <= 0 or epoch > MAX_PAYMENT_LINK_EXPIRES_AT_EPOCH:
        return None
    return epoch


def payment_link_expires_soon(cached: dict[str, Any] | None, *, now: float | None = None) -> bool:
    """QR-based links are reusable only while their provider deadline is safe."""
    if not isinstance(cached, dict):
        return False
    if payment_link_type_from_payload(cached) not in PAYMENT_LINK_QR_TYPES:
        return False
    expires_at = normalize_payment_link_expires_at(cached.get("link_expires_at"))
    if expires_at is None:
        return False
    try:
        current = float(time.time() if now is None else now)
    except (TypeError, ValueError):
        return False
    guard_seconds = (
        UPI_LINK_REUSE_GUARD_SECONDS
        if payment_link_type_from_payload(cached) == "upi"
        else PIX_LINK_REUSE_GUARD_SECONDS
    )
    return expires_at <= current + guard_seconds


def payment_link_requires_regeneration(cached: dict[str, Any] | None) -> bool:
    if not isinstance(cached, dict):
        return False
    return (
        normalize_payment_link_status(cached.get("link_status")) in PAYMENT_LINK_REGENERATE_STATUSES
        or payment_link_expires_soon(cached)
    )


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
        payment_link_format = (
            PAYMENT_LINK_FORMAT_LONG_LINK
            if payment_source == PAYMENT_SOURCE_LONG_LINK
            else PAYMENT_LINK_FORMAT_PAYPAL
        )
    normalized: dict[str, Any] = {
        "plan": plan,
        "generation_kind": payment_link_generation_kind({"plan": plan}),
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
        "checkout_proxy_region": normalize_team_checkout_proxy_region(source) if plan == PAYMENT_LINK_PLAN_TEAM else "",
        "checkout_ui_mode": normalize_team_checkout_ui_mode(source) if plan == PAYMENT_LINK_PLAN_TEAM else "",
    }
    if plan == PAYMENT_LINK_PLAN_TEAM:
        team = _team_param_source(source)
        try:
            seats = int(team["seat_quantity"] or 0)
        except (TypeError, ValueError):
            seats = 0
        normalized.update(
            {
                "workspace_name": team["workspace_name"],
                "price_interval": team["price_interval"],
                "seat_quantity": seats,
                "promo_code_digest": str(source.get("promo_code_digest") or "").strip()
                or (hashlib.sha256(team["promo_code"].encode("utf-8")).hexdigest() if team["promo_code"] else ""),
                "cancel_url": team["cancel_url"],
                "plan_name": team["plan_name"] or "chatgptteamplan",
            }
        )
    normalized["variant_key"] = str(source.get("variant_key") or "").strip() or payment_link_variant_key(
        {**source, **normalized}
    )
    return normalized


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
    cached_generation_kind = str(cached.get("generation_kind") or payment_link_generation_kind(cached)).strip()
    cached_variant_key = str(cached.get("variant_key") or "").strip()
    if payment_link_url_requires_regeneration(cached.get("url"), cached_format):
        return False
    if payment_link_requires_regeneration(cached):
        return False
    matches = (
        cached_plan == expected["plan"]
        and cached_generation_kind == expected["generation_kind"]
        and cached_country == expected["country"]
        and cached_currency == expected["currency"]
        and cached_proxy == expected["proxy"]
        and cached_format == expected["payment_link_format"]
        and cached_source == expected["payment_source"]
        and normalize_team_checkout_proxy_region(cached) == expected["checkout_proxy_region"]
        and (
            expected["plan"] != PAYMENT_LINK_PLAN_TEAM
            or normalize_team_checkout_ui_mode(cached) == expected["checkout_ui_mode"]
        )
    )
    if not matches:
        return False
    if expected["plan"] == PAYMENT_LINK_PLAN_TEAM:
        if str(cached.get("generation_kind") or "").strip().lower() != PAYMENT_LINK_GENERATION_TEAM:
            return False
        # The complete key includes the long-link profile hash.  Before the
        # remote profile is frozen, compare only explicitly supplied business
        # fields and defer the full-key check to the worker.
        if cached_variant_key and expected["variant_key"] and expected.get("profile_hash") and cached_variant_key != expected["variant_key"]:
            return False
        cached_team = normalize_payment_link_params(cached)
        if not str(cached_team.get("workspace_name") or "").strip():
            return False
        explicit_fields = _team_param_presence(params)
        for key in (
            "workspace_name",
            "price_interval",
            "seat_quantity",
            "promo_code_digest",
            "cancel_url",
            "plan_name",
        ):
            if key not in explicit_fields:
                continue
            if cached_team.get(key) != expected.get(key):
                return False
    if expected["payment_source"] in {PAYMENT_SOURCE_LONG_LINK, PAYMENT_SOURCE_LONG_LINK_PAYPAL}:
        return not expected["profile_hash"] or cached_profile_hash == expected["profile_hash"]
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
    if plan == PAYMENT_LINK_PLAN_TEAM:
        team_generation_kind = str(
            payload_source.get("generation_kind")
            or metadata_fallback.get("generation_kind")
            or ""
        ).strip().lower()
        team_candidate = _team_param_source({**metadata_fallback, **payload_source})
        # Historical Team/Business caches predate the current checkout-only
        # contract and do not carry a frozen workspace variant.  Do not silently
        # reinterpret them as a reusable Team checkout.
        if team_generation_kind != PAYMENT_LINK_GENERATION_TEAM or not team_candidate["workspace_name"]:
            return {}
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

    inferred_link_type = payment_link_type_from_payload({**fallback_source, **payload_source})
    explicit_link_type = normalize_payment_link_type(
        payload_source.get("link_type") or metadata_fallback.get("link_type")
    )
    method_link_type = normalize_payment_link_type(
        payload_source.get("payment_method_type") or metadata_fallback.get("payment_method_type")
    )
    if method_link_type in PAYMENT_LINK_QR_TYPES:
        normalized_link_type = method_link_type
    elif explicit_link_type in PAYMENT_LINK_QR_TYPES:
        normalized_link_type = explicit_link_type
    elif inferred_link_type in PAYMENT_LINK_QR_TYPES and (
        not explicit_link_type or explicit_link_type in _GENERIC_PAYMENT_LINK_TYPES
    ):
        normalized_link_type = inferred_link_type
    else:
        normalized_link_type = explicit_link_type or method_link_type or inferred_link_type
    payload: dict[str, Any] = {
        "url": url,
        "plan": plan,
        "country": country,
        "currency": currency,
        "link_type": normalized_link_type.strip().lower(),
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
    payload["generation_kind"] = payment_link_generation_kind({"plan": plan})
    if plan == PAYMENT_LINK_PLAN_TEAM:
        team_source = {
            **metadata_fallback,
            **payload_source,
        }
        team = _team_param_source(team_source)
        try:
            team_seats = int(team["seat_quantity"] or 0)
        except (TypeError, ValueError):
            team_seats = 0
        payload.update(
            {
                "workspace_name": team["workspace_name"],
                "price_interval": team["price_interval"],
                "seat_quantity": team_seats,
                "promo_code_digest": str(payload_source.get("promo_code_digest") or metadata_fallback.get("promo_code_digest") or "").strip()
                or (hashlib.sha256(team["promo_code"].encode("utf-8")).hexdigest() if team["promo_code"] else ""),
                "cancel_url": team["cancel_url"],
                "plan_name": team["plan_name"] or "chatgptteamplan",
                "checkout_proxy_region": normalize_team_checkout_proxy_region(team_source),
                "checkout_ui_mode": normalize_team_checkout_ui_mode(team_source),
            }
        )
    payload["variant_key"] = str(
        payload_source.get("variant_key") or metadata_fallback.get("variant_key") or ""
    ).strip() or payment_link_variant_key(payload)

    fallback_url = normalize_payment_link_url(
        fallback_source.get("url")
        or fallback_source.get("paypal_url")
        or fallback_source.get("provider_redirect_url")
        or fallback_source.get("checkout_url")
        or fallback_source.get("cashier_url"),
        link_format,
    )
    status_source: dict[str, Any] = {}
    if str(payload_source.get("link_status") or "").strip():
        status_source = payload_source
    elif fallback_url and fallback_url == url:
        # Normalizing an unchanged cache must retain its operational status.
        # A newly returned URL must start clean, rather than inheriting the old
        # PIX single-use marker from fallback metadata.
        status_source = metadata_fallback
    for key in ("link_status", "link_status_reason", "link_status_updated_at", "pix_submitted_at"):
        value = status_source.get(key)
        if value is not None and value != "":
            payload[key] = value

    if payload["link_type"] in PAYMENT_LINK_QR_TYPES:
        raw_expires_at = payload_source.get("link_expires_at")
        expiry_metadata = payload_source
        if raw_expires_at is None or raw_expires_at == "":
            raw_expires_at = metadata_fallback.get("link_expires_at")
            expiry_metadata = metadata_fallback
        qr_expires_at = extract_payment_link_qr_expires_at(
            payload_source,
            payment_type=payload["link_type"],
        )
        if qr_expires_at is None:
            qr_expires_at = extract_payment_link_qr_expires_at(
                metadata_fallback,
                payment_type=payload["link_type"],
            )
        # For UPI the QR deadline is authoritative even when an older upstream
        # response also carried a Checkout Session expiry.
        if payload["link_type"] == "upi" and qr_expires_at is not None:
            raw_expires_at = qr_expires_at
        source_value = expiry_metadata.get("link_expiry_source")
        normalized_source = normalize_payment_link_expiry_source(source_value, payload["link_type"])
        if payload["link_type"] == "upi" and qr_expires_at is None and normalized_source == "checkout_session":
            # Checkout Session lifetime is not the UPI QR lifetime.  Treat an
            # explicitly tagged scalar as unknown instead of extending a
            # five-minute QR code to the session deadline.
            raw_expires_at = None
        link_expires_at = normalize_payment_link_expires_at(raw_expires_at)
        if link_expires_at is not None:
            payload["link_expires_at"] = link_expires_at
            if payload["link_type"] == "upi" and qr_expires_at is not None:
                normalized_source = "upi_qr_code"
            elif payload["link_type"] == "upi" and not normalized_source:
                # Historical long-link rows persisted the QR scalar before
                # provenance was added to the schema.
                normalized_source = "upi_qr_code"
            if normalized_source:
                payload["link_expiry_source"] = normalized_source

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
        "link_expiry_source",
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
        "generation_kind",
        "plan_name",
        "promo_code_digest",
        "variant_key",
        "workspace_name",
        "price_interval",
        "seat_quantity",
        "cancel_url",
        "checkout_proxy_region",
        "checkout_ui_mode",
    ):
        value = payload_source.get(key)
        if value is None or value == "":
            value = metadata_fallback.get(key)
        if value is not None and value != "":
            payload[key] = value

    # The metadata compatibility loop above must not overwrite a concrete UPI
    # QR provenance with an older Checkout Session marker.
    inferred_after_metadata = payment_link_type_from_payload(payload)
    if inferred_after_metadata in PAYMENT_LINK_QR_TYPES:
        payload["link_type"] = inferred_after_metadata
    if payload.get("link_type") == "upi":
        qr_expiry = extract_payment_link_qr_expires_at(payload_source, payment_type="upi")
        if qr_expiry is None:
            qr_expiry = extract_payment_link_qr_expires_at(metadata_fallback, payment_type="upi")
        if qr_expiry is not None:
            payload["link_expires_at"] = qr_expiry
            payload["link_expiry_source"] = "upi_qr_code"
        elif normalize_payment_link_expiry_source(payload.get("link_expiry_source"), "upi") == "checkout_session":
            payload.pop("link_expires_at", None)
            payload.pop("link_expiry_source", None)
        elif normalize_payment_link_expires_at(payload.get("link_expires_at")) is not None:
            payload["link_expiry_source"] = "upi_qr_code"

    return payload


def cache_checkout_link_in_extra(extra: dict[str, Any], *, source: str) -> dict[str, Any]:
    if not isinstance(extra, dict):
        return {}
    existing = extra.get("chatgpt_last_payment_link") if isinstance(extra.get("chatgpt_last_payment_link"), dict) else {}
    payload = build_payment_link_cache_payload(extra, source=source, fallback=existing)
    if payload:
        extra["chatgpt_last_payment_link"] = payload
        variants = extra.get("chatgpt_payment_link_variants")
        if not isinstance(variants, dict):
            variants = {}
        variant_key = str(payload.get("variant_key") or payment_link_variant_key(payload)).strip()
        if variant_key:
            variants[variant_key] = dict(payload)
            extra["chatgpt_payment_link_variants"] = variants
    return extra


def payment_link_cache_for_params(
    extra: dict[str, Any] | None,
    params: dict[str, Any] | None,
) -> dict[str, Any]:
    """Read a variant-specific cache without losing legacy Plus pointers."""

    source = extra if isinstance(extra, dict) else {}
    expected_key = payment_link_variant_key(params)
    variants = source.get("chatgpt_payment_link_variants")
    if isinstance(variants, dict):
        candidate = variants.get(expected_key)
        if isinstance(candidate, dict):
            return dict(candidate)
    current = source.get("chatgpt_last_payment_link")
    if isinstance(current, dict) and payment_link_cache_matches(current, params):
        return dict(current)
    if normalize_payment_link_plan((params or {}).get("plan")) == PAYMENT_LINK_PLAN_PLUS:
        legacy = source.get("chatgpt_paypal_url")
        if isinstance(legacy, dict) and payment_link_cache_matches(legacy, params):
            return dict(legacy)
    return {}


def store_payment_link_variant(
    extra: dict[str, Any],
    payload: dict[str, Any],
    *,
    make_current: bool = True,
) -> dict[str, Any]:
    """Persist a normalized variant and keep the historical current pointer."""

    if not isinstance(extra, dict) or not isinstance(payload, dict) or not payload:
        return extra if isinstance(extra, dict) else {}
    variants = extra.get("chatgpt_payment_link_variants")
    if not isinstance(variants, dict):
        variants = {}
    variant_key = str(payload.get("variant_key") or payment_link_variant_key(payload)).strip()
    if variant_key:
        variants[variant_key] = dict(payload)
        extra["chatgpt_payment_link_variants"] = variants
    if make_current:
        extra["chatgpt_last_payment_link"] = dict(payload)
    return extra
