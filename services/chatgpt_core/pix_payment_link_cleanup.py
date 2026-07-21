"""Scan and atomically clean current QR payment links (PIX and UPI)."""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import datetime, time as datetime_time, timedelta, timezone
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
from typing import Any, Callable, Literal, Mapping, cast
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

import requests
from sqlalchemy import text
from sqlmodel import Session

from services.account_filters import upsert_account_list_state_for_account_ids
from services.chatgpt_core.payment_link_cache import (
    PIX_CANCELLED_CLEANED_STATUS,
    PIX_EXPIRED_CLEANED_STATUS,
    PIX_PAID_CLEANED_STATUS,
    UPI_CANCELLED_CLEANED_STATUS,
    UPI_EXPIRED_CLEANED_STATUS,
    UPI_PAID_CLEANED_STATUS,
    PAYMENT_LINK_CLEANED_STATUSES,
    PAYMENT_LINK_QR_TYPES,
    extract_payment_link_qr_expires_at,
    normalize_payment_link_expires_at,
    normalize_payment_link_type,
    payment_link_type_from_payload,
    normalize_payment_link_status,
)


PIX_PAYMENT_TIMEZONE = ZoneInfo("Asia/Shanghai")
PIX_DAILY_EXPIRY_TIME = datetime_time(hour=11)
UPI_QR_VALIDITY_SECONDS = 5 * 60
UPI_QR_EXPIRY_TOLERANCE_SECONDS = 60
PAYMENT_LINK_TYPE_PIX = "pix"
PAYMENT_LINK_TYPE_UPI = "upi"
PAYMENT_LINK_TYPES = frozenset({PAYMENT_LINK_TYPE_PIX, PAYMENT_LINK_TYPE_UPI})
_CURRENT_LINK_URL_FIELDS = (
    "url",
    "paypal_url",
    "provider_redirect_url",
    "approval_url",
    "checkout_url",
    "cashier_url",
)
_LINK_URL_FIELDS_TO_REMOVE = frozenset({
    *_CURRENT_LINK_URL_FIELDS,
    "long_url",
    "stripe_redirect_url",
    "stripe_hosted_url",
    "chatgpt_checkout_url",
})
_BACKUP_MIN_FREE_MARGIN_BYTES = 64 * 1024 * 1024
_STRIPE_PIX_HOST = "payments.stripe.com"
_STRIPE_PIX_PATH_PREFIX = "/qr/instructions/"
_STRIPE_UPI_PATH_PREFIX = "/upi/instructions/"
_STRIPE_PIX_MAX_RESPONSE_BYTES = 256 * 1024
_STRIPE_PIX_CONNECT_TIMEOUT_SECONDS = 5.0
_STRIPE_PIX_READ_TIMEOUT_SECONDS = 10.0
_STRIPE_PIX_MAX_REDIRECTS = 2
_STRIPE_PIX_DEFAULT_CONCURRENCY = 12
_STRIPE_PIX_MAX_CONCURRENCY = 32
_STRIPE_INTENT_STATE_RE = re.compile(r"^[a-z0-9_]{1,64}$")
_STRIPE_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
PIX_CLEANUP_MODE_EXPIRED = "expired"
PIX_CLEANUP_MODE_PAID = "paid"
PIX_CLEANUP_MODE_CANCELLED = "cancelled"
PIX_CLEANUP_MODES = frozenset({
    PIX_CLEANUP_MODE_EXPIRED,
    PIX_CLEANUP_MODE_PAID,
    PIX_CLEANUP_MODE_CANCELLED,
})
PixCleanupMode = Literal["expired", "paid", "cancelled"]
PIX_CLEANUP_MODE_LABELS: dict[str, str] = {
    PIX_CLEANUP_MODE_EXPIRED: "过期",
    PIX_CLEANUP_MODE_PAID: "已支付",
    PIX_CLEANUP_MODE_CANCELLED: "支付已取消",
}
_PAID_LINK_STATUSES = frozenset({"paid", "already_paid"})
_CANCELLED_LINK_STATUSES = frozenset({
    "cancelled",
    "canceled",
    "payment_cancelled",
    "payment_canceled",
})
_PAID_PAYMENT_STATUSES = frozenset({"paid", "success", "completed"})
_CANCELLED_PAYMENT_STATUSES = frozenset({"cancelled", "canceled", "payment_cancelled", "payment_canceled"})
_CLEANED_STATUS_BY_MODE = {
    PIX_CLEANUP_MODE_EXPIRED: PIX_EXPIRED_CLEANED_STATUS,
    PIX_CLEANUP_MODE_PAID: PIX_PAID_CLEANED_STATUS,
    PIX_CLEANUP_MODE_CANCELLED: PIX_CANCELLED_CLEANED_STATUS,
}
_CLEANED_REASON_BY_MODE = {
    PIX_CLEANUP_MODE_EXPIRED: "PIX payment link expired and was cleared",
    PIX_CLEANUP_MODE_PAID: "PIX payment link was paid and cleared",
    PIX_CLEANUP_MODE_CANCELLED: "PIX payment was cancelled and the link was cleared",
}

_CLEANED_STATUS_BY_TYPE_AND_MODE = {
    (PAYMENT_LINK_TYPE_PIX, PIX_CLEANUP_MODE_EXPIRED): PIX_EXPIRED_CLEANED_STATUS,
    (PAYMENT_LINK_TYPE_PIX, PIX_CLEANUP_MODE_PAID): PIX_PAID_CLEANED_STATUS,
    (PAYMENT_LINK_TYPE_PIX, PIX_CLEANUP_MODE_CANCELLED): PIX_CANCELLED_CLEANED_STATUS,
    (PAYMENT_LINK_TYPE_UPI, PIX_CLEANUP_MODE_EXPIRED): UPI_EXPIRED_CLEANED_STATUS,
    (PAYMENT_LINK_TYPE_UPI, PIX_CLEANUP_MODE_PAID): UPI_PAID_CLEANED_STATUS,
    (PAYMENT_LINK_TYPE_UPI, PIX_CLEANUP_MODE_CANCELLED): UPI_CANCELLED_CLEANED_STATUS,
}


@dataclass(frozen=True)
class PaymentLinkCandidate:
    account_id: int
    cashier_url: str
    current_url: str
    payload: dict[str, Any]
    payment_marker: dict[str, Any]
    link_status: str
    generated_at: datetime | None
    expires_at: datetime | None
    expiry_source: str
    payment_type: str = PAYMENT_LINK_TYPE_PIX

    @property
    def link_type(self) -> str:
        return self.payment_type


# Keep the old import/name stable for integrations and existing tests.
PixLinkCandidate = PaymentLinkCandidate


@dataclass(frozen=True)
class StripePaymentDirectState:
    attempted: bool
    success: bool
    intent_state: str = ""
    server_timestamp: datetime | None = None
    payment_type: str = ""
    expires_at: datetime | None = None
    expiry_source: str = ""


StripePixDirectState = StripePaymentDirectState


class _StripePayloadMetaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.data_message = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.data_message or tag.lower() != "meta":
            return
        values = {str(key or "").lower(): str(value or "") for key, value in attrs}
        if values.get("id") == "payload" and values.get("data-message"):
            self.data_message = values["data-message"]


def normalize_pix_cleanup_mode(value: Any) -> PixCleanupMode:
    mode = str(value or PIX_CLEANUP_MODE_EXPIRED).strip().lower()
    if mode not in PIX_CLEANUP_MODES:
        raise ValueError(f"Unsupported PIX cleanup mode: {mode}")
    return cast(PixCleanupMode, mode)


def _utc_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        try:
            parsed = datetime.fromtimestamp(timestamp, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    else:
        raw = str(value or "").strip()
        if not raw:
            return None
        if raw.isdigit():
            return _utc_datetime(int(raw))
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def pix_schedule_expires_at(generated_at: Any) -> datetime | None:
    """Derive the PIX deadline from the Beijing 11:00 daily rollover."""

    generated_utc = _utc_datetime(generated_at)
    if generated_utc is None:
        return None
    generated_beijing = generated_utc.astimezone(PIX_PAYMENT_TIMEZONE)
    expiry_date = generated_beijing.date()
    if generated_beijing.timetz().replace(tzinfo=None) >= PIX_DAILY_EXPIRY_TIME:
        expiry_date += timedelta(days=1)
    expires_beijing = datetime.combine(
        expiry_date,
        PIX_DAILY_EXPIRY_TIME,
        tzinfo=PIX_PAYMENT_TIMEZONE,
    )
    return expires_beijing.astimezone(timezone.utc)


def pix_effective_expires_at(payload: dict[str, Any] | None) -> tuple[datetime | None, str]:
    """Prefer Stripe's deadline, falling back to the Beijing rollover rule."""

    if not isinstance(payload, dict):
        return None, "missing"
    provider_epoch = normalize_payment_link_expires_at(payload.get("link_expires_at"))
    if provider_epoch is not None:
        return datetime.fromtimestamp(provider_epoch, timezone.utc), "provider"
    derived = pix_schedule_expires_at(payload.get("generated_at") or payload.get("created_at"))
    return (derived, "beijing_11") if derived is not None else (None, "missing")


def upi_effective_expires_at(payload: dict[str, Any] | None) -> tuple[datetime | None, str]:
    """Return UPI's concrete QR deadline; never substitute checkout expiry.

    ``link_expires_at`` is the normalized scalar returned by long-link.  Older
    payloads may instead retain the nested SetupIntent response, so inspect the
    QR shape as a fallback.  Missing provider data remains unknown rather than
    fabricating a deadline from generation time.
    """

    if not isinstance(payload, dict):
        return None, "missing"
    qr_epoch = extract_payment_link_qr_expires_at(payload, payment_type=PAYMENT_LINK_TYPE_UPI)
    if qr_epoch is not None:
        return datetime.fromtimestamp(qr_epoch, timezone.utc), "upi_qr_code"
    provider_epoch = normalize_payment_link_expires_at(payload.get("link_expires_at"))
    if provider_epoch is not None:
        source = str(payload.get("link_expiry_source") or "").strip().lower()
        if source == "checkout_session":
            return None, "missing"
        if not source:
            generated_at = _utc_datetime(payload.get("generated_at") or payload.get("created_at"))
            if (
                generated_at is None
                or provider_epoch
                > int(generated_at.timestamp()) + UPI_QR_VALIDITY_SECONDS + UPI_QR_EXPIRY_TOLERANCE_SECONDS
            ):
                return None, "missing"
        # Historical upstream rows persisted the QR-derived scalar before the
        # provenance field was added.  Untagged values remain compatible, but
        # an explicitly tagged Checkout Session deadline is never accepted.
        return datetime.fromtimestamp(provider_epoch, timezone.utc), source or "upi_qr_code"
    return None, "missing"


def payment_link_effective_expires_at(
    payload: dict[str, Any] | None,
    payment_type: Any = "",
) -> tuple[datetime | None, str]:
    normalized_type = normalize_payment_link_type(payment_type) or payment_link_type_from_payload(payload)
    if normalized_type == PAYMENT_LINK_TYPE_UPI:
        return upi_effective_expires_at(payload)
    if normalized_type == PAYMENT_LINK_TYPE_PIX:
        return pix_effective_expires_at(payload)
    return None, "missing"


def latest_pix_expiry_cutoff(now: datetime | None = None) -> datetime:
    now_utc = _utc_datetime(now or datetime.now(timezone.utc)) or datetime.now(timezone.utc)
    now_beijing = now_utc.astimezone(PIX_PAYMENT_TIMEZONE)
    cutoff_date = now_beijing.date()
    if now_beijing.timetz().replace(tzinfo=None) < PIX_DAILY_EXPIRY_TIME:
        cutoff_date -= timedelta(days=1)
    return datetime.combine(
        cutoff_date,
        PIX_DAILY_EXPIRY_TIME,
        tzinfo=PIX_PAYMENT_TIMEZONE,
    ).astimezone(timezone.utc)


def _current_payment_link_url(payload: dict[str, Any]) -> str:
    for key in _CURRENT_LINK_URL_FIELDS:
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return ""


def _strict_stripe_pix_url(value: Any) -> str:
    url = str(value or "").strip()
    if not url or len(url) > 10_000 or any(ord(character) < 32 or ord(character) == 127 for character in url):
        return ""
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return ""
    if (
        parsed.scheme.lower() != "https"
        or (parsed.hostname or "").lower() != _STRIPE_PIX_HOST
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or not any(
            parsed.path.startswith(prefix)
            and parsed.path.removeprefix(prefix).strip("/")
            for prefix in (_STRIPE_PIX_PATH_PREFIX, _STRIPE_UPI_PATH_PREFIX)
        )
    ):
        return ""
    return url


def parse_stripe_payment_instruction_html(
    value: bytes | str,
    *,
    expected_payment_type: str = "",
) -> StripePaymentDirectState:
    """Extract safe state/QR-expiry scalars from a Stripe instruction page."""

    if isinstance(value, bytes):
        if len(value) > _STRIPE_PIX_MAX_RESPONSE_BYTES:
            return StripePaymentDirectState(attempted=True, success=False)
        html = value.decode("utf-8", errors="replace")
    else:
        html = str(value or "")
        if len(html.encode("utf-8", errors="replace")) > _STRIPE_PIX_MAX_RESPONSE_BYTES:
            return StripePaymentDirectState(attempted=True, success=False)
    parser = _StripePayloadMetaParser()
    try:
        parser.feed(html)
        encoded = parser.data_message.strip()
        if not encoded or len(encoded) > _STRIPE_PIX_MAX_RESPONSE_BYTES:
            return StripePaymentDirectState(attempted=True, success=False)
        decoded = base64.b64decode(encoded, validate=True)
        payload = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return StripePixDirectState(attempted=True, success=False)
    if not isinstance(payload, dict):
        return StripePaymentDirectState(attempted=True, success=False)
    detected_type = normalize_payment_link_type(
        payload.get("payment_method_type") or payload.get("payment_type")
    )
    if not detected_type:
        raw_type = str(payload.get("type") or "").strip().lower()
        detected_type = "upi" if raw_type == "upi" else "pix" if raw_type == "qr_instructions" else ""
    expected_type = normalize_payment_link_type(expected_payment_type)
    if detected_type not in PAYMENT_LINK_TYPES or (expected_type and detected_type != expected_type):
        return StripePaymentDirectState(attempted=True, success=False)
    intent_state = str(payload.get("intent_state") or "").strip().lower()
    server_timestamp = _utc_datetime(payload.get("server_timestamp"))
    if not _STRIPE_INTENT_STATE_RE.fullmatch(intent_state) or server_timestamp is None:
        return StripePaymentDirectState(attempted=True, success=False)
    expiry_epoch = extract_payment_link_qr_expires_at(payload, payment_type=detected_type)
    expiry = datetime.fromtimestamp(expiry_epoch, timezone.utc) if expiry_epoch is not None else None
    return StripePaymentDirectState(
        attempted=True,
        success=True,
        intent_state=intent_state,
        server_timestamp=server_timestamp,
        payment_type=detected_type,
        expires_at=expiry,
        expiry_source="upi_qr_code" if detected_type == "upi" and expiry is not None else "",
    )


def parse_stripe_pix_instruction_html(value: bytes | str) -> StripePixDirectState:
    """Backward-compatible PIX-only parser."""

    return parse_stripe_payment_instruction_html(value, expected_payment_type=PAYMENT_LINK_TYPE_PIX)


def _read_limited_response_body(response: requests.Response) -> bytes | None:
    content_length = str(response.headers.get("Content-Length") or "").strip()
    if content_length:
        try:
            if int(content_length) > _STRIPE_PIX_MAX_RESPONSE_BYTES:
                return None
        except ValueError:
            return None
    body = bytearray()
    for chunk in response.iter_content(chunk_size=16 * 1024):
        if not chunk:
            continue
        body.extend(chunk)
        if len(body) > _STRIPE_PIX_MAX_RESPONSE_BYTES:
            return None
    return bytes(body)


def _fetch_stripe_pix_instruction(
    url: str,
    *,
    http_get: Callable[..., requests.Response] | None = None,
) -> StripePixDirectState:
    current_url = _strict_stripe_pix_url(url)
    if not current_url:
        return StripePixDirectState(attempted=False, success=False)
    request_get = http_get or requests.get
    for redirect_index in range(_STRIPE_PIX_MAX_REDIRECTS + 1):
        response: requests.Response | None = None
        try:
            response = request_get(
                current_url,
                allow_redirects=False,
                stream=True,
                timeout=(_STRIPE_PIX_CONNECT_TIMEOUT_SECONDS, _STRIPE_PIX_READ_TIMEOUT_SECONDS),
                headers={
                    "Accept": "text/html,application/xhtml+xml",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                    "User-Agent": "Mozilla/5.0 (compatible; auto-gpt-pix-status/1.0)",
                },
            )
            if response.status_code in _STRIPE_REDIRECT_STATUSES:
                if redirect_index >= _STRIPE_PIX_MAX_REDIRECTS:
                    return StripePixDirectState(attempted=True, success=False)
                next_url = _strict_stripe_pix_url(urljoin(current_url, str(response.headers.get("Location") or "")))
                if not next_url:
                    return StripePixDirectState(attempted=True, success=False)
                current_url = next_url
                continue
            if response.status_code != 200:
                return StripePixDirectState(attempted=True, success=False)
            content_type = str(response.headers.get("Content-Type") or "").lower()
            if "text/html" not in content_type:
                return StripePixDirectState(attempted=True, success=False)
            body = _read_limited_response_body(response)
            if body is None:
                return StripePixDirectState(attempted=True, success=False)
            try:
                path = (urlsplit(current_url).path or "").lower()
            except ValueError:
                path = ""
            expected_type = (
                PAYMENT_LINK_TYPE_UPI
                if path.startswith(_STRIPE_UPI_PATH_PREFIX)
                else PAYMENT_LINK_TYPE_PIX
            )
            return parse_stripe_payment_instruction_html(body, expected_payment_type=expected_type)
        except requests.RequestException:
            return StripePixDirectState(attempted=True, success=False)
        finally:
            if response is not None:
                response.close()
    return StripePixDirectState(attempted=True, success=False)


def _direct_scan_concurrency() -> int:
    try:
        configured = int(str(os.getenv("PIX_LINK_DIRECT_SCAN_CONCURRENCY") or _STRIPE_PIX_DEFAULT_CONCURRENCY).strip())
    except (TypeError, ValueError):
        configured = _STRIPE_PIX_DEFAULT_CONCURRENCY
    return min(max(configured, 1), _STRIPE_PIX_MAX_CONCURRENCY)


def _scan_stripe_pix_states(
    candidates: list[PixLinkCandidate],
) -> dict[tuple[int, str], StripePixDirectState]:
    if not candidates:
        return {}
    candidates_by_url: dict[str, list[PixLinkCandidate]] = {}
    results: dict[tuple[int, str], StripePixDirectState] = {}
    for candidate in candidates:
        strict_url = _strict_stripe_pix_url(candidate.current_url)
        if not strict_url:
            results[(candidate.account_id, candidate.current_url)] = StripePixDirectState(
                attempted=False,
                success=False,
            )
            continue
        candidates_by_url.setdefault(strict_url, []).append(candidate)
    if not candidates_by_url:
        return results
    worker_count = min(_direct_scan_concurrency(), len(candidates_by_url))
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="pix-stripe-scan") as executor:
        futures = {
            executor.submit(_fetch_stripe_pix_instruction, url): (url, linked_candidates)
            for url, linked_candidates in candidates_by_url.items()
        }
        for future in as_completed(futures):
            _url, linked_candidates = futures[future]
            try:
                direct_state = future.result()
            except Exception:
                direct_state = StripePixDirectState(attempted=True, success=False)
            for candidate in linked_candidates:
                results[(candidate.account_id, candidate.current_url)] = direct_state
    return results


def _scan_stripe_payment_states(
    candidates: list[PaymentLinkCandidate],
) -> dict[tuple[int, str], StripePaymentDirectState]:
    """Generic alias used by the mixed PIX/UPI scanner."""

    return _scan_stripe_pix_states(candidates)


def _load_current_payment_link_candidates(
    session: Session,
    *,
    payment_type: str | None = None,
) -> list[PaymentLinkCandidate]:
    rows = session.exec(
        text(
            """
            WITH account_json AS (
                SELECT
                    id,
                    cashier_url,
                    CASE WHEN json_valid(extra_json) THEN extra_json ELSE '{}' END AS extra
                FROM accounts
                WHERE platform = 'chatgpt'
            )
            SELECT
                id,
                cashier_url,
                json_extract(extra, '$.chatgpt_last_payment_link') AS link_json,
                json_extract(extra, '$.baxigpt_cdk') AS payment_json
            FROM account_json
            WHERE json_type(extra, '$.chatgpt_last_payment_link') = 'object'
            """
        )
    ).mappings().all()
    requested_type = normalize_payment_link_type(payment_type) if payment_type else ""
    history_expiry: dict[tuple[int, str], tuple[int, str]] = {}
    try:
        history_rows = session.exec(
            text(
                """
                SELECT account_id, url, link_type, result_json
                FROM payment_link_generations
                WHERE lower(trim(coalesce(status, ''))) = 'succeeded'
                  AND trim(coalesce(url, '')) <> ''
                """
            )
        ).mappings().all()
    except Exception:
        history_rows = []
    for history in history_rows:
        account_id = int(history.get("account_id") or 0)
        history_url = str(history.get("url") or "").strip()
        if not account_id or not history_url:
            continue
        try:
            result_payload = json.loads(str(history.get("result_json") or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            result_payload = {}
        if not isinstance(result_payload, dict):
            result_payload = {}
        history_type = normalize_payment_link_type(
            result_payload.get("link_type")
            or result_payload.get("payment_method_type")
            or history.get("link_type")
        )
        if history_type not in PAYMENT_LINK_TYPES:
            continue
        expiry, source = payment_link_effective_expires_at(result_payload, history_type)
        if expiry is None:
            continue
        history_expiry[(account_id, history_url)] = (int(expiry.timestamp()), source)

    candidates: list[PaymentLinkCandidate] = []
    for row in rows:
        try:
            payload = json.loads(str(row.get("link_json") or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        payment_type_value = normalize_payment_link_type(payment_link_type_from_payload(payload))
        if payment_type_value not in PAYMENT_LINK_TYPES:
            continue
        if requested_type and payment_type_value != requested_type:
            continue
        try:
            payment_marker = json.loads(str(row.get("payment_json") or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            payment_marker = {}
        if not isinstance(payment_marker, dict):
            payment_marker = {}
        current_url = _current_payment_link_url(payload)
        if not current_url:
            continue
        generated_at = _utc_datetime(payload.get("generated_at") or payload.get("created_at"))
        expires_at, expiry_source = payment_link_effective_expires_at(payload, payment_type_value)
        if expires_at is None:
            history_value = history_expiry.get((int(row.get("id") or 0), current_url))
            if history_value is not None:
                expires_at = datetime.fromtimestamp(history_value[0], timezone.utc)
                expiry_source = history_value[1] or expiry_source
        candidates.append(
            PaymentLinkCandidate(
                account_id=int(row.get("id") or 0),
                cashier_url=str(row.get("cashier_url") or "").strip(),
                current_url=current_url,
                payload=payload,
                payment_marker=payment_marker,
                link_status=normalize_payment_link_status(payload.get("link_status")),
                generated_at=generated_at,
                expires_at=expires_at,
                expiry_source=expiry_source,
                payment_type=payment_type_value,
            )
        )
    return candidates


def _load_current_pix_link_candidates(session: Session) -> list[PixLinkCandidate]:
    """Backward-compatible PIX-only candidate loader."""

    return _load_current_payment_link_candidates(session, payment_type=PAYMENT_LINK_TYPE_PIX)


def _payment_marker_timestamp(marker: dict[str, Any]) -> datetime | None:
    values = [
        _utc_datetime(marker.get(key))
        for key in ("last_checked_at", "paid_at", "failed_at", "submitted_at")
    ]
    valid = [value for value in values if value is not None]
    return max(valid) if valid else None


def _marker_applies_to_current_user_link(candidate: PixLinkCandidate) -> bool:
    marker = candidate.payment_marker
    payment_type = normalize_payment_link_type(candidate.payment_type)
    marker_channel = normalize_payment_link_type(marker.get("payment_channel"))
    if marker_channel and marker_channel != payment_type:
        return False
    if payment_type == PAYMENT_LINK_TYPE_PIX and str(marker.get("pix_submit_mode") or "").strip().lower() != "user_link":
        return False
    marker_at = _payment_marker_timestamp(marker)
    if candidate.generated_at is not None and marker_at is not None and candidate.generated_at > marker_at:
        return False
    return marker_at is not None or candidate.link_status == "pix_submitted"


def _is_paid_candidate(candidate: PixLinkCandidate) -> bool:
    if candidate.link_status in _PAID_LINK_STATUSES:
        return True
    marker_status = normalize_payment_link_status(candidate.payment_marker.get("status"))
    return (
        candidate.link_status == "pix_submitted"
        and marker_status in _PAID_PAYMENT_STATUSES
        and _marker_applies_to_current_user_link(candidate)
    )


def _payment_cancelled_evidence(marker: dict[str, Any]) -> bool:
    status = normalize_payment_link_status(marker.get("upstream_status") or marker.get("status"))
    if status in _CANCELLED_PAYMENT_STATUSES:
        return True
    text = " ".join(
        str(marker.get(key) or "").strip().lower()
        for key in ("last_error_message", "error_code", "failure_status", "message")
    )
    return any(token in text for token in (
        "支付已取消",
        "payment cancelled",
        "payment canceled",
        "payment_cancelled",
        "payment_canceled",
    ))


def _is_cancelled_candidate(candidate: PixLinkCandidate) -> bool:
    if candidate.link_status in _CANCELLED_LINK_STATUSES:
        return True
    marker_status = normalize_payment_link_status(candidate.payment_marker.get("status"))
    return (
        marker_status in ({"failed"} | _CANCELLED_PAYMENT_STATUSES)
        and _marker_applies_to_current_user_link(candidate)
        and _payment_cancelled_evidence(candidate.payment_marker)
    )


def _base_report(
    candidates: list[PixLinkCandidate],
    *,
    now: datetime,
    cleanup_mode: PixCleanupMode = PIX_CLEANUP_MODE_EXPIRED,
    direct_results: Mapping[tuple[int, str], StripePixDirectState] | None = None,
    payment_type: str | None = None,
) -> tuple[dict[str, Any], list[PixLinkCandidate]]:
    mode = normalize_pix_cleanup_mode(cleanup_mode)
    now_utc = _utc_datetime(now) or datetime.now(timezone.utc)
    cutoff_utc = latest_pix_expiry_cutoff(now_utc)
    normalized_scope = normalize_payment_link_type(payment_type) if payment_type else ""
    scoped_candidates = [
        item
        for item in candidates
        if not normalized_scope or normalize_payment_link_type(item.payment_type) == normalized_scope
    ]
    candidates = scoped_candidates
    paid: list[PixLinkCandidate] = []
    cancelled: list[PixLinkCandidate] = []
    expired: list[PixLinkCandidate] = []
    valid: list[PixLinkCandidate] = []
    direct_states: dict[str, int] = {}
    direct_attempted = 0
    direct_success = 0
    effective_candidates: list[PixLinkCandidate] = []
    for original_item in candidates:
        item = original_item
        # These buckets drive both the scan UI and cleanup eligibility, so a
        # current link must belong to exactly one operator-visible category.
        direct_state = (direct_results or {}).get((item.account_id, item.current_url))
        item_type = normalize_payment_link_type(item.payment_type)
        state_type = normalize_payment_link_type(direct_state.payment_type) if direct_state is not None else ""
        if state_type and state_type != item_type:
            # A URL/type mismatch is not a trustworthy direct result.  Keep the
            # local classification and expiry instead of allowing one rail's
            # Stripe page to affect another rail.
            direct_state = None
        if direct_state is not None and direct_state.expires_at is not None:
            # A live Stripe page can be newer than the cached/history result.
            # Use its QR deadline for this scan without mutating the account
            # until an explicit cleanup transaction is confirmed.
            item = replace(
                item,
                expires_at=direct_state.expires_at,
                expiry_source=direct_state.expiry_source or item.expiry_source,
            )
        if direct_state is not None and direct_state.attempted:
            direct_attempted += 1
        if direct_state is not None and direct_state.success:
            direct_success += 1
            direct_states[direct_state.intent_state] = direct_states.get(direct_state.intent_state, 0) + 1
        effective_candidates.append(item)
        if direct_state is not None and direct_state.success and direct_state.intent_state == "succeeded":
            paid.append(item)
        elif direct_state is not None and direct_state.success and direct_state.intent_state in {"canceled", "cancelled"}:
            cancelled.append(item)
        elif (
            direct_state is not None
            and direct_state.success
            and direct_state.server_timestamp is not None
            and item.expires_at is not None
            and item.expires_at <= direct_state.server_timestamp
        ):
            expired.append(item)
        elif direct_state is not None and direct_state.success:
            valid.append(item)
        elif _is_paid_candidate(item):
            paid.append(item)
        elif _is_cancelled_candidate(item):
            cancelled.append(item)
        elif item.expires_at is not None and item.expires_at <= now_utc:
            expired.append(item)
        else:
            valid.append(item)
    eligible_by_mode = {
        PIX_CLEANUP_MODE_EXPIRED: expired,
        PIX_CLEANUP_MODE_PAID: paid,
        PIX_CLEANUP_MODE_CANCELLED: cancelled,
    }
    eligible = eligible_by_mode[mode]
    missing = [item for item in effective_candidates if item.expires_at is None]
    valid_missing = [item for item in valid if item.expires_at is None]
    provider_expiry_sources = {"provider", "pix_qr_code", "upi_qr_code"}
    provider_count = sum(item.expiry_source in provider_expiry_sources for item in effective_candidates)
    derived_count = sum(item.expiry_source == "beijing_11" for item in effective_candidates)
    cutoff_beijing = cutoff_utc.astimezone(PIX_PAYMENT_TIMEZONE)
    type_counts = {
        payment_type: sum(1 for item in candidates if normalize_payment_link_type(item.payment_type) == payment_type)
        for payment_type in sorted(PAYMENT_LINK_TYPES)
    }
    report = {
        "instance_id": str(os.getenv("APP_INSTANCE_ID") or "auto-gpt").strip() or "auto-gpt",
        "timezone": "Asia/Shanghai",
        "now": now_utc.isoformat(),
        "cutoff_at": cutoff_utc.isoformat(),
        "cutoff_at_beijing": cutoff_beijing.isoformat(),
        "cutoff_display": cutoff_beijing.strftime("%Y-%m-%d %H:%M"),
        "cleanup_mode": mode,
        "cleanup_label": PIX_CLEANUP_MODE_LABELS[mode],
        "current_pix_links": len(candidates),
        "valid_links": len(valid),
        "expired_links": len(expired),
        "paid_links": len(paid),
        "cancelled_links": len(cancelled),
        "eligible_links": len(eligible),
        "retained_links": len(candidates) - len(eligible),
        "active_links": sum(item.expires_at is not None and item.expires_at > now_utc for item in valid),
        "provider_expiry_links": provider_count,
        "derived_expiry_links": derived_count,
        "missing_expiry_links": len(missing),
        "valid_missing_expiry_links": len(valid_missing),
        "direct_scan_source": "stripe_direct",
        "direct_scan_attempted_links": direct_attempted,
        "direct_scan_success_links": direct_success,
        "direct_scan_fallback_links": len(candidates) - direct_success,
        "direct_scan_state_counts": dict(sorted(direct_states.items())),
        "current_upi_links": type_counts.get(PAYMENT_LINK_TYPE_UPI, 0),
        "payment_type_counts": type_counts,
        "payment_types": sorted(type_counts),
        "upi_qr_expiry_links": sum(
            1
            for item in effective_candidates
            if normalize_payment_link_type(item.payment_type) == PAYMENT_LINK_TYPE_UPI
            and item.expiry_source == "upi_qr_code"
        ),
        "upi_qr_validity_seconds": UPI_QR_VALIDITY_SECONDS,
    }
    for payment_type_name in sorted(PAYMENT_LINK_TYPES):
        report[f"{payment_type_name}_links"] = type_counts.get(payment_type_name, 0)
        report[f"{payment_type_name}_valid_links"] = sum(
            1 for item in valid if normalize_payment_link_type(item.payment_type) == payment_type_name
        )
        report[f"{payment_type_name}_expired_links"] = sum(
            1 for item in expired if normalize_payment_link_type(item.payment_type) == payment_type_name
        )
        report[f"{payment_type_name}_paid_links"] = sum(
            1 for item in paid if normalize_payment_link_type(item.payment_type) == payment_type_name
        )
        report[f"{payment_type_name}_cancelled_links"] = sum(
            1 for item in cancelled if normalize_payment_link_type(item.payment_type) == payment_type_name
        )
    # ``current_pix_links`` is a legacy name.  For a mixed scan it must only
    # report PIX while the generic counters carry the full scope.
    if any(normalize_payment_link_type(item.payment_type) != PAYMENT_LINK_TYPE_PIX for item in candidates):
        report["current_pix_links"] = type_counts.get(PAYMENT_LINK_TYPE_PIX, 0)
        report["pix_links"] = type_counts.get(PAYMENT_LINK_TYPE_PIX, 0)
        report["upi_links"] = type_counts.get(PAYMENT_LINK_TYPE_UPI, 0)
    return report, eligible


def _assert_integrity(connection: sqlite3.Connection, *, label: str) -> None:
    result = [str(row[0] or "").strip().lower() for row in connection.execute("PRAGMA integrity_check")]
    if result != ["ok"]:
        raise RuntimeError(f"SQLite integrity_check failed for {label}: {result[:3]}")


def _assert_session_integrity(session: Session) -> None:
    rows = session.exec(text("PRAGMA integrity_check")).all()
    result: list[str] = []
    for row in rows:
        try:
            value = row[0]
        except (IndexError, KeyError, TypeError):
            value = row
        result.append(str(value or "").strip().lower())
    if result != ["ok"]:
        raise RuntimeError(f"SQLite integrity_check failed after PIX cleanup: {result[:3]}")


def _session_database_path(session: Session) -> Path | None:
    bind = session.get_bind()
    if str(getattr(bind.dialect, "name", "")).lower() != "sqlite":
        return None
    database = str(getattr(bind.url, "database", "") or "").strip()
    if not database or database == ":memory:":
        return None
    path = Path(database).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def _create_verified_backup(session: Session, *, now: datetime) -> str:
    database = _session_database_path(session)
    if database is None:
        return ""
    if not database.is_file() or database.stat().st_size <= 0:
        raise RuntimeError(f"SQLite database is missing or empty: {database}")

    runtime_dir = Path(os.getenv("APP_RUNTIME_DIR") or database.parent).expanduser().resolve()
    backup_dir = runtime_dir / "pix-link-cleanup-backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    required_bytes = database.stat().st_size + _BACKUP_MIN_FREE_MARGIN_BYTES
    if shutil.disk_usage(backup_dir).free < required_bytes:
        raise RuntimeError("Insufficient disk space for verified PIX cleanup backup")

    timestamp = now.strftime("%Y%m%dT%H%M%S%fZ")
    backup = backup_dir / f"{database.stem}.before-pix-link-cleanup.{timestamp}.{os.getpid()}.backup"
    source = sqlite3.connect(str(database), timeout=30)
    destination = sqlite3.connect(str(backup), timeout=30)
    try:
        source.execute("PRAGMA busy_timeout=30000")
        _assert_integrity(source, label=str(database))
        source.backup(destination, pages=2048, sleep=0.05)
        destination.commit()
        _assert_integrity(destination, label=str(backup))
    except Exception:
        destination.close()
        source.close()
        backup.unlink(missing_ok=True)
        raise
    destination.close()
    source.close()
    backup.chmod(0o600)
    return str(backup)


def preview_payment_link_cleanup(
    session: Session,
    *,
    payment_type: str | None = None,
    cleanup_mode: PixCleanupMode = PIX_CLEANUP_MODE_EXPIRED,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Scan current QR payment links, optionally scoped to one payment type."""

    now_utc = _utc_datetime(now or datetime.now(timezone.utc)) or datetime.now(timezone.utc)
    candidates = _load_current_payment_link_candidates(session, payment_type=payment_type)
    session.rollback()
    direct_results = _scan_stripe_payment_states(candidates)
    report, _ = _base_report(
        candidates,
        now=now_utc,
        cleanup_mode=cleanup_mode,
        direct_results=direct_results,
        payment_type=payment_type,
    )
    return report


def preview_expired_pix_payment_links(
    session: Session,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    # Preserve the exact legacy report shape consumed by older operators/tests.
    report = preview_payment_link_cleanup(
        session,
        payment_type=PAYMENT_LINK_TYPE_PIX,
        cleanup_mode=PIX_CLEANUP_MODE_EXPIRED,
        now=now,
    )
    for key in (
        "current_upi_links",
        "payment_type_counts",
        "payment_types",
        "upi_qr_expiry_links",
        "upi_qr_validity_seconds",
        "pix_links",
        "upi_links",
        "pix_valid_links",
        "pix_expired_links",
        "pix_paid_links",
        "pix_cancelled_links",
        "upi_valid_links",
        "upi_expired_links",
        "upi_paid_links",
        "upi_cancelled_links",
    ):
        report.pop(key, None)
    return report


def preview_pix_payment_link_cleanup(
    session: Session,
    *,
    cleanup_mode: PixCleanupMode = PIX_CLEANUP_MODE_EXPIRED,
    now: datetime | None = None,
) -> dict[str, Any]:
    return preview_payment_link_cleanup(
        session,
        payment_type=PAYMENT_LINK_TYPE_PIX,
        cleanup_mode=cleanup_mode,
        now=now,
    )


def preview_upi_payment_link_cleanup(
    session: Session,
    *,
    cleanup_mode: PixCleanupMode = PIX_CLEANUP_MODE_EXPIRED,
    now: datetime | None = None,
) -> dict[str, Any]:
    return preview_payment_link_cleanup(
        session,
        payment_type=PAYMENT_LINK_TYPE_UPI,
        cleanup_mode=cleanup_mode,
        now=now,
    )


def _cleaned_link_payload(
    candidate: PixLinkCandidate,
    *,
    cleaned_at: datetime,
    cleanup_mode: PixCleanupMode,
) -> dict[str, Any]:
    mode = normalize_pix_cleanup_mode(cleanup_mode)
    payment_type = normalize_payment_link_type(candidate.payment_type) or PAYMENT_LINK_TYPE_PIX
    cleaned_status = _CLEANED_STATUS_BY_TYPE_AND_MODE[(payment_type, mode)]
    type_label = payment_type.upper()
    payload = {
        key: value
        for key, value in candidate.payload.items()
        if key not in _LINK_URL_FIELDS_TO_REMOVE
    }
    previous_status = str(candidate.payload.get("link_status") or "").strip()
    if previous_status and previous_status not in PAYMENT_LINK_CLEANED_STATUSES:
        payload["previous_link_status"] = previous_status
    if mode == PIX_CLEANUP_MODE_EXPIRED and payment_type == PAYMENT_LINK_TYPE_PIX:
        cutoff_at = latest_pix_expiry_cutoff(cleaned_at)
        cleanup_through_at = max(
            value
            for value in (cutoff_at, candidate.generated_at)
            if value is not None
        )
    elif mode == PIX_CLEANUP_MODE_EXPIRED:
        cleanup_through_at = max(
            value
            for value in (candidate.expires_at, candidate.generated_at, cleaned_at)
            if value is not None
        )
    else:
        cleanup_through_at = cleaned_at
    payload.update(
        {
            "link_status": cleaned_status,
            "link_status_reason": f"{type_label} {str(_CLEANED_REASON_BY_MODE[mode]).lower()}",
            "link_status_updated_at": cleaned_at.isoformat(),
            "cleaned_at": cleaned_at.isoformat(),
            "pix_cleanup_through_at": cleanup_through_at.isoformat(),
            "pix_cleanup_mode": mode,
            "payment_link_type": payment_type,
            "link_expiry_source": candidate.expiry_source,
        }
    )
    if mode == PIX_CLEANUP_MODE_EXPIRED:
        payload["expired_at"] = candidate.expires_at.isoformat() if candidate.expires_at is not None else ""
    if candidate.expires_at is not None:
        payload["link_expires_at"] = int(candidate.expires_at.timestamp())
    return payload


def clean_expired_pix_payment_links(
    session: Session,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Clean expired current links and their exact cashier mirror in one transaction."""
    return clean_pix_payment_links(
        session,
        cleanup_mode=PIX_CLEANUP_MODE_EXPIRED,
        now=now,
    )


def clean_payment_links(
    session: Session,
    *,
    payment_type: str | None = None,
    cleanup_mode: PixCleanupMode = PIX_CLEANUP_MODE_EXPIRED,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Clean one explicit QR-link category without touching payment history."""

    mode = normalize_pix_cleanup_mode(cleanup_mode)
    now_utc = _utc_datetime(now or datetime.now(timezone.utc)) or datetime.now(timezone.utc)
    initial_candidates = _load_current_payment_link_candidates(session, payment_type=payment_type)
    session.rollback()
    direct_results = _scan_stripe_payment_states(initial_candidates)
    initial_report, initial_eligible = _base_report(
        initial_candidates,
        now=now_utc,
        cleanup_mode=mode,
        direct_results=direct_results,
        payment_type=payment_type,
    )
    if not initial_eligible:
        initial_report.update(
            {
                "cleaned_links": 0,
                "concurrent_skipped_links": 0,
                "list_state_refreshed": 0,
                "backup_created": False,
            }
        )
        return initial_report

    initial_eligible_keys = {
        (candidate.account_id, candidate.current_url)
        for candidate in initial_eligible
    }
    backup_path = _create_verified_backup(session, now=now_utc)
    try:
        session.exec(text("BEGIN IMMEDIATE"))
        report, current_eligible = _base_report(
            _load_current_payment_link_candidates(session, payment_type=payment_type),
            now=now_utc,
            cleanup_mode=mode,
            direct_results=direct_results,
            payment_type=payment_type,
        )
        eligible = [
            candidate
            for candidate in current_eligible
            if (candidate.account_id, candidate.current_url) in initial_eligible_keys
        ]
        cleaned_ids: list[int] = []
        for candidate in eligible:
            marker_json = json.dumps(
                _cleaned_link_payload(candidate, cleaned_at=now_utc, cleanup_mode=mode),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            result = session.exec(
                text(
                    """
                    UPDATE accounts
                    SET
                        extra_json = json_set(
                            extra_json,
                            '$.chatgpt_last_payment_link',
                            json(:marker_json)
                        ),
                        cashier_url = CASE WHEN cashier_url = :current_url THEN '' ELSE cashier_url END,
                        updated_at = :updated_at
                    WHERE id = :account_id
                      AND platform = 'chatgpt'
                      AND json_valid(extra_json)
                      AND coalesce(
                            nullif(trim(json_extract(extra_json, '$.chatgpt_last_payment_link.url')), ''),
                            nullif(trim(json_extract(extra_json, '$.chatgpt_last_payment_link.paypal_url')), ''),
                            nullif(trim(json_extract(extra_json, '$.chatgpt_last_payment_link.provider_redirect_url')), ''),
                            nullif(trim(json_extract(extra_json, '$.chatgpt_last_payment_link.approval_url')), ''),
                            nullif(trim(json_extract(extra_json, '$.chatgpt_last_payment_link.checkout_url')), ''),
                            nullif(trim(json_extract(extra_json, '$.chatgpt_last_payment_link.cashier_url')), ''),
                            ''
                          ) = :current_url
                    """
                ),
                params={
                    "marker_json": marker_json,
                    "current_url": candidate.current_url,
                    "updated_at": now_utc.isoformat(),
                    "account_id": candidate.account_id,
                },
            )
            if int(result.rowcount or 0) == 1:
                cleaned_ids.append(candidate.account_id)

        list_state_refreshed = 0
        if cleaned_ids:
            list_state_refreshed = upsert_account_list_state_for_account_ids(
                session,
                cleaned_ids,
                commit=False,
            )
            if list_state_refreshed != len(cleaned_ids):
                raise RuntimeError("payment-link list state did not refresh completely")
        session.commit()
        _assert_session_integrity(session)
        session.rollback()
    except Exception:
        session.rollback()
        raise

    report.update(
        {
            "cleaned_links": len(cleaned_ids),
            "concurrent_skipped_links": len(initial_eligible) - len(cleaned_ids),
            "list_state_refreshed": list_state_refreshed,
            "backup_created": bool(backup_path),
        }
    )
    return report


def clean_pix_payment_links(
    session: Session,
    *,
    cleanup_mode: PixCleanupMode = PIX_CLEANUP_MODE_EXPIRED,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Backward-compatible PIX-only cleanup entry point."""

    return clean_payment_links(
        session,
        payment_type=PAYMENT_LINK_TYPE_PIX,
        cleanup_mode=cleanup_mode,
        now=now,
    )


def clean_upi_payment_links(
    session: Session,
    *,
    cleanup_mode: PixCleanupMode = PIX_CLEANUP_MODE_EXPIRED,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Clean UPI links using the provider QR expiry/status evidence."""

    return clean_payment_links(
        session,
        payment_type=PAYMENT_LINK_TYPE_UPI,
        cleanup_mode=cleanup_mode,
        now=now,
    )
