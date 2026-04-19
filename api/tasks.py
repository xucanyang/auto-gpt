from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlmodel import Session, select
from typing import Any, Optional
from copy import deepcopy
from core.db import TaskLog, engine
from core.task_runtime import (
    AttemptOutcome,
    AttemptResult,
    RegisterTaskStore,
    SkipCurrentAttemptRequested,
    StopTaskRequested,
)
import time, json, asyncio, threading, logging

router = APIRouter(prefix="/tasks", tags=["tasks"])
logger = logging.getLogger(__name__)

MAX_FINISHED_TASKS = 200
CLEANUP_THRESHOLD = 250
_task_store = RegisterTaskStore(
    max_finished_tasks=MAX_FINISHED_TASKS,
    cleanup_threshold=CLEANUP_THRESHOLD,
)


class RegisterTaskRequest(BaseModel):
    platform: str
    email: Optional[str] = None
    password: Optional[str] = None
    count: int = 1
    concurrency: int = 1
    register_delay_seconds: float = 0
    proxy: Optional[str] = None
    executor_type: str = "protocol"
    captcha_solver: str = "yescaptcha"
    extra: dict = Field(default_factory=dict)


class TaskLogBatchDeleteRequest(BaseModel):
    ids: list[int]


class SubmitVerificationRequest(BaseModel):
    challenge_id: str
    code: str


def _ensure_task_exists(task_id: str) -> None:
    if not _task_store.exists(task_id):
        raise HTTPException(404, "任务不存在")


def _ensure_task_mutable(task_id: str) -> None:
    _ensure_task_exists(task_id)
    snapshot = _task_store.snapshot(task_id)
    if snapshot.get("status") in {"done", "failed", "stopped"}:
        raise HTTPException(409, "任务已结束，无法再执行控制操作")


def _prepare_register_request(req: RegisterTaskRequest) -> RegisterTaskRequest:
    from core.config_store import config_store

    req_data = req.model_dump()
    req_data["extra"] = deepcopy(req_data.get("extra") or {})
    prepared = RegisterTaskRequest(**req_data)

    mail_provider = prepared.extra.get("mail_provider") or config_store.get(
        "mail_provider", ""
    )

    if mail_provider == "manual_email_otp":
        if prepared.platform != "chatgpt":
            raise HTTPException(400, "manual_email_otp 模式目前只支持 ChatGPT")
        prepared.email = str(prepared.email or "").strip()
        if not prepared.email:
            raise HTTPException(400, "manual_email_otp 模式必须填写邮箱地址")
        existing_account_capture = prepared.extra.get("chatgpt_existing_account_capture")
        if isinstance(existing_account_capture, str):
            existing_account_capture = existing_account_capture.strip().lower() in {"1", "true", "yes", "on"}
        else:
            existing_account_capture = bool(existing_account_capture)
        prepared.count = 1
        prepared.concurrency = 1
        if not existing_account_capture:
            prepared.password = None
        prepared.extra["manual_email_address"] = prepared.email
        prepared.extra["chatgpt_existing_account_capture"] = existing_account_capture

    if mail_provider == "luckmail":
        prepared.extra["luckmail_project_code"] = "openai"

    return prepared


def _create_task_record(
    task_id: str, req: RegisterTaskRequest, source: str, meta: dict | None = None
):
    _task_store.create(
        task_id,
        platform=req.platform,
        total=req.count,
        source=source,
        meta=meta,
    )


def enqueue_register_task(
    req: RegisterTaskRequest,
    *,
    background_tasks: BackgroundTasks | None = None,
    source: str = "manual",
    meta: dict | None = None,
) -> str:
    prepared = _prepare_register_request(req)
    task_id = f"task_{int(time.time() * 1000)}"
    _create_task_record(task_id, prepared, source, meta)
    if background_tasks is None:
        thread = threading.Thread(
            target=_run_register, args=(task_id, prepared), daemon=True
        )
        thread.start()
    else:
        background_tasks.add_task(_run_register, task_id, prepared)
    return task_id


def has_active_register_task(
    *, platform: str | None = None, source: str | None = None
) -> bool:
    return _task_store.has_active(platform=platform, source=source)


def _log(task_id: str, msg: str):
    """向任务追加一条日志"""
    ts = time.strftime("%H:%M:%S")
    entry = f"[{ts}] {msg}"
    _task_store.append_log(task_id, entry)
    print(entry)


def _save_task_log(
    platform: str, email: str, status: str, error: str = "", detail: dict = None
):
    """Write a TaskLog record to the database."""
    with Session(engine) as s:
        log = TaskLog(
            platform=platform,
            email=email,
            status=status,
            error=error,
            detail_json=json.dumps(detail or {}, ensure_ascii=False),
        )
        s.add(log)
        s.commit()


def _build_task_log_detail(task_id: str, extra: dict | None = None) -> dict:
    try:
        snapshot = _task_store.snapshot(task_id)
    except Exception:
        snapshot = {}
    detail = {
        "task_id": task_id,
        "status_snapshot": str(snapshot.get("status") or ""),
        "progress": str(snapshot.get("progress") or ""),
        "success": int(snapshot.get("success") or 0),
        "skipped": int(snapshot.get("skipped") or 0),
        "errors": list(snapshot.get("errors") or []),
        "cashier_urls": list(snapshot.get("cashier_urls") or []),
        "source": str(snapshot.get("source") or ""),
        "meta": dict(snapshot.get("meta") or {}),
        "logs": list(snapshot.get("logs") or []),
    }
    if extra:
        detail.update(extra)
    return detail


def _task_log_summary(log: TaskLog) -> dict:
    return {
        "id": log.id,
        "platform": log.platform,
        "email": log.email,
        "status": log.status,
        "error": log.error,
        "created_at": log.created_at,
    }


def _task_log_detail_payload(log: TaskLog) -> dict:
    try:
        detail = json.loads(log.detail_json or "{}")
        if not isinstance(detail, dict):
            detail = {}
    except Exception:
        detail = {}
    return {
        **_task_log_summary(log),
        "detail": detail,
    }


def _auto_upload_integrations(task_id: str, account):
    """注册成功后自动导入外部系统。"""
    _log(task_id, f"[Auto Upload] 开始自动同步外部系统，account_id={getattr(account, 'id', 'unknown')}")
    try:
        from services.external_sync import sync_account

        for result in sync_account(account):
            name = result.get("name", "Auto Upload")
            ok = bool(result.get("ok"))
            msg = result.get("msg", "")
            _log(task_id, f"  [{name}] {'[OK] ' + msg if ok else '[FAIL] ' + msg}")
    except Exception as e:
        _log(task_id, f"  [Auto Upload] 自动导入异常: {e}")


def _is_truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _build_effective_register_extra(req: RegisterTaskRequest) -> dict:
    from core.config_store import config_store

    merged_extra = config_store.get_all().copy()
    merged_extra.update(
        {k: v for k, v in req.extra.items() if v is not None and v != ""}
    )
    if req.email:
        merged_extra.setdefault("manual_email_address", req.email)
        merged_extra.setdefault("email", req.email)
    return merged_extra


def _should_precheck_chatgpt_deferred_invite(
    req: RegisterTaskRequest,
    merged_extra: dict | None = None,
) -> bool:
    effective_extra = merged_extra or _build_effective_register_extra(req)
    return (
        req.platform == "chatgpt"
        and _is_truthy(effective_extra.get("chatgpt_enable_team_invite"))
        and _is_truthy(effective_extra.get("chatgpt_team_invite_deferred_activation"))
    )


def _check_chatgpt_deferred_invite_availability(
    req: RegisterTaskRequest,
    *,
    merged_extra: dict | None = None,
    log_fn=None,
) -> tuple[bool, str]:
    effective_extra = merged_extra or _build_effective_register_extra(req)
    if not _should_precheck_chatgpt_deferred_invite(req, effective_extra):
        return True, ""

    try:
        from platforms.chatgpt.business_workspace_recovery import BusinessWorkspaceRecovery
        from services.team_embedded_backend import team_embedded_backend

        recovery = BusinessWorkspaceRecovery(
            effective_extra,
            proxy=req.proxy,
            browser_mode=req.executor_type or effective_extra.get("default_executor") or "protocol",
            log_fn=(lambda message, *_args: log_fn(message)) if callable(log_fn) else None,
        )
        if not recovery.is_enabled():
            return False, "本地 Team 运行时不可用，无法执行延迟邀请"
        teams = recovery.list_available_teams()
        if not teams:
            return False, "当前没有可邀请 team，延迟邀请任务终止"

        candidate_team_ids = []
        for item in teams:
            try:
                team_id = int((item or {}).get("id") or 0)
            except Exception:
                team_id = 0
            if team_id > 0 and team_id not in candidate_team_ids:
                candidate_team_ids.append(team_id)

        # 预检查不能只看旧 DB 状态；先强制刷新候选 Team，剔除 token 失效/已满/过期项。
        verified_team_ids = []
        for team_id in candidate_team_ids:
            try:
                sync_result = team_embedded_backend.sync_team_info(team_id, force_refresh=True)
            except Exception as exc:
                if callable(log_fn):
                    log_fn(f"[邀请] 预检查刷新 team={team_id} 失败: {exc}")
                continue
            if bool((sync_result or {}).get("success")):
                verified_team_ids.append(team_id)
                continue
            if callable(log_fn):
                error_text = str((sync_result or {}).get("error") or "unknown").strip() or "unknown"
                log_fn(f"[邀请] 预检查剔除 team={team_id}: {error_text}")

        if not verified_team_ids:
            return False, "预检查刷新后候选 Team 全部不可用，延迟邀请任务终止"

        teams = recovery.list_available_teams()
        if not teams:
            return False, "预检查刷新后没有可邀请 team，延迟邀请任务终止"

        team_ids = []
        verified_team_id_set = set(verified_team_ids)
        for item in teams:
            try:
                team_id = int((item or {}).get("id") or 0)
            except Exception:
                team_id = 0
            if team_id <= 0 or team_id not in verified_team_id_set:
                continue
            if team_id not in team_ids:
                team_ids.append(team_id)
        if isinstance(effective_extra, dict) and team_ids:
            effective_extra["chatgpt_deferred_invite_team_ids"] = list(team_ids)
            effective_extra["chatgpt_deferred_invite_team_id"] = team_ids[0]
        if team_ids:
            available_text = ",".join(str(team_id) for team_id in team_ids)
            return True, f"延迟邀请预检查通过，可用 team_id={available_text}"
        return False, "预检查刷新后候选 Team 全部不可用，延迟邀请任务终止"
    except Exception as exc:
        return False, f"延迟邀请预检查失败: {exc}"


def _run_register(task_id: str, req: RegisterTaskRequest):
    from core.registry import get
    from core.base_platform import RegisterConfig
    from core.db import save_account
    from core.base_mailbox import create_mailbox
    from core.proxy_utils import normalize_proxy_url

    control = _task_store.control_for(task_id)
    _task_store.mark_running(task_id)
    success = 0
    skipped = 0
    errors = []
    start_gate_lock = threading.Lock()
    next_start_time = time.time()

    def _sleep_with_control(
        wait_seconds: float,
        *,
        attempt_id: int | None = None,
    ) -> None:
        remaining = max(float(wait_seconds or 0), 0.0)
        while remaining > 0:
            control.checkpoint(attempt_id=attempt_id)
            chunk = min(0.25, remaining)
            time.sleep(chunk)
            remaining -= chunk

    try:
        PlatformCls = get(req.platform)

        initial_merged_extra = _build_effective_register_extra(req)
        deferred_activation_enabled = _should_precheck_chatgpt_deferred_invite(req, initial_merged_extra)
        pending_invite_ids: list[int] = []
        pending_invite_lock = threading.Lock()
        registration_success = 0
        deferred_team_ids: list[int] = []
        deferred_team_ids_lock = threading.Lock()
        deferred_phase_close_reason = ""
        deferred_phase_close_marker = "__deferred_invite_phase_close__"
        registration_phase_close_event = threading.Event()
        if deferred_activation_enabled:
            ok, check_message = _check_chatgpt_deferred_invite_availability(
                req,
                merged_extra=initial_merged_extra,
                log_fn=lambda message: _log(task_id, message),
            )
            if isinstance(initial_merged_extra.get("chatgpt_deferred_invite_team_ids"), list):
                deferred_team_ids = [
                    int(team_id)
                    for team_id in initial_merged_extra.get("chatgpt_deferred_invite_team_ids")
                    if str(team_id).strip().isdigit() and int(team_id) > 0
                ]
                if deferred_team_ids:
                    req.extra["chatgpt_deferred_invite_team_ids"] = list(deferred_team_ids)
                    req.extra["chatgpt_deferred_invite_team_id"] = deferred_team_ids[0]
            if ok and check_message:
                _log(task_id, f"[邀请] {check_message}")
            if not ok:
                _log(task_id, f"[邀请] {check_message}")
                _task_store.finish(
                    task_id,
                    status="failed",
                    success=success,
                    skipped=skipped,
                    errors=[check_message],
                    error=check_message,
                )
                _task_store.cleanup()
                return

        def _build_mailbox(proxy: Optional[str]):
            merged_extra = _build_effective_register_extra(req)
            return create_mailbox(
                provider=merged_extra.get("mail_provider", "luckmail"),
                extra=merged_extra,
                proxy=proxy,
            )

        def _do_one(i: int):
            nonlocal next_start_time, deferred_phase_close_reason
            proxy_pool = None
            _proxy = None
            current_email = req.email or ""
            attempt_id: int | None = None
            try:
                from core.proxy_pool import proxy_pool

                control.checkpoint()
                attempt_id = control.start_attempt()
                control.checkpoint(attempt_id=attempt_id)
                _proxy = req.proxy
                if not _proxy:
                    _proxy = proxy_pool.get_next()
                _proxy = normalize_proxy_url(_proxy)
                if req.register_delay_seconds > 0:
                    with start_gate_lock:
                        control.checkpoint(attempt_id=attempt_id)
                        now = time.time()
                        wait_seconds = max(0.0, next_start_time - now)
                        if wait_seconds > 0:
                            _log(
                                task_id,
                                f"第 {i + 1} 个账号启动前延迟 {wait_seconds:g} 秒",
                            )
                            _sleep_with_control(
                                wait_seconds,
                                attempt_id=attempt_id,
                            )
                        next_start_time = time.time() + req.register_delay_seconds
                control.checkpoint(attempt_id=attempt_id)
                if registration_phase_close_event.is_set():
                    return AttemptResult.stopped(
                        f"{deferred_phase_close_marker}{deferred_phase_close_reason or '当前所有可用 team 都已不可邀请，结束注册阶段并进入激活阶段'}"
                    )
                merged_extra = _build_effective_register_extra(req)
                if deferred_activation_enabled:
                    with deferred_team_ids_lock:
                        current_deferred_team_ids = list(deferred_team_ids)
                    if not current_deferred_team_ids:
                        return AttemptResult.stopped(
                            f"{deferred_phase_close_marker}当前所有可用 team 都已不可邀请，结束注册阶段并进入激活阶段"
                        )
                    merged_extra["chatgpt_deferred_invite_team_ids"] = current_deferred_team_ids
                    merged_extra["chatgpt_deferred_invite_team_id"] = current_deferred_team_ids[0]

                _config = RegisterConfig(
                    executor_type=req.executor_type,
                    captcha_solver=req.captcha_solver,
                    proxy=_proxy,
                    extra=merged_extra,
                )
                _mailbox = _build_mailbox(_proxy)
                _platform = PlatformCls(config=_config, mailbox=_mailbox)
                _platform._task_attempt_token = attempt_id
                _platform._log_fn = lambda msg: _log(task_id, msg)
                _platform.bind_task_control(control)
                if getattr(_platform, "mailbox", None) is not None:
                    _platform.mailbox._task_attempt_token = attempt_id
                    _platform.mailbox._log_fn = _platform._log_fn
                _task_store.set_progress(task_id, f"{i + 1}/{req.count}")
                _log(task_id, f"开始注册第 {i + 1}/{req.count} 个账号")
                if _proxy:
                    _log(task_id, f"使用代理: {_proxy}")
                account = _platform.register(
                    email=req.email or None,
                    password=req.password,
                )
                current_email = account.email or current_email
                if isinstance(account.extra, dict):
                    mail_provider = merged_extra.get("mail_provider", "")
                    if mail_provider:
                        account.extra.setdefault("mail_provider", mail_provider)
                    if req.platform == "chatgpt":
                        for key in (
                            "chatgpt_enable_team_invite",
                            "chatgpt_team_invite_deferred_activation",
                            "chatgpt_capture_free_workspace",
                            "chatgpt_capture_business_workspace",
                            "chatgpt_existing_account_capture",
                            "chatgpt_deferred_invite_team_id",
                            "chatgpt_deferred_invite_team_ids",
                        ):
                            if key in merged_extra:
                                account.extra[key] = merged_extra.get(key)
                    if mail_provider == "luckmail" and req.platform == "chatgpt":
                        mailbox_token = getattr(_mailbox, "_token", "") or ""
                        if mailbox_token:
                            account.extra.setdefault("mailbox_token", mailbox_token)
                        if merged_extra.get("luckmail_project_code"):
                            account.extra.setdefault(
                                "luckmail_project_code",
                                merged_extra.get("luckmail_project_code"),
                            )
                        if merged_extra.get("luckmail_email_type"):
                            account.extra.setdefault(
                                "luckmail_email_type",
                                merged_extra.get("luckmail_email_type"),
                            )
                        if merged_extra.get("luckmail_domain"):
                            account.extra.setdefault(
                                "luckmail_domain", merged_extra.get("luckmail_domain")
                            )
                        if merged_extra.get("luckmail_base_url"):
                            account.extra.setdefault(
                                "luckmail_base_url",
                                merged_extra.get("luckmail_base_url"),
                            )
                additional_accounts_payload = []
                saved_linked_accounts = []
                if isinstance(account.extra, dict):
                    payload = account.extra.pop("_linked_accounts_to_save", None)
                    if isinstance(payload, list):
                        additional_accounts_payload = [item for item in payload if isinstance(item, dict)]
                saved_account = save_account(account)
                pending_invite = None
                if req.platform == "chatgpt" and saved_account is not None:
                    try:
                        from platforms.chatgpt.pending_business_invites import upsert_pending_invite_from_account

                        pending_invite = upsert_pending_invite_from_account(saved_account)
                        if pending_invite is not None:
                            pending_payload = {}
                            if isinstance(saved_account.get_extra(), dict):
                                pending_payload = dict(saved_account.get_extra().get("chatgpt_pending_business_invite") or {})
                            exhausted_team_ids = []
                            for raw in (pending_payload.get("exhausted_team_ids") or []):
                                try:
                                    exhausted_team_id = int(raw)
                                except Exception:
                                    exhausted_team_id = 0
                                if exhausted_team_id > 0 and exhausted_team_id not in exhausted_team_ids:
                                    exhausted_team_ids.append(exhausted_team_id)
                            if deferred_activation_enabled and exhausted_team_ids:
                                with deferred_team_ids_lock:
                                    before_team_ids = list(deferred_team_ids)
                                    deferred_team_ids[:] = [team_id for team_id in deferred_team_ids if team_id not in exhausted_team_ids]
                                    req.extra["chatgpt_deferred_invite_team_ids"] = list(deferred_team_ids)
                                    if deferred_team_ids:
                                        req.extra["chatgpt_deferred_invite_team_id"] = deferred_team_ids[0]
                                    else:
                                        req.extra.pop("chatgpt_deferred_invite_team_id", None)
                                remaining_text = ",".join(str(team_id) for team_id in deferred_team_ids) or "无"
                                removed_text = ",".join(str(team_id) for team_id in exhausted_team_ids)
                                if before_team_ids != deferred_team_ids:
                                    _log(task_id, f"[邀请] team_id={removed_text} 已不可邀请，剩余 team_id={remaining_text}")
                            _log(task_id, f"[邀请] 已保存 pending invite id={pending_invite.id} team={pending_invite.team_id}")
                            if deferred_activation_enabled:
                                with pending_invite_lock:
                                    pending_invite_ids.append(int(pending_invite.id))
                    except Exception as pending_exc:
                        _log(task_id, f"[邀请] 保存 pending invite 失败: {pending_exc}")
                if additional_accounts_payload:
                    from core.base_platform import Account, AccountStatus

                    for extra_account_payload in additional_accounts_payload:
                        try:
                            linked_account = Account(
                                platform=str(extra_account_payload.get("platform") or req.platform),
                                email=str(extra_account_payload.get("email") or account.email or ""),
                                password=str(extra_account_payload.get("password") or account.password or ""),
                                user_id=str(extra_account_payload.get("user_id") or ""),
                                region=str(extra_account_payload.get("region") or ""),
                                token=str(extra_account_payload.get("token") or ""),
                                status=AccountStatus(str(extra_account_payload.get("status") or "registered")),
                                extra=dict(extra_account_payload.get("extra") or {}),
                            )
                            saved_linked = save_account(linked_account)
                            if saved_linked is not None:
                                saved_linked_accounts.append(saved_linked)
                            if isinstance(linked_account.extra, dict):
                                scope_label = str(linked_account.extra.get("chatgpt_workspace_label") or "").strip()
                                if scope_label:
                                    _log(task_id, f"[OK] 已保存附加工作空间: {linked_account.email} [{scope_label}]")
                        except Exception as save_exc:
                            _log(task_id, f"[WARN] 保存附加工作空间失败: {save_exc}")
                if _proxy:
                    proxy_pool.report_success(_proxy)
                if deferred_activation_enabled and pending_invite is not None:
                    _log(task_id, f"[OK] 注册完成并已发送邀请: {account.email}")
                    _save_task_log(
                        req.platform,
                        account.email,
                        "pending_activation",
                        detail=_build_task_log_detail(
                            task_id,
                            {
                                "attempt_outcome": "invite_saved_pending_activation",
                                "email": account.email,
                                "pending_invite_id": int(pending_invite.id or 0),
                            },
                        ),
                    )
                    return AttemptResult.success()
                _log(task_id, f"[OK] 注册成功: {account.email}")
                _auto_upload_integrations(task_id, saved_account or account)
                for linked_saved_account in saved_linked_accounts:
                    _auto_upload_integrations(task_id, linked_saved_account)
                cashier_url = (account.extra or {}).get("cashier_url", "")
                if cashier_url:
                    _log(task_id, f"  [升级链接] {cashier_url}")
                    _task_store.add_cashier_url(task_id, cashier_url)
                _save_task_log(
                    req.platform,
                    account.email,
                    "success",
                    detail=_build_task_log_detail(
                        task_id,
                        {
                            "attempt_outcome": "success",
                            "email": account.email,
                        },
                    ),
                )
                return AttemptResult.success()
            except SkipCurrentAttemptRequested as e:
                _log(task_id, f"[SKIP] 已跳过当前账号: {e}")
                _save_task_log(
                    req.platform,
                    current_email,
                    "skipped",
                    error=str(e),
                    detail=_build_task_log_detail(
                        task_id,
                        {
                            "attempt_outcome": "skipped",
                            "email": current_email,
                        },
                    ),
                )
                return AttemptResult.skipped(str(e))
            except StopTaskRequested as e:
                _log(task_id, f"[STOP] {e}")
                return AttemptResult.stopped(str(e))
            except Exception as e:
                if _proxy and proxy_pool is not None:
                    proxy_pool.report_fail(_proxy)
                error_text = str(e)
                if deferred_activation_enabled and (
                    "所有可用 team 都已不可邀请" in error_text
                    or "预选 team_id 列表当前不可用" in error_text
                ):
                    registration_phase_close_event.set()
                    deferred_phase_close_reason = error_text
                    _log(task_id, f"[邀请] {error_text}")
                    _save_task_log(
                        req.platform,
                        current_email,
                        "failed",
                        error=error_text,
                        detail=_build_task_log_detail(
                            task_id,
                            {
                                "attempt_outcome": "invite_exhausted_stop_phase",
                                "email": current_email,
                            },
                        ),
                    )
                    return AttemptResult.stopped(f"{deferred_phase_close_marker}{error_text}")
                _log(task_id, f"[FAIL] 注册失败: {e}")
                _save_task_log(
                    req.platform,
                    current_email,
                    "failed",
                    error=error_text,
                    detail=_build_task_log_detail(
                        task_id,
                        {
                            "attempt_outcome": "failed",
                            "email": current_email,
                        },
                    ),
                )
                return AttemptResult.failed(error_text)
            finally:
                control.finish_attempt(attempt_id)

        from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed

        max_workers = min(req.concurrency, req.count, 5)
        stopped = False
        registration_phase_closed_for_activation = False
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(_do_one, i) for i in range(req.count)]
            for f in as_completed(futures):
                try:
                    result = f.result()
                except CancelledError:
                    continue
                except Exception as e:
                    _log(task_id, f"[ERROR] 任务线程异常: {e}")
                    errors.append(str(e))
                    continue
                if result.outcome == AttemptOutcome.SUCCESS:
                    success += 1
                    registration_success += 1
                elif result.outcome == AttemptOutcome.SKIPPED:
                    skipped += 1
                elif result.outcome == AttemptOutcome.STOPPED:
                    if result.message.startswith(deferred_phase_close_marker):
                        registration_phase_closed_for_activation = True
                        deferred_phase_close_reason = result.message[len(deferred_phase_close_marker):].strip()
                        if deferred_phase_close_reason:
                            errors.append(deferred_phase_close_reason)
                    else:
                        stopped = True
                else:
                    errors.append(result.message)
                if registration_phase_closed_for_activation or stopped or control.is_stop_requested():
                    if stopped:
                        stopped = True
                    for pending in futures:
                        if pending is not f:
                            pending.cancel()

        if deferred_activation_enabled and (registration_phase_closed_for_activation or not (control.is_stop_requested() or stopped)):
            from core.db import AccountModel
            from platforms.chatgpt.pending_business_invites import activate_pending_invites

            pending_ids = list(pending_invite_ids)
            if registration_phase_closed_for_activation and deferred_phase_close_reason:
                _log(task_id, f"[邀请] {deferred_phase_close_reason}")
            _log(task_id, f"[激活] 注册/邀请阶段完成，准备统一激活 {len(pending_ids)} 个账号")

            def _upload_activation_accounts(item: dict[str, Any]) -> None:
                local_account_id = int(item.get("local_account_id") or 0)
                linked_account_ids = [int(x) for x in item.get("linked_account_ids", []) if x]
                email = str(item.get("email") or "")
                upload_ids = [acc_id for acc_id in [local_account_id] + linked_account_ids if acc_id > 0]

                for index, acc_id in enumerate(upload_ids, start=1):
                    with Session(engine) as s:
                        account_to_upload = s.get(AccountModel, acc_id)
                    if account_to_upload is None:
                        _log(task_id, f"[激活上传] {email} {index}/{len(upload_ids)} 跳过：本地账号不存在 id={acc_id}")
                        continue
                    _log(task_id, f"[激活上传] {email} {index}/{len(upload_ids)} 开始上传")
                    _auto_upload_integrations(task_id, account_to_upload)

            activation_result = activate_pending_invites(
                invite_ids=pending_ids,
                log_fn=lambda message: _log(task_id, message),
                on_success=_upload_activation_accounts,
            )
            success = int(activation_result.get("success") or 0)
            failed_items = [item for item in (activation_result.get("errors") or []) if isinstance(item, dict)]
            for item in failed_items:
                email = str(item.get("email") or "") or f"invite#{item.get('invite_id') or '-'}"
                error_message = str(item.get("error") or "激活失败")
                errors.append(error_message)
                _save_task_log(
                    req.platform,
                    email,
                    "failed",
                    error=error_message,
                    detail=_build_task_log_detail(
                        task_id,
                        {
                            "attempt_outcome": "activation_failed",
                            "email": email,
                            "invite_id": int(item.get("invite_id") or 0),
                        },
                    ),
                )
            for item in [entry for entry in (activation_result.get("results") or []) if isinstance(entry, dict)]:
                local_account_id = int(item.get("local_account_id") or 0)
                linked_account_ids = [int(x) for x in item.get("linked_account_ids", []) if x]
                email = str(item.get("email") or "")
                upload_ids = [acc_id for acc_id in [local_account_id] + linked_account_ids if acc_id > 0]

                _save_task_log(
                    req.platform,
                    email,
                    "success",
                    detail=_build_task_log_detail(
                        task_id,
                        {
                            "attempt_outcome": "activation_success",
                            "email": email,
                            "invite_id": int(item.get("invite_id") or 0),
                            "uploaded_count": len([x for x in upload_ids if x > 0]),
                        },
                    ),
                )
            _log(task_id, f"[激活] 统一激活完成：成功 {success} / {len(pending_ids)}")
    except Exception as e:
        _log(task_id, f"致命错误: {e}")
        _task_store.finish(
            task_id,
            status="failed",
            success=success,
            skipped=skipped,
            errors=errors,
            error=str(e),
        )
        _task_store.cleanup()
        return

    final_status = "stopped" if control.is_stop_requested() or stopped else "done"
    if deferred_activation_enabled:
        if final_status == "stopped":
            summary = (
                f"任务已停止: 注册完成 {registration_success} 个, 激活成功 {success} 个, 跳过 {skipped} 个, 失败 {len(errors)} 个"
            )
        else:
            summary = f"完成: 注册完成 {registration_success} 个, 激活成功 {success} 个, 跳过 {skipped} 个, 失败 {len(errors)} 个"
    elif final_status == "stopped":
        summary = (
            f"任务已停止: 成功 {success} 个, 跳过 {skipped} 个, 失败 {len(errors)} 个"
        )
    else:
        summary = f"完成: 成功 {success} 个, 跳过 {skipped} 个, 失败 {len(errors)} 个"
    _log(task_id, summary)
    _task_store.finish(
        task_id,
        status=final_status,
        success=success,
        skipped=skipped,
        errors=errors,
    )
    _task_store.cleanup()


@router.post("/register")
def create_register_task(
    req: RegisterTaskRequest,
    background_tasks: BackgroundTasks,
):
    task_id = enqueue_register_task(req, background_tasks=background_tasks)
    return {"task_id": task_id}


@router.post("/{task_id}/submit-verification")
def submit_verification(task_id: str, body: SubmitVerificationRequest):
    _ensure_task_mutable(task_id)
    control = _task_store.control_for(task_id)
    try:
        challenge = control.submit_verification(
            challenge_id=body.challenge_id,
            code=body.code,
        )
    except KeyError as e:
        raise HTTPException(404, e.args[0] if e.args else "验证码挑战不存在")
    except ValueError as e:
        raise HTTPException(400, str(e))

    _log(
        task_id,
        "收到人工验证码提交: "
        f"phase={challenge.get('phase')} email={challenge.get('email')}",
    )
    return {"ok": True, "task_id": task_id, "challenge": challenge}


@router.post("/{task_id}/skip-current")
def skip_current_account(task_id: str):
    _ensure_task_mutable(task_id)
    control = _task_store.request_skip_current(task_id)
    _log(task_id, "收到手动跳过当前账号请求")
    return {"ok": True, "task_id": task_id, "control": control}


@router.post("/{task_id}/stop")
def stop_task(task_id: str):
    _ensure_task_mutable(task_id)
    control = _task_store.request_stop(task_id)
    _log(task_id, "收到手动停止任务请求")
    return {"ok": True, "task_id": task_id, "control": control}


@router.get("/logs")
def get_logs(platform: str = None, page: int = 1, page_size: int = 50):
    with Session(engine) as s:
        q = select(TaskLog)
        if platform:
            q = q.where(TaskLog.platform == platform)
        q = q.order_by(TaskLog.id.desc())
        total = len(s.exec(q).all())
        items = s.exec(q.offset((page - 1) * page_size).limit(page_size)).all()
    return {"total": total, "items": [_task_log_summary(item) for item in items]}


@router.get("/logs/{log_id}")
def get_log_detail(log_id: int):
    with Session(engine) as s:
        log = s.get(TaskLog, log_id)
        if log is None:
            raise HTTPException(404, "任务历史不存在")
    return _task_log_detail_payload(log)


@router.post("/logs/batch-delete")
def batch_delete_logs(body: TaskLogBatchDeleteRequest):
    if not body.ids:
        raise HTTPException(400, "任务历史 ID 列表不能为空")

    unique_ids = list(dict.fromkeys(body.ids))
    if len(unique_ids) > 1000:
        raise HTTPException(400, "单次最多删除 1000 条任务历史")

    with Session(engine) as s:
        try:
            logs = s.exec(select(TaskLog).where(TaskLog.id.in_(unique_ids))).all()
            found_ids = {log.id for log in logs if log.id is not None}

            for log in logs:
                s.delete(log)

            s.commit()
            deleted_count = len(found_ids)
            not_found_ids = [log_id for log_id in unique_ids if log_id not in found_ids]
            logger.info("批量删除任务历史成功: %s 条", deleted_count)

            return {
                "deleted": deleted_count,
                "not_found": not_found_ids,
                "total_requested": len(unique_ids),
            }
        except Exception as e:
            s.rollback()
            logger.exception("批量删除任务历史失败")
            raise HTTPException(500, f"批量删除任务历史失败: {str(e)}")


@router.get("/{task_id}/logs/stream")
async def stream_logs(task_id: str, since: int = 0):
    """SSE 实时日志流"""
    _ensure_task_exists(task_id)

    async def event_generator():
        sent = since
        while True:
            logs, status = _task_store.log_state(task_id)
            while sent < len(logs):
                yield f"data: {json.dumps({'line': logs[sent]})}\n\n"
                sent += 1
            if status in ("done", "failed", "stopped"):
                yield f"data: {json.dumps({'done': True, 'status': status})}\n\n"
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/{task_id}")
def get_task(task_id: str):
    _ensure_task_exists(task_id)
    return _task_store.snapshot(task_id)


@router.get("")
def list_tasks():
    return _task_store.list_snapshots()
