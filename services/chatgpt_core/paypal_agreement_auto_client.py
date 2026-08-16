"""Authenticated client for the local PayPal agreement payment queue."""

from __future__ import annotations

import os
import re
import urllib.parse
from typing import Any, Iterable

import requests


DEFAULT_BASE_URL = "http://172.20.0.1:18098"
DEFAULT_TIMEOUT_SECONDS = 10.0
PROFILE_PATH = "/api/internal/auto-payments/profile"
ENQUEUE_PATH = "/api/internal/auto-payments"

_BA_TOKEN_RE = re.compile(r"\bBA-[A-Za-z0-9]{8,80}\b")
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_HTTP_URL_RE = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class PaypalAgreementAutoError(RuntimeError):
    """Raised when the internal automatic-payment contract cannot be used."""


def sanitize_paypal_agreement_error(
    value: Any,
    *,
    secrets: Iterable[str] = (),
) -> str:
    """Remove credentials, approval links and BA Tokens from an error string."""

    text = str(value or "").strip()
    for secret in secrets:
        normalized = str(secret or "")
        if normalized:
            text = text.replace(normalized, "[REDACTED_TOKEN]")
    text = _BEARER_RE.sub("Bearer [REDACTED_TOKEN]", text)
    text = _HTTP_URL_RE.sub("[REDACTED_URL]", text)
    text = _BA_TOKEN_RE.sub("[REDACTED_BA_TOKEN]", text)
    return text[:500]


def normalize_paypal_agreement_base_url(value: Any) -> str:
    raw = str(value or "").strip().rstrip("/")
    if not raw:
        raise PaypalAgreementAutoError("未配置 PayPal 自动支付内部服务地址")
    if len(raw) > 2048 or any(ord(character) < 32 or ord(character) == 127 for character in raw):
        raise PaypalAgreementAutoError("PayPal 自动支付内部服务地址格式无效")
    try:
        parsed = urllib.parse.urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise PaypalAgreementAutoError("PayPal 自动支付内部服务地址格式无效") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise PaypalAgreementAutoError(
            "PayPal 自动支付内部服务地址必须是无凭据、无查询参数的 HTTP(S) URL"
        )
    hostname = parsed.hostname
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    netloc = hostname if port is None else f"{hostname}:{port}"
    return urllib.parse.urlunsplit(
        (parsed.scheme.lower(), netloc, parsed.path.rstrip("/"), "", "")
    )


def normalize_paypal_approval_url(value: Any) -> str:
    """Validate the production/sandbox PayPal agreement approval URL contract."""

    raw = str(value or "").strip()
    if (
        not raw
        or len(raw) > 10_000
        or any(ord(character) < 32 or ord(character) == 127 for character in raw)
    ):
        raise PaypalAgreementAutoError("提链结果不是有效的 PayPal approval URL")
    try:
        parsed = urllib.parse.urlsplit(raw)
        port = parsed.port
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    except ValueError as exc:
        raise PaypalAgreementAutoError("提链结果不是有效的 PayPal approval URL") from exc

    hostname = str(parsed.hostname or "").lower().rstrip(".")
    path = parsed.path.rstrip("/")
    tokens = query.get("ba_token") or []
    token = str(tokens[0] or "") if len(tokens) == 1 else ""
    if (
        parsed.scheme.lower() != "https"
        or not hostname
        or not (hostname == "paypal.com" or hostname.endswith(".paypal.com"))
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or path != "/agreements/approve"
        or parsed.fragment
        or not _BA_TOKEN_RE.fullmatch(token)
        or _BA_TOKEN_RE.findall(raw) != [token]
    ):
        raise PaypalAgreementAutoError("提链结果不是有效的 PayPal approval URL")

    netloc = hostname if port is None else f"{hostname}:{port}"
    return urllib.parse.urlunsplit(("https", netloc, parsed.path, parsed.query, ""))


def _timeout_from_env() -> float:
    try:
        value = float(
            str(
                os.getenv("PAYPAL_AGREEMENT_INTERNAL_TIMEOUT_SECONDS")
                or DEFAULT_TIMEOUT_SECONDS
            ).strip()
        )
    except (TypeError, ValueError):
        value = DEFAULT_TIMEOUT_SECONDS
    return max(1.0, min(value, 60.0))


def _safe_identifier(value: Any, label: str) -> str:
    normalized = str(value or "").strip()
    if not _SAFE_ID_RE.fullmatch(normalized):
        raise PaypalAgreementAutoError(f"PayPal 自动支付服务未返回有效{label}")
    return normalized


def _nonnegative_int(value: Any, label: str) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError) as exc:
        raise PaypalAgreementAutoError(f"PayPal 自动支付服务返回的{label}格式无效") from exc
    return max(parsed, 0)


class PaypalAgreementAutoClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_token: str,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = normalize_paypal_agreement_base_url(base_url)
        self.api_token = str(api_token or "").strip()
        self.timeout_seconds = max(1.0, min(float(timeout_seconds), 60.0))
        self.session = session or requests.Session()
        if (
            len(self.api_token) < 32
            or len(self.api_token) > 1024
            or any(ord(character) < 32 or ord(character) == 127 for character in self.api_token)
        ):
            raise PaypalAgreementAutoError("PayPal 自动支付内部 Token 未配置或格式无效")

    @classmethod
    def from_env(cls) -> "PaypalAgreementAutoClient":
        return cls(
            base_url=str(
                os.getenv("PAYPAL_AGREEMENT_INTERNAL_BASE_URL") or DEFAULT_BASE_URL
            ).strip(),
            api_token=str(os.getenv("PAYPAL_AGREEMENT_INTERNAL_API_TOKEN") or "").strip(),
            timeout_seconds=_timeout_from_env(),
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        secrets: Iterable[str] = (),
    ) -> dict[str, Any]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Internal-Auto-Channel": "1",
            "Authorization": f"Bearer {self.api_token}",
        }
        try:
            response = self.session.request(
                method,
                f"{self.base_url}{path}",
                headers=headers,
                json=payload,
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            detail = sanitize_paypal_agreement_error(
                exc,
                secrets=(*secrets, self.api_token),
            )
            raise PaypalAgreementAutoError(
                f"PayPal 自动支付内部服务不可用: {detail or type(exc).__name__}"
            ) from exc

        status_code = int(response.status_code or 0)
        try:
            data = response.json()
        except ValueError as exc:
            raise PaypalAgreementAutoError(
                f"PayPal 自动支付内部服务返回无效 JSON (HTTP {status_code})"
            ) from exc
        if not isinstance(data, dict):
            raise PaypalAgreementAutoError(
                f"PayPal 自动支付内部服务返回格式错误 (HTTP {status_code})"
            )
        if not 200 <= status_code < 300:
            detail = data.get("detail") or data.get("error") or data.get("message") or "上游请求失败"
            safe_detail = sanitize_paypal_agreement_error(
                detail,
                secrets=(*secrets, self.api_token),
            )
            raise PaypalAgreementAutoError(
                f"PayPal 自动支付内部服务请求失败 (HTTP {status_code}): {safe_detail}"
            )
        if data.get("ok") is not True:
            detail = sanitize_paypal_agreement_error(
                data.get("error") or data.get("message") or "服务未确认请求成功",
                secrets=(*secrets, self.api_token),
            )
            raise PaypalAgreementAutoError(f"PayPal 自动支付内部服务拒绝请求: {detail}")
        return data

    def get_profile(self) -> dict[str, Any]:
        data = self._request("GET", PROFILE_PATH)
        queue = data.get("queue") if isinstance(data.get("queue"), dict) else {}
        status_counts = (
            queue.get("status_counts")
            if isinstance(queue.get("status_counts"), dict)
            else {}
        )
        return {
            "configured": data.get("configured") is True,
            "ready": data.get("ready") is True,
            "blocking_reason": sanitize_paypal_agreement_error(
                data.get("blocking_reason") or ""
            ),
            "country": str(data.get("country") or "").strip().upper(),
            "proxy_country": str(data.get("proxy_country") or "").strip().upper(),
            "buyer_mode": str(data.get("buyer_mode") or "").strip().lower(),
            "browser_profile": str(data.get("browser_profile") or "").strip().lower(),
            "matching_phone_count": _nonnegative_int(
                data.get("matching_phone_count"),
                "手机号数量",
            ),
            "queue": {
                "batch_count": _nonnegative_int(queue.get("batch_count"), "批次数量"),
                "pending_items": _nonnegative_int(
                    queue.get("pending_items"),
                    "待处理条目数量",
                ),
                "status_counts": {
                    str(key)[:64]: _nonnegative_int(value, "状态数量")
                    for key, value in status_counts.items()
                    if str(key or "").strip()
                },
            },
        }

    def enqueue(self, paypal_url: Any) -> dict[str, Any]:
        approval_url = normalize_paypal_approval_url(paypal_url)
        data = self._request(
            "POST",
            ENQUEUE_PATH,
            payload={"paypal_url": approval_url},
            secrets=(approval_url,),
        )
        batch = data.get("batch") if isinstance(data.get("batch"), dict) else {}
        item = data.get("item") if isinstance(data.get("item"), dict) else {}
        return {
            "batch_id": _safe_identifier(batch.get("id"), "批次 ID"),
            "item_id": _safe_identifier(item.get("id"), "条目 ID"),
            "created": data.get("created") is True,
            "idempotent": data.get("idempotent") is True,
            "state": str(data.get("state") or "accepted").strip().lower()[:64],
            "batch_status": str(batch.get("status") or "").strip().lower()[:64],
            "remote_status": str(item.get("status") or "pending").strip().lower()[:64],
        }
