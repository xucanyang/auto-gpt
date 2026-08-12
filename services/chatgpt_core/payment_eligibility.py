"""Read-only ChatGPT checkout capability probes.

The two probes in this module intentionally share the checkout preparation
chain but have independent business answers and persistence-friendly evidence.
Neither probe confirms a payment method, approves a checkout, or starts a
provider redirect.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from typing import Any, Callable, Mapping

from curl_cffi import requests as cffi_requests

from core.dynamic_proxy import dynamic_proxy_supported
from core.proxy_utils import (
    build_requests_proxy_config,
    get_global_dynamic_proxy_template,
    normalize_proxy_url,
    resolve_task_proxy_candidates,
)
from services.chatgpt_core.account_fingerprint import resolve_account_browser_fingerprint
from services.chatgpt_core.checkout_probe import probe_chatgpt_checkout_amount
from services.chatgpt_core.utils import coerce_browser_fingerprint


ZERO_AMOUNT_KIND = "zero_amount_eligibility"
GCASH_KIND = "gcash_payment_method"
PROFILE = {
    "plan": "chatgptplusplan",
    "billing_country": "PH",
    "currency": "PHP",
    "checkout_ui_mode": "custom",
    "promotion": "plus-1-month-free",
    "proxy_chain": {
        "checkout": "US",
        "promotion": "VN",
        "taxes": "US",
    },
}

_CPMT_RE = re.compile(r"^cpmt_[A-Za-z0-9]+$")
_SESSION_RE = re.compile(r"^(?:cs|oaics)_[A-Za-z0-9_]+$")
_MAX_JSON_DEPTH = 8
_DEFAULT_ATTEMPTS = 2
_DEFAULT_TIMEOUT_SECONDS = 30


class PaymentEligibilityProbeError(RuntimeError):
    """A technical failure; business negatives are returned as states."""


class PaymentEligibilityProtocolError(PaymentEligibilityProbeError):
    pass


class PaymentEligibilityHttpError(PaymentEligibilityProbeError):
    def __init__(self, stage: str, status_code: int, detail: str = "") -> None:
        self.stage = str(stage or "").strip()
        self.status_code = int(status_code or 0)
        self.detail = _safe_text(detail)
        suffix = f": {self.detail}" if self.detail else ""
        super().__init__(f"{self.stage} HTTP {self.status_code}{suffix}")


class PaymentEligibilityInterruption(PaymentEligibilityProbeError):
    """Reserved for callers that need to distinguish local cancellation."""


@dataclass(frozen=True)
class CheckoutIdentity:
    session_id: str
    provider: str
    checkout_provider: str
    processor_entity: str
    checkout_url: str


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_text(value: Any, limit: int = 240) -> str:
    text = str(value or "").strip()
    if len(text) > limit:
        return text[:limit]
    return text


def _response_error_detail(response: Any) -> str:
    try:
        payload = response.json()
    except Exception:
        return ""
    if not isinstance(payload, dict):
        return ""
    candidates: list[Any] = [
        payload.get("detail"),
        payload.get("message"),
        payload.get("msg"),
    ]
    error = payload.get("error")
    if isinstance(error, dict):
        candidates.extend((error.get("message"), error.get("detail"), error.get("code"), error.get("type")))
    else:
        candidates.append(error)
    for candidate in candidates:
        if isinstance(candidate, (str, int, float)):
            detail = _safe_text(candidate)
            if detail:
                return detail
    return ""


def _is_explicit_promotion_unavailable(exc: PaymentEligibilityHttpError) -> bool:
    detail = str(exc.detail or "").strip().lower().rstrip(".")
    return (
        exc.stage == "promotion 刷新"
        and exc.status_code == 403
        and detail == "this promotion is not available"
    )


def _account_extra(account: Any) -> dict[str, Any]:
    try:
        value = account.get_extra() if callable(getattr(account, "get_extra", None)) else getattr(account, "extra", {})
    except Exception:
        value = {}
    return value if isinstance(value, dict) else {}


def _access_token(account: Any) -> str:
    extra = _account_extra(account)
    value = str(
        extra.get("access_token")
        or extra.get("accessToken")
        or extra.get("webAccessToken")
        or getattr(account, "token", "")
        or getattr(account, "access_token", "")
        or ""
    ).strip()
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    return value


def _account_cookie(account: Any, extra: dict[str, Any]) -> str:
    return str(
        getattr(account, "cookies", "")
        or extra.get("cookies")
        or extra.get("cookie_header")
        or extra.get("cookie")
        or ""
    ).strip()


def _account_identifier(account: Any, extra: dict[str, Any]) -> str:
    return str(
        extra.get("account_id")
        or extra.get("chatgpt_account_id")
        or extra.get("workspace_id")
        or getattr(account, "user_id", "")
        or ""
    ).strip()


def _browser_profile(account: Any) -> dict[str, Any]:
    extra = _account_extra(account)
    existing = resolve_account_browser_fingerprint(extra)
    fingerprint = coerce_browser_fingerprint(
        existing or None,
        accept_language="en-US,en;q=0.9,zh-CN;q=0.8",
    )
    return {
        "device_id": str(fingerprint.device_id or "").strip(),
        "ua": str(fingerprint.user_agent or "").strip(),
        "accept_language": str(fingerprint.accept_language or "en-US,en;q=0.9"),
        "locale": "en-US",
        "impersonate": str(fingerprint.impersonate or "chrome146"),
        "timezone": "America/New_York",
        "browser_fingerprint_signature": hashlib.sha256(
            json.dumps(
                {
                    "device_id": fingerprint.device_id,
                    "ua": fingerprint.user_agent,
                    "accept_language": fingerprint.accept_language,
                    "impersonate": fingerprint.impersonate,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:16],
    }


def _infer_provider(session_id: str, checkout_provider: Any = "") -> str:
    value = str(session_id or "").strip()
    if value.startswith("oaics_"):
        return "open_ai"
    if value.startswith("cs_"):
        return "stripe"
    provider = str(checkout_provider or "").strip().lower().replace("-", "_")
    return provider if provider in {"open_ai", "stripe"} else ""


def _walk_dicts(value: Any, *, depth: int = 0):
    if depth > _MAX_JSON_DEPTH:
        return
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _walk_dicts(nested, depth=depth + 1)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_dicts(nested, depth=depth + 1)


def extract_checkout_state(payload: Any, expected_id: str = "") -> dict[str, Any]:
    """Find the OAICS checkout state without relying on response presentation."""
    expected = str(expected_id or "").strip()
    for node in _walk_dicts(payload):
        state = node.get("checkout_state")
        if isinstance(state, dict):
            state_id = str(state.get("id") or "").strip()
            if not expected or not state_id or state_id == expected:
                return dict(state)
        node_id = str(node.get("id") or "").strip()
        if isinstance(node.get("total"), dict) and node_id.startswith("oaics_"):
            if not expected or node_id == expected:
                return dict(node)
    return {}


def _collect_nested_value(payload: Any, key: str) -> list[Any]:
    values: list[Any] = []
    for node in _walk_dicts(payload):
        if key in node:
            values.append(node.get(key))
    return values


def apply_checkout_response(checkout: dict[str, Any], payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return checkout
    state = extract_checkout_state(payload, str(checkout.get("session_id") or ""))
    if state:
        checkout["checkout_state"] = state
    for key in ("payment_method_types", "custom_payment_methods"):
        values = _collect_nested_value(payload, key)
        if values:
            latest = next((item for item in reversed(values) if isinstance(item, list)), None)
            if latest is not None:
                checkout[key] = list(latest)
    for node in _walk_dicts(payload):
        for key in ("checkout_provider", "processor_entity", "publishable_key", "customer_session_client_secret"):
            value = str(node.get(key) or "").strip()
            if value:
                checkout[key] = value
    return checkout


def extract_processor_entity(payload: Any, default: str = "") -> str:
    for node in _walk_dicts(payload):
        value = str(node.get("processor_entity") or node.get("processorEntity") or "").strip()
        if value:
            return value
    return str(default or "").strip()


def unique_cpmt_ids(checkout_or_payload: Any) -> tuple[str, ...]:
    values: list[str] = []
    candidates = _collect_nested_value(checkout_or_payload, "custom_payment_methods")
    if isinstance(checkout_or_payload, dict) and isinstance(checkout_or_payload.get("custom_payment_methods"), list):
        candidates.append(checkout_or_payload.get("custom_payment_methods"))
    for collection in candidates:
        if not isinstance(collection, list):
            continue
        for item in collection:
            if not isinstance(item, dict):
                continue
            method_id = str(item.get("id") or "").strip()
            if _CPMT_RE.fullmatch(method_id) and method_id not in values:
                values.append(method_id)
    return tuple(values)


def _minor_amount(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        raise PaymentEligibilityProtocolError("结账金额缺失")
    text = str(value).strip()
    if not text:
        raise PaymentEligibilityProtocolError("结账金额缺失")
    try:
        decimal_value = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise PaymentEligibilityProtocolError("结账金额格式无效") from exc
    if decimal_value != decimal_value.to_integral_value():
        raise PaymentEligibilityProtocolError("结账金额不是整数 minor units")
    return int(decimal_value)


def oaics_amount(checkout: Mapping[str, Any]) -> tuple[int, str]:
    state = checkout.get("checkout_state")
    if not isinstance(state, dict):
        raise PaymentEligibilityProtocolError("OAICS checkout_state 缺失")
    total = state.get("total")
    total_total = total.get("total") if isinstance(total, dict) else None
    amount = total_total.get("minorUnitsAmount") if isinstance(total_total, dict) else None
    if amount is None:
        raise PaymentEligibilityProtocolError("OAICS total.total.minorUnitsAmount 缺失")
    currency = str(state.get("currency") or checkout.get("currency") or "").strip().upper()
    if not currency:
        raise PaymentEligibilityProtocolError("OAICS 货币缺失")
    return _minor_amount(amount), currency


def _digest_method_ids(method_ids: tuple[str, ...]) -> str:
    if not method_ids:
        return ""
    return hashlib.sha256(",".join(method_ids).encode("utf-8")).hexdigest()[:16]


def payment_eligibility_stage_regions(
    kind: str,
    settings: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Return the requested stage exits without exposing runtime proxy URLs."""
    normalized_kind = str(kind or "").strip().lower()
    if normalized_kind not in {ZERO_AMOUNT_KIND, GCASH_KIND}:
        raise ValueError(f"unsupported eligibility kind: {kind}")
    promotion_region = str(PROFILE["proxy_chain"]["promotion"] or "VN").strip().upper()
    if normalized_kind == ZERO_AMOUNT_KIND:
        requested_region = str(
            (settings or {}).get("promotion_proxy_country_code") or promotion_region
        ).strip().upper()
        if len(requested_region) == 2 and requested_region.isascii() and requested_region.isalpha():
            promotion_region = requested_region
    return {
        "checkout": str(PROFILE["proxy_chain"]["checkout"] or "US").strip().upper(),
        "promotion": promotion_region,
        "taxes": str(PROFILE["proxy_chain"]["taxes"] or "US").strip().upper(),
    }


def _redacted_proxy_settings(kind: str, settings: Mapping[str, Any]) -> dict[str, Any]:
    mode = str(settings.get("proxy_mode") or "global").strip().lower()
    return {
        "mode": mode,
        "stage_regions": payment_eligibility_stage_regions(kind, settings),
    }


def _resolve_proxy_chain(kind: str, settings: Mapping[str, Any]) -> dict[str, str]:
    """Resolve independent stage exits; raw URLs never leave this function."""
    values = dict(settings or {})
    stage_regions = payment_eligibility_stage_regions(kind, values)
    mode = str(values.get("proxy_mode") or "global").strip().lower()
    explicit = str(values.get("proxy") or "").strip()
    if mode in {"direct", "none", "no_proxy"}:
        return {stage: "" for stage in stage_regions}

    template = explicit
    if mode in {"global", "dynamic", ""} and not template:
        template = str(get_global_dynamic_proxy_template() or "").strip()
    if mode == "pool":
        chain: dict[str, str] = {}
        for stage, region in stage_regions.items():
            candidates = resolve_task_proxy_candidates(
                {
                    "proxy_mode": "pool",
                    "proxy_country_code": region,
                    "proxy_failover": False,
                    "proxy_max_candidates": values.get("proxy_max_candidates") or 1,
                    "proxy_min_score": values.get("proxy_min_score") or 0,
                },
                default_mode="direct",
            )
            chain[stage] = str(candidates[0][0] if candidates else "").strip()
        return chain
    if not template:
        raise PaymentEligibilityProbeError("缺少动态代理模板；需配置账号任务代理")
    # Specified proxies can be fixed exits. Dynamic mode must retain the
    # region marker because each stage owns a different required country.
    if not dynamic_proxy_supported(template):
        if mode == "dynamic":
            raise PaymentEligibilityProbeError("动态代理模板缺少 region-XX 标记")
        runtime_proxy = normalize_proxy_url(template) or ""
        if not runtime_proxy:
            raise PaymentEligibilityProbeError("指定代理解析后为空")
        return {stage: runtime_proxy for stage in stage_regions}
    retention = values.get("dynamic_proxy_ip_retention_minutes")
    chain = {}
    for stage, region in stage_regions.items():
        try:
            candidate_params = {
                "proxy_mode": "dynamic",
                "proxy": template,
                "proxy_country_code": region,
                # The outer probe attempt owns retries. Resolve and validate one
                # fresh stage SID instead of preparing candidates we discard.
                "proxy_failover": False,
                "dynamic_proxy_max_attempts": 1,
                "dynamic_proxy_ip_retention_minutes": retention,
            }
            for key in (
                "dynamic_proxy_probe_enabled",
                "dynamic_proxy_require_country_match",
                "dynamic_proxy_probe_timeout_seconds",
            ):
                if key in values:
                    candidate_params[key] = values.get(key)
            candidates = resolve_task_proxy_candidates(
                candidate_params,
                default_mode="direct",
                target="chatgpt",
            )
        except Exception as exc:
            raise PaymentEligibilityProbeError(f"{stage} 动态代理不可用: {_safe_text(exc)}") from exc
        runtime_proxy = str(candidates[0][0] if candidates else "").strip()
        if not runtime_proxy:
            raise PaymentEligibilityProbeError(f"{stage} 动态代理解析后为空")
        chain[stage] = runtime_proxy
    return chain


class _CheckoutClient:
    def __init__(self, account: Any, profile: dict[str, Any], stop_checker: Callable[[], None] | None = None):
        self.account = account
        self.profile = profile
        self.stop_checker = stop_checker
        self.extra = _account_extra(account)
        self.access_token = _access_token(account)
        if not self.access_token:
            raise PaymentEligibilityProbeError("账号缺少 Access Token")
        self.cookies = _account_cookie(account, self.extra)
        self.account_identifier = _account_identifier(account, self.extra)

    def checkpoint(self) -> None:
        if self.stop_checker is not None:
            self.stop_checker()

    def _session(self, proxy: str):
        session = cffi_requests.Session(impersonate=str(self.profile.get("impersonate") or "chrome146"))
        headers = {
            "User-Agent": str(self.profile.get("ua") or ""),
            "Accept": "*/*",
            "Accept-Language": str(self.profile.get("accept_language") or "en-US,en;q=0.9"),
            "Authorization": f"Bearer {self.access_token}",
            "Origin": "https://chatgpt.com",
            "Referer": "https://chatgpt.com/",
            "Content-Type": "application/json",
            "oai-device-id": str(self.profile.get("device_id") or ""),
            "oai-language": "en-US",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
        }
        if self.cookies:
            headers["Cookie"] = self.cookies
        if self.account_identifier:
            headers["chatgpt-account-id"] = self.account_identifier
        session.headers.update(headers)
        proxies = build_requests_proxy_config(proxy or None)
        if proxies:
            session.proxies = proxies
        return session

    def post(
        self,
        path: str,
        body: dict[str, Any],
        proxy: str,
        stage: str,
        *,
        referer: str = "",
    ) -> dict[str, Any]:
        self.checkpoint()
        session = self._session(proxy)
        try:
            headers = {
                "x-openai-target-path": path,
                "x-openai-target-route": path,
                "Referer": referer or "https://chatgpt.com/",
            }
            try:
                response = session.post(
                    f"https://chatgpt.com{path}",
                    json=body,
                    timeout=_DEFAULT_TIMEOUT_SECONDS,
                    headers=headers,
                )
            except Exception as exc:
                raise PaymentEligibilityProbeError(f"{stage} 网络失败: {_safe_text(exc)}") from exc
            self.checkpoint()
            status = int(getattr(response, "status_code", 0) or 0)
            if status >= 400:
                detail = _response_error_detail(response)
                raise PaymentEligibilityHttpError(stage, status, detail)
            try:
                payload = response.json() or {}
            except Exception as exc:
                raise PaymentEligibilityProtocolError(f"{stage} 返回不是 JSON") from exc
            if not isinstance(payload, dict):
                raise PaymentEligibilityProtocolError(f"{stage} 返回格式无效")
            return payload
        finally:
            session.close()


def _create_checkout(client: _CheckoutClient, proxy: str) -> tuple[dict[str, Any], CheckoutIdentity]:
    body = {
        "entry_point": "all_plans_pricing_modal",
        "plan_name": PROFILE["plan"],
        "billing_details": {
            "country": PROFILE["billing_country"],
            "currency": PROFILE["currency"],
        },
        "promo_campaign": {
            "promo_campaign_id": PROFILE["promotion"],
            "is_coupon_from_query_param": False,
        },
        "checkout_ui_mode": PROFILE["checkout_ui_mode"],
    }
    payload = client.post("/backend-api/payments/checkout", body, proxy, "checkout 创建")
    session_id = str(
        payload.get("checkout_session_id") or payload.get("session_id") or payload.get("id") or ""
    ).strip()
    if not _SESSION_RE.fullmatch(session_id):
        raise PaymentEligibilityProtocolError("checkout 未返回受支持的 session id")
    provider = _infer_provider(session_id, payload.get("checkout_provider"))
    checkout_provider = str(payload.get("checkout_provider") or provider).strip().lower().replace("-", "_")
    processor = extract_processor_entity(payload, "openai_llc" if provider == "open_ai" else "")
    checkout = {
        "session_id": session_id,
        "checkout_provider": checkout_provider,
        "processor_entity": processor,
        "billing_country": PROFILE["billing_country"],
        "currency": PROFILE["currency"],
        "plan_name": PROFILE["plan"],
        "payment_method_types": list(payload.get("payment_method_types") or []),
        "custom_payment_methods": list(payload.get("custom_payment_methods") or []),
        "checkout_state": dict(payload.get("checkout_state") or {}),
        "publishable_key": str(payload.get("publishable_key") or ""),
        "customer_session_client_secret": str(payload.get("customer_session_client_secret") or ""),
    }
    apply_checkout_response(checkout, payload)
    checkout_url = (
        f"https://checkout.stripe.com/c/pay/{session_id}"
        if provider == "stripe"
        else f"https://chatgpt.com/checkout/{processor or 'openai_llc'}/{session_id}"
    )
    return checkout, CheckoutIdentity(
        session_id=session_id,
        provider=provider,
        checkout_provider=checkout_provider,
        processor_entity=processor,
        checkout_url=checkout_url,
    )


def _refresh_promotion(client: _CheckoutClient, checkout: dict[str, Any], proxy: str) -> None:
    session_id = str(checkout.get("session_id") or "")
    processor = str(checkout.get("processor_entity") or "openai_llc")
    payload = client.post(
        "/backend-api/payments/checkout/update",
        {
            "checkout_session_id": session_id,
            "processor_entity": processor,
            "plan_name": PROFILE["plan"],
            "price_interval": "month",
            "seat_quantity": 1,
            "promo_campaign": {
                "promo_campaign_id": PROFILE["promotion"],
                "is_coupon_from_query_param": False,
            },
        },
        proxy,
        "promotion 刷新",
        referer=f"https://chatgpt.com/checkout/{processor}/{session_id}",
    )
    if payload.get("success") is False:
        raise PaymentEligibilityProtocolError("promotion 刷新被拒绝")
    apply_checkout_response(checkout, payload)


def _refresh_taxes(client: _CheckoutClient, account: Any, checkout: dict[str, Any], proxy: str) -> None:
    session_id = str(checkout.get("session_id") or "")
    processor = str(checkout.get("processor_entity") or "openai_llc")
    email = str(getattr(account, "email", "") or "buyer@example.com").strip() or "buyer@example.com"
    payload = client.post(
        "/backend-api/payments/checkout/taxes",
        {
            "checkout_session_id": session_id,
            "checkout_email": email,
            "billing_country": PROFILE["billing_country"],
            "billing_name": "Alex Morgan",
            "currency": PROFILE["currency"],
            "tax_id": None,
            "processor_entity": processor,
            "billing_address": {
                "line1": "",
                "city": "",
                "country": PROFILE["billing_country"],
                "postal_code": "",
            },
        },
        proxy,
        "taxes 刷新",
        referer=f"https://chatgpt.com/checkout/{processor}/{session_id}",
    )
    apply_checkout_response(checkout, payload)


def _stripe_amount(account: Any, checkout: dict[str, Any], proxy: str, profile: dict[str, Any]) -> tuple[int, str, dict[str, Any]]:
    # checkout_probe is the existing structured Stripe payment_pages reader;
    # pass the already-updated cs_* URL so it cannot create another checkout.
    extra = _account_extra(account)

    class _ProbeAccount:
        def __init__(self) -> None:
            self.access_token = _access_token(account)
            self.email = str(getattr(account, "email", "") or "")
            self.extra = extra
            self.cookies = _account_cookie(account, extra)

    probe_account = _ProbeAccount()
    result = probe_chatgpt_checkout_amount(
        probe_account,
        checkout_url=f"https://checkout.stripe.com/c/pay/{checkout['session_id']}",
        country=PROFILE["billing_country"],
        currency=PROFILE["currency"],
        proxy=proxy,
        browser_profile={
            "device_id": profile.get("device_id"),
            "ua": profile.get("ua"),
            "accept_language": profile.get("accept_language"),
            "locale": profile.get("locale"),
            "impersonate": profile.get("impersonate"),
            "timezone": profile.get("timezone"),
        },
    )
    amount = result.get("amount")
    try:
        amount_minor = _minor_amount(amount)
    except PaymentEligibilityProbeError as exc:
        raise PaymentEligibilityProtocolError("Stripe 最终金额缺失") from exc
    currency = str(result.get("currency") or PROFILE["currency"]).strip().upper()
    return amount_minor, currency, result


def _base_evidence(
    kind: str,
    *,
    attempt: int,
    identity: CheckoutIdentity,
    checkout: Mapping[str, Any],
    stage_regions: Mapping[str, str],
    verified_stage: str = "taxes_refresh",
) -> dict[str, Any]:
    methods = unique_cpmt_ids(checkout)
    return {
        "kind": kind,
        "profile": {
            "plan": PROFILE["plan"],
            "billing_country": PROFILE["billing_country"],
            "currency": PROFILE["currency"],
            "checkout_ui_mode": PROFILE["checkout_ui_mode"],
            "proxy_chain": dict(stage_regions),
        },
        "session_provider": identity.provider,
        "checkout_provider": str(checkout.get("checkout_provider") or identity.checkout_provider),
        "processor_entity": str(checkout.get("processor_entity") or identity.processor_entity),
        "custom_payment_method_count": len(methods),
        "custom_payment_method_digest": _digest_method_ids(methods),
        "attempt": int(attempt),
        "verified_stage": verified_stage,
    }


def _business_result(kind: str, state: str, evidence: dict[str, Any], reason_code: str, message: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "state": state,
        "business_result": True,
        "reason_code": reason_code,
        "message": message,
        "evidence": evidence,
        "checked_at": utc_now_iso(),
    }


def _probe_once(
    account: Any,
    kind: str,
    *,
    settings: Mapping[str, Any],
    attempt: int,
    stop_checker: Callable[[], None] | None,
) -> dict[str, Any]:
    profile = _browser_profile(account)
    stage_regions = payment_eligibility_stage_regions(kind, settings)
    chain = _resolve_proxy_chain(kind, settings)
    client = _CheckoutClient(account, profile, stop_checker)
    checkout, identity = _create_checkout(client, chain["checkout"])
    if identity.provider not in {"stripe", "open_ai"}:
        raise PaymentEligibilityProtocolError("checkout provider 无法识别")

    if kind == GCASH_KIND:
        if identity.provider == "stripe":
            # Stripe cs_* is a definitive GCash-negative branch, but still
            # completes the promotion/taxes chain before classification.
            _refresh_promotion(client, checkout, chain["promotion"])
            _refresh_taxes(client, account, checkout, chain["taxes"])
            evidence = _base_evidence(
                kind,
                attempt=attempt,
                identity=identity,
                checkout=checkout,
                stage_regions=stage_regions,
            )
            evidence["network"] = _redacted_proxy_settings(kind, settings)
            return _business_result(
                kind,
                "unavailable",
                evidence,
                "stripe_checkout",
                "Stripe checkout 不提供 GCash custom method",
            )
        provider = str(checkout.get("checkout_provider") or identity.checkout_provider or "").strip().lower().replace("-", "_")
        if provider and provider != "open_ai":
            raise PaymentEligibilityProtocolError("OAICS checkout_provider 不是 open_ai")
        processor = str(checkout.get("processor_entity") or identity.processor_entity or "").strip().lower()
        if processor != "openai_llc":
            raise PaymentEligibilityProtocolError("OAICS processor_entity 不是 openai_llc")
        initial_methods = unique_cpmt_ids(checkout)
        _refresh_promotion(client, checkout, chain["promotion"])
        promotion_methods = unique_cpmt_ids(checkout)
        _refresh_taxes(client, account, checkout, chain["taxes"])
        final_methods = unique_cpmt_ids(checkout)
        evidence = _base_evidence(
            kind,
            attempt=attempt,
            identity=identity,
            checkout=checkout,
            stage_regions=stage_regions,
        )
        evidence["network"] = _redacted_proxy_settings(kind, settings)
        evidence.update(
            {
                "initial_custom_payment_method_count": len(initial_methods),
                "promotion_custom_payment_method_count": len(promotion_methods),
                "final_custom_payment_method_count": len(final_methods),
                "stable": bool(initial_methods and initial_methods == promotion_methods == final_methods and len(final_methods) == 1),
            }
        )
        if len(final_methods) == 1 and initial_methods == promotion_methods == final_methods:
            return _business_result(
                kind,
                "available",
                evidence,
                "stable_cpmt",
                "GCash custom payment method 在最终税费刷新后稳定可用",
            )
        return _business_result(
            kind,
            "unavailable",
            evidence,
            "cpmt_not_unique_or_unstable",
            "最终 OAICS 未暴露稳定且唯一的 GCash custom method",
        )

    # Zero-amount eligibility deliberately ignores payment methods, including
    # GCash, and continues through both refresh stages on either protocol.
    try:
        _refresh_promotion(client, checkout, chain["promotion"])
    except PaymentEligibilityHttpError as exc:
        if not _is_explicit_promotion_unavailable(exc):
            raise
        evidence = _base_evidence(
            kind,
            attempt=attempt,
            identity=identity,
            checkout=checkout,
            stage_regions=stage_regions,
            verified_stage="promotion_rejected",
        )
        evidence["network"] = _redacted_proxy_settings(kind, settings)
        evidence.update(
            {
                "upstream_status": exc.status_code,
                "promotion_result": "unavailable",
            }
        )
        return _business_result(
            kind,
            "ineligible",
            evidence,
            "promotion_unavailable",
            "上游明确返回试用优惠不可用，当前不具备 0 元资格",
        )
    _refresh_taxes(client, account, checkout, chain["taxes"])
    if identity.provider == "open_ai":
        amount_minor, currency = oaics_amount(checkout)
        stripe_payload: dict[str, Any] = {}
    else:
        amount_minor, currency, stripe_payload = _stripe_amount(account, checkout, chain["taxes"], profile)
    evidence = _base_evidence(
        kind,
        attempt=attempt,
        identity=identity,
        checkout=checkout,
        stage_regions=stage_regions,
    )
    evidence["network"] = _redacted_proxy_settings(kind, settings)
    evidence.update(
        {
            "amount_minor": amount_minor,
            "currency": currency,
            "amount_source": (
                "oaics.checkout_state.total.total.minorUnitsAmount"
                if identity.provider == "open_ai"
                else str(stripe_payload.get("amount_source") or "stripe.payment_pages.init")
            ),
        }
    )
    if currency != PROFILE["currency"]:
        raise PaymentEligibilityProtocolError("最终货币不是 PHP")
    if amount_minor == 0:
        return _business_result(kind, "eligible", evidence, "zero_php", "最终应付金额为 0 PHP")
    return _business_result(kind, "ineligible", evidence, "nonzero_php", f"最终应付金额为 {amount_minor} minor units PHP")


def run_payment_eligibility_probe(
    account: Any,
    kind: str,
    *,
    settings: Mapping[str, Any] | None = None,
    stop_checker: Callable[[], None] | None = None,
    max_attempts: int = _DEFAULT_ATTEMPTS,
) -> dict[str, Any]:
    normalized_kind = str(kind or "").strip().lower()
    if normalized_kind not in {ZERO_AMOUNT_KIND, GCASH_KIND}:
        raise ValueError(f"unsupported eligibility kind: {kind}")
    runtime_settings = dict(settings or {})
    attempts = max(1, min(int(max_attempts or _DEFAULT_ATTEMPTS), 4))
    last_error = ""
    for attempt in range(1, attempts + 1):
        if stop_checker is not None:
            stop_checker()
        try:
            result = _probe_once(
                account,
                normalized_kind,
                settings=runtime_settings,
                attempt=attempt,
                stop_checker=stop_checker,
            )
            result["attempt_count"] = attempt
            result.setdefault("evidence", {})["attempt_count"] = attempt
            return result
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            # TaskInterruption is intentionally re-raised by the task runner;
            # avoid turning a user stop into a provider failure here.
            if exc.__class__.__name__ in {"TaskInterruption", "StopTaskRequested", "SkipCurrentAttemptRequested"}:
                raise
            last_error = _safe_text(exc)
            if attempt >= attempts:
                break
    return {
        "kind": normalized_kind,
        "state": "probe_failed",
        "attempt_count": attempts,
        "business_result": False,
        "reason_code": "technical_error",
        "message": last_error or "结账探测失败",
        "evidence": {
            "kind": normalized_kind,
            "profile": {
                "plan": PROFILE["plan"],
                "billing_country": PROFILE["billing_country"],
                "currency": PROFILE["currency"],
                "checkout_ui_mode": PROFILE["checkout_ui_mode"],
                "proxy_chain": payment_eligibility_stage_regions(normalized_kind, runtime_settings),
            },
            "network": _redacted_proxy_settings(normalized_kind, runtime_settings),
            "attempt_count": attempts,
        },
        "checked_at": utc_now_iso(),
    }


def probe_zero_amount_eligibility(account: Any, **kwargs: Any) -> dict[str, Any]:
    return run_payment_eligibility_probe(account, ZERO_AMOUNT_KIND, **kwargs)


def probe_gcash_payment_method(account: Any, **kwargs: Any) -> dict[str, Any]:
    return run_payment_eligibility_probe(account, GCASH_KIND, **kwargs)
