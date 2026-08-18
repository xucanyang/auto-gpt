"""Durable reconciliation for registration PayPal auto-payment handoffs.

The registration task only owns link extraction and queue submission.  This
module owns everything that may outlive that task: remote item polling, the
post-payment Web Session refresh, and a redacted append-only event timeline.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func
from sqlmodel import Session, select

from core import db as core_db
from core.db import (
    AccountModel,
    ChatGPTSubscriptionStateModel,
    RegistrationPaypalPaymentEventModel,
    RegistrationPaypalPaymentFollowupModel,
)
from services.chatgpt_core.paypal_agreement_auto_client import (
    PaypalAgreementAutoClient,
    sanitize_paypal_agreement_error,
)
from services.chatgpt_core.task_logging import mask_email_for_log, sanitize_error_message


logger = logging.getLogger(__name__)

PAYMENT_PENDING = "payment_pending"
RELOGIN_PENDING = "relogin_pending"
LOCAL_REFRESH_PENDING = "local_refresh_pending"
TERMINAL_STATES = {
    "payment_failed",
    "payment_unknown",
    "relogin_failed",
    "subscription_confirmed",
    "local_unconfirmed",
    "account_identity_changed",
}
ACTIVE_STATES = {PAYMENT_PENDING, RELOGIN_PENDING, LOCAL_REFRESH_PENDING}


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


POLL_INTERVAL_SECONDS = _env_float(
    "REGISTRATION_PAYPAL_FOLLOWUP_POLL_INTERVAL_SECONDS", 8.0, 2.0, 120.0
)
POLL_MAX_SECONDS = _env_float(
    "REGISTRATION_PAYPAL_FOLLOWUP_MAX_SECONDS", 24 * 60 * 60, 300.0, 7 * 24 * 60 * 60
)
MAX_RELOGIN_ATTEMPTS = 3
LOCAL_REFRESH_GRACE_SECONDS = _env_float(
    "REGISTRATION_PAYPAL_FOLLOWUP_LOCAL_REFRESH_GRACE_SECONDS", 180.0, 15.0, 1800.0
)

_STOP_EVENT = threading.Event()
_WORKER_LOCK = threading.RLock()
_WORKER_THREAD: threading.Thread | None = None


def _now_ts() -> float:
    return time.time()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _account_created_at_text(value: Any) -> str:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None).isoformat(sep=" ")
    return str(value or "").strip()


def _safe_text(value: Any, limit: int = 500) -> str:
    return sanitize_paypal_agreement_error(value)[:limit]


def _safe_metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key, item in value.items():
        normalized_key = str(key or "").strip()[:64]
        if not normalized_key:
            continue
        if isinstance(item, (str, int, float, bool)) or item is None:
            result[normalized_key] = _safe_text(item, 240) if isinstance(item, str) else item
    return result


def _event_key(
    *,
    task_id: str,
    account_id: int,
    stage: str,
    message: str,
    metadata: dict[str, Any],
    idempotency_key: str = "",
) -> str:
    explicit = str(idempotency_key or "").strip()
    if explicit:
        return explicit[:320]
    digest = hashlib.sha256()
    digest.update(f"{task_id}|{account_id}|{stage}|{message}|".encode("utf-8"))
    digest.update(repr(sorted(metadata.items())).encode("utf-8"))
    return f"paypal:{digest.hexdigest()}"


def _emit_live_event(task_id: str, message: str, level: str) -> None:
    """Mirror durable events into the live task window when it still exists."""
    if not task_id:
        return
    try:
        from api.tasks import _log, _task_store

        if _task_store.exists(task_id):
            _log(task_id, message, level)
    except Exception:
        # A payment followup is intentionally independent from the task
        # process; a missing/cleaned task must never stop reconciliation.
        return


def append_registration_paypal_event(
    *,
    task_id: str,
    account_id: int = 0,
    account_email: str = "",
    account_created_at: str = "",
    stage: str,
    message: str,
    level: str = "info",
    metadata: dict[str, Any] | None = None,
    idempotency_key: str = "",
) -> bool:
    """Append one redacted event, ignoring duplicate idempotency keys."""
    safe_stage = str(stage or "payment").strip().lower()[:64] or "payment"
    safe_level = str(level or "info").strip().lower()[:16] or "info"
    safe_message = _safe_text(message, 1000)
    safe_meta = _safe_metadata(metadata)
    key = _event_key(
        task_id=str(task_id or "")[:160],
        account_id=int(account_id or 0),
        stage=safe_stage,
        message=safe_message,
        metadata=safe_meta,
        idempotency_key=idempotency_key,
    )
    try:
        with Session(core_db.engine) as session:
            existing = session.exec(
                select(RegistrationPaypalPaymentEventModel).where(
                    RegistrationPaypalPaymentEventModel.idempotency_key == key
                )
            ).first()
            if existing is not None:
                return False
            event = RegistrationPaypalPaymentEventModel(
                task_id=str(task_id or "")[:160],
                account_id=max(int(account_id or 0), 0),
                account_email_masked=mask_email_for_log(account_email)[:160],
                account_created_at=str(account_created_at or "")[:64],
                stage=safe_stage,
                level=safe_level,
                message=safe_message,
                idempotency_key=key,
                created_at=_utcnow(),
            )
            event.set_metadata(safe_meta)
            session.add(event)
            session.commit()
    except Exception:
        logger.warning("PayPal followup event persistence failed", exc_info=True)
        return False
    _emit_live_event(str(task_id or ""), safe_message, safe_level)
    return True


def _find_followup(
    session: Session,
    *,
    account_id: int,
    account_created_at: str,
    batch_id: str,
    item_id: str,
) -> RegistrationPaypalPaymentFollowupModel | None:
    return session.exec(
        select(RegistrationPaypalPaymentFollowupModel).where(
            RegistrationPaypalPaymentFollowupModel.account_id == int(account_id),
            RegistrationPaypalPaymentFollowupModel.account_created_at == str(account_created_at or ""),
            RegistrationPaypalPaymentFollowupModel.batch_id == str(batch_id or ""),
            RegistrationPaypalPaymentFollowupModel.item_id == str(item_id or ""),
        )
    ).first()


def ensure_payment_followup(
    *,
    task_id: str,
    account_id: int,
    account_email: str,
    account_created_at: str,
    batch_id: str,
    item_id: str,
    remote_status: str = "pending",
    idempotent: bool = False,
) -> RegistrationPaypalPaymentFollowupModel | None:
    """Create the durable payment-pending row exactly once."""
    if int(account_id or 0) <= 0 or not batch_id or not item_id:
        return None
    now = _now_ts()
    created = False
    try:
        with Session(core_db.engine) as session:
            row = _find_followup(
                session,
                account_id=int(account_id),
                account_created_at=str(account_created_at or ""),
                batch_id=batch_id,
                item_id=item_id,
            )
            if row is None:
                row = RegistrationPaypalPaymentFollowupModel(
                    task_id=str(task_id or "")[:160],
                    account_id=int(account_id),
                    account_email=str(account_email or "")[:320],
                    account_created_at=str(account_created_at or "")[:64],
                    batch_id=str(batch_id)[:128],
                    item_id=str(item_id)[:128],
                    state=PAYMENT_PENDING,
                    remote_status=str(remote_status or "pending")[:64],
                    next_poll_at=now,
                    deadline_at=now + POLL_MAX_SECONDS,
                    created_at=_utcnow(),
                    updated_at=_utcnow(),
                )
                session.add(row)
                created = True
            else:
                # A previously reconciled row is immutable; an idempotent
                # enqueue response must not reset a completed payment.
                if row.state in TERMINAL_STATES:
                    return row
                row.task_id = str(task_id or row.task_id)[:160]
                row.account_email = str(account_email or row.account_email)[:320]
                row.remote_status = str(remote_status or row.remote_status)[:64]
                row.next_poll_at = min(float(row.next_poll_at or now), now)
                row.updated_at = _utcnow()
                session.add(row)
            session.commit()
            session.refresh(row)
            result = row
    except Exception:
        logger.warning("PayPal followup row persistence failed", exc_info=True)
        return None

    append_registration_paypal_event(
        task_id=task_id,
        account_id=account_id,
        account_email=account_email,
        account_created_at=account_created_at,
        stage="queued",
        message=(
            "PayPal 支付条目已进入持久化跟进队列"
            if created
            else "PayPal 支付条目已恢复跟进（幂等，不重复入队）"
        ),
        metadata={"batch_id": batch_id, "item_id": item_id, "remote_status": remote_status},
        idempotency_key=f"paypal:{account_id}:{account_created_at}:{batch_id}:{item_id}:queued",
    )
    return result


def _update_marker_for_followup(
    account_id: int,
    *,
    email: str,
    created_at: str,
    row: RegistrationPaypalPaymentFollowupModel,
) -> None:
    """Keep the legacy account marker useful while the new table is primary."""
    try:
        with Session(core_db.engine) as session:
            account = session.get(AccountModel, int(account_id))
            if account is None:
                return
            if (
                str(account.email or "").strip().lower() != str(email or "").strip().lower()
                or _account_created_at_text(account.created_at) != str(created_at or "")
            ):
                return
            extra = account.get_extra()
            marker = extra.get("chatgpt_paypal_auto_payment")
            if not isinstance(marker, dict):
                marker = {}
            marker.update(
                {
                    "status": row.state,
                    "remote_status": row.remote_status,
                    "remote_stage": row.remote_stage,
                    "payment_result": row.payment_result,
                    "payment_result_code": row.payment_result_code,
                    "job_id": row.remote_job_id,
                    "settlement_status": row.settlement_status,
                    "paypal_authorized": bool(row.paypal_authorized),
                    "merchant_redirect_succeeded": row.merchant_redirect_succeeded,
                    "entitlement_verified": row.entitlement_verified,
                    "last_error": _safe_text(row.last_error),
                    "updated_at": _utcnow().isoformat(),
                }
            )
            extra["chatgpt_paypal_auto_payment"] = marker
            extra["chatgpt_paypal_payment_followup"] = {
                "state": row.state,
                "batch_id": row.batch_id,
                "item_id": row.item_id,
                "updated_at": marker["updated_at"],
            }
            account.set_extra(extra)
            account.updated_at = _utcnow()
            session.add(account)
            session.commit()
    except Exception:
        logger.warning("PayPal followup marker update failed account_id=%s", account_id, exc_info=True)


def _persist_row_update(row_id: int, **changes: Any) -> RegistrationPaypalPaymentFollowupModel | None:
    try:
        with Session(core_db.engine) as session:
            row = session.get(RegistrationPaypalPaymentFollowupModel, int(row_id))
            if row is None:
                return None
            for key, value in changes.items():
                if hasattr(row, key):
                    setattr(row, key, value)
            row.updated_at = _utcnow()
            session.add(row)
            session.commit()
            session.refresh(row)
            return row
    except Exception:
        logger.warning("PayPal followup update failed row_id=%s", row_id, exc_info=True)
        return None


def _backoff(attempt: int) -> float:
    return min(POLL_INTERVAL_SECONDS * (2 ** max(int(attempt) - 1, 0)), 15 * 60.0)


def _remote_is_authorized(result: dict[str, Any]) -> bool:
    return bool(
        result.get("paypal_authorized") is True
        or result.get("merchant_redirect_succeeded") is True
        or str(result.get("settlement_status") or "").strip().lower()
        in {"merchant_redirect_succeeded", "authorized", "confirmed", "pending_verification"}
    )


def _remote_failure_state(result: dict[str, Any]) -> str:
    status = str(result.get("status") or "").strip().lower()
    settlement = str(result.get("settlement_status") or "").strip().lower()
    if status in {"failed", "cancelled", "canceled"} or settlement in {
        "merchant_redirect_failed",
        "failed",
        "vault_failed",
    }:
        return "payment_failed"
    if status == "interrupted" or str(result.get("error_code") or "").upper() in {
        "SERVICE_RESTART_REVIEW_REQUIRED",
        "BATCH_INTERNAL_ERROR",
    }:
        return "payment_unknown"
    return ""


def _local_subscription_confirmed(account_id: int) -> bool:
    try:
        with Session(core_db.engine) as session:
            subscription = session.get(ChatGPTSubscriptionStateModel, int(account_id))
            if subscription is not None:
                state = str(subscription.current_state or "").strip().lower()
                plan = str(subscription.current_plan or subscription.last_confirmed_plan or "").strip().lower()
                if state in {"active", "confirmed", "subscribed"} and plan not in {"", "unknown", "free"}:
                    return True
            account = session.get(AccountModel, int(account_id))
            if account is None:
                return False
            if str(account.status or "").strip().lower() == "subscribed":
                return True
            extra = account.get_extra()
            local = extra.get("chatgpt_local") if isinstance(extra.get("chatgpt_local"), dict) else {}
            plan = str(
                local.get("plan")
                or local.get("subscription_plan")
                or local.get("plan_type")
                or ""
            ).strip().lower()
            return bool(plan and plan not in {"free", "unknown", "none", "null"})
    except Exception:
        return False


def _revive_invalid_status_after_successful_login(account_id: int) -> None:
    """A successful payment followup login is positive liveness evidence."""
    try:
        from services.account_filters import upsert_account_list_state_for_account_ids

        with Session(core_db.engine) as session:
            account = session.get(AccountModel, int(account_id))
            if account is None or str(account.status or "").strip().lower() != "invalid":
                return
            extra = account.get_extra()
            marker = extra.get("chatgpt_paypal_auto_payment") if isinstance(extra.get("chatgpt_paypal_auto_payment"), dict) else {}
            followup_state = str(marker.get("state") or marker.get("status") or "").strip().lower()
            account.status = "pending_payment" if followup_state in {
                "payment_authorized",
                "relogin_pending",
                "local_refresh_pending",
                "subscription_confirmed",
            } else "registered"
            account.updated_at = _utcnow()
            session.add(account)
            upsert_account_list_state_for_account_ids(session, [account.id], commit=False)
            session.commit()
    except Exception:
        logger.warning("PayPal followup status revival failed account_id=%s", account_id, exc_info=True)


def _account_matches(row: RegistrationPaypalPaymentFollowupModel, account: AccountModel) -> bool:
    return bool(
        account is not None
        and account.platform == "chatgpt"
        and int(account.id or 0) == int(row.account_id or 0)
        and str(account.email or "").strip().lower() == str(row.account_email or "").strip().lower()
        and _account_created_at_text(account.created_at) == str(row.account_created_at or "")
    )


def _followup_login_candidates() -> list[tuple[str, Any, str]]:
    from core.proxy_utils import resolve_task_proxy_candidates

    return resolve_task_proxy_candidates(
        {},
        fallback_proxy=None,
        default_mode="global",
        target="chatgpt",
    )


def _followup_login_proxy_error(result: dict[str, Any], error_text: str) -> bool:
    from core.proxy_utils import is_proxy_error_text

    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    error_code = str(data.get("error_code") or "").strip().lower()
    if error_code in {
        "account_identity_mismatch",
        "account_deactivated",
        "password_invalid",
        "login_blocked",
        "missing_email",
        "missing_mailbox_state",
        "invalid_account_id",
        "account_not_found",
        "otp_rate_limited",
    }:
        return False
    return error_code == "network_failed" or is_proxy_error_text(error_text)


def _process_relogin(row: RegistrationPaypalPaymentFollowupModel) -> None:
    from services.chatgpt_core.web_session_login import execute_chatgpt_web_session_login

    append_registration_paypal_event(
        task_id=row.task_id,
        account_id=row.account_id,
        account_email=row.account_email,
        account_created_at=row.account_created_at,
        stage="relogin_started",
        message="支付已确认，开始本地重新登录并刷新 ChatGPT Web Session",
        idempotency_key=f"paypal:{row.id}:relogin:{row.relogin_attempt_count + 1}:start",
        metadata={"batch_id": row.batch_id, "item_id": row.item_id},
    )

    def log_fn(message: str, level: str = "info") -> None:
        # The login core can emit many low-level browser lines.  Mirror those
        # into the live task window when available, while keeping the durable
        # event table reserved for stable state transitions.
        _emit_live_event(row.task_id, _safe_text(message, 1000), level)

    result: dict[str, Any] = {}
    try:
        candidates = _followup_login_candidates()
    except Exception as exc:
        candidates = []
        result = {
            "ok": False,
            "error": sanitize_error_message(exc),
            "data": {"error_code": "proxy_configuration_error"},
        }
    for candidate_index, (proxy_url, proxy_pool, proxy_source) in enumerate(candidates, start=1):
        _emit_live_event(
            row.task_id,
            f"[支付后登录][代理] 候选={candidate_index}/{len(candidates)}｜来源={_safe_text(proxy_source, 160) or 'direct'}",
            "info",
        )
        try:
            candidate_result = execute_chatgpt_web_session_login(
                int(row.account_id),
                task_id=f"paypal-followup-{row.id or row.item_id}",
                log_fn=log_fn,
                proxy_url=proxy_url or None,
                hold_browser=False,
            )
        except Exception as exc:
            candidate_result = {
                "ok": False,
                "error": sanitize_error_message(exc),
                "data": {"error_code": "worker_exception"},
            }
        result = candidate_result
        if bool(candidate_result.get("ok")):
            if proxy_pool is not None and proxy_url:
                try:
                    proxy_pool.report_success(proxy_url)
                except Exception:
                    pass
            break
        data = candidate_result.get("data") if isinstance(candidate_result.get("data"), dict) else {}
        error_text = str(
            candidate_result.get("error")
            or data.get("message")
            or "本地重新登录失败"
        )
        if not _followup_login_proxy_error(candidate_result, error_text):
            break
        if proxy_pool is not None and proxy_url:
            try:
                proxy_pool.report_fail(proxy_url)
            except Exception:
                pass
        if candidate_index < len(candidates):
            _emit_live_event(
                row.task_id,
                f"[支付后登录][代理] 当前候选失败，切换下一候选｜原因={_safe_text(error_text, 180)}",
                "warning",
            )
    if not result:
        result = {
            "ok": False,
            "error": "支付后登录没有可用代理候选",
            "data": {"error_code": "proxy_configuration_error"},
        }
    if bool(result.get("ok")):
        _revive_invalid_status_after_successful_login(row.account_id)
        updated = _persist_row_update(
            int(row.id or 0),
            state=LOCAL_REFRESH_PENDING,
            relogin_attempt_count=int(row.relogin_attempt_count or 0) + 1,
            local_refresh_generation=_utcnow().isoformat(),
            next_poll_at=_now_ts() + 8.0,
            last_error="",
        )
        if updated is not None:
            _update_marker_for_followup(
                updated.account_id,
                email=updated.account_email,
                created_at=updated.account_created_at,
                row=updated,
            )
            append_registration_paypal_event(
                task_id=updated.task_id,
                account_id=updated.account_id,
                account_email=updated.account_email,
                account_created_at=updated.account_created_at,
                stage="relogin_succeeded",
                message="本地重新登录成功，已写回 AT、Session、Cookie；本地状态刷新已调度",
                metadata={"batch_id": updated.batch_id, "item_id": updated.item_id},
                idempotency_key=f"paypal:{updated.id}:relogin:{updated.relogin_attempt_count}:success",
            )
        return

    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    error_code = str(data.get("error_code") or "relogin_failed").strip().lower()
    error_text = _safe_text(result.get("error") or data.get("message") or "本地重新登录失败")
    attempts = int(row.relogin_attempt_count or 0) + 1
    retryable = error_code in {
        "network_failed",
        "timeout",
        "rate_limited",
        "otp_rate_limited",
        "browser_lease_interrupted",
        "worker_exception",
        "proxy_configuration_error",
    }
    next_state = RELOGIN_PENDING if retryable and attempts < MAX_RELOGIN_ATTEMPTS else "relogin_failed"
    updated = _persist_row_update(
        int(row.id or 0),
        state=next_state,
        relogin_attempt_count=attempts,
        next_poll_at=_now_ts() + (_backoff(attempts) if next_state == RELOGIN_PENDING else 3600),
        last_error=error_text,
    )
    if updated is not None:
        _update_marker_for_followup(
            updated.account_id,
            email=updated.account_email,
            created_at=updated.account_created_at,
            row=updated,
        )
        append_registration_paypal_event(
            task_id=updated.task_id,
            account_id=updated.account_id,
            account_email=updated.account_email,
            account_created_at=updated.account_created_at,
            stage="relogin_failed",
            message=(
                f"本地重新登录失败，{_backoff(attempts):.0f}s 后重试：{error_text}"
                if next_state == RELOGIN_PENDING
                else f"本地重新登录失败，保留原账号状态：{error_text}"
            ),
            level="warning",
            metadata={"error_code": error_code, "batch_id": updated.batch_id, "item_id": updated.item_id},
            idempotency_key=f"paypal:{updated.id}:relogin:{attempts}:{next_state}",
        )


def _process_row(row: RegistrationPaypalPaymentFollowupModel) -> None:
    now = _now_ts()
    # The deadline bounds remote payment-result polling only.  Once durable
    # authorization evidence exists, a restart or a slow queue must not skip
    # the required local login and mislabel it as a completed local refresh.
    if row.state == PAYMENT_PENDING and row.deadline_at and now >= float(row.deadline_at):
        updated = _persist_row_update(
            int(row.id or 0),
            state="payment_unknown",
            last_error="支付跟进超过截止时间，需人工核验",
            next_poll_at=now + 24 * 60 * 60,
        )
        if updated:
            _update_marker_for_followup(
                updated.account_id,
                email=updated.account_email,
                created_at=updated.account_created_at,
                row=updated,
            )
            append_registration_paypal_event(
                task_id=updated.task_id,
                account_id=updated.account_id,
                account_email=updated.account_email,
                account_created_at=updated.account_created_at,
                stage="deadline",
                message="支付跟进超过截止时间，停止自动重试并标记为需人工核验",
                level="warning",
                metadata={"batch_id": updated.batch_id, "item_id": updated.item_id},
                idempotency_key=f"paypal:{updated.id}:deadline",
            )
        return

    if row.state == RELOGIN_PENDING:
        _process_relogin(row)
        return
    if row.state == LOCAL_REFRESH_PENDING:
        if _local_subscription_confirmed(row.account_id):
            updated = _persist_row_update(
                int(row.id or 0),
                state="subscription_confirmed",
                next_poll_at=now + 24 * 60 * 60,
                last_error="",
                entitlement_verified=True,
            )
            if updated:
                _update_marker_for_followup(
                    updated.account_id,
                    email=updated.account_email,
                    created_at=updated.account_created_at,
                    row=updated,
                )
                append_registration_paypal_event(
                    task_id=updated.task_id,
                    account_id=updated.account_id,
                    account_email=updated.account_email,
                    account_created_at=updated.account_created_at,
                    stage="subscription_confirmed",
                    message="本地状态刷新结果确认付费权益",
                    metadata={"batch_id": updated.batch_id, "item_id": updated.item_id},
                    idempotency_key=f"paypal:{updated.id}:subscription_confirmed",
                )
            return
        if row.local_refresh_generation:
            try:
                generation_at = datetime.fromisoformat(row.local_refresh_generation).timestamp()
            except (TypeError, ValueError, OverflowError):
                generation_at = now
            if now - generation_at >= LOCAL_REFRESH_GRACE_SECONDS:
                updated = _persist_row_update(
                    int(row.id or 0),
                    state="local_unconfirmed",
                    next_poll_at=now + 24 * 60 * 60,
                    last_error="支付已授权，但本地状态刷新未确认付费权益",
                )
                if updated:
                    _update_marker_for_followup(
                        updated.account_id,
                        email=updated.account_email,
                        created_at=updated.account_created_at,
                        row=updated,
                    )
                    append_registration_paypal_event(
                        task_id=updated.task_id,
                        account_id=updated.account_id,
                        account_email=updated.account_email,
                        account_created_at=updated.account_created_at,
                        stage="local_unconfirmed",
                        message="支付已授权，但本地状态刷新暂未确认付费权益；未伪造 subscribed 状态",
                        level="warning",
                        metadata={"batch_id": updated.batch_id, "item_id": updated.item_id},
                        idempotency_key=f"paypal:{updated.id}:local_unconfirmed",
                    )
                return
        _persist_row_update(int(row.id or 0), next_poll_at=now + _backoff(max(row.attempt_count, 1)))
        return

    if row.state != PAYMENT_PENDING:
        return

    try:
        result = PaypalAgreementAutoClient.from_env().get_item_result(row.batch_id, row.item_id)
    except Exception as exc:
        attempts = int(row.attempt_count or 0) + 1
        updated = _persist_row_update(
            int(row.id or 0),
            attempt_count=attempts,
            next_poll_at=now + _backoff(attempts),
            last_error=_safe_text(exc),
        )
        if updated:
            append_registration_paypal_event(
                task_id=updated.task_id,
                account_id=updated.account_id,
                account_email=updated.account_email,
                account_created_at=updated.account_created_at,
                stage="poll_error",
                message=f"支付结果查询失败，稍后重试：{updated.last_error}",
                level="warning",
                metadata={"batch_id": updated.batch_id, "item_id": updated.item_id},
                idempotency_key=f"paypal:{updated.id}:poll_error:{attempts}",
            )
        return

    attempts = int(row.attempt_count or 0) + 1
    failure_state = _remote_failure_state(result)
    authorized = _remote_is_authorized(result)
    next_state = failure_state or (RELOGIN_PENDING if authorized else PAYMENT_PENDING)
    updated = _persist_row_update(
        int(row.id or 0),
        state=next_state,
        remote_status=str(result.get("status") or "")[:64],
        remote_stage=_safe_text(result.get("stage") or ""),
        payment_result=_safe_text(result.get("payment_result") or ""),
        payment_result_code=_safe_text(
            result.get("payment_result_code") or result.get("error_code") or "",
            128,
        ),
        remote_job_id=_safe_text(result.get("job_id") or "", 128),
        settlement_status=str(result.get("settlement_status") or "")[:128],
        paypal_authorized=authorized or bool(result.get("paypal_authorized") is True),
        merchant_redirect_succeeded=result.get("merchant_redirect_succeeded"),
        entitlement_verified=result.get("entitlement_verified"),
        attempt_count=attempts,
        next_poll_at=(now + (8.0 if authorized else _backoff(attempts))),
        last_error=_safe_text(result.get("error") or ""),
    )
    if updated is None:
        return
    _update_marker_for_followup(
        updated.account_id,
        email=updated.account_email,
        created_at=updated.account_created_at,
        row=updated,
    )
    if failure_state:
        append_registration_paypal_event(
            task_id=updated.task_id,
            account_id=updated.account_id,
            account_email=updated.account_email,
            account_created_at=updated.account_created_at,
            stage=failure_state,
            message=(
                "支付结果失败，未执行重新登录"
                + (
                    f"：{updated.last_error or updated.payment_result or updated.remote_stage}"
                    if updated.last_error or updated.payment_result or updated.remote_stage
                    else ""
                )
                if failure_state == "payment_failed"
                else "支付条目被中断，结果不确定，需人工核验"
            ),
            level="warning",
            metadata={
                "batch_id": updated.batch_id,
                "item_id": updated.item_id,
                "job_id": updated.remote_job_id,
                "remote_status": updated.remote_status,
                "remote_stage": updated.remote_stage,
                "payment_result_code": updated.payment_result_code,
            },
            idempotency_key=f"paypal:{updated.id}:{failure_state}:{attempts}",
        )
    elif authorized:
        append_registration_paypal_event(
            task_id=updated.task_id,
            account_id=updated.account_id,
            account_email=updated.account_email,
            account_created_at=updated.account_created_at,
            stage="payment_authorized",
            message="支付结果已回读：PayPal 已授权/商户回跳成功",
            metadata={
                "batch_id": updated.batch_id,
                "item_id": updated.item_id,
                "job_id": updated.remote_job_id,
                "remote_status": updated.remote_status,
                "remote_stage": updated.remote_stage,
                "payment_result": updated.payment_result,
                "settlement_status": updated.settlement_status,
            },
            idempotency_key=f"paypal:{updated.id}:payment_authorized",
        )
        _process_relogin(updated)
    else:
        append_registration_paypal_event(
            task_id=updated.task_id,
            account_id=updated.account_id,
            account_email=updated.account_email,
            account_created_at=updated.account_created_at,
            stage="waiting_result",
            message=f"等待支付结果：{updated.remote_status or 'pending'}",
            metadata={"batch_id": updated.batch_id, "item_id": updated.item_id, "attempt": attempts},
            idempotency_key=f"paypal:{updated.id}:waiting:{attempts}",
        )


def reconcile_due_followups(*, limit: int = 20) -> int:
    now = _now_ts()
    try:
        with Session(core_db.engine) as session:
            rows = session.exec(
                select(RegistrationPaypalPaymentFollowupModel)
                .where(RegistrationPaypalPaymentFollowupModel.state.in_(list(ACTIVE_STATES)))
                .where(RegistrationPaypalPaymentFollowupModel.next_poll_at <= now)
                .order_by(RegistrationPaypalPaymentFollowupModel.next_poll_at.asc())
                .limit(max(1, min(int(limit or 20), 100)))
            ).all()
    except Exception:
        logger.warning("PayPal followup due-row query failed", exc_info=True)
        return 0
    for row in rows:
        try:
            with Session(core_db.engine) as session:
                account = session.get(AccountModel, int(row.account_id or 0))
            if account is None or not _account_matches(row, account):
                updated = _persist_row_update(
                    int(row.id or 0),
                    state="account_identity_changed",
                    next_poll_at=now + 24 * 60 * 60,
                    last_error="账号身份已变化，停止自动跟进",
                )
                if updated:
                    append_registration_paypal_event(
                        task_id=updated.task_id,
                        account_id=updated.account_id,
                        account_email=updated.account_email,
                        account_created_at=updated.account_created_at,
                        stage="account_identity_changed",
                        message="账号已删除或替换，停止支付跟进",
                        level="warning",
                        idempotency_key=f"paypal:{updated.id}:account_identity_changed",
                    )
                continue
            _process_row(row)
        except Exception:
            logger.exception("PayPal followup row processing failed row_id=%s", row.id)
    return len(rows)


def backfill_followups_from_markers(*, limit: int | None = None) -> int:
    """Recover old submitted markers after an upgrade without re-enqueueing.

    ``limit`` applies to matching payment markers, not to the account table.
    Large instances can contain many thousands of ordinary accounts before the
    first submitted marker, so limiting the unfiltered account scan would make
    recovery depend on row order and silently skip real payments.
    """
    recovered = 0
    try:
        with Session(core_db.engine) as session:
            marker_path = "$.chatgpt_paypal_auto_payment"
            marker_status = func.lower(
                func.coalesce(
                    func.json_extract(
                        AccountModel.extra_json,
                        f"{marker_path}.status",
                    ),
                    "",
                )
            )
            statement = (
                select(AccountModel)
                .where(
                    AccountModel.platform == "chatgpt",
                    func.json_valid(AccountModel.extra_json) == 1,
                    marker_status.in_(
                        (
                            "submitted",
                            "running",
                            "payment_pending",
                            "pending",
                            "relogin_pending",
                            "local_refresh_pending",
                        )
                    ),
                    func.coalesce(
                        func.json_extract(AccountModel.extra_json, f"{marker_path}.batch_id"),
                        "",
                    )
                    != "",
                    func.coalesce(
                        func.json_extract(AccountModel.extra_json, f"{marker_path}.item_id"),
                        "",
                    )
                    != "",
                )
                .order_by(AccountModel.id.asc())
            )
            if limit is not None:
                statement = statement.limit(max(1, int(limit)))
            accounts = session.exec(statement).all()
        for account in accounts:
            extra = account.get_extra()
            marker = extra.get("chatgpt_paypal_auto_payment")
            if not isinstance(marker, dict):
                continue
            batch_id = str(marker.get("batch_id") or "").strip()
            item_id = str(marker.get("item_id") or "").strip()
            if not batch_id or not item_id:
                continue
            state = str(marker.get("status") or "").strip().lower()
            if state in {
                "submitted",
                "running",
                "payment_pending",
                "pending",
                "relogin_pending",
                "local_refresh_pending",
            }:
                row = ensure_payment_followup(
                    task_id=str(marker.get("task_id") or "history-reconciliation"),
                    account_id=int(account.id or 0),
                    account_email=str(account.email or ""),
                    account_created_at=_account_created_at_text(account.created_at),
                    batch_id=batch_id,
                    item_id=item_id,
                    remote_status=str(marker.get("remote_status") or "pending"),
                    idempotent=True,
                )
                if row is not None:
                    # The table is authoritative.  Repair legacy markers left
                    # on an active state by an older worker after the durable
                    # row had already reached a terminal result.
                    _update_marker_for_followup(
                        row.account_id,
                        email=row.account_email,
                        created_at=row.account_created_at,
                        row=row,
                    )
                    recovered += 1
                    append_registration_paypal_event(
                        task_id=row.task_id,
                        account_id=row.account_id,
                        account_email=row.account_email,
                        account_created_at=row.account_created_at,
                        stage="history_reconciliation",
                        message="历史 PayPal 提交记录已恢复跟进（不重复支付）",
                        metadata={"batch_id": row.batch_id, "item_id": row.item_id},
                        idempotency_key=f"paypal:{row.id}:history_reconciliation",
                    )
    except Exception:
        logger.warning("PayPal marker backfill failed", exc_info=True)
    return recovered


def _worker_loop() -> None:
    backfill_followups_from_markers()
    while not _STOP_EVENT.is_set():
        try:
            reconcile_due_followups()
        except Exception:
            logger.exception("PayPal followup worker iteration failed")
        _STOP_EVENT.wait(POLL_INTERVAL_SECONDS)


def start_registration_paypal_followup_worker() -> None:
    global _WORKER_THREAD
    with _WORKER_LOCK:
        if _WORKER_THREAD is not None and _WORKER_THREAD.is_alive():
            return
        _STOP_EVENT.clear()
        _WORKER_THREAD = threading.Thread(
            target=_worker_loop,
            name="registration-paypal-followup",
            daemon=True,
        )
        _WORKER_THREAD.start()


def stop_registration_paypal_followup_worker() -> None:
    global _WORKER_THREAD
    with _WORKER_LOCK:
        _STOP_EVENT.set()
        thread = _WORKER_THREAD
        _WORKER_THREAD = None
    if thread is not None and thread.is_alive():
        thread.join(timeout=5.0)
