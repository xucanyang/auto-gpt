from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, Field
from sqlmodel import Session

from core.db import get_session
from api.auth import require_auth
from services.delivery_cards import service

router = APIRouter(prefix="/delivery-cards", tags=["delivery-cards"])
public_router = APIRouter(prefix="/public/delivery-cards", tags=["public-delivery-cards"])


class CreateBatchRequest(BaseModel):
    name: str = ""
    sku_code: str
    count: int = Field(default=1, ge=1, le=5000)
    strict_stock_check: bool = True
    expires_at: str = ""
    note: str = ""


class RedeemRequest(BaseModel):
    code: str
    consumer: str = ""
    request_id: str = ""


class SettingsUpdateRequest(BaseModel):
    data: dict = Field(default_factory=dict)


class DisableRequest(BaseModel):
    reason: str = ""


class TokenTestRequest(BaseModel):
    token: str = ""


class LookupCardRequest(BaseModel):
    code: str


def _client_ip(request: Request) -> str:
    forwarded = str(request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if forwarded:
        return forwarded[:120]
    return str(request.client.host if request.client else "")[:120]


@router.get("/summary", dependencies=[Depends(require_auth)])
def admin_delivery_summary(session: Session = Depends(get_session)):
    return service.admin_summary(session)


@router.get("/skus", dependencies=[Depends(require_auth)])
def list_delivery_skus(session: Session = Depends(get_session)):
    return {"items": [service.serialize_sku(item) for item in service.get_skus(session)]}


@router.get("/settings", dependencies=[Depends(require_auth)])
def get_delivery_settings():
    return service.get_delivery_settings()


@router.put("/settings", dependencies=[Depends(require_auth)])
def update_delivery_settings(body: SettingsUpdateRequest):
    return service.update_delivery_settings(body.data)


@router.post("/settings/token/rotate", dependencies=[Depends(require_auth)])
def rotate_delivery_token():
    return service.rotate_api_token()


@router.post("/settings/test-auth", dependencies=[Depends(require_auth)])
def test_delivery_auth(authorization: str = Header(default="")):
    # 兼容旧接口。注意这里的 Authorization 会和管理端 JWT 冲突，新页面使用 /settings/test-token。
    ok = service.verify_candidate_api_token(authorization)
    return {"ok": ok}


@router.post("/settings/test-token", dependencies=[Depends(require_auth)])
def test_delivery_token(body: TokenTestRequest):
    return {"ok": service.verify_candidate_api_token(body.token)}


@router.post("/batches", dependencies=[Depends(require_auth)])
def create_delivery_batch(body: CreateBatchRequest, session: Session = Depends(get_session)):
    return service.create_batch(
        session,
        name=body.name,
        sku_code=body.sku_code,
        count=body.count,
        strict_stock_check=body.strict_stock_check,
        expires_at=body.expires_at,
        note=body.note,
    )


@router.get("/batches", dependencies=[Depends(require_auth)])
def list_delivery_batches(sku_code: str = "", session: Session = Depends(get_session)):
    return service.list_batches(session, sku_code=sku_code)


@router.post("/cards/lookup", dependencies=[Depends(require_auth)])
def lookup_delivery_card(body: LookupCardRequest, session: Session = Depends(get_session)):
    return service.lookup_card_by_code(session, body.code)


@router.get("/consistency", dependencies=[Depends(require_auth)])
def check_delivery_consistency(session: Session = Depends(get_session)):
    return service.scan_consistency(session)


@router.post("/consistency/repair", dependencies=[Depends(require_auth)])
def repair_delivery_consistency(session: Session = Depends(get_session)):
    return service.repair_consistency(session)


@router.get("/cards", dependencies=[Depends(require_auth)])
def list_delivery_cards(
    sku_code: str = "",
    status: str = "",
    batch_id: int = 0,
    search: str = "",
    limit: int = 200,
    session: Session = Depends(get_session),
):
    return service.list_cards(session, sku_code=sku_code, status=status, batch_id=batch_id, search=search, limit=limit)


@router.get("/cards/{card_id}", dependencies=[Depends(require_auth)])
def get_delivery_card(card_id: int, session: Session = Depends(get_session)):
    return service.card_detail(session, card_id)


@router.post("/cards/{card_id}/disable", dependencies=[Depends(require_auth)])
def disable_delivery_card(card_id: int, body: DisableRequest, session: Session = Depends(get_session)):
    return service.set_card_status(session, card_id, service.STATUS_DISABLED, reason=body.reason)


@router.post("/cards/{card_id}/enable", dependencies=[Depends(require_auth)])
def enable_delivery_card(card_id: int, session: Session = Depends(get_session)):
    return service.set_card_status(session, card_id, service.STATUS_UNUSED, reason="启用卡密")




@router.get("/events", dependencies=[Depends(require_auth)])
def list_delivery_events(
    sku_code: str = "",
    result: str = "",
    failure_code: str = "",
    consumer: str = "",
    request_id: str = "",
    limit: int = 200,
    session: Session = Depends(get_session),
):
    return service.list_events(session, sku_code=sku_code, result=result, failure_code=failure_code, consumer=consumer, request_id=request_id, limit=limit)


@router.get("/api-logs", dependencies=[Depends(require_auth)])
def list_delivery_api_logs(
    sku_code: str = "",
    result: str = "",
    error_code: str = "",
    consumer: str = "",
    request_id: str = "",
    limit: int = 200,
    session: Session = Depends(get_session),
):
    return service.list_api_logs(session, sku_code=sku_code, result=result, error_code=error_code, consumer=consumer, request_id=request_id, limit=limit)


@public_router.post("/redeem")
def redeem_delivery_card(
    body: RedeemRequest,
    request: Request,
    authorization: str = Header(default=""),
    idempotency_key: str = Header(default=""),
):
    return service.redeem_card(
        code=body.code,
        consumer=body.consumer,
        request_id=body.request_id,
        idempotency_key=idempotency_key or body.request_id,
        authorization=authorization,
        client_ip=_client_ip(request),
        user_agent=str(request.headers.get("user-agent") or ""),
    )
