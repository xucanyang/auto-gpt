"""Client helpers for the external plus.iceaix.com PayPal binding job API."""

from __future__ import annotations

import json
import random
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterator

from core.safe_http import open_http_url


DEFAULT_BASE_URL = "https://plus.iceaix.com"
DEFAULT_OTP_SIGNAL = "otp_needed"


class PlusIceaixClientError(RuntimeError):
    """Raised when the external PayPal binding service cannot be reached or parsed."""


def _normalize_http_base_url(value: Any) -> str:
    raw = str(value or DEFAULT_BASE_URL).strip().rstrip("/") or DEFAULT_BASE_URL
    try:
        parsed = urllib.parse.urlsplit(raw)
        hostname = parsed.hostname
        username = parsed.username
        password = parsed.password
    except ValueError as exc:
        raise PlusIceaixClientError("PayPal 绑定服务 base URL 格式无效") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not hostname
        or username is not None
        or password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise PlusIceaixClientError("PayPal 绑定服务 base URL 必须是 HTTP(S) 地址且不得包含凭据或片段")
    return raw


def normalize_phone(value: Any) -> str:
    """Normalize the phone shape expected by the plus.iceaix.com submit script.

    The reference script accepts Japanese phone numbers as local digits, e.g.
    ``8083291906``.  It also tolerates ``+81`` and leading ``0`` variants.
    """

    if not value:
        return ""
    digits = re.sub(r"\D", "", str(value))
    if digits.startswith("81") and len(digits) == 12:
        digits = digits[2:]
    if digits.startswith("0") and len(digits) == 11:
        digits = digits[1:]
    return digits


def mask_secret(value: Any, *, keep_start: int = 6, keep_end: int = 4) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= keep_start + keep_end + 3:
        return "***"
    return f"{text[:keep_start]}...{text[-keep_end:]}"


def mask_proxy(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = urllib.parse.urlsplit(text)
    except Exception:
        return mask_secret(text)
    if not parsed.scheme or not parsed.netloc:
        return mask_secret(text)
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    user = parsed.username or ""
    auth = f"{user}:***@" if user else ""
    return urllib.parse.urlunsplit((parsed.scheme, f"{auth}{host}{port}", parsed.path or "", "", ""))


def sanitize_result(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {}
    sanitized: dict[str, Any] = {}
    for key, value in result.items():
        key_text = str(key)
        lower = key_text.lower()
        if lower in {"password", "pass", "pwd"}:
            sanitized[key_text] = mask_secret(value)
        elif lower in {"ba_token", "ec_token", "token", "access_token", "refresh_token"}:
            sanitized[key_text] = mask_secret(value)
        else:
            sanitized[key_text] = value
    return sanitized


def _parse_json(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text}


_BROWSER_HEADER_PROFILES: tuple[dict[str, str], ...] = (
    {
        "label": "Chrome Windows",
        "browser": "Chrome",
        "platform": "Windows",
        "major": "149",
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
        "sec_ch_ua": '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": '"Windows"',
        "accept_language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    },
    {
        "label": "Edge Windows",
        "browser": "Edge",
        "platform": "Windows",
        "major": "149",
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36 Edg/149.0.0.0",
        "sec_ch_ua": '"Microsoft Edge";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": '"Windows"',
        "accept_language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    },
    {
        "label": "Chrome macOS",
        "browser": "Chrome",
        "platform": "macOS",
        "major": "149",
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
        "sec_ch_ua": '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": '"macOS"',
        "accept_language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    },
    {
        "label": "Edge macOS",
        "browser": "Edge",
        "platform": "macOS",
        "major": "149",
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36 Edg/149.0.0.0",
        "sec_ch_ua": '"Microsoft Edge";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": '"macOS"',
        "accept_language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    },
    {
        "label": "Chrome Linux",
        "browser": "Chrome",
        "platform": "Linux",
        "major": "149",
        "user_agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
        "sec_ch_ua": '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": '"Linux"',
        "accept_language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    },
)


def random_browser_header_profile() -> dict[str, str]:
    """Return one coherent desktop Chrome/Edge browser header profile."""

    return dict(random.choice(_BROWSER_HEADER_PROFILES))


def browser_profile_summary(profile: dict[str, str] | None) -> str:
    if not isinstance(profile, dict):
        return "-"
    label = str(profile.get("label") or "").strip()
    major = str(profile.get("major") or "").strip()
    if label and major:
        return f"{label}/{major}"
    return label or str(profile.get("user_agent") or "")[:64] or "-"


def _common_headers(
    url: str,
    accept: str,
    *,
    method: str = "GET",
    browser_profile: dict[str, str] | None = None,
    event_stream: bool = False,
) -> dict[str, str]:
    parsed_url = urllib.parse.urlsplit(url)
    origin = f"{parsed_url.scheme}://{parsed_url.netloc}"
    profile = browser_profile if isinstance(browser_profile, dict) else random_browser_header_profile()
    headers = {
        "Accept": accept,
        "Accept-Language": str(profile.get("accept_language") or "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7"),
        "Referer": f"{origin}/",
        "User-Agent": str(profile.get("user_agent") or _BROWSER_HEADER_PROFILES[0]["user_agent"]),
        "sec-ch-ua": str(profile.get("sec_ch_ua") or _BROWSER_HEADER_PROFILES[0]["sec_ch_ua"]),
        "sec-ch-ua-mobile": str(profile.get("sec_ch_ua_mobile") or "?0"),
        "sec-ch-ua-platform": str(profile.get("sec_ch_ua_platform") or '"Windows"'),
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }
    if str(method or "").upper() in {"POST", "PUT", "PATCH", "DELETE"}:
        headers["Origin"] = origin
    if event_stream:
        headers["Cache-Control"] = "no-cache"
        headers["Pragma"] = "no-cache"
    return headers


def parse_sse_payload(data_lines: list[str], event_name: str | None = None) -> dict[str, Any] | None:
    if not data_lines:
        return None

    data = "\n".join(data_lines)
    parsed = _parse_json(data)
    if isinstance(parsed, dict):
        payload = dict(parsed)
        if event_name and "event" not in payload:
            payload["event"] = event_name
        if event_name and "type" not in payload:
            payload["type"] = event_name
        return payload
    return {"type": event_name or "message", "data": parsed}


class PlusIceaixClient:
    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30,
        event_timeout: float = 60,
        browser_profile: dict[str, str] | None = None,
    ):
        self.base_url = _normalize_http_base_url(base_url)
        self.timeout = float(timeout or 30)
        self.event_timeout = float(event_timeout or 60)
        self.browser_profile = dict(browser_profile) if isinstance(browser_profile, dict) else random_browser_header_profile()

    @property
    def browser_profile_label(self) -> str:
        return browser_profile_summary(self.browser_profile)

    @property
    def user_agent(self) -> str:
        return str(self.browser_profile.get("user_agent") or "")

    def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = _common_headers(url, "*/*", method="POST", browser_profile=self.browser_profile)
        headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with open_http_url(
                request,
                timeout=self.timeout,
            ) as response:
                text = response.read().decode("utf-8", errors="replace")
                parsed = _parse_json(text)
                return parsed if isinstance(parsed, dict) else {"data": parsed}
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", errors="replace")
            raise PlusIceaixClientError(f"HTTP {exc.code} {url}: {text}") from exc
        except urllib.error.URLError as exc:
            raise PlusIceaixClientError(f"Request failed: {exc}") from exc

    def create_job(
        self,
        *,
        input_token: str,
        proxy: str,
        phone: str,
        proxy_jp: str = "",
        email: str = "",
        sms_api: str = "",
        otp_timeout: int | str = 180,
        pplink_retry: int | str = 3,
    ) -> dict[str, Any]:
        payload = {
            "input": str(input_token or "").strip(),
            "proxy": str(proxy or "").strip(),
            "proxy_jp": str(proxy_jp or "").strip(),
            "phone": normalize_phone(phone),
            "otp": "",
            "sms_api": str(sms_api or "").strip(),
            "otp_timeout": str(otp_timeout or 180),
            "pplink_retry": str(pplink_retry or 3),
            "email": str(email or "").strip(),
        }
        if not payload["input"]:
            raise PlusIceaixClientError("缺少 Access Token")
        if not payload["proxy"]:
            raise PlusIceaixClientError("缺少外部绑定代理")
        if not payload["phone"]:
            raise PlusIceaixClientError("缺少 PayPal 手机号")
        return self._post_json("/api/jobs", payload)

    def submit_otp(self, job_id: str, pin: str) -> dict[str, Any]:
        job_id_value = str(job_id or "").strip()
        pin_value = str(pin or "").strip()
        if not job_id_value:
            raise PlusIceaixClientError("job_id 为空")
        if not pin_value:
            raise PlusIceaixClientError("OTP 为空")
        return self._post_json(f"/api/jobs/{urllib.parse.quote(job_id_value, safe='')}/otp", {"pin": pin_value})

    def events_url(self, job_id: str) -> str:
        return f"{self.base_url}/api/jobs/{urllib.parse.quote(str(job_id or '').strip(), safe='')}/events"

    def iter_events(self, job_id: str) -> Iterator[dict[str, Any]]:
        url = self.events_url(job_id)
        request = urllib.request.Request(
            url,
            headers=_common_headers(
                url,
                "text/event-stream",
                method="GET",
                browser_profile=self.browser_profile,
                event_stream=True,
            ),
            method="GET",
        )
        try:
            with open_http_url(
                request,
                timeout=self.event_timeout,
            ) as response:
                data_lines: list[str] = []
                event_name: str | None = None
                while True:
                    raw_line = response.readline()
                    if raw_line == b"":
                        payload = parse_sse_payload(data_lines, event_name)
                        if payload:
                            yield payload
                        return

                    line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                    if not line:
                        payload = parse_sse_payload(data_lines, event_name)
                        data_lines = []
                        event_name = None
                        if payload:
                            yield payload
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
            raise PlusIceaixClientError(f"HTTP {exc.code} {url}: {text}") from exc
        except urllib.error.URLError as exc:
            raise PlusIceaixClientError(f"Event stream failed: {exc}") from exc
