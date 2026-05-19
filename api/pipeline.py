import asyncio
import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from services.pipeline import pipeline_engine
from services.pipeline.models import PipelineConfig

router = APIRouter(prefix="/pipeline", tags=["pipeline"])


class PipelineConfigUpdateRequest(BaseModel):
    config: PipelineConfig


@router.get("/config")
def get_pipeline_config():
    return pipeline_engine.config.model_dump()


@router.put("/config")
def update_pipeline_config(body: PipelineConfigUpdateRequest):
    config = body.config
    errors = pipeline_engine.config_store.validate(config)
    if errors:
        raise HTTPException(400, {"errors": errors})
    if pipeline_engine.status == "running" and not pipeline_engine.config_store.can_update_while_running():
        raise HTTPException(409, "流水线运行中，暂不允许修改配置")
    saved = pipeline_engine.set_config(config)
    return {"ok": True, "config": saved.model_dump()}


@router.post("/start")
def start_pipeline():
    pipeline_engine.start()
    return {"ok": True, "status": pipeline_engine.status}


@router.post("/stop")
def stop_pipeline():
    pipeline_engine.stop()
    return {"ok": True, "status": pipeline_engine.status}


@router.post("/pause")
def pause_pipeline():
    if pipeline_engine.status == "paused":
        pipeline_engine.resume()
    else:
        pipeline_engine.pause()
    return {"ok": True, "status": pipeline_engine.status}


@router.get("/status")
def get_pipeline_status():
    return pipeline_engine.get_status_snapshot()


@router.get("/history")
def get_pipeline_history(limit: int = 20):
    tasks = pipeline_engine.state_store.list_task_history(limit=limit)
    return {
        "items": [
            {
                "id": task.id,
                "task_key": task.task_key,
                "status": task.status,
                "started_at": task.started_at.isoformat() if task.started_at else "",
                "stopped_at": task.stopped_at.isoformat() if task.stopped_at else "",
                "updated_at": task.updated_at.isoformat() if task.updated_at else "",
            }
            for task in tasks
        ]
    }


@router.get("/logs/task/{task_id}")
def get_pipeline_task_logs(task_id: int):
    task = pipeline_engine.state_store.get_task(int(task_id or 0))
    if task is None:
        raise HTTPException(404, "流水线任务不存在")
    return {
        "task": {
            "id": task.id,
            "task_key": task.task_key,
            "status": task.status,
            "started_at": task.started_at.isoformat() if task.started_at else "",
            "stopped_at": task.stopped_at.isoformat() if task.stopped_at else "",
            "updated_at": task.updated_at.isoformat() if task.updated_at else "",
        },
        "logs": pipeline_engine.state_store.list_task_logs(int(task.id or 0)),
    }


@router.get("/logs/stream")
async def stream_pipeline_logs():
    subscriber = pipeline_engine.log_bus.subscribe(replay=True)

    async def event_generator():
        try:
            while True:
                try:
                    line = subscriber.get(timeout=1.0)
                except Exception:
                    await asyncio.sleep(0.1)
                    continue
                yield f"data: {json.dumps({'line': line}, ensure_ascii=False)}\n\n"
        finally:
            pipeline_engine.log_bus.unsubscribe(subscriber)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
