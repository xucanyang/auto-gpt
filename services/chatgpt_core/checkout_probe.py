"""Checkout amount probe for ChatGPT Plus subscriptions.

This is the non-payment, non-session subset of the old GoPay flow. It keeps
only the read-only checkout inspection used by subscription preflight logic.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Optional
from urllib.parse import parse_qsl, urlsplit

from curl_cffi import requests as cffi_requests

from core.proxy_utils import build_requests_proxy_config
from core.timezone import beijing_log_time
from services.chatgpt_core.payment import (
    CHATGPT_CHECKOUT_BASE_URL,
    DEFAULT_CHECKOUT_COUNTRY,
    DEFAULT_CHECKOUT_CURRENCY,
    DEFAULT_STRIPE_PK,
    PAYMENT_CHECKOUT_URL,
    _extract_oai_did,
    normalize_checkout_country,
    normalize_checkout_currency,
)

STRIPE_API = "https://api.stripe.com"
STRIPE_VERSION_BASE = "2025-03-31.basil"
STRIPE_VERSION_HOSTED = "2020-08-27;custom_checkout_beta=v1; checkout_server_update_beta=v1; checkout_manual_approval_preview=v1"
DEFAULT_STRIPE_RUNTIME_VERSION = "0711c6012f"
KNOWN_PUBLISHABLE_KEYS = (
    DEFAULT_STRIPE_PK,
    "pk_live_51HOrSwC6h1nxGoI3lTAgRjYVrz4dU3fVOabyCcKR3pbEJguCVAlqCxdxCUvoRh1XWwRacViovU3kLKvpkjh7IqkW00iXQsjo3n",
)
DEFAULT_TIMEOUT = 30
CHECKOUT_PROBE_TIMEOUT = 8
MIDTRANS_SNAP_SIGNING_KEY = "1feab063-bf3f-4025-90bf-3be6fa4f4cc2"
MIDTRANS_SNAP_SOURCE_HEADERS = {
    "X-Source": "snap",
    "X-Source-App-Type": "redirection",
    "X-Source-Version": "2.3.0",
}


class CheckoutProbeError(RuntimeError):
    pass


@dataclass
class CheckoutProbeSession:
    session_id: str
    account_id: int
    email: str
    country: str = DEFAULT_CHECKOUT_COUNTRY
    currency: str = DEFAULT_CHECKOUT_CURRENCY
    proxy: str = ""
    proxy_source: str = "probe"
    checkout_url: str = ""
    stripe_checkout_url: str = ""
    processor_entity: str = ""
    logs: list[str] = field(default_factory=list)
    browser_profile: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)


class CheckoutProbeRunner:
    def __init__(self, session: CheckoutProbeSession, account: Any):
        self.s = session
        self.account = account
        self.proxy = self.s.proxy or ""
        self.profile = self._complete_profile(self.s.browser_profile)
        self.ext = cffi_requests.Session(impersonate=str(self.profile.get("impersonate") or "chrome146"))
        self.ext.headers.update({
            "User-Agent": str(self.profile.get("ua") or _build_chatgpt_headers(account).get("User-Agent", "")),
            "Accept-Language": str(self.profile.get("accept_language") or "id-ID,id;q=0.9,en-US;q=0.8"),
        })
        proxies = build_requests_proxy_config(self.proxy or None)
        if proxies:
            self.ext.proxies = proxies

    @staticmethod
    def _complete_profile(profile: dict[str, Any]) -> dict[str, Any]:
        profile = dict(profile or {})
        profile.setdefault("name", "probe")
        profile.setdefault("impersonate", "chrome146")
        profile.setdefault("ua", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36")
        profile.setdefault("accept_language", "id-ID,id;q=0.9,en-US;q=0.8")
        profile.setdefault("locale", "id-ID")
        profile.setdefault("stripe_locale", "id")
        profile.setdefault("timezone", "Asia/Jakarta")
        profile.setdefault("checkout_probe_timeout", CHECKOUT_PROBE_TIMEOUT)
        return profile

    def log(self, message: str) -> None:
        text = str(message or "").strip()
        if not text:
            return
        self.s.logs.append(f"[{beijing_log_time()}] {text}")
        self.s.logs = self.s.logs[-200:]

    def _stripe_pk(self) -> str:
        value = str((getattr(self.account, "extra", {}) or {}).get("stripe_publishable_key") or DEFAULT_STRIPE_PK).strip()
        if not value:
            raise CheckoutProbeError("缺少 Stripe publishable key，请在账号 extra.stripe_publishable_key 中配置")
        return value

    def _processor_entity(self) -> str:
        if self.s.processor_entity:
            return self.s.processor_entity
        extra = getattr(self.account, "extra", {}) or {}
        configured = str(
            extra.get("checkout_processor_entity")
            or extra.get("chatgpt_checkout_processor_entity")
            or ""
        ).strip()
        if configured:
            self.s.processor_entity = configured
            return configured
        entity = _extract_processor_entity(self.s.checkout_url, default="")
        if entity:
            self.s.processor_entity = entity
            return entity
        self.s.processor_entity = "openai_llc"
        return self.s.processor_entity

    def _resolve_checkout(self) -> str:
        checkout_input = str(self.s.checkout_url or "").strip()
        if checkout_input:
            if checkout_input.startswith("http://") or checkout_input.startswith("https://"):
                cs_id, stripe_url = parse_checkout_url(checkout_input)
                if cs_id:
                    self.s.checkout_url = checkout_input
                    self.s.stripe_checkout_url = stripe_url
                    self.s.processor_entity = _extract_processor_entity(checkout_input, default=self._processor_entity())
                    return cs_id
                self.s.stripe_checkout_url = checkout_input
                return self._chatgpt_create_checkout()
            cs_id, stripe_url = parse_checkout_url(checkout_input)
            self.s.stripe_checkout_url = stripe_url
            if not self.s.checkout_url:
                self.s.checkout_url = CHATGPT_CHECKOUT_BASE_URL + cs_id
            self.s.processor_entity = _extract_processor_entity(self.s.checkout_url, default=self._processor_entity())
            return cs_id
        return self._chatgpt_create_checkout()

    def _chatgpt_create_checkout(self) -> str:
        checkout_url, cs_id, processor_entity = _create_hosted_checkout(
            self.account,
            country=self.s.country,
            currency=self.s.currency,
            proxy=self.proxy,
            profile=self.profile,
        )
        self.s.checkout_url = checkout_url
        self.s.processor_entity = processor_entity or _extract_processor_entity(checkout_url, default=self._processor_entity())
        _, stripe_checkout_url = parse_checkout_url(checkout_url)
        self.s.stripe_checkout_url = stripe_checkout_url
        if cs_id:
            self.s.session_id = self.s.session_id or cs_id
        return cs_id or checkout_url

    def _fetch_publishable_key(self, cs_id: str) -> str:
        configured = self._stripe_pk()
        last_err = ""
        for key in [configured, *[item for item in KNOWN_PUBLISHABLE_KEYS if item != configured]]:
            body = {
                "key": key,
                "_stripe_version": STRIPE_VERSION_BASE,
                "browser_locale": str(self.profile.get("stripe_locale") or self.profile.get("locale") or "id"),
            }
            r = self.ext.post(
                f"{STRIPE_API}/v1/payment_pages/{cs_id}/init",
                data=body,
                headers=_stripe_headers(self.profile),
                timeout=15,
            )
            if r.status_code == 200:
                return key
            last_err = f"key={key[:28]}... status={r.status_code} body={r.text[:300]}"
            if r.status_code in (400, 401, 403):
                continue
        raise CheckoutProbeError(f"无法为当前 checkout session 探测可用的 Stripe publishable key: {last_err}")

    def _stripe_init_checkout(self, cs_id: str, stripe_pk: str) -> tuple[dict[str, Any], str, dict[str, Any]]:
        stripe_js_id = str(uuid.uuid4())
        request_timeout = int(self.profile.get("checkout_probe_timeout") or DEFAULT_TIMEOUT)
        body = {
            "key": stripe_pk,
            "eid": "NA",
            "browser_locale": str(self.profile.get("locale") or "id-ID"),
            "browser_timezone": str(self.profile.get("timezone") or "Asia/Jakarta"),
            "redirect_type": "url",
        }
        r = self.ext.post(
            f"{STRIPE_API}/v1/payment_pages/{cs_id}/init",
            data=body,
            headers=_stripe_headers(self.profile),
            timeout=request_timeout,
        )
        _raise_for_checkout_status(r, "stripe payment_pages init")
        init_data = r.json() or {}
        ctx = {
            "stripe_js_id": stripe_js_id,
            "locale": init_data.get("locale") or self.profile.get("stripe_locale") or "id",
            "browser_locale": body["browser_locale"],
            "browser_timezone": body["browser_timezone"],
            "currency": str(init_data.get("currency") or self.s.currency or "idr").lower(),
            "checkout_amount": ((init_data.get("total_summary") or {}).get("due")
                                if (init_data.get("total_summary") or {}).get("due") is not None
                                else (init_data.get("invoice") or {}).get("amount_due")),
            "payment_method_types": _extract_payment_method_types(init_data),
            "config_id": init_data.get("config_id", ""),
            "init_checksum": init_data.get("init_checksum", ""),
            "return_url": init_data.get("return_url") or "",
            "stripe_hosted_url": init_data.get("stripe_hosted_url") or self.s.checkout_url or "",
            "stripe_version": STRIPE_VERSION_HOSTED,
        }
        return init_data, STRIPE_VERSION_HOSTED, ctx

    @staticmethod
    def checkout_amount_from_init(init_resp: dict[str, Any]) -> tuple[Any, str]:
        total_summary = init_resp.get("total_summary") if isinstance(init_resp, dict) else {}
        if isinstance(total_summary, dict) and total_summary.get("due") is not None:
            return total_summary.get("due"), "total_summary.due"
        invoice = init_resp.get("invoice") if isinstance(init_resp, dict) else {}
        if isinstance(invoice, dict) and invoice.get("amount_due") is not None:
            return invoice.get("amount_due"), "invoice.amount_due"
        return None, ""


def _account_extra(account: Any) -> dict[str, Any]:
    extra = getattr(account, "extra", {}) or {}
    return extra if isinstance(extra, dict) else {}


def _build_chatgpt_headers(account: Any) -> dict[str, str]:
    if not getattr(account, "access_token", ""):
        raise CheckoutProbeError("账号缺少 access_token")
    extra = _account_extra(account)
    headers = {
        "Authorization": f"Bearer {account.access_token}",
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "oai-language": "zh-CN",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
        ),
    }
    cookies = str(getattr(account, "cookies", "") or "")
    if not cookies:
        session_token = str(getattr(account, "session_token", "") or extra.get("session_token") or "").strip()
        if session_token:
            cookies = (
                f"__Secure-next-auth.session-token={session_token}; "
                f"__Secure-authjs.session-token={session_token}"
            )
    if cookies:
        headers["Cookie"] = cookies
        oai_did = _extract_oai_did(cookies)
        if oai_did:
            headers["oai-device-id"] = oai_did
    chatgpt_account_id = str(
        extra.get("account_id")
        or extra.get("chatgpt_account_id")
        or extra.get("workspace_id")
        or ""
    ).strip()
    if chatgpt_account_id:
        headers["chatgpt-account-id"] = chatgpt_account_id
    return headers


def _build_profile_chatgpt_headers(account: Any, profile: dict[str, Any]) -> dict[str, str]:
    headers = _build_chatgpt_headers(account)
    headers["User-Agent"] = str(profile.get("ua") or headers.get("User-Agent") or "")
    headers["Accept-Language"] = str(profile.get("accept_language") or "id-ID,id;q=0.9,en-US;q=0.8")
    headers["oai-language"] = str(profile.get("locale") or "id-ID")
    return headers


def _post_chatgpt_with_profile(
    account: Any,
    url: str,
    *,
    json_body: dict[str, Any],
    proxy: str = "",
    profile: dict[str, Any],
    extra_headers: Optional[dict[str, str]] = None,
):
    headers = _build_profile_chatgpt_headers(account, profile)
    if extra_headers:
        headers.update(extra_headers)
    return cffi_requests.post(
        url,
        headers=headers,
        json=json_body,
        proxies=build_requests_proxy_config(proxy or None),
        timeout=DEFAULT_TIMEOUT,
        impersonate=str(profile.get("impersonate") or "chrome146"),
    )


def _get_chatgpt_with_profile(
    account: Any,
    url: str,
    *,
    params: Optional[dict[str, Any]] = None,
    proxy: str = "",
    profile: dict[str, Any],
    extra_headers: Optional[dict[str, str]] = None,
):
    headers = _build_profile_chatgpt_headers(account, profile)
    if extra_headers:
        headers.update(extra_headers)
    return cffi_requests.get(
        url,
        headers=headers,
        params=params,
        proxies=build_requests_proxy_config(proxy or None),
        timeout=DEFAULT_TIMEOUT,
        impersonate=str(profile.get("impersonate") or "chrome146"),
    )


def _raise_for_checkout_status(resp: Any, context: str) -> None:
    status_code = int(getattr(resp, "status_code", 0) or 0)
    if status_code < 400:
        return
    text = str(getattr(resp, "text", "") or "")
    message = text[:800]
    try:
        payload = resp.json()
        if isinstance(payload, dict):
            error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
            detail = payload.get("detail") or payload.get("message")
            message = str(error.get("message") or detail or message).strip()
            if error.get("param"):
                message = f"{message} (param={error.get('param')})"
    except Exception:
        pass
    raise CheckoutProbeError(f"{context} HTTP {status_code}: {message}")


def parse_checkout_url(raw: str) -> tuple[str, str]:
    raw = str(raw or "").strip()
    parsed = urlsplit(raw)
    retired_plans = {"team", "business", "enterprise"}
    path_segments = {
        segment.strip().lower()
        for segment in str(parsed.path or "").split("/")
        if segment.strip()
    }
    query = {
        str(key or "").strip().lower(): str(value or "").strip().lower()
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
    }
    explicit_plan = next(
        (
            query[key]
            for key in ("plan", "plan_type", "plan_name", "subscription_plan")
            if key in query
        ),
        "",
    )
    if path_segments.intersection(retired_plans) or explicit_plan in retired_plans:
        raise CheckoutProbeError("Plus checkout 不接受 Team、Business 或 Enterprise 计划")
    lowered = raw.lower()
    if "chatgptteamplan" in lowered or "team_workspace_purchase" in lowered:
        raise CheckoutProbeError("Plus checkout 不接受 Team、Business 或 Enterprise 计划")
    if raw.startswith("http://") or raw.startswith("https://"):
        if "checkout.stripe.com" in raw:
            match = re.search(r"(cs_(?:live|test)_[A-Za-z0-9]+)", raw)
            if match:
                return match.group(1), raw
        match = re.search(r"(cs_(?:live|test)_[A-Za-z0-9]+)", raw)
        if match:
            cs_id = match.group(1)
            return cs_id, f"https://checkout.stripe.com/c/pay/{cs_id}"
        return "", raw
    match = re.search(r"(cs_(?:live|test)_[A-Za-z0-9]+)", raw)
    if not match:
        raise CheckoutProbeError(f"无法从 checkout 输入中提取 session id: {raw[:120]}")
    cs_id = match.group(1)
    if "checkout.stripe.com" in raw:
        return cs_id, raw
    return cs_id, f"https://checkout.stripe.com/c/pay/{cs_id}"


def _extract_processor_entity(raw: str, default: str = "openai_llc") -> str:
    raw = str(raw or "").strip()
    match = re.search(r"chatgpt\.com/checkout/([A-Za-z0-9_]+)/", raw)
    if match:
        return match.group(1)
    match = re.search(r"[?&]processor_entity=([A-Za-z0-9_]+)", raw)
    if match:
        return match.group(1)
    return default


def _processor_entity_from_checkout_data(data: dict[str, Any], fallback_url: str = "") -> str:
    configured = str(data.get("processor_entity") or "").strip()
    if configured:
        return configured
    checkout_url = str(
        data.get("url")
        or data.get("checkout_url")
        or data.get("cashier_url")
        or fallback_url
        or ""
    ).strip()
    return _extract_processor_entity(checkout_url, default="")


def _create_hosted_checkout(account: Any, *, country: str, currency: str, proxy: str, profile: dict[str, Any]) -> tuple[str, str, str]:
    extra = _account_extra(account)
    processor_entity = str(
        extra.get("checkout_processor_entity")
        or extra.get("chatgpt_checkout_processor_entity")
        or ""
    ).strip()
    body = {
        "plan_name": "chatgptplusplan",
        "billing_details": {"country": country, "currency": currency},
        "promo_campaign": {
            "promo_campaign_id": "plus-1-month-free",
            "is_coupon_from_query_param": False,
        },
        "checkout_ui_mode": "hosted",
    }
    if processor_entity:
        body["processor_entity"] = processor_entity
    r = _post_chatgpt_with_profile(
        account,
        PAYMENT_CHECKOUT_URL,
        json_body=body,
        proxy=proxy,
        profile=profile,
        extra_headers={
            "x-openai-target-path": "/backend-api/payments/checkout",
            "x-openai-target-route": "/backend-api/payments/checkout",
            "oai-session-id": str(uuid.uuid4()),
        },
    )
    _raise_for_checkout_status(r, "chatgpt checkout create")
    data = r.json()
    checkout_url = str(data.get("url") or data.get("checkout_url") or data.get("cashier_url") or "").strip()
    cs_id = str(data.get("checkout_session_id") or data.get("session_id") or data.get("id") or "").strip()
    response_entity = _processor_entity_from_checkout_data(data, fallback_url=checkout_url) or processor_entity or "openai_llc"
    if isinstance(extra, dict):
        extra["chatgpt_checkout_processor_entity"] = response_entity
        extra["checkout_processor_entity"] = response_entity
    if checkout_url and not cs_id:
        cs_id, _ = parse_checkout_url(checkout_url)
    if checkout_url and cs_id:
        return checkout_url, cs_id, response_entity
    if cs_id:
        return f"https://chatgpt.com/checkout/{response_entity}/{cs_id}", cs_id, response_entity
    raise CheckoutProbeError(f"checkout create: bad response {data!r}")


def _normalize_checkout_amount_value(value: Any) -> str:
    text = str(value if value is not None else "").strip()
    if not text:
        return ""
    try:
        decimal_value = Decimal(text)
    except (InvalidOperation, ValueError):
        return text
    if decimal_value == decimal_value.to_integral_value():
        return str(decimal_value.to_integral_value())
    return format(decimal_value.normalize(), "f")


def _extract_payment_method_types(payload: dict[str, Any]) -> list[str]:
    payment_method_types = payload.get("payment_method_types")
    if isinstance(payment_method_types, list) and payment_method_types:
        return [str(item) for item in payment_method_types if item]
    specs = payload.get("payment_method_specs")
    if isinstance(specs, list):
        out = [str(spec.get("type")) for spec in specs if isinstance(spec, dict) and spec.get("type")]
        if out:
            return out
    return ["card"]


def _is_zero_checkout_amount(value: Any) -> bool:
    text = str(value if value is not None else "").strip()
    if not text:
        return False
    try:
        return Decimal(text) == 0
    except (InvalidOperation, ValueError):
        return text == "0"


def _stripe_headers(profile: Optional[dict[str, Any]] = None) -> dict[str, str]:
    profile = profile or {}
    return {
        "User-Agent": str(profile.get("ua") or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"),
        "Accept": "application/json",
        "Accept-Language": str(profile.get("accept_language") or "id-ID,id;q=0.9,en-US;q=0.8"),
        "Origin": "https://js.stripe.com",
        "Referer": "https://js.stripe.com/",
    }


def _midtrans_snap_signature(path: str, body_text: str, timestamp: str) -> str:
    base = f"{path}:{timestamp}:{body_text or ''}"
    digest = hmac.new(
        MIDTRANS_SNAP_SIGNING_KEY.encode("utf-8"),
        base.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return digest


def _raise_for_stripe_status(resp: Any, context: str) -> None:
    _raise_for_checkout_status(resp, context)


def probe_chatgpt_checkout_amount(
    account: Any,
    *,
    checkout_url: str = "",
    country: str = DEFAULT_CHECKOUT_COUNTRY,
    currency: str = DEFAULT_CHECKOUT_CURRENCY,
    proxy: str = "",
    browser_profile: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    profile = dict(browser_profile or {})
    profile.setdefault("checkout_probe_timeout", CHECKOUT_PROBE_TIMEOUT)
    session = CheckoutProbeSession(
        session_id=f"probe_{uuid.uuid4().hex}",
        account_id=0,
        email=str(getattr(account, "email", "") or ""),
        country=normalize_checkout_country(country),
        currency=normalize_checkout_currency(currency, country),
        proxy=str(proxy or ""),
        proxy_source="probe",
        checkout_url=str(checkout_url or "").strip(),
        browser_profile=profile,
    )
    runner = CheckoutProbeRunner(session, account)
    cs_id = runner._resolve_checkout()
    stripe_pk = runner._fetch_publishable_key(cs_id)
    init_resp, stripe_version, init_ctx = runner._stripe_init_checkout(cs_id, stripe_pk)
    amount, amount_source = runner.checkout_amount_from_init(init_resp)
    currency_text = str(init_ctx.get("currency") or init_resp.get("currency") or session.currency or "").lower()
    return {
        "checkout_url": session.checkout_url,
        "stripe_checkout_url": session.stripe_checkout_url,
        "checkout_session_id": cs_id,
        "processor_entity": session.processor_entity,
        "stripe_publishable_key_prefix": stripe_pk[:28] + "..." if stripe_pk else "",
        "stripe_version": stripe_version,
        "amount": amount,
        "amount_text": _normalize_checkout_amount_value(amount),
        "amount_source": amount_source,
        "amount_is_zero": _is_zero_checkout_amount(amount),
        "currency": currency_text,
        "payment_method_types": init_ctx.get("payment_method_types") or [],
        "logs": list(session.logs),
    }
