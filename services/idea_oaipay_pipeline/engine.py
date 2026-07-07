from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlmodel import Session, select

from core.db import AccountModel, TaskLog, engine
from services.account_filters import (
    account_base_query,
    account_oaipay_state,
    account_subscription_type,
    account_validity,
    filter_account_rows,
)
from services.chatgpt_account_state import classify_chatgpt_capabilities, is_chatgpt_upload_ready
from services.chatgpt_core.local_status_refresh import summarize_status_refresh, sync_chatgpt_account_local_status
from services.oaipay_sync import backfill_chatgpt_account_to_oaipay, get_oaipay_sync_state

from .models import IdeaOaiPayPipelineConfig, IdeaOaiPayPipelineItem, IdeaOaiPayPipelineTask
from .state import IdeaOaiPayPipelineStateStore


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _safe_str(value: Any) -> str:
    return str(value or "").strip()


def _lower(value: Any) -> str:
    return _safe_str(value).lower()


def _safe_json_obj(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _safe_json_list(raw: str) -> list[Any]:
    try:
        data = json.loads(raw or "[]")
    except Exception:
        return []
    return data if isinstance(data, list) else []


def _account_extra(account: AccountModel) -> dict[str, Any]:
    try:
        extra = account.get_extra()
    except Exception:
        return {}
    return extra if isinstance(extra, dict) else {}


def _has_phone_binding(account: AccountModel) -> bool:
    extra = _account_extra(account)
    binding = extra.get("chatgpt_phone_binding") if isinstance(extra.get("chatgpt_phone_binding"), dict) else {}
    status = _lower(binding.get("status") or binding.get("result") or "")
    if status in {"bound", "success", "completed"}:
        return True
    if binding.get("phone") and binding.get("finished_at"):
        return True
    bound_phone = extra.get("chatgpt_bound_phone") if isinstance(extra.get("chatgpt_bound_phone"), dict) else {}
    return bool(
        bound_phone.get("phone")
        or bound_phone.get("phone_number")
        or bound_phone.get("masked")
        or bound_phone.get("masked_phone")
        or extra.get("chatgpt_bound_phone_number")
        or extra.get("chatgpt_bound_phone_masked")
    )


def _account_access_token(account: AccountModel) -> str:
    extra = _account_extra(account)
    return _safe_str(extra.get("access_token") or extra.get("accessToken") or account.token)


class IdeaOaiPayPipelineEngine:
    def __init__(self) -> None:
        self.state_store = IdeaOaiPayPipelineStateStore()
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()
        self._thread: threading.Thread | None = None
        self.status = "stopped"

    def validate_config(self, config: IdeaOaiPayPipelineConfig) -> list[str]:
        errors: list[str] = []
        source_type = _lower(config.source.type)
        if source_type not in {"register", "local"}:
            errors.append("账号来源只能是 register 或 local")
        if source_type == "register" and int(config.source.target_count or 0) <= 0:
            errors.append("注册来源必须设置 target_count > 0")
        if source_type == "local" and not config.source.account_ids and not config.source.all_filtered:
            errors.append("本地账号来源必须提供 account_ids 或 all_filtered=true")
        if config.idea.enabled and not config.idea.use_pool and not _safe_str(config.idea.code_lines):
            errors.append("Idea 开启且不使用卡密池时，必须粘贴卡密")
        if _lower(config.phone.policy) not in {"disabled", "best_effort", "required"}:
            errors.append("手机号策略只能是 disabled / best_effort / required")
        if _lower(config.phone.apply_to) not in {"gate_passed", "all", "free", "plus"}:
            errors.append("手机号适用范围只能是 gate_passed / all / free / plus")
        if _lower(config.check.gate.mode) not in {"none", "account_valid", "subscription_in", "upload_ready"}:
            errors.append("状态放行模式只能是 none / account_valid / subscription_in / upload_ready")
        return errors

    def start(self, config: IdeaOaiPayPipelineConfig) -> IdeaOaiPayPipelineTask:
        errors = self.validate_config(config)
        if errors:
            raise ValueError("; ".join(errors))
        with self._lock:
            latest = self.state_store.get_latest_task()
            if latest is not None and _lower(latest.status) in {"running", "paused"}:
                raise RuntimeError(f"已有流水线正在运行: task_id={latest.id}")
            task_key = f"idea_oaipay_{uuid.uuid4().hex[:12]}"
            payload = config.model_dump(by_alias=True)
            task = self.state_store.create_task(
                task_key,
                status="running",
                source_type=_lower(config.source.type) or "local",
                target_success_count=int(config.source.target_count or 0),
                config=payload,
                runtime_config=payload,
            )
            self.status = "running"
            self._stop_event.clear()
            self._pause_event.set()
            self._log(task.id, f"流水线已启动: source={task.source_type} task_key={task.task_key}")
            if task.source_type == "local":
                count = self._seed_local_items(task, config)
                self._log(task.id, f"本地账号快照完成: items={count}")
                if count <= 0:
                    task = self.state_store.get_task(int(task.id or 0)) or task
                    task.status = "failed"
                    task.stopped_at = _utcnow()
                    task.last_error = "本地账号来源未匹配到任何 ChatGPT 账号"
                    self.state_store.save_task(task)
                    self.status = "failed"
                    self._log(task.id, "流水线结束：本地账号来源为空")
                    return task
            self._ensure_worker()
            return task

    def pause(self) -> None:
        with self._lock:
            task = self.state_store.get_latest_task()
            if task is None or _lower(task.status) != "running":
                return
            task.status = "paused"
            self.state_store.save_task(task)
            self.status = "paused"
            self._pause_event.clear()
            self._log(task.id, "流水线已暂停：不再调度新动作，已启动的子任务会继续自然结束")

    def resume(self) -> None:
        with self._lock:
            task = self.state_store.get_latest_task()
            if task is None or _lower(task.status) != "paused":
                return
            task.status = "running"
            self.state_store.save_task(task)
            self.status = "running"
            self._pause_event.set()
            self._ensure_worker()
            self._log(task.id, "流水线已恢复")

    def stop(self) -> None:
        with self._lock:
            task = self.state_store.get_latest_task()
            if task is not None and _lower(task.status) in {"running", "paused"}:
                task.status = "stopped"
                task.stopped_at = _utcnow()
                self.state_store.save_task(task)
                self._log(task.id, "流水线已停止：不再调度新动作，已启动子任务请在任务中心单独中断")
            self.status = "stopped"
            self._stop_event.set()
            self._pause_event.set()

    def shutdown(self) -> None:
        """Stop the in-process scheduler without changing persisted task state."""
        with self._lock:
            self.status = "stopped"
            self._stop_event.set()
            self._pause_event.set()

    def recover_latest_task(self) -> IdeaOaiPayPipelineTask | None:
        task = self.state_store.get_latest_task()
        if task is None:
            return None
        status = _lower(task.status)
        self.status = status or "stopped"
        if status in {"running", "paused"}:
            if status == "paused":
                self._pause_event.clear()
            else:
                self._pause_event.set()
            self._ensure_worker()
        return task

    def get_status_snapshot(self, *, item_limit: int = 500) -> dict[str, Any]:
        task = self.state_store.get_latest_task()
        if task is None:
            return {
                "task": None,
                "config": IdeaOaiPayPipelineConfig().model_dump(),
                "summary": {},
                "items": [],
                "logs": [],
                "history": [],
            }
        summary = self.state_store.update_task_summary(int(task.id or 0))
        task = self.state_store.get_task(int(task.id or 0)) or task
        return {
            "task": self._task_to_dict(task),
            "config": self.state_store.task_config(task),
            "summary": summary,
            "items": [self._item_to_dict(item) for item in self.state_store.list_items(int(task.id or 0), limit=item_limit)],
            "logs": self.state_store.list_task_logs(int(task.id or 0)),
            "history": [self._task_to_dict(item) for item in self.state_store.list_tasks(limit=20)],
        }

    def retry_item_stage(self, item_id: int, stage: str) -> dict[str, Any]:
        item = self.state_store.get_item(item_id)
        if item is None:
            raise ValueError("流水线账号不存在")
        latest = self.state_store.get_latest_task()
        if latest is None or int(latest.id or 0) != int(item.pipeline_task_id or 0):
            raise ValueError("只能重试最近一条流水线任务中的账号；历史任务请重新启动一条新流水线")
        stage = _lower(stage)
        patch: dict[str, Any]
        if stage == "idea":
            patch = {"idea_stage": "pending", "overall_status": "pending", "idea_error": "", "last_error": ""}
        elif stage == "check":
            patch = {"check_stage": "pending", "gate_stage": "pending", "overall_status": "pending", "last_error": ""}
        elif stage == "phone":
            patch = {"phone_stage": "pending", "phone_error": "", "overall_status": "pending", "last_error": ""}
        elif stage == "oaipay":
            patch = {"oaipay_stage": "pending", "oaipay_message": "", "overall_status": "pending", "last_error": ""}
        elif stage == "skip-phone":
            patch = {"phone_stage": "skipped", "phone_error": "", "overall_status": "pending", "last_error": ""}
        else:
            raise ValueError("未知重试阶段")
        updated = self.state_store.update_item(item_id, **patch)
        task = self.state_store.get_task(int(item.pipeline_task_id or 0))
        if task is not None and _lower(task.status) in {"stopped", "done", "failed"}:
            task.status = "running"
            task.stopped_at = None
            self.state_store.save_task(task)
        self.status = "running"
        self._stop_event.clear()
        self._pause_event.set()
        self._ensure_worker()
        self._log(item.pipeline_task_id, f"已重置账号阶段: item_id={item_id} stage={stage}")
        return self._item_to_dict(updated) if updated else {}

    def _ensure_worker(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run_loop, name="idea-oaipay-pipeline", daemon=True)
        self._thread.start()

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            self._pause_event.wait()
            if self._stop_event.is_set():
                break
            task = self.state_store.get_latest_task()
            if task is None or _lower(task.status) != "running":
                time.sleep(1)
                continue
            config = IdeaOaiPayPipelineConfig(**self.state_store.task_runtime_config(task))
            try:
                self._tick(task, config)
            except Exception as exc:
                task = self.state_store.get_task(int(task.id or 0)) or task
                task.last_error = str(exc or "流水线调度异常")
                self.state_store.save_task(task)
                self._log(task.id, f"[ERROR] 流水线调度异常: {exc}")
            interval = max(1, min(int(config.tick_interval_seconds or 3), 60))
            for _ in range(interval):
                if self._stop_event.is_set() or not self._pause_event.is_set():
                    break
                time.sleep(1)

    def _tick(self, task: IdeaOaiPayPipelineTask, config: IdeaOaiPayPipelineConfig) -> None:
        task_id = int(task.id or 0)
        if task.source_type == "register":
            if task.active_register_task_id:
                self._poll_register_task(task, config)
                return
            self._maybe_start_register_task(task, config)

        task = self.state_store.get_task(task_id) or task
        if task.active_idea_task_id:
            self._poll_idea_task(task, config)
            return
        self._prepare_or_start_idea(task, config)

        task = self.state_store.get_task(task_id) or task
        self._run_check_step(task, config, limit=5)

        task = self.state_store.get_task(task_id) or task
        if task.active_phone_task_id:
            self._poll_phone_task(task, config)
            return
        self._prepare_or_start_phone(task, config)

        task = self.state_store.get_task(task_id) or task
        self._run_oaipay_step(task, config, limit=3)
        self._finalize_if_complete(task, config)
        self.state_store.update_task_summary(task_id)

    def _seed_local_items(self, task: IdeaOaiPayPipelineTask, config: IdeaOaiPayPipelineConfig) -> int:
        accounts = self._resolve_source_accounts(config)
        items: list[IdeaOaiPayPipelineItem] = []
        for account in accounts:
            extra = _account_extra(account)
            sub = account_subscription_type(account, extra)
            validity = account_validity(account, extra)
            phone_stage = "disabled" if _lower(config.phone.policy) == "disabled" else "pending"
            oaipay_stage = "pending" if config.oaipay.enabled else "disabled"
            items.append(
                IdeaOaiPayPipelineItem(
                    pipeline_task_id=int(task.id or 0),
                    account_id=int(account.id or 0),
                    email=str(account.email or ""),
                    source_stage="selected",
                    register_stage="skipped",
                    idea_stage="pending" if config.idea.enabled else "disabled",
                    check_stage="pending" if config.check.enabled else "skipped",
                    gate_stage="pending" if config.check.gate.enabled else "skipped",
                    phone_stage=phone_stage,
                    phone_policy=_lower(config.phone.policy),
                    oaipay_stage=oaipay_stage,
                    overall_status="pending",
                    subscription_type_before=sub,
                    account_validity=validity,
                )
            )
        self.state_store.bulk_create_items(items)
        return len(items)

    def _resolve_source_accounts(self, config: IdeaOaiPayPipelineConfig) -> list[AccountModel]:
        source = config.source
        requested_ids: list[int] = []
        seen: set[int] = set()
        for value in source.account_ids or []:
            try:
                account_id = int(value or 0)
            except Exception:
                account_id = 0
            if account_id > 0 and account_id not in seen:
                seen.add(account_id)
                requested_ids.append(account_id)
        limit = max(0, int(source.limit or 0))
        with Session(engine) as session:
            if requested_ids:
                rows = list(
                    session.exec(
                        select(AccountModel)
                        .where(AccountModel.platform == "chatgpt")
                        .where(AccountModel.id.in_(requested_ids))
                    ).all()
                )
                row_map = {int(row.id or 0): row for row in rows}
                ordered = [row_map[account_id] for account_id in requested_ids if account_id in row_map]
                return ordered[:limit] if limit > 0 else ordered
            if not source.all_filtered:
                return []
            query = account_base_query(platform="chatgpt", status=source.status, email=source.email)
            rows = list(session.exec(query).all())
            filtered = filter_account_rows(
                rows,
                manually_used=self._optional_bool(source.manually_used),
                auth_type=source.auth_type,
                subscription_type=source.subscription_type,
                account_validity_filter=source.account_validity,
                sub2api_state=source.sub2api_state,
                idea_submit_state=source.idea_submit_state,
            )
            oaipay_filter = _lower(source.oaipay_state)
            if oaipay_filter:
                filtered = [row for row in filtered if account_oaipay_state(row) == oaipay_filter]
            return filtered[:limit] if limit > 0 else filtered[:1000]

    def _maybe_start_register_task(self, task: IdeaOaiPayPipelineTask, config: IdeaOaiPayPipelineConfig) -> None:
        target = max(0, int(config.source.target_count or task.target_success_count or 0))
        if target <= 0:
            return
        items = self.state_store.list_items(int(task.id or 0), limit=5000)
        done = len([item for item in items if item.overall_status == "done"])
        promising = len([item for item in items if item.overall_status not in {"failed", "manual_required", "skipped"}])
        if done >= target or promising >= target:
            return
        refill_count = max(1, min(target - promising, int(config.source.register_config.get("batch_size") or target - promising or 1), 50))
        from api.tasks import RegisterTaskRequest, enqueue_register_task

        register_extra = dict(config.source.register_config.get("extra") if isinstance(config.source.register_config.get("extra"), dict) else {})
        register_extra.update(
            {
                "mail_provider": _safe_str(config.source.register_config.get("mail_provider") or register_extra.get("mail_provider") or ""),
                "chatgpt_registration_mode": register_extra.get("chatgpt_registration_mode") or "access_token_only",
                "chatgpt_access_token_only_checkout_amount_check_enabled": False,
                "chatgpt_capture_free_workspace": False,
            }
        )
        req = RegisterTaskRequest(
            platform="chatgpt",
            count=refill_count,
            concurrency=max(1, int(config.source.register_config.get("concurrency") or 1)),
            register_delay_seconds=float(config.source.register_config.get("register_delay_seconds") or 0),
            register_delay_max_seconds=float(config.source.register_config.get("register_delay_max_seconds") or 0),
            proxy=config.source.register_config.get("proxy"),
            proxy_mode=_safe_str(config.source.register_config.get("proxy_mode") or ""),
            proxy_country_code=_safe_str(config.source.register_config.get("proxy_country_code") or ""),
            proxy_failover=bool(config.source.register_config.get("proxy_failover") or False),
            proxy_max_candidates=int(config.source.register_config.get("proxy_max_candidates") or 0),
            proxy_min_score=float(config.source.register_config.get("proxy_min_score") or 0),
            executor_type=_safe_str(config.source.register_config.get("executor_type") or "protocol"),
            captcha_solver=_safe_str(config.source.register_config.get("captcha_solver") or "yescaptcha"),
            extra=register_extra,
        )
        meta = {"idea_oaipay_pipeline_task_id": int(task.id or 0), "pipeline_key": task.task_key}
        child_task_id = enqueue_register_task(req, source="idea_oaipay_pipeline", meta=meta)
        task.active_register_task_id = child_task_id
        self.state_store.save_task(task)
        self._log(task.id, f"启动注册补位: task_id={child_task_id} count={refill_count}")

    def _poll_register_task(self, task: IdeaOaiPayPipelineTask, config: IdeaOaiPayPipelineConfig) -> None:
        from api.tasks import get_task

        child_id = task.active_register_task_id
        try:
            snapshot = get_task(child_id)
        except Exception as exc:
            task.active_register_task_id = ""
            self.state_store.save_task(task)
            self._log(task.id, f"[WARN] 注册子任务引用已失效，已清理并等待补位: task_id={child_id} error={exc}")
            return
        status = _lower((snapshot or {}).get("status"))
        if status not in {"done", "failed", "stopped"}:
            return
        created = self._collect_registered_accounts(task, child_id, config, snapshot=snapshot)
        if status != "done" and created <= 0:
            self._log(task.id, f"注册子任务未成功: task_id={child_id} status={status}")
        task.active_register_task_id = ""
        self.state_store.save_task(task)
        self._log(task.id, f"注册子任务结束: task_id={child_id} status={status} collected={created}")

    def _collect_registered_accounts(
        self,
        task: IdeaOaiPayPipelineTask,
        child_task_id: str,
        config: IdeaOaiPayPipelineConfig,
        *,
        snapshot: dict[str, Any] | None = None,
    ) -> int:
        created = 0
        task_id = int(task.id or 0)
        registered_accounts: list[dict[str, Any]] = []
        meta = snapshot.get("meta") if isinstance(snapshot, dict) and isinstance(snapshot.get("meta"), dict) else {}
        meta_rows = meta.get("registered_accounts") if isinstance(meta.get("registered_accounts"), list) else []
        for row in meta_rows:
            if isinstance(row, dict):
                registered_accounts.append(row)
        with Session(engine) as session:
            if not registered_accounts:
                logs = list(
                    session.exec(
                        select(TaskLog)
                        .where(TaskLog.platform == "chatgpt")
                        .where(TaskLog.status == "success")
                        .order_by(TaskLog.id.asc())
                    ).all()
                )
                for log in logs:
                    detail = _safe_json_obj(log.detail_json)
                    if _safe_str(detail.get("task_id")) != child_task_id:
                        continue
                    log_meta = detail.get("meta") if isinstance(detail.get("meta"), dict) else {}
                    if int(log_meta.get("idea_oaipay_pipeline_task_id") or 0) != task_id:
                        continue
                    registered_accounts.append({"email": _safe_str(detail.get("email") or log.email)})
            for row in registered_accounts:
                account_id = int(row.get("account_id") or 0)
                email = _safe_str(row.get("email"))
                if account_id > 0:
                    account = session.get(AccountModel, account_id)
                elif email:
                    account = session.exec(
                        select(AccountModel)
                        .where(AccountModel.platform == "chatgpt")
                        .where(AccountModel.email == email)
                        .order_by(AccountModel.created_at.desc(), AccountModel.id.desc())
                    ).first()
                else:
                    account = None
                if account is None or int(account.id or 0) <= 0:
                    continue
                if self.state_store.get_item_by_account_id(task_id, int(account.id or 0)):
                    continue
                extra = _account_extra(account)
                sub = account_subscription_type(account, extra)
                validity = account_validity(account, extra)
                self.state_store.create_item(
                    IdeaOaiPayPipelineItem(
                        pipeline_task_id=task_id,
                        account_id=int(account.id or 0),
                        email=str(account.email or ""),
                        source_stage="registered",
                        register_stage="success",
                        idea_stage="pending" if config.idea.enabled else "disabled",
                        check_stage="pending" if config.check.enabled else "skipped",
                        gate_stage="pending" if config.check.gate.enabled else "skipped",
                        phone_stage="disabled" if _lower(config.phone.policy) == "disabled" else "pending",
                        phone_policy=_lower(config.phone.policy),
                        oaipay_stage="pending" if config.oaipay.enabled else "disabled",
                        overall_status="pending",
                        subscription_type_before=sub,
                        account_validity=validity,
                    )
                )
                created += 1
        return created

    def _prepare_or_start_idea(self, task: IdeaOaiPayPipelineTask, config: IdeaOaiPayPipelineConfig) -> None:
        task_id = int(task.id or 0)
        if not config.idea.enabled:
            for item in self.state_store.list_items_by_statuses(task_id, idea_stages=["pending"], limit=100):
                self.state_store.update_item(int(item.id or 0), idea_stage="disabled")
            return
        candidates = self.state_store.list_items_by_statuses(task_id, idea_stages=["pending"], limit=100)
        ready_account_ids: list[int] = []
        for item in candidates:
            account_id = int(item.account_id or 0)
            if account_id <= 0:
                self.state_store.update_item(int(item.id or 0), idea_stage="failed", overall_status="failed", idea_error="缺少账号 ID", last_error="缺少账号 ID")
                continue
            with Session(engine) as session:
                account = session.get(AccountModel, account_id)
                if account is None or account.platform != "chatgpt":
                    self.state_store.update_item(int(item.id or 0), idea_stage="failed", overall_status="failed", idea_error="账号不存在", last_error="账号不存在")
                    continue
                sub = account_subscription_type(account)
                if sub in {_lower(value) for value in config.idea.skip_if_subscription_in}:
                    self.state_store.update_item(
                        int(item.id or 0),
                        idea_stage="skipped",
                        subscription_type_before=sub,
                        check_stage="pending" if config.check.enabled else "skipped",
                    )
                    continue
                if not _account_access_token(account):
                    self.state_store.update_item(int(item.id or 0), idea_stage="failed", overall_status="failed", idea_error="账号缺少 Access Token", last_error="账号缺少 Access Token")
                    continue
            ready_account_ids.append(account_id)
        if not ready_account_ids:
            return
        from api.tasks import BaxiGptCdkSubmitTaskRequest, enqueue_baxigpt_cdk_submit_task

        req = BaxiGptCdkSubmitTaskRequest(
            account_ids=ready_account_ids,
            code_lines=config.idea.code_lines,
            use_pool=config.idea.use_pool,
            precheck=config.idea.precheck,
            failure_continue=config.idea.failure_continue,
            submit_interval_seconds=max(0, int(config.idea.submit_interval_seconds or 0)),
            auto_poll_status=config.idea.auto_poll_status,
            status_poll_interval_seconds=max(1, int(config.idea.status_poll_interval_seconds or 5)),
            status_poll_timeout_seconds=max(1800, int(config.idea.status_poll_timeout_seconds or 1800)),
        )
        result = enqueue_baxigpt_cdk_submit_task(req)
        child_id = _safe_str((result or {}).get("task_id"))
        if not child_id:
            reason = "Idea 没有可提交配对，可能缺卡密/账号不满足条件"
            for account_id in ready_account_ids:
                item = self.state_store.get_item_by_account_id(task_id, account_id)
                if item:
                    self.state_store.update_item(int(item.id or 0), idea_stage="failed", overall_status="failed", idea_error=reason, last_error=reason)
            self._log(task.id, reason)
            return
        for account_id in ready_account_ids:
            item = self.state_store.get_item_by_account_id(task_id, account_id)
            if item:
                self.state_store.update_item(int(item.id or 0), idea_stage="submitting", overall_status="running", idea_task_id=child_id)
        task.active_idea_task_id = child_id
        self.state_store.save_task(task)
        self._log(task.id, f"启动 Idea 批量提交: task_id={child_id} accounts={len(ready_account_ids)}")

    def _poll_idea_task(self, task: IdeaOaiPayPipelineTask, config: IdeaOaiPayPipelineConfig) -> None:
        from api.tasks import get_task

        child_id = task.active_idea_task_id
        try:
            snapshot = get_task(child_id)
        except Exception as exc:
            reason = f"Idea 子任务引用已失效: {exc}"
            for item in self.state_store.list_items_by_statuses(int(task.id or 0), idea_stages=["submitting", "polling"], limit=1000):
                if item.idea_task_id == child_id:
                    self.state_store.update_item(
                        int(item.id or 0),
                        idea_stage="failed",
                        overall_status="manual_required",
                        idea_error=reason,
                        last_error=reason,
                    )
            task.active_idea_task_id = ""
            self.state_store.save_task(task)
            self._log(task.id, f"[WARN] {reason} task_id={child_id}")
            return
        status = _lower((snapshot or {}).get("status"))
        for item in self.state_store.list_items_by_statuses(int(task.id or 0), idea_stages=["submitting"], limit=1000):
            self.state_store.update_item(int(item.id or 0), idea_stage="polling")
        if status not in {"done", "failed", "stopped"}:
            return
        meta = snapshot.get("meta") if isinstance(snapshot.get("meta"), dict) else {}
        results = meta.get("runtime_results") if isinstance(meta.get("runtime_results"), list) else []
        by_account_id: dict[int, dict[str, Any]] = {}
        for result in results:
            if not isinstance(result, dict):
                continue
            account_id = int(result.get("account_id") or 0)
            if account_id > 0:
                by_account_id[account_id] = result
        for item in self.state_store.list_items(int(task.id or 0), limit=5000):
            if item.idea_task_id != child_id or item.idea_stage not in {"submitting", "polling"}:
                continue
            result = by_account_id.get(int(item.account_id or 0), {})
            r_status = _lower(result.get("status"))
            order_id = _safe_str(result.get("order_id"))
            cdk_id = int(result.get("cdk_id") or 0)
            code_masked = _safe_str(result.get("code_masked"))
            if r_status in {"paid", "success", "completed"}:
                self.state_store.update_item(
                    int(item.id or 0),
                    idea_stage="paid",
                    check_stage="pending" if config.check.enabled else "skipped",
                    cdk_id=cdk_id,
                    cdk_masked=code_masked,
                    idea_order_id=order_id,
                    overall_status="pending",
                )
            elif r_status == "timeout":
                self.state_store.update_item(
                    int(item.id or 0),
                    idea_stage="timeout",
                    idea_order_id=order_id,
                    idea_error=_safe_str(result.get("reason") or "Idea 轮询超时"),
                    overall_status="manual_required",
                    last_error=_safe_str(result.get("reason") or "Idea 轮询超时"),
                )
            elif result:
                reason = _safe_str(result.get("reason") or result.get("message") or "Idea 开通失败")
                self.state_store.update_item(
                    int(item.id or 0),
                    idea_stage="failed",
                    cdk_id=cdk_id,
                    cdk_masked=code_masked,
                    idea_order_id=order_id,
                    idea_error=reason,
                    overall_status="failed",
                    last_error=reason,
                )
            else:
                reason = "Idea 子任务结束但未返回该账号结果"
                self.state_store.update_item(int(item.id or 0), idea_stage="failed", overall_status="failed", idea_error=reason, last_error=reason)
        task.active_idea_task_id = ""
        self.state_store.save_task(task)
        self._log(task.id, f"Idea 子任务结束: task_id={child_id} status={status}")

    def _run_check_step(self, task: IdeaOaiPayPipelineTask, config: IdeaOaiPayPipelineConfig, *, limit: int) -> None:
        task_id = int(task.id or 0)
        if not config.check.enabled:
            for item in self.state_store.list_items_by_statuses(task_id, check_stages=["pending"], limit=limit):
                self.state_store.update_item(int(item.id or 0), check_stage="skipped", gate_stage="skipped")
            return
        candidates = [
            item for item in self.state_store.list_items_by_statuses(task_id, check_stages=["pending"], limit=limit)
            if item.idea_stage in {"paid", "skipped", "disabled"}
        ]
        for item in candidates:
            self.state_store.update_item(int(item.id or 0), check_stage="running", overall_status="running")
            with Session(engine) as session:
                account = session.get(AccountModel, int(item.account_id or 0)) if int(item.account_id or 0) > 0 else None
                if account is None or account.platform != "chatgpt":
                    self.state_store.update_item(int(item.id or 0), check_stage="failed", gate_stage="blocked", overall_status="failed", last_error="账号不存在")
                    continue
                before = account_subscription_type(account)
                try:
                    refresh_result = sync_chatgpt_account_local_status(session, account)
                    summary = summarize_status_refresh(refresh_result, trigger="idea_oaipay_pipeline")
                    session.commit()
                    try:
                        session.refresh(account)
                    except Exception:
                        pass
                    after = account_subscription_type(account)
                    validity = account_validity(account)
                    gate_ok, gate_msg = self._evaluate_status_gate(account, config)
                    self.state_store.update_item(
                        int(item.id or 0),
                        check_stage="refreshed",
                        gate_stage="pass" if gate_ok else "blocked",
                        subscription_type_before=item.subscription_type_before or before,
                        subscription_type_after=after,
                        account_validity=validity,
                        overall_status="pending" if gate_ok else "manual_required",
                        last_error="" if gate_ok else gate_msg,
                        details_json=json.dumps({"local_status_refresh": summary}, ensure_ascii=False),
                    )
                    self._log(task.id, f"本地状态刷新: {account.email} sub={after} validity={validity} gate={'pass' if gate_ok else 'blocked'}")
                except Exception as exc:
                    session.rollback()
                    error = str(exc or "本地状态刷新失败")
                    self.state_store.update_item(int(item.id or 0), check_stage="failed", gate_stage="blocked", overall_status="manual_required", last_error=error)
                    self._log(task.id, f"[WARN] 本地状态刷新失败: {account.email} - {error}")

    def _evaluate_status_gate(self, account: AccountModel, config: IdeaOaiPayPipelineConfig) -> tuple[bool, str]:
        gate = config.check.gate
        if not gate.enabled or _lower(gate.mode) == "none":
            return True, ""
        sub = account_subscription_type(account)
        validity = account_validity(account)
        mode = _lower(gate.mode)
        if mode == "account_valid":
            return validity != "invalid", "账号无效" if validity == "invalid" else ""
        if mode == "subscription_in":
            allowed = {_lower(value) for value in gate.allowed_subscription_types if _safe_str(value)}
            if not allowed:
                return True, ""
            return sub in allowed, f"订阅类型 {sub or 'unknown'} 不在放行范围 {','.join(sorted(allowed))}"
        if mode == "upload_ready":
            ready, msg, _caps = is_chatgpt_upload_ready(account)
            return bool(ready), _safe_str(msg or "账号未满足上传条件")
        return True, ""

    def _prepare_or_start_phone(self, task: IdeaOaiPayPipelineTask, config: IdeaOaiPayPipelineConfig) -> None:
        policy = _lower(config.phone.policy)
        task_id = int(task.id or 0)
        if policy == "disabled":
            for item in self.state_store.list_items_by_statuses(task_id, phone_stages=["pending"], limit=100):
                self.state_store.update_item(int(item.id or 0), phone_stage="disabled")
            return
        candidate = None
        for item in self.state_store.list_items_by_statuses(task_id, phone_stages=["pending"], limit=50):
            if item.gate_stage not in {"pass", "skipped"}:
                continue
            if not self._phone_applies(item, config):
                self.state_store.update_item(int(item.id or 0), phone_stage="skipped")
                continue
            with Session(engine) as session:
                account = session.get(AccountModel, int(item.account_id or 0)) if int(item.account_id or 0) > 0 else None
                if account is not None and _has_phone_binding(account):
                    self.state_store.update_item(int(item.id or 0), phone_stage="success")
                    continue
            candidate = item
            break
        if candidate is None:
            return
        from api.tasks import PhoneBindingTestTaskRequest, enqueue_phone_binding_test_task

        req = PhoneBindingTestTaskRequest(
            account_ids=[int(candidate.account_id or 0)],
            use_pool=bool(config.phone.use_pool) and not _safe_str(config.phone.phone_lines),
            phone_lines=config.phone.phone_lines,
            timeout_seconds=max(30, int(config.phone.timeout_seconds or 180)),
            poll_interval_seconds=max(1, int(config.phone.poll_interval_seconds or 5)),
            max_resend_attempts=max(0, int(config.phone.max_resend_attempts or 0)),
            resend_interval_seconds=max(1, int(config.phone.resend_interval_seconds or 30)),
            account_interval_seconds=max(0, int(config.phone.account_interval_seconds or 0)),
            proxy=config.phone.proxy,
            proxy_mode=config.phone.proxy_mode,
            proxy_country_code=config.phone.proxy_country_code,
            proxy_failover=config.phone.proxy_failover,
            proxy_max_candidates=config.phone.proxy_max_candidates,
            proxy_min_score=config.phone.proxy_min_score,
            sms_probe_only=False,
            prefix_sms_probe_only=False,
        )
        try:
            result = enqueue_phone_binding_test_task(req)
        except Exception as exc:
            detail = getattr(exc, "detail", None)
            reason = _safe_str(detail or exc or "手机号绑定任务未启动")
            stage = "failed" if policy == "required" else "skipped"
            overall = "manual_required" if policy == "required" else "pending"
            self.state_store.update_item(
                int(candidate.id or 0),
                phone_stage=stage,
                phone_error=reason,
                overall_status=overall,
                last_error=reason if policy == "required" else "",
            )
            self._log(task.id, f"[WARN] 手机号绑定任务未启动: {candidate.email} - {reason}")
            return
        child_id = _safe_str((result or {}).get("task_id"))
        if not child_id:
            reason = "手机号绑定任务未启动：没有可用号码或账号不满足条件"
            stage = "failed" if policy == "required" else "skipped"
            overall = "manual_required" if policy == "required" else "pending"
            self.state_store.update_item(int(candidate.id or 0), phone_stage=stage, phone_error=reason, overall_status=overall, last_error=reason if policy == "required" else "")
            self._log(task.id, reason)
            return
        self.state_store.update_item(int(candidate.id or 0), phone_stage="running", phone_task_id=child_id, phone_policy=policy, overall_status="running")
        task.active_phone_task_id = child_id
        self.state_store.save_task(task)
        self._log(task.id, f"启动手机号绑定: task_id={child_id} account={candidate.email}")

    def _phone_applies(self, item: IdeaOaiPayPipelineItem, config: IdeaOaiPayPipelineConfig) -> bool:
        apply_to = _lower(config.phone.apply_to)
        if apply_to in {"", "gate_passed", "all"}:
            return True
        sub = _lower(item.subscription_type_after or item.subscription_type_before)
        if apply_to == "free":
            return sub == "free"
        if apply_to == "plus":
            return sub in {"plus", "pro", "team", "enterprise"}
        return True

    def _poll_phone_task(self, task: IdeaOaiPayPipelineTask, config: IdeaOaiPayPipelineConfig) -> None:
        from api.tasks import get_task

        child_id = task.active_phone_task_id
        try:
            snapshot = get_task(child_id)
        except Exception as exc:
            reason = f"手机号绑定子任务引用已失效: {exc}"
            for item in self.state_store.list_items_by_statuses(int(task.id or 0), phone_stages=["running"], limit=100):
                if item.phone_task_id != child_id:
                    continue
                if _lower(config.phone.policy) == "required":
                    self.state_store.update_item(int(item.id or 0), phone_stage="failed", overall_status="manual_required", phone_error=reason, last_error=reason)
                else:
                    self.state_store.update_item(int(item.id or 0), phone_stage="failed", overall_status="pending", phone_error=reason, last_error="")
            task.active_phone_task_id = ""
            self.state_store.save_task(task)
            self._log(task.id, f"[WARN] {reason} task_id={child_id}")
            return
        status = _lower((snapshot or {}).get("status"))
        if status not in {"done", "failed", "stopped"}:
            return
        meta = snapshot.get("meta") if isinstance(snapshot.get("meta"), dict) else {}
        account_results = meta.get("account_results") if isinstance(meta.get("account_results"), list) else []
        result_by_account: dict[int, dict[str, Any]] = {}
        for result in account_results:
            if isinstance(result, dict) and int(result.get("account_id") or 0) > 0:
                result_by_account[int(result.get("account_id") or 0)] = result
        for item in self.state_store.list_items_by_statuses(int(task.id or 0), phone_stages=["running"], limit=100):
            if item.phone_task_id != child_id:
                continue
            result = result_by_account.get(int(item.account_id or 0), {})
            result_status = _lower(result.get("status"))
            with Session(engine) as session:
                account = session.get(AccountModel, int(item.account_id or 0)) if int(item.account_id or 0) > 0 else None
                bound = _has_phone_binding(account) if account is not None else False
            if bound or result_status in {"used_for_binding", "bound", "success"}:
                self.state_store.update_item(int(item.id or 0), phone_stage="success", overall_status="pending", phone_error="", last_error="")
            else:
                reason = _safe_str(result.get("reason") or snapshot.get("error") or "手机号绑定失败")
                if _lower(config.phone.policy) == "required":
                    self.state_store.update_item(int(item.id or 0), phone_stage="failed", overall_status="manual_required", phone_error=reason, last_error=reason)
                else:
                    self.state_store.update_item(int(item.id or 0), phone_stage="failed", overall_status="pending", phone_error=reason, last_error="")
        task.active_phone_task_id = ""
        self.state_store.save_task(task)
        self._log(task.id, f"手机号绑定子任务结束: task_id={child_id} status={status}")

    def _run_oaipay_step(self, task: IdeaOaiPayPipelineTask, config: IdeaOaiPayPipelineConfig, *, limit: int) -> None:
        task_id = int(task.id or 0)
        if not config.oaipay.enabled:
            for item in self.state_store.list_items_by_statuses(task_id, oaipay_stages=["pending", "disabled"], limit=limit):
                if self._ready_without_oaipay(item, config):
                    self.state_store.update_item(int(item.id or 0), oaipay_stage="disabled", overall_status="done", last_error="")
            return
        candidates = self.state_store.list_items_by_statuses(task_id, oaipay_stages=["pending"], limit=limit)
        for item in candidates:
            if item.gate_stage not in {"pass", "skipped"}:
                continue
            if not self._phone_passed_for_oaipay(item, config):
                continue
            if not self._oaipay_requirements_pass(item, config):
                continue
            self.state_store.update_item(int(item.id or 0), oaipay_stage="probing", overall_status="running")
            with Session(engine) as session:
                account = session.get(AccountModel, int(item.account_id or 0)) if int(item.account_id or 0) > 0 else None
                if account is None or account.platform != "chatgpt":
                    self.state_store.update_item(int(item.id or 0), oaipay_stage="failed", overall_status="failed", oaipay_message="账号不存在", last_error="账号不存在")
                    continue
                try:
                    outcome = backfill_chatgpt_account_to_oaipay(
                        account,
                        session=session,
                        commit=True,
                        category_mode="auto",
                        fallback_category_id=config.oaipay.category_id,
                    )
                    sync_state = get_oaipay_sync_state(account)
                    remote_state = _lower(sync_state.get("remote_state")) or ("uploaded" if outcome.get("uploaded") else "")
                    ok = bool(outcome.get("ok"))
                    skipped = bool(outcome.get("skipped"))
                    message = _safe_str(outcome.get("message") or sync_state.get("message"))
                    if ok and (outcome.get("uploaded") or remote_state == "uploaded"):
                        stage = "uploaded"
                    elif ok and skipped and config.oaipay.exists_as_success:
                        stage = "exists"
                    elif remote_state == "exists" and config.oaipay.exists_as_success:
                        stage = "exists"
                    elif remote_state == "ambiguous":
                        stage = "ambiguous"
                    else:
                        stage = "failed"
                    overall = "done" if stage in {"uploaded", "exists"} else "manual_required"
                    self.state_store.update_item(
                        int(item.id or 0),
                        oaipay_stage=stage,
                        oaipay_remote_state=remote_state,
                        oaipay_remote_account_id=_safe_str(sync_state.get("remote_account_id")),
                        oaipay_message=message,
                        overall_status=overall,
                        last_error="" if overall == "done" else message,
                    )
                    self._log(task.id, f"OAIPay 处理完成: {account.email} stage={stage} msg={message[:120]}")
                except Exception as exc:
                    session.rollback()
                    error = str(exc or "OAIPay 上传失败")
                    self.state_store.update_item(int(item.id or 0), oaipay_stage="failed", overall_status="manual_required", oaipay_message=error, last_error=error)
                    self._log(task.id, f"[WARN] OAIPay 上传失败: {account.email} - {error}")

    def _ready_without_oaipay(self, item: IdeaOaiPayPipelineItem, config: IdeaOaiPayPipelineConfig) -> bool:
        return item.gate_stage in {"pass", "skipped"} and self._phone_passed_for_oaipay(item, config)

    def _phone_passed_for_oaipay(self, item: IdeaOaiPayPipelineItem, config: IdeaOaiPayPipelineConfig) -> bool:
        policy = _lower(config.phone.policy)
        if policy == "disabled":
            return item.phone_stage in {"disabled", "skipped", "success"}
        if policy == "required":
            return item.phone_stage == "success"
        return item.phone_stage in {"success", "failed", "skipped", "disabled"}

    def _oaipay_requirements_pass(self, item: IdeaOaiPayPipelineItem, config: IdeaOaiPayPipelineConfig) -> bool:
        required_subs = {_lower(value) for value in config.oaipay.require_subscription_in if _safe_str(value)}
        if required_subs and _lower(item.subscription_type_after or item.subscription_type_before) not in required_subs:
            reason = f"OAIPay 上传要求订阅类型 {','.join(sorted(required_subs))}"
            self.state_store.update_item(int(item.id or 0), oaipay_stage="skipped", overall_status="manual_required", oaipay_message=reason, last_error=reason)
            return False
        if config.oaipay.require_phone_bound and item.phone_stage != "success":
            with Session(engine) as session:
                account = session.get(AccountModel, int(item.account_id or 0)) if int(item.account_id or 0) > 0 else None
                if account is not None and _has_phone_binding(account):
                    self.state_store.update_item(int(item.id or 0), phone_stage="success")
                    return True
            reason = "OAIPay 上传要求手机号绑定成功"
            self.state_store.update_item(int(item.id or 0), oaipay_stage="skipped", overall_status="manual_required", oaipay_message=reason, last_error=reason)
            return False
        return True

    def _finalize_if_complete(self, task: IdeaOaiPayPipelineTask, config: IdeaOaiPayPipelineConfig) -> None:
        task = self.state_store.get_task(int(task.id or 0)) or task
        if task.active_register_task_id or task.active_idea_task_id or task.active_phone_task_id:
            return
        items = self.state_store.list_items(int(task.id or 0), limit=5000)
        if not items and task.source_type == "register":
            return
        if not items and task.source_type == "local":
            task.status = "failed"
            task.stopped_at = _utcnow()
            task.last_error = "本地账号来源未匹配到任何 ChatGPT 账号"
            self.state_store.save_task(task)
            self.status = task.status
            self._log(task.id, "流水线结束：本地账号来源为空")
            return
        done_count = len([item for item in items if item.overall_status == "done"])
        terminal = {"done", "failed", "manual_required", "skipped"}
        all_terminal = bool(items) and all(item.overall_status in terminal for item in items)
        target = max(0, int(config.source.target_count or task.target_success_count or 0))
        if task.source_type == "register" and target > 0 and done_count < target:
            return
        if task.source_type == "local" and not all_terminal:
            return
        if task.source_type == "register" and not all_terminal and done_count < target:
            return
        task.status = "done" if done_count > 0 else "failed"
        task.stopped_at = _utcnow()
        self.state_store.save_task(task)
        self.status = task.status
        self._log(task.id, f"流水线结束: status={task.status} done={done_count} total={len(items)}")

    def _optional_bool(self, value: Any) -> bool | None:
        text = _lower(value)
        if not text:
            return None
        if text in {"1", "true", "yes", "on", "used"}:
            return True
        if text in {"0", "false", "no", "off", "unused"}:
            return False
        return None

    def _task_to_dict(self, task: IdeaOaiPayPipelineTask) -> dict[str, Any]:
        summary = _safe_json_obj(task.summary_json)
        return {
            "id": task.id,
            "task_key": task.task_key,
            "status": task.status,
            "source_type": task.source_type,
            "target_success_count": task.target_success_count,
            "active_register_task_id": task.active_register_task_id,
            "active_idea_task_id": task.active_idea_task_id,
            "active_phone_task_id": task.active_phone_task_id,
            "last_error": task.last_error,
            "summary": summary,
            "started_at": task.started_at.isoformat() if task.started_at else "",
            "stopped_at": task.stopped_at.isoformat() if task.stopped_at else "",
            "created_at": task.created_at.isoformat() if task.created_at else "",
            "updated_at": task.updated_at.isoformat() if task.updated_at else "",
        }

    def _item_to_dict(self, item: IdeaOaiPayPipelineItem) -> dict[str, Any]:
        return {
            "id": item.id,
            "pipeline_task_id": item.pipeline_task_id,
            "account_id": item.account_id,
            "email": item.email,
            "source_stage": item.source_stage,
            "register_stage": item.register_stage,
            "idea_stage": item.idea_stage,
            "check_stage": item.check_stage,
            "gate_stage": item.gate_stage,
            "phone_stage": item.phone_stage,
            "oaipay_stage": item.oaipay_stage,
            "overall_status": item.overall_status,
            "subscription_type_before": item.subscription_type_before,
            "subscription_type_after": item.subscription_type_after,
            "account_validity": item.account_validity,
            "cdk_id": item.cdk_id,
            "cdk_masked": item.cdk_masked,
            "idea_task_id": item.idea_task_id,
            "idea_order_id": item.idea_order_id,
            "idea_display_id": item.idea_display_id,
            "idea_error": item.idea_error,
            "phone_task_id": item.phone_task_id,
            "phone_policy": item.phone_policy,
            "phone_error": item.phone_error,
            "oaipay_remote_state": item.oaipay_remote_state,
            "oaipay_remote_account_id": item.oaipay_remote_account_id,
            "oaipay_message": item.oaipay_message,
            "last_error": item.last_error,
            "created_at": item.created_at.isoformat() if item.created_at else "",
            "updated_at": item.updated_at.isoformat() if item.updated_at else "",
        }

    def _log(self, task_id: int | None, message: str) -> None:
        if not task_id:
            return
        self.state_store.append_task_log(int(task_id), str(message or ""))


idea_oaipay_pipeline_engine = IdeaOaiPayPipelineEngine()
