"""
支付核心逻辑 — 生成 Plus/Team 支付链接、无痕打开浏览器、检测订阅状态
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from typing import Any, Optional

from curl_cffi import requests as cffi_requests
from core.browser_runtime import ensure_browser_display_available
from core.proxy_utils import build_requests_proxy_config

# from ..database.models import Account  # removed: external dep

logger = logging.getLogger(__name__)

PAYMENT_CHECKOUT_URL = "https://chatgpt.com/backend-api/payments/checkout"
TEAM_CHECKOUT_BASE_URL = "https://chatgpt.com/checkout/openai_llc/"
ACCOUNTS_CHECK_URL = "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27"
CHECKOUT_PRICING_COUNTRIES_URL = "https://chatgpt.com/backend-api/checkout_pricing_config/countries"
CHECKOUT_PRICING_CONFIG_URL = "https://chatgpt.com/backend-api/checkout_pricing_config/configs/{country_code}"
DEFAULT_CHECKOUT_COUNTRY = "ID"
DEFAULT_CHECKOUT_CURRENCY = "IDR"
DEFAULT_CHECKOUT_PROMO_CODE = "STRIPEATLASGPT4BIZ050126"
DEFAULT_STRIPE_PK = "pk_live_51Pj377KslHRdbaPgTJYjThzH3f5dt1N1vK7LUp0qh0yNSarhfZ6nfbG7FFlh8KLxVkvdMWN5o6Mc4Vda6NHaSnaV00C2Sbl8Zs"
CHECKOUT_CONFIG_CACHE_TTL_SECONDS = 24 * 60 * 60

_checkout_countries_cache: dict[str, Any] = {"expires_at": 0.0, "value": None}
_checkout_pricing_config_cache: dict[str, dict[str, Any]] = {}


class CheckoutRequestError(RuntimeError):
    """Raised when ChatGPT checkout creation returns a non-2xx response."""

    def __init__(self, status_code: int, body: str):
        self.status_code = int(status_code or 0)
        self.body = str(body or "").strip()
        message = f"HTTP Error {self.status_code}"
        if self.body:
            message = f"{message}: {self.body[:500]}"
        super().__init__(message)


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
        "business": currency_config.get("business") or {},
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
        "oai-language": "zh-CN",
    }
    if account.cookies:
        headers["cookie"] = account.cookies
        oai_did = _extract_oai_did(account.cookies)
        if oai_did:
            headers["oai-device-id"] = oai_did
    if chatgpt_account_id:
        headers["chatgpt-account-id"] = chatgpt_account_id
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


def _fetch_checkout_workspace_context(account: Any, proxy: Optional[str] = None) -> tuple[str, str]:
    """模仿脚本里的 accounts/check 逻辑，找到 workspace 账号 ID 和名称。"""
    headers = {
        "Authorization": f"Bearer {account.access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
    }
    if account.cookies:
        headers["cookie"] = account.cookies

    response = cffi_requests.get(
        f"{ACCOUNTS_CHECK_URL}?timezone_offset_min={_local_timezone_offset_minutes()}",
        headers=headers,
        proxies=_build_proxies(proxy),
        timeout=30,
        impersonate="chrome110",
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        return "", ""

    accounts = data.get("accounts") if isinstance(data.get("accounts"), dict) else {}
    account_ordering = data.get("account_ordering") if isinstance(data.get("account_ordering"), list) else []

    def _pick_name(item: dict[str, Any]) -> str:
        account_info = item.get("account") if isinstance(item.get("account"), dict) else {}
        return str(account_info.get("name") or "").strip() or "My Workspace"

    for key, item in accounts.items():
        account_key = str(key or "").strip()
        if not account_key or account_key == "default" or not isinstance(item, dict):
            continue
        account_info = item.get("account") if isinstance(item.get("account"), dict) else {}
        if str(account_info.get("structure") or "").strip().lower() == "workspace":
            return account_key, _pick_name(item)

    for raw_key in account_ordering:
        key = str(raw_key or "").strip()
        item = accounts.get(key)
        if key and isinstance(item, dict):
            return key, _pick_name(item)

    return "", ""


def _generate_checkout_url(
    *,
    account: Any,
    payload: dict[str, Any],
    proxy: Optional[str],
    chatgpt_account_id: str = "",
) -> str:
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

    checkout_url = str(data.get("url") or data.get("checkout_url") or data.get("cashier_url") or "").strip()
    if checkout_url:
        return checkout_url

    checkout_session_id = str(data.get("checkout_session_id") or "").strip()
    if checkout_session_id:
        return TEAM_CHECKOUT_BASE_URL + checkout_session_id

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
) -> str:
    """生成 Plus 支付链接（优先直接返回 hosted URL）"""
    if not account.access_token:
        raise ValueError("账号缺少 access_token")

    country = normalize_checkout_country(country)
    currency = normalize_checkout_currency(currency, country)

    payload = {
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
        "checkout_ui_mode": "hosted",
    }
    return _generate_checkout_url(
        account=account,
        payload=payload,
        proxy=proxy,
    )


def generate_team_link(
    account: Any,
    workspace_name: str = "MyTeam",
    price_interval: str = "month",
    seat_quantity: int = 5,
    promo_code: Optional[str] = None,
    proxy: Optional[str] = None,
    country: str = DEFAULT_CHECKOUT_COUNTRY,
    currency: Optional[str] = DEFAULT_CHECKOUT_CURRENCY,
    billing: Optional[dict[str, Any]] = None,
) -> str:
    """生成 Team 支付链接（按抓包成功参数优先走 custom checkout）"""
    if not account.access_token:
        raise ValueError("账号缺少 access_token")

    country = normalize_checkout_country(country)
    currency = normalize_checkout_currency(currency, country)
    resolved_promo_code = str(promo_code or DEFAULT_CHECKOUT_PROMO_CODE).strip() or DEFAULT_CHECKOUT_PROMO_CODE

    payload = {
        "entry_point": "team_workspace_purchase_modal",
        "plan_name": "chatgptteamplan",
        "team_plan_data": {
            "workspace_name": workspace_name,
            "price_interval": price_interval,
            "seat_quantity": seat_quantity,
        },
        "billing_details": _checkout_billing_details(
            billing,
            country=country,
            currency=currency,
            email=str(getattr(account, "email", "") or ""),
        ),
        "promo_code": resolved_promo_code,
        "cancel_url": f"https://chatgpt.com/?promoCode={resolved_promo_code}",
        "checkout_ui_mode": "custom",
    }
    return _generate_checkout_url(
        account=account,
        payload=payload,
        proxy=proxy,
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
