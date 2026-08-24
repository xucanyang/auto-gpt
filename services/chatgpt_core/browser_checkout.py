"""Camoufox-backed Checkout transport.

The payment eligibility parser owns the request bodies and business
classification.  This module only supplies the HTTP-like ``post`` contract
over a short-lived, account-scoped browser context so the same cookies,
origin, user agent and proxy are used for the whole Checkout chain.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, Callable, Mapping

from core.proxy_utils import normalize_proxy_url
from services.chatgpt_core.browser_cookies import browser_cookie_items
from services.chatgpt_core.shared_camoufox import shared_camoufox_registration_session


_CHATGPT_ORIGIN = "https://chatgpt.com"
_ALLOWED_PATHS = frozenset(
    {
        "/backend-api/payments/checkout",
        "/backend-api/payments/checkout/update",
        "/backend-api/payments/checkout/taxes",
    }
)
_DEFAULT_NAVIGATION_TIMEOUT_MS = 45_000
_DEFAULT_REQUEST_TIMEOUT_MS = 30_000


def _safe_text(value: Any, limit: int = 600) -> str:
    text = str(value or "").strip().replace("\r", " ").replace("\n", " ")
    return text[:limit]


def _account_extra(account: Any) -> dict[str, Any]:
    try:
        value = account.get_extra() if callable(getattr(account, "get_extra", None)) else getattr(account, "extra", {})
    except Exception:
        value = {}
    return dict(value) if isinstance(value, dict) else {}


def _access_token(account: Any, extra: Mapping[str, Any]) -> str:
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


def _account_identifier(account: Any, extra: Mapping[str, Any]) -> str:
    return str(
        extra.get("account_id")
        or extra.get("chatgpt_account_id")
        or extra.get("workspace_id")
        or getattr(account, "user_id", "")
        or ""
    ).strip()


def _response_detail(payload: Any, text: Any = "") -> str:
    if isinstance(payload, dict):
        candidates: list[Any] = [
            payload.get("detail"),
            payload.get("message"),
            payload.get("msg"),
        ]
        error = payload.get("error")
        if isinstance(error, dict):
            candidates.extend(
                (
                    error.get("message"),
                    error.get("detail"),
                    error.get("code"),
                    error.get("type"),
                )
            )
        else:
            candidates.append(error)
        for candidate in candidates:
            if isinstance(candidate, (str, int, float)) and str(candidate).strip():
                return _safe_text(candidate)
    return _safe_text(text)


_FETCH_SCRIPT = """
async ({path, body, headers, referrer}) => {
  try {
    const response = await fetch(path, {
      method: 'POST',
      credentials: 'include',
      mode: 'same-origin',
      referrer,
      headers,
      body: JSON.stringify(body),
    });
    const text = await response.text();
    let payload = null;
    try {
      payload = text ? JSON.parse(text) : {};
    } catch (_error) {
      payload = null;
    }
    return {status: response.status, payload, text: text.slice(0, 4000)};
  } catch (error) {
    return {transport_error: String(error && error.message || error || 'fetch failed')};
  }
}
"""

_STRIPE_FETCH_SCRIPT = """
async ({url, body, headers}) => {
  try {
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(body || {})) {
      params.set(key, String(value ?? ''));
    }
    const response = await fetch(url, {
      method: 'POST',
      mode: 'cors',
      credentials: 'omit',
      headers,
      body: params.toString(),
    });
    const text = await response.text();
    let payload = null;
    try {
      payload = text ? JSON.parse(text) : {};
    } catch (_error) {
      payload = null;
    }
    return {status: response.status, payload, text: text.slice(0, 4000)};
  } catch (error) {
    return {transport_error: String(error && error.message || error || 'fetch failed')};
  }
}
"""


class BrowserCheckoutClient:
    """HTTP-compatible Checkout client backed by one Camoufox Context."""

    def __init__(
        self,
        account: Any,
        profile: Mapping[str, Any] | None = None,
        stop_checker: Callable[[], None] | None = None,
        *,
        reuse_session: bool = False,
        headless: bool = True,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        self.account = account
        self.profile = dict(profile or {})
        self.stop_checker = stop_checker
        self.headless = bool(headless)
        self.logger = logger or (lambda _message: None)
        self.extra = _account_extra(account)
        self.access_token = _access_token(account, self.extra)
        if not self.access_token:
            from services.chatgpt_core.payment_eligibility import PaymentEligibilityProbeError

            raise PaymentEligibilityProbeError("账号缺少 Access Token")
        self.account_identifier = _account_identifier(account, self.extra)
        self.cookie_items, self.cookies_are_structured = browser_cookie_items(
            account,
            self.extra,
        )
        self._session_cm: AbstractContextManager[Any] | None = None
        self._session: Any = None
        self._page: Any = None
        self._proxy = ""
        self._closed = False
        # Browser requests always share one context.  Keep the argument for
        # parity with the protocol client and future transport diagnostics.
        self.reuse_session = bool(reuse_session)

    def checkpoint(self) -> None:
        if self.stop_checker is not None:
            self.stop_checker()

    def _deep_camoufox_profile(self) -> Any:
        from services.chatgpt_core.browser_identity import browser_fingerprint_to_dict
        from services.chatgpt_core.shared_browser import ensure_deep_browser_fingerprint

        source = self.profile.get("browser_fingerprint")
        if not source:
            source = {
                key: self.profile.get(key)
                for key in ("device_id", "accept_language", "locale", "timezone")
                if self.profile.get(key)
            }
        payload = browser_fingerprint_to_dict(source)
        # Checkout browser contexts are intentionally Camoufox/Firefox.  A
        # legacy protocol/Chrome payload can provide stable device/language
        # hints, but cannot be passed to Camoufox as a Chromium profile.
        payload["browser_family"] = "firefox"
        payload.pop("chromium_config", None)
        return ensure_deep_browser_fingerprint(payload, default_family="firefox")

    def _context_cookie_payload(self) -> list[dict[str, Any]]:
        payload: list[dict[str, Any]] = []
        for item in self.cookie_items:
            cookie = dict(item)
            if "url" not in cookie and not (
                str(cookie.get("domain") or "").strip()
                and str(cookie.get("path") or "").strip()
            ):
                # Structured cookies without a scope are ambiguous; do not
                # broaden them silently.
                continue
            payload.append(cookie)
        return payload

    def _open_context(self, proxy: str) -> None:
        self.checkpoint()
        normalized_proxy = normalize_proxy_url(proxy) or ""
        if self._session is not None:
            if normalized_proxy != self._proxy:
                from services.chatgpt_core.payment_eligibility import PaymentEligibilityProbeError

                raise PaymentEligibilityProbeError(
                    "浏览器 Checkout 的代理出口发生变化"
                )
            return
        self._proxy = normalized_proxy
        try:
            self._session_cm = shared_camoufox_registration_session(
                headless=self.headless,
                proxy=normalized_proxy or None,
                browser_fingerprint=self._deep_camoufox_profile(),
                logger=self.logger,
            )
            self._session = self._session_cm.__enter__()
            self._page = self._session.page
            self._page.set_default_timeout(_DEFAULT_REQUEST_TIMEOUT_MS)
            self._page.set_default_navigation_timeout(_DEFAULT_NAVIGATION_TIMEOUT_MS)
            cookies = self._context_cookie_payload()
            if cookies:
                self._session.context.add_cookies(cookies)
            self.checkpoint()
            self._page.goto(
                f"{_CHATGPT_ORIGIN}/",
                wait_until="domcontentloaded",
                timeout=_DEFAULT_NAVIGATION_TIMEOUT_MS,
            )
            self.checkpoint()
            self.logger(
                "[control] checkout_browser_context=ready "
                f"cookies={'structured' if self.cookies_are_structured else 'legacy'}"
            )
        except Exception:
            self._close_context()
            raise

    def _headers(self, path: str) -> dict[str, str]:
        headers = {
            "Accept": "*/*",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.access_token}",
            "oai-device-id": str(self.profile.get("device_id") or ""),
            "oai-language": "en-US",
            "x-openai-target-path": path,
            "x-openai-target-route": path,
        }
        if self.account_identifier:
            headers["chatgpt-account-id"] = self.account_identifier
        return headers

    def post(
        self,
        path: str,
        body: dict[str, Any],
        proxy: str,
        stage: str,
        *,
        referer: str = "",
    ) -> dict[str, Any]:
        from services.chatgpt_core.payment_eligibility import (
            PaymentEligibilityHttpError,
            PaymentEligibilityProbeError,
            PaymentEligibilityProtocolError,
        )

        if path not in _ALLOWED_PATHS:
            raise PaymentEligibilityProtocolError(
                f"浏览器 Checkout 禁止访问未授权路径: {path}"
            )
        self.checkpoint()
        self._open_context(proxy)
        try:
            result = self._page.evaluate(
                _FETCH_SCRIPT,
                {
                    "path": path,
                    "body": body,
                    "headers": self._headers(path),
                    "referrer": referer or f"{_CHATGPT_ORIGIN}/",
                },
            )
        except Exception as exc:
            raise PaymentEligibilityProbeError(
                f"{stage} 网络失败: {_safe_text(exc)}"
            ) from exc
        self.checkpoint()
        if not isinstance(result, dict):
            raise PaymentEligibilityProtocolError(f"{stage} 浏览器返回格式无效")
        transport_error = _safe_text(result.get("transport_error"))
        if transport_error:
            raise PaymentEligibilityProbeError(
                f"{stage} 网络失败: {transport_error}"
            )
        try:
            status = int(result.get("status") or 0)
        except (TypeError, ValueError):
            status = 0
        payload = result.get("payload")
        text = result.get("text")
        if status >= 400:
            raise PaymentEligibilityHttpError(
                stage,
                status,
                _response_detail(payload, text),
            )
        if not isinstance(payload, dict):
            raise PaymentEligibilityProtocolError(f"{stage} 返回不是 JSON")
        return payload

    def stripe_payment_page_init(
        self,
        session_id: str,
        checkout_profile: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Read Stripe's existing payment-page state inside this browser."""

        from services.chatgpt_core.checkout_probe import (
            DEFAULT_STRIPE_PK,
            KNOWN_PUBLISHABLE_KEYS,
            STRIPE_API,
            STRIPE_VERSION_BASE,
            _extract_payment_method_types,
        )
        from services.chatgpt_core.payment_eligibility import (
            PaymentEligibilityHttpError,
            PaymentEligibilityProbeError,
            PaymentEligibilityProtocolError,
        )

        self.checkpoint()
        if self._page is None:
            raise PaymentEligibilityProbeError("Stripe 金额读取前浏览器 Context 未就绪")
        configured = str(self.extra.get("stripe_publishable_key") or DEFAULT_STRIPE_PK).strip()
        keys = [configured, *[item for item in KNOWN_PUBLISHABLE_KEYS if item != configured]]
        url = f"{STRIPE_API}/v1/payment_pages/{str(session_id).strip()}/init"
        base_body = {
            "_stripe_version": STRIPE_VERSION_BASE,
            "browser_locale": str(
                checkout_profile.get("locale")
                or checkout_profile.get("billing_country")
                or "en-US"
            ),
        }
        last_status = 0
        last_detail = ""
        payload: dict[str, Any] | None = None
        selected_key = ""
        for key in keys:
            self.checkpoint()
            try:
                result = self._page.evaluate(
                    _STRIPE_FETCH_SCRIPT,
                    {
                        "url": url,
                        "body": {**base_body, "key": key},
                        "headers": {
                            "Accept": "application/json",
                            "Content-Type": "application/x-www-form-urlencoded",
                            "Origin": "https://chatgpt.com",
                            "Referer": "https://chatgpt.com/",
                            "Accept-Language": str(
                                self.profile.get("accept_language") or "en-US,en;q=0.9"
                            ),
                        },
                    },
                )
            except Exception as exc:
                raise PaymentEligibilityProbeError(
                    f"Stripe payment_pages init 网络失败: {_safe_text(exc)}"
                ) from exc
            if not isinstance(result, dict):
                raise PaymentEligibilityProtocolError(
                    "Stripe payment_pages init 浏览器返回格式无效"
                )
            transport_error = _safe_text(result.get("transport_error"))
            if transport_error:
                raise PaymentEligibilityProbeError(
                    f"Stripe payment_pages init 网络失败: {transport_error}"
                )
            try:
                status = int(result.get("status") or 0)
            except (TypeError, ValueError):
                status = 0
            last_status = status
            detail = _response_detail(result.get("payload"), result.get("text"))
            last_detail = detail
            candidate = result.get("payload")
            if status < 400 and isinstance(candidate, dict):
                payload = candidate
                selected_key = key
                break
        if payload is None:
            raise PaymentEligibilityHttpError(
                "Stripe payment_pages init",
                last_status or 502,
                last_detail,
            )
        total_summary = payload.get("total_summary")
        total_summary = total_summary if isinstance(total_summary, dict) else {}
        invoice = payload.get("invoice")
        invoice = invoice if isinstance(invoice, dict) else {}
        amount = (
            total_summary.get("due")
            if total_summary.get("due") is not None
            else invoice.get("amount_due")
        )
        currency = str(
            payload.get("currency")
            or checkout_profile.get("currency")
            or ""
        ).lower()
        return {
            "amount": amount,
            "amount_text": str(amount if amount is not None else ""),
            "amount_source": (
                "stripe.payment_pages.init.total_summary.due"
                if total_summary.get("due") is not None
                else "stripe.payment_pages.init.invoice.amount_due"
            ),
            "amount_is_zero": str(amount or "").strip() in {"0", "0.0", "0.00"},
            "currency": currency,
            "payment_method_types": _extract_payment_method_types(payload),
            "stripe_publishable_key_prefix": (
                selected_key[:28] + "..." if selected_key else ""
            ),
            "stripe_payload": payload,
        }

    def _close_context(self) -> None:
        session_cm, self._session_cm = self._session_cm, None
        self._page = None
        self._session = None
        if session_cm is not None:
            try:
                session_cm.__exit__(None, None, None)
            except Exception:
                pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._close_context()


__all__ = ["BrowserCheckoutClient"]
