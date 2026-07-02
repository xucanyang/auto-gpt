"""Client helpers for oaipay.12001234.xyz approval URL extraction."""

from __future__ import annotations

import json
import random
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterator

from services.chatgpt_core.paypal_binding_client import (
    browser_profile_summary,
    mask_proxy,
    mask_secret,
    parse_sse_payload,
    random_browser_header_profile,
)


DEFAULT_BASE_URL = "https://oaipay.12001234.xyz"
DEFAULT_PROXY_POOL = "kookeey"


class OaiPayClientError(RuntimeError):
    """Raised when the oaipay upstream cannot be reached or returns an invalid result."""


def sanitize_oaipay_result(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {}
    sanitized: dict[str, Any] = {}
    for key, value in result.items():
        key_text = str(key)
        lower = key_text.lower()
        if lower in {"access_token", "accesstoken", "refresh_token", "token", "stripe_publishable_key"}:
            sanitized[key_text] = mask_secret(value)
        elif lower in {"provider_redirect_url", "long_url", "stripe_redirect_url", "stripe_hosted_url"}:
            sanitized[key_text] = str(value or "")
        elif lower == "payment_method_id":
            sanitized[key_text] = mask_secret(value, keep_start=8, keep_end=4)
        else:
            sanitized[key_text] = value
    return sanitized


def extract_approval_url(result: Any) -> str:
    if not isinstance(result, dict):
        return ""
    for key in ("approval_url", "approvalUrl", "provider_redirect_url", "long_url"):
        value = str(result.get(key) or "").strip()
        if value:
            return value
    return ""


def is_paypal_approval_url(value: Any) -> bool:
    text = str(value or "").strip()
    if not text or not re.match(r"^https?://", text, re.I):
        return False
    try:
        parsed = urllib.parse.urlsplit(text)
    except Exception:
        return False
    host = (parsed.hostname or "").lower()
    path = parsed.path.lower()
    query = urllib.parse.parse_qs(parsed.query)
    return (host == "paypal.com" or host.endswith(".paypal.com")) and (
        "ba_token" in query or "/agreements/approve" in path
    )


def _parse_json(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text}


def _common_headers(
    url: str,
    accept: str,
    *,
    method: str = "GET",
    browser_profile: dict[str, str] | None = None,
    event_stream: bool = False,
    auth_token: str = "",
) -> dict[str, str]:
    parsed_url = urllib.parse.urlsplit(url)
    origin = f"{parsed_url.scheme}://{parsed_url.netloc}"
    profile = browser_profile if isinstance(browser_profile, dict) else random_browser_header_profile()
    headers = {
        "Accept": accept,
        "Accept-Language": str(profile.get("accept_language") or "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7"),
        "Referer": f"{origin}/",
        "User-Agent": str(profile.get("user_agent") or ""),
        "sec-ch-ua": str(profile.get("sec_ch_ua") or ""),
        "sec-ch-ua-mobile": str(profile.get("sec_ch_ua_mobile") or "?0"),
        "sec-ch-ua-platform": str(profile.get("sec_ch_ua_platform") or '"Windows"'),
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        # The captured site sends this header even when auth is disabled.
        "Authorization": "Bearer " + str(auth_token or ""),
    }
    if str(method or "").upper() in {"POST", "PUT", "PATCH", "DELETE"}:
        headers["Origin"] = origin
    if event_stream:
        headers["Cache-Control"] = "no-cache"
        headers["Pragma"] = "no-cache"
    return headers


class OaiPayClient:
    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30,
        event_timeout: float = 90,
        browser_profile: dict[str, str] | None = None,
        auth_token: str = "",
    ):
        self.base_url = str(base_url or DEFAULT_BASE_URL).strip().rstrip("/") or DEFAULT_BASE_URL
        self.timeout = float(timeout or 30)
        self.event_timeout = float(event_timeout or 90)
        self.browser_profile = dict(browser_profile) if isinstance(browser_profile, dict) else random_browser_header_profile()
        self.auth_token = str(auth_token or "")

    @property
    def browser_profile_label(self) -> str:
        return browser_profile_summary(self.browser_profile)

    def _post_json(self, path: str, payload: dict[str, Any], *, timeout: float | None = None) -> tuple[int, str, dict[str, str]]:
        url = f"{self.base_url}{path}"
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = _common_headers(
            url,
            "*/*",
            method="POST",
            browser_profile=self.browser_profile,
            auth_token=self.auth_token,
        )
        headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=float(timeout or self.timeout)) as response:
                text = response.read().decode("utf-8", errors="replace")
                return int(response.status or 0), text, dict(response.headers.items())
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", errors="replace")
            return int(exc.code or 0), text, dict(exc.headers.items())
        except urllib.error.URLError as exc:
            raise OaiPayClientError(f"Request failed: {exc}") from exc

    def token_info(
        self,
        *,
        access_token: str,
        proxy: str = "",
        proxy_pool: str = DEFAULT_PROXY_POOL,
        device_id: str = "",
        user_agent: str = "",
    ) -> dict[str, Any]:
        payload = {
            "accessToken": str(access_token or "").strip(),
            "proxy": str(proxy or "").strip(),
            "proxyPool": str(proxy_pool or "").strip(),
            "device_id": str(device_id or "").strip(),
            "user_agent": str(user_agent or "").strip(),
        }
        if not payload["accessToken"]:
            raise OaiPayClientError("缺少 Access Token")
        status, text, _ = self._post_json("/api/token-info", payload)
        parsed = _parse_json(text)
        if status < 200 or status >= 300:
            detail = parsed.get("detail") if isinstance(parsed, dict) else ""
            raise OaiPayClientError(str(detail or f"HTTP {status}: {text}"))
        if not isinstance(parsed, dict):
            raise OaiPayClientError(f"token-info 返回不是 JSON 对象: {text[:200]}")
        return parsed

    def iter_long_link_events(
        self,
        *,
        access_token: str,
        proxy: str,
        proxy_pool: str = DEFAULT_PROXY_POOL,
        billing_country: str = "US",
        checkout_ui_mode: str = "hosted",
        payment_locale: str = "en",
        stripe_publishable_key: str = "",
        payment_email: str = "",
        device_id: str = "",
        user_agent: str = "",
        approval_url: str = "",
        pp_phone_number: str = "",
        pp_otp: str = "",
        captcha_token: str = "",
        mode: int | str = 2,
        link_type: str = "paypal",
    ) -> Iterator[dict[str, Any]]:
        payload = {
            "accessToken": str(access_token or "").strip(),
            "link_type": str(link_type or "paypal").strip() or "paypal",
            "proxy": str(proxy or "").strip(),
            "proxyPool": str(proxy_pool or "").strip(),
            "billing_country": str(billing_country or "US").strip() or "US",
            "checkout_ui_mode": str(checkout_ui_mode or "hosted").strip() or "hosted",
            "payment_locale": str(payment_locale or "en").strip() or "en",
            "stripe_publishable_key": str(stripe_publishable_key or "").strip(),
            "payment_email": str(payment_email or "").strip(),
            "device_id": str(device_id or "").strip(),
            "user_agent": str(user_agent or "").strip(),
            "approvalUrl": str(approval_url or "").strip(),
            "ppPhoneNumber": str(pp_phone_number or "").strip(),
            "ppOtp": str(pp_otp or "").strip(),
            "captchaToken": str(captcha_token or "").strip(),
            "mode": int(mode or 2),
        }
        if not payload["accessToken"]:
            raise OaiPayClientError("缺少 Access Token")
        if not payload["proxy"]:
            raise OaiPayClientError("缺少上游提交代理")

        url = f"{self.base_url}/api/long-link-stream"
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = _common_headers(
            url,
            "*/*",
            method="POST",
            browser_profile=self.browser_profile,
            event_stream=True,
            auth_token=self.auth_token,
        )
        headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.event_timeout) as response:
                if int(response.status or 0) < 200 or int(response.status or 0) >= 300:
                    text = response.read().decode("utf-8", errors="replace")
                    raise OaiPayClientError(f"HTTP {response.status} {url}: {text}")
                data_lines: list[str] = []
                event_name: str | None = None
                while True:
                    raw_line = response.readline()
                    if raw_line == b"":
                        payload_event = parse_sse_payload(data_lines, event_name)
                        if payload_event:
                            yield payload_event
                        return
                    line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                    if not line:
                        payload_event = parse_sse_payload(data_lines, event_name)
                        data_lines = []
                        event_name = None
                        if payload_event:
                            yield payload_event
                        continue
                    if line.startswith(":"):
                        continue
                    if line.startswith("event:"):
                        event_name = line[6:].strip()
                        continue
                    if line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", errors="replace")
            raise OaiPayClientError(f"HTTP {exc.code} {url}: {text}") from exc
        except urllib.error.URLError as exc:
            raise OaiPayClientError(f"Event stream failed: {exc}") from exc

    def create_paypal_approval_url(self, **kwargs: Any) -> dict[str, Any]:
        final_result: dict[str, Any] | None = None
        final_error = ""
        events: list[dict[str, Any]] = []
        for event in self.iter_long_link_events(**kwargs):
            events.append(event)
            event_type = str(event.get("type") or event.get("event") or "").strip()
            if event_type == "error":
                final_error = str(event.get("detail") or event.get("error") or event.get("message") or "oaipay 上游返回失败")
                break
            if event_type == "done":
                result = event.get("result") if isinstance(event.get("result"), dict) else {}
                final_result = dict(result)
                break
        if final_error:
            raise OaiPayClientError(final_error)
        if final_result is None:
            raise OaiPayClientError("上游事件流结束但未返回 done.result")
        approval_url = extract_approval_url(final_result)
        if not approval_url:
            raise OaiPayClientError("上游成功返回但缺少 approvalUrl/long_url")
        if not is_paypal_approval_url(approval_url):
            raise OaiPayClientError("上游返回的链接不是 PayPal approval URL")
        return {
            "approval_url": approval_url,
            "result": final_result,
            "events": events,
        }
