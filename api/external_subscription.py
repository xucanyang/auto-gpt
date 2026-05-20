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
from sqlmodel import Session, select

from core.config_store import config_store
from core.db import AccountModel, get_session
from services.chatgpt_account_state import (
    apply_chatgpt_status_policy,
    classify_chatgpt_capabilities,
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
DEFAULT_VERIFY_AFTER_SECONDS = 180
SCAN_BATCH_SIZE = 50
DUE_VERIFICATION_LIMIT = 25
SENDABLE_CURRENCY = "USD"
TERMINAL_LINK_STATUSES = {
    "paid",
    "already_paid",
    "invalid",
    "not_usd",
    "amount_not_zero",
    "timeout_unpaid",
}
ACTIVE_LINK_STATUSES = {"leased", "verify_pending"}
_VERIFY_TIMERS: set[str] = set()
_VERIFY_TIMERS_LOCK = threading.Lock()


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


def _clean_text(value: Any, limit: int = 500) -> str:
    text = str(value or "").strip()
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def _is_zero_amount(value: Any) -> bool:
    text = str(value if value is not None else "").strip()
    if not text:
        return False
    try:
        return Decimal(text) == 0
    except (InvalidOperation, ValueError):
        return text in {"0", "0.0", "0.00"}


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
        "proxy": params["proxy"],
        "source": str(cached.get("source") or "").strip(),
        "created_at": str(cached.get("created_at") or "").strip(),
    }
    for key in (
        "checkout_amount",
        "checkout_amount_is_zero",
        "link_status",
        "link_status_reason",
        "last_preflight_at",
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
    if status in ACTIVE_LINK_STATUSES:
        expires_at = _parse_dt(link.get("lease_expires_at"))
        verify_after_at = _parse_dt(link.get("verify_after_at"))
        if expires_at and expires_at > now:
            return True
        if verify_after_at and verify_after_at > now:
            return True
    return False


def _claim_matches_filters(link: dict[str, Any], req: ClaimSubscriptionLinksRequest) -> bool:
    if req.plan and normalize_payment_link_plan(req.plan) != normalize_payment_link_plan(link.get("plan")):
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
        "plan": link.get("plan") or "plus",
        "country": link.get("country") or "",
        "currency": link.get("currency") or "",
        "claim_id": claim.get("claim_id") or "",
        "claim_status": claim.get("status") or "",
        "consumer": claim.get("consumer") or "",
        "lease_expires_at": claim.get("lease_expires_at") or "",
        "verify_after_at": claim.get("verify_after_at") or "",
    }
    for key in ("checkout_amount", "checkout_amount_is_zero", "source", "created_at", "billing"):
        if key in link:
            item[key] = link.get(key)
    return item


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
    capabilities = classify_chatgpt_capabilities(account, local_probe=probe if isinstance(probe, dict) else None)
    return bool(capabilities.get("has_paid_subscription")) or str(account.status or "").strip().lower() == "subscribed"


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
    return link


def _preflight_subscription_link(account: AccountModel, link: dict[str, Any]) -> dict[str, Any]:
    from services.chatgpt_core.gopay_flow import probe_chatgpt_checkout_amount

    checkout_url = str(link.get("url") or "").strip()
    if not checkout_url:
        return {"ok_to_send": False, "link_status": "invalid", "reason": "订阅链接为空", "probe": {}}
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
        return {"ok_to_send": False, "link_status": status, "reason": message, "probe": {}, "error": message}

    currency = str(probe.get("currency") or link.get("currency") or "").strip().upper()
    amount = probe.get("amount")
    amount_is_zero = bool(probe.get("amount_is_zero")) or _is_zero_amount(probe.get("amount_text")) or _is_zero_amount(amount)
    if currency != SENDABLE_CURRENCY:
        return {
            "ok_to_send": False,
            "link_status": "not_usd",
            "reason": f"账单货币不是 USD: {currency or 'unknown'}",
            "probe": probe,
        }
    if not amount_is_zero:
        return {
            "ok_to_send": False,
            "link_status": "amount_not_zero",
            "reason": f"账单金额不是 0: {probe.get('amount_text') or amount or 'unknown'}",
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
    try:
        from services.chatgpt_core.pending_business_invites import upsert_pending_subscription_auth_from_account

        upsert_pending_subscription_auth_from_account(
            account,
            checkout_url=str(link.get("url") or ""),
            plan=str(link.get("plan") or "plus"),
            country=str(link.get("country") or ""),
            currency=str(link.get("currency") or ""),
        )
    except Exception:
        pass


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
    _persist_account(session, account, extra)
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
            message="180s 本地复核已确认订阅状态",
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
        extra = account.get_extra()
        claim = extra.get(EXTERNAL_CLAIM_KEY) if isinstance(extra.get(EXTERNAL_CLAIM_KEY), dict) else claim
        payment = _set_claim_paid(
            session,
            account,
            extra,
            claim,
            now=now,
            provider="local_checkout_verify",
            message="180s checkout 复核显示该订阅链接已支付",
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

    if preflight and not bool(preflight.get("ok_to_send")) and preflight_status not in {"precheck_failed"}:
        failure_status = preflight_status
        failure_reason = str(preflight.get("reason") or "180s checkout 复核未通过")
    else:
        failure_status = "unverified" if status_probe_error else "timeout_unpaid"
        failure_reason = status_probe_error or "180s 本地复核未确认 paid/subscribed"
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
            _verify_subscription_claim_now(session, claim_id)
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
    timer = threading.Timer(delay, _verify_subscription_claim_after_delay, args=(claim_id,))
    timer.daemon = True
    timer.start()


def _run_due_local_verifications(session: Session, now: datetime) -> int:
    rows = session.exec(
        select(AccountModel)
        .where(AccountModel.platform == "chatgpt")
        .order_by(AccountModel.id.asc())
        .limit(1000)
    ).all()
    checked = 0
    for account in rows:
        if checked >= DUE_VERIFICATION_LIMIT:
            break
        extra = account.get_extra()
        claim = extra.get(EXTERNAL_CLAIM_KEY) if isinstance(extra.get(EXTERNAL_CLAIM_KEY), dict) else {}
        if str(claim.get("status") or "").strip().lower() not in {"claimed", "processing"}:
            continue
        verify_after_at = _parse_dt(claim.get("verify_after_at"))
        if not verify_after_at or verify_after_at > now:
            continue
        claim_id = str(claim.get("claim_id") or "").strip()
        if not claim_id:
            continue
        _verify_subscription_claim_now(session, claim_id)
        checked += 1
    return checked


@router.post("/claim", dependencies=[Depends(_require_external_api_token)])
def claim_subscription_links(req: ClaimSubscriptionLinksRequest, session: Session = Depends(get_session)):
    now = _utcnow()
    _run_due_local_verifications(session, now)
    if not _claim_requires_usd_zero(req):
        raise HTTPException(status_code=400, detail="外部订阅链接只允许抽取 USD 0 元账单")
    limit = min(MAX_LIMIT, max(1, _safe_int(req.limit, 1)))
    lease_seconds = min(MAX_LEASE_SECONDS, max(60, _safe_int(req.lease_seconds, DEFAULT_LEASE_SECONDS)))
    lease_expires_at = now + timedelta(seconds=lease_seconds)
    verify_after_at = now + timedelta(seconds=DEFAULT_VERIFY_AFTER_SECONDS)
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

            preflight = _preflight_subscription_link(account, link)
            preflight_probe = preflight.get("probe") if isinstance(preflight.get("probe"), dict) else {}
            if not bool(preflight.get("ok_to_send")):
                status = str(preflight.get("link_status") or "precheck_failed")
                reason = str(preflight.get("reason") or "订阅链接本地预检未通过")
                _mark_link_status(
                    extra,
                    status=status,
                    reason=reason,
                    probe=preflight_probe,
                    now=now,
                    last_preflight_at=_iso(now),
                )
                _persist_account(session, account, extra)
                try:
                    _refresh_account_local_status(session, account, account.get_extra())
                except Exception:
                    pass
                continue

            claim_id = _now_id("subclaim")
            claim = {
                "claim_id": claim_id,
                "consumer": consumer,
                "status": "claimed",
                "claimed_at": _iso(now),
                "lease_expires_at": _iso(lease_expires_at),
                "verify_after_at": _iso(verify_after_at),
                "attempt": _safe_int(current_claim.get("attempt"), 0) + 1,
                "payment_link": link.get("url") or "",
                "plan": link.get("plan") or "plus",
                "country": link.get("country") or "",
                "currency": SENDABLE_CURRENCY,
            }
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
            _persist_account(session, account, extra)
            _upsert_pending_subscription_auth(account, updated_link)
            _schedule_subscription_verification(claim_id, verify_after_at)
            claimed.append(_serialize_claimed_item(account, _payment_link_from_account(account), claim))

    return {
        "ok": True,
        "count": len(claimed),
        "lease_seconds": lease_seconds,
        "lease_expires_at": _iso(lease_expires_at),
        "verify_after_seconds": DEFAULT_VERIFY_AFTER_SECONDS,
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
        _mark_link_status(extra, status="paid", reason=payment["message"] or "外部回写已支付", now=now)
    elif normalized_status == "failed":
        claim["failed_at"] = payment["failed_at"]
        claim["last_error"] = payment["message"] or payment["error_code"]
        _mark_link_status(extra, status="timeout_unpaid", reason=payment["message"] or payment["error_code"], now=now)
    else:
        _mark_link_status(extra, status="verify_pending", reason=payment["message"] or "外部回写处理中", now=now)

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
