from __future__ import annotations

from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from core.base_platform import Account, AccountStatus
from core.db import AccountModel, engine
from services.external_apps import install, list_status, start, start_all, stop, stop_all
from services.chatgpt_sync import backfill_chatgpt_account_to_cpa, get_cliproxy_sync_state
from services.sub2api_sync import backfill_chatgpt_account_to_sub2api, get_sub2api_sync_state

router = APIRouter(prefix="/integrations", tags=["integrations"])


class BackfillRequest(BaseModel):
    platforms: list[str] = Field(default_factory=lambda: ["chatgpt"])
    account_ids: list[int] = Field(default_factory=list)
    destination: str = "cliproxyapi"
    pending_only: bool = False
    status: Optional[str] = None
    email: Optional[str] = None


def _to_account(model: AccountModel) -> Account:
    return Account(
        platform=model.platform,
        email=model.email,
        password=model.password,
        user_id=model.user_id,
        region=model.region,
        token=model.token,
        status=AccountStatus(model.status),
        extra=model.get_extra(),
    )


@router.get("/services")
def get_services():
    return {"items": list_status()}


@router.post("/services/start-all")
def start_all_services():
    return {"items": start_all()}


@router.post("/services/stop-all")
def stop_all_services():
    return {"items": stop_all()}


@router.post("/services/{name}/start")
def start_service(name: str):
    return start(name)


@router.post("/services/{name}/install")
def install_service(name: str):
    return install(name)


@router.post("/services/{name}/stop")
def stop_service(name: str):
    return stop(name)


@router.post("/backfill")
def backfill_integrations(body: BackfillRequest):
    summary = {"total": 0, "success": 0, "failed": 0, "skipped": 0, "items": []}
    targets = set(body.platforms or [])
    destination = str(body.destination or "cliproxyapi").strip().lower() or "cliproxyapi"

    with Session(engine) as s:
        q = select(AccountModel)
        if body.account_ids:
            q = q.where(AccountModel.id.in_(body.account_ids))
            if targets:
                q = q.where(AccountModel.platform.in_(targets))
        elif targets:
            q = q.where(AccountModel.platform.in_(targets))
        else:
            return summary

        if body.status:
            q = q.where(AccountModel.status == body.status)
        if body.email:
            q = q.where(AccountModel.email.contains(body.email))

        rows = s.exec(q).all()
        if body.pending_only:
            def _is_pending_target(row: AccountModel) -> bool:
                if row.platform != "chatgpt":
                    return False
                if destination == "sub2api":
                    state = get_sub2api_sync_state(row)
                else:
                    state = get_cliproxy_sync_state(row)
                if not state:
                    return True
                return str(state.get("remote_state") or "").strip().lower() in {"not_found", "cross_workspace_only"}

            rows = [row for row in rows if _is_pending_target(row)]

        for row in rows:
            item = {"platform": row.platform, "email": row.email, "results": []}
            try:
                results = []
                if row.platform == "chatgpt":
                    if destination == "sub2api":
                        outcome = backfill_chatgpt_account_to_sub2api(row, session=s, commit=True)
                        default_name = "Sub2API"
                    else:
                        outcome = backfill_chatgpt_account_to_cpa(row, session=s, commit=True)
                        default_name = "CLIProxyAPI"

                    ok = bool(outcome.get("ok"))
                    skipped = bool(outcome.get("skipped"))
                    results.extend(outcome.get("results") or [])
                    if not results:
                        results.append({"name": default_name, "ok": ok, "msg": outcome.get("message", "")})
                    if skipped:
                        summary["skipped"] += 1
                    elif ok:
                        summary["success"] += 1
                    else:
                        summary["failed"] += 1

                if not results:
                    item["results"].append({"name": "skip", "ok": False, "msg": "未配置对应导入目标"})
                    summary["failed"] += 1
                else:
                    item["results"] = results
            except Exception as e:
                s.rollback()
                item["results"].append({"name": "error", "ok": False, "msg": str(e)})
                summary["failed"] += 1
            summary["items"].append(item)
            summary["total"] += 1

    return summary
