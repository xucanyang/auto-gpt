"""External API for claiming cached ChatGPT subscription links."""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from core.config_store import config_store
from core.db import AccountModel, get_session
from services.chatgpt_account_state import (
    mark_payment_failed,
    mark_payment_pending,
    mark_payment_succeeded,
)
from services.chatgpt_core.payment_link_cache import (
    normalize_payment_link_params,
    normalize_payment_link_plan,
)


router = APIRouter(prefix="/external/subscription-links", tags=["external-subscription-links"])

EXTERNAL_CLAIM_KEY = "external_subscription_claim"
EXTERNAL_PAYMENT_KEY = "external_subscription_payment"
MAX_LIMIT = 100
DEFAULT_LEASE_SECONDS = 900
MAX_LEASE_SECONDS = 86400


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


def _now_id(prefix: str) -> str:
    return f"{prefix}_{_utcnow().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:10]}"


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
    url = str(
        cached.get("url")
        or cached.get("checkout_url")
        or cached.get("cashier_url")
        or account.cashier_url
        or extra.get("cashier_url")
        or ""
    ).strip()
    if not url:
        return {}
    params = normalize_payment_link_params(cached)
    payload: dict[str, Any] = {
        "url": url,
        "plan": normalize_payment_link_plan(cached.get("plan")),
        "country": params["country"],
        "currency": params["currency"],
        "source": str(cached.get("source") or "").strip(),
        "created_at": str(cached.get("created_at") or "").strip(),
    }
    for key in ("checkout_amount", "checkout_amount_is_zero"):
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


def _claim_matches_filters(link: dict[str, Any], req: ClaimSubscriptionLinksRequest) -> bool:
    if req.plan and normalize_payment_link_plan(req.plan) != normalize_payment_link_plan(link.get("plan")):
        return False
    if req.country or req.currency:
        expected = normalize_payment_link_params({
            "plan": link.get("plan") or "plus",
            "country": req.country or link.get("country"),
            "currency": req.currency or link.get("currency"),
        })
        actual = normalize_payment_link_params(link)
        if req.country and expected["country"] != actual["country"]:
            return False
        if req.currency and expected["currency"] != actual["currency"]:
            return False
    return True


def _serialize_claimed_item(account: AccountModel, link: dict[str, Any], claim: dict[str, Any]) -> dict[str, Any]:
    item = {
        "account_id": int(account.id or 0),
        "email": account.email,
        "status": account.status,
        "payment_link": link.get("url") or "",
        "plan": link.get("plan") or "plus",
        "country": link.get("country") or "",
        "currency": link.get("currency") or "",
        "claim_id": claim.get("claim_id") or "",
        "claim_status": claim.get("status") or "",
        "consumer": claim.get("consumer") or "",
        "lease_expires_at": claim.get("lease_expires_at") or "",
    }
    for key in ("checkout_amount", "checkout_amount_is_zero", "source", "created_at", "billing"):
        if key in link:
            item[key] = link.get(key)
    return item


def _find_claim(session: Session, claim_id: str) -> tuple[AccountModel, dict[str, Any], dict[str, Any]]:
    claim_id = str(claim_id or "").strip()
    if not claim_id:
        raise HTTPException(status_code=404, detail="claim 不存在")
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


@router.post("/claim", dependencies=[Depends(_require_external_api_token)])
def claim_subscription_links(req: ClaimSubscriptionLinksRequest, session: Session = Depends(get_session)):
    now = _utcnow()
    limit = min(MAX_LIMIT, max(1, _safe_int(req.limit, 1)))
    lease_seconds = min(MAX_LEASE_SECONDS, max(60, _safe_int(req.lease_seconds, DEFAULT_LEASE_SECONDS)))
    lease_expires_at = now + timedelta(seconds=lease_seconds)
    consumer = str(req.consumer or "").strip()[:120]
    claimed: list[dict[str, Any]] = []

    rows = session.exec(
        select(AccountModel)
        .where(AccountModel.platform == "chatgpt")
        .where(AccountModel.status.notin_(["subscribed", "invalid"]))
        .order_by(AccountModel.id.asc())
        .limit(max(limit * 5, 20))
    ).all()

    for account in rows:
        if len(claimed) >= limit:
            break
        extra = account.get_extra()
        current_claim = extra.get(EXTERNAL_CLAIM_KEY) if isinstance(extra.get(EXTERNAL_CLAIM_KEY), dict) else {}
        if _claim_is_active(current_claim, now):
            continue
        link = _payment_link_from_account(account)
        if not link or not _claim_matches_filters(link, req):
            continue
        claim = {
            "claim_id": _now_id("subclaim"),
            "consumer": consumer,
            "status": "claimed",
            "claimed_at": _iso(now),
            "lease_expires_at": _iso(lease_expires_at),
            "attempt": _safe_int(current_claim.get("attempt"), 0) + 1,
            "payment_link": link.get("url") or "",
            "plan": link.get("plan") or "plus",
            "country": link.get("country") or "",
            "currency": link.get("currency") or "",
        }
        extra[EXTERNAL_CLAIM_KEY] = claim
        mark_payment_pending(account, reason="external_subscription_claimed")
        _persist_account(session, account, extra)
        claimed.append(_serialize_claimed_item(account, link, claim))

    return {
        "ok": True,
        "count": len(claimed),
        "lease_seconds": lease_seconds,
        "lease_expires_at": _iso(lease_expires_at),
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

    if normalized_status == "paid":
        payment["paid_at"] = str(req.paid_at or "").strip() or _iso(now)
        mark_payment_succeeded(account, reason="external_subscription_paid")
    elif normalized_status == "failed":
        payment["failed_at"] = str(req.failed_at or "").strip() or _iso(now)
        mark_payment_failed(account, reason="external_subscription_failed")
    else:
        mark_payment_pending(account, reason="external_subscription_processing")

    claim.update({
        "status": normalized_status,
        "result_written_at": _iso(now),
        "external_payment_id": payment_id,
        "provider": payment["provider"],
    })
    if normalized_status == "paid":
        claim["paid_at"] = payment["paid_at"]
    elif normalized_status == "failed":
        claim["failed_at"] = payment["failed_at"]
        claim["last_error"] = payment["message"] or payment["error_code"]

    extra[EXTERNAL_CLAIM_KEY] = claim
    extra[EXTERNAL_PAYMENT_KEY] = payment
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
