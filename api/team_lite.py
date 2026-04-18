from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from services.team_lite import team_lite_service

router = APIRouter(prefix="/team-lite", tags=["team-lite"])


class TeamLiteSettingsUpdate(BaseModel):
    team_manager_db_path: str = ""


class TeamInviteRequest(BaseModel):
    email: str


class TeamMemberDeleteRequest(BaseModel):
    user_id: str


class TeamBulkActionRequest(BaseModel):
    ids: list[int] = Field(default_factory=list)


class TeamImportRequest(BaseModel):
    import_type: str = "batch"
    access_token: str | None = None
    refresh_token: str | None = None
    session_token: str | None = None
    client_id: str | None = None
    email: str | None = None
    account_id: str | None = None
    content: str | None = None


class TeamUpdateRequest(BaseModel):
    email: str | None = None
    account_id: str | None = None
    access_token: str | None = None
    refresh_token: str | None = None
    session_token: str | None = None
    client_id: str | None = None
    max_members: int | None = None
    team_name: str | None = None
    status: str | None = None


@router.get("/settings")
def get_team_lite_settings():
    return team_lite_service.get_settings()


@router.put("/settings")
def update_team_lite_settings(body: TeamLiteSettingsUpdate):
    return team_lite_service.update_settings(body.model_dump())


@router.get("/teams")
def list_teams(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    search: str = Query(default=""),
    status: str = Query(default=""),
):
    try:
        return team_lite_service.list_teams(
            page=page,
            page_size=page_size,
            search=search,
            status=status,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/teams/import")
def import_teams(body: TeamImportRequest):
    try:
        return team_lite_service.import_teams(body.model_dump())
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/teams/import-from-account/{account_row_id}")
def import_team_from_account(account_row_id: int):
    try:
        return team_lite_service.import_team_from_account(account_row_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/teams/live-sync")
def sync_live_member_counts(body: TeamBulkActionRequest):
    try:
        return team_lite_service.sync_live_member_counts(body.ids)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/teams/{team_id}/info")
def get_team_info(team_id: int):
    try:
        return team_lite_service.get_team_info(team_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/teams/{team_id}/update")
def update_team(team_id: int, body: TeamUpdateRequest):
    try:
        return team_lite_service.update_team(team_id, body.model_dump())
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/teams/{team_id}/refresh")
def refresh_team(team_id: int):
    try:
        return team_lite_service.refresh_team(team_id, force=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/teams/batch-refresh")
def batch_refresh_teams(body: TeamBulkActionRequest):
    try:
        return team_lite_service.batch_refresh_teams(body.ids)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/teams/{team_id}/delete")
def delete_team(team_id: int):
    try:
        return team_lite_service.delete_team(team_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/teams/batch-delete")
def batch_delete_teams(body: TeamBulkActionRequest):
    try:
        return team_lite_service.batch_delete_teams(body.ids)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/teams/{team_id}/members")
def get_team_members(team_id: int):
    try:
        return team_lite_service.get_team_members(team_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/teams/{team_id}/invite")
def invite_team_member(team_id: int, body: TeamInviteRequest):
    try:
        return team_lite_service.invite_member(team_id, body.email)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/teams/{team_id}/invites/revoke")
def revoke_team_invite(team_id: int, body: TeamInviteRequest):
    try:
        return team_lite_service.revoke_invite(team_id, body.email)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/teams/{team_id}/members/delete")
def delete_team_member(team_id: int, body: TeamMemberDeleteRequest):
    try:
        return team_lite_service.delete_member(team_id, body.user_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/teams/{team_id}/members/check")
def check_team_member(
    team_id: int,
    email: str = Query(default=""),
    force: bool = Query(default=False),
):
    try:
        return team_lite_service.check_member(team_id, email, force=force)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
