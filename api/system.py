"""System and resource health endpoints.

This module intentionally stays read-only.  It gives the frontend one place to
understand whether the local runtime has enough healthy resources before an
operator starts expensive registration/payment workflows.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import APIRouter, Depends
from sqlmodel import Session, select

from core.db import AccountModel, ProxyModel, get_session


router = APIRouter(prefix="/system", tags=["system"])

HealthStatus = str


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _resource(
    *,
    key: str,
    title: str,
    status: HealthStatus = "unknown",
    message: str = "",
    metrics: dict[str, Any] | None = None,
    action_path: str = "",
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "key": key,
        "title": title,
        "status": status,
        "message": message,
        "metrics": metrics or {},
        "action_path": action_path,
        "details": details or {},
    }


def _safe_resource(
    key: str,
    title: str,
    loader: Callable[[], dict[str, Any]],
    *,
    action_path: str = "",
) -> dict[str, Any]:
    try:
        return loader()
    except Exception as exc:
        return _resource(
            key=key,
            title=title,
            status="error",
            message=f"读取状态失败: {exc}",
            action_path=action_path,
        )


def _account_resource(session: Session) -> dict[str, Any]:
    rows = session.exec(select(AccountModel)).all()
    by_status: dict[str, int] = {}
    by_platform: dict[str, int] = {}
    for row in rows:
        by_status[row.status] = by_status.get(row.status, 0) + 1
        by_platform[row.platform] = by_platform.get(row.platform, 0) + 1

    total = len(rows)
    invalid = int(by_status.get("invalid", 0) or 0)
    payment_failed = int(by_status.get("payment_failed", 0) or 0)
    status = "healthy"
    if total == 0:
        status = "warning"
    elif invalid + payment_failed > 0 and invalid + payment_failed >= max(total // 2, 1):
        status = "warning"

    return _resource(
        key="accounts",
        title="账号池",
        status=status,
        message=f"共 {total} 个账号",
        metrics={
            "total": total,
            "by_status": by_status,
            "by_platform": by_platform,
            "invalid": invalid,
            "payment_failed": payment_failed,
        },
        action_path="/chatgpt",
    )


def _proxy_resource(session: Session) -> dict[str, Any]:
    rows = session.exec(select(ProxyModel)).all()
    total = len(rows)
    active = sum(1 for row in rows if bool(getattr(row, "is_active", False)))
    cooldown = sum(
        1
        for row in rows
        if bool(getattr(row, "homepage_circuit_open_until", None)) or bool(getattr(row, "cooldown_until", None))
    )
    consecutive_failures = sum(
        max(
            int(getattr(row, "homepage_consecutive_failures", 0) or 0),
            int(getattr(row, "consecutive_failures", 0) or 0),
        )
        for row in rows
    )
    healthy = sum(1 for row in rows if str(getattr(row, "scan_status", "") or "").lower() == "ok")
    chatgpt_ok = sum(1 for row in rows if str(getattr(row, "chatgpt_status", "") or "").lower() == "ok")
    exit_countries = sorted(
        {
            str(getattr(row, "exit_country_code", "") or "").strip().upper()
            for row in rows
            if str(getattr(row, "exit_country_code", "") or "").strip()
        }
    )
    latest_error = next(
        (
            str(getattr(row, "last_error", "") or getattr(row, "homepage_last_error", "") or "").strip()
            for row in rows
            if str(getattr(row, "last_error", "") or getattr(row, "homepage_last_error", "") or "").strip()
        ),
        "",
    )

    status = "healthy"
    message = f"活跃 {active}/{total}，健康 {healthy}"
    if total == 0:
        status = "warning"
        message = "未配置代理；直连场景可忽略"
    elif active == 0:
        status = "error"
        message = "没有活跃代理"
    elif active and healthy == 0 and any(str(getattr(row, "scan_status", "") or "").lower() != "unchecked" for row in rows):
        status = "error"
        message = f"活跃 {active}/{total}，没有扫描健康代理"
    elif cooldown or consecutive_failures:
        status = "warning"
        message = f"活跃 {active}/{total}，健康 {healthy}，冷却 {cooldown}"

    return _resource(
        key="proxies",
        title="代理池",
        status=status,
        message=message,
        metrics={
            "total": total,
            "active": active,
            "disabled": total - active,
            "healthy": healthy,
            "chatgpt_ok": chatgpt_ok,
            "cooldown": cooldown,
            "consecutive_failures": consecutive_failures,
            "exit_country_count": len(exit_countries),
        },
        action_path="/proxies",
        details={"latest_error": latest_error, "exit_countries": exit_countries},
    )


def _phone_pool_resource() -> dict[str, Any]:
    from services.chatgpt_core.phone_pool_repository import PhonePoolRepository

    repo = PhonePoolRepository()
    records = repo.list()
    summary = repo.summarize(records)
    total = int(summary.get("total") or 0)
    available = int(summary.get("available") or 0)
    remaining_capacity = int(summary.get("remaining_capacity") or 0)

    status = "healthy"
    if total == 0:
        status = "warning"
    elif available <= 0 or remaining_capacity <= 0:
        status = "error"
    elif int(summary.get("rate_limited") or 0) or int(summary.get("cannot_send") or 0):
        status = "warning"

    if total == 0:
        message = "未导入手机号"
    else:
        message = f"可用 {available} 个，剩余容量 {remaining_capacity}"

    return _resource(
        key="phone_pool",
        title="手机号池",
        status=status,
        message=message,
        metrics=summary,
        action_path="/phone-pool",
    )


def _solver_resource() -> dict[str, Any]:
    from services.solver_manager import is_running

    running = bool(is_running())
    return _resource(
        key="solver",
        title="Turnstile Solver",
        status="healthy" if running else "warning",
        message="运行中" if running else "未运行或不可访问",
        metrics={"running": running},
        action_path="/settings",
    )


def _scheduler_resource() -> dict[str, Any]:
    from core.scheduler import scheduler

    running = bool(getattr(scheduler, "_running", False))
    return _resource(
        key="scheduler",
        title="后台调度器",
        status="healthy" if running else "warning",
        message="运行中" if running else "未运行",
        metrics={
            "running": running,
            "loop_interval_seconds": int(getattr(scheduler, "_loop_interval_seconds", 0) or 0),
        },
    )


def _tempmail_archive_resource() -> dict[str, Any]:
    from services.tempmail_archive_cleanup import get_status

    payload = get_status()
    enabled = bool(payload.get("enabled"))
    last_error = str(payload.get("last_error") or "").strip()
    active_task_count = int(payload.get("active_task_count") or 0)
    if not enabled:
        status = "unknown"
        message = "未启用归档清理"
    elif last_error:
        status = "warning"
        message = f"归档清理异常: {last_error}"
    elif active_task_count:
        status = "warning"
        message = f"有 {active_task_count} 个活跃任务，清理可能暂停"
    else:
        status = "healthy"
        message = "归档清理待命"
    return _resource(
        key="tempmail_archive",
        title="TempMail 归档清理",
        status=status,
        message=message,
        metrics=payload,
        action_path="/settings",
    )


def build_system_health(
    session: Session,
    *,
    include_runtime: bool = True,
) -> dict[str, Any]:
    """Build a read-only health snapshot.

    ``include_runtime=False`` is useful for unit tests or callers that only need
    database-backed resources and do not want HTTP probes such as the Solver
    status check.
    """
    resources: list[dict[str, Any]] = [
        _safe_resource("accounts", "账号池", lambda: _account_resource(session), action_path="/chatgpt"),
        _safe_resource("proxies", "代理池", lambda: _proxy_resource(session), action_path="/proxies"),
        _safe_resource("phone_pool", "手机号池", _phone_pool_resource, action_path="/phone-pool"),
    ]

    if include_runtime:
        resources.extend(
            [
                _safe_resource("solver", "Turnstile Solver", _solver_resource, action_path="/settings"),
                _safe_resource("scheduler", "后台调度器", _scheduler_resource),
                _safe_resource("tempmail_archive", "TempMail 归档清理", _tempmail_archive_resource, action_path="/settings"),
            ]
        )

    summary = {status: 0 for status in ("healthy", "warning", "error", "unknown")}
    for item in resources:
        status = str(item.get("status") or "unknown")
        summary[status] = summary.get(status, 0) + 1
    summary["total"] = len(resources)

    return {
        "generated_at": _now_iso(),
        "summary": summary,
        "resources": resources,
    }


@router.get("/health")
def get_system_health(session: Session = Depends(get_session)):
    return build_system_health(session)
