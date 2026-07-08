from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from api.actions import _execute_platform_action
from api.chatgpt import (
    GoPayBatchItemReq,
    GoPayBatchPhoneReq,
    GoPayBatchStartReq,
    cancel_gopay_batch_payment,
    get_active_gopay_batch_payment,
    get_gopay_batch_payment,
    start_gopay_batch_payment,
)
from api.integrations import _adapter_state
from sqlmodel import Session

from core.db import AccountModel, engine
from core.base_platform import RegisterConfig
from services.chatgpt_core import ChatGPTPlatform
from services.chatgpt_core.status_probe import probe_local_chatgpt_status
from services.chatgpt_account_state import mark_payment_failed, mark_payment_pending, mark_payment_succeeded
from services.chatgpt_sync import update_account_model_local_probe

from .config import PipelineConfigStore
from .logs import PipelineLogBus
from .models import PipelineTask
from .state import PipelineStateStore


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def get(platform: str):
    """Compatibility hook for platform lookup used by older pipeline tests/extensions."""
    if str(platform or "").strip().lower() == "chatgpt":
        return ChatGPTPlatform
    raise KeyError(f"unsupported platform: {platform}")


class PaymentBatchScheduler:
    """Payment batch scheduler backed by the existing GoPay batch flow."""

    def __init__(
        self,
        *,
        state_store: PipelineStateStore,
        config_store: PipelineConfigStore,
        log_bus: PipelineLogBus,
    ) -> None:
        self.state_store = state_store
        self.config_store = config_store
        self.log_bus = log_bus

    def tick(self, pipeline_task: PipelineTask) -> dict[str, Any]:
        active_batch_id = str(getattr(pipeline_task, "active_payment_batch_id", "") or "").strip()
        if active_batch_id:
            snapshot = self.poll_active_batch(pipeline_task)
            return {"action": "poll", "batch_id": active_batch_id, "snapshot": snapshot}

        active = get_active_gopay_batch_payment()
        active_task = active.get("task") if isinstance(active, dict) else None
        if isinstance(active_task, dict) and str(active_task.get("task_id") or "").strip():
            batch_id = str(active_task.get("task_id") or "").strip()
            pipeline_task.active_payment_batch_id = batch_id
            pipeline_task.updated_at = _utcnow()
            self.state_store.save_task(pipeline_task)
            self.log_bus.publish(f"检测到活跃 GoPay batch，接管状态: batch_id={batch_id}")
            return {"action": "adopt", "batch_id": batch_id, "snapshot": active_task}

        available_phones = self._available_phone_candidates()
        if not available_phones:
            return {"action": "noop", "reason": "no_available_phone"}

        pending_items = self.state_store.list_pending_payment_items(int(pipeline_task.id or 0))
        if not pending_items:
            return {"action": "noop", "reason": "no_pending_payment_items"}

        batch_size = self._compute_batch_size(len(pending_items), len(available_phones))
        if batch_size <= 0:
            return {"action": "noop", "reason": "batch_size_zero"}

        reserved = self.state_store.reserve_pending_payment_items(int(pipeline_task.id or 0), limit=batch_size)
        if not reserved:
            return {"action": "noop", "reason": "reserve_failed"}

        linked = self._prepare_checkout_links(reserved)
        if linked["ready_items"]:
            try:
                batch = self.start_batch(
                    pipeline_task,
                    linked["ready_items"],
                    available_phones[: len(linked["ready_items"])],
                )
            except Exception as exc:
                error_text = self._exception_text(exc)
                self._mark_batch_start_failure(linked["ready_items"], error_text)
                self.log_bus.publish(f"启动 GoPay 批量支付失败: error={error_text}")
                return {
                    "action": "failed",
                    "reason": "batch_start_failed",
                    "error": error_text,
                    "link_result": linked,
                }
            return {"action": "start", "batch": batch, "link_result": linked}
        return {"action": "noop", "reason": "no_checkout_ready_items", "link_result": linked}

    def _available_phone_candidates(self) -> list[dict[str, Any]]:
        state = _adapter_state()
        phones = state.get("phone_pool") if isinstance(state, dict) else []
        result: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        for phone in phones or []:
            if not isinstance(phone, dict):
                continue
            if phone.get("enabled") is False:
                continue
            if str(phone.get("status") or "").strip().lower() != "ready":
                continue
            phone_key = self._phone_candidate_key(phone)
            if phone_key and phone_key in seen_keys:
                continue
            if phone_key:
                seen_keys.add(phone_key)
            result.append(phone)
        return result

    def _phone_candidate_key(self, phone: dict[str, Any]) -> str:
        country_code = "".join(ch for ch in str(phone.get("phone_country_code") or "") if ch.isdigit())
        phone_number = "".join(ch for ch in str(phone.get("phone_number") or "") if ch.isdigit())
        if country_code or phone_number:
            return f"{country_code}:{phone_number}"
        return str(phone.get("uid") or phone.get("id") or "").strip()

    def _compute_batch_size(self, pending_count: int, available_phone_count: int) -> int:
        config = self.config_store.load()
        max_size = int(config.payment_batch_max_size or 0)
        candidate = min(int(pending_count or 0), int(available_phone_count or 0))
        if max_size > 0:
            candidate = min(candidate, max_size)
        return max(0, candidate)

    def _prepare_checkout_links(self, reserved_items: list[Any]) -> dict[str, Any]:
        ready_items: list[Any] = []
        failed_ids: list[int] = []
        PlatformCls = get("chatgpt")
        instance = PlatformCls(config=RegisterConfig(extra={}))
        config = self.config_store.load()

        with Session(engine) as session:
            for item in reserved_items:
                account_id = int(item.account_id or 0)
                item_id = int(item.id or 0)
                if account_id <= 0 or item_id <= 0:
                    failed_ids.append(item_id)
                    continue
                account = session.get(AccountModel, account_id)
                if account is None or account.platform != "chatgpt":
                    self.state_store.update_account_item(
                        item_id,
                        pipeline_status="failed",
                        payment_stage="failed",
                        payment_failed_stage="payment_link",
                        payment_error_code="account_missing",
                        payment_error_reason="账号不存在",
                        payment_error_detail="账号不存在",
                    )
                    failed_ids.append(item_id)
                    continue

                self.state_store.update_account_item(
                    item_id,
                    pipeline_status="link_generating",
                    payment_stage="link_generating",
                )
                result = _execute_platform_action(
                    instance,
                    "chatgpt",
                    account,
                    "payment_link",
                    {
                        "plan": str(config.gopay_plan or "plus"),
                        "country": str(config.gopay_country or "ID"),
                        "currency": str(config.gopay_currency or "IDR"),
                    },
                    session,
                )
                ok = bool(result.get("ok"))
                data = result.get("data") if isinstance(result.get("data"), dict) else {}
                checkout_url = str(data.get("url") or "").strip()
                if not ok or not checkout_url:
                    self._persist_account_primary_status(account_id, "payment_failed", session=session, commit=False)
                    self.state_store.update_account_item(
                        item_id,
                        pipeline_status="failed",
                        payment_stage="failed",
                        account_primary_status="payment_failed",
                        payment_failed_stage="payment_link",
                        payment_error_code="checkout_invalid",
                        payment_error_reason=str(result.get("error") or "订阅链接生成失败"),
                        payment_error_detail=str(result.get("error") or result),
                    )
                    failed_ids.append(item_id)
                    continue

                updated = self.state_store.update_account_item(
                    item_id,
                    pipeline_status="link_ready",
                    payment_stage="link_ready",
                    checkout_url=checkout_url,
                )
                if updated is not None:
                    ready_items.append(updated)

            session.commit()

        if ready_items:
            self.log_bus.publish(f"订阅链接生成完成: success={len(ready_items)} failed={len(failed_ids)}")
        elif failed_ids:
            self.log_bus.publish(f"订阅链接生成失败: failed={len(failed_ids)}")
        return {"ready_items": ready_items, "failed_item_ids": failed_ids}

    def start_batch(
        self,
        pipeline_task: PipelineTask,
        reserved_items: list[Any],
        phones: list[dict[str, Any]],
    ) -> dict[str, Any]:
        config = self.config_store.load()
        req_items: list[GoPayBatchItemReq] = []
        for index, (item, phone) in enumerate(zip(reserved_items, phones), start=1):
            req_items.append(
                GoPayBatchItemReq(
                    account_id=int(item.account_id or 0),
                    batch_index=index,
                    round=1,
                    phone=GoPayBatchPhoneReq(
                        id=str(phone.get("uid") or phone.get("id") or "").strip(),
                        label=str(phone.get("label") or "").strip(),
                        phone_country_code=str(phone.get("phone_country_code") or "").strip(),
                        phone_number=str(phone.get("phone_number") or "").strip(),
                    ),
                )
            )

        req = GoPayBatchStartReq(
            items=req_items,
            round_interval_seconds=0,
            defaults={
                "country": str(config.gopay_country or "ID"),
                "currency": str(config.gopay_currency or "IDR"),
            },
        )
        with Session(engine) as session:
            batch = start_gopay_batch_payment(req, session=session)

        batch_id = str((batch or {}).get("task_id") or "").strip()
        if not batch_id:
            raise RuntimeError("GoPay 批量支付启动失败: 缺少 task_id")
        now = _utcnow()
        for item in reserved_items:
            self.state_store.update_account_item(
                int(item.id or 0),
                pipeline_status="paying",
                payment_stage="paying",
                payment_batch_task_id=batch_id,
                payment_started_at=now,
            )

        pipeline_task.active_payment_batch_id = batch_id
        pipeline_task.updated_at = now
        self.state_store.save_task(pipeline_task)
        self.log_bus.publish(f"启动 GoPay 批量支付: batch_id={batch_id} items={len(req_items)}")
        return batch

    def poll_active_batch(self, pipeline_task: PipelineTask) -> dict[str, Any] | None:
        batch_id = str(getattr(pipeline_task, "active_payment_batch_id", "") or "").strip()
        if not batch_id:
            return None
        snapshot = get_gopay_batch_payment(batch_id)
        snapshot = self._cancel_batch_if_timed_out(pipeline_task, snapshot)
        self._sync_batch_result_to_items(int(pipeline_task.id or 0), snapshot)
        status = str((snapshot or {}).get("status") or "").strip().lower()
        if status in {"done", "failed", "cancelled"}:
            pipeline_task.active_payment_batch_id = ""
            pipeline_task.updated_at = _utcnow()
            self.state_store.save_task(pipeline_task)
            self.log_bus.publish(f"GoPay 批量支付结束: batch_id={batch_id} status={status}")
        return snapshot

    def _sync_batch_result_to_items(self, pipeline_task_id: int, snapshot: dict[str, Any] | None) -> None:
        if not isinstance(snapshot, dict):
            return
        items = snapshot.get("items") if isinstance(snapshot.get("items"), list) else []
        batch_id = str(snapshot.get("task_id") or "").strip()
        by_account_id = {
            int(item.account_id or 0): item
            for item in self.state_store.list_account_items_by_batch(pipeline_task_id, batch_id)
            if int(item.account_id or 0) > 0
        }
        now = _utcnow()
        for item_snapshot in items:
            if not isinstance(item_snapshot, dict):
                continue
            account_id = int(item_snapshot.get("account_id") or 0)
            record = by_account_id.get(account_id)
            if record is None:
                continue
            item_status = str(item_snapshot.get("status") or "").strip().lower()
            phase = str((item_snapshot.get("snapshot") or {}).get("phase") or "").strip().lower()
            error_text = str(item_snapshot.get("error") or "").strip()
            success_summary = str(item_snapshot.get("phone_deferred_reason") or "").strip()
            patch: dict[str, Any] = {
                "payment_batch_task_id": batch_id,
            }
            if item_status == "done" or phase == "succeeded":
                self._persist_account_primary_status(account_id, "subscribed")
                subscription_patch = self._refresh_subscription_state(account_id)
                patch.update(
                    {
                        "pipeline_status": "paid",
                        "payment_stage": "success",
                        "account_primary_status": "subscribed",
                        "payment_completed_at": now,
                        "success_summary": success_summary or "支付成功",
                        **subscription_patch,
                    }
                )
            elif item_status == "failed" or phase == "failed":
                self._persist_account_primary_status(account_id, "payment_failed")
                patch.update(
                    {
                        "pipeline_status": "failed",
                        "payment_stage": "failed",
                        "account_primary_status": "payment_failed",
                        "payment_completed_at": now,
                        "payment_error_code": self._payment_error_code(error_text),
                        "payment_error_reason": error_text or "支付失败",
                        "payment_error_detail": error_text or "支付失败",
                        "success_summary": "",
                        "subscription_refresh_status": "",
                    }
                )
            elif item_status == "cancelled" or phase == "cancelled":
                self._persist_account_primary_status(account_id, "payment_failed")
                patch.update(
                    {
                        "pipeline_status": "failed",
                        "payment_stage": "failed",
                        "account_primary_status": "payment_failed",
                        "payment_completed_at": now,
                        "payment_error_code": "payment_cancelled",
                        "payment_error_reason": "支付已取消",
                        "payment_error_detail": "支付已取消",
                        "success_summary": "",
                        "subscription_refresh_status": "",
                    }
                )
            self.state_store.update_account_item(int(record.id or 0), **patch)

    def _cancel_batch_if_timed_out(self, pipeline_task: PipelineTask, snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(snapshot, dict):
            return snapshot
        batch_id = str(snapshot.get("task_id") or "").strip()
        if not batch_id:
            return snapshot

        config = self.config_store.load()
        timeout_seconds = max(0, int(config.gopay_timeout_seconds or 0))
        if timeout_seconds <= 0:
            return snapshot

        batch_items = self.state_store.list_account_items_by_batch(int(pipeline_task.id or 0), batch_id)
        started_at_candidates = [item.payment_started_at for item in batch_items if item.payment_started_at is not None]
        if not started_at_candidates:
            return snapshot
        started_at = min(started_at_candidates)
        elapsed = (_utcnow() - started_at).total_seconds()
        status = str(snapshot.get("status") or "").strip().lower()
        if elapsed <= timeout_seconds or status in {"done", "failed", "cancelled"}:
            return snapshot

        self.log_bus.publish(f"GoPay 批量支付超时，尝试取消: batch_id={batch_id} elapsed={int(elapsed)}s")
        try:
            cancelled = cancel_gopay_batch_payment(batch_id)
            return cancelled
        except Exception as exc:
            self.log_bus.publish(f"取消超时 GoPay 批量支付失败: batch_id={batch_id} error={exc}")
            return snapshot

    def _payment_error_code(self, error_text: str) -> str:
        text = str(error_text or "").strip().lower()
        if "no active subscription plans found" in text or "eligible" in text:
            return "not_eligible"
        if "proxy" in text:
            return "proxy_error"
        if "timeout" in text:
            return "otp_timeout"
        if "session" in text or "会话" in text:
            return "session_missing"
        if "cancel" in text or "取消" in text:
            return "payment_cancelled"
        if "declined" in text:
            return "payment_declined"
        return "payment_failed"

    def _refresh_subscription_state(self, account_id: int) -> dict[str, Any]:
        now = _utcnow()
        with Session(engine) as session:
            account = session.get(AccountModel, int(account_id or 0))
            if account is None:
                return {
                    "subscription_plan_confirmed": "unknown",
                    "subscription_refresh_status": "failed",
                    "subscription_refreshed_at": now.isoformat(),
                }
            codex_acc = self._build_probe_account(account)
            try:
                probe = probe_local_chatgpt_status(codex_acc, proxy=None)
                update_account_model_local_probe(account, probe, session=session, commit=True)
                self._persist_account_primary_status(account_id, "subscribed", session=session, commit=True)
                plan = str(
                    ((probe or {}).get("subscription") or {}).get("plan") or "unknown"
                ).strip().lower() or "unknown"
                return {
                    "subscription_plan_confirmed": plan,
                    "subscription_refresh_status": "success",
                    "subscription_refreshed_at": now.isoformat(),
                }
            except Exception as exc:
                self._persist_account_primary_status(account_id, "subscribed", session=session, commit=True)
                self.log_bus.publish(f"订阅类型刷新失败: account_id={account_id} error={exc}")
                return {
                    "subscription_plan_confirmed": "unknown",
                    "subscription_refresh_status": "failed",
                    "subscription_refreshed_at": now.isoformat(),
                }

    def _mark_batch_start_failure(self, items: list[Any], error_text: str) -> None:
        now = _utcnow()
        error_code = self._payment_error_code(error_text)
        for item in items:
            account_id = int(getattr(item, "account_id", 0) or 0)
            if account_id > 0:
                self._persist_account_primary_status(account_id, "payment_failed")
            self.state_store.update_account_item(
                int(item.id or 0),
                pipeline_status="failed",
                payment_stage="failed",
                account_primary_status="payment_failed",
                payment_failed_stage="gopay_start",
                payment_error_code=error_code,
                payment_error_reason=error_text or "启动 GoPay 批量支付失败",
                payment_error_detail=error_text or "启动 GoPay 批量支付失败",
                payment_completed_at=now,
                success_summary="",
            )

    def _persist_account_primary_status(
        self,
        account_id: int,
        status: str,
        *,
        session: Session | None = None,
        commit: bool = True,
    ) -> None:
        account_id_value = int(account_id or 0)
        normalized_status = str(status or "").strip()
        if account_id_value <= 0 or not normalized_status:
            return
        if session is not None:
            account = session.get(AccountModel, account_id_value)
            if account is None:
                return
            previous_status = str(account.status or "")
            if normalized_status == "subscribed":
                mark_payment_succeeded(account, reason="pipeline_payment_succeeded")
            elif normalized_status == "payment_failed":
                mark_payment_failed(account, reason="pipeline_payment_failed")
            elif normalized_status == "pending_payment":
                mark_payment_pending(account, reason="pipeline_payment_pending")
            else:
                account.status = normalized_status
            if str(account.status or "") != previous_status:
                account.updated_at = _utcnow()
                session.add(account)
            if commit:
                session.commit()
                session.refresh(account)
            return

        with Session(engine) as owned_session:
            self._persist_account_primary_status(
                account_id_value,
                normalized_status,
                session=owned_session,
                commit=commit,
            )

    def _exception_text(self, exc: Exception) -> str:
        detail = getattr(exc, "detail", exc)
        return str(detail or exc or "支付失败").strip()

    def _build_probe_account(self, account: AccountModel):
        from core.base_platform import Account, AccountStatus

        extra = account.get_extra()
        return Account(
            platform=account.platform,
            email=account.email,
            password=account.password,
            user_id=account.user_id,
            token=account.token,
            status=AccountStatus(str(account.status or "registered")),
            extra=extra,
        )
