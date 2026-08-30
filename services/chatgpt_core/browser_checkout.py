"""Deployment-browser-backed Checkout transport.

The payment eligibility parser owns the request bodies and business
classification.  This module only supplies the HTTP-like ``post`` contract
over a short-lived, account-scoped browser context so the same cookies,
origin, user agent and proxy are used for the whole Checkout chain.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
import os
import re
import secrets
import uuid
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from core.proxy_utils import build_requests_proxy_config, normalize_proxy_url
from services.chatgpt_core.browser_cookies import browser_cookie_items
from services.chatgpt_core.shared_browser import shared_browser_registration_session


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
_DEFAULT_CHECKOUT_ATTEMPT_TIMEOUT_MS = 60_000
_SENTINEL_LOADER_URL = f"{_CHATGPT_ORIGIN}/backend-api/sentinel/sdk.js"
_SENTINEL_PROBE_URL = f"{_CHATGPT_ORIGIN}/checkout/openai_llc/auto-gpt-sentinel"
_DEFAULT_CLIENT_BUILD_NUMBER = "9758774"
_DEFAULT_CLIENT_VERSION = "prod-180ca8b8699a733aef330b7026892aee9bf85fbe"
_NO_EVALUATE_ARGUMENT = object()


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


def _configured_client_metadata() -> dict[str, str]:
    build_number = str(
        os.getenv("AUTO_GPT_CHATGPT_CLIENT_BUILD_NUMBER", _DEFAULT_CLIENT_BUILD_NUMBER)
        or ""
    ).strip()
    version = str(
        os.getenv("AUTO_GPT_CHATGPT_CLIENT_VERSION", _DEFAULT_CLIENT_VERSION) or ""
    ).strip()
    return {
        "buildNumber": build_number if re.fullmatch(r"[0-9]{1,32}", build_number) else "",
        "version": (
            version
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}", version)
            else ""
        ),
    }


def _checkout_attempt_timeout_ms() -> int:
    default_seconds = _DEFAULT_CHECKOUT_ATTEMPT_TIMEOUT_MS // 1000
    try:
        seconds = int(
            float(
                str(
                    os.getenv(
                        "AUTO_GPT_CHECKOUT_BROWSER_ATTEMPT_TIMEOUT_SECONDS",
                        str(default_seconds),
                    )
                    or str(default_seconds)
                ).strip()
            )
        )
    except (TypeError, ValueError):
        seconds = default_seconds
    return max(15_000, min(seconds * 1000, 120_000))


def _origin_client_metadata(
    configured: Mapping[str, Any],
    page_values: Mapping[str, Any] | None,
) -> tuple[dict[str, str], str]:
    metadata = {
        "buildNumber": str(configured.get("buildNumber") or "").strip(),
        "version": str(configured.get("version") or "").strip(),
    }
    values = dict(page_values or {})
    sequence = str(values.get("sequence") or "").strip()
    build = str(values.get("build") or "").strip()
    dynamic_fields = 0
    if re.fullmatch(r"[0-9]{1,32}", sequence):
        metadata["buildNumber"] = sequence
        dynamic_fields += 1
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}", build):
        metadata["version"] = build
        dynamic_fields += 1
    return metadata, (
        "origin"
        if dynamic_fields == 2
        else "origin_partial"
        if dynamic_fields == 1
        else "configured"
    )


_FETCH_SCRIPT = """
async ({path, body, headers, referrer, requireSentinel, clientMetadata, requestTimeoutMs, attemptTimeoutMs}) => {
  const attemptStartedAt = Date.now();
  const timeoutMs = Math.min(
    Math.max(Number(requestTimeoutMs) || 30000, 1000),
    60000,
  );
  const attemptBudgetMs = Math.min(
    Math.max(Number(attemptTimeoutMs) || 60000, 15000),
    120000,
  );
  const attemptDeadline = attemptStartedAt + attemptBudgetMs;
  const warmupStatuses = [];
  const warmupDurationsMs = [];
  let failureStage = 'request';
  let sentinelMeta = {
    token_length: 0,
    token_json_parsed: false,
    has_p: false,
    has_t: false,
    has_c: false,
    p_length: 0,
    t_length: 0,
    c_length: 0,
  };
  let telemetry = '';
  const diagnostics = () => ({
    ...sentinelMeta,
    warmup_statuses: warmupStatuses.slice(0, 8),
    warmup_durations_ms: warmupDurationsMs.slice(0, 8),
    attempt_duration_ms: Math.max(0, Date.now() - attemptStartedAt),
  });
  const sentinelFailure = (code, message) => ({
    sentinel_error: {
      code,
      message,
      diagnostics: diagnostics(),
    },
  });
  const withTimeout = async (label, operation, abortable = false) => {
    const remainingMs = attemptDeadline - Date.now();
    if (remainingMs <= 0) {
      throw new Error(`attempt deadline exceeded before ${label}`);
    }
    const operationTimeoutMs = Math.max(1, Math.min(timeoutMs, remainingMs));
    const controller = abortable ? new AbortController() : null;
    let timer = null;
    try {
      return await Promise.race([
        Promise.resolve().then(() => operation(controller ? controller.signal : undefined)),
        new Promise((_, reject) => {
          timer = setTimeout(() => {
            if (controller) controller.abort();
            reject(new Error(`${label} timed out after ${operationTimeoutMs}ms`));
          }, operationTimeoutMs);
        }),
      ]);
    } finally {
      if (timer !== null) clearTimeout(timer);
    }
  };
  try {
    const callerHeaders = {...(headers || {})};
    for (const name of Object.keys(callerHeaders)) {
      const normalized = String(name).toLowerCase();
      if (
        normalized === 'oai-client-build-number' ||
        normalized === 'oai-client-version' ||
        normalized === 'openai-sentinel-token' ||
        normalized === 'oai-telemetry'
      ) {
        delete callerHeaders[name];
      }
    }
    const applyClientMetadata = (target) => {
      if (clientMetadata && clientMetadata.buildNumber) {
        target['oai-client-build-number'] = String(clientMetadata.buildNumber);
      }
      if (clientMetadata && clientMetadata.version) {
        target['oai-client-version'] = String(clientMetadata.version);
      }
      return target;
    };
    const authenticatedHeaders = applyClientMetadata({...callerHeaders});
    const warmup = async (warmupPath, method = 'GET') => {
      const startedAt = Date.now();
      const warmupHeaders = applyClientMetadata({
        ...authenticatedHeaders,
        'x-openai-target-path': warmupPath,
        'x-openai-target-route': warmupPath,
      });
      try {
        const warmupResponse = await withTimeout(
          `warmup ${warmupPath}`,
          (signal) => fetch(warmupPath, {
            method,
            credentials: 'include',
            headers: warmupHeaders,
            body: method === 'POST' ? '' : undefined,
            signal,
          }),
          true,
        );
        return {
          status: Number(warmupResponse.status) || 0,
          duration_ms: Math.max(0, Date.now() - startedAt),
        };
      } catch (_error) {
        return {
          status: 0,
          duration_ms: Math.max(0, Date.now() - startedAt),
        };
      }
    };
    const recordWarmups = (results) => {
      for (const result of results) {
        warmupStatuses.push(Number(result && result.status) || 0);
        warmupDurationsMs.push(Number(result && result.duration_ms) || 0);
      }
    };

    if (requireSentinel) {
      failureStage = 'sentinel_warmup';
      const timezoneOffset = new Date().getTimezoneOffset();
      recordWarmups(await Promise.all([
        warmup('/backend-api/accounts/optimized/check'),
        warmup('/backend-api/me'),
        warmup(`/backend-api/accounts/check/v4-2023-04-27?timezone_offset_min=${encodeURIComponent(timezoneOffset)}`),
      ]));

      failureStage = 'sentinel_token';
      const rawToken = await withTimeout(
        'Sentinel SDK token',
        () => window.SentinelSDK.token('chatgpt_checkout'),
      );
      if (typeof rawToken !== 'string' || rawToken.length === 0) {
        return sentinelFailure(
          'sentinel_token_missing',
          'Sentinel SDK returned no token',
        );
      }
      sentinelMeta.token_length = rawToken.length;
      let sentinel = null;
      try {
        sentinel = JSON.parse(rawToken);
        sentinelMeta.token_json_parsed = Boolean(
          sentinel && typeof sentinel === 'object' && !Array.isArray(sentinel)
        );
      } catch (_error) {}
      if (sentinelMeta.token_json_parsed) {
        sentinelMeta.has_p = typeof sentinel.p === 'string' && sentinel.p.length > 0;
        sentinelMeta.has_t = typeof sentinel.t === 'string' && sentinel.t.length > 0;
        sentinelMeta.has_c = typeof sentinel.c === 'string' && sentinel.c.length > 0;
        sentinelMeta.p_length = typeof sentinel.p === 'string' ? sentinel.p.length : 0;
        sentinelMeta.t_length = typeof sentinel.t === 'string' ? sentinel.t.length : 0;
        sentinelMeta.c_length = typeof sentinel.c === 'string' ? sentinel.c.length : 0;
      }
      if (!sentinelMeta.has_t) {
        return sentinelFailure(
          'sentinel_token_incomplete',
          'Sentinel SDK returned no browser enforcement token',
        );
      }

      telemetry = window.SentinelSDK.timing?.() ?? '[1,null]';
      if (typeof telemetry !== 'string') telemetry = JSON.stringify(telemetry);
      authenticatedHeaders['OpenAI-Sentinel-Token'] = rawToken;
      authenticatedHeaders['OAI-Telemetry'] = telemetry || '[1,null]';
      failureStage = 'sentinel_ping';
      recordWarmups([await warmup('/backend-api/sentinel/ping', 'POST')]);
      if (Date.now() >= attemptDeadline) {
        return sentinelFailure(
          'sentinel_attempt_timeout',
          'Checkout attempt deadline expired during Sentinel preparation',
        );
      }
      // Browser-generated values always win over caller placeholders.
      authenticatedHeaders['OpenAI-Sentinel-Token'] = rawToken;
      authenticatedHeaders['OAI-Telemetry'] = telemetry || '[1,null]';
    }

    failureStage = 'checkout_fetch';
    const response = await withTimeout(
      `${path} fetch`,
      (signal) => fetch(path, {
        method: 'POST',
        credentials: 'include',
        mode: 'same-origin',
        referrer,
        headers: authenticatedHeaders,
        body: JSON.stringify(body),
        signal,
      }),
      true,
    );
    failureStage = 'checkout_response';
    const text = await withTimeout(
      `${path} response body`,
      () => response.text(),
    );
    let payload = null;
    try {
      payload = text ? JSON.parse(text) : {};
    } catch (_error) {
      payload = null;
    }
    return {
      status: response.status,
      payload,
      text: text.slice(0, 4000),
      sentinel_meta: sentinelMeta,
      telemetry,
      warmup_statuses: warmupStatuses,
      warmup_durations_ms: warmupDurationsMs,
      attempt_duration_ms: Math.max(0, Date.now() - attemptStartedAt),
    };
  } catch (error) {
    const message = String(error && error.message || error || 'fetch failed');
    if (requireSentinel && failureStage.startsWith('sentinel_')) {
      const timedOut = message.includes('timed out') || message.includes('deadline');
      return sentinelFailure(
        timedOut ? 'sentinel_attempt_timeout' : 'sentinel_prepare_failed',
        message,
      );
    }
    return {
      transport_error: message,
      failure_stage: failureStage,
      diagnostics: diagnostics(),
    };
  }
}
"""

def _new_stripe_http_session(profile: Mapping[str, Any], proxy: str) -> Any:
    """Build Stripe's JS-origin HTTP channel on the frozen Checkout route."""

    from curl_cffi import requests as cffi_requests

    session = cffi_requests.Session(
        impersonate=str(profile.get("impersonate") or "firefox147")
    )
    session.headers.update(
        {
            "User-Agent": str(profile.get("ua") or ""),
            "Accept": "application/json",
            "Accept-Language": str(
                profile.get("accept_language") or "en-US,en;q=0.9"
            ),
            "Origin": "https://js.stripe.com",
            "Referer": "https://js.stripe.com/",
        }
    )
    proxies = build_requests_proxy_config(proxy or None)
    if proxies:
        session.proxies = proxies
    return session


class BrowserCheckoutClient:
    """HTTP-compatible Checkout client backed by one native browser Context."""

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
        self._client_metadata = _configured_client_metadata()
        self._client_metadata_source = "configured"
        self._page_source = "uninitialized"
        self._effective_profile: dict[str, Any] = {}
        self._oai_session_id = str(uuid.uuid4())
        # Browser requests always share one context.  Keep the argument for
        # parity with the protocol client and future transport diagnostics.
        self.reuse_session = bool(reuse_session)

    def checkpoint(self) -> None:
        if self.stop_checker is not None:
            self.stop_checker()

    def _page_evaluate(
        self,
        expression: str,
        argument: Any = _NO_EVALUATE_ARGUMENT,
    ) -> Any:
        """Evaluate page-owned globals in Patchright's main world.

        Patchright defaults ``page.evaluate`` to an isolated execution world.
        That world can access the DOM but cannot see globals installed by page
        scripts, including ``window.SentinelSDK``. Camoufox uses Playwright's
        original signature and must not receive the Patchright-only option.
        """

        from services.chatgpt_core.browser_identity import (
            PATCHRIGHT_BROWSER_RUNTIME,
            configured_browser_runtime,
        )

        backend = str(getattr(self._session, "browser_backend", "") or "")
        is_patchright = backend == "patchright_chromium" or (
            not backend
            and configured_browser_runtime() == PATCHRIGHT_BROWSER_RUNTIME
        )
        options = {"isolated_context": False} if is_patchright else {}
        if argument is _NO_EVALUATE_ARGUMENT:
            return self._page.evaluate(expression, **options)
        return self._page.evaluate(expression, argument, **options)

    def _deep_browser_profile(self) -> Any:
        from services.chatgpt_core.browser_identity import (
            browser_fingerprint_to_dict,
            rebind_browser_fingerprint_geo,
            resolve_browser_geo_identity,
        )
        from services.chatgpt_core.shared_browser import (
            ensure_deep_browser_fingerprint,
        )

        source = self.profile.get("browser_fingerprint")
        if not source:
            source = {
                key: self.profile.get(key)
                for key in ("device_id", "accept_language", "locale", "timezone")
                if self.profile.get(key)
            }
        payload = browser_fingerprint_to_dict(source)
        profile = ensure_deep_browser_fingerprint(payload, default_family="chrome")
        verified_geo = self.profile.get("verified_proxy_geo")
        if isinstance(verified_geo, Mapping):
            exit_ip = str(verified_geo.get("exit_ip") or "").strip()
            country_code = str(verified_geo.get("country_code") or "").strip()
            if exit_ip:
                geo_identity = resolve_browser_geo_identity(
                    exit_ip,
                    country_code=country_code,
                )
                profile = rebind_browser_fingerprint_geo(profile, geo_identity)
                self.logger(
                    "[control] checkout_browser_geo=verified "
                    f"country={geo_identity.country_code or country_code or '-'} "
                    f"source={geo_identity.source}"
                )
        self._effective_profile = browser_fingerprint_to_dict(profile)
        self._effective_profile["ua"] = str(
            self._effective_profile.get("user_agent") or self.profile.get("ua") or ""
        )
        return profile

    def _context_cookie_payload(self) -> list[dict[str, Any]]:
        payload: list[dict[str, Any]] = []
        for item in self.cookie_items:
            cookie = dict(item)
            if str(cookie.get("name") or "").strip().lower() == "oai-did":
                continue
            if "url" not in cookie and not (
                str(cookie.get("domain") or "").strip()
                and str(cookie.get("path") or "").strip()
            ):
                # Structured cookies without a scope are ambiguous; do not
                # broaden them silently.
                continue
            payload.append(cookie)
        effective_profile = self._effective_profile or self.profile
        device_id = str(effective_profile.get("device_id") or "").strip()
        if device_id:
            payload.append(
                {
                    "name": "oai-did",
                    "value": device_id,
                    "domain": "chatgpt.com",
                    "path": "/",
                    "secure": True,
                    "sameSite": "Lax",
                }
            )
        return payload

    def _load_probe_page(self) -> None:
        def fulfill_probe(route: Any) -> None:
            route.fulfill(
                status=200,
                content_type="text/html",
                body=(
                    "<!doctype html><html><head><meta charset='utf-8'>"
                    f"<script src='{_SENTINEL_LOADER_URL}'></script>"
                    "</head><body></body></html>"
                ),
            )

        self._page.route(_SENTINEL_PROBE_URL, fulfill_probe)
        self._page.goto(
            _SENTINEL_PROBE_URL,
            wait_until="domcontentloaded",
            timeout=_DEFAULT_NAVIGATION_TIMEOUT_MS,
        )
        self._page.wait_for_function(
            "() => Boolean(window.SentinelSDK && typeof window.SentinelSDK.token === 'function')",
            timeout=15_000,
        )

    def _prepare_page(self) -> None:
        configured = _configured_client_metadata()
        try:
            response = self._page.goto(
                f"{_CHATGPT_ORIGIN}/",
                wait_until="domcontentloaded",
                timeout=_DEFAULT_NAVIGATION_TIMEOUT_MS,
            )
            status = int(getattr(response, "status", 0) or 0)
            if status >= 400:
                raise RuntimeError(f"ChatGPT origin returned HTTP {status}")
            current = urlsplit(str(getattr(self._page, "url", "") or ""))
            if current.scheme.lower() != "https" or current.hostname != "chatgpt.com":
                raise RuntimeError("ChatGPT origin redirected to an unexpected page")
            page_values = self._page_evaluate(
                """() => ({
                    build: String(document.documentElement?.dataset?.build || ''),
                    sequence: String(document.documentElement?.dataset?.seq || ''),
                })"""
            )
            self._client_metadata, self._client_metadata_source = (
                _origin_client_metadata(
                    configured,
                    page_values if isinstance(page_values, Mapping) else {},
                )
            )
            sdk_ready = bool(
                self._page_evaluate(
                    "() => Boolean(window.SentinelSDK && typeof window.SentinelSDK.token === 'function')"
                )
            )
            if not sdk_ready:
                self._page.add_script_tag(url=_SENTINEL_LOADER_URL)
            self._page.wait_for_function(
                "() => Boolean(window.SentinelSDK && typeof window.SentinelSDK.token === 'function')",
                timeout=15_000,
            )
            self._page_source = "origin"
            return
        except Exception as exc:
            self.logger(
                "[control] checkout_browser_origin=fallback "
                f"reason={_safe_text(exc, 180)}"
            )
        self._load_probe_page()
        self._client_metadata = configured
        self._client_metadata_source = "configured_fallback"
        self._page_source = "probe_fallback"

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
            self._session_cm = shared_browser_registration_session(
                headless=self.headless,
                proxy=normalized_proxy or None,
                browser_fingerprint=self._deep_browser_profile(),
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
            self._prepare_page()
            self.checkpoint()
            self.logger(
                "[control] checkout_browser_context=ready "
                f"cookies={'structured' if self.cookies_are_structured else 'legacy'} "
                f"page_source={self._page_source} "
                f"client_metadata={self._client_metadata_source}"
            )
        except Exception:
            self._close_context()
            raise

    def _headers(self, path: str) -> dict[str, str]:
        effective_profile = self._effective_profile or self.profile
        headers = {
            "Accept": "*/*",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.access_token}",
            "oai-device-id": str(effective_profile.get("device_id") or ""),
            "oai-language": str(effective_profile.get("locale") or "en-US"),
            "oai-session-id": self._oai_session_id,
            "x-oai-is-client-observation": (
                f"v1.r.p.{secrets.token_urlsafe(16).rstrip('=')}"
            ),
            "x-openai-target-path": path,
            "x-openai-target-route": path,
        }
        build_number = str(self._client_metadata.get("buildNumber") or "").strip()
        client_version = str(self._client_metadata.get("version") or "").strip()
        if build_number:
            headers["oai-client-build-number"] = build_number
        if client_version:
            headers["oai-client-version"] = client_version
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
        require_sentinel = path == "/backend-api/payments/checkout"
        effective_referrer = str(referer or "").strip()
        if not effective_referrer and require_sentinel:
            promotion = body.get("promo_campaign")
            promotion = promotion if isinstance(promotion, Mapping) else {}
            campaign = str(promotion.get("promo_campaign_id") or "").strip()
            if campaign:
                effective_referrer = f"{_CHATGPT_ORIGIN}/?promo_campaign={campaign}"
        try:
            result = self._page_evaluate(
                _FETCH_SCRIPT,
                {
                    "path": path,
                    "body": body,
                    "headers": self._headers(path),
                    "referrer": effective_referrer or f"{_CHATGPT_ORIGIN}/",
                    "requireSentinel": require_sentinel,
                    "clientMetadata": dict(self._client_metadata),
                    "requestTimeoutMs": _DEFAULT_REQUEST_TIMEOUT_MS,
                    "attemptTimeoutMs": _checkout_attempt_timeout_ms(),
                },
            )
        except Exception as exc:
            raise PaymentEligibilityProbeError(
                f"{stage} 网络失败: {_safe_text(exc)}"
            ) from exc
        self.checkpoint()
        if not isinstance(result, dict):
            raise PaymentEligibilityProtocolError(f"{stage} 浏览器返回格式无效")
        sentinel_error = result.get("sentinel_error")
        if isinstance(sentinel_error, Mapping):
            from services.chatgpt_core.payment_eligibility import (
                PaymentEligibilitySentinelError,
            )

            diagnostics = sentinel_error.get("diagnostics")
            diagnostics = (
                dict(diagnostics) if isinstance(diagnostics, Mapping) else {}
            )
            diagnostics.update(
                {
                    "page_source": self._page_source,
                    "client_metadata_source": self._client_metadata_source,
                }
            )
            code = (
                _safe_text(sentinel_error.get("code"), 100)
                or "sentinel_prepare_failed"
            )
            self.logger(
                "[control] checkout_browser_sentinel=failed "
                f"code={code} "
                f"json={'yes' if diagnostics.get('token_json_parsed') else 'no'} "
                f"t_length={int(diagnostics.get('t_length') or 0)} "
                f"warmup_statuses={list(diagnostics.get('warmup_statuses') or [])[:8]} "
                f"attempt_ms={int(diagnostics.get('attempt_duration_ms') or 0)}"
            )
            raise PaymentEligibilitySentinelError(
                code,
                _safe_text(sentinel_error.get("message")),
                diagnostics=diagnostics,
            )
        transport_error = _safe_text(result.get("transport_error"))
        if transport_error:
            if (
                "sentinel sdk returned no browser enforcement token"
                in transport_error.lower()
            ):
                from services.chatgpt_core.payment_eligibility import (
                    PaymentEligibilitySentinelError,
                )

                raise PaymentEligibilitySentinelError(
                    "sentinel_token_incomplete",
                    transport_error,
                    diagnostics={
                        "page_source": self._page_source,
                        "client_metadata_source": self._client_metadata_source,
                    },
                )
            raise PaymentEligibilityProbeError(
                f"{stage} 网络失败: {transport_error}"
            )
        try:
            status = int(result.get("status") or 0)
        except (TypeError, ValueError):
            status = 0
        payload = result.get("payload")
        text = result.get("text")
        if require_sentinel:
            sentinel_meta = result.get("sentinel_meta")
            sentinel_meta = sentinel_meta if isinstance(sentinel_meta, Mapping) else {}
            warmup_statuses = result.get("warmup_statuses")
            warmup_statuses = (
                list(warmup_statuses)[:8]
                if isinstance(warmup_statuses, list)
                else []
            )
            self.logger(
                "[control] checkout_browser_sentinel=ready "
                f"t_length={int(sentinel_meta.get('t_length') or 0)} "
                f"telemetry_length={len(str(result.get('telemetry') or ''))} "
                f"warmup_statuses={warmup_statuses} "
                f"attempt_ms={int(result.get('attempt_duration_ms') or 0)}"
            )
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
        """Read Stripe state over HTTP while retaining the browser's route."""

        from services.chatgpt_core.checkout_probe import (
            DEFAULT_STRIPE_PK,
            KNOWN_PUBLISHABLE_KEYS,
            STRIPE_API,
            _extract_payment_method_types,
        )
        from services.chatgpt_core.payment import STRIPE_VERSION_FULL
        from services.chatgpt_core.payment_eligibility import (
            PaymentEligibilityHttpError,
            PaymentEligibilityProbeError,
            PaymentEligibilityProtocolError,
        )

        self.checkpoint()
        if self._page is None:
            raise PaymentEligibilityProbeError("Stripe 金额读取前浏览器 Context 未就绪")
        configured = str(
            checkout_profile.get("publishable_key")
            or self.extra.get("stripe_publishable_key")
            or DEFAULT_STRIPE_PK
        ).strip()
        keys: list[str] = []
        for candidate in (configured, *KNOWN_PUBLISHABLE_KEYS):
            key = str(candidate or "").strip()
            if key and key not in keys:
                keys.append(key)
        url = f"{STRIPE_API}/v1/payment_pages/{str(session_id).strip()}/init"
        effective_profile = self._effective_profile or self.profile
        browser_locale = str(
            checkout_profile.get("locale")
            or effective_profile.get("locale")
            or "en-US"
        ).strip()
        browser_timezone = str(
            checkout_profile.get("timezone")
            or effective_profile.get("timezone")
            or "UTC"
        ).strip()
        elements_locale = str(
            checkout_profile.get("stripe_locale")
            or effective_profile.get("stripe_locale")
            or browser_locale.split("-", 1)[0]
            or "en"
        ).strip()
        last_status = 0
        last_detail = ""
        payload: dict[str, Any] | None = None
        selected_key = ""
        stripe = _new_stripe_http_session(effective_profile, self._proxy)
        try:
            for key in keys:
                self.checkpoint()
                body = {
                    "browser_locale": browser_locale,
                    "browser_timezone": browser_timezone,
                    "elements_session_client[client_betas][0]": (
                        "custom_checkout_server_updates_1"
                    ),
                    "elements_session_client[client_betas][1]": (
                        "custom_checkout_manual_approval_1"
                    ),
                    "elements_session_client[elements_init_source]": (
                        "custom_checkout"
                    ),
                    "elements_session_client[referrer_host]": "chatgpt.com",
                    "elements_session_client[stripe_js_id]": str(uuid.uuid4()),
                    "elements_session_client[locale]": elements_locale,
                    "elements_session_client[is_aggregation_expected]": "false",
                    "elements_options_client[saved_payment_method][enable_save]": (
                        "never"
                    ),
                    "elements_options_client[saved_payment_method][enable_redisplay]": (
                        "never"
                    ),
                    "key": key,
                    "_stripe_version": STRIPE_VERSION_FULL,
                }
                try:
                    response = stripe.post(url, data=body, timeout=30)
                except Exception as exc:
                    raise PaymentEligibilityProbeError(
                        f"Stripe payment_pages init 网络失败: {_safe_text(exc)}"
                    ) from exc
                self.checkpoint()
                try:
                    status = int(getattr(response, "status_code", 0) or 0)
                except (TypeError, ValueError):
                    status = 0
                text = str(getattr(response, "text", "") or "")
                try:
                    candidate = response.json() or {}
                except Exception:
                    candidate = None
                last_status = status
                last_detail = _response_detail(candidate, text)
                if status < 400:
                    if not isinstance(candidate, dict):
                        raise PaymentEligibilityProtocolError(
                            "Stripe payment_pages init 返回不是 JSON"
                        )
                    payload = candidate
                    selected_key = key
                    break
        finally:
            try:
                stripe.close()
            except Exception:
                pass
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
