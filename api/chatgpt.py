"""ChatGPT 专用功能 API"""
from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session
from pydantic import BaseModel
from typing import Optional
from core.db import AccountModel, get_session
from services.chatgpt_account_state import apply_chatgpt_status_policy
import json, sys


router = APIRouter(prefix="/chatgpt", tags=["chatgpt"])

COUNTRIES = ["SG", "US", "TR", "JP", "HK", "GB", "AU", "CA", "IN", "BR", "MX"]


class UploadRequest(BaseModel):
    account_ids: list[int]
    cpa_api_url: Optional[str] = None
    cpa_api_token: Optional[str] = None


def _get_account(account_id: int, session: Session) -> AccountModel:
    acc = session.get(AccountModel, account_id)
    if not acc or acc.platform != "chatgpt":
        raise HTTPException(404, "账号不存在")
    return acc


def _to_codex_account(acc: AccountModel):
    """转换为 codex-register 的 Account 对象（duck-typing）"""
    extra = acc.get_extra()

    class _Acc:
        pass

    a = _Acc()
    a.email = acc.email
    a.access_token = extra.get("access_token") or acc.token
    a.refresh_token = extra.get("refresh_token", "")
    a.id_token = extra.get("id_token", "")
    a.session_token = extra.get("session_token", "")
    a.client_id = extra.get("client_id", "app_EMoamEEZ73f0CkXaXp7hrann")
    a.cookies = extra.get("cookies", "")
    a.user_id = acc.user_id
    return a


def _persist_local_probe(acc: AccountModel, probe: dict, session: Session) -> None:
    extra = acc.get_extra()
    extra["chatgpt_local"] = probe
    acc.set_extra(extra)
    apply_chatgpt_status_policy(acc, local_probe=probe)
    from datetime import datetime
    acc.updated_at = datetime.utcnow()
    session.add(acc)
    session.commit()


# ── Token 刷新 ──────────────────────────────────────────────
@router.post("/{account_id}/refresh-token")
def refresh_token(account_id: int, proxy: Optional[str] = None,
                  session: Session = Depends(get_session)):
    acc = _get_account(account_id, session)
    codex_acc = _to_codex_account(acc)

    from platforms.chatgpt.token_refresh import TokenRefreshManager
    manager = TokenRefreshManager(proxy_url=proxy)
    result = manager.refresh_account(codex_acc)

    if result.success:
        extra = acc.get_extra()
        extra["access_token"] = result.access_token
        if result.refresh_token:
            extra["refresh_token"] = result.refresh_token
        acc.set_extra(extra)
        acc.token = result.access_token
        from datetime import datetime
        acc.updated_at = datetime.utcnow()
        session.add(acc)
        session.commit()
        return {"ok": True, "access_token": result.access_token[:40] + "..."}
    raise HTTPException(400, result.error_message)


# ── 生成支付链接 ────────────────────────────────────────────
class PaymentReq(BaseModel):
    plan: str = "plus"  # plus | team
    country: str = "SG"
    proxy: Optional[str] = None
    workspace_name: str = "MyTeam"
    seat_quantity: int = 5
    price_interval: str = "month"


@router.post("/{account_id}/payment-link")
def generate_payment_link(account_id: int, req: PaymentReq,
                          session: Session = Depends(get_session)):
    acc = _get_account(account_id, session)
    codex_acc = _to_codex_account(acc)

    from platforms.chatgpt.payment import generate_plus_link, generate_team_link
    if req.plan == "plus":
        url = generate_plus_link(codex_acc, proxy=req.proxy, country=req.country)
    else:
        url = generate_team_link(
            codex_acc, workspace_name=req.workspace_name,
            price_interval=req.price_interval, seat_quantity=req.seat_quantity,
            proxy=req.proxy, country=req.country
        )
    return {"url": url, "plan": req.plan, "country": req.country}


# ── 检查订阅状态 ────────────────────────────────────────────
@router.get("/{account_id}/subscription")
def check_subscription(account_id: int, proxy: Optional[str] = None,
                       session: Session = Depends(get_session)):
    acc = _get_account(account_id, session)
    codex_acc = _to_codex_account(acc)

    from platforms.chatgpt.status_probe import probe_local_chatgpt_status

    probe = probe_local_chatgpt_status(codex_acc, proxy=proxy)
    _persist_local_probe(acc, probe, session)
    return {
        "email": acc.email,
        "subscription": probe.get("subscription", {}).get("plan", "unknown"),
        "probe": probe,
    }


@router.post("/{account_id}/probe-local")
def probe_local_status(account_id: int, proxy: Optional[str] = None,
                       session: Session = Depends(get_session)):
    acc = _get_account(account_id, session)
    codex_acc = _to_codex_account(acc)

    from platforms.chatgpt.status_probe import probe_local_chatgpt_status

    probe = probe_local_chatgpt_status(codex_acc, proxy=proxy)
    _persist_local_probe(acc, probe, session)
    return {"ok": True, "email": acc.email, "probe": probe}


class PendingBusinessInviteActivateReq(BaseModel):
    pass


class PendingBusinessInviteBatchActivateReq(BaseModel):
    invite_ids: list[int] = []
    limit: int = 200


class PendingBusinessInviteAbandonReq(BaseModel):
    pass


@router.get("/pending-business-invites")
def list_pending_business_invites(status: Optional[str] = None, limit: int = 200):
    from platforms.chatgpt.pending_business_invites import list_pending_invites

    items = list_pending_invites(status=(status or "").strip() or None, limit=limit)
    return {
        "ok": True,
        "count": len(items),
        "items": items,
    }


@router.post("/pending-business-invites/{invite_id}/activate")
def activate_pending_business_invite(invite_id: int, req: PendingBusinessInviteActivateReq):
    from platforms.chatgpt.pending_business_invites import activate_pending_invite

    try:
        return activate_pending_invite(invite_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/pending-business-invites/batch-activate")
def activate_pending_business_invites(req: PendingBusinessInviteBatchActivateReq):
    from platforms.chatgpt.pending_business_invites import activate_pending_invites

    try:
        return activate_pending_invites(invite_ids=req.invite_ids or None, limit=req.limit)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/pending-business-invites/{invite_id}/abandon")
def abandon_pending_business_invite(invite_id: int, req: PendingBusinessInviteAbandonReq):
    from platforms.chatgpt.pending_business_invites import abandon_pending_invite

    try:
        return abandon_pending_invite(invite_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ── CPA 上传 ────────────────────────────────────────────────
class CpaUploadReq(BaseModel):
    api_url: str
    api_key: str = ""


@router.post("/{account_id}/upload-cpa")
def upload_cpa(account_id: int, req: CpaUploadReq,
               session: Session = Depends(get_session)):
    acc = _get_account(account_id, session)
    codex_acc = _to_codex_account(acc)

    from platforms.chatgpt.cpa_upload import upload_to_cpa, generate_token_json
    token_data = generate_token_json(codex_acc)
    ok, msg = upload_to_cpa(token_data, api_url=req.api_url, api_key=req.api_key)
    return {"ok": ok, "message": msg}


class Sub2ApiUploadReq(BaseModel):
    api_url: str = ""
    api_key: str = ""


@router.post("/{account_id}/upload-sub2api")
def upload_sub2api(account_id: int, req: Sub2ApiUploadReq,
                   session: Session = Depends(get_session)):
    acc = _get_account(account_id, session)

    from services.sub2api_sync import backfill_chatgpt_account_to_sub2api

    outcome = backfill_chatgpt_account_to_sub2api(acc, session=session, commit=True)
    return {
        "ok": bool(outcome.get("ok")),
        "message": str(outcome.get("message") or ""),
        "results": outcome.get("results") or [],
    }
