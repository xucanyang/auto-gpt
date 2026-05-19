"""GoPay payment session flow for ChatGPT subscription checkout.

This module intentionally keeps the live GoPay flow behind a small state
machine so the UI only ever asks for one input at a time.
"""

from __future__ import annotations

import base64
import json
import random
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlsplit

from curl_cffi import requests as cffi_requests

from core.proxy_utils import build_requests_proxy_config
from platforms.chatgpt.payment import (
    DEFAULT_CHECKOUT_COUNTRY,
    DEFAULT_CHECKOUT_CURRENCY,
    DEFAULT_STRIPE_PK,
    PAYMENT_CHECKOUT_URL,
    TEAM_CHECKOUT_BASE_URL,
    _extract_oai_did,
    normalize_checkout_country,
    normalize_checkout_currency,
)

STRIPE_API = "https://api.stripe.com"
STRIPE_VERSION_BASE = "2025-03-31.basil"
STRIPE_VERSION_FULL = "2025-03-31.basil; checkout_server_update_beta=v1; checkout_manual_approval_preview=v1"
DEFAULT_STRIPE_RUNTIME_VERSION = "6f8494a281"
KNOWN_PUBLISHABLE_KEYS = (
    DEFAULT_STRIPE_PK,
    "pk_live_51HOrSwC6h1nxGoI3lTAgRjYVrz4dU3fVOabyCcKR3pbEJguCVAlqCxdxCUvoRh1XWwRacViovU3kLKvpkjh7IqkW00iXQsjo3n",
)
DEFAULT_MIDTRANS_CLIENT_ID = "Mid-client-3TX8nUa-f_RgNrky"
DEFAULT_TIMEOUT = 30
LINK_RETRY_LIMIT = 2
LINK_RETRY_SLEEP_SECONDS = 12.0
DEFAULT_OTP_AUTO_RESEND_DELAY_SECONDS = 120
OTP_RESEND_MIN_INTERVAL_SECONDS = 30
GOPAY_PIN_CLIENT_ID_LINK = "51b5f09a-3813-11ee-be56-0242ac120002-MGUPA"
GOPAY_PIN_CLIENT_ID_CHARGE = "47180a8e-f56e-11ed-a05b-0242ac120003-GWC"

GOPAY_BROWSER_PROFILES = (
    {
        "name": "chrome146-win-id",
        "impersonate": "chrome146",
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
        "accept_language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
        "locale": "id-ID",
        "stripe_locale": "id",
        "timezone": "Asia/Jakarta",
        "platform": "Windows",
        "gopay_platform": "Windows 10",
        "viewport": "1366x768",
    },
    {
        "name": "chrome145-win-id",
        "impersonate": "chrome145",
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
        "accept_language": "id-ID,id;q=0.9,en;q=0.8,en-US;q=0.7",
        "locale": "id-ID",
        "stripe_locale": "id",
        "timezone": "Asia/Jakarta",
        "platform": "Windows",
        "gopay_platform": "Windows 10",
        "viewport": "1440x900",
    },
    {
        "name": "chrome146-mac-id",
        "impersonate": "chrome146",
        "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
        "accept_language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
        "locale": "id-ID",
        "stripe_locale": "id",
        "timezone": "Asia/Jakarta",
        "platform": "macOS",
        "gopay_platform": "Mac OS 14.6",
        "viewport": "1440x900",
    },
    {
        "name": "chrome145-mac-sg",
        "impersonate": "chrome145",
        "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
        "accept_language": "en-SG,en;q=0.9,id-ID;q=0.8,id;q=0.7",
        "locale": "en-SG",
        "stripe_locale": "en",
        "timezone": "Asia/Singapore",
        "platform": "macOS",
        "gopay_platform": "Mac OS 14.6",
        "viewport": "1536x960",
    },
)

PHASE_CREATED = "created"
PHASE_STARTING = "starting"
PHASE_WAITING_OTP = "waiting_otp"
PHASE_WAITING_LINK_PIN = "waiting_link_pin"
PHASE_WAITING_PAYMENT_PIN = "waiting_payment_pin"
PHASE_VERIFYING = "verifying"
PHASE_SUCCEEDED = "succeeded"
PHASE_FAILED = "failed"
PHASE_CANCELLED = "cancelled"


class GoPayFlowError(RuntimeError):
    pass


class GoPayInputRequired(GoPayFlowError):
    def __init__(self, phase: str):
        super().__init__(phase)
        self.phase = phase


GOPAY_ERROR_TRANSLATIONS: tuple[tuple[str, str], ...] = (
    ("account already linked", "该 GoPay 账号已绑定其他 ChatGPT 账号，请先在 App 中解绑后重试"),
    ("GoPay-1604", "验证码错误或已失效，请重新获取 OTP 后再试"),
    ("GoPay-5001", "GoPay 校验失败，服务临时异常，请稍后重试"),
    ("midtrans linking exhausted retries", "Midtrans 绑定失败，可能是该账号已绑定或号码正在冷却中，请稍后重试"),
    ("validate-reference failed", "GoPay 校验引用失败，请稍后重试"),
    ("validate-otp failed", "验证码错误或已失效，请重新获取 OTP 后再试"),
    ("resend-otp failed", "GoPay 验证码重发失败，请稍后重试"),
    ("validate-pin failed", "GoPay PIN 验证失败，请检查后重新输入"),
    ("payment/validate failed after retries", "GoPay 验证超时，请稍后重试"),
    ("payment/process failed", "GoPay 支付处理失败，请稍后重试"),
)


def _translate_gopay_error_message(message: Any) -> str:
    text = str(message or "").strip()
    if not text:
        return text
    if re.search(r"\b34900000(?:\.0+)?\b", text):
        return "该账号无试用资格"
    lowered = text.lower()
    for needle, translated in GOPAY_ERROR_TRANSLATIONS:
        if needle.lower() in lowered:
            return translated
    return text


@dataclass
class GoPaySession:
    session_id: str
    account_id: int
    email: str
    plan: str = "plus"
    country: str = DEFAULT_CHECKOUT_COUNTRY
    currency: str = DEFAULT_CHECKOUT_CURRENCY
    phone_country_code: str = ""
    phone_number: str = ""
    proxy: str = ""
    proxy_source: str = "none"
    processor_entity: str = ""
    checkout_url: str = ""
    stripe_checkout_url: str = ""
    default_pin: str = ""
    billing: dict[str, Any] = field(default_factory=dict)
    phase: str = PHASE_CREATED
    status: str = "active"
    cs_id: str = ""
    pm_id: str = ""
    snap_token: str = ""
    reference_id: str = ""
    charge_ref: str = ""
    link_challenge_id: str = ""
    link_client_id: str = ""
    payment_challenge_id: str = ""
    payment_client_id: str = ""
    otp_waiting_since: str = ""
    otp_resend_count: int = 0
    otp_auto_resend_done: bool = False
    otp_auto_resend_delay_seconds: int = DEFAULT_OTP_AUTO_RESEND_DELAY_SECONDS
    last_otp_resend_at: str = ""
    last_error: str = ""
    logs: list[str] = field(default_factory=list)
    result: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: _utcnow_iso())
    updated_at: str = field(default_factory=lambda: _utcnow_iso())
    lock: threading.Lock = field(default_factory=threading.Lock)
    runner: Any = field(default=None, repr=False)
    browser_profile: dict[str, Any] = field(default_factory=dict)


_SESSIONS: dict[str, GoPaySession] = {}
_SESSIONS_LOCK = threading.Lock()


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_log(session: GoPaySession, message: str) -> None:
    text = str(message or "").strip()
    if not text:
        return
    with session.lock:
        session.logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] {text}")
        session.logs = session.logs[-500:]
        session.updated_at = _utcnow_iso()


def _set_phase(session: GoPaySession, phase: str, *, error: str = "") -> None:
    with session.lock:
        session.phase = phase
        if phase == PHASE_FAILED:
            session.status = "failed"
        elif phase == PHASE_SUCCEEDED:
            session.status = "done"
        elif phase == PHASE_CANCELLED:
            session.status = "cancelled"
        else:
            session.status = "active"
        session.last_error = error
        session.updated_at = _utcnow_iso()


def _normalize_otp_auto_resend_delay(value: Any) -> int:
    try:
        delay = int(value)
    except Exception:
        delay = DEFAULT_OTP_AUTO_RESEND_DELAY_SECONDS
    return max(0, min(delay, 3600))


def _seconds_since_iso(value: str) -> Optional[float]:
    if not value:
        return None
    try:
        return time.time() - datetime.fromisoformat(value).timestamp()
    except Exception:
        return None


def _schedule_otp_auto_resend(session: GoPaySession) -> None:
    with session.lock:
        delay = _normalize_otp_auto_resend_delay(session.otp_auto_resend_delay_seconds)
        if delay <= 0 or session.otp_auto_resend_done:
            return
        session.otp_auto_resend_delay_seconds = delay
    _safe_log(session, f"GoPay OTP auto resend scheduled in {delay}s")

    def _worker() -> None:
        time.sleep(delay)
        with session.lock:
            should_resend = (
                session.phase == PHASE_WAITING_OTP
                and bool(session.reference_id)
                and not session.otp_auto_resend_done
                and session.otp_resend_count <= 0
            )
        if not should_resend:
            return
        try:
            _get_runner(session).resend_otp(auto=True)
        except Exception as exc:
            error = _translate_gopay_error_message(exc)
            _safe_log(session, f"GoPay OTP auto resend failed: {error}")
            with session.lock:
                if session.phase == PHASE_WAITING_OTP:
                    session.last_error = error
                    session.updated_at = _utcnow_iso()

    threading.Thread(target=_worker, daemon=True).start()


def _normalize_gopay_pin(pin: str) -> str:
    return re.sub(r"\D", "", str(pin or ""))


def _clean_str(value: Any, default: str = "") -> str:
    text = str(value or "").strip()
    return text or default


def _safe_json_summary(value: Any, max_len: int = 700) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = str(value)
    if len(text) > max_len:
        return text[:max_len] + "..."
    return text


def _proxy_observation(proxy: Optional[str]) -> dict[str, Any]:
    proxy = str(proxy or "").strip()
    if not proxy:
        return {"present": False, "host": "", "scheme": "", "port": None}
    try:
        parsed = urlsplit(proxy)
        return {
            "present": True,
            "host": parsed.hostname or "",
            "scheme": parsed.scheme or "",
            "port": parsed.port,
        }
    except Exception:
        return {"present": True, "host": "", "scheme": "", "port": None}


def _snapshot(session: GoPaySession) -> dict[str, Any]:
    with session.lock:
        return {
            "session_id": session.session_id,
            "account_id": session.account_id,
            "email": session.email,
            "plan": session.plan,
            "country": session.country,
            "currency": session.currency,
            "phone_country_code": session.phone_country_code,
            "phone_number": session.phone_number,
            "proxy": session.proxy,
            "proxy_source": session.proxy_source,
            "proxy_observation": _proxy_observation(session.proxy),
            "browser_profile": dict(session.browser_profile or {}),
            "processor_entity": session.processor_entity,
            "checkout_url": session.checkout_url,
            "stripe_checkout_url": session.stripe_checkout_url,
            "has_default_pin": bool(session.default_pin),
            "phase": session.phase,
            "status": session.status,
            "cs_id": session.cs_id,
            "pm_id": session.pm_id,
            "snap_token": session.snap_token,
            "reference_id": session.reference_id,
            "charge_ref": session.charge_ref,
            "otp_waiting_since": session.otp_waiting_since,
            "otp_resend_count": session.otp_resend_count,
            "otp_auto_resend_done": session.otp_auto_resend_done,
            "otp_auto_resend_delay_seconds": session.otp_auto_resend_delay_seconds,
            "last_otp_resend_at": session.last_otp_resend_at,
            "last_error": session.last_error,
            "logs": list(session.logs),
            "result": dict(session.result or {}),
            "created_at": session.created_at,
            "updated_at": session.updated_at,
        }


def _set_runner(session: GoPaySession, runner: "GoPayRunner") -> None:
    with session.lock:
        session.runner = runner
        session.updated_at = _utcnow_iso()


def _get_runner(session: GoPaySession) -> "GoPayRunner":
    with session.lock:
        runner = session.runner
    if not runner:
        raise GoPayFlowError("GoPay 会话状态已丢失，请重新开始支付")
    return runner


def get_gopay_session(session_id: str) -> dict[str, Any]:
    session = _SESSIONS.get(session_id)
    if not session:
        raise KeyError(session_id)
    return _snapshot(session)


def list_gopay_sessions() -> list[dict[str, Any]]:
    with _SESSIONS_LOCK:
        sessions = list(_SESSIONS.values())
    return [_snapshot(session) for session in sessions]


def _build_chatgpt_headers(account: Any) -> dict[str, str]:
    if not getattr(account, "access_token", ""):
        raise GoPayFlowError("账号缺少 access_token")
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
    if cookies:
        headers["Cookie"] = cookies
        oai_did = _extract_oai_did(cookies)
        if oai_did:
            headers["oai-device-id"] = oai_did
    return headers


def _complete_gopay_browser_profile(profile: dict[str, Any]) -> dict[str, Any]:
    profile = dict(profile or {})
    profile.setdefault("name", "custom")
    profile.setdefault("accept_language", "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7")
    profile.setdefault("locale", "id-ID")
    profile.setdefault("stripe_locale", profile["locale"].split("-", 1)[0] or "id")
    profile.setdefault("timezone", "Asia/Jakarta")
    profile.setdefault("platform", "Windows")
    profile.setdefault("gopay_platform", profile["platform"])
    profile.setdefault("time_on_page", random.randint(18000, 76000))
    profile.setdefault("viewport", random.choice(("1366x768", "1440x900", "1536x864", "1536x960")))
    return profile


def select_gopay_browser_profile_for_account(account: Any, existing: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    if isinstance(existing, dict) and existing.get("ua") and existing.get("impersonate"):
        return _complete_gopay_browser_profile(existing)
    extra = getattr(account, "extra", {}) or {}
    configured = extra.get("gopay_browser_profile")
    if isinstance(configured, dict) and configured.get("ua") and configured.get("impersonate"):
        profile = dict(configured)
    else:
        profile = dict(random.choice(GOPAY_BROWSER_PROFILES))
    return _complete_gopay_browser_profile(profile)


def _select_gopay_browser_profile(session: GoPaySession, account: Any) -> dict[str, Any]:
    with session.lock:
        existing = dict(session.browser_profile or {})
    profile = select_gopay_browser_profile_for_account(account, existing=existing)
    with session.lock:
        session.browser_profile = dict(profile)
        session.updated_at = _utcnow_iso()
    return profile


def _build_profile_chatgpt_headers(account: Any, profile: dict[str, Any]) -> dict[str, str]:
    headers = _build_chatgpt_headers(account)
    headers["User-Agent"] = str(profile.get("ua") or headers.get("User-Agent") or "")
    headers["Accept-Language"] = str(profile.get("accept_language") or "id-ID,id;q=0.9,en-US;q=0.8")
    headers["oai-language"] = str(profile.get("locale") or "id-ID")
    return headers


def _post_chatgpt(account: Any, url: str, *, json_body: dict[str, Any], proxy: str = ""):
    return cffi_requests.post(
        url,
        headers=_build_chatgpt_headers(account),
        json=json_body,
        proxies=build_requests_proxy_config(proxy or None),
        timeout=DEFAULT_TIMEOUT,
        impersonate="chrome110",
    )


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


def _get_chatgpt(account: Any, url: str, *, params: Optional[dict[str, Any]] = None, proxy: str = ""):
    return cffi_requests.get(
        url,
        headers=_build_chatgpt_headers(account),
        params=params,
        proxies=build_requests_proxy_config(proxy or None),
        timeout=DEFAULT_TIMEOUT,
        impersonate="chrome110",
    )


def _get_chatgpt_with_profile(account: Any, url: str, *, params: Optional[dict[str, Any]] = None, proxy: str = "", profile: dict[str, Any]):
    return cffi_requests.get(
        url,
        headers=_build_profile_chatgpt_headers(account, profile),
        params=params,
        proxies=build_requests_proxy_config(proxy or None),
        timeout=DEFAULT_TIMEOUT,
        impersonate=str(profile.get("impersonate") or "chrome146"),
    )


def _raise_for_gopay_status(resp: Any, context: str) -> None:
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
    if "type=gopay" not in message and ("gopay" in message.lower() and "test mode" in message.lower()):
        message = (
            f"{message}；当前 Stripe live mode 不支持 GoPay payment method，"
            "这一步在创建支付方式时失败，尚未进入 OTP/PIN。"
        )
    raise GoPayFlowError(f"{context} HTTP {status_code}: {message}")


def parse_checkout_url(raw: str) -> tuple[str, str]:
    raw = str(raw or "").strip()
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
        raise GoPayFlowError(f"无法从 checkout 输入中提取 session id: {raw[:120]}")
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


def _create_hosted_checkout(account: Any, *, country: str, currency: str, proxy: str, profile: dict[str, Any]) -> tuple[str, str]:
    processor_entity = str((getattr(account, "extra", {}) or {}).get("gopay_processor_entity") or "").strip()
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
    r = _post_chatgpt_with_profile(account, PAYMENT_CHECKOUT_URL, json_body=body, proxy=proxy, profile=profile)
    _raise_for_gopay_status(r, "chatgpt checkout create")
    data = r.json()
    checkout_url = str(data.get("url") or data.get("checkout_url") or data.get("cashier_url") or "").strip()
    cs_id = str(data.get("checkout_session_id") or data.get("session_id") or data.get("id") or "").strip()
    response_entity = _processor_entity_from_checkout_data(data, fallback_url=checkout_url) or processor_entity or "openai_llc"
    if checkout_url and not cs_id:
        cs_id, _ = parse_checkout_url(checkout_url)
    if checkout_url and cs_id:
        return checkout_url, cs_id
    if cs_id:
        return f"https://chatgpt.com/checkout/{response_entity}/{cs_id}", cs_id
    raise GoPayFlowError(f"checkout create: bad response {data!r}")


def _stripe_headers(profile: Optional[dict[str, Any]] = None) -> dict[str, str]:
    profile = profile or {}
    return {
        "User-Agent": str(profile.get("ua") or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"),
        "Accept": "application/json",
        "Accept-Language": str(profile.get("accept_language") or "id-ID,id;q=0.9,en-US;q=0.8"),
        "Origin": "https://js.stripe.com",
        "Referer": "https://js.stripe.com/",
    }


def _elements_options_client_payload() -> dict[str, str]:
    return {
        "elements_options_client[stripe_js_locale]": "auto",
        "elements_options_client[saved_payment_method][enable_save]": "never",
        "elements_options_client[saved_payment_method][enable_redisplay]": "never",
    }


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


def _gen_elements_session_id() -> str:
    import random
    import string

    return "elements_session_" + "".join(random.choices(string.ascii_letters + string.digits, k=11))


class GoPayRunner:
    def __init__(self, session: GoPaySession, account: Any):
        self.s = session
        self.account = account
        self.proxy = session.proxy or ""
        self.billing = dict(session.billing or {})
        self.profile = _select_gopay_browser_profile(session, account)
        self.ext = cffi_requests.Session(impersonate=str(self.profile.get("impersonate") or "chrome146"))
        self.ext.headers.update({
            "User-Agent": str(self.profile.get("ua") or _build_chatgpt_headers(account).get("User-Agent", "")),
            "Accept-Language": str(self.profile.get("accept_language") or "id-ID,id;q=0.9,en-US;q=0.8"),
        })
        proxies = build_requests_proxy_config(self.proxy or None)
        if proxies:
            self.ext.proxies = proxies
        proxy_seen = _proxy_observation(self.proxy)
        _safe_log(
            self.s,
            (
                "GoPay proxy: "
                f"source={self.s.proxy_source} present={proxy_seen.get('present')} "
                f"host={proxy_seen.get('host')} scheme={proxy_seen.get('scheme')} port={proxy_seen.get('port')}"
            ),
        )
        _safe_log(
            self.s,
            (
                "GoPay browser profile: "
                f"{self.profile.get('name')} {self.profile.get('impersonate')} "
                f"{self.profile.get('locale')} {self.profile.get('timezone')} "
                f"viewport={self.profile.get('viewport')}"
            ),
        )
        _safe_log(
            self.s,
            (
                "GoPay checkout context: "
                f"country={self.s.country} currency={self.s.currency} "
                f"billing_country={self.billing.get('country')} billing_city={self.billing.get('city')} "
                f"profile_locale={self.profile.get('locale')} stripe_locale={self.profile.get('stripe_locale')}"
            ),
        )

    def _stripe_pk(self) -> str:
        value = str((getattr(self.account, "extra", {}) or {}).get("stripe_publishable_key") or DEFAULT_STRIPE_PK).strip()
        if not value:
            raise GoPayFlowError("缺少 Stripe publishable key，请在账号 extra.stripe_publishable_key 中配置")
        return value

    def _midtrans_client_id(self) -> str:
        extra = getattr(self.account, "extra", {}) or {}
        return str(extra.get("gopay_midtrans_client_id") or DEFAULT_MIDTRANS_CLIENT_ID)

    def _processor_entity(self) -> str:
        if self.s.processor_entity:
            return self.s.processor_entity
        extra = getattr(self.account, "extra", {}) or {}
        configured = str(extra.get("gopay_processor_entity") or "").strip()
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
                    self.s.cs_id = cs_id
                    self.s.stripe_checkout_url = stripe_url
                    self.s.checkout_url = checkout_input
                    self.s.processor_entity = _extract_processor_entity(checkout_input, default=self._processor_entity())
                    _safe_log(self.s, f"Using checkout URL: {checkout_input}")
                    return cs_id
                self.s.checkout_url = checkout_input
                self.s.stripe_checkout_url = checkout_input
                _safe_log(self.s, f"Using hosted checkout URL: {checkout_input}")
                return self._chatgpt_create_checkout()
            cs_id, stripe_url = parse_checkout_url(checkout_input)
            self.s.cs_id = cs_id
            self.s.stripe_checkout_url = stripe_url
            if not self.s.checkout_url:
                self.s.checkout_url = TEAM_CHECKOUT_BASE_URL + cs_id
            self.s.processor_entity = _extract_processor_entity(self.s.checkout_url, default=self._processor_entity())
            _safe_log(self.s, f"Using checkout session: {cs_id}")
            return cs_id
        return self._chatgpt_create_checkout()

    def _chatgpt_create_checkout(self) -> str:
        checkout_url, cs_id = _create_hosted_checkout(
            self.account,
            country=self.s.country,
            currency=self.s.currency,
            proxy=self.proxy,
            profile=self.profile,
        )
        self.s.checkout_url = checkout_url
        self.s.processor_entity = _extract_processor_entity(checkout_url, default=self._processor_entity())
        _, stripe_checkout_url = parse_checkout_url(checkout_url)
        self.s.stripe_checkout_url = stripe_checkout_url
        if cs_id:
            self.s.cs_id = cs_id
        _safe_log(self.s, f"ChatGPT checkout created: {checkout_url}")
        return self.s.cs_id or checkout_url

    def _fetch_publishable_key(self, cs_id: str) -> str:
        configured = self._stripe_pk()
        last_err = ""
        for key in [configured, *[item for item in KNOWN_PUBLISHABLE_KEYS if item != configured]]:
            body = {
                "key": key,
                "_stripe_version": STRIPE_VERSION_BASE,
                "browser_locale": str(self.profile.get("stripe_locale") or self.profile.get("locale") or "id"),
            }
            r = self.ext.post(f"{STRIPE_API}/v1/payment_pages/{cs_id}/init", data=body, headers=_stripe_headers(self.profile), timeout=15)
            if r.status_code == 200:
                _safe_log(self.s, f"Stripe publishable key accepted: {key[:28]}...")
                return key
            last_err = f"key={key[:28]}... status={r.status_code} body={r.text[:300]}"
            if r.status_code in (400, 401, 403):
                _safe_log(self.s, f"Stripe publishable key rejected: {key[:28]}... {r.text[:120]}")
                continue
            _safe_log(self.s, f"Stripe publishable key probe unexpected: {key[:28]}... {r.status_code} {r.text[:120]}")
        raise GoPayFlowError(f"无法为当前 checkout session 探测可用的 Stripe publishable key: {last_err}")

    def _stripe_init_checkout(self, cs_id: str, stripe_pk: str) -> tuple[dict[str, Any], str, dict[str, Any]]:
        stripe_js_id = str(uuid.uuid4())
        elements_session_id = _gen_elements_session_id()
        elements_options = _elements_options_client_payload()
        for version in (STRIPE_VERSION_BASE, STRIPE_VERSION_FULL):
            body = {
                "browser_locale": str(self.profile.get("stripe_locale") or self.profile.get("locale") or "id"),
                "browser_timezone": str(self.profile.get("timezone") or "Asia/Jakarta"),
                "elements_session_client[elements_init_source]": "custom_checkout",
                "elements_session_client[referrer_host]": "chatgpt.com",
                "elements_session_client[stripe_js_id]": stripe_js_id,
                "elements_session_client[locale]": str(self.profile.get("locale") or "id-ID"),
                "elements_session_client[is_aggregation_expected]": "false",
                "key": stripe_pk,
                "_stripe_version": version,
            }
            body.update(elements_options)
            if version == STRIPE_VERSION_FULL:
                body["elements_session_client[client_betas][0]"] = "custom_checkout_server_updates_1"
                body["elements_session_client[client_betas][1]"] = "custom_checkout_manual_approval_1"
            r = self.ext.post(f"{STRIPE_API}/v1/payment_pages/{cs_id}/init", data=body, headers=_stripe_headers(self.profile), timeout=DEFAULT_TIMEOUT)
            if r.status_code == 200:
                init_data = r.json() or {}
                ctx = {
                    "stripe_js_id": stripe_js_id,
                    "elements_session_id": elements_session_id,
                    "elements_options_client": elements_options,
                    "locale": init_data.get("locale") or "en",
                    "currency": str(init_data.get("currency") or self.s.currency or "idr").lower(),
                    "checkout_amount": ((init_data.get("total_summary") or {}).get("due")
                                        if (init_data.get("total_summary") or {}).get("due") is not None
                                        else (init_data.get("invoice") or {}).get("amount_due")),
                    "payment_method_types": _extract_payment_method_types(init_data),
                    "config_id": init_data.get("config_id", ""),
                    "init_checksum": init_data.get("init_checksum", ""),
                    "return_url": init_data.get("return_url") or "",
                    "stripe_hosted_url": init_data.get("stripe_hosted_url") or "",
                }
                _safe_log(self.s, f"Stripe checkout initialized: amount={ctx['checkout_amount']} currency={ctx['currency']}")
                return init_data, version, ctx
            if r.status_code == 400 and "beta" in (r.text or "").lower():
                continue
            _raise_for_gopay_status(r, "stripe payment_pages init")
        raise GoPayFlowError("stripe payment_pages init failed")

    def _stripe_create_pm(self, cs_id: str, stripe_pk: str, stripe_ver: str, ctx: dict[str, Any]) -> str:
        billing = dict(self.billing or {})
        runtime = (getattr(self.account, "extra", {}) or {}).get("gopay_runtime") or {}
        runtime_version = ctx.get("runtime_version") or runtime.get("version") or DEFAULT_STRIPE_RUNTIME_VERSION
        stripe_js_id = ctx.get("stripe_js_id") or str(uuid.uuid4())
        elements_session_id = ctx.get("elements_session_id") or _gen_elements_session_id()
        elements_session_config_id = ctx.get("elements_session_config_id") or str(uuid.uuid4())
        checkout_config_id = ctx.get("payment_method_checkout_config_id") or ctx.get("config_id") or ""
        body = {
            "billing_details[name]": _clean_str(billing.get("name"), "John Doe"),
            "billing_details[email]": _clean_str(billing.get("email"), self.s.email or "buyer@example.com"),
            "billing_details[address][country]": _clean_str(billing.get("country"), self.s.country or "ID"),
            "billing_details[address][line1]": _clean_str(billing.get("line1"), "Jl. M.H. Thamrin No. 1"),
            "billing_details[address][city]": _clean_str(billing.get("city"), "Jakarta"),
            "billing_details[address][postal_code]": _clean_str(billing.get("postal_code"), "10310"),
            "billing_details[address][state]": _clean_str(billing.get("state"), "DKI Jakarta"),
            "type": "gopay",
            "payment_user_agent": (
                f"stripe.js/{runtime_version}; stripe-js-v3/{runtime_version}; "
                "payment-element; deferred-intent"
            ),
            "referrer": "https://chatgpt.com",
            "time_on_page": str(ctx.get("time_on_page") or self.profile.get("time_on_page") or 30000),
            "client_attribution_metadata[client_session_id]": stripe_js_id,
            "client_attribution_metadata[checkout_session_id]": cs_id,
            "client_attribution_metadata[checkout_config_id]": checkout_config_id,
            "client_attribution_metadata[elements_session_id]": elements_session_id,
            "client_attribution_metadata[elements_session_config_id]": elements_session_config_id,
            "client_attribution_metadata[merchant_integration_source]": "elements",
            "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
            "client_attribution_metadata[merchant_integration_version]": "2021",
            "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
            "client_attribution_metadata[payment_method_selection_flow]": "automatic",
            "client_attribution_metadata[merchant_integration_additional_elements][0]": "payment",
            "client_attribution_metadata[merchant_integration_additional_elements][1]": "address",
            "guid": ctx.get("guid") or uuid.uuid4().hex,
            "muid": ctx.get("muid") or uuid.uuid4().hex,
            "sid": ctx.get("sid") or uuid.uuid4().hex,
            "key": stripe_pk,
            "_stripe_version": stripe_ver,
        }
        r = self.ext.post(f"{STRIPE_API}/v1/payment_methods", data=body, headers=_stripe_headers(self.profile), timeout=DEFAULT_TIMEOUT)
        _raise_for_gopay_status(r, "stripe payment_methods")
        pm_id = r.json().get("id", "")
        if not str(pm_id).startswith("pm_"):
            raise GoPayFlowError(f"stripe payment_methods: bad response {r.text[:300]}")
        self.s.pm_id = str(pm_id)
        _safe_log(self.s, f"Stripe GoPay payment method created: {self.s.pm_id}")
        return self.s.pm_id

    def _stripe_confirm(self, cs_id: str, pm_id: str, stripe_pk: str, init_resp: dict[str, Any], stripe_ver: str, ctx: dict[str, Any]) -> dict[str, Any]:
        import urllib.parse

        runtime = (getattr(self.account, "extra", {}) or {}).get("gopay_runtime") or {}
        init_checksum = init_resp.get("init_checksum") or ctx.get("init_checksum") or ""
        if not init_checksum:
            raise GoPayFlowError("stripe confirm 缺少 init_checksum")
        expected_amount = "0"
        total_summary = init_resp.get("total_summary") or {}
        if total_summary.get("due") is not None:
            expected_amount = str(total_summary["due"])
        elif (init_resp.get("invoice") or {}).get("amount_due") is not None:
            expected_amount = str((init_resp.get("invoice") or {})["amount_due"])
        elif ctx.get("checkout_amount") is not None:
            expected_amount = str(ctx["checkout_amount"])
        stripe_hosted_url = ctx.get("stripe_hosted_url") or init_resp.get("stripe_hosted_url") or ""
        success_return_url = ctx.get("return_url") or init_resp.get("return_url") or init_resp.get("url") or ""
        processor_entity = self._processor_entity()
        return_url = stripe_hosted_url or success_return_url
        if stripe_hosted_url and success_return_url:
            parsed_hosted = urllib.parse.urlsplit(stripe_hosted_url)
            hosted_query = urllib.parse.urlencode([
                ("returned_from_redirect", "true"),
                ("ui_mode", "custom"),
                ("return_url", success_return_url),
            ])
            return_url = urllib.parse.urlunsplit((
                parsed_hosted.scheme,
                parsed_hosted.netloc,
                parsed_hosted.path,
                hosted_query,
                parsed_hosted.fragment,
            ))
        if not return_url:
            chatgpt_return = (
                f"https://chatgpt.com/checkout/verify?stripe_session_id={cs_id}"
                f"&processor_entity={processor_entity}&plan_type=plus"
            )
            return_url = (
                f"https://checkout.stripe.com/c/pay/{cs_id}"
                f"?returned_from_redirect=true&ui_mode=custom&return_url={urllib.parse.quote(chatgpt_return, safe='')}"
            )
        stripe_js_id = ctx.get("stripe_js_id") or str(uuid.uuid4())
        elements_session_id = ctx.get("elements_session_id") or _gen_elements_session_id()
        elements_session_config_id = ctx.get("elements_session_config_id") or str(uuid.uuid4())
        checkout_config_id = ctx.get("top_checkout_config_id") or ctx.get("config_id") or ""
        body = {
            "guid": ctx.get("guid") or uuid.uuid4().hex,
            "muid": ctx.get("muid") or uuid.uuid4().hex,
            "sid": ctx.get("sid") or uuid.uuid4().hex,
            "payment_method": pm_id,
            "init_checksum": init_checksum,
            "version": ctx.get("runtime_version") or runtime.get("version") or DEFAULT_STRIPE_RUNTIME_VERSION,
            "expected_amount": expected_amount,
            "expected_payment_method_type": "gopay",
            "return_url": return_url,
            "elements_session_client[elements_init_source]": "custom_checkout",
            "elements_session_client[referrer_host]": "chatgpt.com",
            "elements_session_client[stripe_js_id]": stripe_js_id,
            "elements_session_client[locale]": ctx.get("locale") or "en",
            "elements_session_client[is_aggregation_expected]": "false",
            "elements_session_client[session_id]": elements_session_id,
            "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
            "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
            "client_attribution_metadata[client_session_id]": stripe_js_id,
            "client_attribution_metadata[checkout_session_id]": cs_id,
            "client_attribution_metadata[checkout_config_id]": checkout_config_id,
            "client_attribution_metadata[elements_session_id]": elements_session_id,
            "client_attribution_metadata[elements_session_config_id]": elements_session_config_id,
            "client_attribution_metadata[merchant_integration_source]": "checkout",
            "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
            "client_attribution_metadata[merchant_integration_version]": "custom",
            "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
            "client_attribution_metadata[payment_method_selection_flow]": "automatic",
            "client_attribution_metadata[merchant_integration_additional_elements][0]": "payment",
            "client_attribution_metadata[merchant_integration_additional_elements][1]": "address",
            "key": stripe_pk,
            "_stripe_version": STRIPE_VERSION_FULL,
        }
        body.update(ctx.get("elements_options_client") or _elements_options_client_payload())
        consent_collection = init_resp.get("consent_collection") or {}
        if consent_collection.get("terms_of_service") not in (None, "", "none"):
            body["consent[terms_of_service]"] = "accepted"
        if runtime.get("js_checksum"):
            body["js_checksum"] = runtime["js_checksum"]
        if runtime.get("rv_timestamp"):
            body["rv_timestamp"] = runtime["rv_timestamp"]
        r = self.ext.post(f"{STRIPE_API}/v1/payment_pages/{cs_id}/confirm", data=body, headers=_stripe_headers(self.profile), timeout=DEFAULT_TIMEOUT)
        if r.status_code == 400 and "consent[terms_of_service]" not in body and "terms of service" in (r.text or "").lower():
            body["consent[terms_of_service]"] = "accepted"
            r = self.ext.post(f"{STRIPE_API}/v1/payment_pages/{cs_id}/confirm", data=body, headers=_stripe_headers(self.profile), timeout=DEFAULT_TIMEOUT)
        _raise_for_gopay_status(r, "stripe payment_pages confirm")
        payload = r.json() or {}
        _safe_log(self.s, f"Stripe checkout confirmed: {payload.get('payment_status') or payload.get('status')}")
        return payload

    def _stripe_update_payment_page_address(self, cs_id: str, stripe_pk: str, stripe_ver: str, ctx: dict[str, Any]) -> None:
        billing = dict(self.billing or {})
        address = {
            "country": _clean_str(billing.get("country"), self.s.country or "ID").upper(),
            "line1": _clean_str(billing.get("line1"), "Jl. M.H. Thamrin No. 1"),
            "city": _clean_str(billing.get("city"), "Jakarta"),
            "state": _clean_str(billing.get("state"), "DKI Jakarta"),
            "postal_code": _clean_str(billing.get("postal_code"), "10310"),
        }
        elements_session_id = ctx.get("elements_session_id") or _gen_elements_session_id()
        stripe_js_id = ctx.get("stripe_js_id") or str(uuid.uuid4())
        locale = ctx.get("locale") or self.profile.get("stripe_locale") or self.profile.get("locale") or "en"
        body = {
            "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
            "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
            "elements_session_client[elements_init_source]": "custom_checkout",
            "elements_session_client[referrer_host]": "chatgpt.com",
            "elements_session_client[session_id]": elements_session_id,
            "elements_session_client[stripe_js_id]": stripe_js_id,
            "elements_session_client[locale]": locale,
            "elements_session_client[is_aggregation_expected]": "false",
            "client_attribution_metadata[merchant_integration_additional_elements][0]": "payment",
            "client_attribution_metadata[merchant_integration_additional_elements][1]": "address",
            "key": stripe_pk,
            "_stripe_version": stripe_ver,
        }
        body.update(ctx.get("elements_options_client") or _elements_options_client_payload())
        _safe_log(self.s, "Stripe payment page address update starting")
        accumulated: dict[str, str] = {}
        for step_idx, new_fields in enumerate([
            {"tax_region[country]": address["country"]},
            {},
            {"tax_region[line1]": address["line1"]},
            {"tax_region[city]": address["city"]},
            {"tax_region[state]": address["state"]},
            {"tax_region[postal_code]": address["postal_code"]},
        ], start=1):
            accumulated.update(new_fields)
            data = dict(body)
            data.update(accumulated)
            step_name = list(new_fields.keys())[0] if new_fields else "(焦点变更)"
            _safe_log(self.s, f"Stripe payment page address step {step_idx}/6: {step_name}")
            r = self.ext.post(f"{STRIPE_API}/v1/payment_pages/{cs_id}", data=data, headers=_stripe_headers(self.profile), timeout=DEFAULT_TIMEOUT)
            if r.status_code != 200:
                _safe_log(self.s, f"Stripe payment page address step {step_idx} returned {r.status_code}: {r.text[:160]}")
            time.sleep(random.uniform(2.0, 4.5))
        _safe_log(self.s, "Stripe payment page address update completed")

    def _chatgpt_approve(self, cs_id: str) -> dict[str, Any]:
        processor_entity = self._processor_entity()
        try:
            _post_chatgpt_with_profile(
                self.account,
                "https://chatgpt.com/backend-api/sentinel/ping",
                json_body={},
                proxy=self.proxy,
                profile=self.profile,
            )
        except Exception as exc:
            _safe_log(self.s, f"sentinel ping skipped: {exc}")
        r = _post_chatgpt_with_profile(
            self.account,
            "https://chatgpt.com/backend-api/payments/checkout/approve",
            json_body={"checkout_session_id": cs_id, "processor_entity": processor_entity},
            proxy=self.proxy,
            profile=self.profile,
            extra_headers={"Referer": f"https://chatgpt.com/checkout/{processor_entity}/{cs_id}"},
        )
        r.raise_for_status()
        try:
            payload = r.json() or {}
        except Exception:
            payload = {"raw": (r.text or "")[:500]}
        result = payload.get("result") if isinstance(payload, dict) else None
        if result != "approved":
            summary = {
                "status_code": r.status_code,
                "keys": list(payload.keys())[:20] if isinstance(payload, dict) else [],
                "result": result,
                "response": payload,
            }
            _safe_log(self.s, f"ChatGPT approve response: {_safe_json_summary(summary)}")
            if str(result or "").lower() in {"blocked", "exception"}:
                _safe_log(self.s, "ChatGPT approve returned non-terminal state; continuing Stripe redirect polling")
                return payload
            raise GoPayFlowError(f"chatgpt approve: result={result!r}")
        _safe_log(self.s, "ChatGPT checkout approved")
        return payload

    def _confirm_requires_approval(self, payload: dict[str, Any]) -> bool:
        submission = payload.get("submission_attempt") if isinstance(payload, dict) else {}
        state = submission.get("state") if isinstance(submission, dict) else ""
        if state == "requires_approval":
            return True
        raw = str(payload or "").lower()
        return "requires_approval" in raw or "requires_merchant_approval" in raw

    def _fetch_pm_redirect_snap_token(self, pm_url: str) -> str:
        r = self.ext.get(pm_url, allow_redirects=False, timeout=DEFAULT_TIMEOUT)
        if r.status_code not in (301, 302, 303, 307, 308):
            raise GoPayFlowError(f"pm-redirects: expected redirect, got {r.status_code}")
        loc = r.headers.get("Location", "")
        m = re.search(r"app\.midtrans\.com/snap/v[14]/redirection/([a-f0-9-]{36})", loc)
        if not m:
            raise GoPayFlowError(f"pm-redirects: no midtrans token in Location={loc!r}")
        return m.group(1)

    def _extract_redirect_url(self, payload: dict[str, Any]) -> str:
        for key in ("next_action", "payment_intent", "setup_intent"):
            obj = payload.get(key)
            if isinstance(obj, dict):
                next_action = obj if key == "next_action" else obj.get("next_action")
                if isinstance(next_action, dict) and next_action.get("type") == "redirect_to_url":
                    redirect = next_action.get("redirect_to_url") or {}
                    url = redirect.get("url") or ""
                    if url:
                        return str(url)
        raw = str(payload)
        match = re.search(r"https://pm-redirects\.stripe\.com/authorize/[^'\"\\s]+", raw)
        return match.group(0) if match else ""

    def _follow_redirect_to_midtrans(
        self,
        cs_id: str,
        stripe_pk: str,
        confirm_data: Optional[dict[str, Any]] = None,
        ctx: Optional[dict[str, Any]] = None,
    ) -> str:
        redirect_url = self._extract_redirect_url(confirm_data or {})
        if redirect_url:
            snap_token = self._fetch_pm_redirect_snap_token(redirect_url)
            self.s.snap_token = snap_token
            _safe_log(self.s, f"Midtrans snap token resolved from confirm: {snap_token}")
            return snap_token
        ctx = dict(ctx or {})
        stripe_ver = ctx.get("stripe_version") or STRIPE_VERSION_FULL
        deadline = time.time() + 60
        params = {
            "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
            "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
            "elements_session_client[elements_init_source]": "custom_checkout",
            "elements_session_client[referrer_host]": "chatgpt.com",
            "elements_session_client[session_id]": ctx.get("elements_session_id") or f"elements_session_{uuid.uuid4().hex[:11]}",
            "elements_session_client[stripe_js_id]": ctx.get("stripe_js_id") or str(uuid.uuid4()),
            "elements_session_client[locale]": ctx.get("locale") or self.profile.get("stripe_locale") or "en",
            "elements_session_client[is_aggregation_expected]": "false",
            "key": stripe_pk,
            "_stripe_version": stripe_ver,
        }
        params.update(ctx.get("elements_options_client") or _elements_options_client_payload())
        last_err = ""
        poll_i = 0
        while time.time() < deadline:
            poll_i += 1
            r = self.ext.get(f"{STRIPE_API}/v1/payment_pages/{cs_id}", params=params, headers=_stripe_headers(self.profile), timeout=DEFAULT_TIMEOUT)
            if r.status_code == 200:
                payload = r.json() or {}
                redirect_url = self._extract_redirect_url(payload)
                if redirect_url:
                    snap_token = self._fetch_pm_redirect_snap_token(redirect_url)
                    self.s.snap_token = snap_token
                    _safe_log(self.s, f"Midtrans snap token resolved: {snap_token}")
                    return snap_token
                setup_intent = payload.get("setup_intent") or {}
                redirect = (setup_intent.get("next_action") or {}).get("redirect_to_url") or {}
                pm_url = redirect.get("url") or ""
                if setup_intent.get("status") == "requires_action" and pm_url:
                    snap_token = self._fetch_pm_redirect_snap_token(pm_url)
                    self.s.snap_token = snap_token
                    _safe_log(self.s, f"Midtrans snap token resolved: {snap_token}")
                    return snap_token
                submission = payload.get("submission_attempt") or {}
                last_err = (
                    f"setup_intent={setup_intent.get('status')!r} "
                    f"payment_status={payload.get('payment_status')!r} "
                    f"status={payload.get('status')!r} "
                    f"submission={submission.get('state')!r}"
                )
                _safe_log(self.s, f"Stripe redirect poll {poll_i}: {last_err}")
            else:
                last_err = f"http {r.status_code}: {r.text[:120]}"
                _safe_log(self.s, f"Stripe redirect poll {poll_i}: {last_err}")
            time.sleep(1)
        raise GoPayFlowError(f"snap_token resolution timeout: {last_err}")

    def _midtrans_auth(self) -> dict[str, str]:
        token = base64.b64encode(f"{self._midtrans_client_id()}:".encode("ascii")).decode("ascii")
        return {"Authorization": f"Basic {token}"}

    def _midtrans_load_transaction(self, snap_token: str) -> None:
        r = self.ext.get(
            f"https://app.midtrans.com/snap/v1/transactions/{snap_token}",
            headers={"x-source": "snap", "x-source-app-type": "redirection", "x-source-version": "2.3.0"},
            timeout=DEFAULT_TIMEOUT,
        )
        _raise_for_gopay_status(r, "midtrans load transaction")
        enabled = [p.get("type") for p in (r.json() or {}).get("enabled_payments", [])]
        _safe_log(self.s, f"Midtrans payments loaded: {enabled}")

    def _midtrans_init_linking(self, snap_token: str, phone_country_code: str, phone_number: str) -> str:
        url = f"https://app.midtrans.com/snap/v3/accounts/{snap_token}/linking"
        body = {"type": "gopay", "country_code": phone_country_code, "phone_number": phone_number}
        headers = {
            **self._midtrans_auth(),
            "Content-Type": "application/json",
            "Origin": "https://app.midtrans.com",
            "Referer": f"https://app.midtrans.com/snap/v4/redirection/{snap_token}",
        }
        last_err = ""
        for attempt in range(1, LINK_RETRY_LIMIT + 2):
            r = self.ext.post(url, json=body, headers=headers, timeout=DEFAULT_TIMEOUT)
            if r.status_code == 201:
                data = r.json()
                m = re.search(r"reference=([a-f0-9-]{36})", data.get("activation_link_url", ""))
                if not m:
                    raise GoPayFlowError(f"midtrans linking 201 but no reference: {data}")
                self.s.reference_id = m.group(1)
                _safe_log(self.s, f"Midtrans linking ready: {self.s.reference_id}")
                return self.s.reference_id
            if r.status_code in (406, 429):
                try:
                    payload = r.json()
                    last_err = str((payload.get("error_messages") or ["?"])[0]) if isinstance(payload, dict) else str(payload)
                except Exception:
                    last_err = r.text[:120]
                if r.status_code == 429:
                    retry_after = str(r.headers.get("Retry-After") or "").strip()
                    try:
                        sleep_seconds = float(retry_after) if retry_after else LINK_RETRY_SLEEP_SECONDS
                    except Exception:
                        sleep_seconds = LINK_RETRY_SLEEP_SECONDS
                    _safe_log(self.s, f"Midtrans linking rate limited after 429: retry {attempt}/{LINK_RETRY_LIMIT}")
                    time.sleep(sleep_seconds)
                else:
                    _safe_log(self.s, f"Midtrans linking cooling down after 406: retry {attempt}/{LINK_RETRY_LIMIT}")
                    time.sleep(LINK_RETRY_SLEEP_SECONDS)
                continue
            raise GoPayFlowError(f"midtrans linking unexpected status={r.status_code} body={r.text[:300]}")
        raise GoPayFlowError(f"midtrans linking exhausted retries: {last_err}")

    def start_until_otp(self, phone_country_code: str, phone_number: str) -> None:
        _set_phase(self.s, PHASE_STARTING)
        cs_id = self._resolve_checkout()
        stripe_pk = self._fetch_publishable_key(cs_id)
        init_resp, stripe_ver, init_ctx = self._stripe_init_checkout(cs_id, stripe_pk)
        checkout_amount = init_resp.get("total_summary", {}).get("due")
        if checkout_amount is None:
            checkout_amount = (init_resp.get("invoice") or {}).get("amount_due")
        if str(checkout_amount or "").strip() == "34900000":
            raise GoPayFlowError("该账号无试用资格")
        now_ms = int(time.time() * 1000)
        init_ctx.update({
            "guid": uuid.uuid4().hex,
            "muid": uuid.uuid4().hex,
            "sid": uuid.uuid4().hex,
            "page_load_ts": now_ms,
            "time_on_page": self.profile.get("time_on_page") or random.randint(18000, 76000),
            "runtime_version": ((getattr(self.account, "extra", {}) or {}).get("gopay_runtime") or {}).get("version") or DEFAULT_STRIPE_RUNTIME_VERSION,
            "top_checkout_config_id": init_ctx.get("config_id", ""),
            "payment_method_checkout_config_id": init_ctx.get("config_id", ""),
        })
        self._stripe_update_payment_page_address(cs_id, stripe_pk, stripe_ver, init_ctx)
        init_ctx["time_on_page"] = max(int(init_ctx.get("time_on_page") or 0), int(time.time() * 1000) - now_ms)
        pm_id = self._stripe_create_pm(cs_id, stripe_pk, stripe_ver, init_ctx)
        confirm_data = self._stripe_confirm(cs_id, pm_id, stripe_pk, init_resp, stripe_ver, init_ctx)
        if self._confirm_requires_approval(confirm_data):
            self._chatgpt_approve(cs_id)
        else:
            submission = confirm_data.get("submission_attempt") if isinstance(confirm_data, dict) else {}
            state = submission.get("state") if isinstance(submission, dict) else ""
            setup_intent = confirm_data.get("setup_intent") if isinstance(confirm_data, dict) else {}
            setup_status = setup_intent.get("status") if isinstance(setup_intent, dict) else ""
            _safe_log(
                self.s,
                f"ChatGPT approve skipped: submission={state!r} setup_intent={setup_status!r}",
            )
        snap_token = self._follow_redirect_to_midtrans(cs_id, stripe_pk, confirm_data, init_ctx)
        self._midtrans_load_transaction(snap_token)
        reference_id = self._midtrans_init_linking(snap_token, phone_country_code, phone_number)
        self._gopay_validate_reference(reference_id)
        self._gopay_user_consent(reference_id)
        with self.s.lock:
            self.s.otp_waiting_since = _utcnow_iso()
            self.s.otp_auto_resend_done = False
        _set_phase(self.s, PHASE_WAITING_OTP)
        _safe_log(self.s, "Waiting for GoPay OTP")
        _schedule_otp_auto_resend(self.s)

    def _gopay_validate_reference(self, reference_id: str) -> None:
        r = self.ext.post(
            "https://gwa.gopayapi.com/v1/linking/validate-reference",
            json={"reference_id": reference_id},
            headers={"Origin": "https://merchants-gws-app.gopayapi.com", "Referer": "https://merchants-gws-app.gopayapi.com/"},
            timeout=DEFAULT_TIMEOUT,
        )
        _raise_for_gopay_status(r, "gopay validate-reference")
        if not r.json().get("success"):
            raise GoPayFlowError(f"validate-reference failed: {r.text[:300]}")

    def _gopay_user_consent(self, reference_id: str) -> None:
        r = self.ext.post(
            "https://gwa.gopayapi.com/v1/linking/user-consent",
            json={"reference_id": reference_id},
            headers={
                "Origin": "https://merchants-gws-app.gopayapi.com",
                "Referer": "https://merchants-gws-app.gopayapi.com/",
                "x-user-locale": str(self.profile.get("stripe_locale") or "id"),
            },
            timeout=DEFAULT_TIMEOUT,
        )
        _raise_for_gopay_status(r, "gopay user-consent")
        if not r.json().get("success"):
            raise GoPayFlowError(f"user-consent failed: {r.text[:300]}")

    def resend_otp(self, *, auto: bool = False) -> None:
        with self.s.lock:
            if self.s.phase != PHASE_WAITING_OTP:
                raise GoPayFlowError(f"当前阶段不需要重发 OTP: {self.s.phase}")
            reference_id = str(self.s.reference_id or "").strip()
            last_resend_at = self.s.last_otp_resend_at
        if not reference_id:
            raise GoPayFlowError("GoPay 会话缺少 reference_id，无法重发 OTP")

        elapsed = _seconds_since_iso(last_resend_at)
        if elapsed is not None and elapsed < OTP_RESEND_MIN_INTERVAL_SECONDS:
            wait_seconds = int(OTP_RESEND_MIN_INTERVAL_SECONDS - elapsed) + 1
            raise GoPayFlowError(f"GoPay OTP 重发冷却中，请等待 {wait_seconds}s")

        r = self.ext.post(
            "https://gwa.gopayapi.com/v1/linking/resend-otp",
            json={"reference_id": reference_id},
            headers={
                "Origin": "https://merchants-gws-app.gopayapi.com",
                "Referer": "https://merchants-gws-app.gopayapi.com/",
                "x-user-locale": str(self.profile.get("stripe_locale") or "id"),
            },
            timeout=DEFAULT_TIMEOUT,
        )
        _raise_for_gopay_status(r, "gopay resend-otp")
        data = r.json()
        if not data.get("success"):
            raise GoPayFlowError(f"resend-otp failed: {data}")
        with self.s.lock:
            self.s.otp_resend_count += 1
            if auto:
                self.s.otp_auto_resend_done = True
            self.s.last_otp_resend_at = _utcnow_iso()
            self.s.last_error = ""
            self.s.updated_at = _utcnow_iso()
        _safe_log(self.s, "GoPay OTP auto resend requested" if auto else "GoPay OTP resend requested")

    def submit_otp_until_link_pin(self, otp: str) -> None:
        r = self.ext.post(
            "https://gwa.gopayapi.com/v1/linking/validate-otp",
            json={"reference_id": self.s.reference_id, "otp": str(otp).strip()},
            headers={"Origin": "https://merchants-gws-app.gopayapi.com", "Referer": "https://merchants-gws-app.gopayapi.com/"},
            timeout=DEFAULT_TIMEOUT,
        )
        _raise_for_gopay_status(r, "gopay validate-otp")
        data = r.json()
        if not data.get("success"):
            raise GoPayFlowError(f"validate-otp failed: {data}")
        challenge = data.get("data", {}).get("challenge", {}).get("action", {}).get("value", {})
        self.s.link_challenge_id = challenge.get("challenge_id") or ""
        self.s.link_client_id = challenge.get("client_id") or GOPAY_PIN_CLIENT_ID_LINK
        if not self.s.link_challenge_id:
            raise GoPayFlowError(f"validate-otp: missing challenge details {data}")
        _set_phase(self.s, PHASE_WAITING_LINK_PIN)
        _safe_log(self.s, "OTP accepted; waiting for GoPay linking PIN")
        if self.s.default_pin:
            _safe_log(self.s, "OTP accepted; using saved GoPay PIN")
            self.submit_link_pin_until_payment_pin(self.s.default_pin, auto=True)

    def _tokenize_pin(self, challenge_id: str, client_id: str, pin: str) -> str:
        r = self.ext.post(
            "https://customer.gopayapi.com/api/v1/users/pin/tokens/nb",
            json={"challenge_id": challenge_id, "client_id": client_id, "pin": str(pin).strip()},
            headers={
                "x-appversion": "1.0.0",
                "x-correlation-id": str(uuid.uuid4()),
                "x-is-mobile": "false",
                "x-platform": str(self.profile.get("gopay_platform") or self.profile.get("platform") or "Windows 10"),
                "x-request-id": str(uuid.uuid4()),
                "x-user-locale": str(self.profile.get("stripe_locale") or "id"),
                "Origin": "https://pin-web-client.gopayapi.com",
                "Referer": "https://pin-web-client.gopayapi.com/",
            },
            timeout=DEFAULT_TIMEOUT,
        )
        if r.status_code in (400, 401, 403):
            _raise_for_gopay_status(r, "gopay pin tokenize")
        _raise_for_gopay_status(r, "gopay pin tokenize")
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        token = body.get("token") or body.get("data", {}).get("token") or body.get("data", {}).get("pin_token") or ""
        if not token:
            raise GoPayFlowError(f"pin tokenize: no token in response {r.text[:300]}")
        return token

    def submit_link_pin_until_payment_pin(self, pin: str, *, auto: bool = False) -> None:
        pin_token = self._tokenize_pin(self.s.link_challenge_id, self.s.link_client_id, pin)
        r = self.ext.post(
            "https://gwa.gopayapi.com/v1/linking/validate-pin",
            json={"reference_id": self.s.reference_id, "token": pin_token},
            headers={"Origin": "https://merchants-gws-app.gopayapi.com", "Referer": "https://merchants-gws-app.gopayapi.com/"},
            timeout=DEFAULT_TIMEOUT,
        )
        _raise_for_gopay_status(r, "gopay validate-pin")
        if not r.json().get("success"):
            raise GoPayFlowError(f"validate-pin failed: {r.text[:300]}")
        _safe_log(self.s, "GoPay account linking completed")
        self.s.charge_ref = self._midtrans_create_charge()
        self._gopay_payment_validate(self.s.charge_ref)
        challenge_id, client_id = self._gopay_payment_confirm(self.s.charge_ref)
        self.s.payment_challenge_id = challenge_id
        self.s.payment_client_id = client_id or GOPAY_PIN_CLIENT_ID_CHARGE
        _set_phase(self.s, PHASE_WAITING_PAYMENT_PIN)
        _safe_log(self.s, "Waiting for GoPay payment PIN")
        if auto:
            _safe_log(self.s, "Using saved GoPay PIN for payment confirmation")
            self.submit_payment_pin_until_done(pin)

    def _midtrans_create_charge(self) -> str:
        url = f"https://app.midtrans.com/snap/v2/transactions/{self.s.snap_token}/charge"
        headers = {
            **self._midtrans_auth(),
            "Content-Type": "application/json",
            "Origin": "https://app.midtrans.com",
            "Referer": f"https://app.midtrans.com/snap/v4/redirection/{self.s.snap_token}",
        }
        r = self.ext.post(url, json={"payment_type": "gopay", "tokenization": "true", "promo_details": None}, headers=headers, timeout=DEFAULT_TIMEOUT)
        _raise_for_gopay_status(r, "midtrans create charge")
        link = r.json().get("gopay_verification_link_url", "")
        m = re.search(r"reference=([A-Za-z0-9]+)", link)
        if not m:
            raise GoPayFlowError(f"midtrans charge: no reference in {link!r}")
        _safe_log(self.s, f"Midtrans charge created: {m.group(1)}")
        return m.group(1)

    def _gopay_payment_validate(self, charge_ref: str) -> None:
        last = None
        for _ in range(8):
            r = self.ext.get(
                f"https://gwa.gopayapi.com/v1/payment/validate?reference_id={charge_ref}",
                headers={"Origin": "https://merchants-gws-app.gopayapi.com", "Referer": "https://merchants-gws-app.gopayapi.com/"},
                timeout=DEFAULT_TIMEOUT,
            )
            last = r
            if r.status_code == 200 and r.json().get("success"):
                return
            time.sleep(1.5)
        raise GoPayFlowError(f"payment/validate failed after retries: {last.status_code if last else '?'} {last.text[:200] if last else ''}")

    def _gopay_payment_confirm(self, charge_ref: str) -> tuple[str, str]:
        r = self.ext.post(
            f"https://gwa.gopayapi.com/v1/payment/confirm?reference_id={charge_ref}",
            json={"payment_instructions": []},
            headers={"Origin": "https://merchants-gws-app.gopayapi.com", "Referer": "https://merchants-gws-app.gopayapi.com/"},
            timeout=DEFAULT_TIMEOUT,
        )
        _raise_for_gopay_status(r, "gopay payment confirm")
        data = r.json()
        if not data.get("success"):
            raise GoPayFlowError(f"payment/confirm failed: {data}")
        challenge = data.get("data", {}).get("challenge", {}).get("action", {}).get("value", {})
        return str(challenge.get("challenge_id") or ""), str(challenge.get("client_id") or "")

    def submit_payment_pin_until_done(self, pin: str) -> None:
        pin_token = self._tokenize_pin(self.s.payment_challenge_id, self.s.payment_client_id, pin)
        r = self.ext.post(
            f"https://gwa.gopayapi.com/v1/payment/process?reference_id={self.s.charge_ref}",
            json={"challenge": {"type": "GOPAY_PIN_CHALLENGE", "value": {"pin_token": pin_token}}},
            headers={"Origin": "https://merchants-gws-app.gopayapi.com", "Referer": "https://merchants-gws-app.gopayapi.com/"},
            timeout=DEFAULT_TIMEOUT,
        )
        if r.status_code != 200:
            raise GoPayFlowError(f"payment/process {r.status_code}: {r.text[:600]}")
        data = r.json()
        if not data.get("success") or data.get("data", {}).get("next_action") != "payment-success":
            raise GoPayFlowError(f"payment/process failed: {data}")
        _safe_log(self.s, "GoPay charge settled")
        _set_phase(self.s, PHASE_VERIFYING)
        self.s.result = self._chatgpt_verify()
        _set_phase(self.s, PHASE_SUCCEEDED if self.s.result.get("state") == "succeeded" else PHASE_FAILED, error=self.s.result.get("error", ""))

    def _chatgpt_verify(self) -> dict[str, Any]:
        deadline = time.time() + 60
        while time.time() < deadline:
            r = _get_chatgpt_with_profile(
                self.account,
                "https://chatgpt.com/checkout/verify",
                params={
                    "stripe_session_id": self.s.cs_id,
                    "processor_entity": self._processor_entity(),
                    "plan_type": "plus",
                },
                proxy=self.proxy,
                profile=self.profile,
            )
            if r.status_code == 200:
                _safe_log(self.s, "ChatGPT checkout verified")
                return {"state": "succeeded", "cs_id": self.s.cs_id}
            time.sleep(2)
        return {"state": "verify_timeout", "cs_id": self.s.cs_id, "error": "verify timeout"}


def create_gopay_session(
    account_id: int,
    account: Any,
    *,
    plan: str,
    country: str,
    currency: str,
    proxy: str,
    phone_country_code: str,
    phone_number: str,
    checkout_url: str = "",
    default_pin: str = "",
    billing: Optional[dict[str, Any]] = None,
    proxy_source: str = "none",
    browser_profile: Optional[dict[str, Any]] = None,
    otp_auto_resend_delay_seconds: int = DEFAULT_OTP_AUTO_RESEND_DELAY_SECONDS,
) -> dict[str, Any]:
    if str(plan or "plus").strip().lower() != "plus":
        raise GoPayFlowError("GoPay 当前仅支持 Plus 订阅")
    phone_country_code = re.sub(r"\D", "", str(phone_country_code or "").lstrip("+"))
    phone_number = re.sub(r"\D", "", str(phone_number or ""))
    if not phone_country_code or not phone_number:
        raise GoPayFlowError("缺少 GoPay 手机区号或手机号")
    checkout_url = str(checkout_url or "").strip()
    if checkout_url:
        parse_checkout_url(checkout_url)
    default_pin = _normalize_gopay_pin(default_pin)
    billing = dict(billing or {})

    session = GoPaySession(
        session_id=f"gp_{uuid.uuid4().hex}",
        account_id=int(account_id),
        email=str(getattr(account, "email", "") or ""),
        plan="plus",
        country=normalize_checkout_country(country),
        currency=normalize_checkout_currency(currency, country),
        phone_country_code=phone_country_code,
        phone_number=phone_number,
        proxy=str(proxy or ""),
        proxy_source=str(proxy_source or "none"),
        checkout_url=checkout_url,
        default_pin=default_pin,
        billing=billing,
        browser_profile=dict(browser_profile or {}),
        otp_auto_resend_delay_seconds=_normalize_otp_auto_resend_delay(otp_auto_resend_delay_seconds),
    )
    with _SESSIONS_LOCK:
        _SESSIONS[session.session_id] = session

    def _worker() -> None:
        try:
            runner = GoPayRunner(session, account)
            _set_runner(session, runner)
            runner.start_until_otp(phone_country_code, phone_number)
        except Exception as exc:
            error = _translate_gopay_error_message(exc)
            _safe_log(session, f"FAILED: {error}")
            _set_phase(session, PHASE_FAILED, error=error)

    threading.Thread(target=_worker, daemon=True).start()
    return _snapshot(session)


def resend_gopay_otp(session_id: str) -> dict[str, Any]:
    session = _SESSIONS.get(session_id)
    if not session:
        raise KeyError(session_id)
    _get_runner(session).resend_otp(auto=False)
    return _snapshot(session)


def submit_gopay_otp(session_id: str, account: Any, otp: str) -> dict[str, Any]:
    session = _SESSIONS.get(session_id)
    if not session:
        raise KeyError(session_id)
    if session.phase != PHASE_WAITING_OTP:
        raise GoPayFlowError(f"当前阶段不需要 OTP: {session.phase}")
    try:
        _get_runner(session).submit_otp_until_link_pin(otp)
    except Exception as exc:
        error = _translate_gopay_error_message(exc)
        _safe_log(session, f"OTP step failed: {error}")
        with session.lock:
            session.last_error = error
            session.updated_at = _utcnow_iso()
            if session.phase not in {PHASE_WAITING_LINK_PIN, PHASE_WAITING_PAYMENT_PIN, PHASE_VERIFYING, PHASE_SUCCEEDED}:
                session.phase = PHASE_WAITING_OTP
        if isinstance(exc, GoPayFlowError):
            raise GoPayFlowError(error) from exc
        raise GoPayFlowError(error) from exc
    return _snapshot(session)


def submit_gopay_pin(session_id: str, account: Any, pin: str) -> dict[str, Any]:
    session = _SESSIONS.get(session_id)
    if not session:
        raise KeyError(session_id)
    try:
        if session.phase == PHASE_WAITING_LINK_PIN:
            _get_runner(session).submit_link_pin_until_payment_pin(pin, auto=True)
        elif session.phase == PHASE_WAITING_PAYMENT_PIN:
            _get_runner(session).submit_payment_pin_until_done(pin)
        else:
            raise GoPayFlowError(f"当前阶段不需要 PIN: {session.phase}")
    except Exception as exc:
        error = _translate_gopay_error_message(exc)
        _safe_log(session, f"PIN step failed: {error}")
        with session.lock:
            session.last_error = error
            session.updated_at = _utcnow_iso()
        if isinstance(exc, GoPayFlowError):
            raise GoPayFlowError(error) from exc
        raise GoPayFlowError(error) from exc
    return _snapshot(session)


def cancel_gopay_session(session_id: str) -> dict[str, Any]:
    session = _SESSIONS.get(session_id)
    if not session:
        raise KeyError(session_id)
    _set_phase(session, PHASE_CANCELLED)
    _safe_log(session, "Session cancelled")
    return _snapshot(session)
