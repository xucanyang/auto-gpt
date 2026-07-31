"""External API for claiming cached ChatGPT subscription links."""

from __future__ import annotations

import secrets
import threading
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from core.config_store import config_store
from core.db import AccountModel, ExternalSubscriptionClaimModel, get_session
from services.chatgpt_account_state import (
    apply_chatgpt_status_policy,
    classify_chatgpt_capabilities,
    mark_payment_failed,
    mark_payment_pending,
    mark_payment_succeeded,
)
from services.chatgpt_core.payment_link_cache import (
    normalize_payment_link_params,
    payment_link_status_label,
)


router = APIRouter(prefix="/external/subscription-links", tags=["external-subscription-links"])

EXTERNAL_CLAIM_KEY = "external_subscription_claim"
EXTERNAL_PAYMENT_KEY = "external_subscription_payment"
MAX_LIMIT = 100
DEFAULT_LEASE_SECONDS = 900
MAX_LEASE_SECONDS = 86400
DEFAULT_VERIFY_AFTER_SECONDS = 300
DEFAULT_PRECHECK_SECONDS = 300
PRECHECK_FAILED_COOLDOWN_SECONDS = 600
SCAN_BATCH_SIZE = 50
DUE_VERIFICATION_LIMIT = 25
SENDABLE_CURRENCY = "USD"
ACTIVE_CLAIM_STATUSES = {"prechecking", "claimed", "processing"}
TERMINAL_LINK_STATUSES = {
    "paid",
    "already_paid",
    "invalid",
    "not_usd",
    "amount_not_zero",
}
ACTIVE_LINK_STATUSES = {"leased", "verify_pending"}
_VERIFY_TIMERS: set[str] = set()
_VERIFY_TIMERS_LOCK = threading.Lock()
_VERIFY_SWEEP_STOP = threading.Event()
_VERIFY_SWEEP_THREAD: threading.Thread | None = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse_dt(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if not text:
        return default
    return text in {"1", "true", "yes", "on", "enabled", "enable"}


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _verify_after_seconds() -> int:
    value = _safe_int(config_store.get("external_subscription_verify_after_seconds", ""), DEFAULT_VERIFY_AFTER_SECONDS)
    return min(MAX_LEASE_SECONDS, max(60, value))


def _verify_label() -> str:
    return f"{_verify_after_seconds()}s"


def _clean_text(value: Any, limit: int = 500) -> str:
    text = str(value or "").strip()
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def _is_zero_amount(value: Any) -> bool:
    text = str(value if value is not None else "").strip()
    if not text:
        return False
    normalized = text.replace(",", "").strip()
    candidates = [normalized]
    parts = normalized.split()
    if parts:
        candidates.extend([parts[0], parts[-1]])
    for candidate in candidates:
        token = str(candidate or "").strip().strip("$")
        if not token:
            continue
        try:
            return Decimal(token) == 0
        except (InvalidOperation, ValueError):
            continue
    return normalized.upper() in {"0", "0.0", "0.00", "0 USD", "0.0 USD", "0.00 USD", "$0", "$0.00"}


def _now_id(prefix: str) -> str:
    return f"{prefix}_{_utcnow().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:10]}"


def _looks_like_paypal_url(value: Any) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return False
    return (
        "ba_token=" in text
        or ("paypal." in text and "/agreements/approve" in text)
        or ("paypal.com" in text and "approve" in text)
    )


def _currency_from_amount_text(value: Any) -> str:
    text = str(value or "").strip().upper()
    if "USD" in text or "$" in text:
        return "USD"
    return ""


def _external_api_enabled() -> bool:
    return _parse_bool(config_store.get("external_subscription_api_enabled", "false"), default=False)


def _external_api_token() -> str:
    return str(config_store.get("external_subscription_api_token", "") or "").strip()


def _require_external_api_token(authorization: str = Header(default="")) -> None:
    if not _external_api_enabled():
        raise HTTPException(status_code=403, detail="外部订阅链接 API 未启用")
    expected = _external_api_token()
    if not expected:
        raise HTTPException(status_code=403, detail="外部订阅链接 API token 未配置")
    token = str(authorization or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token or not secrets.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="外部订阅链接 API token 无效")


class ClaimSubscriptionLinksRequest(BaseModel):
    consumer: str = ""
    limit: int = 1
    plan: str = ""
    country: str = ""
    currency: str = ""
    lease_seconds: int = DEFAULT_LEASE_SECONDS


class SubscriptionLinkResultRequest(BaseModel):
    status: str
    provider: str = "external"
    external_payment_id: str = ""
    paid_at: str = ""
    failed_at: str = ""
    message: str = ""
    error_code: str = ""
    raw: dict[str, Any] = {}
    trigger_auth_capture: bool = False


class ReleaseSubscriptionLinkRequest(BaseModel):
    reason: str = ""


def _payment_link_from_account(account: AccountModel) -> dict[str, Any]:
    extra = account.get_extra()
    cached = extra.get("chatgpt_last_payment_link") if isinstance(extra.get("chatgpt_last_payment_link"), dict) else {}
    paypal = extra.get("chatgpt_paypal_url") if isinstance(extra.get("chatgpt_paypal_url"), dict) else {}
    legacy_approval = (
        extra.get("chatgpt_oaipay_approval")
        if isinstance(extra.get("chatgpt_oaipay_approval"), dict)
        else {}
    )
    paypal_url = str(
        paypal.get("paypal_url")
        or paypal.get("url")
        or paypal.get("approval_url")
        or legacy_approval.get("paypal_url")
        or legacy_approval.get("approval_url")
        or legacy_approval.get("provider_redirect_url")
        or legacy_approval.get("long_url")
        or cached.get("paypal_url")
        or (
            cached.get("url")
            if str(cached.get("payment_link_format") or "").strip().lower() == "paypal_url"
            or _looks_like_paypal_url(cached.get("url"))
            else ""
        )
        or ""
    ).strip()
    if paypal_url:
        explicit_plan = str(paypal.get("plan") or cached.get("plan") or "").strip().lower()
        if explicit_plan != "plus":
            return {}
        amount_text = str(paypal.get("checkout_amount") or legacy_approval.get("checkout_amount") or cached.get("checkout_amount") or "").strip()
        currency = str(paypal.get("currency") or legacy_approval.get("currency") or cached.get("currency") or _currency_from_amount_text(amount_text) or SENDABLE_CURRENCY).strip().upper()
        source = str(paypal.get("source") or legacy_approval.get("upstream") or legacy_approval.get("source") or cached.get("source") or "").strip()
        payload: dict[str, Any] = {
            "url": paypal_url,
            "paypal_url": paypal_url,
            "plan": explicit_plan,
            "country": str(paypal.get("country") or paypal.get("billing_country") or legacy_approval.get("billing_country") or cached.get("country") or "US").strip().upper() or "US",
            "currency": currency or SENDABLE_CURRENCY,
            "proxy": str(paypal.get("proxy") or cached.get("proxy") or "").strip(),
            "payment_link_format": "paypal_url",
            "link_type": str(paypal.get("link_type") or legacy_approval.get("link_type") or "paypal").strip() or "paypal",
            "source": source,
            "created_at": str(paypal.get("created_at") or legacy_approval.get("created_at") or legacy_approval.get("updated_at") or "").strip(),
            "upstream": source,
        }
        if amount_text:
            payload["checkout_amount"] = amount_text
            payload["checkout_amount_is_zero"] = _is_zero_amount(amount_text)
        elif "checkout_amount_is_zero" in paypal:
            payload["checkout_amount_is_zero"] = _parse_bool(paypal.get("checkout_amount_is_zero"), default=False)
        elif "checkout_amount_is_zero" in cached:
            payload["checkout_amount_is_zero"] = _parse_bool(cached.get("checkout_amount_is_zero"), default=False)
        else:
            payload["checkout_amount_is_zero"] = True
        for key in (
            "cs_id",
            "payment_method_id",
            "payment_method_type",
            "processor_entity",
            "payment_locale",
            "provider_redirect_url",
            "long_url",
            "stripe_redirect_url",
            "stripe_hosted_url",
            "stage",
            "status",
            "link_status",
            "link_status_reason",
            "last_preflight_at",
            "precheck_retry_after_at",
            "verify_after_at",
            "lease_expires_at",
            "claim_id",
        ):
            if key in paypal:
                payload[key] = paypal.get(key)
            elif key in legacy_approval:
                payload[key] = legacy_approval.get(key)
            elif key in cached:
                payload[key] = cached.get(key)
        return payload

    url = str(
        cached.get("url")
        or cached.get("paypal_url")
        or cached.get("checkout_url")
        or cached.get("cashier_url")
        or account.cashier_url
        or extra.get("cashier_url")
        or ""
    ).strip()
    if not url:
        return {}
    explicit_plan = str(cached.get("plan") or "").strip().lower()
    if explicit_plan != "plus":
        return {}
    params = normalize_payment_link_params(cached)
    payload: dict[str, Any] = {
        "url": url,
        "paypal_url": str(cached.get("paypal_url") or "").strip(),
        "plan": explicit_plan,
        "country": params["country"],
        "currency": params["currency"],
        "proxy": params["proxy"],
        "payment_link_format": params.get("payment_link_format", "long_hosted"),
        "source": str(cached.get("source") or "").strip(),
        "created_at": str(cached.get("created_at") or "").strip(),
    }
    for key in (
        "checkout_amount",
        "checkout_amount_is_zero",
        "link_status",
        "link_status_reason",
        "last_preflight_at",
        "precheck_retry_after_at",
        "verify_after_at",
        "lease_expires_at",
        "claim_id",
    ):
        if key in cached:
            payload[key] = cached.get(key)
    billing = cached.get("billing") if isinstance(cached.get("billing"), dict) else {}
    if billing:
        payload["billing"] = {
            key: str(billing.get(key) or "").strip()
            for key in ("country", "email")
            if str(billing.get(key) or "").strip()
        }
    return payload


def _claim_is_active(claim: dict[str, Any], now: datetime) -> bool:
    status = str(claim.get("status") or "").strip().lower()
    if status not in {"claimed", "processing"}:
        return False
    expires_at = _parse_dt(claim.get("lease_expires_at"))
    return bool(expires_at and expires_at > now)


def _link_is_blocked(link: dict[str, Any], now: datetime) -> bool:
    status = str(link.get("link_status") or "").strip().lower()
    if status in TERMINAL_LINK_STATUSES:
        return True
    if status == "precheck_failed":
        retry_after_at = _parse_dt(link.get("precheck_retry_after_at"))
        return bool(retry_after_at and retry_after_at > now)
    if status in ACTIVE_LINK_STATUSES:
        expires_at = _parse_dt(link.get("lease_expires_at"))
        verify_after_at = _parse_dt(link.get("verify_after_at"))
        if expires_at and expires_at > now:
            return True
        if verify_after_at and verify_after_at > now:
            return True
    return False


def _cached_subscription_link_preflight(link: dict[str, Any]) -> dict[str, Any]:
    link_format = str(link.get("payment_link_format") or "").strip().lower()
    if link_format == "paypal_url" or _looks_like_paypal_url(link.get("paypal_url") or link.get("url")):
        currency = str(link.get("currency") or "").strip().upper()
        amount_value = link.get("checkout_amount")
        amount_text = str(amount_value if amount_value is not None else "").strip()
        amount_is_zero = (
            _parse_bool(link.get("checkout_amount_is_zero"), default=False)
            or _is_zero_amount(amount_value)
            or (not amount_text and currency == SENDABLE_CURRENCY)
        )
        if currency and currency != SENDABLE_CURRENCY:
            return {
                "ok_to_send": False,
                "link_status": "not_usd",
                "reason": f"{payment_link_status_label('not_usd')}: PayPal 链接账单货币不是 USD: {currency}",
                "probe": {
                    "source": "paypal_url_cache",
                    "currency": currency.lower(),
                    "amount_text": amount_text,
                    "amount_is_zero": amount_is_zero,
                    "payment_link_format": "paypal_url",
                },
            }
        if not amount_is_zero:
            return {
                "ok_to_send": False,
                "link_status": "amount_not_zero",
                "reason": f"{payment_link_status_label('amount_not_zero')}: PayPal 链接账单金额不是 0: {amount_text or 'unknown'}",
                "probe": {
                    "source": "paypal_url_cache",
                    "currency": (currency or SENDABLE_CURRENCY).lower(),
                    "amount": amount_value,
                    "amount_text": amount_text,
                    "amount_is_zero": False,
                    "payment_link_format": "paypal_url",
                },
            }
        return {
            "ok_to_send": True,
            "link_status": "available",
            "reason": "已使用缓存的 PayPal approval URL 校验结果",
            "probe": {
                "source": "paypal_url_cache",
                "currency": (currency or SENDABLE_CURRENCY).lower(),
                "amount": amount_value,
                "amount_text": amount_text or "0",
                "amount_is_zero": True,
                "payment_link_format": "paypal_url",
            },
            "checkout_amount": amount_text or "0",
            "checkout_amount_is_zero": True,
        }

    currency = str(link.get("currency") or "").strip().upper()
    amount_seen = "checkout_amount" in link or "checkout_amount_is_zero" in link
    amount_value = link.get("checkout_amount")
    amount_text = str(amount_value if amount_value is not None else "").strip()
    amount_is_zero = _parse_bool(link.get("checkout_amount_is_zero"), default=False) or _is_zero_amount(amount_value)

    if currency and currency != SENDABLE_CURRENCY:
        return {
            "ok_to_send": False,
            "link_status": "not_usd",
            "reason": f"{payment_link_status_label('not_usd')}: 账单货币不是 USD: {currency}",
            "probe": {
                "source": "cached_payment_link",
                "currency": currency.lower(),
                "amount_text": amount_text,
                "amount_is_zero": amount_is_zero,
            },
        }
    if amount_seen and not amount_is_zero:
        return {
            "ok_to_send": False,
            "link_status": "amount_not_zero",
            "reason": f"{payment_link_status_label('amount_not_zero')}: 账单金额不是 0: {amount_text or 'unknown'}",
            "probe": {
                "source": "cached_payment_link",
                "currency": currency.lower(),
                "amount": amount_value,
                "amount_text": amount_text,
                "amount_is_zero": False,
            },
        }
    if currency == SENDABLE_CURRENCY and amount_is_zero:
        return {
            "ok_to_send": True,
            "link_status": "available",
            "reason": "已使用缓存的 USD 0 元账单校验结果",
            "probe": {
                "source": "cached_payment_link",
                "currency": currency.lower(),
                "amount": amount_value,
                "amount_text": amount_text or "0",
                "amount_is_zero": True,
            },
            "checkout_amount": amount_text or "0",
            "checkout_amount_is_zero": True,
        }
    return {}


def _claim_subscription_link_preflight(account: AccountModel, link: dict[str, Any]) -> dict[str, Any]:
    cached_preflight = _cached_subscription_link_preflight(link)
    if cached_preflight and not bool(cached_preflight.get("ok_to_send")):
        return cached_preflight
    if (
        cached_preflight
        and (
            str(link.get("payment_link_format") or "").strip().lower() == "paypal_url"
            or _looks_like_paypal_url(link.get("paypal_url") or link.get("url"))
        )
    ):
        return cached_preflight
    # A cached USD 0 amount can become stale after the checkout session is paid or expires.
    # The external claim API is the send boundary, so positive cache hits must be rechecked live.
    return _preflight_subscription_link(account, link)


def _claim_matches_filters(link: dict[str, Any], req: ClaimSubscriptionLinksRequest) -> bool:
    requested_plan = str(req.plan or "").strip().lower()
    if requested_plan and requested_plan != "plus":
        return False
    if str(link.get("plan") or "").strip().lower() != "plus":
        return False
    if req.country:
        expected = normalize_payment_link_params({
            "plan": link.get("plan") or "plus",
            "country": req.country or link.get("country"),
            "currency": link.get("currency"),
        })
        actual = normalize_payment_link_params(link)
        if expected["country"] != actual["country"]:
            return False
    return True


def _claim_requires_usd_zero(req: ClaimSubscriptionLinksRequest) -> bool:
    if str(req.currency or "").strip() and str(req.currency or "").strip().upper() != SENDABLE_CURRENCY:
        return False
    return True


def _serialize_claimed_item(account: AccountModel, link: dict[str, Any], claim: dict[str, Any]) -> dict[str, Any]:
    item = {
        "account_id": int(account.id or 0),
        "email": account.email,
        "status": account.status,
        "payment_link": link.get("url") or "",
        "paypal_url": link.get("paypal_url") or link.get("url") or "",
        "plan": link.get("plan") or "plus",
        "country": link.get("country") or "",
        "currency": link.get("currency") or "",
        "claim_id": claim.get("claim_id") or "",
        "claim_status": claim.get("status") or "",
        "consumer": claim.get("consumer") or "",
        "lease_expires_at": claim.get("lease_expires_at") or "",
        "verify_after_at": claim.get("verify_after_at") or "",
    }
    for key in ("checkout_amount", "checkout_amount_is_zero", "source", "created_at", "billing", "payment_link_format", "upstream", "link_type"):
        if key in link:
            item[key] = link.get(key)
    return item


def _claim_row_to_dict(row: ExternalSubscriptionClaimModel) -> dict[str, Any]:
    details = row.get_details()
    claim: dict[str, Any] = {
        "claim_id": row.claim_id,
        "consumer": row.consumer,
        "status": row.status,
        "claimed_at": row.claimed_at,
        "lease_expires_at": row.lease_expires_at,
        "verify_after_at": row.verify_after_at,
        "payment_link": row.payment_link,
        "paypal_url": details.get("paypal_url") or row.payment_link,
        "plan": row.plan,
        "country": row.country,
        "currency": row.currency,
        "external_payment_id": row.external_payment_id,
        "provider": row.provider,
        "paid_at": row.paid_at,
        "failed_at": row.failed_at,
        "released_at": row.released_at,
        "last_error": row.last_error,
        "failure_status": details.get("failure_status") or row.error_code,
        "release_reason": details.get("release_reason") or "",
        "attempt": _safe_int(details.get("attempt"), 1),
        "source": "external_subscription_claims",
    }
    for key in ("payment_link_format", "link_source", "upstream", "link_type"):
        if details.get(key):
            claim[key] = details.get(key)
    for key in ("precheck_expires_at", "prechecked_at", "result_written_at", "message", "error_code"):
        value = getattr(row, key, "")
        if value:
            claim[key] = value
    return claim


def _find_claim_row(session: Session, claim_id: str) -> Optional[ExternalSubscriptionClaimModel]:
    claim_id = str(claim_id or "").strip()
    if not claim_id:
        return None
    return session.exec(
        select(ExternalSubscriptionClaimModel).where(ExternalSubscriptionClaimModel.claim_id == claim_id)
    ).first()


def _active_claim_for_account(session: Session, account_id: int) -> Optional[ExternalSubscriptionClaimModel]:
    return session.exec(
        select(ExternalSubscriptionClaimModel)
        .where(ExternalSubscriptionClaimModel.account_id == int(account_id or 0))
        .where(ExternalSubscriptionClaimModel.status.in_(tuple(ACTIVE_CLAIM_STATUSES)))
        .order_by(ExternalSubscriptionClaimModel.id.desc())
    ).first()


def _expire_stale_claims(session: Session, now: datetime) -> int:
    precheck_result = session.execute(
        text(
            """
            UPDATE external_subscription_claims
            SET status = 'precheck_expired',
                last_error = '预检预占超时，已自动释放',
                updated_at = :updated_at
            WHERE status = 'prechecking'
              AND precheck_expires_at != ''
              AND precheck_expires_at <= :now_iso
            """
        ),
        {"updated_at": now, "now_iso": _iso(now)},
    )
    lease_result = session.execute(
        text(
            """
            UPDATE external_subscription_claims
            SET status = 'lease_expired',
                released_at = :now_iso,
                last_error = '领取租约超时，已自动释放',
                updated_at = :updated_at
            WHERE status = 'claimed'
              AND lease_expires_at != ''
              AND lease_expires_at <= :now_iso
            """
        ),
        {"updated_at": now, "now_iso": _iso(now)},
    )
    session.commit()
    return int(getattr(precheck_result, "rowcount", 0) or 0) + int(getattr(lease_result, "rowcount", 0) or 0)


def _expire_stale_prechecking_claims(session: Session, now: datetime) -> int:
    return _expire_stale_claims(session, now)


def _update_claim_row_from_claim(
    session: Session,
    claim: dict[str, Any],
    *,
    details_update: dict[str, Any] | None = None,
) -> None:
    row = _find_claim_row(session, str(claim.get("claim_id") or ""))
    if row is None:
        return
    for key in (
        "consumer",
        "status",
        "payment_link",
        "plan",
        "country",
        "currency",
        "lease_expires_at",
        "verify_after_at",
        "claimed_at",
        "prechecked_at",
        "result_written_at",
        "paid_at",
        "failed_at",
        "released_at",
        "provider",
        "external_payment_id",
        "message",
        "error_code",
        "last_error",
    ):
        if key in claim:
            setattr(row, key, str(claim.get(key) or ""))
    details = row.get_details()
    if isinstance(details_update, dict):
        details.update(details_update)
    if "attempt" in claim:
        details["attempt"] = _safe_int(claim.get("attempt"), 1)
    if "failure_status" in claim:
        details["failure_status"] = str(claim.get("failure_status") or "")
    if "release_reason" in claim:
        details["release_reason"] = str(claim.get("release_reason") or "")
    row.set_details(details)
    row.updated_at = _utcnow()
    session.add(row)


def _reserve_subscription_claim(
    session: Session,
    account: AccountModel,
    link: dict[str, Any],
    *,
    consumer: str,
    now: datetime,
    attempt: int,
) -> dict[str, Any]:
    if _active_claim_for_account(session, int(account.id or 0)) is not None:
        return {}
    claim_id = _now_id("subclaim")
    precheck_expires_at = now + timedelta(seconds=DEFAULT_PRECHECK_SECONDS)
    row = ExternalSubscriptionClaimModel(
        claim_id=claim_id,
        account_id=int(account.id or 0),
        email=str(account.email or ""),
        consumer=consumer,
        status="prechecking",
        payment_link=str(link.get("url") or ""),
        plan=str(link.get("plan") or "plus"),
        country=str(link.get("country") or ""),
        currency=str(link.get("currency") or ""),
        precheck_expires_at=_iso(precheck_expires_at),
        claimed_at=_iso(now),
    )
    row.set_details(
        {
            "attempt": max(1, int(attempt or 1)),
            "precheck_seconds": DEFAULT_PRECHECK_SECONDS,
            "paypal_url": str(link.get("paypal_url") or "").strip(),
            "payment_link_format": str(link.get("payment_link_format") or "").strip(),
            "link_source": str(link.get("source") or "").strip(),
            "upstream": str(link.get("upstream") or link.get("source") or "").strip(),
            "link_type": str(link.get("link_type") or "").strip(),
        }
    )
    session.add(row)
    try:
        session.commit()
        session.refresh(row)
    except IntegrityError:
        session.rollback()
        return {}
    return _claim_row_to_dict(row)


def _account_probe_object(account: AccountModel, extra: dict[str, Any] | None = None) -> SimpleNamespace:
    data = extra if isinstance(extra, dict) else account.get_extra()
    return SimpleNamespace(
        id=account.id,
        email=account.email,
        password=account.password,
        user_id=account.user_id,
        token=account.token,
        status=account.status,
        access_token=str(data.get("access_token") or account.token or "").strip(),
        refresh_token=str(data.get("refresh_token") or "").strip(),
        id_token=str(data.get("id_token") or "").strip(),
        session_token=str(data.get("session_token") or "").strip(),
        client_id=str(data.get("client_id") or "app_EMoamEEZ73f0CkXaXp7hrann").strip(),
        cookies=str(data.get("cookies") or "").strip(),
        workspace_id=str(data.get("workspace_id") or "").strip(),
        extra=data,
    )


def _refresh_account_local_status(session: Session, account: AccountModel, extra: dict[str, Any]) -> dict[str, Any]:
    from services.chatgpt_core.status_probe import probe_local_chatgpt_status

    probe_account = _account_probe_object(account, extra)
    probe = probe_local_chatgpt_status(probe_account, proxy="")
    extra["chatgpt_local"] = probe
    account.set_extra(extra)
    capabilities = classify_chatgpt_capabilities(account, local_probe=probe)
    extra["chatgpt_capabilities"] = capabilities
    account.set_extra(extra)
    apply_chatgpt_status_policy(account, local_probe=probe)
    _persist_account(session, account, extra)
    return probe


def _paid_from_probe(account: AccountModel, probe: dict[str, Any] | None) -> bool:
    if not isinstance(probe, dict) or not probe:
        return False
    capabilities = classify_chatgpt_capabilities(account, local_probe=probe if isinstance(probe, dict) else None)
    return bool(capabilities.get("has_paid_subscription"))


def _mark_link_status(
    extra: dict[str, Any],
    *,
    status: str,
    reason: str = "",
    probe: dict[str, Any] | None = None,
    now: datetime | None = None,
    **updates: Any,
) -> dict[str, Any]:
    link = extra.get("chatgpt_last_payment_link") if isinstance(extra.get("chatgpt_last_payment_link"), dict) else {}
    link = dict(link or {})
    current = now or _utcnow()
    link["link_status"] = str(status or "").strip()
    link["link_status_reason"] = _clean_text(reason, 800)
    link["link_status_updated_at"] = _iso(current)
    if isinstance(probe, dict):
        link["last_preflight_probe"] = probe
    if updates:
        link.update({key: value for key, value in updates.items() if value is not None})
    extra["chatgpt_last_payment_link"] = link
    paypal = extra.get("chatgpt_paypal_url") if isinstance(extra.get("chatgpt_paypal_url"), dict) else {}
    if paypal:
        paypal_url = str(paypal.get("paypal_url") or paypal.get("url") or "").strip()
        link_url = str(link.get("paypal_url") or link.get("url") or "").strip()
        if not paypal_url or not link_url or paypal_url == link_url:
            paypal.update({key: value for key, value in link.items() if key in {
                "link_status",
                "link_status_reason",
                "link_status_updated_at",
                "last_preflight_probe",
                "checkout_amount",
                "checkout_amount_is_zero",
                "currency",
                "claim_id",
                "consumer",
                "lease_expires_at",
                "verify_after_at",
                "last_preflight_at",
                "precheck_retry_after_at",
            }})
            extra["chatgpt_paypal_url"] = paypal
    return link


def _preflight_subscription_link(account: AccountModel, link: dict[str, Any]) -> dict[str, Any]:
    from services.chatgpt_core.checkout_probe import probe_chatgpt_checkout_amount

    checkout_url = str(link.get("url") or "").strip()
    if not checkout_url:
        return {"ok_to_send": False, "link_status": "invalid", "reason": "无效: 订阅链接为空", "probe": {}}
    try:
        probe = probe_chatgpt_checkout_amount(
            _account_probe_object(account),
            checkout_url=checkout_url,
            country=str(link.get("country") or "US"),
            currency=str(link.get("currency") or SENDABLE_CURRENCY),
            proxy=str(link.get("proxy") or ""),
        )
    except Exception as exc:
        message = _clean_text(exc, 800)
        lowered = message.lower()
        if any(needle in lowered for needle in ("you have paid", "already paid", "no payment required")):
            status = "already_paid"
        elif any(needle in lowered for needle in ("expired", "invalid", "no such checkout", "not found")):
            status = "invalid"
        else:
            status = "precheck_failed"
        label = payment_link_status_label(status)
        return {"ok_to_send": False, "link_status": status, "reason": f"{label}: {message}", "probe": {}, "error": message}

    currency = str(probe.get("currency") or link.get("currency") or "").strip().upper()
    amount = probe.get("amount")
    amount_is_zero = bool(probe.get("amount_is_zero")) or _is_zero_amount(probe.get("amount_text")) or _is_zero_amount(amount)
    if currency != SENDABLE_CURRENCY:
        return {
            "ok_to_send": False,
            "link_status": "not_usd",
            "reason": f"{payment_link_status_label('not_usd')}: 账单货币不是 USD: {currency or 'unknown'}",
            "probe": probe,
        }
    if not amount_is_zero:
        return {
            "ok_to_send": False,
            "link_status": "amount_not_zero",
            "reason": f"{payment_link_status_label('amount_not_zero')}: 账单金额不是 0: {probe.get('amount_text') or amount or 'unknown'}",
            "probe": probe,
        }
    return {
        "ok_to_send": True,
        "link_status": "available",
        "reason": "",
        "probe": probe,
        "checkout_amount": probe.get("amount_text") or probe.get("amount") or "0",
        "checkout_amount_is_zero": True,
    }


def _find_claim(session: Session, claim_id: str) -> tuple[AccountModel, dict[str, Any], dict[str, Any]]:
    claim_id = str(claim_id or "").strip()
    if not claim_id:
        raise HTTPException(status_code=404, detail="claim 不存在")
    row = _find_claim_row(session, claim_id)
    if row is not None:
        account = session.get(AccountModel, int(row.account_id or 0))
        if account is None or account.platform != "chatgpt":
            raise HTTPException(status_code=404, detail="claim 对应账号不存在")
        return account, account.get_extra(), _claim_row_to_dict(row)
    rows = session.exec(select(AccountModel).where(AccountModel.platform == "chatgpt")).all()
    for account in rows:
        extra = account.get_extra()
        claim = extra.get(EXTERNAL_CLAIM_KEY) if isinstance(extra.get(EXTERNAL_CLAIM_KEY), dict) else {}
        if str(claim.get("claim_id") or "") == claim_id:
            return account, extra, claim
    raise HTTPException(status_code=404, detail="claim 不存在")


def _persist_account(session: Session, account: AccountModel, extra: dict[str, Any]) -> None:
    account.set_extra(extra)
    account.updated_at = _utcnow()
    session.add(account)
    session.commit()
    session.refresh(account)


def _upsert_pending_subscription_auth(account: AccountModel, link: dict[str, Any]) -> None:
    # 订阅补抓 Auth 已改为账号级 OAuth capture；pending 只属于旧 team invite 链路。
    return None


def _enqueue_resume_subscription_auth(account_id: int) -> str:
    try:
        from api.tasks import enqueue_resume_subscription_auth_task

        return enqueue_resume_subscription_auth_task(int(account_id or 0))
    except Exception:
        return ""


def _set_claim_paid(
    session: Session,
    account: AccountModel,
    extra: dict[str, Any],
    claim: dict[str, Any],
    *,
    now: datetime,
    provider: str,
    message: str,
) -> dict[str, Any]:
    claim.update({
        "status": "paid",
        "result_written_at": _iso(now),
        "paid_at": claim.get("paid_at") or _iso(now),
        "provider": provider,
    })
    payment = {
        "status": "paid",
        "provider": provider,
        "external_payment_id": str(claim.get("external_payment_id") or ""),
        "claim_id": str(claim.get("claim_id") or ""),
        "message": message,
        "error_code": "",
        "written_at": _iso(now),
        "paid_at": claim["paid_at"],
        "raw": {},
    }
    extra[EXTERNAL_CLAIM_KEY] = claim
    extra[EXTERNAL_PAYMENT_KEY] = payment
    _mark_link_status(extra, status="paid", reason=message, now=now)
    mark_payment_succeeded(account, reason="external_subscription_local_verify_paid")
    _update_claim_row_from_claim(session, claim, details_update={"payment": payment})
    _persist_account(session, account, extra)
    return payment


def _set_claim_failed(
    session: Session,
    account: AccountModel,
    extra: dict[str, Any],
    claim: dict[str, Any],
    *,
    now: datetime,
    status: str,
    reason: str,
) -> dict[str, Any]:
    claim.update({
        "status": "failed",
        "failed_at": _iso(now),
        "result_written_at": _iso(now),
        "last_error": _clean_text(reason, 800),
        "failure_status": status,
    })
    payment = {
        "status": "failed",
        "provider": "local_verify",
        "external_payment_id": str(claim.get("external_payment_id") or ""),
        "claim_id": str(claim.get("claim_id") or ""),
        "message": _clean_text(reason, 800),
        "error_code": status,
        "written_at": _iso(now),
        "failed_at": _iso(now),
        "raw": {},
    }
    extra[EXTERNAL_CLAIM_KEY] = claim
    extra[EXTERNAL_PAYMENT_KEY] = payment
    _mark_link_status(extra, status=status, reason=reason, now=now)
    mark_payment_failed(account, reason="external_subscription_local_verify_failed")
    _update_claim_row_from_claim(session, claim, details_update={"payment": payment})
    _persist_account(session, account, extra)
    return payment


def _set_claim_verify_pending(
    session: Session,
    account: AccountModel,
    extra: dict[str, Any],
    claim: dict[str, Any],
    payment: dict[str, Any],
    *,
    now: datetime,
    reason: str,
    probe: dict[str, Any] | None = None,
) -> dict[str, Any]:
    verify_after_at = now + timedelta(seconds=_verify_after_seconds())
    claim.update({
        "status": "processing",
        "result_written_at": _iso(now),
        "verify_after_at": _iso(verify_after_at),
        "last_error": _clean_text(reason, 800),
    })
    payment["status"] = "verify_pending"
    payment["verify_after_at"] = _iso(verify_after_at)
    extra[EXTERNAL_CLAIM_KEY] = claim
    extra[EXTERNAL_PAYMENT_KEY] = payment
    _mark_link_status(
        extra,
        status="verify_pending",
        reason=reason,
        probe=probe if isinstance(probe, dict) else None,
        now=now,
        verify_after_at=_iso(verify_after_at),
    )
    mark_payment_pending(account, reason="external_subscription_paid_waiting_local_plus")
    _update_claim_row_from_claim(session, claim, details_update={"payment": payment})
    _persist_account(session, account, extra)
    _schedule_subscription_verification(str(claim.get("claim_id") or ""), verify_after_at)
    return payment


def _verify_subscription_claim_now(session: Session, claim_id: str) -> dict[str, Any]:
    account, extra, claim = _find_claim(session, claim_id)
    now = _utcnow()
    status = str(claim.get("status") or "").strip().lower()
    if status == "paid":
        return {"ok": True, "status": "paid", "account_id": int(account.id or 0), "claim": claim}
    if status not in {"claimed", "processing"}:
        return {"ok": True, "status": status or "skipped", "account_id": int(account.id or 0), "claim": claim}

    probe: dict[str, Any] = {}
    status_probe_error = ""
    try:
        probe = _refresh_account_local_status(session, account, extra)
        extra = account.get_extra()
        claim = extra.get(EXTERNAL_CLAIM_KEY) if isinstance(extra.get(EXTERNAL_CLAIM_KEY), dict) else claim
    except Exception as exc:
        status_probe_error = f"本地状态复核失败: {_clean_text(exc, 700)}"

    if _paid_from_probe(account, probe):
        payment = _set_claim_paid(
            session,
            account,
            extra,
            claim,
            now=now,
            provider="local_verify",
            message=f"{_verify_label()} 本地复核已确认订阅状态",
        )
        task_id = _enqueue_resume_subscription_auth(int(account.id or 0))
        return {
            "ok": True,
            "status": "paid",
            "account_id": int(account.id or 0),
            "claim": claim,
            "payment": payment,
            "auth_capture_task_id": task_id,
        }

    link = _payment_link_from_account(account)
    preflight = _preflight_subscription_link(account, link) if link else {}
    preflight_status = str(preflight.get("link_status") or "").strip() or "precheck_failed"
    if preflight_status == "already_paid":
        reason = f"{_verify_label()} checkout 复核显示该订阅链接已支付，但本地订阅状态尚未显示 Plus/付费计划"
        payment = _set_claim_failed(
            session,
            account,
            extra,
            claim,
            now=now,
            status=preflight_status,
            reason=reason,
        )
        return {"ok": False, "status": preflight_status, "account_id": int(account.id or 0), "claim": claim, "payment": payment}

    if preflight and not bool(preflight.get("ok_to_send")) and preflight_status not in {"precheck_failed"}:
        failure_status = preflight_status
        failure_reason = str(preflight.get("reason") or f"{_verify_label()} checkout 复核未通过")
    else:
        failure_status = "unverified" if status_probe_error else "timeout_unpaid"
        failure_reason = status_probe_error or f"{_verify_label()} 本地复核未确认 paid/subscribed"
    payment = _set_claim_failed(
        session,
        account,
        extra,
        claim,
        now=now,
        status=failure_status,
        reason=failure_reason,
    )
    return {"ok": False, "status": failure_status, "account_id": int(account.id or 0), "claim": claim, "payment": payment}


def _verify_subscription_claim_after_delay(claim_id: str) -> None:
    try:
        from core.db import engine

        with Session(engine) as session:
            print(f"[ExternalSubscription] 执行本地复核: claim={claim_id}", flush=True)
            result = _verify_subscription_claim_now(session, claim_id)
            print(
                f"[ExternalSubscription] 本地复核完成: claim={claim_id} status={result.get('status')}",
                flush=True,
            )
    except Exception as exc:
        print(f"[ExternalSubscription] 本地复核失败: claim={claim_id} error={_clean_text(exc, 700)}", flush=True)
    finally:
        with _VERIFY_TIMERS_LOCK:
            _VERIFY_TIMERS.discard(str(claim_id or ""))


def _schedule_subscription_verification(claim_id: str, verify_after_at: datetime) -> None:
    claim_id = str(claim_id or "").strip()
    if not claim_id:
        return
    delay = max(0.0, (verify_after_at - _utcnow()).total_seconds())
    with _VERIFY_TIMERS_LOCK:
        if claim_id in _VERIFY_TIMERS:
            return
        _VERIFY_TIMERS.add(claim_id)
    print(f"[ExternalSubscription] 已调度本地复核: claim={claim_id} delay={delay:.1f}s", flush=True)
    timer = threading.Timer(delay, _verify_subscription_claim_after_delay, args=(claim_id,))
    timer.daemon = True
    timer.start()


def _schedule_due_local_verifications(session: Session, now: datetime) -> int:
    scheduled = 0
    rows = session.exec(
        select(ExternalSubscriptionClaimModel)
        .where(ExternalSubscriptionClaimModel.status.in_(("claimed", "processing")))
        .order_by(ExternalSubscriptionClaimModel.id.asc())
        .limit(1000)
    ).all()
    for row in rows:
        if scheduled >= DUE_VERIFICATION_LIMIT:
            break
        verify_after_at = _parse_dt(row.verify_after_at)
        if verify_after_at and verify_after_at <= now:
            _schedule_subscription_verification(row.claim_id, now)
            scheduled += 1
    return scheduled


def restore_subscription_verification_timers() -> int:
    from core.db import engine

    now = _utcnow()
    with Session(engine) as session:
        rows = session.exec(
            select(ExternalSubscriptionClaimModel)
            .where(ExternalSubscriptionClaimModel.status.in_(("claimed", "processing")))
            .order_by(ExternalSubscriptionClaimModel.id.asc())
            .limit(1000)
        ).all()
    scheduled = 0
    for row in rows:
        verify_after_at = _parse_dt(row.verify_after_at)
        if verify_after_at is None:
            verify_after_at = now
        _schedule_subscription_verification(row.claim_id, verify_after_at)
        scheduled += 1
    if scheduled:
        print(f"[ExternalSubscription] 已恢复 {scheduled} 个待本地复核 claim", flush=True)
    return scheduled


def _subscription_verification_sweep_loop() -> None:
    while not _VERIFY_SWEEP_STOP.wait(60):
        try:
            restore_subscription_verification_timers()
        except Exception as exc:
            print(f"[ExternalSubscription] 恢复本地复核定时器失败: {_clean_text(exc, 700)}", flush=True)


def start_subscription_verification_scheduler() -> None:
    global _VERIFY_SWEEP_THREAD
    _VERIFY_SWEEP_STOP.clear()
    restore_subscription_verification_timers()
    if _VERIFY_SWEEP_THREAD and _VERIFY_SWEEP_THREAD.is_alive():
        return
    _VERIFY_SWEEP_THREAD = threading.Thread(
        target=_subscription_verification_sweep_loop,
        name="external-subscription-verification-sweep",
        daemon=True,
    )
    _VERIFY_SWEEP_THREAD.start()


def stop_subscription_verification_scheduler() -> None:
    _VERIFY_SWEEP_STOP.set()


def _run_due_local_verifications(session: Session, now: datetime) -> int:
    checked = 0
    rows = session.exec(
        select(ExternalSubscriptionClaimModel)
        .where(ExternalSubscriptionClaimModel.status.in_(("claimed", "processing")))
        .order_by(ExternalSubscriptionClaimModel.id.asc())
        .limit(1000)
    ).all()
    for row in rows:
        if checked >= DUE_VERIFICATION_LIMIT:
            return checked
        verify_after_at = _parse_dt(row.verify_after_at)
        if verify_after_at and verify_after_at <= now:
            _verify_subscription_claim_now(session, row.claim_id)
            checked += 1

    legacy_rows = session.exec(
        select(AccountModel)
        .where(AccountModel.platform == "chatgpt")
        .order_by(AccountModel.id.asc())
        .limit(1000)
    ).all()
    for account in legacy_rows:
        if checked >= DUE_VERIFICATION_LIMIT:
            break
        extra = account.get_extra()
        claim = extra.get(EXTERNAL_CLAIM_KEY) if isinstance(extra.get(EXTERNAL_CLAIM_KEY), dict) else {}
        if str(claim.get("source") or "") == "external_subscription_claims":
            continue
        if str(claim.get("status") or "").strip().lower() not in {"claimed", "processing"}:
            continue
        verify_after_at = _parse_dt(claim.get("verify_after_at"))
        if verify_after_at and verify_after_at <= now:
            claim_id = str(claim.get("claim_id") or "").strip()
            if claim_id:
                _verify_subscription_claim_now(session, claim_id)
                checked += 1
    return checked


@router.post("/claim", dependencies=[Depends(_require_external_api_token)])
def claim_subscription_links(req: ClaimSubscriptionLinksRequest, session: Session = Depends(get_session)):
    requested_plan = str(req.plan or "").strip().lower()
    if requested_plan and requested_plan != "plus":
        raise HTTPException(status_code=400, detail="外部订阅链接只支持 Plus 套餐")
    now = _utcnow()
    _schedule_due_local_verifications(session, now)
    _expire_stale_claims(session, now)
    if not _claim_requires_usd_zero(req):
        raise HTTPException(status_code=400, detail="外部订阅链接只允许抽取 USD 0 元账单")
    limit = min(MAX_LIMIT, max(1, _safe_int(req.limit, 1)))
    lease_seconds = min(MAX_LEASE_SECONDS, max(60, _safe_int(req.lease_seconds, DEFAULT_LEASE_SECONDS)))
    lease_expires_at = now + timedelta(seconds=lease_seconds)
    verify_after_seconds = _verify_after_seconds()
    verify_after_at = now + timedelta(seconds=verify_after_seconds)
    consumer = str(req.consumer or "").strip()[:120]
    claimed: list[dict[str, Any]] = []
    last_seen_id = 0

    while len(claimed) < limit:
        rows = session.exec(
            select(AccountModel)
            .where(AccountModel.platform == "chatgpt")
            .where(AccountModel.status.notin_(["subscribed", "invalid"]))
            .where(AccountModel.id > last_seen_id)
            .order_by(AccountModel.id.asc())
            .limit(SCAN_BATCH_SIZE)
        ).all()
        if not rows:
            break

        for account in rows:
            last_seen_id = max(last_seen_id, int(account.id or 0))
            if len(claimed) >= limit:
                break
            extra = account.get_extra()
            current_claim = extra.get(EXTERNAL_CLAIM_KEY) if isinstance(extra.get(EXTERNAL_CLAIM_KEY), dict) else {}
            if _claim_is_active(current_claim, now):
                continue
            link = _payment_link_from_account(account)
            if not link or _link_is_blocked(link, now) or not _claim_matches_filters(link, req):
                continue

            claim = _reserve_subscription_claim(
                session,
                account,
                link,
                consumer=consumer,
                now=now,
                attempt=_safe_int(current_claim.get("attempt"), 0) + 1,
            )
            if not claim:
                continue

            preflight = _claim_subscription_link_preflight(account, link)
            preflight_probe = preflight.get("probe") if isinstance(preflight.get("probe"), dict) else {}
            if not bool(preflight.get("ok_to_send")):
                status = str(preflight.get("link_status") or "precheck_failed")
                reason = str(preflight.get("reason") or "订阅链接本地预检未通过")
                if status == "already_paid":
                    try:
                        _refresh_account_local_status(session, account, extra)
                        extra = account.get_extra()
                        if str(account.status or "").strip().lower() == "subscribed":
                            reason = "已经支付过: 已同步账号状态为已订阅"
                        else:
                            reason = "已经支付过: 已触发账号状态更新，本地仍未确认订阅"
                    except Exception as exc:
                        reason = f"已经支付过: 账号状态更新失败: {_clean_text(exc, 700)}"
                claim.update({
                    "status": status,
                    "prechecked_at": _iso(_utcnow()),
                    "result_written_at": _iso(_utcnow()),
                    "last_error": _clean_text(reason, 800),
                    "failure_status": status,
                })
                _mark_link_status(
                    extra,
                    status=status,
                    reason=reason,
                    probe=preflight_probe,
                    now=now,
                    last_preflight_at=_iso(now),
                    precheck_retry_after_at=(
                        _iso(now + timedelta(seconds=PRECHECK_FAILED_COOLDOWN_SECONDS))
                        if status == "precheck_failed"
                        else None
                    ),
                )
                extra[EXTERNAL_CLAIM_KEY] = claim
                _update_claim_row_from_claim(
                    session,
                    claim,
                    details_update={"preflight": preflight_probe, "preflight_error": reason},
                )
                _persist_account(session, account, extra)
                continue

            claim_id = str(claim.get("claim_id") or "")
            claim.update({
                "status": "claimed",
                "lease_expires_at": _iso(lease_expires_at),
                "verify_after_at": _iso(verify_after_at),
                "payment_link": link.get("url") or "",
                "plan": link.get("plan") or "plus",
                "country": link.get("country") or "",
                "currency": SENDABLE_CURRENCY,
                "prechecked_at": _iso(_utcnow()),
                "last_error": "",
            })
            extra[EXTERNAL_CLAIM_KEY] = claim
            updated_link = _mark_link_status(
                extra,
                status="leased",
                reason="已通过 USD 0 元本地预检并出租",
                probe=preflight_probe,
                now=now,
                last_preflight_at=_iso(now),
                checkout_amount=preflight.get("checkout_amount") or "0",
                checkout_amount_is_zero=True,
                currency=SENDABLE_CURRENCY,
                claim_id=claim_id,
                consumer=consumer,
                lease_expires_at=_iso(lease_expires_at),
                verify_after_at=_iso(verify_after_at),
            )
            mark_payment_pending(account, reason="external_subscription_claimed")
            _update_claim_row_from_claim(
                session,
                claim,
                details_update={"preflight": preflight_probe, "link_status": "leased"},
            )
            _persist_account(session, account, extra)
            _schedule_subscription_verification(claim_id, verify_after_at)
            claimed.append(_serialize_claimed_item(account, _payment_link_from_account(account), claim))

    return {
        "ok": True,
        "count": len(claimed),
        "lease_seconds": lease_seconds,
        "lease_expires_at": _iso(lease_expires_at),
        "verify_after_seconds": verify_after_seconds,
        "verify_after_at": _iso(verify_after_at),
        "items": claimed,
    }


@router.get("/{claim_id}", dependencies=[Depends(_require_external_api_token)])
def get_subscription_claim(claim_id: str, session: Session = Depends(get_session)):
    account, extra, claim = _find_claim(session, claim_id)
    link = _payment_link_from_account(account)
    payment = extra.get(EXTERNAL_PAYMENT_KEY) if isinstance(extra.get(EXTERNAL_PAYMENT_KEY), dict) else {}
    return {
        "ok": True,
        "account_id": int(account.id or 0),
        "email": account.email,
        "account_status": account.status,
        "claim": claim,
        "payment": payment,
        "item": _serialize_claimed_item(account, link, claim) if link else {},
    }


@router.post("/{claim_id}/release", dependencies=[Depends(_require_external_api_token)])
def release_subscription_claim(
    claim_id: str,
    req: ReleaseSubscriptionLinkRequest,
    session: Session = Depends(get_session),
):
    account, extra, claim = _find_claim(session, claim_id)
    status = str(claim.get("status") or "").strip().lower()
    if status == "paid":
        return {"ok": True, "released": False, "status": "paid", "message": "claim 已支付，不能释放"}
    claim.update({
        "status": "released",
        "released_at": _iso(_utcnow()),
        "release_reason": str(req.reason or "").strip(),
    })
    extra[EXTERNAL_CLAIM_KEY] = claim
    _mark_link_status(extra, status="available", reason=str(req.reason or "claim 已释放"), now=_utcnow())
    _update_claim_row_from_claim(session, claim, details_update={"release_reason": str(req.reason or "").strip()})
    _persist_account(session, account, extra)
    return {"ok": True, "released": True, "claim": claim}


@router.post("/{claim_id}/result", dependencies=[Depends(_require_external_api_token)])
def write_subscription_result(
    claim_id: str,
    req: SubscriptionLinkResultRequest,
    session: Session = Depends(get_session),
):
    account, extra, claim = _find_claim(session, claim_id)
    now = _utcnow()
    status = str(req.status or "").strip().lower()
    if status in {"success", "succeeded", "paid", "subscribed"}:
        normalized_status = "paid"
    elif status in {"failed", "fail", "error", "cancelled", "canceled"}:
        normalized_status = "failed"
    elif status in {"processing", "pending", "running"}:
        normalized_status = "processing"
    else:
        raise HTTPException(status_code=400, detail="status 只能是 paid/failed/processing")

    payment_id = str(req.external_payment_id or "").strip()
    existing_payment = extra.get(EXTERNAL_PAYMENT_KEY) if isinstance(extra.get(EXTERNAL_PAYMENT_KEY), dict) else {}
    if (
        existing_payment
        and str(existing_payment.get("claim_id") or "") == str(claim_id)
        and str(existing_payment.get("external_payment_id") or "") == payment_id
        and str(existing_payment.get("status") or "") == normalized_status
    ):
        return {
            "ok": True,
            "idempotent": True,
            "account_id": int(account.id or 0),
            "account_status": account.status,
            "claim": claim,
            "payment": existing_payment,
        }

    payment = {
        "status": normalized_status,
        "provider": str(req.provider or "external").strip() or "external",
        "external_payment_id": payment_id,
        "claim_id": str(claim_id),
        "message": str(req.message or "").strip(),
        "error_code": str(req.error_code or "").strip(),
        "written_at": _iso(now),
        "raw": req.raw if isinstance(req.raw, dict) else {},
    }
    claim.update({
        "status": normalized_status,
        "result_written_at": _iso(now),
        "external_payment_id": payment_id,
        "provider": payment["provider"],
    })

    if normalized_status == "paid":
        payment["paid_at"] = str(req.paid_at or "").strip() or _iso(now)
        claim["paid_at"] = payment["paid_at"]
        probe: dict[str, Any] = {}
        local_error = ""
        try:
            probe = _refresh_account_local_status(session, account, extra)
            extra = account.get_extra()
            claim = extra.get(EXTERNAL_CLAIM_KEY) if isinstance(extra.get(EXTERNAL_CLAIM_KEY), dict) else claim
            claim.update({
                "status": normalized_status,
                "result_written_at": _iso(now),
                "external_payment_id": payment_id,
                "provider": payment["provider"],
                "paid_at": payment["paid_at"],
            })
        except Exception as exc:
            local_error = f"本地订阅状态刷新失败: {_clean_text(exc, 700)}"
        if _paid_from_probe(account, probe):
            payment = _set_claim_paid(
                session,
                account,
                extra,
                claim,
                now=now,
                provider=payment["provider"],
                message=payment["message"] or "外部回写已支付，本地已确认订阅状态",
            )
        else:
            reason = local_error or "外部回写已支付，但本地订阅状态尚未显示 Plus/付费计划"
            payment = _set_claim_verify_pending(
                session,
                account,
                extra,
                claim,
                payment,
                now=now,
                reason=reason,
                probe=probe,
            )
        return {
            "ok": True,
            "idempotent": False,
            "account_id": int(account.id or 0),
            "account_status": account.status,
            "claim": claim,
            "payment": payment,
            "trigger_auth_capture": bool(req.trigger_auth_capture),
        }
    elif normalized_status == "failed":
        payment["failed_at"] = str(req.failed_at or "").strip() or _iso(now)
        claim["failed_at"] = payment["failed_at"]
        probe: dict[str, Any] = {}
        try:
            probe = _refresh_account_local_status(session, account, extra)
            extra = account.get_extra()
            claim = extra.get(EXTERNAL_CLAIM_KEY) if isinstance(extra.get(EXTERNAL_CLAIM_KEY), dict) else claim
            claim.update({
                "status": normalized_status,
                "result_written_at": _iso(now),
                "external_payment_id": payment_id,
                "provider": payment["provider"],
                "failed_at": payment["failed_at"],
            })
        except Exception:
            probe = {}
        if _paid_from_probe(account, probe):
            payment = _set_claim_paid(
                session,
                account,
                extra,
                claim,
                now=now,
                provider="local_verify",
                message="外部回写失败，但本地已确认订阅状态",
            )
            return {
                "ok": True,
                "idempotent": False,
                "account_id": int(account.id or 0),
                "account_status": account.status,
                "claim": claim,
                "payment": payment,
                "trigger_auth_capture": bool(req.trigger_auth_capture),
            }
        mark_payment_failed(account, reason="external_subscription_failed")
    else:
        mark_payment_pending(account, reason="external_subscription_processing")
    if normalized_status == "failed":
        claim["failed_at"] = payment["failed_at"]
        claim["last_error"] = payment["message"] or payment["error_code"]
        _mark_link_status(extra, status="available", reason=payment["message"] or payment["error_code"], now=now)
    else:
        _mark_link_status(extra, status="verify_pending", reason=payment["message"] or "外部回写处理中", now=now)

    extra[EXTERNAL_CLAIM_KEY] = claim
    extra[EXTERNAL_PAYMENT_KEY] = payment
    _update_claim_row_from_claim(session, claim, details_update={"payment": payment})
    _persist_account(session, account, extra)

    return {
        "ok": True,
        "idempotent": False,
        "account_id": int(account.id or 0),
        "account_status": account.status,
        "claim": claim,
        "payment": payment,
        "trigger_auth_capture": bool(req.trigger_auth_capture),
    }
