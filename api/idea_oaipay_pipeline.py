from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from services.idea_oaipay_pipeline.engine import idea_oaipay_pipeline_engine
from services.idea_oaipay_pipeline.models import IdeaOaiPayPipelineConfig

router = APIRouter(prefix="/idea-oaipay-pipeline", tags=["idea-oaipay-pipeline"])


class StartPipelineRequest(BaseModel):
    config: IdeaOaiPayPipelineConfig


@router.post("/start")
def start_idea_oaipay_pipeline(body: StartPipelineRequest):
    try:
        task = idea_oaipay_pipeline_engine.start(body.config)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True, "task": idea_oaipay_pipeline_engine.get_status_snapshot().get("task") or {"id": task.id}}


@router.post("/pause")
def pause_idea_oaipay_pipeline():
    idea_oaipay_pipeline_engine.pause()
    return {"ok": True, "status": idea_oaipay_pipeline_engine.status}


@router.post("/resume")
def resume_idea_oaipay_pipeline():
    idea_oaipay_pipeline_engine.resume()
    return {"ok": True, "status": idea_oaipay_pipeline_engine.status}


@router.post("/stop")
def stop_idea_oaipay_pipeline():
    idea_oaipay_pipeline_engine.stop()
    return {"ok": True, "status": idea_oaipay_pipeline_engine.status}


@router.get("/status")
def get_idea_oaipay_pipeline_status(item_limit: int = 500):
    return idea_oaipay_pipeline_engine.get_status_snapshot(item_limit=max(1, min(int(item_limit or 500), 5000)))


@router.get("/history")
def get_idea_oaipay_pipeline_history(limit: int = 20):
    snapshot = idea_oaipay_pipeline_engine.get_status_snapshot(item_limit=1)
    history = snapshot.get("history") or []
    return {"items": history[: max(1, min(int(limit or 20), 200))]}


@router.post("/items/{item_id}/retry/{stage}")
def retry_idea_oaipay_pipeline_item_stage(item_id: int, stage: str):
    try:
        item = idea_oaipay_pipeline_engine.retry_item_stage(item_id, stage)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "item": item}
