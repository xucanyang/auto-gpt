"""
支付核心逻辑 — 生成 Plus 支付链接、无痕打开浏览器、检测订阅状态
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
import time
import uuid
from typing import Any, Optional

from curl_cffi import requests as cffi_requests
from core.browser_runtime import ensure_browser_display_available
from core.proxy_utils import build_requests_proxy_config

# from ..database.models import Account  # removed: external dep

logger = logging.getLogger(__name__)

PAYMENT_CHECKOUT_URL = "https://chatgpt.com/backend-api/payments/checkout"
CHATGPT_CHECKOUT_BASE_URL = "https://chatgpt.com/checkout/openai_llc/"
PAY_OPENAI_CHECKOUT_BASE_URL = "https://pay.openai.com/c/pay/"
PAY_OPENAI_CHECKOUT_FRAGMENT = (
    "#fidnandhYHdWcXxpYCc%2FJ2FgY2RwaXEnKSdpamZkaWAnPyd%2FbScpJ3ZwZ3Zmd2x1cWxqa1BrbHRwYGtgdnZAa2RnaWBhJz9jZGl2YCknYnBkZmRoamlgU2R3bGRrcSc%2FJ2Zqa3F3amknKSdkdWxOYHwnPyd1blppbHNgWjA0TUp3VnJGM200a31Cakw2aVFEYldvXFN3fzFhUDZjU0pkZ3xGZk5XNnVnQE9icEZTRGl0Rn1hfUZQc2pXbTRdUnJXZGZTbGpzUDZuSU5zdW5vbTJMdG5SNTVsXVR2b2o2aycpJ2N3amhWYHdzYHcnP3F3cGApJ2dkZm5id2pwa2FGamlqdyc%2FJyZjY2NjY2MnKSdpZHxqcHFRfHVgJz8ndmxrYmlgWmxxYGgnKSdga2RnaWBVaWRmYG1qaWFgd3YnP3F3cGB4JSUl"
)
CHECKOUT_PRICING_COUNTRIES_URL = "https://chatgpt.com/backend-api/checkout_pricing_config/countries"
CHECKOUT_PRICING_CONFIG_URL = "https://chatgpt.com/backend-api/checkout_pricing_config/configs/{country_code}"
DEFAULT_CHECKOUT_COUNTRY = "ID"
DEFAULT_CHECKOUT_CURRENCY = "IDR"
DEFAULT_STRIPE_PK = "pk_live_51Pj377KslHRdbaPgTJYjThzH3f5dt1N1vK7LUp0qh0yNSarhfZ6nfbG7FFlh8KLxVkvdMWN5o6Mc4Vda6NHaSnaV00C2Sbl8Zs"
OPENAI_STRIPE_PK = "pk_live_51HOrSwC6h1nxGoI3lTAgRjYVrz4dU3fVOabyCcKR3pbEJguCVAlqCxdxCUvoRh1XWwRacViovU3kLKvpkjh7IqkW00iXQsjo3n"
STRIPE_API = "https://api.stripe.com"
STRIPE_VERSION_BASE = "2025-03-31.basil"
STRIPE_VERSION_FULL = "2025-03-31.basil; checkout_server_update_beta=v1; checkout_manual_approval_preview=v1"
CHECKOUT_CONFIG_CACHE_TTL_SECONDS = 24 * 60 * 60
PAYMENT_LINK_FORMAT_LONG = "long_hosted"
PAYMENT_LINK_FORMAT_SHORT = "short_chatgpt"
DEFAULT_PAYMENT_LINK_FORMAT = PAYMENT_LINK_FORMAT_LONG

_checkout_countries_cache: dict[str, Any] = {"expires_at": 0.0, "value": None}
_checkout_pricing_config_cache: dict[str, dict[str, Any]] = {}
_CHECKOUT_SESSION_RE = re.compile(r"(cs_(?:live|test)_[A-Za-z0-9_]+)")


class CheckoutRequestError(RuntimeError):
    """Raised when ChatGPT checkout creation returns a non-2xx response."""

    def __init__(self, status_code: int, body: str):
        self.status_code = int(status_code or 0)
        self.body = str(body or "").strip()
        message = f"HTTP Error {self.status_code}"
        if self.body:
            message = f"{message}: {self.body[:500]}"
        super().__init__(message)


class CheckoutHostedUrlResolutionError(RuntimeError):
    """Raised when a checkout session cannot be converted to a hosted pay URL."""


class CustomCheckoutResolutionError(CheckoutHostedUrlResolutionError):
    """Backward-compatible error name for custom checkout hosted URL resolution."""


def _build_proxies(proxy: Optional[str]) -> Optional[dict]:
    return build_requests_proxy_config(proxy)


_COUNTRY_CURRENCY_MAP = {
    "ID": "IDR",
    "DE": "EUR",
    "SG": "SGD",
    "US": "USD",
    "TR": "TRY",
    "JP": "JPY",
    "HK": "HKD",
    "GB": "GBP",
    "EU": "EUR",
    "AU": "AUD",
    "CA": "CAD",
    "IN": "INR",
    "BR": "BRL",
    "MX": "MXN",
}


def normalize_checkout_country(country: Optional[str]) -> str:
    value = str(country or DEFAULT_CHECKOUT_COUNTRY).strip().upper()
    return value or DEFAULT_CHECKOUT_COUNTRY


def normalize_checkout_currency(currency: Optional[str], country: Optional[str] = None) -> str:
    value = str(currency or "").strip().upper()
    if value:
        return value
    normalized_country = normalize_checkout_country(country)
    return _COUNTRY_CURRENCY_MAP.get(normalized_country, DEFAULT_CHECKOUT_CURRENCY)


def normalize_payment_link_format(value: Any) -> str:
    text = str(value or "").strip().lower().replace("-", "_")
    if text in {"short", "short_chatgpt", "chatgpt", "custom", "custom_checkout"}:
        return PAYMENT_LINK_FORMAT_SHORT
    if text in {"long", "long_hosted", "hosted", "hosted_checkout", "pay_openai", "stripe_hosted"}:
        return PAYMENT_LINK_FORMAT_LONG
    return DEFAULT_PAYMENT_LINK_FORMAT


def checkout_ui_mode_for_link_format(value: Any) -> str:
    return "custom" if normalize_payment_link_format(value) == PAYMENT_LINK_FORMAT_SHORT else "hosted"


def fetch_checkout_countries(proxy: Optional[str] = None) -> list[str]:
    """读取 ChatGPT 当前支持的结账国家列表。只供用户打开/刷新选择器时调用。"""
    now = time.time()
    if not proxy and _checkout_countries_cache.get("value") and float(_checkout_countries_cache.get("expires_at") or 0) > now:
        return list(_checkout_countries_cache["value"])

    resp = cffi_requests.get(
        CHECKOUT_PRICING_COUNTRIES_URL,
        proxies=_build_proxies(proxy),
        timeout=30,
        impersonate="chrome110",
    )
    resp.raise_for_status()
    data = resp.json()
    countries = data.get("countries") if isinstance(data, dict) else []
    normalized: list[str] = []
    seen: set[str] = set()
    for item in countries or []:
        raw_code = str(item or "").strip().upper()
        if not raw_code:
            continue
        code = normalize_checkout_country(raw_code)
        if code and code not in seen:
            seen.add(code)
            normalized.append(code)
    if not proxy:
        _checkout_countries_cache["value"] = list(normalized)
        _checkout_countries_cache["expires_at"] = now + CHECKOUT_CONFIG_CACHE_TTL_SECONDS
    return normalized


def fetch_checkout_pricing_config(country: str, proxy: Optional[str] = None) -> dict[str, Any]:
    """读取指定国家的价格/货币配置。只供用户选择或切换国家时调用。"""
    normalized_country = normalize_checkout_country(country)
    now = time.time()
    if not proxy:
        cached = _checkout_pricing_config_cache.get(normalized_country)
        if cached and float(cached.get("expires_at") or 0) > now and isinstance(cached.get("value"), dict):
            return dict(cached["value"])

    resp = cffi_requests.get(
        CHECKOUT_PRICING_CONFIG_URL.format(country_code=normalized_country),
        proxies=_build_proxies(proxy),
        timeout=30,
        impersonate="chrome110",
    )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise ValueError("价格配置响应不是 JSON 对象")
    if not proxy:
        _checkout_pricing_config_cache[normalized_country] = {
            "value": dict(data),
            "expires_at": now + CHECKOUT_CONFIG_CACHE_TTL_SECONDS,
        }
    return data


def summarize_checkout_pricing_config(config: dict[str, Any]) -> dict[str, Any]:
    country = normalize_checkout_country(str((config or {}).get("country_code") or ""))
    currency = normalize_checkout_currency(str((config or {}).get("symbol_code") or ""), country)
    currency_config = (config or {}).get("currency_config") or {}
    return {
        "country_code": country,
        "symbol_code": currency,
        "symbol": str((config or {}).get("symbol") or ""),
        "minor_unit_exponent": (config or {}).get("minor_unit_exponent"),
        "plus": currency_config.get("plus") or {},
        "tax_type": (config or {}).get("tax_type"),
        "tax_percent": (config or {}).get("tax_percent"),
    }


def _extract_oai_did(cookies_str: str) -> Optional[str]:
    """从 cookie 字符串中提取 oai-device-id"""
    for part in cookies_str.split(";"):
        part = part.strip()
        if part.startswith("oai-did="):
            return part[len("oai-did=") :].strip()
    return None


def _clean_str(value: Any, default: str = "") -> str:
    text = str(value or "").strip()
    return text or default


def extract_checkout_session_id(value: Any) -> str:
    match = _CHECKOUT_SESSION_RE.search(str(value or ""))
    return match.group(1) if match else ""


def _checkout_hash_fragment(value: Any) -> str:
    text = str(value or "").strip()
    if "#" not in text:
        return ""
    fragment = "#" + text.split("#", 1)[1]
    return fragment if fragment.lower().startswith("#fid") else ""


def hosted_checkout_url_from_session_id(session_id: Any, fragment: Any = "") -> str:
    cs_id = extract_checkout_session_id(session_id)
    if not cs_id:
        return ""
    checkout_fragment = str(fragment or "").strip()
    if checkout_fragment and not checkout_fragment.startswith("#"):
        checkout_fragment = "#" + checkout_fragment
    if not checkout_fragment.lower().startswith("#fid"):
        checkout_fragment = PAY_OPENAI_CHECKOUT_FRAGMENT
    return PAY_OPENAI_CHECKOUT_BASE_URL + cs_id + checkout_fragment


def is_default_hosted_checkout_fragment(value: Any) -> bool:
    return _checkout_hash_fragment(value) == PAY_OPENAI_CHECKOUT_FRAGMENT


def chatgpt_checkout_url_from_session_id(session_id: Any, processor_entity: Any = "openai_llc") -> str:
    cs_id = extract_checkout_session_id(session_id)
    if not cs_id:
        return ""
    entity = str(processor_entity or "openai_llc").strip().strip("/") or "openai_llc"
    return f"https://chatgpt.com/checkout/{entity}/{cs_id}"


def _extract_processor_entity(url: Any, default: str = "openai_llc") -> str:
    text = str(url or "").strip()
    match = re.search(r"chatgpt\.com/checkout/([^/?#]+)/", text)
    if match:
        return match.group(1).strip() or default
    return default


def normalize_hosted_checkout_url(url: Any) -> str:
    text = str(url or "").strip()
    if not text:
        return ""
    cs_id = extract_checkout_session_id(text)
    if not cs_id:
        return text
    lowered = text.lower()
    if (
        "chatgpt.com/checkout/" in lowered
        or "checkout.stripe.com/c/pay/" in lowered
        or "pay.openai.com/c/pay/" in lowered
    ):
        return hosted_checkout_url_from_session_id(cs_id, _checkout_hash_fragment(text))
    return text


def normalize_chatgpt_checkout_url(url: Any, processor_entity: Any = "openai_llc") -> str:
    text = str(url or "").strip()
    if not text:
        return ""
    cs_id = extract_checkout_session_id(text)
    if not cs_id:
        return text
    entity = _extract_processor_entity(text, str(processor_entity or "openai_llc").strip() or "openai_llc")
    return chatgpt_checkout_url_from_session_id(cs_id, entity)


def normalize_checkout_url_for_link_format(url: Any, link_format: Any = None) -> str:
    return normalize_hosted_checkout_url(url)


def _stripe_headers() -> dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/146.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Origin": "https://js.stripe.com",
        "Referer": "https://js.stripe.com/",
    }


def _stripe_payment_page_init_body(stripe_pk: str, stripe_version: str) -> dict[str, str]:
    body = {
        "browser_locale": "zh-CN",
        "browser_timezone": "Asia/Shanghai",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[stripe_js_id]": str(uuid.uuid4()),
        "elements_session_client[locale]": "zh-CN",
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": "auto",
        "elements_options_client[saved_payment_method][enable_redisplay]": "auto",
        "key": stripe_pk,
        "_stripe_version": stripe_version,
    }
    if stripe_version == STRIPE_VERSION_FULL:
        body["elements_session_client[client_betas][0]"] = "custom_checkout_server_updates_1"
        body["elements_session_client[client_betas][1]"] = "custom_checkout_manual_approval_1"
    return body


def _stripe_key_candidates(primary: Any = None) -> list[str]:
    seen: set[str] = set()
    candidates: list[str] = []
    for raw in (primary, OPENAI_STRIPE_PK, DEFAULT_STRIPE_PK):
        key = str(raw or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        candidates.append(key)
    return candidates


def _resolve_checkout_hosted_url(
    data: dict[str, Any],
    *,
    checkout_url: str = "",
    checkout_session_id: str = "",
    proxy: Optional[str] = None,
    context: str = "支付链接",
) -> str:
    raw_url = str(checkout_url or "").strip()
    cs_id = extract_checkout_session_id(checkout_session_id or raw_url)
    if raw_url and cs_id and _checkout_hash_fragment(raw_url):
        return normalize_hosted_checkout_url(raw_url)
    if not cs_id:
        if raw_url:
            return normalize_hosted_checkout_url(raw_url)
        raise CheckoutHostedUrlResolutionError(f"{context}未返回 checkout session id")

    last_error = ""
    for stripe_pk in _stripe_key_candidates((data or {}).get("publishable_key")):
        for stripe_version in (STRIPE_VERSION_FULL, STRIPE_VERSION_BASE):
            response = cffi_requests.post(
                f"{STRIPE_API}/v1/payment_pages/{cs_id}/init",
                headers=_stripe_headers(),
                data=_stripe_payment_page_init_body(stripe_pk, stripe_version),
                proxies=_build_proxies(proxy),
                timeout=30,
                impersonate="chrome110",
            )
            status_code = int(getattr(response, "status_code", 0) or 0)
            text = str(getattr(response, "text", "") or "")
            if status_code == 200:
                payload = response.json() or {}
                hosted_url = str(payload.get("stripe_hosted_url") or "").strip()
                if hosted_url:
                    return normalize_hosted_checkout_url(hosted_url)
                last_error = "Stripe init 成功但未返回 stripe_hosted_url"
                continue
            last_error = f"Stripe init HTTP {status_code}: {text[:300]}"
            if status_code >= 500:
                break
    raise CheckoutHostedUrlResolutionError(last_error or f"{context}未能解析出可打开的支付链接")


def _resolve_custom_checkout_hosted_url(
    data: dict[str, Any],
    *,
    checkout_url: str = "",
    checkout_session_id: str = "",
    proxy: Optional[str] = None,
) -> str:
    try:
        return _resolve_checkout_hosted_url(
            data,
            checkout_url=checkout_url,
            checkout_session_id=checkout_session_id,
            proxy=proxy,
            context="短连接路径",
        )
    except CheckoutHostedUrlResolutionError as exc:
        raise CustomCheckoutResolutionError(str(exc)) from exc


def _local_timezone_offset_minutes() -> int:
    from datetime import datetime

    offset = datetime.now().astimezone().utcoffset()
    if offset is None:
        return 0
    return int(offset.total_seconds() // 60)


def _resolve_workspace_identity(account: Any) -> tuple[str, str]:
    """尽量从账号元数据里补出 workspace/account id，作为 accounts/check 失败时的回退。"""
    extra = getattr(account, "extra", {}) or {}
    account_id = str(
        extra.get("account_id")
        or extra.get("chatgpt_account_id")
        or getattr(account, "user_id", "")
        or ""
    ).strip()
    workspace_id = str(
        extra.get("workspace_id")
        or extra.get("organization_id")
        or getattr(account, "workspace_id", "")
        or ""
    ).strip()
    return account_id, workspace_id


def _build_checkout_headers(account: Any, *, chatgpt_account_id: str = "") -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {account.access_token}",
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "oai-language": "zh-CN",
        "oai-session-id": str(uuid.uuid4()),
        "x-openai-target-path": "/backend-api/payments/checkout",
        "x-openai-target-route": "/backend-api/payments/checkout",
    }
    cookies = str(getattr(account, "cookies", "") or "").strip()
    if not cookies:
        session_token = str(
            getattr(account, "session_token", "")
            or (getattr(account, "extra", {}) or {}).get("session_token")
            or ""
        ).strip()
        if session_token:
            cookies = (
                f"__Secure-next-auth.session-token={session_token}; "
                f"__Secure-authjs.session-token={session_token}"
            )
    if cookies:
        headers["cookie"] = cookies
        oai_did = _extract_oai_did(cookies)
        if oai_did:
            headers["oai-device-id"] = oai_did
    extra = getattr(account, "extra", {}) or {}
    resolved_account_id = str(
        chatgpt_account_id
        or extra.get("account_id")
        or extra.get("chatgpt_account_id")
        or extra.get("workspace_id")
        or ""
    ).strip()
    if resolved_account_id:
        headers["chatgpt-account-id"] = resolved_account_id
    return headers


def _checkout_billing_details(
    billing: Optional[dict[str, Any]],
    *,
    country: str,
    currency: str,
    email: str,
) -> dict[str, Any]:
    billing = dict(billing or {})
    billing_country = _clean_str(billing.get("country"), country).upper()
    details: dict[str, Any] = {
        "country": billing_country,
        "currency": _clean_str(currency, DEFAULT_CHECKOUT_CURRENCY).upper(),
    }
    name = _clean_str(billing.get("name"))
    billing_email = _clean_str(billing.get("email"), email)
    if name:
        details["name"] = name
    if billing_email:
        details["email"] = billing_email

    address = {
        "country": billing_country,
        "line1": _clean_str(billing.get("line1")),
        "city": _clean_str(billing.get("city")),
        "state": _clean_str(billing.get("state")),
        "postal_code": _clean_str(billing.get("postal_code")),
    }
    if any(value for key, value in address.items() if key != "country"):
        details["address"] = {key: value for key, value in address.items() if value}
    return details


def _generate_checkout_url(
    *,
    account: Any,
    payload: dict[str, Any],
    proxy: Optional[str],
    chatgpt_account_id: str = "",
    link_format: Any = None,
) -> str:
    normalized_format = normalize_payment_link_format(link_format)
    processor_entity = str(payload.get("processor_entity") or "openai_llc").strip() or "openai_llc"
    response = cffi_requests.post(
        PAYMENT_CHECKOUT_URL,
        headers=_build_checkout_headers(account, chatgpt_account_id=chatgpt_account_id),
        json=payload,
        proxies=_build_proxies(proxy),
        timeout=30,
        impersonate="chrome110",
    )
    if int(getattr(response, "status_code", 0) or 0) >= 400:
        raise CheckoutRequestError(int(response.status_code), str(getattr(response, "text", "") or ""))
    data = response.json()
    processor_entity = str(data.get("processor_entity") or processor_entity).strip() or "openai_llc"
    extra = getattr(account, "extra", None)
    if isinstance(extra, dict):
        extra["chatgpt_checkout_processor_entity"] = processor_entity
        extra["checkout_processor_entity"] = processor_entity

    checkout_url = str(data.get("url") or data.get("checkout_url") or data.get("cashier_url") or "").strip()
    checkout_session_id = str(data.get("checkout_session_id") or data.get("session_id") or data.get("id") or "").strip()
    if normalized_format == PAYMENT_LINK_FORMAT_SHORT:
        return _resolve_custom_checkout_hosted_url(
            data if isinstance(data, dict) else {},
            checkout_url=checkout_url,
            checkout_session_id=checkout_session_id,
            proxy=proxy,
        )

    hosted_url = _resolve_checkout_hosted_url(
        data if isinstance(data, dict) else {},
        checkout_url=checkout_url,
        checkout_session_id=checkout_session_id,
        proxy=proxy,
        context="长支付链接",
    )
    if hosted_url:
        return hosted_url

    raise ValueError(data.get("detail", "API 未返回支付链接"))


def _parse_cookie_str(cookies_str: str, domain: str) -> list:
    """将 'key=val; key2=val2' 格式解析为 Playwright cookie 列表"""
    cookies = []
    for part in cookies_str.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        name, _, value = part.partition("=")
        cookies.append(
            {
                "name": name.strip(),
                "value": value.strip(),
                "domain": domain,
                "path": "/",
            }
        )
    return cookies


def _open_url_system_browser(url: str) -> bool:
    """回退方案：调用系统浏览器以无痕模式打开"""
    platform = sys.platform
    try:
        if platform == "win32":
            for browser, flag in [("chrome", "--incognito"), ("msedge", "--inprivate")]:
                try:
                    subprocess.Popen(f'start {browser} {flag} "{url}"', shell=True)
                    return True
                except Exception:
                    continue
        elif platform == "darwin":
            subprocess.Popen(
                ["open", "-a", "Google Chrome", "--args", "--incognito", url]
            )
            return True
        else:
            for binary in ["google-chrome", "chromium-browser", "chromium"]:
                try:
                    subprocess.Popen([binary, "--incognito", url])
                    return True
                except FileNotFoundError:
                    continue
    except Exception as e:
        logger.warning(f"系统浏览器无痕打开失败: {e}")
    return False


def generate_plus_link(
    account: Any,
    proxy: Optional[str] = None,
    country: str = DEFAULT_CHECKOUT_COUNTRY,
    currency: Optional[str] = DEFAULT_CHECKOUT_CURRENCY,
    billing: Optional[dict[str, Any]] = None,
    link_format: Any = DEFAULT_PAYMENT_LINK_FORMAT,
) -> str:
    """生成 Plus 支付链接。默认长 hosted 链接，可按参数返回 ChatGPT 短链接。"""
    if not account.access_token:
        raise ValueError("账号缺少 access_token")

    country = normalize_checkout_country(country)
    currency = normalize_checkout_currency(currency, country)
    resolved_link_format = normalize_payment_link_format(link_format)

    payload = {
        "entry_point": "all_plans_pricing_modal",
        "plan_name": "chatgptplusplan",
        "billing_details": _checkout_billing_details(
            billing,
            country=country,
            currency=currency,
            email=str(getattr(account, "email", "") or ""),
        ),
        "promo_campaign": {
            "promo_campaign_id": "plus-1-month-free",
            "is_coupon_from_query_param": False,
        },
        "checkout_ui_mode": checkout_ui_mode_for_link_format(resolved_link_format),
    }
    return _generate_checkout_url(
        account=account,
        payload=payload,
        proxy=proxy,
        link_format=resolved_link_format,
    )


def generate_plus_short_link(
    account: Any,
    proxy: Optional[str] = None,
    country: str = DEFAULT_CHECKOUT_COUNTRY,
    currency: Optional[str] = DEFAULT_CHECKOUT_CURRENCY,
    billing: Optional[dict[str, Any]] = None,
) -> str:
    return generate_plus_link(
        account,
        proxy=proxy,
        country=country,
        currency=currency,
        billing=billing,
        link_format=PAYMENT_LINK_FORMAT_SHORT,
    )


def open_url_incognito(url: str, cookies_str: Optional[str] = None) -> bool:
    """用 Playwright 以无痕模式打开 URL，可注入 cookie"""
    import threading

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning("playwright 未安装，回退到系统浏览器")
        return _open_url_system_browser(url)

    def _launch():
        try:
            with sync_playwright() as p:
                ensure_browser_display_available(False)
                browser = p.chromium.launch(headless=False, args=["--incognito"])
                ctx = browser.new_context()
                if cookies_str:
                    ctx.add_cookies(_parse_cookie_str(cookies_str, "chatgpt.com"))
                page = ctx.new_page()
                page.goto(url)
                # 保持窗口打开直到用户关闭
                page.wait_for_timeout(300_000)  # 最多等待 5 分钟
        except Exception as e:
            logger.warning(f"Playwright 无痕打开失败: {e}")

    threading.Thread(target=_launch, daemon=True).start()
    return True


def check_subscription_status(account: Any, proxy: Optional[str] = None) -> str:
    """
    检测账号当前订阅状态。

    Returns:
        'free' / 'plus' / 'team'
    """
    if not account.access_token:
        raise ValueError("账号缺少 access_token")

    headers = {
        "Authorization": f"Bearer {account.access_token}",
        "Content-Type": "application/json",
    }

    resp = cffi_requests.get(
        "https://chatgpt.com/backend-api/me",
        headers=headers,
        proxies=_build_proxies(proxy),
        timeout=20,
        impersonate="chrome110",
    )
    resp.raise_for_status()
    data = resp.json()

    # 解析订阅类型
    plan = data.get("plan_type") or ""
    if "team" in plan.lower():
        return "team"
    if "plus" in plan.lower():
        return "plus"

    # 尝试从 orgs 或 workspace 信息判断
    orgs = data.get("orgs", {}).get("data", [])
    for org in orgs:
        settings_ = org.get("settings", {})
        if settings_.get("workspace_plan_type") in ("team", "enterprise"):
            return "team"

    return "free"
