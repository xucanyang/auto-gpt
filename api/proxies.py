from datetime import datetime, timezone
import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select
from pydantic import BaseModel, Field
from core.db import ProxyModel, get_session
from core.proxy_pool import proxy_pool
from core.timezone import beijing_iso
from services.proxy_scanner import calculate_health_score, mask_proxy_url, parse_proxy_endpoint, proxy_scan_manager, scan_proxy_url
from services import proxy_scan_scheduler

router = APIRouter(prefix="/proxies", tags=["proxies"])


class ProxyCreate(BaseModel):
    url: str
    region: str = ""
    desired_country_code: str = ""
    provider: str = ""
    note: str = ""


class ProxyBulkCreate(BaseModel):
    proxies: list[str]
    region: str = ""
    desired_country_code: str = ""
    provider: str = ""
    note: str = ""


class ProxyBulkDelete(BaseModel):
    ids: list[int] = Field(default_factory=list)


class ProxyScanRequest(BaseModel):
    ids: list[int] = Field(default_factory=list)
    scope: str = "all"
    targets: list[str] = Field(default_factory=lambda: ["basic", "geo"])
    concurrency: int = 8
    timeout_seconds: int = 8
    refresh_geo: bool = True
    only_active: bool = False


class ProxyCandidateRequest(BaseModel):
    target: str = "chatgpt"
    country_code: str = ""
    region: str = ""
    limit: int = 10
    min_score: float = 0


class ProxySnapshotRequest(BaseModel):
    ids: list[int] = Field(default_factory=list)


class DynamicProxyPreviewRequest(BaseModel):
    proxy: str = ""
    country_code: str = ""
    retention_minutes: int | None = None
    refresh_sid: bool = True
    probe: bool = True
    require_country_match: bool | None = None
    timeout_seconds: int = 8


def _apply_proxy_metadata(proxy: ProxyModel, body: ProxyCreate | ProxyBulkCreate) -> None:
    endpoint = parse_proxy_endpoint(proxy.url)
    proxy.scheme = endpoint["scheme"]
    proxy.host = endpoint["host"]
    proxy.port = endpoint["port"]
    proxy.region = str(getattr(body, "region", "") or "").strip()
    proxy.desired_country_code = str(getattr(body, "desired_country_code", "") or "").strip().upper()
    proxy.provider = str(getattr(body, "provider", "") or "").strip()
    proxy.note = str(getattr(body, "note", "") or "").strip()


def _iso(value: Any) -> Any:
    if isinstance(value, datetime):
        return beijing_iso(value)
    return value


def _proxy_to_dict(proxy: ProxyModel) -> dict[str, Any]:
    if hasattr(proxy, "model_dump"):
        data = proxy.model_dump()
    else:
        data = proxy.dict()
    return {key: _iso(value) for key, value in data.items()}


def _unique_positive_ids(values: list[int]) -> list[int]:
    ids: list[int] = []
    seen: set[int] = set()
    for value in values or []:
        try:
            proxy_id = int(value)
        except (TypeError, ValueError):
            continue
        if proxy_id <= 0 or proxy_id in seen:
            continue
        seen.add(proxy_id)
        ids.append(proxy_id)
    return ids


def _normalize_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _is_future(value: Any, now: datetime | None = None) -> bool:
    parsed = _normalize_dt(value)
    return bool(parsed and parsed > (now or datetime.now(timezone.utc)))


def _proxy_summary(items: list[ProxyModel]) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    exit_countries = {
        str(item.exit_country_code or "").strip().upper()
        for item in items
        if str(item.exit_country_code or "").strip()
    }
    return {
        "total": len(items),
        "active": sum(1 for item in items if bool(item.is_active)),
        "disabled": sum(1 for item in items if not bool(item.is_active)),
        "cooling": sum(1 for item in items if _is_future(item.cooldown_until, now) or _is_future(item.homepage_circuit_open_until, now)),
        "failed": sum(
            1
            for item in items
            if int(item.consecutive_failures or 0) > 0
            or int(item.homepage_consecutive_failures or 0) > 0
            or bool(item.last_error or item.homepage_last_error or item.chatgpt_last_error)
        ),
        "healthy": sum(1 for item in items if str(item.scan_status or "").lower() == "ok"),
        "degraded": sum(1 for item in items if str(item.scan_status or "").lower() == "degraded"),
        "unchecked": sum(1 for item in items if str(item.scan_status or "unchecked").lower() == "unchecked"),
        "chatgpt_ok": sum(1 for item in items if str(item.chatgpt_status or "").lower() == "ok"),
        "exit_country_count": len(exit_countries),
    }


def _snapshot_proxies(session: Session, ids: list[int]) -> dict[str, Any]:
    requested_ids = _unique_positive_ids(ids)
    rows = session.exec(select(ProxyModel)).all()
    by_id = {int(item.id or 0): item for item in rows}
    return {
        "items": [_proxy_to_dict(by_id[proxy_id]) for proxy_id in requested_ids if proxy_id in by_id],
        "missing": [proxy_id for proxy_id in requested_ids if proxy_id not in by_id],
        "summary": _proxy_summary(rows),
    }


def _parse_probe_json(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except Exception as exc:
        return {"error": f"last_probe_json 解析失败: {exc}"}
    return parsed if isinstance(parsed, dict) else {}


def _proxy_diagnostics(proxy: ProxyModel) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    item = _proxy_to_dict(proxy)
    endpoint = parse_proxy_endpoint(proxy.url)
    last_probe = _parse_probe_json(proxy.last_probe_json)
    notes: list[dict[str, str]] = []

    def add_note(severity: str, code: str, message: str) -> None:
        notes.append({"severity": severity, "code": code, "message": message})

    if not bool(proxy.is_active):
        add_note("info", "disabled", "代理已禁用，不会被代理池选中。")
    if not endpoint.get("scheme") or not endpoint.get("host") or not endpoint.get("port"):
        add_note("error", "invalid_endpoint", "代理地址缺少协议、host 或端口，扫描和任务使用都会失败。")

    cooldown_until = _normalize_dt(proxy.cooldown_until)
    homepage_cooldown_until = _normalize_dt(proxy.homepage_circuit_open_until)
    active_cooldown = cooldown_until if cooldown_until and cooldown_until > now else None
    active_homepage_cooldown = homepage_cooldown_until if homepage_cooldown_until and homepage_cooldown_until > now else None
    if active_cooldown or active_homepage_cooldown:
        add_note("warning", "cooling", f"代理仍在冷却中，冷却至 {_iso(active_cooldown or active_homepage_cooldown)}。")
    elif proxy.cooldown_until or proxy.homepage_circuit_open_until:
        add_note("warning", "stale_cooldown", "冷却时间已经过去，但冷却字段还在，可以清空冷却。")

    scan_status = str(proxy.scan_status or "unchecked").lower()
    if scan_status == "unchecked":
        add_note("warning", "unchecked", "代理还没有完整扫描，建议先扫描此代理。")
    elif scan_status == "failed":
        add_note("error", "scan_failed", f"基础扫描失败：{proxy.last_error_code or '-'} {proxy.last_error or ''}".strip())
    elif scan_status == "degraded":
        add_note("warning", "scan_degraded", "基础代理可用，但某些目标不可用或数据不完整。")

    chatgpt_status = str(proxy.chatgpt_status or "unchecked").lower()
    cffi_targets = {}
    chatgpt_probe = last_probe.get("chatgpt") if isinstance(last_probe.get("chatgpt"), dict) else {}
    if isinstance(chatgpt_probe.get("targets"), dict):
        cffi_targets = chatgpt_probe.get("targets") or {}
    cffi_chatgpt_ok = bool(isinstance(cffi_targets.get("chatgpt"), dict) and cffi_targets["chatgpt"].get("ok"))
    cffi_auth_ok = bool(isinstance(cffi_targets.get("auth"), dict) and cffi_targets["auth"].get("ok"))
    registration_probe_ok = bool(chatgpt_probe.get("ok") and str(chatgpt_probe.get("target") or "") == "registration_homepage_csrf")
    if chatgpt_status == "unchecked":
        add_note("info", "chatgpt_unchecked", "还没检测 ChatGPT 首页可达性。")
    elif chatgpt_status in {"blocked_403", "rate_limited_429", "timeout", "tls_error", "dns_error", "proxy_error", "proxy_auth_failed", "connection_error", "connection_refused", "failed"}:
        if registration_probe_ok:
            add_note("success", "register_probe_ok", "注册链路首页+CSRF 检测可用；普通 HTTP 403 已作为诊断保留。")
        else:
            add_note("error" if chatgpt_status in {"blocked_403", "proxy_auth_failed"} else "warning", "chatgpt_unavailable", f"ChatGPT 检测异常：{chatgpt_status} {proxy.chatgpt_last_error or ''}".strip())
            if cffi_chatgpt_ok and cffi_auth_ok:
                add_note("info", "cffi_probe_only", "通用 curl_cffi 检测可用，但真实注册首页+CSRF 预检未通过，暂不作为注册候选。")

    desired = str(proxy.desired_country_code or proxy.region or "").strip().upper()
    actual = str(proxy.exit_country_code or "").strip().upper()
    if desired and len(desired) <= 3 and actual and desired != actual:
        add_note("warning", "country_mismatch", f"期望出口 {desired}，实测出口 {actual}。")
    if float(proxy.health_score or 0) < 50:
        add_note("warning", "low_health_score", f"健康分 {float(proxy.health_score or 0):.1f}，低于默认候选阈值 50。")
    if int(proxy.consecutive_failures or 0) > 0 or int(proxy.homepage_consecutive_failures or 0) > 0:
        add_note("warning", "recent_failures", f"连续失败：基础 {int(proxy.consecutive_failures or 0)}，ChatGPT {int(proxy.homepage_consecutive_failures or 0)}。")
    if bool(proxy.is_active) and scan_status == "ok" and chatgpt_status in {"ok", "unchecked"} and float(proxy.health_score or 0) >= 50:
        add_note("success", "candidate_ready", "代理当前符合进入候选池的基础条件。")
    elif bool(proxy.is_active) and registration_probe_ok and float(proxy.health_score or 0) >= 50:
        add_note("success", "candidate_ready", "代理当前符合注册链路候选条件。")

    return {
        "ok": True,
        "item": item,
        "masked_url": mask_proxy_url(proxy.url),
        "endpoint": endpoint,
        "last_probe": last_probe,
        "scheduler": proxy_scan_scheduler.status(),
        "notes": notes,
    }


@router.get("")
def list_proxies(session: Session = Depends(get_session)):
    items = session.exec(select(ProxyModel)).all()
    return items


@router.get("/summary")
def proxy_summary(session: Session = Depends(get_session)):
    items = session.exec(select(ProxyModel)).all()
    return {"summary": _proxy_summary(items)}


@router.post("/snapshot")
def snapshot_proxies(body: ProxySnapshotRequest, session: Session = Depends(get_session)):
    return _snapshot_proxies(session, body.ids)


@router.post("")
def add_proxy(body: ProxyCreate, session: Session = Depends(get_session)):
    url = str(body.url or "").strip()
    if not url:
        raise HTTPException(400, "代理地址不能为空")
    existing = session.exec(select(ProxyModel).where(ProxyModel.url == url)).first()
    if existing:
        raise HTTPException(400, "代理已存在")
    p = ProxyModel(url=url)
    _apply_proxy_metadata(p, body)
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


@router.post("/bulk")
def bulk_add_proxies(body: ProxyBulkCreate, session: Session = Depends(get_session)):
    added = 0
    for url in body.proxies:
        url = url.strip()
        if not url:
            continue
        existing = session.exec(select(ProxyModel).where(ProxyModel.url == url)).first()
        if not existing:
            proxy = ProxyModel(url=url)
            _apply_proxy_metadata(proxy, body)
            session.add(proxy)
            added += 1
    session.commit()
    return {"added": added}


@router.post("/bulk-delete")
def bulk_delete_proxies(body: ProxyBulkDelete, session: Session = Depends(get_session)):
    ids: list[int] = []
    seen: set[int] = set()
    for raw_id in body.ids:
        try:
            proxy_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if proxy_id <= 0 or proxy_id in seen:
            continue
        seen.add(proxy_id)
        ids.append(proxy_id)
    if not ids:
        raise HTTPException(400, "请先选择代理")

    deleted = 0
    for proxy_id in ids:
        proxy = session.get(ProxyModel, proxy_id)
        if not proxy:
            continue
        session.delete(proxy)
        deleted += 1
    session.commit()
    return {"ok": True, "deleted": deleted, "missing": len(ids) - deleted}


@router.post("/scan")
def start_proxy_scan(body: ProxyScanRequest):
    ids = body.ids if str(body.scope or "all").lower() in {"selected", "ids"} else None
    job = proxy_scan_manager.start_job_from_query(
        ids=ids,
        only_active=body.only_active,
        targets=body.targets,
        concurrency=body.concurrency,
        timeout_seconds=body.timeout_seconds,
        refresh_geo=body.refresh_geo,
    )
    return job


@router.get("/scan/{job_id}")
def get_proxy_scan(job_id: str):
    job = proxy_scan_manager.get_job(job_id)
    if not job:
        raise HTTPException(404, "扫描任务不存在")
    return job


@router.post("/scan/{job_id}/cancel")
def cancel_proxy_scan(job_id: str):
    job = proxy_scan_manager.cancel_job(job_id)
    if not job:
        raise HTTPException(404, "扫描任务不存在")
    return job


@router.post("/candidates")
def get_proxy_candidates(body: ProxyCandidateRequest):
    return {
        "items": proxy_pool.get_candidate_records(
            region=body.region,
            country_code=body.country_code,
            target=body.target,
            limit=body.limit,
            min_score=body.min_score,
        )
    }


@router.post("/dynamic-preview")
def dynamic_proxy_preview(body: DynamicProxyPreviewRequest):
    from core.config_store import config_store
    from core.dynamic_proxy import resolve_dynamic_proxy_template, redact_proxy_url
    from core.proxy_utils import get_global_dynamic_proxy_country, get_global_dynamic_proxy_template, normalize_proxy_url

    template = str(body.proxy or get_global_dynamic_proxy_template() or "").strip()
    country_code = str(body.country_code or get_global_dynamic_proxy_country("") or "").strip().upper()
    retention_minutes = (
        body.retention_minutes
        if body.retention_minutes is not None
        else config_store.get("dynamic_proxy_ip_retention_minutes", "5")
    )
    if retention_minutes in (None, ""):
        retention_minutes = "5"
    if not template:
        raise HTTPException(400, "动态节点地址不能为空")
    if not country_code:
        raise HTTPException(400, "出口国家不能为空")

    try:
        resolved = resolve_dynamic_proxy_template(
            template,
            country_code,
            refresh_sid=bool(body.refresh_sid),
            retention_minutes=retention_minutes,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    runtime_proxy = normalize_proxy_url(resolved.proxy_url) or ""
    runtime_redacted = mask_proxy_url(runtime_proxy)
    timeout = max(2, min(60, int(body.timeout_seconds or config_store.get("dynamic_proxy_probe_timeout_seconds", "8") or 8)))
    require_match = body.require_country_match
    if require_match is None:
        require_match = str(config_store.get("dynamic_proxy_require_country_match", "true") or "true").strip().lower() not in {"0", "false", "no", "off"}

    response: dict[str, Any] = {
        "ok": True,
        "provider": resolved.provider,
        "expected_country": resolved.requested_country_code,
        "template_country": resolved.template_country_code,
        "declared_country": resolved.resolved_country_code,
        "sid_refreshed": bool(resolved.sid_refreshed),
        "retention_minutes": resolved.retention_minutes,
        "retention_applied": bool(resolved.retention_applied),
        "template_redacted": resolved.redacted_template,
        "proxy": runtime_redacted,
        "runtime_proxy_redacted": runtime_redacted,
        "normalized_proxy_redacted": redact_proxy_url(runtime_proxy),
        "probe_enabled": bool(body.probe),
        "require_country_match": bool(require_match),
        "actual_country": "",
        "exit_ip": "",
        "match": False,
        "latency_ms": 0,
    }

    if not body.probe:
        response["match"] = False
        response["message"] = "已生成动态代理；未执行出口探测"
        return response

    summary = scan_proxy_url(runtime_proxy, targets=["basic", "geo"], timeout_seconds=timeout, refresh_geo=True)
    basic = summary.get("basic") if isinstance(summary.get("basic"), dict) else {}
    geo = summary.get("geo") if isinstance(summary.get("geo"), dict) else {}
    exit_ip = str((basic or {}).get("exit_ip") or "").strip()
    actual_country = str((geo or {}).get("country_code") or "").strip().upper()
    declared_country = str(resolved.resolved_country_code or "").strip().upper()
    geo_unverified = bool((basic or {}).get("ok") and not actual_country)
    trusted_declared_country = bool(
        geo_unverified
        and resolved.provider == "cliproxy"
        and declared_country == resolved.requested_country_code
    )
    response.update(
        {
            "exit_ip": exit_ip,
            "actual_country": actual_country,
            "match": bool(actual_country and actual_country == resolved.requested_country_code),
            "geo_unverified": geo_unverified,
            "trusted_declared_country": trusted_declared_country,
            "geo_source": str((geo or {}).get("source") or ""),
            "latency_ms": int((basic or {}).get("latency_ms") or summary.get("duration_ms") or 0),
            "probe": {
                "basic_ok": bool((basic or {}).get("ok")),
                "geo_ok": bool((geo or {}).get("ok")),
                "geo_source": str((geo or {}).get("source") or ""),
                "basic_error_code": str((basic or {}).get("error_code") or ""),
                "basic_error": str((basic or {}).get("error") or "")[:200],
                "geo_error_code": str((geo or {}).get("error_code") or ""),
                "geo_error": str((geo or {}).get("error") or "")[:200],
            },
        }
    )
    if not (basic or {}).get("ok"):
        response["ok"] = False
        response["message"] = str((basic or {}).get("error") or "动态代理基础连通性探测失败")[:200]
    elif require_match and actual_country and not response["match"]:
        response["ok"] = False
        response["message"] = f"出口国家不匹配：期望 {resolved.requested_country_code}，实测 {actual_country or 'unknown'}"
    elif require_match and not actual_country and not trusted_declared_country:
        response["ok"] = False
        response["message"] = f"出口国家无法实测：期望 {resolved.requested_country_code}，GeoIP 未返回国家"
    elif require_match and not actual_country and trusted_declared_country:
        response["message"] = "动态代理基础连通性通过；GeoIP 未返回国家，已按 Cliproxy 模板 region 标记为未实测可用"
    else:
        response["message"] = "动态代理出口探测完成"
    return response


@router.get("/scan-scheduler/status")
def get_proxy_scan_scheduler_status():
    return proxy_scan_scheduler.status()


@router.post("/scan-scheduler/run")
def run_proxy_scan_scheduler_once():
    return proxy_scan_scheduler.run_once(force=True)


@router.get("/{proxy_id}/diagnostics")
def proxy_diagnostics(proxy_id: int, session: Session = Depends(get_session)):
    proxy = session.get(ProxyModel, proxy_id)
    if not proxy:
        raise HTTPException(404, "代理不存在")
    return _proxy_diagnostics(proxy)


@router.post("/{proxy_id}/clear-cooldown")
def clear_proxy_cooldown(proxy_id: int, session: Session = Depends(get_session)):
    proxy = session.get(ProxyModel, proxy_id)
    if not proxy:
        raise HTTPException(404, "代理不存在")
    proxy.homepage_circuit_open_until = None
    proxy.cooldown_until = None
    proxy.homepage_consecutive_failures = 0
    proxy.consecutive_failures = 0
    proxy.health_score = calculate_health_score(proxy)
    session.add(proxy)
    session.commit()
    session.refresh(proxy)
    return _proxy_to_dict(proxy)


@router.delete("/{proxy_id}")
def delete_proxy(proxy_id: int, session: Session = Depends(get_session)):
    p = session.get(ProxyModel, proxy_id)
    if not p:
        raise HTTPException(404, "代理不存在")
    session.delete(p)
    session.commit()
    return {"ok": True}


@router.patch("/{proxy_id}/toggle")
def toggle_proxy(proxy_id: int, session: Session = Depends(get_session)):
    p = session.get(ProxyModel, proxy_id)
    if not p:
        raise HTTPException(404, "代理不存在")
    p.is_active = not p.is_active
    p.health_score = calculate_health_score(p)
    session.add(p)
    session.commit()
    session.refresh(p)
    return _proxy_to_dict(p)


@router.post("/{proxy_id}/scan")
def scan_one_proxy(proxy_id: int, body: ProxyScanRequest | None = None):
    targets = body.targets if body else ["basic", "geo", "chatgpt"]
    timeout_seconds = body.timeout_seconds if body else 8
    refresh_geo = body.refresh_geo if body else True
    job = proxy_scan_manager.start_job(
        [proxy_id],
        targets=targets,
        concurrency=1,
        timeout_seconds=timeout_seconds,
        refresh_geo=refresh_geo,
    )
    return job


@router.post("/check")
def check_proxies():
    job = proxy_scan_manager.start_job_from_query(
        targets=["basic", "geo", "chatgpt"],
        concurrency=8,
        timeout_seconds=8,
        refresh_geo=True,
    )
    return {"message": "检测任务已启动", "job_id": job.get("job_id"), **job}


@router.post("/clear-cooldowns")
def clear_proxy_cooldowns(session: Session = Depends(get_session)):
    items = session.exec(select(ProxyModel)).all()
    cleared = 0
    for item in items:
        had_cooldown = bool(getattr(item, "homepage_circuit_open_until", None)) or bool(getattr(item, "cooldown_until", None))
        if (
            had_cooldown
            or int(getattr(item, "homepage_consecutive_failures", 0) or 0) > 0
            or int(getattr(item, "consecutive_failures", 0) or 0) > 0
        ):
            item.homepage_circuit_open_until = None
            item.cooldown_until = None
            item.homepage_consecutive_failures = 0
            item.consecutive_failures = 0
            item.health_score = calculate_health_score(item)
            session.add(item)
            cleared += 1
    session.commit()
    return {"ok": True, "cleared": cleared}
