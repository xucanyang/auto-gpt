"""Shared normalization for detected ChatGPT payment methods."""

from __future__ import annotations

import re
from typing import Any, Iterable


PAYMENT_METHOD_NAMES = {
    "card": "信用卡/借记卡",
    "paypal": "PayPal",
    "pix": "Pix",
    "gcash": "GCash",
    "kakao_pay": "Kakao Pay",
    "naver_pay": "Naver Pay",
    "payco": "PAYCO",
    "link": "Link",
    "ideal": "iDEAL",
    "bancontact": "Bancontact",
    "sofort": "Sofort",
    "sepa_debit": "SEPA",
    "giropay": "Giropay",
    "eps": "EPS",
    "p24": "Przelewy24",
    "przelewy24": "Przelewy24",
    "blik": "BLIK",
    "twint": "TWINT",
    "grabpay": "GrabPay",
    "dana": "DANA",
    "ovo": "OVO",
    "shopeepay": "ShopeePay",
    "promptpay": "PromptPay",
    "paynow": "PayNow",
    "konbini": "便利店",
    "payeasy": "Pay-easy",
    "boleto": "Boleto",
    "oxxo": "OXXO",
    "alipay": "支付宝",
    "wechat_pay": "微信支付",
    "upi": "UPI",
    "netbanking": "NetBanking",
}

_METHOD_TYPE_RE = re.compile(r"[^a-z0-9_]+")


def normalize_payment_method_type(value: Any) -> str:
    """Return a stable, language-independent payment method identifier."""

    if isinstance(value, dict):
        value = value.get("id") or value.get("type") or value.get("value")
    normalized = str(value or "").strip().lower().replace("-", "_")
    normalized = re.sub(r"\s+", "_", normalized)
    normalized = _METHOD_TYPE_RE.sub("_", normalized).strip("_")
    return normalized[:80]


def payment_method_label(method_type: Any, fallback: Any = "") -> str:
    normalized = normalize_payment_method_type(method_type)
    canonical_label = PAYMENT_METHOD_NAMES.get(normalized)
    if canonical_label:
        return canonical_label
    fallback_text = str(fallback or "").strip()
    if fallback_text:
        return fallback_text[:120]
    return normalized.replace("_", " ").title()


def normalize_payment_method_entries(
    methods: Any,
    methods_display: Any = None,
) -> list[dict[str, str]]:
    """Normalize raw method IDs and their optional display labels together."""

    raw_methods: Iterable[Any]
    if isinstance(methods, (list, tuple, set)):
        raw_methods = methods
    elif methods:
        raw_methods = (methods,)
    else:
        raw_methods = ()
    display_values = methods_display if isinstance(methods_display, (list, tuple)) else []
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, raw_method in enumerate(raw_methods):
        method_type = normalize_payment_method_type(raw_method)
        if not method_type or method_type in seen:
            continue
        seen.add(method_type)
        fallback = display_values[index] if index < len(display_values) else ""
        entries.append({
            "type": method_type,
            "label": payment_method_label(method_type, fallback),
        })
    return entries
