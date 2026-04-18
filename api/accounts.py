from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from fastapi.responses import StreamingResponse
from sqlmodel import Session, select, func
from pydantic import BaseModel
from core.db import AccountModel, PendingBusinessInviteModel, get_session
from services.team_lite import team_lite_service
from typing import Any, Optional
from datetime import datetime, timezone
import io, csv, json, logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/accounts", tags=["accounts"])


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _safe_str(value: Any) -> str:
    return str(value or "").strip()


def _serialize_account(account: AccountModel, *, team_invite_source: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    data = account.model_dump(mode="json") if hasattr(account, "model_dump") else account.dict()
    if team_invite_source:
        data["team_invite_source"] = team_invite_source
    return data


def _build_team_invite_sources(accounts: list[AccountModel], session: Session) -> dict[int, dict[str, Any]]:
    chatgpt_accounts = [account for account in accounts if account.platform == "chatgpt" and int(account.id or 0) > 0]
    if not chatgpt_accounts:
        return {}

    account_ids = [int(account.id or 0) for account in chatgpt_accounts]
    pending_rows = session.exec(
        select(PendingBusinessInviteModel).where(PendingBusinessInviteModel.account_id.in_(account_ids))
    ).all()
    pending_by_account = {
        int(row.account_id or 0): row
        for row in pending_rows
        if int(row.account_id or 0) > 0
    }

    sources: dict[int, dict[str, Any]] = {}
    team_ids: list[int] = []
    seen_team_ids: set[int] = set()

    for account in chatgpt_accounts:
        account_id = int(account.id or 0)
        extra = account.get_extra()
        pending_payload = dict(extra.get("chatgpt_pending_business_invite") or {})
        pending_row = pending_by_account.get(account_id)

        team_id = _safe_int(getattr(pending_row, "team_id", 0) if pending_row else pending_payload.get("team_id"))
        invite_status = _safe_str(getattr(pending_row, "status", "") if pending_row else pending_payload.get("status"))
        workspace_scope = _safe_str(extra.get("chatgpt_workspace_scope"))
        team_name = _safe_str(getattr(pending_row, "team_name", "") if pending_row else pending_payload.get("team_name"))
        invited_at = _safe_str(getattr(pending_row, "invited_at", "") if pending_row else pending_payload.get("invite_sent_at") or pending_payload.get("invited_at"))
        joined_at = _safe_str(getattr(pending_row, "joined_at", "") if pending_row else pending_payload.get("joined_at"))
        removed_from_team_at = _safe_str(extra.get("chatgpt_team_invite_removed_at"))

        if team_id <= 0 and not invite_status and workspace_scope not in {"business", "pending_activation"}:
            continue

        source = {
            "team_id": team_id,
            "team_name": team_name,
            "invite_status": invite_status,
            "workspace_scope": workspace_scope,
            "invited_at": invited_at,
            "joined_at": joined_at,
            "removed_from_team_at": removed_from_team_at,
            "removable": team_id > 0 and not removed_from_team_at,
        }
        sources[account_id] = source
        if team_id > 0 and team_id not in seen_team_ids:
            seen_team_ids.add(team_id)
            team_ids.append(team_id)

    if not sources:
        return {}

    team_briefs = team_lite_service.get_team_db_briefs(team_ids)
    for source in sources.values():
        team_id = _safe_int(source.get("team_id"))
        if team_id <= 0:
            continue
        team_brief = team_briefs.get(team_id) or {}
        primary_account = dict(team_brief.get("primary_account") or {})
        source.update(
            {
                "team_email": _safe_str(team_brief.get("email")),
                "team_account_id": _safe_str(team_brief.get("account_id")),
                "team_status": _safe_str(team_brief.get("status")),
                "primary_account_id": _safe_str(primary_account.get("account_id")),
                "primary_account_name": _safe_str(primary_account.get("account_name")),
            }
        )
        if not source.get("team_name"):
            source["team_name"] = _safe_str(team_brief.get("team_name"))

    return sources


class AccountCreate(BaseModel):
    platform: str
    email: str
    password: str
    status: str = "registered"
    token: str = ""
    cashier_url: str = ""


class AccountUpdate(BaseModel):
    status: Optional[str] = None
    token: Optional[str] = None
    cashier_url: Optional[str] = None


class ImportRequest(BaseModel):
    platform: str
    lines: list[str]


class BatchDeleteRequest(BaseModel):
    ids: list[int]


class BatchDeleteByFilterRequest(BaseModel):
    platform: Optional[str] = None
    status: Optional[str] = None
    email: Optional[str] = None


@router.get("")
def list_accounts(
    platform: Optional[str] = None,
    status: Optional[str] = None,
    email: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
    session: Session = Depends(get_session),
):
    q = select(AccountModel)
    if platform:
        q = q.where(AccountModel.platform == platform)
    if status:
        q = q.where(AccountModel.status == status)
    if email:
        q = q.where(AccountModel.email.contains(email))
    total = int(session.exec(select(func.count()).select_from(q.subquery())).one())
    items = session.exec(q.offset((page - 1) * page_size).limit(page_size)).all()
    team_invite_sources = _build_team_invite_sources(items, session)
    return {
        "total": total,
        "page": page,
        "items": [
            _serialize_account(item, team_invite_source=team_invite_sources.get(int(item.id or 0)))
            for item in items
        ],
    }


@router.post("")
def create_account(body: AccountCreate, session: Session = Depends(get_session)):
    acc = AccountModel(
        platform=body.platform,
        email=body.email,
        password=body.password,
        status=body.status,
        token=body.token,
        cashier_url=body.cashier_url,
    )
    session.add(acc)
    session.commit()
    session.refresh(acc)
    return acc


@router.get("/stats")
def get_stats(session: Session = Depends(get_session)):
    """统计各平台账号数量和状态分布"""
    accounts = session.exec(select(AccountModel)).all()
    platforms: dict = {}
    statuses: dict = {}
    for acc in accounts:
        platforms[acc.platform] = platforms.get(acc.platform, 0) + 1
        statuses[acc.status] = statuses.get(acc.status, 0) + 1
    return {"total": len(accounts), "by_platform": platforms, "by_status": statuses}


@router.get("/export")
def export_accounts(
    platform: Optional[str] = None,
    status: Optional[str] = None,
    session: Session = Depends(get_session),
):
    q = select(AccountModel)
    if platform:
        q = q.where(AccountModel.platform == platform)
    if status:
        q = q.where(AccountModel.status == status)
    accounts = session.exec(q).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["platform", "email", "password", "user_id", "region",
                     "status", "cashier_url", "created_at"])
    for acc in accounts:
        writer.writerow([acc.platform, acc.email, acc.password, acc.user_id,
                         acc.region, acc.status, acc.cashier_url,
                         acc.created_at.strftime("%Y-%m-%d %H:%M:%S")])
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=accounts.csv"}
    )


@router.post("/import")
def import_accounts(
    body: ImportRequest,
    session: Session = Depends(get_session),
):
    """批量导入，每行格式: email password [extra]"""
    created = 0
    for line in body.lines:
        parts = line.strip().split()
        if len(parts) < 2:
            continue
        email, password = parts[0], parts[1]
        extra = parts[2] if len(parts) > 2 else ""
        if extra:
            try:
                json.loads(extra)
            except (json.JSONDecodeError, ValueError):
                extra = "{}"
        else:
            extra = "{}"
        acc = AccountModel(platform=body.platform, email=email,
                           password=password, extra_json=extra)
        session.add(acc)
        created += 1
    session.commit()
    return {"created": created}


@router.post("/batch-delete")
def batch_delete_accounts(
    body: BatchDeleteRequest,
    session: Session = Depends(get_session)
):
    """批量删除账号"""
    if not body.ids:
        raise HTTPException(400, "账号 ID 列表不能为空")
    
    if len(body.ids) > 1000:
        raise HTTPException(400, "单次最多删除 1000 个账号")
    
    deleted_count = 0
    not_found_ids = []
    
    try:
        for account_id in body.ids:
            acc = session.get(AccountModel, account_id)
            if acc:
                session.delete(acc)
                deleted_count += 1
            else:
                not_found_ids.append(account_id)
        
        session.commit()
        logger.info(f"批量删除成功: {deleted_count} 个账号")
        
        return {
            "deleted": deleted_count,
            "not_found": not_found_ids,
            "total_requested": len(body.ids)
        }
    except Exception as e:
        session.rollback()
        logger.exception("批量删除失败")
        raise HTTPException(500, f"批量删除失败: {str(e)}")


@router.post("/check-all")
def check_all_accounts(platform: Optional[str] = None,
                       background_tasks: BackgroundTasks = None):
    from core.scheduler import scheduler
    background_tasks.add_task(scheduler.check_accounts_valid, platform)
    return {"message": "批量检测任务已启动"}


@router.post("/batch-delete-by-filter")
def batch_delete_accounts_by_filter(
    body: BatchDeleteByFilterRequest,
    session: Session = Depends(get_session),
):
    """按筛选条件批量删除账号。至少需要一个筛选条件。"""
    if not any([body.platform, body.status, body.email]):
        raise HTTPException(400, "至少需要一个筛选条件")

    q = select(AccountModel)
    if body.platform:
        q = q.where(AccountModel.platform == body.platform)
    if body.status:
        q = q.where(AccountModel.status == body.status)
    if body.email:
        q = q.where(AccountModel.email.contains(body.email))

    accounts = session.exec(q).all()
    deleted_count = 0
    deleted_ids: list[int] = []

    try:
        for acc in accounts:
            if acc.id is None:
                continue
            deleted_ids.append(acc.id)
            session.delete(acc)
            deleted_count += 1

        session.commit()
        logger.info("按筛选条件批量删除成功: %s 个账号", deleted_count)
        filters = body.model_dump() if hasattr(body, "model_dump") else body.dict()
        return {
            "deleted": deleted_count,
            "deleted_ids": deleted_ids,
            "filters": filters,
        }
    except Exception as e:
        session.rollback()
        logger.exception("按筛选条件批量删除失败")
        raise HTTPException(500, f"按筛选条件批量删除失败: {str(e)}")


@router.get("/{account_id}")
def get_account(account_id: int, session: Session = Depends(get_session)):
    acc = session.get(AccountModel, account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    team_invite_source = _build_team_invite_sources([acc], session).get(int(acc.id or 0))
    return _serialize_account(acc, team_invite_source=team_invite_source)


@router.post("/{account_id}/chatgpt-team-remove")
def remove_chatgpt_team_member(account_id: int, session: Session = Depends(get_session)):
    acc = session.get(AccountModel, account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    if acc.platform != "chatgpt":
        raise HTTPException(400, "只有 ChatGPT 账号支持移除队伍")

    team_invite_source = _build_team_invite_sources([acc], session).get(int(acc.id or 0))
    if not team_invite_source:
        raise HTTPException(400, "当前账号没有 Team Invite 来源信息")

    team_id = _safe_int(team_invite_source.get("team_id"))
    if team_id <= 0:
        raise HTTPException(400, "当前账号未关联可操作的 Team")

    email = _safe_str(acc.email).lower()
    if not email:
        raise HTTPException(400, "当前账号缺少邮箱")

    try:
        member_result = team_lite_service.check_member(team_id, email, force=True)
    except Exception as exc:
        raise HTTPException(400, f"检查 Team 成员失败: {exc}") from exc

    member = dict(member_result.get("member") or {})
    member_status = _safe_str(member_result.get("status") or member.get("status")).lower()
    matched = bool(member_result.get("matched"))

    try:
        if matched and member_status == "joined":
            role = _safe_str(member.get("role")).lower()
            if role == "account-owner":
                raise HTTPException(400, "这是 Team 母号，不能直接从自己的 Team 中移除")
            user_id = _safe_str(member.get("user_id"))
            if not user_id:
                raise HTTPException(400, "命中了已加入成员，但缺少 user_id，无法删除")
            result = team_lite_service.delete_member(team_id, user_id)
            action = "delete_member"
            message_text = "已从 Team 中删除成员"
        elif matched and member_status == "invited":
            result = team_lite_service.revoke_invite(team_id, email)
            action = "revoke_invite"
            message_text = "已撤销 Team 邀请"
        elif _safe_str(team_invite_source.get("invite_status")) and _safe_str(team_invite_source.get("invite_status")) != "completed":
            result = team_lite_service.revoke_invite(team_id, email)
            action = "revoke_invite"
            message_text = "已按 pending invite 撤销 Team 邀请"
        else:
            # 如果 Team 已经没有该账号，视为“已移除”，记录本地移除时间以便前端更新按钮态
            action = "noop"
            result = None
            message_text = "Team 中未找到该账号，可能已经被移除"
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(400, f"移除队伍失败: {exc}") from exc

    extra = acc.get_extra()
    extra["chatgpt_team_invite_removed_at"] = datetime.now(timezone.utc).isoformat()
    acc.set_extra(extra)
    acc.updated_at = datetime.now(timezone.utc)
    session.add(acc)
    session.commit()
    session.refresh(acc)

    if action == "noop" and not team_invite_source.get("removed_from_team_at"):
        # 为了让前端“移除队伍”按钮立即消失，未匹配到成员时也直接写本地移除时间
        team_invite_source["removed_from_team_at"] = extra["chatgpt_team_invite_removed_at"]
        team_invite_source["removable"] = False

    refreshed_source = _build_team_invite_sources([acc], session).get(int(acc.id or 0)) or team_invite_source
    if not action == "noop":
        # 明确刷新成功执行动作后再同步一次，避免因列表查询延迟导致前端刷新后又出现按钮
        refreshed_source = _build_team_invite_sources([acc], session).get(int(acc.id or 0)) or team_invite_source
    return {
        "ok": True,
        "action": action,
        "message": message_text,
        "result": result,
        "team_invite_source": refreshed_source,
    }


@router.patch("/{account_id}")
def update_account(account_id: int, body: AccountUpdate,
                   session: Session = Depends(get_session)):
    acc = session.get(AccountModel, account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    if body.status is not None:
        acc.status = body.status
    if body.token is not None:
        acc.token = body.token
    if body.cashier_url is not None:
        acc.cashier_url = body.cashier_url
    acc.updated_at = datetime.now(timezone.utc)
    session.add(acc)
    session.commit()
    session.refresh(acc)
    return acc


@router.delete("/{account_id}")
def delete_account(account_id: int, session: Session = Depends(get_session)):
    acc = session.get(AccountModel, account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    session.delete(acc)
    session.commit()
    return {"ok": True}


@router.post("/{account_id}/check")
def check_account(account_id: int, background_tasks: BackgroundTasks,
                  session: Session = Depends(get_session)):
    acc = session.get(AccountModel, account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    background_tasks.add_task(_do_check, account_id)
    return {"message": "检测任务已启动"}


def _do_check(account_id: int):
    from core.db import engine
    from sqlmodel import Session
    with Session(engine) as s:
        acc = s.get(AccountModel, account_id)
    if acc:
        from core.base_platform import Account, RegisterConfig
        from core.registry import get
        try:
            PlatformCls = get(acc.platform)
            plugin = PlatformCls(config=RegisterConfig())
            obj = Account(platform=acc.platform, email=acc.email,
                         password=acc.password, user_id=acc.user_id,
                         region=acc.region, token=acc.token,
                         extra=json.loads(acc.extra_json or "{}"))
            valid = plugin.check_valid(obj)
            with Session(engine) as s:
                a = s.get(AccountModel, account_id)
                if a:
                    if a.platform != "chatgpt":
                        a.status = a.status if valid else "invalid"
                    a.updated_at = datetime.now(timezone.utc)
                    s.add(a)
                    s.commit()
        except Exception:
            logger.exception("检测账号 %s 时出错", account_id)
