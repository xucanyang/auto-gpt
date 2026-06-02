from fastapi import APIRouter
from pydantic import BaseModel

from services.tempmail_archive_cleanup import get_status, run_once


router = APIRouter(prefix="/tempmail-archive", tags=["tempmail-archive"])


class TempMailArchiveRunRequest(BaseModel):
    force: bool = True
    ignore_active_tasks: bool = False
    delete: bool = True


@router.get("/status")
def tempmail_archive_status():
    return get_status()


@router.post("/run")
def run_tempmail_archive_cleanup(body: TempMailArchiveRunRequest | None = None):
    payload = body or TempMailArchiveRunRequest()
    return run_once(
        force=payload.force,
        ignore_active_tasks=payload.ignore_active_tasks,
        delete=payload.delete,
    )
