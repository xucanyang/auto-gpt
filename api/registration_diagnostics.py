"""Authenticated registration diagnostic artifact management."""

from __future__ import annotations

import mimetypes
import os
from pathlib import Path
import re
import shutil

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from services.chatgpt_core.registration_diagnostics import (
    build_registration_diagnostic_bundle,
    delete_registration_diagnostic,
    diagnostic_limits,
    diagnostics_root,
    list_registration_diagnostics,
    prune_registration_diagnostics,
    registration_diagnostic_path,
    registration_diagnostics_summary,
    set_registration_diagnostic_pinned,
)


router = APIRouter(prefix="/tasks", tags=["registration-diagnostics"])
_TASK_ID_RE = re.compile(r"^task_[A-Za-z0-9_.-]{1,120}$")
_ALLOWED_FILES = {
    "manifest.json",
    "diagnosis.json",
    "trace.zip",
    "network.har.zip",
    "protocol.har.zip",
    "events.jsonl",
    "mailbox.jsonl",
    "browser-console.jsonl",
    "key-http-responses.jsonl",
    "runtime.json",
    "final-state.json",
    "final-page.html",
    "final-page.png",
    "video.webm",
    "video.zip",
}
_DOWNLOAD_HEADERS = {
    "Cache-Control": "no-store, private",
    "Pragma": "no-cache",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "sandbox",
}


class DiagnosticPinRequest(BaseModel):
    pinned: bool = True


def _task_id(value: str) -> str:
    normalized = str(value or "").strip()
    if not _TASK_ID_RE.fullmatch(normalized):
        raise HTTPException(400, "任务 ID 无效")
    return normalized


def _translate_error(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(404, str(exc).strip("'"))
    if isinstance(exc, FileNotFoundError):
        return HTTPException(404, str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(409, str(exc))
    return HTTPException(500, f"注册诊断操作失败: {exc}")


@router.get("/registration-diagnostics/capacity")
def registration_diagnostics_capacity():
    root = diagnostics_root()
    usage = shutil.disk_usage(root)
    return {
        "ok": True,
        "instance_id": str(os.getenv("APP_INSTANCE_ID") or ""),
        "limits": diagnostic_limits(),
        "disk": {
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
        },
    }


@router.get("/{task_id}/diagnostics")
def list_task_registration_diagnostics(task_id: str):
    normalized = _task_id(task_id)
    items = list_registration_diagnostics(normalized)
    return {
        "ok": True,
        "task_id": normalized,
        "items": items,
        "summary": registration_diagnostics_summary(normalized),
    }


@router.post("/{task_id}/diagnostics/prune")
def prune_task_registration_diagnostics(task_id: str):
    normalized = _task_id(task_id)
    return prune_registration_diagnostics(task_id=normalized)


@router.get("/{task_id}/diagnostics/{artifact_id}")
def get_task_registration_diagnostic(task_id: str, artifact_id: int):
    normalized = _task_id(task_id)
    for item in list_registration_diagnostics(normalized):
        if int(item.get("id") or 0) == int(artifact_id or 0):
            return {"ok": True, "item": item}
    raise HTTPException(404, "注册诊断包不存在")


@router.get("/{task_id}/diagnostics/{artifact_id}/download")
def download_task_registration_diagnostic(task_id: str, artifact_id: int):
    normalized = _task_id(task_id)
    try:
        row, path = build_registration_diagnostic_bundle(
            artifact_id,
            task_id=normalized,
        )
    except Exception as exc:
        raise _translate_error(exc) from exc
    filename = (
        f"registration-diagnostic-{normalized}-"
        f"attempt-{int(row.attempt_number or row.attempt_id or 0):04d}.zip"
    )
    return FileResponse(
        path,
        filename=filename,
        media_type="application/zip",
        headers=_DOWNLOAD_HEADERS,
        background=BackgroundTask(path.unlink, missing_ok=True),
    )


@router.get("/{task_id}/diagnostics/{artifact_id}/files/{filename}")
def download_task_registration_diagnostic_file(
    task_id: str,
    artifact_id: int,
    filename: str,
):
    normalized = _task_id(task_id)
    safe_name = Path(filename).name
    if safe_name != filename or safe_name not in _ALLOWED_FILES:
        raise HTTPException(400, "诊断文件名无效")
    try:
        _row, path = registration_diagnostic_path(
            artifact_id,
            task_id=normalized,
            filename=safe_name,
        )
    except Exception as exc:
        raise _translate_error(exc) from exc
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(
        path,
        filename=f"{normalized}-{safe_name}",
        media_type=media_type,
        headers=_DOWNLOAD_HEADERS,
    )


@router.post("/{task_id}/diagnostics/{artifact_id}/pin")
def pin_task_registration_diagnostic(
    task_id: str,
    artifact_id: int,
    body: DiagnosticPinRequest,
):
    normalized = _task_id(task_id)
    try:
        item = set_registration_diagnostic_pinned(
            artifact_id,
            task_id=normalized,
            pinned=body.pinned,
        )
    except Exception as exc:
        raise _translate_error(exc) from exc
    return {"ok": True, "item": item}


@router.delete("/{task_id}/diagnostics/{artifact_id}")
def delete_task_registration_diagnostic(task_id: str, artifact_id: int):
    normalized = _task_id(task_id)
    try:
        item = delete_registration_diagnostic(
            artifact_id,
            task_id=normalized,
        )
    except Exception as exc:
        raise _translate_error(exc) from exc
    return {"ok": True, "item": item}
