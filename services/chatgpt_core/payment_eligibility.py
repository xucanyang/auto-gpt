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
    normalize_proxy_url,
    resolve_task_proxy_candidates,
)
from services.chatgpt_core.account_fingerprint import resolve_account_browser_fingerprint
from services.chatgpt_core.checkout_probe import probe_chatgpt_checkout_amount
from services.chatgpt_core.payment_link_cache import TEAM_BILLING_COUNTRY_CURRENCIES
from services.chatgpt_core.utils import coerce_browser_fingerprint


ZERO_AMOUNT_KIND = "zero_amount_eligibility"
PAYMENT_METHODS_KIND = "payment_methods"
GCASH_KIND = "gcash_payment_method"
CHECKOUT_LINK_TYPE_KIND = "checkout_link_type"
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

_CPMT_RE = re.compile(r"^cpmt_[A-Za-z0-9]+$")
_SESSION_RE = re.compile(r"^(?:cs|oaics)_[A-Za-z0-9_]+$")
_MAX_JSON_DEPTH = 8
_DEFAULT_ATTEMPTS = 2
_DEFAULT_TIMEOUT_SECONDS = 30
_DEFAULT_ZERO_AMOUNT_COUNTRY = "VN"
_ZERO_DECIMAL_CURRENCIES = frozenset(
    {
        "CLP",
        "JPY",
        "KRW",
        "IDR",
    }
)


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


def is_payment_eligibility_unauthorized(exc: Exception) -> bool:
    if isinstance(exc, PaymentEligibilityHttpError):
        return exc.status_code == 401
    text = str(exc or "").lower()
    return "http 401" in text or "unauthorized" in text or "token_invalidated" in text


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


def _normalized_country(
    settings: Mapping[str, Any] | None = None,
    default: str = _DEFAULT_ZERO_AMOUNT_COUNTRY,
) -> str:
    values = settings or {}
    country = str(
        values.get("checkout_country_code")
        or values.get("country_code")
        or values.get("country")
        or values.get("promotion_proxy_country_code")
        or default
    ).strip().upper()
    if not (len(country) == 2 and country.isascii() and country.isalpha()):
        raise ValueError("结账国家必须是两位 ISO 国家代码")
    if country not in TEAM_BILLING_COUNTRY_CURRENCIES:
        raise ValueError(f"结账国家不受支持: {country}")
    return country


def _normalized_zero_amount_country(settings: Mapping[str, Any] | None = None) -> str:
    return _normalized_country(settings, default=_DEFAULT_ZERO_AMOUNT_COUNTRY)


def payment_eligibility_profile(
    kind: str,
    settings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the effective checkout contract for one probe kind."""
    normalized_kind = str(kind or "").strip().lower()
    if normalized_kind not in {ZERO_AMOUNT_KIND, GCASH_KIND, PAYMENT_METHODS_KIND, CHECKOUT_LINK_TYPE_KIND}:
        raise ValueError(f"unsupported eligibility kind: {kind}")
    if normalized_kind == GCASH_KIND:
        return {
            **PROFILE,
            "proxy_chain": dict(PROFILE["proxy_chain"]),
        }

    default_c = "PH" if normalized_kind == PAYMENT_METHODS_KIND else _DEFAULT_ZERO_AMOUNT_COUNTRY
    country = _normalized_country(settings, default=default_c)
    currency = str(TEAM_BILLING_COUNTRY_CURRENCIES[country]).strip().upper()
    return {
        "plan": PROFILE["plan"],
        "billing_country": country,
        "currency": currency,
        "checkout_ui_mode": PROFILE["checkout_ui_mode"],
        "promotion": PROFILE["promotion"],
        "proxy_chain": {
            "checkout": country,
            "promotion": country,
            "taxes": country,
        },
    }


def currency_minor_unit_exponent(currency: Any) -> int:
    return 0 if str(currency or "").strip().upper() in _ZERO_DECIMAL_CURRENCIES else 2


def format_minor_amount(amount_minor: Any, currency: Any) -> str:
    amount = _minor_amount(amount_minor)
    normalized_currency = str(currency or "").strip().upper()
    if not normalized_currency:
        raise PaymentEligibilityProtocolError("结账货币缺失")
    exponent = currency_minor_unit_exponent(normalized_currency)
    major = Decimal(amount) / (Decimal(10) ** exponent)
    return f"{major:,.{exponent}f} {normalized_currency}"


def payment_eligibility_stage_regions(
    kind: str,
    settings: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Return the requested stage exits without exposing runtime proxy URLs."""
    return dict(payment_eligibility_profile(kind, settings)["proxy_chain"])


def _redacted_proxy_settings(kind: str, settings: Mapping[str, Any]) -> dict[str, Any]:
    mode = str(settings.get("proxy_mode") or "global").strip().lower()
    return {
        "mode": mode,
        "dynamic_proxy_provider": str(
            settings.get("dynamic_proxy_provider") or "cliproxy"
        ).strip().lower(),
        "stage_regions": payment_eligibility_stage_regions(kind, settings),
    }


def _resolve_proxy_chain(kind: str, settings: Mapping[str, Any]) -> dict[str, str]:
    """Resolve stage exits; zero-amount probes reuse one verified checkout exit."""
    values = dict(settings or {})
    stage_regions = payment_eligibility_stage_regions(kind, values)
    mode = str(values.get("proxy_mode") or "global").strip().lower()
    explicit = str(values.get("proxy") or "").strip()
    if mode in {"direct", "none", "no_proxy"}:
        if kind in {ZERO_AMOUNT_KIND, CHECKOUT_LINK_TYPE_KIND, PAYMENT_METHODS_KIND}:
            raise PaymentEligibilityProbeError("0 元、支付方式与链接格式检测必须使用与结账国家一致的代理出口")
        return {stage: "" for stage in stage_regions}

    # A fixed URL has no trustworthy country metadata.  Zero-amount checks
    # cannot label it as the selected checkout country without verifying the
    # actual exit; GCash keeps its legacy explicit-proxy behavior unchanged.
    if explicit and not dynamic_proxy_supported(explicit) and mode != "dynamic":
        runtime_proxy = normalize_proxy_url(explicit) or ""
        if not runtime_proxy:
            raise PaymentEligibilityProbeError("指定代理解析后为空")
        if kind in {ZERO_AMOUNT_KIND, CHECKOUT_LINK_TYPE_KIND, PAYMENT_METHODS_KIND}:
            _verify_zero_amount_proxy_country(
                runtime_proxy,
                stage_regions["checkout"],
                values,
            )
        return {stage: runtime_proxy for stage in stage_regions}

    resolver_mode = mode or "global"
    if explicit and dynamic_proxy_supported(explicit):
        resolver_mode = "dynamic"
    stages_to_resolve = (
        [("checkout", stage_regions["checkout"])]
        if kind in {ZERO_AMOUNT_KIND, CHECKOUT_LINK_TYPE_KIND, PAYMENT_METHODS_KIND}
        else list(stage_regions.items())
    )
    chain: dict[str, str] = {}
    for stage, region in stages_to_resolve:
        try:
            candidate_params = {
                "proxy_country_code": region,
                "proxy_failover": False,
                "dynamic_proxy_max_attempts": 1,
                "proxy_max_candidates": values.get("proxy_max_candidates") or 1,
                "proxy_min_score": values.get("proxy_min_score") or 0,
            }
            if kind in {ZERO_AMOUNT_KIND, CHECKOUT_LINK_TYPE_KIND, PAYMENT_METHODS_KIND}:
                # The strict check below covers fixed, pool and dynamic URLs
                # uniformly and requires a real GeoIP answer.  Disable the
                # shared dynamic pre-probe to avoid scanning the same URL twice.
                candidate_params["dynamic_proxy_probe_enabled"] = False
            if resolver_mode not in {"global", ""}:
                candidate_params["proxy_mode"] = resolver_mode
            if explicit:
                candidate_params["proxy"] = explicit
            for key in (
                "dynamic_proxy_provider",
                "dynamic_proxy_probe_enabled",
                "dynamic_proxy_require_country_match",
                "dynamic_proxy_probe_timeout_seconds",
                "dynamic_proxy_ip_retention_minutes",
                "miyaip_crc",
                "miyaip_key_name",
                "miyaip_pool",
                "miyaip_gateway_server",
                "miyaip_protocol",
                "miyaip_request_timeout_seconds",
            ):
                if kind in {ZERO_AMOUNT_KIND, CHECKOUT_LINK_TYPE_KIND, PAYMENT_METHODS_KIND} and key == "dynamic_proxy_probe_enabled":
                    continue
                if key in values:
                    candidate_params[key] = values.get(key)
            candidates = resolve_task_proxy_candidates(
                candidate_params,
                default_mode="global" if resolver_mode in {"global", ""} else "direct",
                target="chatgpt",
            )
        except Exception as exc:
            raise PaymentEligibilityProbeError(f"{stage} 代理不可用: {_safe_text(exc)}") from exc
        runtime_proxy = str(candidates[0][0] if candidates else "").strip()
        if not runtime_proxy:
            raise PaymentEligibilityProbeError(f"{stage} 代理解析后为空")
        if kind in {ZERO_AMOUNT_KIND, CHECKOUT_LINK_TYPE_KIND, PAYMENT_METHODS_KIND}:
            _verify_zero_amount_proxy_country(runtime_proxy, region, values)
        chain[stage] = runtime_proxy
    if kind in {ZERO_AMOUNT_KIND, CHECKOUT_LINK_TYPE_KIND, PAYMENT_METHODS_KIND}:
        return {stage: chain["checkout"] for stage in stage_regions}
    return chain


def _verify_zero_amount_proxy_country(
    proxy: str,
    expected_country: str,
    settings: Mapping[str, Any],
) -> None:
    from services.proxy_scanner import scan_proxy_url

    try:
        timeout_seconds = max(
            2,
            min(int(settings.get("dynamic_proxy_probe_timeout_seconds") or 8), 60),
        )
    except (TypeError, ValueError):
        timeout_seconds = 8
    try:
        summary = scan_proxy_url(
            proxy,
            targets=["basic", "geo"],
            timeout_seconds=timeout_seconds,
            refresh_geo=True,
        )
    except Exception as exc:
        raise PaymentEligibilityProbeError(
            f"checkout 代理出口国家校验失败: {_safe_text(exc)}"
        ) from exc
    basic = summary.get("basic") if isinstance(summary, dict) else {}
    geo = summary.get("geo") if isinstance(summary, dict) else {}
    if not isinstance(basic, dict) or not basic.get("ok"):
        detail = (
            basic.get("error")
            if isinstance(basic, dict)
            else "代理基础连通性检测失败"
        )
        raise PaymentEligibilityProbeError(
            f"checkout 代理出口国家校验失败: {_safe_text(detail) or '代理基础连通性检测失败'}"
        )
    actual_country = str(
        geo.get("country_code") if isinstance(geo, dict) else ""
    ).strip().upper()
    expected = str(expected_country or "").strip().upper()
    if not actual_country:
        detail = (
            geo.get("error") or geo.get("error_code")
            if isinstance(geo, dict)
            else "geo_unavailable"
        )
        raise PaymentEligibilityProbeError(
            f"checkout 代理出口国家无法确认: expected={expected}, detail={_safe_text(detail) or 'geo_unavailable'}"
        )
    if actual_country != expected:
        raise PaymentEligibilityProbeError(
            f"checkout 代理出口国家不一致: expected={expected}, actual={actual_country}"
        )


class _CheckoutClient:
    def __init__(
        self,
        account: Any,
        profile: dict[str, Any],
        stop_checker: Callable[[], None] | None = None,
        *,
        reuse_session: bool = False,
    ):
        self.account = account
        self.profile = profile
        self.stop_checker = stop_checker
        self.reuse_session = bool(reuse_session)
        self._shared_session: Any = None
        self._shared_proxy = ""
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

    def _request_session(self, proxy: str) -> tuple[Any, bool]:
        if not self.reuse_session:
            return self._session(proxy), True
        normalized_proxy = normalize_proxy_url(proxy) or ""
        if self._shared_session is None:
            self._shared_session = self._session(normalized_proxy)
            self._shared_proxy = normalized_proxy
        elif normalized_proxy != self._shared_proxy:
            raise PaymentEligibilityProbeError("0 元检测 HTTP Session 的代理出口发生变化")
        return self._shared_session, False

    def close(self) -> None:
        session = self._shared_session
        self._shared_session = None
        self._shared_proxy = ""
        if session is not None:
            session.close()

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
        session, close_after_request = self._request_session(proxy)
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
            if close_after_request:
                session.close()


def _create_checkout(
    client: _CheckoutClient,
    proxy: str,
    checkout_profile: Mapping[str, Any],
) -> tuple[dict[str, Any], CheckoutIdentity]:
    body = {
        "entry_point": "all_plans_pricing_modal",
        "plan_name": checkout_profile["plan"],
        "billing_details": {
            "country": checkout_profile["billing_country"],
            "currency": checkout_profile["currency"],
        },
        "promo_campaign": {
            "promo_campaign_id": checkout_profile["promotion"],
            "is_coupon_from_query_param": False,
        },
        "checkout_ui_mode": checkout_profile["checkout_ui_mode"],
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
        "billing_country": checkout_profile["billing_country"],
        "currency": checkout_profile["currency"],
        "plan_name": checkout_profile["plan"],
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


def _refresh_promotion(
    client: _CheckoutClient,
    checkout: dict[str, Any],
    proxy: str,
    checkout_profile: Mapping[str, Any],
) -> None:
    session_id = str(checkout.get("session_id") or "")
    processor = str(checkout.get("processor_entity") or "openai_llc")
    payload = client.post(
        "/backend-api/payments/checkout/update",
        {
            "checkout_session_id": session_id,
            "processor_entity": processor,
            "plan_name": checkout_profile["plan"],
            "price_interval": "month",
            "seat_quantity": 1,
            "promo_campaign": {
                "promo_campaign_id": checkout_profile["promotion"],
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


def _refresh_taxes(
    client: _CheckoutClient,
    account: Any,
    checkout: dict[str, Any],
    proxy: str,
    checkout_profile: Mapping[str, Any],
) -> None:
    session_id = str(checkout.get("session_id") or "")
    processor = str(checkout.get("processor_entity") or "openai_llc")
    email = str(getattr(account, "email", "") or "buyer@example.com").strip() or "buyer@example.com"
    payload = client.post(
        "/backend-api/payments/checkout/taxes",
        {
            "checkout_session_id": session_id,
            "checkout_email": email,
            "billing_country": checkout_profile["billing_country"],
            "billing_name": "Alex Morgan",
            "currency": checkout_profile["currency"],
            "tax_id": None,
            "processor_entity": processor,
            "billing_address": {
                "line1": "",
                "city": "",
                "country": checkout_profile["billing_country"],
                "postal_code": "",
            },
        },
        proxy,
        "taxes 刷新",
        referer=f"https://chatgpt.com/checkout/{processor}/{session_id}",
    )
    apply_checkout_response(checkout, payload)


def _stripe_amount(
    account: Any,
    checkout: dict[str, Any],
    proxy: str,
    browser_profile: dict[str, Any],
    checkout_profile: Mapping[str, Any],
) -> tuple[int, str, dict[str, Any]]:
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
        country=str(checkout_profile["billing_country"]),
        currency=str(checkout_profile["currency"]),
        proxy=proxy,
        browser_profile={
            "device_id": browser_profile.get("device_id"),
            "ua": browser_profile.get("ua"),
            "accept_language": browser_profile.get("accept_language"),
            "locale": browser_profile.get("locale"),
            "impersonate": browser_profile.get("impersonate"),
            "timezone": browser_profile.get("timezone"),
        },
    )
    amount = result.get("amount")
    try:
        amount_minor = _minor_amount(amount)
    except PaymentEligibilityProbeError as exc:
        raise PaymentEligibilityProtocolError("Stripe 最终金额缺失") from exc
    currency = str(result.get("currency") or checkout_profile["currency"]).strip().upper()
    return amount_minor, currency, result


def _base_evidence(
    kind: str,
    *,
    attempt: int,
    identity: CheckoutIdentity,
    checkout: Mapping[str, Any],
    checkout_profile: Mapping[str, Any],
    stage_regions: Mapping[str, str],
    verified_stage: str = "taxes_refresh",
) -> dict[str, Any]:
    methods = unique_cpmt_ids(checkout)
    return {
        "kind": kind,
        "profile": {
            "plan": checkout_profile["plan"],
            "billing_country": checkout_profile["billing_country"],
            "currency": checkout_profile["currency"],
            "checkout_ui_mode": checkout_profile["checkout_ui_mode"],
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
    browser_profile = _browser_profile(account)
    checkout_profile = payment_eligibility_profile(kind, settings)
    stage_regions = payment_eligibility_stage_regions(kind, settings)
    chain = _resolve_proxy_chain(kind, settings)
    client = _CheckoutClient(
        account,
        browser_profile,
        stop_checker,
        reuse_session=kind == ZERO_AMOUNT_KIND,
    )
    try:
        checkout, identity = _create_checkout(
            client,
            chain["checkout"],
            checkout_profile,
        )
        if identity.provider not in {"stripe", "open_ai"}:
            raise PaymentEligibilityProtocolError("checkout provider 无法识别")

        if kind == CHECKOUT_LINK_TYPE_KIND:
            link_type = "oaics" if identity.provider == "open_ai" else "cs"
            evidence = _base_evidence(
                kind,
                attempt=attempt,
                identity=identity,
                checkout=checkout,
                checkout_profile=checkout_profile,
                stage_regions=stage_regions,
                verified_stage="checkout_created",
            )
            evidence["network"] = _redacted_proxy_settings(kind, settings)
            evidence["link_type"] = link_type
            evidence["session_id"] = identity.session_id
            evidence["checkout_url"] = identity.checkout_url
            return _business_result(
                kind,
                link_type,
                evidence,
                f"{link_type}_checkout",
                f"收银台链接格式为 {link_type.upper()}" if link_type == "oaics" else "收银台链接格式为 Stripe (CS)",
            )

        if kind in {PAYMENT_METHODS_KIND, GCASH_KIND}:
            if kind == GCASH_KIND:
                if identity.provider == "stripe":
                    # Stripe cs_* is a definitive GCash-negative branch, but still
                    # completes the promotion/taxes chain before classification.
                    _refresh_promotion(client, checkout, chain["promotion"], checkout_profile)
                    _refresh_taxes(client, account, checkout, chain["taxes"], checkout_profile)
                    evidence = _base_evidence(
                        kind,
                        attempt=attempt,
                        identity=identity,
                        checkout=checkout,
                        checkout_profile=checkout_profile,
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
                _refresh_promotion(client, checkout, chain["promotion"], checkout_profile)
                promotion_methods = unique_cpmt_ids(checkout)
                _refresh_taxes(client, account, checkout, chain["taxes"], checkout_profile)
                final_methods = unique_cpmt_ids(checkout)
                evidence = _base_evidence(
                    kind,
                    attempt=attempt,
                    identity=identity,
                    checkout=checkout,
                    checkout_profile=checkout_profile,
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

            # PAYMENT_METHODS_KIND generic probe
            methods: list[str] = []
            custom_methods: list[dict[str, Any]] = []
            if identity.provider == "stripe":
                _refresh_promotion(client, checkout, chain["promotion"], checkout_profile)
                _refresh_taxes(client, account, checkout, chain["taxes"], checkout_profile)
                amount_minor, currency, stripe_payload = _stripe_amount(
                    account,
                    checkout,
                    chain["taxes"],
                    browser_profile,
                    checkout_profile,
                )
                stripe_methods = stripe_payload.get("payment_method_types") or []
                for m in stripe_methods:
                    m_str = str(m).strip().lower()
                    if m_str and m_str not in methods:
                        methods.append(m_str)
                amount_display = format_minor_amount(amount_minor, currency)
            else:
                initial_methods = unique_cpmt_ids(checkout)
                _refresh_promotion(client, checkout, chain["promotion"], checkout_profile)
                promotion_methods = unique_cpmt_ids(checkout)
                _refresh_taxes(client, account, checkout, chain["taxes"], checkout_profile)
                final_cpmts = unique_cpmt_ids(checkout)

                raw_pmts = checkout.get("payment_method_types") or []
                for m in raw_pmts:
                    m_str = str(m).strip().lower()
                    if m_str and m_str not in methods:
                        methods.append(m_str)

                raw_cpms = checkout.get("custom_payment_methods") or []
                for cpm in raw_cpms:
                    if isinstance(cpm, dict):
                        custom_methods.append(cpm)

                if checkout_profile["billing_country"] == "PH" and final_cpmts:
                    if "gcash" not in methods:
                        methods.append("gcash")

                amount_minor, currency = oaics_amount(checkout)
                amount_display = format_minor_amount(amount_minor, currency)

            methods_display = [PAYMENT_METHOD_NAMES.get(m, m.replace("_", " ").title()) for m in methods]

            evidence = _base_evidence(
                kind,
                attempt=attempt,
                identity=identity,
                checkout=checkout,
                checkout_profile=checkout_profile,
                stage_regions=stage_regions,
            )
            evidence["network"] = _redacted_proxy_settings(kind, settings)
            evidence.update({
                "country": checkout_profile["billing_country"],
                "currency": checkout_profile["currency"],
                "provider": identity.provider,
                "session_id": identity.session_id,
                "checkout_url": identity.checkout_url,
                "methods": methods,
                "methods_display": methods_display,
                "custom_methods": custom_methods,
                "amount_minor": amount_minor,
                "amount_display": amount_display,
            })

            methods_summary_text = "、".join(methods_display) if methods_display else "无可用方式"
            state = "available" if methods else "no_methods"
            return _business_result(
                kind,
                state,
                evidence,
                f"methods_{state}",
                f"{checkout_profile['billing_country']} 支付方式: {methods_summary_text}" if methods else f"{checkout_profile['billing_country']} 未检测到可用支付方式",
            )

        # Zero-amount eligibility deliberately ignores payment methods,
        # including GCash, and finishes the same checkout environment.
        try:
            _refresh_promotion(client, checkout, chain["promotion"], checkout_profile)
        except PaymentEligibilityHttpError as exc:
            if not _is_explicit_promotion_unavailable(exc):
                raise
            evidence = _base_evidence(
                kind,
                attempt=attempt,
                identity=identity,
                checkout=checkout,
                checkout_profile=checkout_profile,
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
        _refresh_taxes(client, account, checkout, chain["taxes"], checkout_profile)
        if identity.provider == "open_ai":
            amount_minor, currency = oaics_amount(checkout)
            stripe_payload: dict[str, Any] = {}
        else:
            amount_minor, currency, stripe_payload = _stripe_amount(
                account,
                checkout,
                chain["taxes"],
                browser_profile,
                checkout_profile,
            )
        evidence = _base_evidence(
            kind,
            attempt=attempt,
            identity=identity,
            checkout=checkout,
            checkout_profile=checkout_profile,
            stage_regions=stage_regions,
        )
        evidence["network"] = _redacted_proxy_settings(kind, settings)
        evidence.update(
            {
                "amount_minor": amount_minor,
                "currency": currency,
                "minor_unit_exponent": currency_minor_unit_exponent(currency),
                "amount_display": format_minor_amount(amount_minor, currency),
                "amount_source": (
                    "oaics.checkout_state.total.total.minorUnitsAmount"
                    if identity.provider == "open_ai"
                    else str(stripe_payload.get("amount_source") or "stripe.payment_pages.init")
                ),
            }
        )
        expected_currency = str(checkout_profile["currency"]).strip().upper()
        if currency != expected_currency:
            raise PaymentEligibilityProtocolError(
                f"最终货币与结账国家不一致: expected={expected_currency}, actual={currency or '-'}"
            )
        if amount_minor == 0:
            return _business_result(
                kind,
                "eligible",
                evidence,
                "zero_checkout_amount",
                f"最终应付金额为 {evidence['amount_display']}",
            )
        return _business_result(
            kind,
            "ineligible",
            evidence,
            "nonzero_checkout_amount",
            f"最终应付金额为 {evidence['amount_display']}",
        )
    finally:
        client.close()


def run_payment_eligibility_probe(
    account: Any,
    kind: str,
    *,
    settings: Mapping[str, Any] | None = None,
    stop_checker: Callable[[], None] | None = None,
    max_attempts: int = _DEFAULT_ATTEMPTS,
) -> dict[str, Any]:
    normalized_kind = str(kind or "").strip().lower()
    if normalized_kind not in {ZERO_AMOUNT_KIND, GCASH_KIND, PAYMENT_METHODS_KIND, CHECKOUT_LINK_TYPE_KIND}:
        raise ValueError(f"unsupported eligibility kind: {kind}")
    runtime_settings = dict(settings or {})
    effective_profile = payment_eligibility_profile(normalized_kind, runtime_settings)
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
                "plan": effective_profile["plan"],
                "billing_country": effective_profile["billing_country"],
                "currency": effective_profile["currency"],
                "checkout_ui_mode": effective_profile["checkout_ui_mode"],
                "proxy_chain": payment_eligibility_stage_regions(normalized_kind, runtime_settings),
            },
            "network": _redacted_proxy_settings(normalized_kind, runtime_settings),
            "attempt_count": attempts,
        },
        "checked_at": utc_now_iso(),
    }


def probe_zero_amount_eligibility(account: Any, **kwargs: Any) -> dict[str, Any]:
    return run_payment_eligibility_probe(account, ZERO_AMOUNT_KIND, **kwargs)


def probe_payment_methods(account: Any, **kwargs: Any) -> dict[str, Any]:
    return run_payment_eligibility_probe(account, PAYMENT_METHODS_KIND, **kwargs)


def probe_gcash_payment_method(account: Any, **kwargs: Any) -> dict[str, Any]:
    return run_payment_eligibility_probe(account, GCASH_KIND, **kwargs)


def probe_checkout_link_type(account: Any, **kwargs: Any) -> dict[str, Any]:
    return run_payment_eligibility_probe(account, CHECKOUT_LINK_TYPE_KIND, **kwargs)

