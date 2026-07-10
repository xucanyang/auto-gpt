"""ChatGPT 专用功能 API"""
import asyncio
import base64
import hashlib
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlmodel import Session, select
from pydantic import BaseModel, Field
from core.db import AccountModel, get_session
from services.chatgpt_account_state import (
    apply_chatgpt_status_policy,
    apply_payment_snapshot_status,
    classify_chatgpt_capabilities,
    mark_payment_failed,
    mark_payment_pending,
)
from services.chatgpt_core.gopay_phone import (
    GOPAY_RECOGNIZED_COUNTRY_CODES_KEY,
    normalize_gopay_recognized_country_codes,
    split_gopay_phone_input,
)
from services.chatgpt_core.codex_usage import (
    build_codex_usage_progress_from_extra,
    persist_codex_usage_probe,
    probe_codex_usage_window,
)
from services.chatgpt_core.local_status_refresh import schedule_chatgpt_local_status_refresh_for_account_id
import json, sys


router = APIRouter(prefix="/chatgpt", tags=["chatgpt"])

COUNTRIES = ["ID", "DE", "SG", "US", "TR", "JP", "HK", "GB", "AU", "CA", "IN", "BR", "MX"]
BROWSER_AUTH_VIEWPORT_WIDTH = 1365
BROWSER_AUTH_VIEWPORT_HEIGHT = 900
DEFAULT_GOPAY_BILLING = {
    "country": "ID",
    "line1": "Jl. M.H. Thamrin No. 1",
    "city": "Jakarta",
    "state": "DKI Jakarta",
    "postal_code": "10310",
}
DEFAULT_GOPAY_BILLING_LLM_BASE_URL = "https://api.666800.xyz"
DEFAULT_GOPAY_BILLING_LLM_MODEL = "gpt-5.4"
DEFAULT_GOPAY_BILLING_LLM_WIRE_API = "responses"
DEFAULT_GOPAY_BILLING_LLM_REASONING_EFFORT = "xhigh"
DEFAULT_GOPAY_BILLING_LLM_TIMEOUT_SECONDS = 45.0
DEFAULT_GOPAY_BILLING_LLM_COUNTRY_STRATEGY = "billing_country"
DEFAULT_GOPAY_BILLING_LLM_FIXED_COUNTRY = "US"
GOPAY_BILLING_LLM_COUNTRY_STRATEGIES = {"billing_country", "checkout_country", "fixed_country"}
DEFAULT_GOPAY_BILLING_LLM_PROMPT = (
    "生成一个真实可用的账单地址，地址在谷歌地图中能找到对应的位置。"
    "地址必须和指定国家/地区一致，格式与当前 GoPay 账单地址字段一致。"
)
GOPAY_TASK_SOURCE = "gopay_payment"
GOPAY_TERMINAL_PHASES = {"succeeded", "failed", "cancelled"}
GOPAY_ACTIVE_PHASES = {"created", "starting", "waiting_otp", "waiting_link_pin", "waiting_payment_pin", "verifying"}
GOPAY_OTP_AUTO_RESEND_DELAY_SECONDS_KEY = "chatgpt_gopay_otp_auto_resend_delay_seconds"
DEFAULT_GOPAY_OTP_AUTO_RESEND_DELAY_SECONDS = 120
GOPAY_BATCH_TASKS_KEY = "chatgpt_gopay_batch_tasks"
GOPAY_BATCH_ACTIVE_TASK_KEY = "chatgpt_gopay_active_batch_task_id"
GOPAY_BATCH_ACTIVE_STATUSES = {"queued", "running"}
GOPAY_BATCH_TERMINAL_STATUSES = {"done", "failed", "cancelled"}
GOPAY_BATCH_CONSUMED_PHONE_PHASES = {"waiting_otp", "waiting_link_pin", "waiting_payment_pin", "verifying", "succeeded"}

_GOPAY_TASK_LOCK = threading.Lock()
_GOPAY_TASK_CURSORS: dict[str, int] = {}
_GOPAY_TASK_FINALIZED: set[str] = set()
_GOPAY_TASK_MONITORS: set[str] = set()
_GOPAY_BATCH_LOCK = threading.RLock()
_GOPAY_BATCH_WORKERS: set[str] = set()
_SUB2API_EXPORT_TICKET_LOCK = threading.Lock()
_SUB2API_EXPORT_TICKETS: dict[str, dict[str, Any]] = {}
CHATGPT_EXPORT_MODE_SUB2API = "sub2api"
CHATGPT_EXPORT_MODE_ACCESS_TOKEN = "access_token"
CHATGPT_EXPORT_MODES = frozenset({
    CHATGPT_EXPORT_MODE_SUB2API,
    CHATGPT_EXPORT_MODE_ACCESS_TOKEN,
})


class UploadRequest(BaseModel):
    account_ids: list[int]
    cpa_api_url: Optional[str] = None
    cpa_api_token: Optional[str] = None


class Sub2ApiExportTicketReq(BaseModel):
    ids: list[int] = Field(default_factory=list)
    status: str = ""
    # Keep the original Sub2API JSON export as the default for old callers.
    mode: str = CHATGPT_EXPORT_MODE_SUB2API


class CodexUsageRefreshReq(BaseModel):
    force: bool = True
    proxy: Optional[str] = None
    model: str = ""


class CodexUsageBatchRefreshReq(BaseModel):
    ids: list[int] = Field(default_factory=list)
    limit: int = 50
    force: bool = True
    proxy: Optional[str] = None
    model: str = ""
    status: str = ""
    email: str = ""


class K12WorkspaceRecaptureReq(BaseModel):
    workspace_ids: Any = None
    save_all_spaces: bool = True
    strict_join: bool = False
    proxy: Optional[str] = None
    join_timeout_seconds: Optional[int] = None
    join_retry_count: Optional[int] = None
    post_join_poll_seconds: Optional[str] = None


def _get_account(account_id: int, session: Session) -> AccountModel:
    acc = session.get(AccountModel, account_id)
    if not acc or acc.platform != "chatgpt":
        raise HTTPException(404, "账号不存在")
    return acc


def _to_codex_account(acc: AccountModel):
    """转换为 codex-register 的 Account 对象（duck-typing）"""
    extra = acc.get_extra()

    class _Acc:
        pass

    a = _Acc()
    a.email = acc.email
    a.access_token = extra.get("access_token") or acc.token
    a.refresh_token = extra.get("refresh_token", "")
    a.id_token = extra.get("id_token", "")
    a.session_token = extra.get("session_token", "")
    a.client_id = extra.get("client_id", "app_EMoamEEZ73f0CkXaXp7hrann")
    a.cookies = extra.get("cookies", "")
    a.user_id = acc.user_id
    a.extra = extra
    return a


def _to_gopay_account(acc: AccountModel, access_token: Optional[str] = None):
    codex_acc = _to_codex_account(acc)
    custom_access_token = str(access_token or "").strip()
    if custom_access_token:
        codex_acc.access_token = custom_access_token
        codex_acc.cookies = ""
        codex_acc.extra = {**(getattr(codex_acc, "extra", {}) or {}), "chatgpt_gopay_custom_access_token": True}
    return codex_acc


def _persist_local_probe(acc: AccountModel, probe: dict, session: Session) -> None:
    extra = acc.get_extra()
    extra["chatgpt_local"] = probe
    acc.set_extra(extra)
    apply_chatgpt_status_policy(acc, local_probe=probe)
    from datetime import datetime
    acc.updated_at = datetime.utcnow()
    session.add(acc)
    session.commit()


def _persist_codex_usage_probe(acc: AccountModel, codex_probe: dict[str, Any], session: Session) -> dict[str, Any]:
    """Persist only Codex usage/auth material; never mark the account invalid for quota exhaustion."""
    return persist_codex_usage_probe(acc, codex_probe, session, commit=True)


def _get_tasks_api():
    try:
        from api import tasks as task_api

        return task_api
    except Exception:
        return None


def _gopay_task_line(message: str) -> str:
    return f"[{datetime.now().strftime('%H:%M:%S')}] {message}"


def _task_store_exists(task_api, task_id: str) -> bool:
    store = getattr(task_api, "_task_store", None)
    if store is None:
        return False
    try:
        return bool(store.exists(task_id))
    except Exception:
        return False


def _append_gopay_task_entry(task_api, task_id: str, entry: str) -> None:
    store = getattr(task_api, "_task_store", None)
    if store is None:
        return
    try:
        store.append_log(task_id, entry)
    except Exception:
        pass


def _build_gopay_task_detail(task_api, task_id: str, acc: AccountModel, snapshot: dict, extra: dict | None = None) -> dict:
    payload = {
        "email": acc.email,
        "account_id": acc.id,
        "gopay_session_id": snapshot.get("session_id"),
        "phase": snapshot.get("phase"),
        "payment_status": snapshot.get("status"),
        "plan": snapshot.get("plan"),
        "country": snapshot.get("country"),
        "currency": snapshot.get("currency"),
        "last_error": snapshot.get("last_error") or "",
        "gopay_logs": list(snapshot.get("logs") or []),
        "source": GOPAY_TASK_SOURCE,
    }
    if extra:
        payload.update(extra)
    builder = getattr(task_api, "_build_task_log_detail", None)
    if callable(builder):
        try:
            return builder(task_id, payload)
        except Exception:
            pass
    return {"task_id": task_id, **payload}


def _save_gopay_task_history(
    task_api,
    task_id: str,
    acc: AccountModel,
    snapshot: dict,
    status: str,
    *,
    error: str = "",
    outcome: str = "",
) -> None:
    save = getattr(task_api, "_save_task_log", None)
    if not callable(save):
        return
    detail = _build_gopay_task_detail(
        task_api,
        task_id,
        acc,
        snapshot,
        {"attempt_outcome": outcome or f"gopay_payment_{status}"},
    )
    try:
        save("chatgpt", acc.email or "", status, error=error, detail=detail)
    except Exception:
        pass


def _create_gopay_task_record(task_api, task_id: str, acc: AccountModel, snapshot: dict) -> None:
    meta = {
        "account_id": acc.id,
        "email": acc.email,
        "gopay_session_id": snapshot.get("session_id"),
        "plan": snapshot.get("plan") or "plus",
        "country": snapshot.get("country") or "",
        "currency": snapshot.get("currency") or "",
    }
    create_standalone = getattr(task_api, "_create_standalone_task_record", None)
    store = getattr(task_api, "_task_store", None)
    if callable(create_standalone):
        create_standalone(
            task_id,
            platform="chatgpt",
            source=GOPAY_TASK_SOURCE,
            total=1,
            meta=meta,
        )
    elif store is not None:
        store.create(
            task_id,
            platform="chatgpt",
            total=1,
            source=GOPAY_TASK_SOURCE,
            meta=meta,
        )
    else:
        return

    try:
        store = getattr(task_api, "_task_store", None)
        if store is not None:
            store.mark_running(task_id)
            store.set_progress(task_id, "0/1")
    except Exception:
        pass
    _append_gopay_task_entry(
        task_api,
        task_id,
        _gopay_task_line(f"GoPay 支付任务已创建: {acc.email or acc.id}"),
    )
    _save_gopay_task_history(
        task_api,
        task_id,
        acc,
        snapshot,
        "running",
        outcome="gopay_payment_created",
    )


def _ensure_gopay_task_record(acc: AccountModel, snapshot: dict) -> tuple[Any, str, bool]:
    task_api = _get_tasks_api()
    if task_api is None or not snapshot.get("session_id"):
        return None, "", False

    task_id = str(snapshot.get("task_id") or "").strip()
    if not task_id:
        task_id = f"task_gopay_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
        snapshot["task_id"] = task_id

    with _GOPAY_TASK_LOCK:
        created = False
        if not _task_store_exists(task_api, task_id):
            _create_gopay_task_record(task_api, task_id, acc, snapshot)
            _GOPAY_TASK_CURSORS[task_id] = 0
            created = True
        else:
            _GOPAY_TASK_CURSORS.setdefault(task_id, int(snapshot.get("task_log_cursor") or 0))
        return task_api, task_id, created


def _finish_gopay_task_if_needed(task_api, task_id: str, acc: AccountModel, snapshot: dict) -> None:
    phase = str(snapshot.get("phase") or "").strip()
    if phase not in GOPAY_TERMINAL_PHASES:
        return
    if snapshot.get("task_history_saved"):
        with _GOPAY_TASK_LOCK:
            _GOPAY_TASK_FINALIZED.add(task_id)
        return

    with _GOPAY_TASK_LOCK:
        if task_id in _GOPAY_TASK_FINALIZED:
            snapshot["task_history_saved"] = True
            return
        _GOPAY_TASK_FINALIZED.add(task_id)

    last_error = str(snapshot.get("last_error") or "").strip()
    store = getattr(task_api, "_task_store", None)
    if phase == "succeeded":
        final_status = "done"
        history_status = "success"
        success = 1
        skipped = 0
        errors: list[str] = []
        error_text = ""
        summary = "GoPay 支付完成，账号状态已写入已订阅"
        outcome = "gopay_payment_success"
    elif phase == "cancelled":
        final_status = "stopped"
        history_status = "stopped"
        success = 0
        skipped = 1
        errors = []
        error_text = last_error or "GoPay 支付已取消"
        summary = error_text
        outcome = "gopay_payment_cancelled"
    else:
        final_status = "failed"
        history_status = "failed"
        success = 0
        skipped = 0
        error_text = last_error or "GoPay 支付失败"
        errors = [error_text]
        summary = error_text
        outcome = "gopay_payment_failed"

    try:
        if store is not None:
            store.set_progress(task_id, "1/1")
    except Exception:
        pass
    _append_gopay_task_entry(task_api, task_id, _gopay_task_line(summary))
    _save_gopay_task_history(
        task_api,
        task_id,
        acc,
        snapshot,
        history_status,
        error=error_text,
        outcome=outcome,
    )
    try:
        if store is not None:
            store.finish(
                task_id,
                status=final_status,
                success=success,
                skipped=skipped,
                errors=errors,
                error=error_text,
            )
            store.cleanup()
    except Exception:
        pass
    snapshot["task_history_saved"] = True


def _sync_gopay_task_snapshot(acc: AccountModel, snapshot: dict) -> None:
    task_api, task_id, created = _ensure_gopay_task_record(acc, snapshot)
    if task_api is None or not task_id:
        return

    logs = [str(line) for line in (snapshot.get("logs") or []) if str(line or "").strip()]
    with _GOPAY_TASK_LOCK:
        cursor = 0 if created else int(snapshot.get("task_log_cursor") or _GOPAY_TASK_CURSORS.get(task_id, 0) or 0)
        if cursor < 0 or cursor > len(logs):
            cursor = 0
        for line in logs[cursor:]:
            _append_gopay_task_entry(task_api, task_id, line)
        _GOPAY_TASK_CURSORS[task_id] = len(logs)
        snapshot["task_log_cursor"] = len(logs)

    _finish_gopay_task_if_needed(task_api, task_id, acc, snapshot)


def _apply_gopay_payment_status(acc: AccountModel, snapshot: dict) -> None:
    apply_payment_snapshot_status(acc, snapshot)


def _start_gopay_task_monitor(account_id: int, gopay_session_id: str) -> None:
    session_id = str(gopay_session_id or "").strip()
    if not session_id:
        return
    with _GOPAY_TASK_LOCK:
        if session_id in _GOPAY_TASK_MONITORS:
            return
        _GOPAY_TASK_MONITORS.add(session_id)

    def _worker() -> None:
        try:
            from core.db import engine
            from services.chatgpt_core.gopay_flow import get_gopay_session

            deadline = time.time() + 60 * 60
            while time.time() < deadline:
                try:
                    snapshot = get_gopay_session(session_id)
                except KeyError:
                    with Session(engine) as db:
                        acc = db.get(AccountModel, int(account_id or 0))
                        if acc is not None:
                            _persist_missing_gopay_session(acc, session_id, db)
                    break
                except Exception:
                    time.sleep(1.0)
                    continue

                with Session(engine) as db:
                    acc = db.get(AccountModel, int(account_id or 0))
                    if acc is None:
                        break
                    _persist_gopay_snapshot(acc, snapshot, db)

                if str(snapshot.get("phase") or "") in GOPAY_TERMINAL_PHASES:
                    break
                time.sleep(1.0)
        finally:
            with _GOPAY_TASK_LOCK:
                _GOPAY_TASK_MONITORS.discard(session_id)

    threading.Thread(target=_worker, daemon=True).start()


def _persist_gopay_snapshot(acc: AccountModel, snapshot: dict, session: Session) -> dict:
    extra = acc.get_extra()
    incoming = dict(snapshot or {})
    saved = extra.get("chatgpt_gopay") if isinstance(extra.get("chatgpt_gopay"), dict) else {}
    if str(saved.get("session_id") or "") == str(incoming.get("session_id") or ""):
        for key in ("task_id", "task_log_cursor", "task_history_saved"):
            if saved.get(key) and not incoming.get(key):
                incoming[key] = saved.get(key)
    _apply_gopay_payment_status(acc, incoming)
    _sync_gopay_task_snapshot(acc, incoming)
    extra["chatgpt_gopay"] = incoming
    acc.set_extra(extra)
    from datetime import datetime, timezone
    acc.updated_at = datetime.now(timezone.utc)
    session.add(acc)
    session.commit()
    return extra["chatgpt_gopay"]


def _persist_missing_gopay_session(acc: AccountModel, session_id: str, session: Session) -> dict:
    extra = acc.get_extra()
    saved = extra.get("chatgpt_gopay") if isinstance(extra.get("chatgpt_gopay"), dict) else {}
    if str(saved.get("session_id") or "") != str(session_id):
        raise HTTPException(404, "GoPay 会话不存在")
    if str(saved.get("status") or "") == "failed":
        return saved
    snapshot = dict(saved)
    if str(snapshot.get("status") or "") == "active":
        snapshot["status"] = "failed"
        snapshot["phase"] = "failed"
        snapshot["last_error"] = "GoPay 后端会话已丢失，请重新开始支付"
        logs = snapshot.get("logs") if isinstance(snapshot.get("logs"), list) else []
        snapshot["logs"] = [*logs, "GoPay 后端会话已丢失，请重新开始支付"][-500:]
        _persist_gopay_snapshot(acc, snapshot, session)
    return snapshot


def _ensure_gopay_snapshot_account(snapshot: dict, account_id: int) -> None:
    if int(snapshot.get("account_id") or 0) != int(account_id):
        raise HTTPException(404, "GoPay 会话不存在")


def _resolve_chatgpt_proxy(proxy: Optional[str] = None) -> str:
    from core.proxy_utils import resolve_default_chatgpt_proxy

    return resolve_default_chatgpt_proxy(proxy)


def _resolve_browser_auth_proxy(proxy: Optional[str] = None) -> str:
    from core.proxy_utils import normalize_proxy_url, resolve_default_chatgpt_proxy

    explicit = normalize_proxy_url(proxy)
    if explicit:
        return explicit
    try:
        return resolve_default_chatgpt_proxy(None)
    except Exception as exc:
        raise HTTPException(400, f"默认代理解析失败: {exc}") from exc


def _is_authenticated_socks_proxy(proxy: Optional[str]) -> bool:
    from urllib.parse import urlsplit

    parts = urlsplit(str(proxy or "").strip())
    return parts.scheme.lower().startswith("socks") and bool(parts.username or parts.password)


def _playwright_proxy_config_for_browser_auth(proxy: Optional[str]) -> dict[str, str] | None:
    from core.proxy_utils import build_playwright_proxy_config

    if _is_authenticated_socks_proxy(proxy):
        raise HTTPException(
            400,
            "浏览器登录不支持带账号密码认证的 SOCKS 代理，请改用 HTTP/HTTPS 代理或无认证 SOCKS 代理",
        )
    return build_playwright_proxy_config(proxy or None)


async def _browser_auth_goto(state: Any, url: Optional[str]) -> None:
    if state.page is None:
        raise HTTPException(410, "浏览器已关闭")
    state.last_error = ""
    try:
        await state.page.goto(_browser_auth_url(url), wait_until="domcontentloaded", timeout=45000)
    except Exception as exc:
        state.last_error = f"页面打开失败: {exc}"


def _resolve_optional_checkout_proxy(proxy: Optional[str] = None) -> str:
    from core.proxy_utils import resolve_default_chatgpt_proxy

    return resolve_default_chatgpt_proxy(proxy)


def _iter_chatgpt_candidate_proxies(proxy: Optional[str] = None) -> list[str]:
    from core.proxy_utils import normalize_proxy_url, resolve_task_proxy_candidates

    results: list[str] = []
    seen: set[str] = set()
    explicit = normalize_proxy_url(proxy)
    if explicit:
        results.append(explicit)
        seen.add(explicit)
    else:
        for item, _pool, _source in resolve_task_proxy_candidates(
            {},
            fallback_proxy=None,
            default_mode="global",
            target="chatgpt",
        ):
            value = normalize_proxy_url(item) or ""
            if not value or value in seen:
                continue
            seen.add(value)
            results.append(value)
    if not results:
        results.append("")
    return results


def _resolve_required_checkout_proxy(proxy: Optional[str] = None) -> str:
    try:
        candidates = [item for item in _iter_chatgpt_candidate_proxies(proxy) if str(item or "").strip()]
    except Exception as exc:
        raise HTTPException(400, f"当前没有可用代理，无法生成订阅链接: {exc}") from exc
    if not candidates:
        raise HTTPException(400, "当前没有可用代理，无法生成订阅链接")
    return str(candidates[0]).strip()


def _codex_usage_list_item(acc: AccountModel) -> dict[str, Any]:
    extra = acc.get_extra()
    chatgpt_local = extra.get("chatgpt_local") if isinstance(extra.get("chatgpt_local"), dict) else {}
    codex = chatgpt_local.get("codex") if isinstance(chatgpt_local.get("codex"), dict) else {}
    usage = codex.get("usage") if isinstance(codex.get("usage"), dict) else {}
    # 兼容早期/导入数据把 codex_* 字段放在 extra 顶层的情况。
    if not usage:
        usage = {
            key: value
            for key, value in extra.items()
            if str(key).startswith("codex_")
        }
    return {
        "id": acc.id,
        "email": acc.email,
        "status": acc.status,
        "state": str(codex.get("state") or "").strip(),
        "checked_at": str(codex.get("checked_at") or "").strip(),
        "source": str(codex.get("source") or "").strip(),
        "http_status": int(codex.get("http_status") or 0),
        "error_code": str(codex.get("error_code") or "").strip(),
        "message": str(codex.get("message") or "").strip(),
        "chatgpt_account_id": str(codex.get("chatgpt_account_id") or "").strip(),
        "usage": usage,
        "progress": build_codex_usage_progress_from_extra(usage),
    }


@router.get("/codex-usage")
def list_codex_usage(
    page: int = 1,
    page_size: int = 100,
    status: Optional[str] = None,
    email: Optional[str] = None,
    session: Session = Depends(get_session),
):
    page_value = max(1, int(page or 1))
    page_size_value = max(1, min(int(page_size or 100), 500))
    q = select(AccountModel).where(AccountModel.platform == "chatgpt")
    if status:
        statuses = [item.strip() for item in str(status or "").split(",") if item.strip()]
        if len(statuses) == 1:
            q = q.where(AccountModel.status == statuses[0])
        elif statuses:
            q = q.where(AccountModel.status.in_(statuses))
    if email:
        q = q.where(AccountModel.email.contains(str(email).strip()))
    rows = session.exec(q.order_by(AccountModel.id.desc())).all()
    total = len(rows)
    items = rows[(page_value - 1) * page_size_value:page_value * page_size_value]
    return {
        "ok": True,
        "total": total,
        "page": page_value,
        "page_size": page_size_value,
        "items": [_codex_usage_list_item(acc) for acc in items],
    }


@router.post("/{account_id}/codex-usage/refresh")
def refresh_account_codex_usage(account_id: int, req: CodexUsageRefreshReq,
                                session: Session = Depends(get_session)):
    acc = _get_account(account_id, session)
    codex_acc = _to_codex_account(acc)

    probe = probe_codex_usage_window(
        codex_acc,
        proxy=req.proxy,
        force=req.force,
        model=req.model,
    )
    codex = _persist_codex_usage_probe(acc, probe, session)
    return {
        "ok": str(probe.get("state") or "") in {"usable", "quota_exhausted"},
        "account_id": acc.id,
        "email": acc.email,
        "codex": codex,
        "probe": {key: value for key, value in probe.items() if not key.startswith("_")},
    }


@router.post("/codex-usage/refresh")
def refresh_codex_usage_batch(req: CodexUsageBatchRefreshReq,
                              session: Session = Depends(get_session)):
    selected_ids = []
    for item in req.ids or []:
        try:
            value = int(item)
        except Exception:
            continue
        if value > 0:
            selected_ids.append(value)
    selected_ids = list(dict.fromkeys(selected_ids))

    limit_value = max(1, min(int(req.limit or 50), 200))
    q = select(AccountModel).where(AccountModel.platform == "chatgpt")
    if selected_ids:
        q = q.where(AccountModel.id.in_(selected_ids))
    if req.status:
        statuses = [item.strip() for item in str(req.status or "").split(",") if item.strip()]
        if len(statuses) == 1:
            q = q.where(AccountModel.status == statuses[0])
        elif statuses:
            q = q.where(AccountModel.status.in_(statuses))
    if req.email:
        q = q.where(AccountModel.email.contains(str(req.email).strip()))

    accounts = session.exec(q.order_by(AccountModel.id.desc()).limit(limit_value)).all()
    items: list[dict[str, Any]] = []
    for acc in accounts:
        codex_acc = _to_codex_account(acc)
        try:
            probe = probe_codex_usage_window(
                codex_acc,
                proxy=req.proxy,
                force=req.force,
                model=req.model,
            )
            codex = _persist_codex_usage_probe(acc, probe, session)
            items.append(
                {
                    "ok": str(probe.get("state") or "") in {"usable", "quota_exhausted"},
                    "account_id": acc.id,
                    "email": acc.email,
                    "state": str(codex.get("state") or "").strip(),
                    "codex": codex,
                    "message": str(codex.get("message") or "").strip(),
                }
            )
        except Exception as exc:
            items.append(
                {
                    "ok": False,
                    "account_id": acc.id,
                    "email": acc.email,
                    "state": "probe_failed",
                    "message": str(exc),
                }
            )
    return {
        "ok": True,
        "count": len(items),
        "success_count": sum(1 for item in items if item.get("ok")),
        "items": items,
    }


# ── Token 刷新 ──────────────────────────────────────────────
@router.post("/{account_id}/refresh-token")
def refresh_token(account_id: int, proxy: Optional[str] = None,
                  session: Session = Depends(get_session)):
    acc = _get_account(account_id, session)
    codex_acc = _to_codex_account(acc)

    from services.chatgpt_core.token_refresh import TokenRefreshManager
    resolved_proxy = _resolve_chatgpt_proxy(proxy)
    manager = TokenRefreshManager(proxy_url=resolved_proxy or None)
    result = manager.refresh_account(codex_acc)

    if result.success:
        extra = acc.get_extra()
        extra["access_token"] = result.access_token
        if result.refresh_token:
            extra["refresh_token"] = result.refresh_token
        acc.set_extra(extra)
        acc.token = result.access_token
        from datetime import datetime
        acc.updated_at = datetime.utcnow()
        session.add(acc)
        session.commit()
        schedule_chatgpt_local_status_refresh_for_account_id(
            acc.id,
            proxy=resolved_proxy,
            reason="chatgpt_refresh_token",
        )
        return {"ok": True, "access_token": result.access_token[:40] + "..."}
    raise HTTPException(400, result.error_message)


@router.post("/{account_id}/k12-workspaces/recapture")
def recapture_k12_workspaces(account_id: int, req: K12WorkspaceRecaptureReq,
                             session: Session = Depends(get_session)):
    """Reuse saved AT/cookies to re-join K12 targets and export current workspace variants."""
    acc = _get_account(account_id, session)
    try:
        from core.config_store import config_store
        from services.chatgpt_core.k12_recapture import (
            K12_RECAPTURE_CONFIG_KEYS,
            recapture_saved_account_k12_workspaces,
        )
        from services.chatgpt_core.k12_workspace import safe_k12_error

        config = {key: config_store.get(key, "") for key in K12_RECAPTURE_CONFIG_KEYS}
        if req.join_timeout_seconds is not None:
            config["chatgpt_k12_join_timeout_seconds"] = max(5, min(int(req.join_timeout_seconds), 180))
        if req.join_retry_count is not None:
            config["chatgpt_k12_join_retry_count"] = max(0, min(int(req.join_retry_count), 5))
        if req.post_join_poll_seconds is not None:
            config["chatgpt_k12_post_join_poll_seconds"] = str(req.post_join_poll_seconds or "")
        result = recapture_saved_account_k12_workspaces(
            session=session,
            account=acc,
            config=config,
            workspace_ids=req.workspace_ids,
            save_all_spaces=req.save_all_spaces,
            strict_join=req.strict_join,
            proxy=str(req.proxy or "").strip(),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        try:
            from services.chatgpt_core.k12_workspace import safe_k12_error
            detail = safe_k12_error(exc, 300)
        except Exception:
            detail = exc.__class__.__name__
        raise HTTPException(500, f"K12 workspace 重新捕获失败: {detail}") from exc

    for changed_id in result.get("changed_account_ids") or []:
        try:
            schedule_chatgpt_local_status_refresh_for_account_id(
                int(changed_id),
                proxy=str(req.proxy or "").strip() or None,
                reason="k12_workspace_recapture",
                delay_seconds=2.0,
            )
        except Exception:
            pass
    return result


# ── 生成支付链接 ────────────────────────────────────────────
class PaymentReq(BaseModel):
    plan: str = "plus"  # plus | team
    country: str = "ID"
    currency: str = "IDR"
    proxy: Optional[str] = None
    promo_code: Optional[str] = None
    workspace_name: str = "MyTeam"
    seat_quantity: int = 5
    price_interval: str = "month"
    payment_link_format: str = "long_hosted"
    save_defaults: bool = True


class GoPayStartReq(BaseModel):
    phone_country_code: str
    phone_number: str
    access_token: Optional[str] = None
    plan: str = "plus"
    country: str = "ID"
    currency: str = "IDR"
    proxy: Optional[str] = None
    checkout_url: Optional[str] = None
    pin: Optional[str] = None
    pin_source: Optional[str] = None
    save_defaults: bool = True
    billing_name: Optional[str] = None
    billing_email: Optional[str] = None
    billing_country: Optional[str] = None
    billing_line1: Optional[str] = None
    billing_city: Optional[str] = None
    billing_state: Optional[str] = None
    billing_postal_code: Optional[str] = None
    billing_generation_context: Optional[str] = None


class GoPayBatchPhoneReq(BaseModel):
    id: str = ""
    label: str = ""
    phone_country_code: str
    phone_number: str


class GoPayBatchItemReq(BaseModel):
    account_id: int
    account: Optional[dict[str, Any]] = None
    phone: GoPayBatchPhoneReq
    batch_index: int = Field(default=0, alias="batchIndex")
    round: int = 1

    class Config:
        populate_by_name = True


class GoPayBatchStartReq(BaseModel):
    items: list[GoPayBatchItemReq]
    round_interval_seconds: int = 60
    defaults: dict[str, Any] = Field(default_factory=dict)
    otp_auto_resend_delay_seconds: Optional[int] = None


class GoPayGenerateBillingReq(BaseModel):
    country: str = "ID"
    billing_name: Optional[str] = None
    billing_email: Optional[str] = None
    billing_country: Optional[str] = None
    billing_line1: Optional[str] = None
    billing_city: Optional[str] = None
    billing_state: Optional[str] = None
    billing_postal_code: Optional[str] = None


class GoPayOtpReq(BaseModel):
    otp: str


class GoPayPinReq(BaseModel):
    pin: str


class BrowserAuthStartReq(BaseModel):
    proxy: Optional[str] = None
    url: Optional[str] = None
    fresh_profile: bool = False


class BrowserAuthNavigateReq(BaseModel):
    url: str


class BrowserAuthClickReq(BaseModel):
    x: float
    y: float


class BrowserAuthTypeReq(BaseModel):
    text: str


class BrowserAuthKeyReq(BaseModel):
    key: str


class BrowserAuthCloseReq(BaseModel):
    pass


def _billing_for_browser_injection(acc: AccountModel) -> dict[str, str]:
    defaults = _load_global_gopay_defaults()
    extra = acc.get_extra()
    last_payment = extra.get("chatgpt_last_payment_link") if isinstance(extra.get("chatgpt_last_payment_link"), dict) else {}
    last_billing = last_payment.get("billing") if isinstance(last_payment.get("billing"), dict) else {}

    def pick(key: str, default: str = "") -> str:
        return str(defaults.get(key) or last_billing.get(key.replace("billing_", "")) or default or "").strip()

    country = pick("billing_country", DEFAULT_GOPAY_BILLING["country"]).upper() or DEFAULT_GOPAY_BILLING["country"]
    return {
        "name": pick("billing_name", "John Doe"),
        "email": str(acc.email or pick("billing_email", "buyer@example.com")).strip(),
        "country": country,
        "line1": pick("billing_line1", DEFAULT_GOPAY_BILLING["line1"]),
        "city": pick("billing_city", DEFAULT_GOPAY_BILLING["city"]),
        "state": pick("billing_state", DEFAULT_GOPAY_BILLING["state"]),
        "postal_code": pick("billing_postal_code", DEFAULT_GOPAY_BILLING["postal_code"]),
    }


_BILLING_INJECT_SCRIPT = r"""
async (billing) => {
  const countryNames = {
    ID: "Indonesia", US: "United States", SG: "Singapore", DE: "Germany",
    JP: "Japan", HK: "Hong Kong", GB: "United Kingdom", AU: "Australia",
    CA: "Canada", IN: "India", BR: "Brazil", MX: "Mexico", TR: "Turkey"
  };
  const values = {
    name: billing.name || "",
    email: billing.email || "",
    country: (billing.country || "").toUpperCase(),
    countryName: countryNames[(billing.country || "").toUpperCase()] || billing.country || "",
    line1: billing.line1 || "",
    city: billing.city || "",
    state: billing.state || "",
    postal_code: billing.postal_code || ""
  };
  const fields = [
    { key: "email", value: values.email, terms: ["email", "e-mail"], negative: [] },
    { key: "name", value: values.name, terms: ["billing name", "cardholder name", "name on card", "full name", "nama", "name"], negative: ["country", "email", "address"] },
    { key: "country", value: values.country, displayValue: values.countryName, terms: ["country", "negara", "billing country"], negative: [] },
    { key: "line1", value: values.line1, terms: ["address line 1", "line1", "street", "billing address", "alamat", "address"], negative: ["line 2", "line2", "address2"] },
    { key: "city", value: values.city, terms: ["city", "kota", "locality", "address-level2"], negative: [] },
    { key: "state", value: values.state, terms: ["state", "province", "region", "provinsi", "administrative", "address-level1"], negative: [] },
    { key: "postal_code", value: values.postal_code, terms: ["postal", "postcode", "zip", "kode pos", "postal-code"], negative: [] }
  ];
  const changed = [];
  const used = new Set();
  let addressAccepted = false;

  const isUsable = (el) => {
    if (!el || used.has(el) || el.disabled || el.readOnly) return false;
    const tag = (el.tagName || "").toLowerCase();
    if (!["input", "textarea", "select"].includes(tag) && !el.isContentEditable) return false;
    if ((el.type || "").toLowerCase() === "hidden") return false;
    const style = window.getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden") return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };

  const labelText = (el) => {
    const parts = [
      el.getAttribute("name"), el.id, el.getAttribute("autocomplete"),
      el.getAttribute("aria-label"), el.getAttribute("placeholder"),
      el.getAttribute("data-testid"), el.getAttribute("type")
    ];
    if (el.id && window.CSS && CSS.escape) {
      document.querySelectorAll(`label[for="${CSS.escape(el.id)}"]`).forEach((label) => parts.push(label.textContent));
    }
    const closestLabel = el.closest && el.closest("label");
    if (closestLabel) parts.push(closestLabel.textContent);
    return parts.filter(Boolean).join(" ").replace(/\s+/g, " ").toLowerCase();
  };

  const score = (text, field) => {
    if (!text) return 0;
    if (field.negative.some((term) => text.includes(term))) return -10;
    let total = 0;
    for (const term of field.terms) {
      if (text.includes(term)) total += term.length >= 8 ? 4 : 2;
    }
    return total;
  };

  const setNativeValue = (el, value) => {
    const tag = (el.tagName || "").toLowerCase();
    if (tag === "select") {
      const wanted = String(value || "").toLowerCase();
      const display = String(values.countryName || value || "").toLowerCase();
      const option = Array.from(el.options || []).find((item) => {
        const val = String(item.value || "").toLowerCase();
        const text = String(item.textContent || "").toLowerCase();
        return val === wanted || text === wanted || text.includes(display) || display.includes(text);
      });
      if (!option) return false;
      el.value = option.value;
    } else if (el.isContentEditable) {
      el.textContent = value;
    } else {
      const proto = tag === "textarea" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const descriptor = Object.getOwnPropertyDescriptor(proto, "value");
      if (descriptor && descriptor.set) descriptor.set.call(el, value);
      else el.value = value;
    }
    for (const eventName of ["input", "change", "blur"]) {
      el.dispatchEvent(new Event(eventName, { bubbles: true }));
    }
    return true;
  };

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  const visible = (el) => {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden" || style.pointerEvents === "none") return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };

  const clickCandidate = (el) => {
    const rect = el.getBoundingClientRect();
    const x = rect.left + Math.min(Math.max(rect.width / 2, 8), Math.max(rect.width - 8, 8));
    const y = rect.top + Math.min(Math.max(rect.height / 2, 8), Math.max(rect.height - 8, 8));
    for (const eventName of ["pointerdown", "mousedown", "pointerup", "mouseup", "click"]) {
      el.dispatchEvent(new MouseEvent(eventName, {
        bubbles: true,
        cancelable: true,
        view: window,
        clientX: x,
        clientY: y
      }));
    }
  };

  const acceptAddressSuggestion = async (line1El) => {
    if (!line1El) return false;
    line1El.focus();
    line1El.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", code: "ArrowDown", bubbles: true }));
    await sleep(350);
    const selectors = [
      "[role='option']",
      "[role='listbox'] [role='option']",
      "[aria-label*='suggestion' i]",
      "[id*='autocomplete' i]",
      "[class*='autocomplete' i]",
      "[class*='suggestion' i]",
      "[data-testid*='suggestion' i]"
    ];
    const candidates = [];
    for (const selector of selectors) {
      document.querySelectorAll(selector).forEach((el) => {
        if (visible(el) && !candidates.includes(el)) candidates.push(el);
      });
    }
    const wanted = String(values.line1 || "").toLowerCase();
    const candidate = candidates.find((el) => String(el.textContent || "").toLowerCase().includes(wanted.slice(0, 12))) || candidates[0];
    if (candidate) {
      clickCandidate(candidate);
      await sleep(250);
      return true;
    }
    for (const key of ["ArrowDown", "Enter"]) {
      line1El.dispatchEvent(new KeyboardEvent("keydown", { key, code: key, bubbles: true, cancelable: true }));
      line1El.dispatchEvent(new KeyboardEvent("keyup", { key, code: key, bubbles: true, cancelable: true }));
      await sleep(150);
    }
    return true;
  };

  const controls = Array.from(document.querySelectorAll("input, textarea, select, [contenteditable='true']"));
  for (const field of fields) {
    const value = field.key === "country" ? (field.displayValue || field.value) : field.value;
    if (!value) continue;
    let best = null;
    let bestScore = 0;
    for (const el of controls) {
      if (!isUsable(el)) continue;
      const currentScore = score(labelText(el), field);
      if (currentScore > bestScore) {
        best = el;
        bestScore = currentScore;
      }
    }
    if (best && setNativeValue(best, field.key === "country" ? field.value : value)) {
      used.add(best);
      changed.push(field.key);
      if (field.key === "line1") {
        addressAccepted = await acceptAddressSuggestion(best);
      }
    }
  }
  return { changed, addressAccepted, url: location.href };
}
"""


_ADDRESS_SUGGESTION_CLICK_SCRIPT = r"""
() => {
  const visible = (el) => {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden" || style.pointerEvents === "none") return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };
  const selectors = [
    "[role='option']",
    "[role='listbox'] [role='option']",
    "[aria-label*='suggestion' i]",
    "[id*='autocomplete' i]",
    "[class*='autocomplete' i]",
    "[class*='suggestion' i]",
    "[data-testid*='suggestion' i]"
  ];
  const candidates = [];
  for (const selector of selectors) {
    document.querySelectorAll(selector).forEach((el) => {
      if (visible(el) && !candidates.includes(el)) candidates.push(el);
    });
  }
  const candidate = candidates.find((el) => String(el.textContent || "").trim()) || candidates[0];
  if (!candidate) return { clicked: false, url: location.href };
  const rect = candidate.getBoundingClientRect();
  const x = rect.left + Math.min(Math.max(rect.width / 2, 8), Math.max(rect.width - 8, 8));
  const y = rect.top + Math.min(Math.max(rect.height / 2, 8), Math.max(rect.height - 8, 8));
  for (const eventName of ["pointerdown", "mousedown", "pointerup", "mouseup", "click"]) {
    candidate.dispatchEvent(new MouseEvent(eventName, {
      bubbles: true,
      cancelable: true,
      view: window,
      clientX: x,
      clientY: y
    }));
  }
  return { clicked: true, text: String(candidate.textContent || "").replace(/\s+/g, " ").trim().slice(0, 160), url: location.href };
}
"""


class _BrowserAuthSession:
    def __init__(self, *, capture_id: str, account_id: int, proxy: str):
        self.capture_id = capture_id
        self.account_id = int(account_id)
        self.proxy = proxy
        self.last_error = ""
        self.created_at = datetime.now(timezone.utc)
        self.updated_at = self.created_at
        self.lock = asyncio.Lock()
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.user_agent = ""
        self.accept_language = "en-US,en;q=0.9"
        self.sec_ch_ua = ""
        self.viewport_width = BROWSER_AUTH_VIEWPORT_WIDTH
        self.viewport_height = BROWSER_AUTH_VIEWPORT_HEIGHT


_BROWSER_AUTH_SESSIONS: dict[str, _BrowserAuthSession] = {}


def _normalize_gopay_pin(pin: Optional[str]) -> str:
    return re.sub(r"\D", "", str(pin or ""))


def _browser_auth_url(raw: Optional[str]) -> str:
    text = str(raw or "").strip()
    if not text:
        return "https://chatgpt.com/"
    if not re.match(r"^https?://", text, re.I):
        text = "https://" + text
    return text


def _infer_chrome_major(user_agent: str) -> str:
    match = re.search(r"(?:Chrome|Chromium)/(\d+)", str(user_agent or ""))
    return match.group(1) if match else "136"


def _sec_ch_ua_for_major(major: str) -> str:
    major = re.sub(r"\D", "", str(major or "")) or "136"
    return f'"Not.A/Brand";v="99", "Chromium";v="{major}", "Google Chrome";v="{major}"'


def _cookie_header_to_playwright(cookies: str, url: str = "https://chatgpt.com/") -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for part in str(cookies or "").split(";"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name:
            continue
        out.append(
            {
                "name": name,
                "value": value,
                "url": url,
                "secure": True,
                "sameSite": "Lax",
            }
        )
    return out


def _cookies_to_header(cookies: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    for cookie in cookies:
        name = str(cookie.get("name") or "").strip()
        value = str(cookie.get("value") or "")
        if not name or name in seen:
            continue
        seen.add(name)
        parts.append(f"{name}={value}")
    return "; ".join(parts)


def _find_cookie_value(cookies: list[dict[str, Any]], names: set[str]) -> str:
    for cookie in cookies:
        name = str(cookie.get("name") or "").strip()
        if name in names:
            return str(cookie.get("value") or "")
    return ""


def _browser_auth_get(capture_id: str, account_id: int) -> _BrowserAuthSession:
    state = _BROWSER_AUTH_SESSIONS.get(str(capture_id or ""))
    if not state or int(state.account_id) != int(account_id):
        raise HTTPException(404, "浏览器登录会话不存在")
    return state


async def _browser_auth_close_state(state: _BrowserAuthSession) -> None:
    async with state.lock:
        if state.browser is not None:
            try:
                await state.browser.close()
            except Exception:
                pass
        if state.playwright is not None:
            try:
                await state.playwright.stop()
            except Exception:
                pass
        state.browser = None
        state.context = None
        state.page = None
        state.playwright = None
        state.updated_at = datetime.now(timezone.utc)
    _BROWSER_AUTH_SESSIONS.pop(state.capture_id, None)


async def _browser_auth_snapshot_locked(state: _BrowserAuthSession) -> dict[str, Any]:
    if state.page is None:
        raise HTTPException(410, "浏览器已关闭")
    page = state.page
    try:
        title = await page.title()
    except Exception:
        title = ""
    try:
        image = await page.screenshot(type="jpeg", quality=72, full_page=False, timeout=15000)
        screenshot = base64.b64encode(image).decode("ascii")
    except Exception as exc:
        screenshot = ""
        title = title or f"截图失败: {exc}"
    viewport = page.viewport_size or {"width": state.viewport_width, "height": state.viewport_height}
    state.updated_at = datetime.now(timezone.utc)
    payload = {
        "capture_id": state.capture_id,
        "account_id": state.account_id,
        "url": str(page.url or ""),
        "title": title,
        "width": int(viewport.get("width") or state.viewport_width),
        "height": int(viewport.get("height") or state.viewport_height),
        "screenshot": screenshot,
        "updated_at": state.updated_at.isoformat(),
    }
    if state.last_error:
        payload["error"] = state.last_error
    return payload


def _account_browser_fingerprint(acc: AccountModel) -> dict[str, Any]:
    from services.chatgpt_core.account_fingerprint import resolve_account_browser_fingerprint

    extra = acc.get_extra()
    resolved = resolve_account_browser_fingerprint(extra)
    if resolved:
        return {
            **resolved,
            "user_agent": resolved.get("user_agent")
            or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/136.0.7103.92 Safari/537.36",
            "accept_language": resolved.get("accept_language") or "en-US,en;q=0.9",
            "sec_ch_ua": resolved.get("sec_ch_ua") or _sec_ch_ua_for_major(
                int(resolved.get("chrome_major") or _infer_chrome_major(str(resolved.get("user_agent") or "")) or 136)
            ),
            "viewport_width": int(resolved.get("viewport_width") or 1365),
            "viewport_height": int(resolved.get("viewport_height") or 900),
            "device_id": str(resolved.get("device_id") or "").strip(),
        }
    registration_context = extra.get("chatgpt_registration_context")
    if not isinstance(registration_context, dict):
        registration_context = {}
    browser_fingerprint = registration_context.get("browser_fingerprint")
    if not isinstance(browser_fingerprint, dict):
        browser_fingerprint = {}
    user_agent = (
        str(registration_context.get("user_agent") or "").strip()
        or str(browser_fingerprint.get("user_agent") or "").strip()
        or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/136.0.7103.92 Safari/537.36"
    )
    major = _infer_chrome_major(user_agent)
    return {
        "user_agent": user_agent,
        "accept_language": (
            str(registration_context.get("accept_language") or "").strip()
            or str(browser_fingerprint.get("accept_language") or "").strip()
            or "en-US,en;q=0.9"
        ),
        "sec_ch_ua": (
            str(registration_context.get("sec_ch_ua") or "").strip()
            or str(browser_fingerprint.get("sec_ch_ua") or "").strip()
            or _sec_ch_ua_for_major(major)
        ),
        "viewport_width": int(registration_context.get("viewport_width") or browser_fingerprint.get("viewport_width") or 1365),
        "viewport_height": int(registration_context.get("viewport_height") or browser_fingerprint.get("viewport_height") or 900),
        "device_id": str(registration_context.get("device_id") or browser_fingerprint.get("device_id") or "").strip(),
    }


def _fresh_browser_fingerprint_dict() -> dict[str, Any]:
    from services.chatgpt_core.utils import generate_browser_fingerprint

    fp = generate_browser_fingerprint()
    return {
        "device_id": fp.device_id,
        "accept_language": fp.accept_language,
        "impersonate": fp.impersonate,
        "chrome_major": fp.chrome_major,
        "chrome_full_version": fp.chrome_full_version,
        "user_agent": fp.user_agent,
        "sec_ch_ua": fp.sec_ch_ua,
        "platform_version": fp.platform_version,
        "viewport_width": fp.viewport_width,
        "viewport_height": fp.viewport_height,
    }


async def _browser_auth_session_payload(page: Any) -> dict[str, Any]:
    try:
        payload = await page.evaluate(
            """
            async () => {
              try {
                const response = await fetch('/api/auth/session', { credentials: 'include' });
                if (!response.ok) return {};
                return await response.json();
              } catch (e) {
                return {};
              }
            }
            """
        )
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


async def _browser_auth_capture_to_account(state: _BrowserAuthSession, acc: AccountModel, session: Session) -> dict[str, Any]:
    async with state.lock:
        if state.page is None or state.context is None:
            raise HTTPException(410, "浏览器已关闭")
        page = state.page
        if "chatgpt.com" not in str(page.url or "").lower():
            try:
                await page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=45000)
            except Exception:
                pass
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass

        payload = await _browser_auth_session_payload(page)
        cookies = await state.context.cookies(["https://chatgpt.com/"])
        cookie_header = _cookies_to_header(cookies)
        session_token = _find_cookie_value(
            cookies,
            {
                "__Secure-next-auth.session-token",
                "__Secure-authjs.session-token",
                "next-auth.session-token",
                "authjs.session-token",
            },
        )
        device_id = _find_cookie_value(cookies, {"oai-did"}) or _find_cookie_value(cookies, {"oai-device-id"})
        access_token = str(
            payload.get("accessToken")
            or payload.get("access_token")
            or payload.get("webAccessToken")
            or ""
        ).strip()
        try:
            user_agent = str(await page.evaluate("navigator.userAgent") or state.user_agent or "").strip()
        except Exception:
            user_agent = state.user_agent
        try:
            language = str(await page.evaluate("navigator.language") or "").strip()
        except Exception:
            language = ""

    if not session_token:
        raise HTTPException(400, "未捕获到 ChatGPT session cookie，请确认浏览器里已经登录成功")
    if not access_token and not (acc.token or acc.get_extra().get("access_token")):
        raise HTTPException(400, "已捕获 cookie，但未获取 access token；请进入 ChatGPT 首页后再捕获")

    extra = acc.get_extra()
    extra["cookies"] = cookie_header
    extra["session_token"] = session_token
    if access_token:
        extra["access_token"] = access_token
        acc.token = access_token

    major = _infer_chrome_major(user_agent)
    sec_ch_ua = _sec_ch_ua_for_major(major)
    accept_language = state.accept_language
    if language:
        accept_language = f"{language},{language.split('-', 1)[0]};q=0.9" if "-" in language else f"{language};q=0.9"

    registration_context = extra.get("chatgpt_registration_context")
    if not isinstance(registration_context, dict):
        registration_context = {}
    browser_fingerprint = registration_context.get("browser_fingerprint")
    if not isinstance(browser_fingerprint, dict):
        browser_fingerprint = {}
    browser_fingerprint.update(
        {
            "user_agent": user_agent or state.user_agent,
            "accept_language": accept_language,
            "sec_ch_ua": sec_ch_ua,
            "viewport_width": state.viewport_width,
            "viewport_height": state.viewport_height,
        }
    )
    if device_id:
        browser_fingerprint["device_id"] = device_id
        registration_context["device_id"] = device_id
    registration_context.update(
        {
            "user_agent": user_agent or state.user_agent,
            "accept_language": accept_language,
            "sec_ch_ua": sec_ch_ua,
            "browser_fingerprint": browser_fingerprint,
        }
    )
    if int(major or 0) >= 136:
        registration_context["impersonate"] = "chrome136"
    extra["chatgpt_registration_context"] = registration_context
    try:
        from services.chatgpt_core.account_fingerprint import persist_account_browser_fingerprint

        extra = persist_account_browser_fingerprint(
            extra,
            browser_fingerprint,
            source="browser_auth",
            overwrite=True,
        )
    except Exception:
        pass
    extra["chatgpt_browser_auth"] = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "url": str(state.page.url if state.page is not None else ""),
        "cookies_len": len(cookie_header),
        "has_session_token": bool(session_token),
        "has_access_token": bool(access_token or acc.token),
        "device_id": device_id,
        "user_agent": user_agent or state.user_agent,
    }
    acc.set_extra(extra)
    acc.updated_at = datetime.now(timezone.utc)
    session.add(acc)
    session.commit()
    if access_token or acc.token:
        schedule_chatgpt_local_status_refresh_for_account_id(acc.id, reason="browser_auth_capture")
    return {
        "ok": True,
        "message": "浏览器登录态已保存",
        "cookies_len": len(cookie_header),
        "has_session_token": bool(session_token),
        "has_access_token": bool(access_token or acc.token),
        "url": extra["chatgpt_browser_auth"]["url"],
    }


def _load_global_gopay_defaults() -> dict:
    try:
        from core.config_store import config_store

        raw = str(config_store.get("chatgpt_gopay_defaults", "") or "").strip()
        parsed = json.loads(raw) if raw else {}
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _save_global_gopay_defaults(defaults: dict) -> None:
    from core.config_store import config_store

    config_store.set("chatgpt_gopay_defaults", json.dumps(defaults or {}, ensure_ascii=False))


def _load_gopay_recognized_country_codes() -> list[str]:
    try:
        from core.config_store import config_store

        raw = config_store.get(GOPAY_RECOGNIZED_COUNTRY_CODES_KEY, "")
        try:
            parsed = json.loads(str(raw or ""))
        except Exception:
            parsed = raw
        return normalize_gopay_recognized_country_codes(parsed)
    except Exception:
        return normalize_gopay_recognized_country_codes([])


def _merge_gopay_defaults(*defaults: dict) -> dict:
    merged: dict = {}
    for item in defaults:
        if isinstance(item, dict):
            merged.update(item)
    return merged


def _parse_bool_config(value: Any, default: bool = False) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "on", "enabled", "enable"}:
        return True
    if text in {"0", "false", "no", "off", "disabled", "disable"}:
        return False
    return default


def _parse_float_config(value: Any, default: float, *, minimum: float, maximum: float) -> float:
    try:
        parsed = float(str(value or "").strip())
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _responses_url(api_base_url: str) -> str:
    base = str(api_base_url or "").strip().rstrip("/") or DEFAULT_GOPAY_BILLING_LLM_BASE_URL
    if base.endswith("/v1"):
        return f"{base}/responses"
    return f"{base}/v1/responses"


def _chat_completions_url(api_base_url: str) -> str:
    base = str(api_base_url or "").strip().rstrip("/") or DEFAULT_GOPAY_BILLING_LLM_BASE_URL
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


def _extract_json_object(text: str) -> dict:
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`").strip()
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            raise
        parsed = json.loads(raw[start:end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("模型返回不是 JSON 对象")
    return parsed


def _extract_llm_response_text(payload: dict) -> str:
    if not isinstance(payload, dict):
        return ""
    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    texts: list[str] = []
    output = payload.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if isinstance(content, str) and content.strip():
                texts.append(content.strip())
            if isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    text = part.get("text") or part.get("content")
                    if isinstance(text, str) and text.strip():
                        texts.append(text.strip())
    if texts:
        return "\n".join(texts).strip()

    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0] if isinstance(choices[0], dict) else {}
        message = first.get("message") if isinstance(first.get("message"), dict) else {}
        content = message.get("content") or first.get("text")
        if isinstance(content, str):
            return content.strip()
    return ""


def _parse_llm_http_response_payload(response: Any) -> dict:
    try:
        payload = response.json()
        return payload if isinstance(payload, dict) else {}
    except ValueError:
        raw = str(getattr(response, "text", "") or "")

    sse_payloads: list[dict[str, Any]] = []
    output_text_parts: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            item = json.loads(data)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict):
            continue
        sse_payloads.append(item)
        event_type = str(item.get("type") or "").strip()
        if event_type == "response.output_text.delta":
            delta = item.get("delta")
            if isinstance(delta, str):
                output_text_parts.append(delta)
        elif event_type == "response.output_text.done":
            text = item.get("text")
            if isinstance(text, str):
                output_text_parts = [text]

    if output_text_parts:
        return {"output_text": "".join(output_text_parts)}
    for item in reversed(sse_payloads):
        response_payload = item.get("response")
        if isinstance(response_payload, dict):
            text = _extract_llm_response_text(response_payload)
            if text:
                return response_payload
        text = _extract_llm_response_text(item)
        if text:
            return item

    snippet = raw.strip()[:300]
    raise ValueError(f"模型响应不是 JSON/SSE: {snippet or '<empty>'}")


def _load_gopay_billing_llm_config() -> dict[str, Any]:
    import os

    try:
        from core.config_store import config_store
    except Exception:
        config_store = None

    def get_first(keys: list[str], default: str = "") -> str:
        if config_store is None:
            return default
        for key in keys:
            value = str(config_store.get(key, "") or "").strip()
            if value:
                return value
        return default

    api_key = (
        get_first([
            "chatgpt_gopay_billing_llm_api_key",
            "chatgpt_llm_api_key",
            "openai_api_key",
        ])
        or str(os.environ.get("OPENAI_API_KEY") or "").strip()
    )
    prompt = get_first(["chatgpt_gopay_billing_llm_prompt"], DEFAULT_GOPAY_BILLING_LLM_PROMPT)
    if "谷歌地图" not in prompt and "Google Maps" not in prompt:
        prompt = f"{DEFAULT_GOPAY_BILLING_LLM_PROMPT}\n{prompt}".strip()

    return {
        "enabled": _parse_bool_config(get_first(["chatgpt_gopay_billing_llm_enabled"], "true"), default=True),
        "api_base_url": get_first(
            ["chatgpt_gopay_billing_llm_base_url", "chatgpt_llm_api_base_url"],
            DEFAULT_GOPAY_BILLING_LLM_BASE_URL,
        ).rstrip("/"),
        "api_key": api_key,
        "model": get_first(["chatgpt_gopay_billing_llm_model", "chatgpt_llm_model"], DEFAULT_GOPAY_BILLING_LLM_MODEL),
        "wire_api": get_first(["chatgpt_gopay_billing_llm_wire_api"], DEFAULT_GOPAY_BILLING_LLM_WIRE_API).lower(),
        "country_strategy": get_first(
            ["chatgpt_gopay_billing_llm_country_strategy"],
            DEFAULT_GOPAY_BILLING_LLM_COUNTRY_STRATEGY,
        ).lower(),
        "fixed_country": get_first(
            ["chatgpt_gopay_billing_llm_fixed_country"],
            DEFAULT_GOPAY_BILLING_LLM_FIXED_COUNTRY,
        ).upper(),
        "reasoning_effort": get_first(
            ["chatgpt_gopay_billing_llm_reasoning_effort"],
            DEFAULT_GOPAY_BILLING_LLM_REASONING_EFFORT,
        ),
        "timeout_seconds": _parse_float_config(
            get_first(["chatgpt_gopay_billing_llm_timeout_seconds"], str(DEFAULT_GOPAY_BILLING_LLM_TIMEOUT_SECONDS)),
            DEFAULT_GOPAY_BILLING_LLM_TIMEOUT_SECONDS,
            minimum=5.0,
            maximum=180.0,
        ),
        "prompt": prompt,
    }


def _normalize_llm_billing_country(value: Any, target_country: str) -> str:
    target = str(target_country or "").strip().upper()
    if target:
        return target
    text = str(value or "").strip()
    country_map = {
        "INDONESIA": "ID",
        "UNITED STATES": "US",
        "USA": "US",
        "UNITED STATES OF AMERICA": "US",
        "SINGAPORE": "SG",
        "GERMANY": "DE",
        "JAPAN": "JP",
        "HONG KONG": "HK",
        "UNITED KINGDOM": "GB",
        "GREAT BRITAIN": "GB",
        "AUSTRALIA": "AU",
        "CANADA": "CA",
        "INDIA": "IN",
        "BRAZIL": "BR",
        "MEXICO": "MX",
        "TURKEY": "TR",
    }
    upper = text.upper()
    return country_map.get(upper, upper or DEFAULT_GOPAY_BILLING["country"])


def _normalize_gopay_billing_address_payload(payload: dict, target_country: str) -> dict[str, str]:
    def pick(*keys: str) -> str:
        for key in keys:
            value = payload.get(key)
            if value is None:
                continue
            text = str(value).strip()
            if text:
                return text
        return ""

    address = {
        "name": pick("billing_name", "name", "full_name"),
        "country": _normalize_llm_billing_country(pick("country", "billing_country"), target_country),
        "line1": pick("line1", "billing_line1", "address", "street_address", "street"),
        "city": pick("city", "billing_city", "locality"),
        "state": pick("state", "billing_state", "province", "region"),
        "postal_code": pick("postal_code", "billing_postal_code", "zip", "postcode"),
    }
    missing = [key for key, value in address.items() if not str(value or "").strip()]
    if missing:
        raise ValueError(f"模型返回缺少字段: {', '.join(missing)}")
    return address


def _billing_address_response_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "billing_name": {"type": "string"},
            "country": {"type": "string"},
            "line1": {"type": "string"},
            "city": {"type": "string"},
            "state": {"type": "string"},
            "postal_code": {"type": "string"},
        },
        "required": ["billing_name", "country", "line1", "city", "state", "postal_code"],
    }


def _resolve_gopay_billing_target_country(
    cfg: dict[str, Any],
    *,
    checkout_country: str,
    billing_country: str,
) -> tuple[str, str]:
    strategy = str(cfg.get("country_strategy") or DEFAULT_GOPAY_BILLING_LLM_COUNTRY_STRATEGY).strip().lower()
    if strategy not in GOPAY_BILLING_LLM_COUNTRY_STRATEGIES:
        strategy = DEFAULT_GOPAY_BILLING_LLM_COUNTRY_STRATEGY

    if strategy == "checkout_country":
        target = str(checkout_country or "").strip().upper()
    elif strategy == "fixed_country":
        target = str(cfg.get("fixed_country") or "").strip().upper()
    else:
        target = str(billing_country or "").strip().upper()

    if not target:
        target = str(billing_country or checkout_country or DEFAULT_GOPAY_BILLING["country"]).strip().upper()
    return target or DEFAULT_GOPAY_BILLING["country"], strategy


def _gopay_generation_batch_index(generation_context: str) -> Optional[int]:
    context = str(generation_context or "").strip()
    match = re.search(r"(?:^|[;,\s])batch_index=(\d+)", context)
    if not match:
        return None
    try:
        value = int(match.group(1))
    except ValueError:
        return None
    return value if value > 0 else None


def _gopay_billing_diversity_hint(target_country: str, generation_context: str) -> str:
    context = str(generation_context or "").strip()
    if not context:
        return ""
    pools = {
        "ID": [
            "Jakarta Selatan",
            "Jakarta Pusat",
            "Bandung",
            "Surabaya",
            "Denpasar",
            "Medan",
            "Yogyakarta",
            "Semarang",
            "Tangerang Selatan",
            "Makassar",
        ],
        "US": [
            "Seattle, WA",
            "Austin, TX",
            "San Diego, CA",
            "Denver, CO",
            "Chicago, IL",
            "Boston, MA",
            "Atlanta, GA",
            "Portland, OR",
            "Phoenix, AZ",
            "Miami, FL",
        ],
        "SG": [
            "Raffles Place",
            "Orchard",
            "Tanjong Pagar",
            "Bugis",
            "Novena",
            "Jurong East",
            "Tampines",
            "Paya Lebar",
        ],
        "JP": ["Tokyo", "Osaka", "Yokohama", "Nagoya", "Fukuoka", "Sapporo", "Kyoto", "Kobe"],
        "GB": ["London", "Manchester", "Birmingham", "Leeds", "Glasgow", "Edinburgh", "Bristol", "Liverpool"],
        "DE": ["Berlin", "Munich", "Hamburg", "Cologne", "Frankfurt", "Stuttgart", "Dusseldorf", "Leipzig"],
        "AU": ["Sydney", "Melbourne", "Brisbane", "Perth", "Adelaide", "Canberra", "Gold Coast", "Hobart"],
        "CA": ["Toronto", "Vancouver", "Montreal", "Calgary", "Ottawa", "Edmonton", "Quebec City", "Winnipeg"],
    }
    pool = pools.get(str(target_country or "").strip().upper())
    if not pool:
        return ""
    batch_index = _gopay_generation_batch_index(context)
    if batch_index:
        return pool[(batch_index - 1) % len(pool)]
    digest = hashlib.sha256(f"{target_country}:{context}".encode("utf-8")).digest()
    return pool[int.from_bytes(digest[:4], "big") % len(pool)]


def _gopay_billing_name_diversity_hint(generation_context: str) -> str:
    context = str(generation_context or "").strip()
    if not context:
        return ""
    first_names = [
        "Avery",
        "Blake",
        "Cameron",
        "Dylan",
        "Elliot",
        "Finley",
        "Grant",
        "Hayden",
        "Isaac",
        "Julian",
        "Kai",
        "Landon",
        "Miles",
        "Noah",
        "Owen",
        "Parker",
        "Quinn",
        "Rowan",
        "Sawyer",
        "Theo",
    ]
    last_names = [
        "Anderson",
        "Bennett",
        "Carlisle",
        "Donovan",
        "Ellis",
        "Foster",
        "Grayson",
        "Hayes",
        "Irving",
        "Jensen",
        "Keaton",
        "Lawson",
        "Morgan",
        "Nolan",
        "Owens",
        "Porter",
        "Reed",
        "Sullivan",
        "Turner",
        "Walker",
    ]
    batch_index = _gopay_generation_batch_index(context)
    if batch_index:
        first = first_names[(batch_index - 1) % len(first_names)]
        last = last_names[((batch_index - 1) * 7) % len(last_names)]
        return f"{first} {last}"
    digest = hashlib.sha256(context.encode("utf-8")).digest()
    first = first_names[digest[0] % len(first_names)]
    last = last_names[digest[1] % len(last_names)]
    return f"{first} {last}"


def _call_gopay_billing_address_llm(
    fallback_billing: dict[str, str],
    *,
    checkout_country: str = "",
    generation_context: str = "",
    force: bool = False,
) -> Optional[tuple[dict[str, str], str, str]]:
    cfg = _load_gopay_billing_llm_config()
    if (not cfg["enabled"] and not force) or not cfg["api_key"]:
        return None

    target_country, country_strategy = _resolve_gopay_billing_target_country(
        cfg,
        checkout_country=checkout_country,
        billing_country=str(fallback_billing.get("country") or DEFAULT_GOPAY_BILLING["country"]),
    )
    context_text = str(generation_context or "").strip()
    diversity_hint = _gopay_billing_diversity_hint(target_country, context_text)
    name_hint = _gopay_billing_name_diversity_hint(context_text)
    fallback_address_text = ", ".join(
        str(fallback_billing.get(key) or "").strip()
        for key in ("line1", "city", "state", "postal_code", "country")
        if str(fallback_billing.get(key) or "").strip()
    )
    content = (
        f"{cfg['prompt']}\n\n"
        f"目标账单国家/地区代码: {target_country}\n"
        "只返回 JSON，不要解释，不要 Markdown。字段必须且只能包含: "
        "billing_name, country, line1, city, state, postal_code。\n"
        "要求:\n"
        "- 地址在谷歌地图中能找到对应的位置。\n"
        "- billing_name 生成一个自然真实的英文姓名。\n"
        "- 不要生成 billing_email，邮箱由系统保留。\n"
        "- line1 使用街道门牌地址，不要包含城市、州省、邮编。\n"
        "- country 使用目标账单国家/地区代码。\n"
        "- postal_code 使用对应城市真实邮编格式。"
    )
    if context_text:
        content = (
            f"{content}\n\n"
            f"本次请求唯一上下文: {context_text}\n"
            "请把唯一上下文作为选择姓名、城市、街道和门牌号的随机依据；"
            "批量支付中每个账号都必须生成不同的真实姓名和不同的真实账单地址。\n"
            f"- 不要返回与当前兜底/旧地址相同或高度相似的地址: {fallback_address_text}。\n"
            "- 不要使用过于常见的示例地址、地标首页地址或默认地址。"
        )
        if diversity_hint:
            content = f"{content}\n- 优先在这个城市/区域附近选择真实可查地址: {diversity_hint}。"
        if name_hint:
            content = (
                f"{content}\n- billing_name 的姓名风格种子: {name_hint}；"
                "可以用相近的自然英文姓名，但同一批次不要复用其他账号姓名。"
            )
    system_text = "你是 GoPay/Stripe 账单地址生成器。你只输出可解析的 JSON 对象。"
    headers = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
    }

    if cfg["wire_api"] == "responses":
        url = _responses_url(cfg["api_base_url"])
        body: dict[str, Any] = {
            "model": cfg["model"],
            "instructions": system_text,
            "input": [
                {"role": "system", "content": [{"type": "input_text", "text": system_text}]},
                {"role": "user", "content": [{"type": "input_text", "text": content}]},
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "gopay_billing_address",
                    "schema": _billing_address_response_schema(),
                    "strict": True,
                }
            },
            "store": False,
        }
        reasoning_effort = str(cfg.get("reasoning_effort") or "").strip()
        if reasoning_effort:
            body["reasoning"] = {"effort": reasoning_effort}
    else:
        url = _chat_completions_url(cfg["api_base_url"])
        body = {
            "model": cfg["model"],
            "messages": [
                {"role": "system", "content": system_text},
                {"role": "user", "content": content},
            ],
            "response_format": {"type": "json_object"},
        }

    import requests

    last_error: Optional[Exception] = None
    context_digest = hashlib.sha256(context_text.encode("utf-8")).digest() if context_text else b"\0"
    for attempt in range(3):
        try:
            response = requests.post(url, json=body, headers=headers, timeout=float(cfg["timeout_seconds"]))
            if response.status_code >= 400 and "reasoning" in body:
                retry_body = dict(body)
                retry_body.pop("reasoning", None)
                response = requests.post(url, json=retry_body, headers=headers, timeout=float(cfg["timeout_seconds"]))
            if response.status_code >= 400:
                raise RuntimeError(f"LLM HTTP {response.status_code}: {str(response.text or '')[:500]}")

            payload = _parse_llm_http_response_payload(response)
            text = _extract_llm_response_text(payload)
            if not text:
                raise ValueError("模型响应中没有文本内容")
            parsed = _extract_json_object(text)
            return _normalize_gopay_billing_address_payload(parsed, target_country), target_country, country_strategy
        except Exception as exc:
            last_error = exc
            if attempt >= 2:
                break
            jitter = (context_digest[attempt % len(context_digest)] / 255.0) * 0.5
            time.sleep(0.8 * (attempt + 1) + jitter)
    if last_error:
        raise last_error
    return None


def _build_gopay_billing_from_request(req: GoPayStartReq, saved_defaults: dict, acc: AccountModel) -> dict[str, str]:
    return {
        "name": str(req.billing_name or saved_defaults.get("billing_name") or "").strip(),
        "email": str(req.billing_email or saved_defaults.get("billing_email") or acc.email or "").strip(),
        "country": str(req.billing_country or saved_defaults.get("billing_country") or DEFAULT_GOPAY_BILLING["country"]).strip().upper() or DEFAULT_GOPAY_BILLING["country"],
        "line1": str(req.billing_line1 or saved_defaults.get("billing_line1") or DEFAULT_GOPAY_BILLING["line1"]).strip(),
        "city": str(req.billing_city or saved_defaults.get("billing_city") or DEFAULT_GOPAY_BILLING["city"]).strip(),
        "state": str(req.billing_state or saved_defaults.get("billing_state") or DEFAULT_GOPAY_BILLING["state"]).strip(),
        "postal_code": str(req.billing_postal_code or saved_defaults.get("billing_postal_code") or DEFAULT_GOPAY_BILLING["postal_code"]).strip(),
    }


def _resolve_gopay_billing(
    req: GoPayStartReq,
    saved_defaults: dict,
    acc: AccountModel,
    *,
    checkout_country: str = "",
) -> tuple[dict[str, str], str, str, str, str]:
    fallback = _build_gopay_billing_from_request(req, saved_defaults, acc)
    try:
        generated = _call_gopay_billing_address_llm(
            fallback,
            checkout_country=checkout_country,
            generation_context=str(req.billing_generation_context or "").strip(),
        )
    except Exception as exc:
        return fallback, "manual_fallback", "", "", str(exc)
    if not generated:
        return fallback, "manual", "", "", ""

    generated_address, target_country, country_strategy = generated
    billing = dict(fallback)
    billing.update(generated_address)
    billing["email"] = fallback["email"]
    return billing, "llm", target_country, country_strategy, ""


def _resolve_gopay_billing_for_manual_generation(
    req: GoPayGenerateBillingReq,
    acc: AccountModel,
) -> tuple[dict[str, str], str, str]:
    fallback = {
        "name": str(req.billing_name or "").strip(),
        "email": str(req.billing_email or acc.email or "").strip(),
        "country": str(req.billing_country or DEFAULT_GOPAY_BILLING["country"]).strip().upper() or DEFAULT_GOPAY_BILLING["country"],
        "line1": str(req.billing_line1 or DEFAULT_GOPAY_BILLING["line1"]).strip(),
        "city": str(req.billing_city or DEFAULT_GOPAY_BILLING["city"]).strip(),
        "state": str(req.billing_state or DEFAULT_GOPAY_BILLING["state"]).strip(),
        "postal_code": str(req.billing_postal_code or DEFAULT_GOPAY_BILLING["postal_code"]).strip(),
    }
    generated = _call_gopay_billing_address_llm(
        fallback,
        checkout_country=str(req.country or "").strip().upper(),
        force=True,
    )
    if not generated:
        raise HTTPException(400, "请先启用 GoPay 账单地址 LLM 并填写 API Key")
    generated_address, target_country, country_strategy = generated
    billing = dict(fallback)
    billing.update(generated_address)
    billing["email"] = fallback["email"]
    return billing, target_country, country_strategy


def _load_gopay_otp_auto_resend_delay_seconds() -> int:
    try:
        from core.config_store import config_store

        value = int(str(config_store.get(GOPAY_OTP_AUTO_RESEND_DELAY_SECONDS_KEY, "") or "").strip())
    except Exception:
        value = DEFAULT_GOPAY_OTP_AUTO_RESEND_DELAY_SECONDS
    return max(0, min(value, 3600))


def _save_gopay_otp_auto_resend_delay_seconds(value: Any) -> int:
    try:
        delay = int(value)
    except Exception:
        delay = DEFAULT_GOPAY_OTP_AUTO_RESEND_DELAY_SECONDS
    delay = max(0, min(delay, 3600))
    from core.config_store import config_store

    config_store.set(GOPAY_OTP_AUTO_RESEND_DELAY_SECONDS_KEY, str(delay))
    return delay


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_gopay_batch_tasks_unlocked() -> dict[str, dict]:
    from core.config_store import config_store

    raw = str(config_store.get(GOPAY_BATCH_TASKS_KEY, "") or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _save_gopay_batch_tasks_unlocked(tasks: dict[str, dict]) -> None:
    from core.config_store import config_store

    # Keep config_store compact enough for UI restoration while preserving active tasks.
    items = list((tasks or {}).items())
    active = {task_id for task_id, task in items if str(task.get("status") or "") in GOPAY_BATCH_ACTIVE_STATUSES}
    if len(items) > 20:
        items.sort(key=lambda pair: str(pair[1].get("updated_at") or pair[1].get("created_at") or ""))
        keep = set(active)
        keep.update(task_id for task_id, _ in items[-20:])
        tasks = {task_id: task for task_id, task in tasks.items() if task_id in keep}
    config_store.set(GOPAY_BATCH_TASKS_KEY, json.dumps(tasks or {}, ensure_ascii=False))


def _load_gopay_batch_task(task_id: str) -> dict:
    task_id = str(task_id or "").strip()
    if not task_id:
        raise HTTPException(404, "GoPay 批量任务不存在")
    with _GOPAY_BATCH_LOCK:
        task = _load_gopay_batch_tasks_unlocked().get(task_id)
        if not isinstance(task, dict):
            raise HTTPException(404, "GoPay 批量任务不存在")
        return task


def _set_gopay_batch_active_id(task_id: str) -> None:
    from core.config_store import config_store

    config_store.set(GOPAY_BATCH_ACTIVE_TASK_KEY, str(task_id or "").strip())


def _get_gopay_batch_active_id() -> str:
    from core.config_store import config_store

    return str(config_store.get(GOPAY_BATCH_ACTIVE_TASK_KEY, "") or "").strip()


def _compact_gopay_snapshot(snapshot: Any) -> dict:
    if not isinstance(snapshot, dict):
        return {}
    compact = dict(snapshot)
    logs = compact.get("logs")
    if isinstance(logs, list):
        compact["logs"] = logs[-80:]
    return compact


def _gopay_batch_item_status_from_snapshot(snapshot: dict) -> str:
    phase = str((snapshot or {}).get("phase") or "").strip()
    if phase == "succeeded":
        return "done"
    if phase == "failed":
        return "failed"
    if phase == "cancelled":
        return "cancelled"
    return "running"


def _gopay_batch_recalculate(task: dict) -> dict:
    items = task.get("items") if isinstance(task.get("items"), list) else []
    for item in items:
        if not isinstance(item, dict):
            continue
        snapshot = item.get("snapshot") if isinstance(item.get("snapshot"), dict) else {}
        phase = str(snapshot.get("phase") or "").strip()
        if phase in GOPAY_TERMINAL_PHASES:
            item["status"] = _gopay_batch_item_status_from_snapshot(snapshot)
        item["batchIndex"] = int(item.get("batchIndex") or item.get("batch_index") or 0)
        item["batch_index"] = item["batchIndex"]
    task["total"] = len(items)
    task["success"] = sum(1 for item in items if str(item.get("status") or "") == "done")
    task["failed"] = sum(1 for item in items if str(item.get("status") or "") == "failed")
    task["cancelled"] = sum(1 for item in items if str(item.get("status") or "") == "cancelled")
    task["updated_at"] = _utcnow_iso()
    return task


def _save_gopay_batch_task(task: dict) -> dict:
    task_id = str(task.get("task_id") or "").strip()
    if not task_id:
        raise HTTPException(500, "GoPay 批量任务缺少 task_id")
    with _GOPAY_BATCH_LOCK:
        tasks = _load_gopay_batch_tasks_unlocked()
        task = _gopay_batch_recalculate(dict(task))
        tasks[task_id] = task
        _save_gopay_batch_tasks_unlocked(tasks)
        if str(task.get("status") or "") in GOPAY_BATCH_ACTIVE_STATUSES:
            _set_gopay_batch_active_id(task_id)
        elif _get_gopay_batch_active_id() == task_id:
            _set_gopay_batch_active_id("")
        return task


def _mutate_gopay_batch_task(task_id: str, mutator) -> dict:
    with _GOPAY_BATCH_LOCK:
        tasks = _load_gopay_batch_tasks_unlocked()
        task = tasks.get(str(task_id or "").strip())
        if not isinstance(task, dict):
            raise HTTPException(404, "GoPay 批量任务不存在")
        mutator(task)
        task = _gopay_batch_recalculate(task)
        tasks[str(task_id)] = task
        _save_gopay_batch_tasks_unlocked(tasks)
        if str(task.get("status") or "") in GOPAY_BATCH_ACTIVE_STATUSES:
            _set_gopay_batch_active_id(str(task_id))
        elif _get_gopay_batch_active_id() == str(task_id):
            _set_gopay_batch_active_id("")
        return task


def _find_gopay_batch_item(task: dict, account_id: int) -> dict:
    for item in task.get("items") or []:
        if isinstance(item, dict) and int(item.get("account_id") or 0) == int(account_id):
            return item
    raise HTTPException(404, "GoPay 批量任务项不存在")


def _gopay_batch_item_is_active(item: dict) -> bool:
    status = str(item.get("status") or "").strip()
    if status == "starting":
        return True
    snapshot = item.get("snapshot") if isinstance(item.get("snapshot"), dict) else {}
    phase = str(snapshot.get("phase") or "").strip()
    return status == "running" and (not phase or phase in GOPAY_ACTIVE_PHASES)


def _gopay_batch_all_terminal(items: list[dict]) -> bool:
    if not items:
        return True
    return all(str(item.get("status") or "") in GOPAY_BATCH_TERMINAL_STATUSES for item in items)


def _gopay_batch_phone_key(item: dict) -> str:
    phone = item.get("phone") if isinstance(item.get("phone"), dict) else {}
    country_code = re.sub(r"\D", "", str(phone.get("phone_country_code") or ""))
    number = re.sub(r"\D", "", str(phone.get("phone_number") or ""))
    return f"{country_code}:{number}" if country_code and number else ""


def _gopay_batch_phone_is_consumed(item: dict) -> bool:
    """Return true once the flow reached GoPay/Midtrans phone-linking territory."""
    snapshot = item.get("snapshot") if isinstance(item.get("snapshot"), dict) else {}
    phase = str(snapshot.get("phase") or "").strip()
    if phase in GOPAY_BATCH_CONSUMED_PHONE_PHASES:
        return True
    if snapshot.get("snap_token") or snapshot.get("reference_id") or snapshot.get("otp_waiting_since") or snapshot.get("charge_ref"):
        return True
    return False


def _gopay_batch_item_releases_phone(item: dict) -> bool:
    if str(item.get("status") or "") != "failed":
        return False
    return not _gopay_batch_phone_is_consumed(item)


def _gopay_batch_active_phone_keys(items: list[dict]) -> set[str]:
    keys: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        if _gopay_batch_item_is_active(item) or _gopay_batch_phone_is_consumed(item):
            key = _gopay_batch_phone_key(item)
            if key:
                keys.add(key)
    return keys


def _gopay_batch_released_phone_items(items: list[dict]) -> list[dict]:
    active_or_consumed = _gopay_batch_active_phone_keys(items)
    released: list[dict] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict) or not _gopay_batch_item_releases_phone(item):
            continue
        key = _gopay_batch_phone_key(item)
        if not key or key in active_or_consumed or key in seen:
            continue
        released.append(item)
        seen.add(key)
    return released


def _gopay_batch_promote_with_released_phones(task: dict) -> list[int]:
    items = [item for item in (task.get("items") or []) if isinstance(item, dict)]
    released = _gopay_batch_released_phone_items(items)
    queued = [item for item in items if str(item.get("status") or "") == "queued"]
    if not released or not queued:
        return []

    promoted: list[int] = []
    changed = False
    now = _utcnow_iso()
    released_iter = iter(released)
    for item in queued:
        try:
            source = next(released_iter)
        except StopIteration:
            break
        item["phone"] = dict(source.get("phone") or {})
        item["round"] = max(1, int(task.get("current_round") or source.get("round") or item.get("round") or 1))
        note = (
            f"手机号由失败账号 {source.get('email') or source.get('account_id')} 递延: "
            f"{source.get('error') or '前序账号未消耗手机号'}"
        )
        item["phone_deferred_from_account_id"] = int(source.get("account_id") or 0)
        item["phone_deferred_reason"] = note
        item["updated_at"] = now
        logs = item.get("logs") if isinstance(item.get("logs"), list) else []
        item["logs"] = [*logs, note][-50:]
        account_id = int(item.get("account_id") or 0)
        if account_id:
            promoted.append(account_id)
        changed = True

    if changed:
        task["next_round_at"] = None
        task["message"] = "GoPay 批量支付任务运行中，已递延未消耗的手机号"
    return promoted


def _build_gopay_batch_start_request(task: dict, item: dict) -> GoPayStartReq:
    defaults = task.get("defaults") if isinstance(task.get("defaults"), dict) else {}
    phone = item.get("phone") if isinstance(item.get("phone"), dict) else {}
    account = item.get("account") if isinstance(item.get("account"), dict) else {}
    account_email = str(item.get("email") or account.get("email") or "").strip()
    phone_country_code = str(phone.get("phone_country_code") or "").strip()
    phone_number = str(phone.get("phone_number") or "").strip()
    return GoPayStartReq(
        phone_country_code=phone_country_code,
        phone_number=phone_number,
        pin=str(defaults.get("pin") or "").strip(),
        access_token=str(defaults.get("access_token") or "").strip(),
        proxy=str(defaults.get("proxy") or "").strip(),
        save_defaults=False,
        plan="plus",
        country=str(defaults.get("country") or "ID").strip() or "ID",
        currency=str(defaults.get("currency") or "IDR").strip() or "IDR",
        checkout_url="",
        billing_name=str(defaults.get("billing_name") or "").strip(),
        billing_email=account_email or str(defaults.get("billing_email") or "").strip(),
        billing_country=str(defaults.get("billing_country") or "US").strip() or "US",
        billing_line1=str(defaults.get("billing_line1") or "").strip(),
        billing_city=str(defaults.get("billing_city") or "").strip(),
        billing_state=str(defaults.get("billing_state") or "").strip(),
        billing_postal_code=str(defaults.get("billing_postal_code") or "").strip(),
        billing_generation_context="; ".join(
            part
            for part in (
                f"batch_id={task.get('task_id') or ''}",
                f"batch_index={item.get('batch_index') or item.get('batchIndex') or ''}",
                f"round={item.get('round') or ''}",
                f"account_id={item.get('account_id') or ''}",
                f"account_email={account_email}",
                f"phone=+{phone_country_code} {phone_number}",
            )
            if not part.endswith("=") and not part.endswith("=+ ")
        ),
    )


def _refresh_gopay_batch_active_items(task_id: str) -> dict:
    from core.db import engine

    task = _load_gopay_batch_task(task_id)
    changed = False
    items = [item for item in (task.get("items") or []) if isinstance(item, dict) and _gopay_batch_item_is_active(item)]
    if not items:
        return task
    with Session(engine) as db:
        for item in items:
            snapshot = item.get("snapshot") if isinstance(item.get("snapshot"), dict) else {}
            session_id = str(snapshot.get("session_id") or "").strip()
            if not session_id:
                continue
            try:
                latest = get_gopay_payment(int(item.get("account_id") or 0), session_id, session=db)
                item["snapshot"] = _compact_gopay_snapshot(latest)
                item["status"] = _gopay_batch_item_status_from_snapshot(latest)
                item["error"] = str((latest or {}).get("last_error") or "")
            except HTTPException as exc:
                item["status"] = "failed"
                item["error"] = str(exc.detail or "刷新 GoPay 会话失败")
            except Exception as exc:
                item["error"] = str(exc) or "刷新 GoPay 会话失败"
            item["updated_at"] = _utcnow_iso()
            changed = True
    return _save_gopay_batch_task(task) if changed else task


def _start_gopay_batch_item(task_id: str, account_id: int) -> None:
    from core.db import engine

    def mark_starting(task: dict) -> None:
        item = _find_gopay_batch_item(task, account_id)
        if str(item.get("status") or "") != "queued":
            return
        item["status"] = "starting"
        item["error"] = ""
        item["started_at"] = _utcnow_iso()
        item["updated_at"] = item["started_at"]

    _mutate_gopay_batch_task(task_id, mark_starting)
    try:
        task = _load_gopay_batch_task(task_id)
        item = _find_gopay_batch_item(task, account_id)
        if str(item.get("status") or "") != "starting":
            return
        req = _build_gopay_batch_start_request(task, item)
        with Session(engine) as db:
            snapshot = start_gopay_payment(account_id, req, session=db)

        def mark_running(next_task: dict) -> None:
            next_item = _find_gopay_batch_item(next_task, account_id)
            if str(next_task.get("status") or "") == "cancelled":
                next_item["status"] = "cancelled"
                return
            next_item["snapshot"] = _compact_gopay_snapshot(snapshot)
            next_item["status"] = _gopay_batch_item_status_from_snapshot(snapshot)
            next_item["error"] = str((snapshot or {}).get("last_error") or "")
            next_item["updated_at"] = _utcnow_iso()

        _mutate_gopay_batch_task(task_id, mark_running)
    except HTTPException as exc:
        message = str(exc.detail or "启动失败")

        def mark_failed(task: dict) -> None:
            item = _find_gopay_batch_item(task, account_id)
            item["status"] = "failed"
            item["error"] = message
            item["updated_at"] = _utcnow_iso()

        _mutate_gopay_batch_task(task_id, mark_failed)
    except Exception as exc:
        message = str(exc) or "启动失败"

        def mark_failed(task: dict) -> None:
            item = _find_gopay_batch_item(task, account_id)
            item["status"] = "failed"
            item["error"] = message
            item["updated_at"] = _utcnow_iso()

        _mutate_gopay_batch_task(task_id, mark_failed)


def _start_gopay_batch_round(task_id: str, round_number: int) -> None:
    task = _load_gopay_batch_task(task_id)
    account_ids = [
        int(item.get("account_id") or 0)
        for item in (task.get("items") or [])
        if isinstance(item, dict)
        and int(item.get("round") or 0) == int(round_number)
        and str(item.get("status") or "") == "queued"
    ]
    threads: list[threading.Thread] = []
    for account_id in account_ids:
        thread = threading.Thread(target=_start_gopay_batch_item, args=(task_id, account_id), daemon=True)
        thread.start()
        threads.append(thread)
    for thread in threads:
        thread.join()


def _finalize_gopay_batch_if_complete(task: dict) -> dict:
    items = [item for item in (task.get("items") or []) if isinstance(item, dict)]
    if not items or not _gopay_batch_all_terminal(items):
        return task
    if str(task.get("status") or "") == "cancelled":
        return _save_gopay_batch_task(task)
    task["status"] = "done"
    task["current_round"] = 0
    task["next_round_at"] = None
    task["message"] = "GoPay 批量支付任务已完成"
    return _save_gopay_batch_task(task)


def _run_gopay_batch_worker(task_id: str) -> None:
    try:
        while True:
            task = _load_gopay_batch_task(task_id)
            status = str(task.get("status") or "")
            if status not in GOPAY_BATCH_ACTIVE_STATUSES:
                break
            if status == "queued":
                task["status"] = "running"
                task = _save_gopay_batch_task(task)

            task = _refresh_gopay_batch_active_items(task_id)
            task = _finalize_gopay_batch_if_complete(task)
            if str(task.get("status") or "") not in GOPAY_BATCH_ACTIVE_STATUSES:
                break

            items = [item for item in (task.get("items") or []) if isinstance(item, dict)]
            promoted_account_ids = _gopay_batch_promote_with_released_phones(task)
            if promoted_account_ids:
                task = _save_gopay_batch_task(task)
                threads: list[threading.Thread] = []
                for account_id in promoted_account_ids:
                    thread = threading.Thread(target=_start_gopay_batch_item, args=(task_id, account_id), daemon=True)
                    thread.start()
                    threads.append(thread)
                for thread in threads:
                    thread.join()
                time.sleep(1.0)
                continue

            pending_rounds = sorted({
                int(item.get("round") or 0)
                for item in items
                if str(item.get("status") or "") not in GOPAY_BATCH_TERMINAL_STATUSES
            })
            if not pending_rounds:
                task = _finalize_gopay_batch_if_complete(task)
                if str(task.get("status") or "") not in GOPAY_BATCH_ACTIVE_STATUSES:
                    break
                time.sleep(1.0)
                continue

            current_round = pending_rounds[0]
            current_items = [item for item in items if int(item.get("round") or 0) == current_round]
            if any(_gopay_batch_item_is_active(item) for item in current_items):
                task["current_round"] = current_round
                _save_gopay_batch_task(task)
                time.sleep(3.0)
                continue
            if all(str(item.get("status") or "") in GOPAY_BATCH_TERMINAL_STATUSES for item in current_items):
                time.sleep(0.5)
                continue

            queued_current = [item for item in current_items if str(item.get("status") or "") == "queued"]
            if not queued_current:
                time.sleep(1.0)
                continue

            interval = max(0, min(int(task.get("round_interval_seconds") or 0), 600))
            now = time.time()
            next_round_at = task.get("next_round_at")
            if current_round > 1 and not next_round_at:
                task["current_round"] = current_round
                task["next_round_at"] = now + interval
                task = _save_gopay_batch_task(task)
                next_round_at = task.get("next_round_at")
            if current_round > 1 and float(next_round_at or 0) > now:
                time.sleep(min(1.0, max(0.1, float(next_round_at) - now)))
                continue

            task["current_round"] = current_round
            task["next_round_at"] = None
            _save_gopay_batch_task(task)
            _start_gopay_batch_round(task_id, current_round)
            time.sleep(1.0)
    except HTTPException:
        pass
    except Exception as exc:
        try:
            def mark_failed(task: dict) -> None:
                task["status"] = "failed"
                task["message"] = str(exc) or "GoPay 批量任务异常退出"
                task["next_round_at"] = None

            _mutate_gopay_batch_task(task_id, mark_failed)
        except Exception:
            pass
    finally:
        with _GOPAY_BATCH_LOCK:
            _GOPAY_BATCH_WORKERS.discard(str(task_id))


def _ensure_gopay_batch_worker(task_id: str) -> None:
    task_id = str(task_id or "").strip()
    if not task_id:
        return
    try:
        task = _load_gopay_batch_task(task_id)
    except HTTPException:
        return
    if str(task.get("status") or "") not in GOPAY_BATCH_ACTIVE_STATUSES:
        return
    with _GOPAY_BATCH_LOCK:
        if task_id in _GOPAY_BATCH_WORKERS:
            return
        _GOPAY_BATCH_WORKERS.add(task_id)
    threading.Thread(target=_run_gopay_batch_worker, args=(task_id,), daemon=True).start()


def _active_gopay_batch_task() -> dict | None:
    with _GOPAY_BATCH_LOCK:
        tasks = _load_gopay_batch_tasks_unlocked()
        active_id = _get_gopay_batch_active_id()
        active = tasks.get(active_id) if active_id else None
        if isinstance(active, dict) and str(active.get("status") or "") in GOPAY_BATCH_ACTIVE_STATUSES:
            _ensure_gopay_batch_worker(active_id)
            return active
        for task_id, task in sorted(
            tasks.items(),
            key=lambda pair: str(pair[1].get("updated_at") or pair[1].get("created_at") or ""),
            reverse=True,
        ):
            if isinstance(task, dict) and str(task.get("status") or "") in GOPAY_BATCH_ACTIVE_STATUSES:
                _set_gopay_batch_active_id(task_id)
                _ensure_gopay_batch_worker(task_id)
                return task
        if active_id:
            _set_gopay_batch_active_id("")
        return None


def _build_gopay_batch_item(req_item: GoPayBatchItemReq, acc: AccountModel) -> dict:
    phone = req_item.phone
    normalized_phone = split_gopay_phone_input(
        phone.phone_country_code,
        phone.phone_number,
        _load_gopay_recognized_country_codes(),
    )
    return {
        "account_id": int(acc.id),
        "account": {
            "id": int(acc.id),
            "email": acc.email,
            "status": acc.status,
        },
        "email": acc.email or "",
        "phone": {
            "id": str(phone.id or "").strip(),
            "label": str(phone.label or "").strip(),
            "phone_country_code": normalized_phone["phone_country_code"],
            "phone_number": normalized_phone["phone_number"],
        },
        "batch_index": int(req_item.batch_index or 0),
        "batchIndex": int(req_item.batch_index or 0),
        "round": max(1, int(req_item.round or 1)),
        "status": "queued",
        "snapshot": {},
        "error": "",
        "started_at": "",
        "updated_at": _utcnow_iso(),
    }


@router.post("/gopay/batch/start")
def start_gopay_batch_payment(req: GoPayBatchStartReq,
                              session: Session = Depends(get_session)):
    active = _active_gopay_batch_task()
    if active:
        return active
    if not req.items:
        raise HTTPException(400, "请选择要批量支付的账号")

    seen: set[int] = set()
    items: list[dict] = []
    for req_item in req.items:
        account_id = int(req_item.account_id or 0)
        if not account_id or account_id in seen:
            continue
        seen.add(account_id)
        acc = _get_account(account_id, session)
        item = _build_gopay_batch_item(req_item, acc)
        if not item["phone"]["phone_country_code"] or not item["phone"]["phone_number"]:
            raise HTTPException(400, f"{acc.email or acc.id} 缺少 GoPay 手机号")
        items.append(item)
    if not items:
        raise HTTPException(400, "没有可启动的批量支付账号")

    if req.otp_auto_resend_delay_seconds is not None:
        _save_gopay_otp_auto_resend_delay_seconds(req.otp_auto_resend_delay_seconds)

    now = _utcnow_iso()
    task_id = f"gopay_batch_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
    task = {
        "task_id": task_id,
        "status": "queued",
        "created_at": now,
        "updated_at": now,
        "round_interval_seconds": max(0, min(int(req.round_interval_seconds or 0), 600)),
        "current_round": 1,
        "next_round_at": None,
        "defaults": dict(req.defaults or {}),
        "items": items,
        "total": len(items),
        "success": 0,
        "failed": 0,
        "cancelled": 0,
        "message": "GoPay 批量支付任务已创建",
    }
    saved = _save_gopay_batch_task(task)
    _ensure_gopay_batch_worker(task_id)
    return saved


@router.get("/gopay/batch/active")
def get_active_gopay_batch_payment():
    task = _active_gopay_batch_task()
    if not task:
        return {"task": None}
    try:
        task = _refresh_gopay_batch_active_items(str(task.get("task_id") or ""))
    except Exception:
        pass
    return {"task": task}


@router.get("/gopay/batch/{batch_id}")
def get_gopay_batch_payment(batch_id: str):
    task = _refresh_gopay_batch_active_items(batch_id)
    _ensure_gopay_batch_worker(batch_id)
    return task


@router.post("/gopay/batch/{batch_id}/cancel")
def cancel_gopay_batch_payment(batch_id: str):
    task = _load_gopay_batch_task(batch_id)
    for item in task.get("items") or []:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "")
        snapshot = item.get("snapshot") if isinstance(item.get("snapshot"), dict) else {}
        session_id = str(snapshot.get("session_id") or "").strip()
        if session_id and status not in GOPAY_BATCH_TERMINAL_STATUSES:
            try:
                from core.db import engine

                with Session(engine) as db:
                    latest = cancel_gopay_payment(int(item.get("account_id") or 0), session_id, session=db)
                item["snapshot"] = _compact_gopay_snapshot(latest)
            except Exception:
                pass
        if status not in GOPAY_BATCH_TERMINAL_STATUSES:
            item["status"] = "cancelled"
            item["updated_at"] = _utcnow_iso()
    task["status"] = "cancelled"
    task["next_round_at"] = None
    task["message"] = "GoPay 批量支付任务已取消"
    return _save_gopay_batch_task(task)


@router.post("/gopay/batch/{batch_id}/items/{account_id}/cancel")
def cancel_gopay_batch_payment_item(batch_id: str, account_id: int):
    task = _load_gopay_batch_task(batch_id)
    item = _find_gopay_batch_item(task, account_id)
    snapshot = item.get("snapshot") if isinstance(item.get("snapshot"), dict) else {}
    session_id = str(snapshot.get("session_id") or "").strip()
    if session_id:
        from core.db import engine

        with Session(engine) as db:
            try:
                latest = cancel_gopay_payment(account_id, session_id, session=db)
                item["snapshot"] = _compact_gopay_snapshot(latest)
            except HTTPException:
                item["snapshot"] = snapshot
    item["status"] = "cancelled"
    item["updated_at"] = _utcnow_iso()
    saved = _save_gopay_batch_task(task)
    return _find_gopay_batch_item(saved, account_id)


@router.post("/gopay/batch/{batch_id}/items/{account_id}/resend-otp")
def resend_gopay_batch_payment_otp(batch_id: str, account_id: int):
    task = _load_gopay_batch_task(batch_id)
    item = _find_gopay_batch_item(task, account_id)
    snapshot = item.get("snapshot") if isinstance(item.get("snapshot"), dict) else {}
    session_id = str(snapshot.get("session_id") or "").strip()
    if not session_id:
        raise HTTPException(400, "GoPay 会话尚未启动")
    from core.db import engine

    with Session(engine) as db:
        latest = resend_gopay_payment_otp(account_id, session_id, session=db)
    item["snapshot"] = _compact_gopay_snapshot(latest)
    item["status"] = _gopay_batch_item_status_from_snapshot(latest)
    item["error"] = str((latest or {}).get("last_error") or "")
    item["updated_at"] = _utcnow_iso()
    saved = _save_gopay_batch_task(task)
    return _find_gopay_batch_item(saved, account_id)


@router.post("/gopay/batch/{batch_id}/items/{account_id}/otp")
def submit_gopay_batch_payment_otp(batch_id: str, account_id: int, req: GoPayOtpReq):
    task = _load_gopay_batch_task(batch_id)
    item = _find_gopay_batch_item(task, account_id)
    snapshot = item.get("snapshot") if isinstance(item.get("snapshot"), dict) else {}
    session_id = str(snapshot.get("session_id") or "").strip()
    if not session_id:
        raise HTTPException(400, "GoPay 会话尚未启动")
    from core.db import engine

    with Session(engine) as db:
        latest = submit_gopay_payment_otp(account_id, session_id, req, session=db)
    item["snapshot"] = _compact_gopay_snapshot(latest)
    item["status"] = _gopay_batch_item_status_from_snapshot(latest)
    item["error"] = str((latest or {}).get("last_error") or "")
    item["updated_at"] = _utcnow_iso()
    saved = _save_gopay_batch_task(task)
    return _find_gopay_batch_item(saved, account_id)


@router.post("/gopay/batch/{batch_id}/items/{account_id}/pin")
def submit_gopay_batch_payment_pin(batch_id: str, account_id: int, req: GoPayPinReq):
    task = _load_gopay_batch_task(batch_id)
    item = _find_gopay_batch_item(task, account_id)
    snapshot = item.get("snapshot") if isinstance(item.get("snapshot"), dict) else {}
    session_id = str(snapshot.get("session_id") or "").strip()
    if not session_id:
        raise HTTPException(400, "GoPay 会话尚未启动")
    from core.db import engine

    with Session(engine) as db:
        latest = submit_gopay_payment_pin(account_id, session_id, req, session=db)
    item["snapshot"] = _compact_gopay_snapshot(latest)
    item["status"] = _gopay_batch_item_status_from_snapshot(latest)
    item["error"] = str((latest or {}).get("last_error") or "")
    item["updated_at"] = _utcnow_iso()
    saved = _save_gopay_batch_task(task)
    return _find_gopay_batch_item(saved, account_id)


@router.get("/payment-countries")
def list_payment_countries(proxy: Optional[str] = None):
    from services.chatgpt_core.payment import fetch_checkout_countries

    return {"countries": fetch_checkout_countries(proxy=_resolve_optional_checkout_proxy(proxy))}


@router.get("/payment-config/{country_code}")
def get_payment_config(country_code: str, proxy: Optional[str] = None):
    from services.chatgpt_core.payment import fetch_checkout_pricing_config, summarize_checkout_pricing_config

    config = fetch_checkout_pricing_config(country_code, proxy=_resolve_optional_checkout_proxy(proxy))
    return summarize_checkout_pricing_config(config)


@router.post("/{account_id}/gopay/generate-billing-address")
def generate_gopay_billing_address(account_id: int, req: GoPayGenerateBillingReq,
                                   session: Session = Depends(get_session)):
    acc = _get_account(account_id, session)
    try:
        billing, target_country, country_strategy = _resolve_gopay_billing_for_manual_generation(req, acc)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(400, f"生成 GoPay 账单地址失败: {exc}") from exc
    return {
        "billing": {
            "billing_name": billing["name"],
            "billing_email": billing["email"],
            "billing_country": billing["country"],
            "billing_line1": billing["line1"],
            "billing_city": billing["city"],
            "billing_state": billing["state"],
            "billing_postal_code": billing["postal_code"],
        },
        "target_country": target_country,
        "strategy": country_strategy,
        "source": "llm",
    }


@router.post("/{account_id}/browser-auth/start")
async def start_browser_auth(account_id: int, req: BrowserAuthStartReq,
                             session: Session = Depends(get_session)):
    acc = _get_account(account_id, session)
    proxy = _resolve_browser_auth_proxy(req.proxy)
    for old in list(_BROWSER_AUTH_SESSIONS.values()):
        if int(old.account_id) == int(account_id):
            await _browser_auth_close_state(old)

    try:
        from playwright.async_api import async_playwright
        from core.proxy_utils import build_playwright_proxy_config
    except Exception as exc:
        raise HTTPException(500, f"Playwright 不可用: {exc}") from exc

    fingerprint = _fresh_browser_fingerprint_dict() if req.fresh_profile else _account_browser_fingerprint(acc)
    capture_id = f"cba_{uuid.uuid4().hex}"
    state = _BrowserAuthSession(capture_id=capture_id, account_id=account_id, proxy=proxy)
    state.user_agent = str(fingerprint["user_agent"])
    state.accept_language = str(fingerprint["accept_language"])
    state.sec_ch_ua = str(fingerprint["sec_ch_ua"])
    state.viewport_width = BROWSER_AUTH_VIEWPORT_WIDTH
    state.viewport_height = BROWSER_AUTH_VIEWPORT_HEIGHT

    try:
        state.playwright = await async_playwright().start()
        launch_args: dict[str, Any] = {
            "headless": False,
            "args": [
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--window-position=0,0",
                f"--window-size={state.viewport_width},{state.viewport_height}",
                "--force-device-scale-factor=1",
            ],
        }
        proxy_config = _playwright_proxy_config_for_browser_auth(proxy or None)
        if proxy_config:
            launch_args["proxy"] = proxy_config
        try:
            state.browser = await state.playwright.chromium.launch(**launch_args)
        except Exception:
            launch_args["headless"] = True
            state.browser = await state.playwright.chromium.launch(**launch_args)

        locale = state.accept_language.split(",", 1)[0].split(";", 1)[0].strip() or "en-US"
        state.context = await state.browser.new_context(
            viewport={"width": state.viewport_width, "height": state.viewport_height},
            user_agent=state.user_agent,
            locale=locale,
            ignore_https_errors=True,
            extra_http_headers={
                "Accept-Language": state.accept_language,
                "sec-ch-ua": state.sec_ch_ua,
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
            },
        )
        await state.context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )

        extra = acc.get_extra()
        cookies = str(extra.get("cookies") or "").strip()
        session_token = str(extra.get("session_token") or "").strip()
        cookie_payload = _cookie_header_to_playwright(cookies)
        cookie_names = {str(item.get("name") or "") for item in cookie_payload}
        if session_token and "__Secure-next-auth.session-token" not in cookie_names:
            cookie_payload.append(
                {
                    "name": "__Secure-next-auth.session-token",
                    "value": session_token,
                    "url": "https://chatgpt.com/",
                    "secure": True,
                    "sameSite": "Lax",
                }
            )
        if fingerprint.get("device_id") and "oai-did" not in cookie_names:
            cookie_payload.append(
                {
                    "name": "oai-did",
                    "value": str(fingerprint["device_id"]),
                    "url": "https://chatgpt.com/",
                    "secure": True,
                    "sameSite": "Lax",
                }
            )
        if cookie_payload:
            await state.context.add_cookies(cookie_payload)

        state.page = await state.context.new_page()
        state.page.set_default_timeout(45000)
        state.page.set_default_navigation_timeout(45000)
        _BROWSER_AUTH_SESSIONS[capture_id] = state
        await _browser_auth_goto(state, req.url)
        async with state.lock:
            return await _browser_auth_snapshot_locked(state)
    except HTTPException:
        await _browser_auth_close_state(state)
        raise
    except Exception as exc:
        await _browser_auth_close_state(state)
        raise HTTPException(500, f"启动浏览器登录失败: {exc}") from exc


@router.get("/{account_id}/browser-auth/{capture_id}")
async def get_browser_auth(account_id: int, capture_id: str):
    state = _browser_auth_get(capture_id, account_id)
    async with state.lock:
        return await _browser_auth_snapshot_locked(state)


@router.post("/{account_id}/browser-auth/{capture_id}/navigate")
async def navigate_browser_auth(account_id: int, capture_id: str, req: BrowserAuthNavigateReq):
    state = _browser_auth_get(capture_id, account_id)
    async with state.lock:
        if state.page is None:
            raise HTTPException(410, "浏览器已关闭")
        await _browser_auth_goto(state, req.url)
        return await _browser_auth_snapshot_locked(state)


@router.post("/{account_id}/browser-auth/{capture_id}/click")
async def click_browser_auth(account_id: int, capture_id: str, req: BrowserAuthClickReq):
    state = _browser_auth_get(capture_id, account_id)
    async with state.lock:
        if state.page is None:
            raise HTTPException(410, "浏览器已关闭")
        await state.page.mouse.click(float(req.x), float(req.y))
        try:
            await state.page.wait_for_load_state("domcontentloaded", timeout=2500)
        except Exception:
            pass
        return await _browser_auth_snapshot_locked(state)


@router.post("/{account_id}/browser-auth/{capture_id}/type")
async def type_browser_auth(account_id: int, capture_id: str, req: BrowserAuthTypeReq):
    state = _browser_auth_get(capture_id, account_id)
    async with state.lock:
        if state.page is None:
            raise HTTPException(410, "浏览器已关闭")
        await state.page.keyboard.type(str(req.text or ""), delay=18)
        return await _browser_auth_snapshot_locked(state)


@router.post("/{account_id}/browser-auth/{capture_id}/key")
async def key_browser_auth(account_id: int, capture_id: str, req: BrowserAuthKeyReq):
    state = _browser_auth_get(capture_id, account_id)
    key = str(req.key or "").strip()
    allowed = {
        "Enter", "Tab", "Escape", "Backspace", "Delete",
        "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown",
        "Home", "End", "PageUp", "PageDown",
    }
    if key not in allowed:
        raise HTTPException(400, "不支持的按键")
    async with state.lock:
        if state.page is None:
            raise HTTPException(410, "浏览器已关闭")
        await state.page.keyboard.press(key)
        try:
            await state.page.wait_for_load_state("domcontentloaded", timeout=2500)
        except Exception:
            pass
        return await _browser_auth_snapshot_locked(state)


@router.post("/{account_id}/browser-auth/{capture_id}/inject-billing")
async def inject_browser_auth_billing(account_id: int, capture_id: str,
                                      session: Session = Depends(get_session)):
    acc = _get_account(account_id, session)
    billing = _billing_for_browser_injection(acc)
    state = _browser_auth_get(capture_id, account_id)
    async with state.lock:
        if state.page is None:
            raise HTTPException(410, "浏览器已关闭")
        frame_results: list[dict[str, Any]] = []
        for frame in list(state.page.frames):
            try:
                result = await frame.evaluate(_BILLING_INJECT_SCRIPT, billing)
                if isinstance(result, dict) and result.get("changed"):
                    frame_results.append(result)
            except Exception:
                continue
        suggestion_results: list[dict[str, Any]] = []
        if any("line1" in result.get("changed", []) for result in frame_results):
            try:
                await state.page.keyboard.press("ArrowDown")
                await state.page.wait_for_timeout(250)
                await state.page.keyboard.press("Enter")
                await state.page.wait_for_timeout(350)
            except Exception:
                pass
            for frame in list(state.page.frames):
                try:
                    result = await frame.evaluate(_ADDRESS_SUGGESTION_CLICK_SCRIPT)
                    if isinstance(result, dict) and result.get("clicked"):
                        suggestion_results.append(result)
                except Exception:
                    continue
        try:
            await state.page.wait_for_load_state("domcontentloaded", timeout=1000)
        except Exception:
            pass
        snapshot = await _browser_auth_snapshot_locked(state)
        changed = sorted({item for result in frame_results for item in result.get("changed", [])})
        snapshot["billing_injection"] = {
            "changed": changed,
            "frames": frame_results,
            "address_suggestions": suggestion_results,
            "billing_country": billing.get("country", ""),
            "billing_email": billing.get("email", ""),
        }
        return snapshot


@router.post("/{account_id}/browser-auth/{capture_id}/capture")
async def capture_browser_auth(account_id: int, capture_id: str,
                               session: Session = Depends(get_session)):
    acc = _get_account(account_id, session)
    state = _browser_auth_get(capture_id, account_id)
    result = await _browser_auth_capture_to_account(state, acc, session)
    try:
        await _browser_auth_close_state(state)
    except Exception:
        pass
    return result


@router.post("/{account_id}/browser-auth/{capture_id}/close")
async def close_browser_auth(account_id: int, capture_id: str, req: BrowserAuthCloseReq):
    state = _browser_auth_get(capture_id, account_id)
    await _browser_auth_close_state(state)
    return {"ok": True, "message": "浏览器登录会话已关闭"}


@router.post("/{account_id}/payment-link")
def generate_payment_link(account_id: int, req: PaymentReq,
                          session: Session = Depends(get_session)):
    acc = _get_account(account_id, session)
    codex_acc = _to_codex_account(acc)

    from services.chatgpt_core.payment import (
        generate_plus_link,
        generate_team_link,
        normalize_checkout_country,
        normalize_checkout_currency,
        normalize_payment_link_format,
    )

    plan = str(req.plan or "plus").strip().lower()
    if plan not in {"plus", "team"}:
        plan = "plus"
    country = normalize_checkout_country(req.country)
    currency = normalize_checkout_currency(req.currency, country)
    payment_link_format = normalize_payment_link_format(req.payment_link_format)
    proxy = _resolve_optional_checkout_proxy(req.proxy)
    if plan == "plus":
        url = generate_plus_link(
            codex_acc,
            proxy=proxy,
            country=country,
            currency=currency,
            link_format=payment_link_format,
        )
    else:
        url = generate_team_link(
            codex_acc,
            workspace_name=req.workspace_name,
            price_interval=req.price_interval,
            seat_quantity=req.seat_quantity,
            promo_code=req.promo_code,
            proxy=proxy,
            country=country,
            currency=currency,
            link_format=payment_link_format,
        )
    acc.cashier_url = str(url or "")
    mark_payment_pending(acc, reason="payment_link_generated")
    extra = acc.get_extra()
    extra["chatgpt_last_payment_link"] = {
        "url": str(url or ""),
        "plan": plan,
        "country": country,
        "currency": currency,
        "proxy": proxy,
        "payment_link_format": payment_link_format,
    }
    if req.save_defaults:
        defaults_payload = {
            "plan": plan,
            "country": country,
            "currency": currency,
            "proxy": proxy,
            "payment_link_format": payment_link_format,
            "promo_code": str(req.promo_code or "").strip(),
            "workspace_name": str(req.workspace_name or "MyTeam").strip() or "MyTeam",
            "seat_quantity": max(2, int(req.seat_quantity or 5)),
            "price_interval": str(req.price_interval or "month").strip().lower() or "month",
        }
        extra["chatgpt_payment_link_defaults"] = defaults_payload
        try:
            config_store.set("chatgpt_payment_link_defaults", json.dumps(defaults_payload, ensure_ascii=False))
        except Exception:
            pass
    acc.set_extra(extra)
    from datetime import datetime, timezone
    acc.updated_at = datetime.now(timezone.utc)
    session.add(acc)
    session.commit()
    return {
        "url": url,
        "plan": plan,
        "country": country,
        "currency": currency,
        "proxy": proxy,
        "payment_link_format": payment_link_format,
        "promo_code": str(req.promo_code or "").strip(),
    }


@router.post("/{account_id}/gopay/start")
def start_gopay_payment(account_id: int, req: GoPayStartReq,
                        session: Session = Depends(get_session)):
    acc = _get_account(account_id, session)
    custom_access_token = str(req.access_token or "").strip()
    codex_acc = _to_gopay_account(acc, custom_access_token)

    from services.chatgpt_core.gopay_flow import GoPayFlowError, create_gopay_session
    from services.chatgpt_core.payment import normalize_checkout_country, normalize_checkout_currency

    try:
        country = normalize_checkout_country(req.country)
        currency = normalize_checkout_currency(req.currency, country)
        extra = acc.get_extra()
        account_defaults = extra.get("chatgpt_gopay_defaults") if isinstance(extra.get("chatgpt_gopay_defaults"), dict) else {}
        global_defaults = _load_global_gopay_defaults()
        saved_defaults = _merge_gopay_defaults(account_defaults, global_defaults)
        requested_pin = _normalize_gopay_pin(req.pin)
        default_pin = requested_pin or _normalize_gopay_pin(saved_defaults.get("pin"))
        normalized_phone = split_gopay_phone_input(
            req.phone_country_code,
            req.phone_number,
            _load_gopay_recognized_country_codes(),
        )
        billing, billing_source, billing_target_country, billing_country_strategy, billing_warning = _resolve_gopay_billing(
            req,
            saved_defaults,
            acc,
            checkout_country=country,
        )
        proxy = _resolve_required_checkout_proxy(req.proxy)
        snapshot = create_gopay_session(
            account_id,
            codex_acc,
            plan=req.plan,
            country=country,
            currency=currency,
            proxy=proxy,
            phone_country_code=normalized_phone["phone_country_code"],
            phone_number=normalized_phone["phone_number"],
            checkout_url=str(req.checkout_url or ""),
            default_pin=default_pin,
            billing=billing,
            otp_auto_resend_delay_seconds=_load_gopay_otp_auto_resend_delay_seconds(),
            pin_source=str(req.pin_source or ("本次输入PIN" if requested_pin else ("全局默认PIN" if default_pin else "未配置"))).strip(),
        )
        logs = snapshot.get("logs") if isinstance(snapshot.get("logs"), list) else []
        logs.append(f"GoPay checkout 代理: {proxy}")
        if billing_source == "llm":
            logs.append(
                "GoPay 账单地址已由 LLM 生成: "
                f"strategy={billing_country_strategy} target={billing_target_country} "
                f"{billing.get('name')} {billing.get('country')} {billing.get('city')} {billing.get('postal_code')}"
            )
        elif billing_warning:
            logs.append(f"GoPay 账单地址 LLM 生成失败，已使用兜底地址: {billing_warning[:300]}")
        if custom_access_token:
            logs.append("GoPay 本次使用自定义 access token 创建和确认 checkout，未写回账号 access token")
        snapshot["logs"] = logs[-500:]
        _ensure_gopay_snapshot_account(snapshot, account_id)
        _start_gopay_task_monitor(account_id, snapshot.get("session_id") or "")
        if req.save_defaults:
            defaults_payload = {
                "phone_country_code": normalized_phone["phone_country_code"],
                "phone_number": normalized_phone["phone_number"],
                "country": country,
                "currency": currency,
                "pin": default_pin,
                "billing_name": billing["name"],
                "billing_email": billing["email"],
                "billing_country": billing["country"],
            }
            if billing_source != "llm":
                defaults_payload.update({
                    "billing_line1": billing["line1"],
                    "billing_city": billing["city"],
                    "billing_state": billing["state"],
                    "billing_postal_code": billing["postal_code"],
                })
            _save_global_gopay_defaults(defaults_payload)
        return _persist_gopay_snapshot(acc, snapshot, session)
    except GoPayFlowError as exc:
        mark_payment_failed(acc, reason="gopay_start_failed")
        acc.updated_at = datetime.now(timezone.utc)
        session.add(acc)
        session.commit()
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        mark_payment_failed(acc, reason="gopay_start_failed")
        acc.updated_at = datetime.now(timezone.utc)
        session.add(acc)
        session.commit()
        raise HTTPException(500, str(exc)) from exc


@router.get("/{account_id}/gopay/{gopay_session_id}")
def get_gopay_payment(account_id: int, gopay_session_id: str,
                      session: Session = Depends(get_session)):
    acc = _get_account(account_id, session)

    from services.chatgpt_core.gopay_flow import get_gopay_session

    try:
        snapshot = get_gopay_session(gopay_session_id)
    except KeyError:
        return _persist_missing_gopay_session(acc, gopay_session_id, session)
    _ensure_gopay_snapshot_account(snapshot, account_id)
    return _persist_gopay_snapshot(acc, snapshot, session)


@router.post("/{account_id}/gopay/{gopay_session_id}/resend-otp")
def resend_gopay_payment_otp(account_id: int, gopay_session_id: str,
                             session: Session = Depends(get_session)):
    acc = _get_account(account_id, session)

    from services.chatgpt_core.gopay_flow import GoPayFlowError, get_gopay_session, resend_gopay_otp

    try:
        _ensure_gopay_snapshot_account(get_gopay_session(gopay_session_id), account_id)
        snapshot = resend_gopay_otp(gopay_session_id)
        _ensure_gopay_snapshot_account(snapshot, account_id)
        return _persist_gopay_snapshot(acc, snapshot, session)
    except KeyError:
        return _persist_missing_gopay_session(acc, gopay_session_id, session)
    except GoPayFlowError as exc:
        try:
            _persist_gopay_snapshot(acc, get_gopay_session(gopay_session_id), session)
        except Exception:
            pass
        raise HTTPException(400, str(exc)) from exc


@router.post("/{account_id}/gopay/{gopay_session_id}/otp")
def submit_gopay_payment_otp(account_id: int, gopay_session_id: str, req: GoPayOtpReq,
                             session: Session = Depends(get_session)):
    acc = _get_account(account_id, session)
    codex_acc = _to_codex_account(acc)

    from services.chatgpt_core.gopay_flow import GoPayFlowError, submit_gopay_otp

    try:
        from services.chatgpt_core.gopay_flow import get_gopay_session

        _ensure_gopay_snapshot_account(get_gopay_session(gopay_session_id), account_id)
        snapshot = submit_gopay_otp(gopay_session_id, codex_acc, req.otp)
        _ensure_gopay_snapshot_account(snapshot, account_id)
        return _persist_gopay_snapshot(acc, snapshot, session)
    except KeyError:
        return _persist_missing_gopay_session(acc, gopay_session_id, session)
    except GoPayFlowError as exc:
        try:
            from services.chatgpt_core.gopay_flow import get_gopay_session

            _persist_gopay_snapshot(acc, get_gopay_session(gopay_session_id), session)
        except Exception:
            pass
        raise HTTPException(400, str(exc)) from exc


@router.post("/{account_id}/gopay/{gopay_session_id}/pin")
def submit_gopay_payment_pin(account_id: int, gopay_session_id: str, req: GoPayPinReq,
                             session: Session = Depends(get_session)):
    acc = _get_account(account_id, session)
    codex_acc = _to_codex_account(acc)

    from services.chatgpt_core.gopay_flow import GoPayFlowError, submit_gopay_pin

    try:
        from services.chatgpt_core.gopay_flow import get_gopay_session

        _ensure_gopay_snapshot_account(get_gopay_session(gopay_session_id), account_id)
        normalized_pin = _normalize_gopay_pin(req.pin)
        if normalized_pin:
            defaults = _load_global_gopay_defaults()
            defaults["pin"] = normalized_pin
            _save_global_gopay_defaults(defaults)
        snapshot = submit_gopay_pin(gopay_session_id, codex_acc, normalized_pin)
        _ensure_gopay_snapshot_account(snapshot, account_id)
        saved = _persist_gopay_snapshot(acc, snapshot, session)
        if str(saved.get("phase") or "") == "succeeded":
            try:
                from services.chatgpt_core.status_probe import probe_local_chatgpt_status

                probe = probe_local_chatgpt_status(codex_acc, proxy=_resolve_chatgpt_proxy(saved.get("proxy")))
                _persist_local_probe(acc, probe, session)
            except Exception:
                pass
        return saved
    except KeyError:
        return _persist_missing_gopay_session(acc, gopay_session_id, session)
    except GoPayFlowError as exc:
        try:
            from services.chatgpt_core.gopay_flow import get_gopay_session

            _persist_gopay_snapshot(acc, get_gopay_session(gopay_session_id), session)
        except Exception:
            pass
        raise HTTPException(400, str(exc)) from exc


@router.post("/{account_id}/gopay/{gopay_session_id}/cancel")
def cancel_gopay_payment(account_id: int, gopay_session_id: str,
                         session: Session = Depends(get_session)):
    acc = _get_account(account_id, session)

    from services.chatgpt_core.gopay_flow import cancel_gopay_session

    try:
        from services.chatgpt_core.gopay_flow import get_gopay_session

        _ensure_gopay_snapshot_account(get_gopay_session(gopay_session_id), account_id)
        snapshot = cancel_gopay_session(gopay_session_id)
    except KeyError:
        return _persist_missing_gopay_session(acc, gopay_session_id, session)
    _ensure_gopay_snapshot_account(snapshot, account_id)
    return _persist_gopay_snapshot(acc, snapshot, session)


# ── 检查订阅状态 ────────────────────────────────────────────
@router.get("/{account_id}/subscription")
def check_subscription(account_id: int, proxy: Optional[str] = None,
                       session: Session = Depends(get_session)):
    acc = _get_account(account_id, session)
    codex_acc = _to_codex_account(acc)

    from services.chatgpt_core.status_probe import probe_local_chatgpt_status

    probe = probe_local_chatgpt_status(codex_acc, proxy=proxy)
    _persist_local_probe(acc, probe, session)
    return {
        "email": acc.email,
        "subscription": probe.get("subscription", {}).get("plan", "unknown"),
        "probe": probe,
    }


@router.post("/{account_id}/probe-local")
def probe_local_status(account_id: int, proxy: Optional[str] = None,
                       session: Session = Depends(get_session)):
    acc = _get_account(account_id, session)
    codex_acc = _to_codex_account(acc)

    from services.chatgpt_core.status_probe import probe_local_chatgpt_status

    probe = probe_local_chatgpt_status(codex_acc, proxy=proxy)
    _persist_local_probe(acc, probe, session)
    return {"ok": True, "email": acc.email, "probe": probe}


class PendingBusinessInviteActivateReq(BaseModel):
    pass


class PendingBusinessInviteBatchActivateReq(BaseModel):
    invite_ids: list[int] = []
    limit: int = 200


class PendingBusinessInviteAbandonReq(BaseModel):
    pass


@router.get("/pending-business-invites")
def list_pending_business_invites(status: Optional[str] = None, limit: int = 200):
    from services.chatgpt_core.pending_business_invites import list_pending_invites

    items = list_pending_invites(status=(status or "").strip() or None, limit=limit)
    return {
        "ok": True,
        "count": len(items),
        "items": items,
    }


@router.post("/pending-business-invites/{invite_id}/activate")
def activate_pending_business_invite(invite_id: int, req: PendingBusinessInviteActivateReq):
    from services.chatgpt_core.pending_business_invites import activate_pending_invite

    try:
        return activate_pending_invite(invite_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/pending-business-invites/batch-activate")
def activate_pending_business_invites(req: PendingBusinessInviteBatchActivateReq):
    from services.chatgpt_core.pending_business_invites import activate_pending_invites

    try:
        return activate_pending_invites(invite_ids=req.invite_ids or None, limit=req.limit)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/pending-business-invites/{invite_id}/abandon")
def abandon_pending_business_invite(invite_id: int, req: PendingBusinessInviteAbandonReq):
    from services.chatgpt_core.pending_business_invites import abandon_pending_invite

    try:
        return abandon_pending_invite(invite_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ── CPA 上传 ────────────────────────────────────────────────
class CpaUploadReq(BaseModel):
    api_url: str
    api_key: str = ""


@router.post("/{account_id}/upload-cpa")
def upload_cpa(account_id: int, req: CpaUploadReq,
               session: Session = Depends(get_session)):
    acc = _get_account(account_id, session)
    codex_acc = _to_codex_account(acc)

    from services.chatgpt_core.cpa_upload import upload_to_cpa, generate_token_json
    token_data = generate_token_json(codex_acc)
    ok, msg = upload_to_cpa(token_data, api_url=req.api_url, api_key=req.api_key)
    return {"ok": ok, "message": msg}


class Sub2ApiUploadReq(BaseModel):
    api_url: str = ""
    api_key: str = ""


def _parse_export_ids(ids: Optional[str] = None, id_list: list[int] | None = None) -> list[int]:
    selected_ids: list[int] = []
    for item in id_list or []:
        try:
            selected_ids.append(int(item))
        except (TypeError, ValueError):
            raise HTTPException(400, "ids 参数必须是账号 ID 列表")
    if ids:
        for part in str(ids or "").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                selected_ids.append(int(part))
            except ValueError:
                raise HTTPException(400, "ids 参数必须是逗号分隔的账号 ID")
    return list(dict.fromkeys(selected_ids))


def _normalize_chatgpt_export_mode(value: Optional[str]) -> str:
    mode = str(value or CHATGPT_EXPORT_MODE_SUB2API).strip().lower()
    if mode not in CHATGPT_EXPORT_MODES:
        raise HTTPException(400, "不支持的导出模式")
    return mode


def _query_chatgpt_export_accounts(
    *,
    session: Session,
    status: Optional[str] = None,
    selected_ids: list[int] | None = None,
) -> list[AccountModel]:
    q = select(AccountModel).where(AccountModel.platform == "chatgpt")
    if status:
        q = q.where(AccountModel.status == status)
    if selected_ids:
        q = q.where(AccountModel.id.in_(selected_ids))
    # A deterministic order makes AccessToken-only exports stable and keeps one
    # account represented by exactly one predictable line.
    q = q.order_by(AccountModel.id)
    return session.exec(q).all()


def _access_token_for_export(acc: AccountModel) -> str:
    """Read both current and legacy saved AT fields without exposing other secrets."""
    return _access_token_from_stored_values(acc.token, acc.extra_json)


def _access_token_from_stored_values(token_value: Any, extra_json: Any) -> str:
    try:
        extra = json.loads(str(extra_json or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        extra = {}
    if not isinstance(extra, dict):
        extra = {}
    for value in (
        extra.get("access_token"),
        extra.get("accessToken"),
        extra.get("webAccessToken"),
        token_value,
    ):
        token = str(value or "").strip()
        if token:
            return token
    return ""


def _query_chatgpt_export_access_tokens(
    *,
    session: Session,
    status: Optional[str] = None,
    selected_ids: list[int] | None = None,
) -> list[str]:
    """Read only token storage columns; account detail JSON can be very large."""
    q = select(AccountModel.token, AccountModel.extra_json).where(AccountModel.platform == "chatgpt")
    if status:
        q = q.where(AccountModel.status == status)
    if selected_ids:
        q = q.where(AccountModel.id.in_(selected_ids))
    q = q.order_by(AccountModel.id)
    return [
        token
        for token_value, extra_json in session.exec(q).all()
        if (token := _access_token_from_stored_values(token_value, extra_json))
    ]


def _build_access_token_export_content(access_tokens: list[str]) -> tuple[str, str, str, str]:
    tokens = [str(token or "").strip() for token in access_tokens]
    tokens = [token for token in tokens if token]
    if not tokens:
        raise HTTPException(400, "所选账号没有可导出的 AccessToken")
    return (
        "\n".join(tokens) + "\n",
        "text/plain; charset=utf-8",
        "chatgpt-access-token",
        "txt",
    )


def _build_chatgpt_export_content(
    *,
    accounts: list[AccountModel],
    export_mode: str,
) -> tuple[str, str, str, str]:
    """Return body, MIME type, ASCII filename prefix and extension for an export."""
    if export_mode == CHATGPT_EXPORT_MODE_ACCESS_TOKEN:
        # Deliberately omit accounts without an AT: blank lines would break the
        # stated one-account/one-token-line contract for downstream importers.
        access_tokens = [
            token
            for acc in accounts
            if (token := _access_token_for_export(acc))
        ]
        return _build_access_token_export_content(access_tokens)

    from datetime import datetime, timezone
    from services.chatgpt_core.sub2api_upload import build_sub2api_export_account_payload

    exported_accounts: list[dict[str, Any]] = []
    for acc in accounts:
        codex_acc = _to_codex_account(acc)
        item = build_sub2api_export_account_payload(codex_acc)
        exported_accounts.append(item)

    payload = {
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "proxies": [],
        "accounts": exported_accounts,
    }
    return (
        json.dumps(payload, ensure_ascii=False, indent=2),
        "application/json",
        "sub2api-account",
        "json",
    )


def _build_sub2api_export_response(
    *,
    session: Session,
    status: Optional[str] = None,
    selected_ids: list[int] | None = None,
    export_mode: str = CHATGPT_EXPORT_MODE_SUB2API,
) -> StreamingResponse:
    from datetime import datetime, timezone
    mode = _normalize_chatgpt_export_mode(export_mode)
    if mode == CHATGPT_EXPORT_MODE_ACCESS_TOKEN:
        body, media_type, filename_prefix, extension = _build_access_token_export_content(
            _query_chatgpt_export_access_tokens(
                session=session,
                status=status,
                selected_ids=selected_ids,
            )
        )
    else:
        accounts = _query_chatgpt_export_accounts(
            session=session,
            status=status,
            selected_ids=selected_ids,
        )
        body, media_type, filename_prefix, extension = _build_chatgpt_export_content(
            accounts=accounts,
            export_mode=mode,
        )
    return StreamingResponse(
        iter([body]),
        media_type=media_type,
        headers={
            "Content-Disposition": (
                f"attachment; filename={filename_prefix}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}.{extension}"
            ),
        },
    )


@router.post("/{account_id}/upload-sub2api")
def upload_sub2api(account_id: int, req: Sub2ApiUploadReq,
                   session: Session = Depends(get_session)):
    acc = _get_account(account_id, session)

    from services.sub2api_sync import backfill_chatgpt_account_to_sub2api

    outcome = backfill_chatgpt_account_to_sub2api(acc, session=session, commit=True)
    return {
        "ok": bool(outcome.get("ok")),
        "message": str(outcome.get("message") or ""),
        "results": outcome.get("results") or [],
    }


@router.post("/export-sub2api-ticket")
def create_chatgpt_accounts_sub2api_export_ticket(
    req: Sub2ApiExportTicketReq,
):
    mode = _normalize_chatgpt_export_mode(req.mode)
    selected_ids = _parse_export_ids(id_list=req.ids)
    status = str(req.status or "").strip()
    now = time.time()
    ticket = uuid.uuid4().hex
    with _SUB2API_EXPORT_TICKET_LOCK:
        expired = [
            key
            for key, value in _SUB2API_EXPORT_TICKETS.items()
            if float(value.get("expires_at") or 0) <= now
        ]
        for key in expired:
            _SUB2API_EXPORT_TICKETS.pop(key, None)
        _SUB2API_EXPORT_TICKETS[ticket] = {
            "ids": selected_ids,
            "status": status,
            "mode": mode,
            "expires_at": now + 300,
        }
    return {
        "ticket": ticket,
        "expires_in": 300,
        "mode": mode,
    }


@router.get("/export-sub2api")
def export_chatgpt_accounts_sub2api(
    status: Optional[str] = None,
    ids: Optional[str] = None,
    mode: str = CHATGPT_EXPORT_MODE_SUB2API,
    session: Session = Depends(get_session),
):
    return _build_sub2api_export_response(
        session=session,
        status=status,
        selected_ids=_parse_export_ids(ids=ids),
        export_mode=mode,
    )


@router.get("/export-sub2api-download")
def download_chatgpt_accounts_sub2api_export(
    ticket: str,
    session: Session = Depends(get_session),
):
    ticket = str(ticket or "").strip()
    with _SUB2API_EXPORT_TICKET_LOCK:
        payload = _SUB2API_EXPORT_TICKETS.pop(ticket, None)
    if not payload:
        raise HTTPException(404, "导出票据不存在或已使用")
    if float(payload.get("expires_at") or 0) <= time.time():
        raise HTTPException(410, "导出票据已过期")
    return _build_sub2api_export_response(
        session=session,
        status=str(payload.get("status") or "").strip() or None,
        selected_ids=list(payload.get("ids") or []),
        export_mode=str(payload.get("mode") or CHATGPT_EXPORT_MODE_SUB2API),
    )
