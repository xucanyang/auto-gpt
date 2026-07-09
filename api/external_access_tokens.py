"""External API for claiming live-checked ChatGPT access tokens."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from core.config_store import config_store
from core.db import AccountModel, ExternalAccessTokenClaimModel, get_session
from services.chatgpt_account_state import (
    apply_chatgpt_status_policy,
    classify_chatgpt_capabilities,
)

router = APIRouter(prefix="/external/access-tokens", tags=["external-access-tokens"])

EXTERNAL_AT_CLAIM_KEY = "external_access_token_claim"
EXTERNAL_AT_PAYMENT_KEY = "external_access_token_payment"
EXTERNAL_AT_PRECHECK_KEY = "external_access_token_precheck"

DEFAULT_LEASE_SECONDS = 86400
MAX_LEASE_SECONDS = 86400 * 7
DEFAULT_MAX_LIMIT = 50
HARD_MAX_LIMIT = 100
DEFAULT_PRECHECK_COOLDOWN_SECONDS = 600
SCAN_BATCH_SIZE = 50
ACTIVE_CLAIM_STATUSES = {"prechecking", "claimed", "processing"}
BLOCKING_EXTRA_STATUSES = ACTIVE_CLAIM_STATUSES | {"paid"}
VALID_AUTH_STATES = {"access_token_valid", "refresh_token_valid"}
FREE_PLAN = "free"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse_dt(value: Any) -> Optional[datetime]:
    text_value = str(value or "").strip()
    if not text_value:
        return None
    if text_value.endswith("Z"):
        text_value = text_value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text_value)
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
    text_value = str(value).strip().lower()
    if not text_value:
        return default
    return text_value in {"1", "true", "yes", "on", "enabled", "enable"}


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _clean_text(value: Any, limit: int = 500) -> str:
    text_value = str(value or "").strip()
    if len(text_value) > limit:
        return text_value[:limit] + "..."
    return text_value


def _now_id(prefix: str) -> str:
    return f"{prefix}_{_utcnow().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:10]}"


def _fingerprint_token(access_token: str) -> str:
    token = str(access_token or "").strip()
    if not token:
        return ""
    return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:32]


def _external_api_enabled() -> bool:
    return _parse_bool(config_store.get("external_access_token_api_enabled", "false"), default=False)


def _external_api_token() -> str:
    return str(config_store.get("external_access_token_api_token", "") or "").strip()


def _allow_refresh_default() -> bool:
    return _parse_bool(config_store.get("external_access_token_allow_refresh", "true"), default=True)


def _max_limit() -> int:
    configured = _safe_int(config_store.get("external_access_token_max_limit", ""), DEFAULT_MAX_LIMIT)
    return min(HARD_MAX_LIMIT, max(1, configured))


def _default_lease_seconds() -> int:
    configured = _safe_int(config_store.get("external_access_token_default_lease_seconds", ""), DEFAULT_LEASE_SECONDS)
    return min(MAX_LEASE_SECONDS, max(60, configured))


def _precheck_cooldown_seconds() -> int:
    configured = _safe_int(
        config_store.get("external_access_token_precheck_cooldown_seconds", ""),
        DEFAULT_PRECHECK_COOLDOWN_SECONDS,
    )
    return min(MAX_LEASE_SECONDS, max(60, configured))


def _require_external_api_token(authorization: str = Header(default="")) -> None:
    if not _external_api_enabled():
        raise HTTPException(status_code=403, detail="外部 AccessToken API 未启用")
    expected = _external_api_token()
    if not expected:
        raise HTTPException(status_code=403, detail="外部 AccessToken API token 未配置")
    token = str(authorization or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token or not secrets.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="外部 AccessToken API token 无效")


class ClaimAccessTokensRequest(BaseModel):
    consumer: str = ""
    limit: int = 1
    lease_seconds: int = 0
    allow_refresh: Optional[bool] = None


class AccessTokenResultRequest(BaseModel):
    status: str
    provider: str = "external"
    external_payment_id: str = ""
    paid_at: str = ""
    failed_at: str = ""
    message: str = ""
    error_code: str = ""
    raw: dict[str, Any] = {}


class ReleaseAccessTokenRequest(BaseModel):
    reason: str = ""


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


def _refresh_access_token(extra: dict[str, Any], *, proxy: str = "") -> dict[str, Any]:
    refresh_token = str(extra.get("refresh_token") or "").strip()
    if not refresh_token:
        return {"ok": False, "message": "账号缺少 refresh_token"}
    client_id = str(extra.get("client_id") or "app_EMoamEEZ73f0CkXaXp7hrann").strip()
    from core.proxy_utils import resolve_default_chatgpt_proxy
    from services.chatgpt_core.token_refresh import TokenRefreshManager

    try:
        proxy_url = resolve_default_chatgpt_proxy(proxy)
    except Exception as exc:
        return {"ok": False, "message": f"默认代理解析失败: {exc}"}
    result = TokenRefreshManager(proxy_url=proxy_url or None).refresh_by_oauth_token(
        refresh_token=refresh_token,
        client_id=client_id or None,
    )
    if not result.success or not str(result.access_token or "").strip():
        return {"ok": False, "message": str(result.error_message or "refresh_token 刷新失败")}
    return {
        "ok": True,
        "access_token": str(result.access_token or "").strip(),
        "refresh_token": str(result.refresh_token or refresh_token or "").strip(),
        "expires_at": result.expires_at.isoformat() if getattr(result, "expires_at", None) else "",
        "message": "refresh_token 刷新成功",
    }


def _probe_account_status(account: AccountModel, extra: dict[str, Any], *, proxy: str = "") -> dict[str, Any]:
    from services.chatgpt_core.status_probe import probe_local_chatgpt_status

    return probe_local_chatgpt_status(_account_probe_object(account, extra), proxy=proxy or "")


def _persist_account(session: Session, account: AccountModel, extra: dict[str, Any]) -> None:
    account.set_extra(extra)
    account.updated_at = _utcnow()
    session.add(account)
    session.commit()
    session.refresh(account)


def _refresh_account_local_status(session: Session, account: AccountModel, extra: dict[str, Any]) -> dict[str, Any]:
    probe = _probe_account_status(account, extra, proxy="")
    extra["chatgpt_local"] = probe
    capabilities = classify_chatgpt_capabilities(account, local_probe=probe)
    extra["chatgpt_capabilities"] = capabilities
    account.set_extra(extra)
    apply_chatgpt_status_policy(account, local_probe=probe)
    _persist_account(session, account, extra)
    return probe


def _active_claim_for_account(session: Session, account_id: int) -> Optional[ExternalAccessTokenClaimModel]:
    return session.exec(
        select(ExternalAccessTokenClaimModel)
        .where(ExternalAccessTokenClaimModel.account_id == int(account_id or 0))
        .where(ExternalAccessTokenClaimModel.status.in_(tuple(ACTIVE_CLAIM_STATUSES)))
        .order_by(ExternalAccessTokenClaimModel.id.desc())
    ).first()


def _find_claim_row(session: Session, claim_id: str) -> Optional[ExternalAccessTokenClaimModel]:
    claim_id = str(claim_id or "").strip()
    if not claim_id:
        return None
    return session.exec(
        select(ExternalAccessTokenClaimModel).where(ExternalAccessTokenClaimModel.claim_id == claim_id)
    ).first()


def _claim_row_to_dict(row: ExternalAccessTokenClaimModel) -> dict[str, Any]:
    details = row.get_details()
    claim: dict[str, Any] = {
        "claim_id": row.claim_id,
        "account_id": row.account_id,
        "email": row.email,
        "consumer": row.consumer,
        "status": row.status,
        "token_source": row.token_source,
        "token_fingerprint": row.token_fingerprint,
        "auth_state": row.auth_state,
        "subscription_plan": row.subscription_plan,
        "subscription_checked_at": row.subscription_checked_at,
        "lease_expires_at": row.lease_expires_at,
        "claimed_at": row.claimed_at,
        "prechecked_at": row.prechecked_at,
        "paid_at": row.paid_at,
        "failed_at": row.failed_at,
        "released_at": row.released_at,
        "result_written_at": row.result_written_at,
        "provider": row.provider,
        "external_payment_id": row.external_payment_id,
        "message": row.message,
        "error_code": row.error_code,
        "last_error": row.last_error,
        "source": "external_access_token_claims",
    }
    if isinstance(details, dict):
        for key in ("attempt", "lease_seconds", "preflight", "payment"):
            if key in details:
                claim[key] = details.get(key)
    return claim


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
        "email",
        "token_source",
        "token_fingerprint",
        "auth_state",
        "subscription_plan",
        "subscription_checked_at",
        "lease_expires_at",
        "claimed_at",
        "prechecked_at",
        "paid_at",
        "failed_at",
        "released_at",
        "result_written_at",
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
    row.set_details(details)
    row.updated_at = _utcnow()
    session.add(row)


def _reserve_access_token_claim(
    session: Session,
    account: AccountModel,
    *,
    consumer: str,
    now: datetime,
    lease_seconds: int,
    attempt: int,
) -> dict[str, Any]:
    if _active_claim_for_account(session, int(account.id or 0)) is not None:
        return {}
    claim_id = _now_id("atclaim")
    row = ExternalAccessTokenClaimModel(
        claim_id=claim_id,
        account_id=int(account.id or 0),
        email=str(account.email or ""),
        consumer=consumer,
        status="prechecking",
        claimed_at=_iso(now),
    )
    row.set_details({"attempt": max(1, int(attempt or 1)), "lease_seconds": lease_seconds})
    session.add(row)
    try:
        session.commit()
        session.refresh(row)
    except IntegrityError:
        session.rollback()
        return {}
    return _claim_row_to_dict(row)


def _expire_stale_claims(session: Session, now: datetime) -> int:
    result = session.execute(
        text(
            """
            UPDATE external_access_token_claims
            SET status = 'lease_expired',
                released_at = :now_iso,
                last_error = 'AccessToken 领取租约超时，已自动释放',
                updated_at = :updated_at
            WHERE status = 'claimed'
              AND lease_expires_at != ''
              AND lease_expires_at <= :now_iso
            """
        ),
        {"updated_at": now, "now_iso": _iso(now)},
    )
    session.commit()
    return int(getattr(result, "rowcount", 0) or 0)


def _claim_is_blocking(claim: dict[str, Any], now: datetime) -> bool:
    status = str(claim.get("status") or "").strip().lower()
    if status == "paid":
        return True
    if status not in ACTIVE_CLAIM_STATUSES:
        return False
    expires_at = _parse_dt(claim.get("lease_expires_at"))
    if status == "claimed" and expires_at and expires_at <= now:
        return False
    return True


def _precheck_blocked(extra: dict[str, Any], now: datetime) -> bool:
    precheck = extra.get(EXTERNAL_AT_PRECHECK_KEY) if isinstance(extra.get(EXTERNAL_AT_PRECHECK_KEY), dict) else {}
    retry_after_at = _parse_dt(precheck.get("retry_after_at"))
    return bool(retry_after_at and retry_after_at > now)


def _locally_sendable(account: AccountModel, extra: dict[str, Any], now: datetime) -> bool:
    status = str(account.status or "").strip().lower()
    if status in {"subscribed", "invalid", "banned", "account_deactivated"}:
        return False
    claim = extra.get(EXTERNAL_AT_CLAIM_KEY) if isinstance(extra.get(EXTERNAL_AT_CLAIM_KEY), dict) else {}
    if _claim_is_blocking(claim, now):
        return False
    if _precheck_blocked(extra, now):
        return False
    return bool(str(extra.get("access_token") or account.token or "").strip() or str(extra.get("refresh_token") or "").strip())


def _set_precheck_result(extra: dict[str, Any], *, status: str, reason: str, now: datetime) -> None:
    retry_after_at = ""
    if status == "precheck_failed":
        retry_after_at = _iso(now + timedelta(seconds=_precheck_cooldown_seconds()))
    extra[EXTERNAL_AT_PRECHECK_KEY] = {
        "last_checked_at": _iso(now),
        "last_status": status,
        "last_error": _clean_text(reason, 800),
        "retry_after_at": retry_after_at,
    }


def _preflight_access_token(
    session: Session,
    account: AccountModel,
    *,
    allow_refresh: bool,
) -> dict[str, Any]:
    now = _utcnow()
    extra = account.get_extra()
    token_source = "access_token"
    access_token = str(extra.get("access_token") or account.token or "").strip()

    if allow_refresh and str(extra.get("refresh_token") or "").strip():
        refresh = _refresh_access_token(extra, proxy="")
        if bool(refresh.get("ok")) and str(refresh.get("access_token") or "").strip():
            access_token = str(refresh.get("access_token") or "").strip()
            token_source = "refresh_token"
            extra["access_token"] = access_token
            account.token = access_token
            if str(refresh.get("refresh_token") or "").strip():
                extra["refresh_token"] = str(refresh.get("refresh_token") or "").strip()
            if str(refresh.get("expires_at") or "").strip():
                extra["access_token_expires_at"] = str(refresh.get("expires_at") or "").strip()
        elif not access_token:
            reason = str(refresh.get("message") or "refresh_token 刷新失败且缺少可回退 access_token")
            _set_precheck_result(extra, status="invalid_token", reason=reason, now=now)
            _persist_account(session, account, extra)
            return {"ok_to_send": False, "status": "invalid_token", "reason": reason, "probe": {"refresh": refresh}}

    if not access_token:
        reason = "账号缺少 access_token"
        _set_precheck_result(extra, status="missing_token", reason=reason, now=now)
        _persist_account(session, account, extra)
        return {"ok_to_send": False, "status": "missing_token", "reason": reason, "probe": {}}

    extra["access_token"] = access_token
    account.token = access_token
    probe = _probe_account_status(account, extra, proxy="")
    extra["chatgpt_local"] = probe
    capabilities = classify_chatgpt_capabilities(account, local_probe=probe)
    extra["chatgpt_capabilities"] = capabilities

    auth = probe.get("auth") if isinstance(probe.get("auth"), dict) else {}
    subscription = probe.get("subscription") if isinstance(probe.get("subscription"), dict) else {}
    auth_state = str(auth.get("state") or "").strip()
    subscription_plan = str(subscription.get("plan") or capabilities.get("subscription_plan") or "unknown").strip().lower() or "unknown"
    checked_at = str(subscription.get("checked_at") or probe.get("checked_at") or _iso(now)).strip()

    if auth_state not in VALID_AUTH_STATES:
        status = "invalid_token" if "invalid" in auth_state or auth_state == "unauthorized" else "precheck_failed"
        reason = str(auth.get("message") or f"access_token live 校验未通过: {auth_state or 'unknown'}")
        _set_precheck_result(extra, status=status, reason=reason, now=now)
        if status == "invalid_token":
            apply_chatgpt_status_policy(account, local_probe=probe)
        _persist_account(session, account, extra)
        return {
            "ok_to_send": False,
            "status": status,
            "reason": reason,
            "auth_state": auth_state,
            "subscription_plan": subscription_plan,
            "subscription_checked_at": checked_at,
            "probe": probe,
        }

    if bool(capabilities.get("has_paid_subscription")) or subscription_plan != FREE_PLAN:
        status = "subscribed" if bool(capabilities.get("has_paid_subscription")) else "precheck_failed"
        reason = (
            f"live 探测显示账号已订阅: {subscription_plan}"
            if status == "subscribed"
            else f"live 探测未能确认账号为 free: {subscription_plan}"
        )
        _set_precheck_result(extra, status=status, reason=reason, now=now)
        if status == "subscribed":
            apply_chatgpt_status_policy(account, local_probe=probe)
        _persist_account(session, account, extra)
        return {
            "ok_to_send": False,
            "status": status,
            "reason": reason,
            "auth_state": auth_state,
            "subscription_plan": subscription_plan,
            "subscription_checked_at": checked_at,
            "probe": probe,
        }

    _set_precheck_result(extra, status="claimed", reason="access_token live 校验有效且账号为 free", now=now)
    _persist_account(session, account, extra)
    return {
        "ok_to_send": True,
        "access_token": access_token,
        "token_source": token_source,
        "token_fingerprint": _fingerprint_token(access_token),
        "auth_state": auth_state,
        "subscription_plan": subscription_plan,
        "subscription_checked_at": checked_at,
        "probe": probe,
    }


def _serialize_claimed_item(account: AccountModel, claim: dict[str, Any], access_token: str) -> dict[str, Any]:
    return {
        "claim_id": claim.get("claim_id") or "",
        "account_id": int(account.id or 0),
        "email": account.email,
        "access_token": access_token,
        "token_source": claim.get("token_source") or "",
        "auth_state": claim.get("auth_state") or "",
        "subscription_plan": claim.get("subscription_plan") or "",
        "subscription_checked_at": claim.get("subscription_checked_at") or "",
        "consumer": claim.get("consumer") or "",
        "claim_status": claim.get("status") or "",
        "lease_expires_at": claim.get("lease_expires_at") or "",
    }


def _find_claim(session: Session, claim_id: str) -> tuple[AccountModel, dict[str, Any], dict[str, Any]]:
    row = _find_claim_row(session, claim_id)
    if row is None:
        raise HTTPException(status_code=404, detail="AccessToken claim 不存在")
    account = session.get(AccountModel, int(row.account_id or 0))
    if account is None:
        raise HTTPException(status_code=404, detail="claim 对应账号不存在")
    return account, account.get_extra(), _claim_row_to_dict(row)


@router.post("/claim", dependencies=[Depends(_require_external_api_token)])
def claim_access_tokens(req: ClaimAccessTokensRequest, session: Session = Depends(get_session)):
    now = _utcnow()
    _expire_stale_claims(session, now)
    limit = min(_max_limit(), max(1, _safe_int(req.limit, 1)))
    lease_seconds = min(MAX_LEASE_SECONDS, max(60, _safe_int(req.lease_seconds, _default_lease_seconds()) or _default_lease_seconds()))
    lease_expires_at = now + timedelta(seconds=lease_seconds)
    consumer = str(req.consumer or "").strip()[:120]
    allow_refresh = _allow_refresh_default() if req.allow_refresh is None else bool(req.allow_refresh)
    claimed: list[dict[str, Any]] = []
    sent_fingerprints: set[str] = set()
    last_seen_id = 0

    while len(claimed) < limit:
        rows = session.exec(
            select(AccountModel)
            .where(AccountModel.platform == "chatgpt")
            .where(AccountModel.status.notin_(["subscribed", "invalid", "banned", "account_deactivated"]))
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
            if not _locally_sendable(account, extra, now):
                continue
            current_claim = extra.get(EXTERNAL_AT_CLAIM_KEY) if isinstance(extra.get(EXTERNAL_AT_CLAIM_KEY), dict) else {}
            claim = _reserve_access_token_claim(
                session,
                account,
                consumer=consumer,
                now=now,
                lease_seconds=lease_seconds,
                attempt=_safe_int(current_claim.get("attempt"), 0) + 1,
            )
            if not claim:
                continue

            preflight = _preflight_access_token(session, account, allow_refresh=allow_refresh)
            if not bool(preflight.get("ok_to_send")):
                status = str(preflight.get("status") or "precheck_failed")
                reason = str(preflight.get("reason") or "AccessToken live 预检未通过")
                claim.update({
                    "status": status,
                    "prechecked_at": _iso(_utcnow()),
                    "result_written_at": _iso(_utcnow()),
                    "last_error": _clean_text(reason, 800),
                    "auth_state": str(preflight.get("auth_state") or ""),
                    "subscription_plan": str(preflight.get("subscription_plan") or ""),
                    "subscription_checked_at": str(preflight.get("subscription_checked_at") or ""),
                })
                extra = account.get_extra()
                extra[EXTERNAL_AT_CLAIM_KEY] = claim
                _update_claim_row_from_claim(session, claim, details_update={"preflight": preflight.get("probe") or {}, "preflight_error": reason})
                _persist_account(session, account, extra)
                continue

            fp = str(preflight.get("token_fingerprint") or "")
            if fp and fp in sent_fingerprints:
                reason = "本轮领取已发送过相同 access_token，跳过重复发送"
                claim.update({
                    "status": "duplicate_in_claim_round",
                    "prechecked_at": _iso(_utcnow()),
                    "result_written_at": _iso(_utcnow()),
                    "token_fingerprint": fp,
                    "last_error": reason,
                })
                extra = account.get_extra()
                extra[EXTERNAL_AT_CLAIM_KEY] = claim
                _update_claim_row_from_claim(session, claim, details_update={"preflight_error": reason})
                _persist_account(session, account, extra)
                continue

            sent_fingerprints.add(fp)
            claim.update({
                "status": "claimed",
                "lease_expires_at": _iso(lease_expires_at),
                "prechecked_at": _iso(_utcnow()),
                "token_source": str(preflight.get("token_source") or "access_token"),
                "token_fingerprint": fp,
                "auth_state": str(preflight.get("auth_state") or ""),
                "subscription_plan": str(preflight.get("subscription_plan") or "free"),
                "subscription_checked_at": str(preflight.get("subscription_checked_at") or ""),
                "last_error": "",
            })
            extra = account.get_extra()
            extra[EXTERNAL_AT_CLAIM_KEY] = claim
            _update_claim_row_from_claim(session, claim, details_update={"preflight": preflight.get("probe") or {}})
            _persist_account(session, account, extra)
            claimed.append(_serialize_claimed_item(account, claim, str(preflight.get("access_token") or "")))

    return {
        "ok": True,
        "count": len(claimed),
        "lease_seconds": lease_seconds,
        "lease_expires_at": _iso(lease_expires_at),
        "items": claimed,
    }


@router.get("/{claim_id}", dependencies=[Depends(_require_external_api_token)])
def get_access_token_claim(claim_id: str, session: Session = Depends(get_session)):
    account, extra, claim = _find_claim(session, claim_id)
    payment = extra.get(EXTERNAL_AT_PAYMENT_KEY) if isinstance(extra.get(EXTERNAL_AT_PAYMENT_KEY), dict) else {}
    public_claim = {k: v for k, v in claim.items() if k != "access_token"}
    return {
        "ok": True,
        "account_id": int(account.id or 0),
        "email": account.email,
        "account_status": account.status,
        "claim": public_claim,
        "payment": payment,
    }


@router.post("/{claim_id}/release", dependencies=[Depends(_require_external_api_token)])
def release_access_token_claim(
    claim_id: str,
    req: ReleaseAccessTokenRequest,
    session: Session = Depends(get_session),
):
    account, extra, claim = _find_claim(session, claim_id)
    status = str(claim.get("status") or "").strip().lower()
    if status == "paid":
        return {"ok": True, "released": False, "status": "paid", "message": "claim 已支付，不能释放"}
    claim.update({
        "status": "released",
        "released_at": _iso(_utcnow()),
        "last_error": "",
        "message": str(req.reason or "").strip(),
    })
    extra[EXTERNAL_AT_CLAIM_KEY] = claim
    _update_claim_row_from_claim(session, claim, details_update={"release_reason": str(req.reason or "").strip()})
    _persist_account(session, account, extra)
    return {"ok": True, "released": True, "claim": claim}


@router.post("/{claim_id}/result", dependencies=[Depends(_require_external_api_token)])
def write_access_token_result(
    claim_id: str,
    req: AccessTokenResultRequest,
    session: Session = Depends(get_session),
):
    account, extra, claim = _find_claim(session, claim_id)
    now = _utcnow()
    status = str(req.status or "").strip().lower()
    if status in {"success", "succeeded", "paid", "subscribed"}:
        normalized_status = "paid"
    elif status in {"failed", "fail", "error", "cancelled", "canceled"}:
        normalized_status = "failed"
    else:
        raise HTTPException(status_code=400, detail="status 只能是 paid/failed")

    existing_payment = extra.get(EXTERNAL_AT_PAYMENT_KEY) if isinstance(extra.get(EXTERNAL_AT_PAYMENT_KEY), dict) else {}
    payment_id = str(req.external_payment_id or "").strip()
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
            "email": account.email,
            "account_status": account.status,
            "claim": claim,
            "payment": existing_payment,
        }

    claim.update({
        "status": normalized_status,
        "result_written_at": _iso(now),
        "provider": str(req.provider or "external").strip() or "external",
        "external_payment_id": payment_id,
        "message": _clean_text(req.message, 800),
        "error_code": _clean_text(req.error_code, 200),
    })
    if normalized_status == "paid":
        claim["paid_at"] = str(req.paid_at or "").strip() or _iso(now)
    else:
        claim["failed_at"] = str(req.failed_at or "").strip() or _iso(now)
        claim["last_error"] = _clean_text(req.message or req.error_code or "外部服务回写支付失败", 800)

    payment = {
        "status": normalized_status,
        "provider": claim["provider"],
        "external_payment_id": payment_id,
        "claim_id": str(claim_id),
        "message": claim.get("message") or "",
        "error_code": claim.get("error_code") or "",
        "written_at": _iso(now),
        "paid_at": claim.get("paid_at") or "",
        "failed_at": claim.get("failed_at") or "",
        "raw": req.raw if isinstance(req.raw, dict) else {},
    }
    extra[EXTERNAL_AT_CLAIM_KEY] = claim
    extra[EXTERNAL_AT_PAYMENT_KEY] = payment
    _update_claim_row_from_claim(session, claim, details_update={"payment": payment})

    refresh_result: dict[str, Any] = {}
    if normalized_status == "paid":
        account.set_extra(extra)
        try:
            refresh_result = _refresh_account_local_status(session, account, extra)
            extra = account.get_extra()
        except Exception as exc:
            refresh_result = {"ok": False, "error": _clean_text(exc, 800)}
            extra[EXTERNAL_AT_CLAIM_KEY] = claim
            extra[EXTERNAL_AT_PAYMENT_KEY] = payment
            _persist_account(session, account, extra)
    else:
        # 支付失败只记录 claim/payment 回写，不改变账号订阅/本地状态。
        _persist_account(session, account, extra)

    return {
        "ok": True,
        "account_id": int(account.id or 0),
        "email": account.email,
        "account_status": account.status,
        "claim": claim,
        "payment": payment,
        "local_refresh": refresh_result if normalized_status == "paid" else {},
    }
