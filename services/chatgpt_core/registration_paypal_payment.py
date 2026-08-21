"""Bounded post-registration PayPal link extraction and payment handoff."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import logging
import os
import threading
from typing import Any, Callable

from sqlmodel import Session

from services.chatgpt_core.paypal_agreement_auto_client import (
    PaypalAgreementAutoClient,
    PaypalAgreementAutoError,
    normalize_paypal_approval_url,
    sanitize_paypal_agreement_error,
)
from services.chatgpt_core.task_logging import mask_email_for_log


logger = logging.getLogger(__name__)

DEFAULT_CONCURRENCY = 2
RESULT_RETAIN_LIMIT = 500
ACCOUNT_MARKER_KEY = "chatgpt_paypal_auto_payment"
_PROCESS_CAPACITY = threading.BoundedSemaphore(DEFAULT_CONCURRENCY)
_RESULT_STATES = {
    "link_succeeded",
    "submitted",
    "extract_failed",
    "submit_failed",
    "pending_auth",
    "skipped",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _account_created_at_text(value: Any) -> str:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None).isoformat(sep=" ")
    return str(value or "").strip()


def _account_identity_matches(account: Any, email: str, created_at: str) -> bool:
    if account is None or str(getattr(account, "platform", "") or "").strip().lower() != "chatgpt":
        return False
    return (
        str(getattr(account, "email", "") or "").strip().lower()
        == str(email or "").strip().lower()
        and _account_created_at_text(getattr(account, "created_at", None))
        == str(created_at or "").strip()
    )


def _safe_marker(
    *,
    status: str,
    task_id: str,
    profile_hash: str,
    reason_code: str = "",
    message: str = "",
    started_at: str = "",
    batch_id: str = "",
    item_id: str = "",
    remote_status: str = "",
    batch_status: str = "",
    idempotent: bool = False,
) -> dict[str, Any]:
    marker: dict[str, Any] = {
        "status": status,
        "task_id": str(task_id or "")[:160],
        "profile_hash": str(profile_hash or "")[:128],
        "reason_code": str(reason_code or "")[:128],
        "message": sanitize_paypal_agreement_error(message),
        "started_at": str(started_at or "")[:64],
        "updated_at": _now_iso(),
    }
    if status in _RESULT_STATES:
        marker["completed_at"] = marker["updated_at"]
    if batch_id:
        marker["batch_id"] = str(batch_id)[:128]
    if item_id:
        marker["item_id"] = str(item_id)[:128]
    if remote_status:
        marker["remote_status"] = str(remote_status)[:64]
    if batch_status:
        marker["batch_status"] = str(batch_status)[:64]
    if status == "submitted":
        marker["idempotent"] = bool(idempotent)
        marker["submitted_at"] = marker["updated_at"]
    return marker


def _persist_marker(
    account_id: int,
    *,
    email: str,
    created_at: str,
    marker: dict[str, Any],
) -> bool:
    from core import db as core_db
    from core.db import AccountModel

    try:
        with Session(core_db.engine) as session:
            account = session.get(AccountModel, int(account_id))
            if not _account_identity_matches(account, email, created_at):
                return False
            extra = account.get_extra()
            extra[ACCOUNT_MARKER_KEY] = dict(marker)
            account.set_extra(extra)
            account.updated_at = datetime.now(timezone.utc)
            session.add(account)
            session.commit()
        return True
    except Exception:
        logger.warning(
            "registration PayPal payment marker persistence failed account_id=%s",
            account_id,
            exc_info=True,
        )
        return False


def _result(
    account_id: int,
    email: str,
    state: str,
    *,
    reason_code: str,
    message: str,
    batch_id: str = "",
    item_id: str = "",
    remote_status: str = "",
    batch_status: str = "",
    idempotent: bool = False,
) -> dict[str, Any]:
    return {
        "account_id": int(account_id),
        "email": str(email or ""),
        "state": state,
        "reason_code": str(reason_code or "")[:128],
        "message": sanitize_paypal_agreement_error(message),
        "batch_id": str(batch_id or "")[:128],
        "item_id": str(item_id or "")[:128],
        "remote_status": str(remote_status or "")[:64],
        "batch_status": str(batch_status or "")[:64],
        "idempotent": bool(idempotent),
        "completed_at": _now_iso(),
    }


def _paypal_event(
    *,
    task_id: str,
    account_id: int,
    email: str,
    created_at: str,
    stage: str,
    message: str,
    level: str = "info",
    metadata: dict[str, Any] | None = None,
    idempotency_key: str = "",
) -> None:
    """Best-effort durable event; payment handoff must survive log failures."""
    try:
        from services.chatgpt_core.registration_paypal_followup import (
            append_registration_paypal_event,
        )

        append_registration_paypal_event(
            task_id=task_id,
            account_id=account_id,
            account_email=email,
            account_created_at=created_at,
            stage=stage,
            message=message,
            level=level,
            metadata=metadata,
            idempotency_key=idempotency_key,
        )
    except Exception:
        logger.warning("registration PayPal event callback failed", exc_info=True)


def run_registration_paypal_payment_for_account(
    account_id: Any,
    settings: dict[str, Any],
    *,
    task_id: str = "",
) -> dict[str, Any]:
    """Generate one PayPal approval URL, persist it, then enqueue payment."""

    from api.actions import _apply_action_result, _to_platform_account
    from core import db as core_db
    from core.base_platform import RegisterConfig
    from core.config_store import config_store
    from core.db import AccountModel
    from services.account_filters import upsert_account_list_state_for_account_ids
    from services.chatgpt_core import ChatGPTPlatform
    from services.chatgpt_core.registration_pipeline import (
        update_registration_pipeline_stage,
    )

    try:
        normalized_account_id = int(account_id or 0)
    except (TypeError, ValueError):
        normalized_account_id = 0
    if normalized_account_id <= 0:
        return _result(
            0,
            "",
            "skipped",
            reason_code="invalid_account_id",
            message="账号 ID 无效",
        )

    frozen = dict(settings or {})
    submit_payment = bool(frozen.get("submit_payment", True))
    profile_hash = str(frozen.get("profile_hash") or "").strip()
    configuration_error = sanitize_paypal_agreement_error(
        frozen.get("_configuration_error") or ""
    )
    started_at = _now_iso()
    email = ""
    created_at = ""

    with Session(core_db.engine) as session:
        account = session.get(AccountModel, normalized_account_id)
        if account is None or str(account.platform or "").strip().lower() != "chatgpt":
            return _result(
                normalized_account_id,
                "",
                "skipped",
                reason_code="account_not_found",
                message="ChatGPT 账号不存在",
            )
        email = str(account.email or "")
        created_at = _account_created_at_text(account.created_at)
        extra = account.get_extra()
        existing = extra.get(ACCOUNT_MARKER_KEY)
        if isinstance(existing, dict) and (
            str(existing.get("task_id") or "") == str(task_id or "")
            and str(existing.get("profile_hash") or "") == profile_hash
            and str(existing.get("status") or "") == "submitted"
            and str(existing.get("batch_id") or "").strip()
            and str(existing.get("item_id") or "").strip()
        ):
            update_registration_pipeline_stage(
                normalized_account_id,
                "payment_link",
                "succeeded",
                task_id=task_id,
                email=email,
                created_at=created_at,
                reason_code="paypal_url_persisted",
                message="检测到该注册任务已有 PayPal 提链结果",
                idempotent=True,
            )
            update_registration_pipeline_stage(
                normalized_account_id,
                "payment",
                "submitted",
                task_id=task_id,
                email=email,
                created_at=created_at,
                reason_code="already_submitted_locally",
                message="检测到已有支付提交记录，已恢复结果跟进",
                batch_id=str(existing.get("batch_id") or ""),
                item_id=str(existing.get("item_id") or ""),
                remote_status=str(existing.get("remote_status") or "pending"),
                batch_status=str(existing.get("batch_status") or ""),
                idempotent=True,
            )
            try:
                from services.chatgpt_core.registration_paypal_followup import ensure_payment_followup

                ensure_payment_followup(
                    task_id=task_id,
                    account_id=normalized_account_id,
                    account_email=email,
                    account_created_at=created_at,
                    batch_id=str(existing.get("batch_id") or ""),
                    item_id=str(existing.get("item_id") or ""),
                    remote_status=str(existing.get("remote_status") or "pending"),
                    idempotent=True,
                )
            except Exception:
                logger.warning("registration PayPal idempotent followup recovery failed", exc_info=True)
            _paypal_event(
                task_id=task_id,
                account_id=normalized_account_id,
                email=email,
                created_at=created_at,
                stage="payment_submitted",
                message="检测到已有 PayPal 支付提交记录，已恢复结果跟进且不会重复入队",
                metadata={
                    "batch_id": str(existing.get("batch_id") or ""),
                    "item_id": str(existing.get("item_id") or ""),
                },
                idempotency_key=f"paypal:{task_id}:{normalized_account_id}:payment_submitted",
            )
            return _result(
                normalized_account_id,
                email,
                "submitted",
                reason_code="already_submitted_locally",
                message="该注册任务已交 PayPal 支付队列",
                batch_id=str(existing.get("batch_id") or ""),
                item_id=str(existing.get("item_id") or ""),
                remote_status=str(existing.get("remote_status") or ""),
                batch_status=str(existing.get("batch_status") or ""),
                idempotent=True,
            )

        if configuration_error:
            marker = _safe_marker(
                status="extract_failed",
                task_id=task_id,
                profile_hash=profile_hash,
                reason_code="postprocessor_unavailable",
                message=configuration_error,
                started_at=started_at,
            )
            extra[ACCOUNT_MARKER_KEY] = marker
            account.set_extra(extra)
            account.updated_at = datetime.now(timezone.utc)
            session.add(account)
            session.commit()
            update_registration_pipeline_stage(
                normalized_account_id,
                "payment_link",
                "failed",
                task_id=task_id,
                email=email,
                created_at=created_at,
                reason_code="postprocessor_unavailable",
                message=configuration_error,
            )
            update_registration_pipeline_stage(
                normalized_account_id,
                "payment",
                "blocked" if submit_payment else "disabled",
                task_id=task_id,
                email=email,
                created_at=created_at,
                reason_code=("payment_link_not_completed" if submit_payment else "not_requested"),
                message=("提链配置不可用，未执行支付" if submit_payment else ""),
            )
            _paypal_event(
                task_id=task_id,
                account_id=normalized_account_id,
                email=email,
                created_at=created_at,
                stage="extract_failed",
                message=f"PayPal 提链配置不可用：{configuration_error}",
                level="warning",
                metadata={"reason_code": "postprocessor_unavailable"},
                idempotency_key=f"paypal:{task_id}:{normalized_account_id}:configuration_failed",
            )
            return _result(
                normalized_account_id,
                email,
                "extract_failed",
                reason_code="postprocessor_unavailable",
                message=configuration_error,
            )

        access_token = str(
            extra.get("access_token")
            or extra.get("accessToken")
            or account.token
            or ""
        ).strip()
        if not access_token:
            reason_code = (
                "registered_auth_pending"
                if bool(extra.get("registered_auth_pending"))
                else "missing_access_token"
            )
            message = (
                "账号注册成功但 Auth 待补抓，未执行提链"
                if reason_code == "registered_auth_pending"
                else "账号缺少 Access Token，未执行提链"
            )
            marker = _safe_marker(
                status="pending_auth",
                task_id=task_id,
                profile_hash=profile_hash,
                reason_code=reason_code,
                message=message,
                started_at=started_at,
            )
            extra[ACCOUNT_MARKER_KEY] = marker
            account.set_extra(extra)
            account.updated_at = datetime.now(timezone.utc)
            session.add(account)
            session.commit()
            update_registration_pipeline_stage(
                normalized_account_id,
                "payment_link",
                "pending_auth",
                task_id=task_id,
                email=email,
                created_at=created_at,
                reason_code=reason_code,
                message=message,
            )
            update_registration_pipeline_stage(
                normalized_account_id,
                "payment",
                "blocked" if submit_payment else "disabled",
                task_id=task_id,
                email=email,
                created_at=created_at,
                reason_code=("payment_link_not_completed" if submit_payment else "not_requested"),
                message=("Auth 待补抓，未执行支付" if submit_payment else ""),
            )
            _paypal_event(
                task_id=task_id,
                account_id=normalized_account_id,
                email=email,
                created_at=created_at,
                stage="pending_auth",
                message=message,
                level="warning",
                metadata={"reason_code": reason_code},
                idempotency_key=f"paypal:{task_id}:{normalized_account_id}:pending_auth",
            )
            return _result(
                normalized_account_id,
                email,
                "pending_auth",
                reason_code=reason_code,
                message=message,
            )

        if not profile_hash or str(frozen.get("link_type") or "").strip().lower() != "paypal":
            message = "注册任务未冻结有效的 PayPal 提链配置"
            marker = _safe_marker(
                status="extract_failed",
                task_id=task_id,
                profile_hash=profile_hash,
                reason_code="invalid_frozen_profile",
                message=message,
                started_at=started_at,
            )
            extra[ACCOUNT_MARKER_KEY] = marker
            account.set_extra(extra)
            account.updated_at = datetime.now(timezone.utc)
            session.add(account)
            session.commit()
            update_registration_pipeline_stage(
                normalized_account_id,
                "payment_link",
                "failed",
                task_id=task_id,
                email=email,
                created_at=created_at,
                reason_code="invalid_frozen_profile",
                message=message,
            )
            update_registration_pipeline_stage(
                normalized_account_id,
                "payment",
                "blocked" if submit_payment else "disabled",
                task_id=task_id,
                email=email,
                created_at=created_at,
                reason_code=("payment_link_not_completed" if submit_payment else "not_requested"),
                message=("提链配置无效，未执行支付" if submit_payment else ""),
            )
            _paypal_event(
                task_id=task_id,
                account_id=normalized_account_id,
                email=email,
                created_at=created_at,
                stage="extract_failed",
                message=message,
                level="warning",
                metadata={"reason_code": "invalid_frozen_profile"},
                idempotency_key=f"paypal:{task_id}:{normalized_account_id}:invalid_profile",
            )
            return _result(
                normalized_account_id,
                email,
                "extract_failed",
                reason_code="invalid_frozen_profile",
                message=message,
            )

        platform_account = _to_platform_account(account)
        extra[ACCOUNT_MARKER_KEY] = _safe_marker(
            status="running",
            task_id=task_id,
            profile_hash=profile_hash,
            reason_code="extracting_link",
            message="正在生成 PayPal approval URL",
            started_at=started_at,
        )
        account.set_extra(extra)
        account.updated_at = datetime.now(timezone.utc)
        session.add(account)
        session.commit()

    update_registration_pipeline_stage(
        normalized_account_id,
        "payment_link",
        "running",
        task_id=task_id,
        email=email,
        created_at=created_at,
        reason_code="extracting_link",
        message="正在生成 PayPal approval URL",
    )

    _paypal_event(
        task_id=task_id,
        account_id=normalized_account_id,
        email=email,
        created_at=created_at,
        stage="extracting_link",
        message="开始提取 PayPal approval URL",
        metadata={"profile_hash": profile_hash},
        idempotency_key=f"paypal:{task_id}:{normalized_account_id}:extracting_link",
    )

    instance_id = str(os.getenv("APP_INSTANCE_ID") or "auto-gpt").strip() or "auto-gpt"
    request_id = (
        f"registration:{instance_id}:{str(task_id or '').strip()}:{normalized_account_id}"
    )[:240]
    params = {
        "plan": "plus",
        "payment_profile_hash": profile_hash,
        "request_id": request_id,
        "reuse_cached_link": True,
    }

    try:
        instance = ChatGPTPlatform(config=RegisterConfig(extra=config_store.get_all()))
        action_result = instance.execute_action("payment_link", platform_account, params)
        if not isinstance(action_result, dict):
            raise PaypalAgreementAutoError("PayPal 提链返回格式无效")
        data = (
            action_result.get("data")
            if isinstance(action_result.get("data"), dict)
            else {}
        )
        if action_result.get("ok") is not True:
            raise PaypalAgreementAutoError(
                sanitize_paypal_agreement_error(
                    action_result.get("error")
                    or data.get("message")
                    or "PayPal 提链失败"
                )
            )
        link_type = str(data.get("link_type") or "").strip().lower()
        if link_type and link_type != "paypal":
            raise PaypalAgreementAutoError("提链结果不是 PayPal 类型")
        paypal_url = normalize_paypal_approval_url(
            data.get("paypal_url")
            or data.get("url")
            or data.get("provider_redirect_url")
        )
    except Exception as exc:
        error_text = sanitize_paypal_agreement_error(exc) or "PayPal 提链失败"
        _persist_marker(
            normalized_account_id,
            email=email,
            created_at=created_at,
            marker=_safe_marker(
                status="extract_failed",
                task_id=task_id,
                profile_hash=profile_hash,
                reason_code="payment_link_generation_failed",
                message=error_text,
                started_at=started_at,
            ),
        )
        update_registration_pipeline_stage(
            normalized_account_id,
            "payment_link",
            "failed",
            task_id=task_id,
            email=email,
            created_at=created_at,
            reason_code="payment_link_generation_failed",
            message=error_text,
        )
        update_registration_pipeline_stage(
            normalized_account_id,
            "payment",
            "blocked" if submit_payment else "disabled",
            task_id=task_id,
            email=email,
            created_at=created_at,
            reason_code=("payment_link_not_completed" if submit_payment else "not_requested"),
            message=("提链失败，未执行支付" if submit_payment else ""),
        )
        _paypal_event(
            task_id=task_id,
            account_id=normalized_account_id,
            email=email,
            created_at=created_at,
            stage="extract_failed",
            message=f"PayPal 提链失败：{error_text}",
            level="warning",
            metadata={"reason_code": "payment_link_generation_failed"},
            idempotency_key=f"paypal:{task_id}:{normalized_account_id}:extract_failed",
        )
        return _result(
            normalized_account_id,
            email,
            "extract_failed",
            reason_code="payment_link_generation_failed",
            message=error_text,
        )

    with Session(core_db.engine) as session:
        account = session.get(AccountModel, normalized_account_id)
        if not _account_identity_matches(account, email, created_at):
            return _result(
                normalized_account_id,
                email,
                "skipped",
                reason_code="account_identity_changed",
                message="账号已被删除或替换，提链结果未写入且未提交支付",
            )
        try:
            _apply_action_result(
                "chatgpt",
                "payment_link",
                account,
                {"ok": True, "data": data},
                session,
            )
            extra = account.get_extra()
            extra[ACCOUNT_MARKER_KEY] = _safe_marker(
                status="running",
                task_id=task_id,
                profile_hash=profile_hash,
                reason_code=("submitting_payment" if submit_payment else "link_persisted"),
                message=(
                    "PayPal approval URL 已保存，正在提交支付队列"
                    if submit_payment
                    else "PayPal approval URL 已保存"
                ),
                started_at=started_at,
            )
            account.set_extra(extra)
            account.updated_at = datetime.now(timezone.utc)
            session.add(account)
            upsert_account_list_state_for_account_ids(
                session,
                [normalized_account_id],
                commit=False,
            )
            session.commit()
            _paypal_event(
                task_id=task_id,
                account_id=normalized_account_id,
                email=email,
                created_at=created_at,
                stage="link_extracted",
                message=(
                    "PayPal approval URL 提取成功，已安全保存并准备提交支付队列"
                    if submit_payment
                    else "PayPal approval URL 提取成功并已安全保存"
                ),
                metadata={"link_type": "paypal"},
                idempotency_key=f"paypal:{task_id}:{normalized_account_id}:link_extracted",
            )
            update_registration_pipeline_stage(
                normalized_account_id,
                "payment_link",
                "succeeded",
                task_id=task_id,
                email=email,
                created_at=created_at,
                reason_code="paypal_url_persisted",
                message="PayPal approval URL 提取成功并已保存",
                generated_at=data.get("generated_at") or data.get("created_at"),
            )
        except Exception as exc:
            session.rollback()
            error_text = sanitize_paypal_agreement_error(exc) or "PayPal 提链结果写入失败"
            _persist_marker(
                normalized_account_id,
                email=email,
                created_at=created_at,
                marker=_safe_marker(
                    status="extract_failed",
                    task_id=task_id,
                    profile_hash=profile_hash,
                    reason_code="payment_link_persist_failed",
                    message=error_text,
                    started_at=started_at,
                ),
            )
            update_registration_pipeline_stage(
                normalized_account_id,
                "payment_link",
                "failed",
                task_id=task_id,
                email=email,
                created_at=created_at,
                reason_code="payment_link_persist_failed",
                message=error_text,
            )
            update_registration_pipeline_stage(
                normalized_account_id,
                "payment",
                "blocked" if submit_payment else "disabled",
                task_id=task_id,
                email=email,
                created_at=created_at,
                reason_code=("payment_link_not_completed" if submit_payment else "not_requested"),
                message=("提链结果写入失败，未执行支付" if submit_payment else ""),
            )
            _paypal_event(
                task_id=task_id,
                account_id=normalized_account_id,
                email=email,
                created_at=created_at,
                stage="extract_failed",
                message=f"PayPal 提链结果写入失败：{error_text}",
                level="warning",
                metadata={"reason_code": "payment_link_persist_failed"},
                idempotency_key=f"paypal:{task_id}:{normalized_account_id}:persist_failed",
            )
            return _result(
                normalized_account_id,
                email,
                "extract_failed",
                reason_code="payment_link_persist_failed",
                message=error_text,
            )

    if not submit_payment:
        link_marker = _safe_marker(
            status="link_succeeded",
            task_id=task_id,
            profile_hash=profile_hash,
            reason_code="paypal_url_persisted",
            message="PayPal approval URL 提取成功，支付未开启",
            started_at=started_at,
        )
        _persist_marker(
            normalized_account_id,
            email=email,
            created_at=created_at,
            marker=link_marker,
        )
        update_registration_pipeline_stage(
            normalized_account_id,
            "payment",
            "disabled",
            task_id=task_id,
            email=email,
            created_at=created_at,
            reason_code="not_requested",
            message="提链成功，支付未开启",
        )
        return _result(
            normalized_account_id,
            email,
            "link_succeeded",
            reason_code="paypal_url_persisted",
            message="PayPal approval URL 提取成功，支付未开启",
        )

    with Session(core_db.engine) as session:
        current = session.get(AccountModel, normalized_account_id)
        if not _account_identity_matches(current, email, created_at):
            return _result(
                normalized_account_id,
                email,
                "skipped",
                reason_code="account_identity_changed_before_submit",
                message="账号已被删除或替换，未提交 PayPal 支付",
            )

    try:
        update_registration_pipeline_stage(
            normalized_account_id,
            "payment",
            "submitting",
            task_id=task_id,
            email=email,
            created_at=created_at,
            reason_code="submitting_payment",
            message="提链成功，正在提交支付队列",
        )
        _paypal_event(
            task_id=task_id,
            account_id=normalized_account_id,
            email=email,
            created_at=created_at,
            stage="submitting_payment",
            message="开始提交 PayPal 支付队列",
            metadata={"request_id": request_id},
            idempotency_key=f"paypal:{task_id}:{normalized_account_id}:submitting_payment",
        )
        enqueue_result = PaypalAgreementAutoClient.from_env().enqueue(paypal_url)
    except Exception as exc:
        error_text = sanitize_paypal_agreement_error(exc) or "PayPal 支付入队失败"
        _persist_marker(
            normalized_account_id,
            email=email,
            created_at=created_at,
            marker=_safe_marker(
                status="submit_failed",
                task_id=task_id,
                profile_hash=profile_hash,
                reason_code="payment_enqueue_failed",
                message=error_text,
                started_at=started_at,
            ),
        )
        update_registration_pipeline_stage(
            normalized_account_id,
            "payment",
            "submit_failed",
            task_id=task_id,
            email=email,
            created_at=created_at,
            reason_code="payment_enqueue_failed",
            message=error_text,
        )
        _paypal_event(
            task_id=task_id,
            account_id=normalized_account_id,
            email=email,
            created_at=created_at,
            stage="submit_failed",
            message=f"PayPal 支付入队失败：{error_text}",
            level="warning",
            metadata={"reason_code": "payment_enqueue_failed"},
            idempotency_key=f"paypal:{task_id}:{normalized_account_id}:submit_failed",
        )
        return _result(
            normalized_account_id,
            email,
            "submit_failed",
            reason_code="payment_enqueue_failed",
            message=error_text,
        )

    batch_id = str(enqueue_result.get("batch_id") or "")
    item_id = str(enqueue_result.get("item_id") or "")
    remote_status = str(enqueue_result.get("remote_status") or "pending")
    batch_status = str(enqueue_result.get("batch_status") or "")
    idempotent = bool(enqueue_result.get("idempotent"))
    submitted_marker = _safe_marker(
        status="submitted",
        task_id=task_id,
        profile_hash=profile_hash,
        reason_code="payment_enqueued",
        message=(
            "PayPal approval URL 已存在于支付队列"
            if idempotent
            else "PayPal approval URL 已交支付队列"
        ),
        started_at=started_at,
        batch_id=batch_id,
        item_id=item_id,
        remote_status=remote_status,
        batch_status=batch_status,
        idempotent=idempotent,
    )
    marker_persisted = _persist_marker(
        normalized_account_id,
        email=email,
        created_at=created_at,
        marker=submitted_marker,
    )
    update_registration_pipeline_stage(
        normalized_account_id,
        "payment",
        "submitted",
        task_id=task_id,
        email=email,
        created_at=created_at,
        reason_code="payment_enqueued",
        message=(
            "PayPal approval URL 已存在于支付队列"
            if idempotent
            else "PayPal approval URL 已提交支付队列，等待支付结果"
        ),
        batch_id=batch_id,
        item_id=item_id,
        remote_status=remote_status,
        batch_status=batch_status,
        idempotent=idempotent,
    )
    try:
        from services.chatgpt_core.registration_paypal_followup import ensure_payment_followup

        ensure_payment_followup(
            task_id=task_id,
            account_id=normalized_account_id,
            account_email=email,
            account_created_at=created_at,
            batch_id=batch_id,
            item_id=item_id,
            remote_status=remote_status,
            idempotent=idempotent,
        )
    except Exception:
        logger.warning("registration PayPal followup creation failed", exc_info=True)
    _paypal_event(
        task_id=task_id,
        account_id=normalized_account_id,
        email=email,
        created_at=created_at,
        stage="payment_submitted",
        message=(
            "PayPal approval URL 已存在于支付队列，已恢复结果跟进"
            if idempotent
            else "PayPal approval URL 已提交支付队列，等待支付结果"
        ),
        metadata={"batch_id": batch_id, "item_id": item_id, "remote_status": remote_status},
        idempotency_key=f"paypal:{task_id}:{normalized_account_id}:payment_submitted",
    )
    message = str(submitted_marker.get("message") or "")
    if not marker_persisted:
        message = "PayPal approval URL 已交支付队列，但账号已被删除或替换，本地标记未写入"
    return _result(
        normalized_account_id,
        email,
        "submitted",
        reason_code="payment_enqueued",
        message=message,
        batch_id=batch_id,
        item_id=item_id,
        remote_status=remote_status,
        batch_status=batch_status,
        idempotent=idempotent,
    )


class RegistrationPaypalPaymentCoordinator:
    """Run extraction and durable payment handoff outside registration workers."""

    def __init__(
        self,
        *,
        task_id: str,
        settings: dict[str, Any],
        run_account: Callable[..., dict[str, Any]],
        update_meta: Callable[[dict[str, Any]], None],
        log: Callable[[str, str], None],
        on_result: Callable[[dict[str, Any]], None] | None = None,
        concurrency: int = DEFAULT_CONCURRENCY,
    ) -> None:
        self.task_id = str(task_id or "")
        self.settings = dict(settings or {})
        self.run_account = run_account
        self.update_meta = update_meta
        self.log = log
        self.on_result = on_result
        self.concurrency = max(
            1,
            min(int(concurrency or DEFAULT_CONCURRENCY), DEFAULT_CONCURRENCY),
        )
        self._lock = threading.RLock()
        self._executor: ThreadPoolExecutor | None = None
        self._executor_error = ""
        try:
            self._executor = ThreadPoolExecutor(
                max_workers=self.concurrency,
                thread_name_prefix="registration-paypal-payment",
            )
        except Exception as exc:
            self._executor_error = sanitize_paypal_agreement_error(
                exc
            ) or "后处理线程池不可用"
        self._account_ids: set[int] = set()
        self._results: list[dict[str, Any]] = []
        self._finished = False
        self._counts = {
            "queued": 0,
            "running": 0,
            "link_succeeded": 0,
            "submitted": 0,
            "extract_failed": 0,
            "submit_failed": 0,
            "pending_auth": 0,
            "skipped": 0,
            "completed": 0,
        }
        self._publish()

    def _snapshot(self) -> dict[str, Any]:
        with self._lock:
            link_profile = self.settings.get("link_profile")
            payment_profile = self.settings.get("payment_profile")
            submitted_results = [
                item
                for item in self._results
                if str(item.get("state") or "").strip().lower() == "submitted"
            ]
            return {
                "enabled": True,
                "link_profile": dict(link_profile) if isinstance(link_profile, dict) else {},
                "payment_profile": (
                    dict(payment_profile) if isinstance(payment_profile, dict) else {}
                ),
                "payment_enabled": bool(self.settings.get("submit_payment", True)),
                "profile_hash": str(self.settings.get("profile_hash") or "")[:128],
                "effective_concurrency": self.concurrency,
                "global_concurrency_limit": DEFAULT_CONCURRENCY,
                "scheduled": len(self._account_ids),
                "finished": self._finished,
                "counts": dict(self._counts),
                "results": list(self._results[-RESULT_RETAIN_LIMIT:]),
                # Keep successful queue handoffs independent from mixed failures,
                # so later failures cannot push the account list out of the UI.
                "submitted_results": list(
                    submitted_results[-RESULT_RETAIN_LIMIT:]
                ),
                "submitted_results_total": len(submitted_results),
                "submitted_results_truncated": (
                    len(submitted_results) > RESULT_RETAIN_LIMIT
                ),
            }

    def _publish(self) -> None:
        try:
            self.update_meta(self._snapshot())
        except Exception:
            logger.warning(
                "registration PayPal payment meta update failed task_id=%s",
                self.task_id,
                exc_info=True,
            )

    def _log_safely(self, message: str, level: str = "info") -> None:
        try:
            self.log(sanitize_paypal_agreement_error(message), level)
        except Exception:
            logger.warning(
                "registration PayPal payment log callback failed task_id=%s",
                self.task_id,
                exc_info=True,
            )

    def _failure_result(self, account_id: int, email: str, exc: Exception) -> dict[str, Any]:
        error_text = sanitize_paypal_agreement_error(exc) or "PayPal 提链后处理异常"
        try:
            from services.chatgpt_core.registration_pipeline import (
                update_registration_pipeline_stage,
            )

            update_registration_pipeline_stage(
                account_id,
                "payment_link",
                "failed",
                task_id=self.task_id,
                email=email,
                reason_code="task_exception",
                message=error_text,
            )
            update_registration_pipeline_stage(
                account_id,
                "payment",
                "blocked" if bool(self.settings.get("submit_payment", True)) else "disabled",
                task_id=self.task_id,
                email=email,
                reason_code=(
                    "payment_link_not_completed"
                    if bool(self.settings.get("submit_payment", True))
                    else "not_requested"
                ),
                message=(
                    "提链异常，未执行支付"
                    if bool(self.settings.get("submit_payment", True))
                    else ""
                ),
            )
        except Exception:
            logger.warning(
                "registration PayPal unhandled failure marker update failed account_id=%s",
                account_id,
                exc_info=True,
            )
        return _result(
            account_id,
            email,
            "extract_failed",
            reason_code="task_exception",
            message=error_text,
        )

    def _record_result(
        self,
        account_id: int,
        email: str,
        result: Any,
        *,
        was_running: bool,
    ) -> None:
        if not isinstance(result, dict):
            result = self._failure_result(
                account_id,
                email,
                TypeError("PayPal 后处理返回了无效结果"),
            )
        state = str(result.get("state") or "submit_failed").strip().lower()
        if state not in _RESULT_STATES:
            result = self._failure_result(
                account_id,
                email,
                ValueError(f"PayPal 后处理返回了未知状态: {state or '-'}"),
            )
            state = "submit_failed"
        compact = {
            "account_id": int(account_id),
            "email": str(result.get("email") or email or ""),
            "state": state,
            "reason_code": str(result.get("reason_code") or "")[:128],
            "message": sanitize_paypal_agreement_error(
                result.get("message") or result.get("error") or ""
            ),
            "batch_id": str(result.get("batch_id") or "")[:128],
            "item_id": str(result.get("item_id") or "")[:128],
            "remote_status": str(result.get("remote_status") or "")[:64],
            "batch_status": str(result.get("batch_status") or "")[:64],
            "idempotent": bool(result.get("idempotent")),
            "completed_at": str(result.get("completed_at") or "")[:64],
        }
        with self._lock:
            if was_running:
                self._counts["running"] = max(0, self._counts["running"] - 1)
            self._counts[state] += 1
            self._counts["completed"] += 1
            self._results.append(compact)
        self._publish()

        label = {
            "link_succeeded": "提链成功，支付未开启",
            "submitted": "已交支付队列",
            "extract_failed": "提链失败",
            "submit_failed": "支付入队失败",
            "pending_auth": "待补 Auth",
            "skipped": "已跳过",
        }[state]
        level = "info" if state in {"link_succeeded", "submitted"} else "warning"
        self._log_safely(
            f"[PayPal 注册链路] 完成｜账号={mask_email_for_log(email) or account_id}"
            f"｜结果={label}｜原因码={compact['reason_code'] or '-'}",
            level,
        )
        if self.on_result is not None:
            try:
                self.on_result(dict(compact))
            except Exception:
                logger.warning(
                    "registration PayPal result callback failed task_id=%s account_id=%s",
                    self.task_id,
                    account_id,
                    exc_info=True,
                )

    def submit(self, account_id: Any, email: str = "") -> bool:
        try:
            account_id_value = int(account_id or 0)
        except (TypeError, ValueError):
            account_id_value = 0
        if account_id_value <= 0:
            return False

        with self._lock:
            executor = self._executor
            if self._finished or account_id_value in self._account_ids:
                return False
            self._account_ids.add(account_id_value)
            self._counts["queued"] += 1
        self._publish()
        if executor is None:
            with self._lock:
                self._counts["queued"] = max(0, self._counts["queued"] - 1)
            self._record_configuration_failure(
                account_id_value,
                str(email or ""),
                self._executor_error or "后处理线程池不可用",
            )
            return True
        try:
            executor.submit(self._run, account_id_value, str(email or ""))
        except Exception as exc:
            with self._lock:
                self._counts["queued"] = max(0, self._counts["queued"] - 1)
            self._record_configuration_failure(
                account_id_value,
                str(email or ""),
                f"后处理入队失败: {sanitize_paypal_agreement_error(exc)}",
            )
        return True

    def _record_configuration_failure(
        self,
        account_id: int,
        email: str,
        error_text: str,
    ) -> None:
        try:
            result = self.run_account(
                account_id,
                {
                    **self.settings,
                    "_configuration_error": sanitize_paypal_agreement_error(error_text),
                },
                task_id=self.task_id,
            )
        except Exception as exc:
            result = self._failure_result(account_id, email, exc)
        self._record_result(
            account_id,
            email,
            result,
            was_running=False,
        )

    def _run(self, account_id: int, email: str) -> None:
        with _PROCESS_CAPACITY:
            with self._lock:
                self._counts["queued"] = max(0, self._counts["queued"] - 1)
                self._counts["running"] += 1
            self._publish()
            self._log_safely(
                f"[PayPal 提链] 开始｜账号={mask_email_for_log(email) or account_id}",
                "info",
            )
            try:
                result = self.run_account(
                    account_id,
                    self.settings,
                    task_id=self.task_id,
                )
            except Exception as exc:
                result = self._failure_result(account_id, email, exc)
            self._record_result(
                account_id,
                email,
                result,
                was_running=True,
            )

    def finish(self) -> dict[str, Any]:
        with self._lock:
            executor = self._executor
            self._executor = None
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=False)

        with self._lock:
            self._finished = True
            counts = dict(self._counts)
        self._publish()
        if counts["completed"]:
            self._log_safely(
                "[PayPal 注册链路] 汇总｜"
                f"仅提链成功={counts['link_succeeded']}｜已交支付队列={counts['submitted']}｜提链失败={counts['extract_failed']}｜"
                f"支付入队失败={counts['submit_failed']}｜待补 Auth={counts['pending_auth']}｜"
                f"已跳过={counts['skipped']}",
                "info",
            )
        return self._snapshot()
