"""Durable, credential-free registration post-processing state."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlmodel import Session


PIPELINE_MARKER_KEY = "chatgpt_registration_pipeline"
PIPELINE_VERSION = 2
CONTINUATION_RUNNING_TIMEOUT_SECONDS = 30 * 60
PIPELINE_ACTIVE_MAX_IDLE_SECONDS = 30 * 60

_ACTIVE_STATES = {
    "queued",
    "running",
    "submitting",
    "submitted",
    "payment_pending",
}
_PAYMENT_SUCCESS_FOLLOWUP_STATES = {
    "payment_authorized",
    "relogin_pending",
    "local_refresh_pending",
    "subscription_confirmed",
    "relogin_failed",
    "local_unconfirmed",
}
_PUBLIC_STAGE_FIELDS = {
    "state",
    "reason_code",
    "message",
    "updated_at",
    "checked_at",
    "generated_at",
    "amount_display",
    "currency",
    "followup_state",
    "batch_id",
    "item_id",
    "remote_status",
    "remote_stage",
    "payment_result",
    "payment_result_code",
    "settlement_status",
    "paypal_authorized",
    "merchant_redirect_succeeded",
    "entitlement_verified",
    "idempotent",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: Any, limit: int = 500) -> str:
    return str(value or "").strip()[:limit]


def _account_created_at_text(value: Any) -> str:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None).isoformat(sep=" ")
    return _text(value, 64)


def _country_code(value: Any, default: str = "VN") -> str:
    country = _text(value, 8).upper()
    if len(country) == 2 and country.isascii() and country.isalpha():
        return country
    return default


def _timestamp(value: Any) -> float:
    text = _text(value, 64)
    if not text:
        return 0.0
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _now_timestamp() -> float:
    return datetime.now(timezone.utc).timestamp()


def _stage_is_active(value: Any) -> bool:
    stage = value if isinstance(value, dict) else {}
    if _text(stage.get("state"), 64).lower() not in _ACTIVE_STATES:
        return False
    updated_at = _timestamp(stage.get("updated_at"))
    if updated_at <= 0:
        return False
    return _now_timestamp() - updated_at <= PIPELINE_ACTIVE_MAX_IDLE_SECONDS


def _stage(
    state: str,
    *,
    reason_code: str = "",
    message: str = "",
    at: str = "",
    **metadata: Any,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "state": _text(state, 64).lower() or "unknown",
        "reason_code": _text(reason_code, 128),
        "message": _text(message),
        "updated_at": _text(at, 64) or _now_iso(),
    }
    for key, value in metadata.items():
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            result[_text(key, 64)] = value
        elif isinstance(value, (int, float)):
            result[_text(key, 64)] = value
        else:
            result[_text(key, 64)] = _text(value, 500)
    return result


def _requested_flags(value: Any) -> dict[str, bool]:
    source = value if isinstance(value, dict) else {}
    return {
        "zero_amount": bool(source.get("zero_amount")),
        "payment_link": bool(source.get("payment_link")),
        "payment": bool(source.get("payment")),
    }


def _public_stage(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    result: dict[str, Any] = {}
    for key in _PUBLIC_STAGE_FIELDS:
        item = source.get(key)
        if item is None or item == "":
            continue
        if isinstance(item, bool):
            result[key] = item
        elif isinstance(item, (int, float)):
            result[key] = item
        else:
            result[key] = _text(item, 500)
    result.setdefault("state", "unknown")
    return result


def initialize_registration_pipeline(
    account_id: int,
    *,
    email: str,
    created_at: str,
    task_id: str,
    zero_amount_enabled: bool,
    payment_link_enabled: bool,
    payment_enabled: bool,
    auth_pending: bool = False,
    legacy_combined: bool = False,
    zero_amount_checkout_country: str = "VN",
    payment_link_profile_hash: str = "",
    payment_link_type: str = "paypal",
) -> bool:
    """Initialize the account-level pipeline after registration is persisted."""

    from core import db as core_db
    from core.db import AccountModel

    try:
        with Session(core_db.engine) as session:
            account = session.get(AccountModel, int(account_id or 0))
            if account is None or str(account.platform or "").lower() != "chatgpt":
                return False
            if str(account.email or "").strip().lower() != str(email or "").strip().lower():
                return False
            if _account_created_at_text(account.created_at) != _text(created_at, 64):
                return False

            requested = {
                "zero_amount": bool(zero_amount_enabled),
                "payment_link": bool(payment_link_enabled),
                "payment": bool(payment_enabled),
            }
            zero_state = (
                _stage(
                    "pending_auth",
                    reason_code="registered_auth_pending",
                    message="注册成功但 Auth 待补抓，0 元检测暂停",
                )
                if zero_amount_enabled and auth_pending
                else _stage("queued", reason_code="registration_succeeded")
                if zero_amount_enabled
                else _stage("disabled", reason_code="not_requested")
            )
            link_state = (
                _stage("disabled", reason_code="not_requested")
                if not payment_link_enabled
                else _stage(
                    "pending_auth",
                    reason_code="registered_auth_pending",
                    message="注册成功但 Auth 待补抓，未执行提链",
                )
                if auth_pending
                else _stage(
                    "waiting_zero_amount" if zero_amount_enabled else "queued",
                    reason_code=("zero_amount_not_completed" if zero_amount_enabled else "legacy_combined"),
                )
            )
            payment_state = (
                _stage("disabled", reason_code="not_requested")
                if not payment_enabled
                else _stage(
                    "blocked",
                    reason_code=("registered_auth_pending" if auth_pending else "payment_link_not_completed"),
                    message=("Auth 待补抓，未执行支付" if auth_pending else "等待提链成功"),
                )
            )
            marker = {
                "version": PIPELINE_VERSION,
                "task_id": _text(task_id, 160),
                "requested": requested,
                "legacy_combined": bool(legacy_combined),
                # Only non-secret frozen selectors are retained. Runtime proxy
                # credentials and payment URLs must never enter this marker.
                "continuation": {
                    "state": "waiting_auth" if auth_pending else "not_needed",
                    "zero_amount_checkout_country": _country_code(
                        zero_amount_checkout_country
                    ),
                    "payment_link_profile_hash": _text(
                        payment_link_profile_hash,
                        128,
                    ),
                    "payment_link_type": (
                        _text(payment_link_type, 32).lower() or "paypal"
                    ),
                    "attempts": 0,
                    "updated_at": _now_iso(),
                },
                "registration": _stage(
                    "pending_auth" if auth_pending else "succeeded",
                    reason_code=("registered_auth_pending" if auth_pending else "account_saved"),
                    message=("注册成功，Auth 待补抓" if auth_pending else "注册成功并已保存账号"),
                ),
                "zero_amount": zero_state,
                "payment_link": link_state,
                "payment": payment_state,
                "updated_at": _now_iso(),
            }
            extra = account.get_extra()
            extra[PIPELINE_MARKER_KEY] = marker
            account.set_extra(extra)
            account.updated_at = datetime.now(timezone.utc)
            session.add(account)
            session.commit()
            return True
    except Exception:
        return False


def claim_registration_pipeline_continuation(account_id: int) -> dict[str, Any]:
    """Claim one Auth-blocked pipeline and return its credential-free intent."""

    from core import db as core_db
    from core.db import AccountModel

    try:
        with Session(core_db.engine) as session:
            account = session.get(AccountModel, int(account_id or 0))
            if account is None or str(account.platform or "").lower() != "chatgpt":
                return {"claimed": False, "reason": "account_not_found"}
            extra = account.get_extra()
            marker = extra.get(PIPELINE_MARKER_KEY)
            if not isinstance(marker, dict):
                return {"claimed": False, "reason": "pipeline_not_found"}

            access_token = _text(
                extra.get("access_token")
                or extra.get("accessToken")
                or getattr(account, "token", ""),
                16384,
            )
            if not access_token:
                return {"claimed": False, "reason": "auth_still_missing"}

            requested = _requested_flags(marker.get("requested"))
            registration = (
                marker.get("registration")
                if isinstance(marker.get("registration"), dict)
                else {}
            )
            downstream = [
                marker.get(name) if isinstance(marker.get(name), dict) else {}
                for name in ("zero_amount", "payment_link", "payment")
            ]
            needs_continuation = (
                _text(registration.get("state"), 64).lower() == "pending_auth"
                or any(
                    _text(stage.get("state"), 64).lower() == "pending_auth"
                    or _text(stage.get("reason_code"), 128).lower()
                    == "registered_auth_pending"
                    for stage in downstream
                )
            )

            marker["registration"] = _stage(
                "succeeded",
                reason_code="auth_recovered",
                message="注册成功，Auth 已补抓",
            )
            continuation = (
                dict(marker.get("continuation"))
                if isinstance(marker.get("continuation"), dict)
                else {}
            )
            if not needs_continuation or not any(requested.values()):
                continuation.update(
                    {
                        "state": "completed",
                        "updated_at": _now_iso(),
                    }
                )
                marker["continuation"] = continuation
                marker["updated_at"] = _now_iso()
                extra[PIPELINE_MARKER_KEY] = marker
                account.set_extra(extra)
                account.updated_at = datetime.now(timezone.utc)
                session.add(account)
                session.commit()
                return {"claimed": False, "reason": "nothing_to_resume"}

            continuation_state = _text(continuation.get("state"), 64).lower()
            continuation_updated_at = _timestamp(continuation.get("updated_at"))
            if (
                continuation_state == "running"
                and continuation_updated_at > 0
                and datetime.now(timezone.utc).timestamp() - continuation_updated_at
                < CONTINUATION_RUNNING_TIMEOUT_SECONDS
            ):
                return {"claimed": False, "reason": "already_running"}

            continuation.update(
                {
                    "state": "running",
                    "attempts": max(int(continuation.get("attempts") or 0), 0) + 1,
                    "started_at": _now_iso(),
                    "updated_at": _now_iso(),
                }
            )
            marker["continuation"] = continuation
            marker["updated_at"] = _now_iso()
            extra[PIPELINE_MARKER_KEY] = marker
            account.set_extra(extra)
            account.updated_at = datetime.now(timezone.utc)
            session.add(account)
            session.commit()
            return {
                "claimed": True,
                "account_id": int(account.id or 0),
                "email": str(account.email or ""),
                "created_at": _account_created_at_text(account.created_at),
                "task_id": _text(marker.get("task_id"), 160),
                "requested": requested,
                "legacy_combined": bool(marker.get("legacy_combined")),
                "zero_amount_checkout_country": _country_code(
                    continuation.get("zero_amount_checkout_country")
                ),
                "payment_link_profile_hash": _text(
                    continuation.get("payment_link_profile_hash"),
                    128,
                ),
                "payment_link_type": (
                    _text(continuation.get("payment_link_type"), 32).lower()
                    or "paypal"
                ),
                "zero_amount_state": _text(
                    (marker.get("zero_amount") or {}).get("state"),
                    64,
                ).lower(),
                "payment_link_state": _text(
                    (marker.get("payment_link") or {}).get("state"),
                    64,
                ).lower(),
                "payment_state": _text(
                    (marker.get("payment") or {}).get("state"),
                    64,
                ).lower(),
            }
    except Exception:
        return {"claimed": False, "reason": "claim_failed"}


def finish_registration_pipeline_continuation(
    account_id: int,
    *,
    state: str,
    message: str = "",
) -> bool:
    from core import db as core_db
    from core.db import AccountModel

    try:
        with Session(core_db.engine) as session:
            account = session.get(AccountModel, int(account_id or 0))
            if account is None or str(account.platform or "").lower() != "chatgpt":
                return False
            extra = account.get_extra()
            marker = extra.get(PIPELINE_MARKER_KEY)
            if not isinstance(marker, dict):
                return False
            continuation = (
                dict(marker.get("continuation"))
                if isinstance(marker.get("continuation"), dict)
                else {}
            )
            continuation.update(
                {
                    "state": _text(state, 64).lower() or "completed",
                    "message": _text(message),
                    "completed_at": _now_iso(),
                    "updated_at": _now_iso(),
                }
            )
            marker["continuation"] = continuation
            marker["version"] = PIPELINE_VERSION
            marker["updated_at"] = _now_iso()
            extra[PIPELINE_MARKER_KEY] = marker
            account.set_extra(extra)
            account.updated_at = datetime.now(timezone.utc)
            session.add(account)
            session.commit()
            return True
    except Exception:
        return False


def update_registration_pipeline_stage(
    account_id: int,
    stage_name: str,
    state: str,
    *,
    task_id: str = "",
    email: str = "",
    created_at: str = "",
    reason_code: str = "",
    message: str = "",
    **metadata: Any,
) -> bool:
    """Update one stage without allowing an old async task to overwrite a new one."""

    from core import db as core_db
    from core.db import AccountModel

    stage_key = _text(stage_name, 64)
    if stage_key not in {"registration", "zero_amount", "payment_link", "payment"}:
        return False
    try:
        with Session(core_db.engine) as session:
            account = session.get(AccountModel, int(account_id or 0))
            if account is None or str(account.platform or "").lower() != "chatgpt":
                return False
            if email and str(account.email or "").strip().lower() != str(email).strip().lower():
                return False
            if created_at and _account_created_at_text(account.created_at) != _text(created_at, 64):
                return False
            extra = account.get_extra()
            marker = extra.get(PIPELINE_MARKER_KEY)
            if not isinstance(marker, dict):
                marker = {
                    "version": PIPELINE_VERSION,
                    "task_id": _text(task_id, 160),
                    "requested": {},
                }
            marker_task_id = _text(marker.get("task_id"), 160)
            if marker_task_id and task_id and marker_task_id != _text(task_id, 160):
                return False
            if task_id and not marker_task_id:
                marker["task_id"] = _text(task_id, 160)
            marker[stage_key] = _stage(
                state,
                reason_code=reason_code,
                message=message,
                **metadata,
            )
            marker["version"] = PIPELINE_VERSION
            marker["updated_at"] = _now_iso()
            extra[PIPELINE_MARKER_KEY] = marker
            account.set_extra(extra)
            account.updated_at = datetime.now(timezone.utc)
            session.add(account)
            session.commit()
            return True
    except Exception:
        return False


def block_registration_pipeline_downstream(
    account_id: int,
    *,
    task_id: str,
    email: str,
    zero_state: str,
    reason_code: str = "",
    message: str = "",
    payment_link_enabled: bool = True,
    payment_enabled: bool = True,
) -> None:
    state = _text(zero_state, 64).lower()
    reason = _text(reason_code, 128) or f"zero_amount_{state or 'not_eligible'}"
    if state == "pending_auth":
        link_state = "pending_auth"
        link_message = "Auth 待补抓，未执行提链"
    else:
        link_state = "blocked"
        link_message = message or "0 元资格未通过，未执行提链"
    if payment_link_enabled:
        update_registration_pipeline_stage(
            account_id,
            "payment_link",
            link_state,
            task_id=task_id,
            email=email,
            reason_code=reason,
            message=link_message,
        )
    if payment_enabled:
        update_registration_pipeline_stage(
            account_id,
            "payment",
            "blocked",
            task_id=task_id,
            email=email,
            reason_code="payment_link_not_completed",
            message="提链未成功，未执行支付",
        )


def apply_payment_followup_to_extra(extra: dict[str, Any], row: Any) -> dict[str, Any]:
    """Mirror one PayPal followup row into the credential-free pipeline marker."""

    if not isinstance(extra, dict):
        return extra
    marker = extra.get(PIPELINE_MARKER_KEY)
    if not isinstance(marker, dict):
        marker = {
            "version": PIPELINE_VERSION,
            "task_id": _text(getattr(row, "task_id", ""), 160),
            "requested": {"zero_amount": False, "payment_link": True, "payment": True},
            "registration": _stage("succeeded", reason_code="account_saved"),
            "payment_link": _stage("succeeded", reason_code="paypal_url_persisted"),
        }
    marker_task_id = _text(marker.get("task_id"), 160)
    row_task_id = _text(getattr(row, "task_id", ""), 160)
    if marker_task_id and row_task_id and marker_task_id != row_task_id:
        return extra

    followup_state = _text(getattr(row, "state", ""), 64).lower()
    authorized = bool(getattr(row, "paypal_authorized", False))
    if authorized or followup_state in _PAYMENT_SUCCESS_FOLLOWUP_STATES:
        state = "succeeded"
        reason_code = _text(getattr(row, "payment_result_code", ""), 128) or "paypal_authorized"
    elif followup_state == "payment_failed":
        state = "failed"
        reason_code = _text(getattr(row, "payment_result_code", ""), 128) or "payment_failed"
    elif followup_state == "payment_unknown":
        state = "unknown"
        reason_code = _text(getattr(row, "payment_result_code", ""), 128) or "payment_unknown"
    elif followup_state == "payment_pending":
        state = "payment_pending"
        reason_code = "payment_pending"
    else:
        state = "payment_pending"
        reason_code = followup_state or "payment_pending"

    marker["payment"] = _stage(
        state,
        reason_code=reason_code,
        message=(
            _text(getattr(row, "last_error", ""))
            or _text(getattr(row, "payment_result", ""))
            or _text(getattr(row, "remote_stage", ""))
        ),
        followup_state=followup_state,
        batch_id=getattr(row, "batch_id", ""),
        item_id=getattr(row, "item_id", ""),
        remote_status=getattr(row, "remote_status", ""),
        remote_stage=getattr(row, "remote_stage", ""),
        payment_result=getattr(row, "payment_result", ""),
        payment_result_code=getattr(row, "payment_result_code", ""),
        settlement_status=getattr(row, "settlement_status", ""),
        paypal_authorized=authorized,
        merchant_redirect_succeeded=getattr(row, "merchant_redirect_succeeded", None),
        entitlement_verified=getattr(row, "entitlement_verified", None),
    )
    marker["version"] = PIPELINE_VERSION
    marker["updated_at"] = _now_iso()
    extra[PIPELINE_MARKER_KEY] = marker
    return extra


def _zero_amount_summary(extra: dict[str, Any]) -> dict[str, Any]:
    marker = extra.get("chatgpt_zero_amount_eligibility")
    if not isinstance(marker, dict):
        return _stage("not_run", reason_code="no_result")
    last_attempt = marker.get("last_attempt") if isinstance(marker.get("last_attempt"), dict) else {}
    confirmed = _text(marker.get("confirmed_state"), 64).lower()
    attempt = _text(last_attempt.get("state"), 64).lower()
    state = attempt if attempt in {"running", "probe_failed", "pending_auth", "skipped"} else confirmed
    if not state:
        state = "not_run"
    evidence = last_attempt.get("evidence") if isinstance(last_attempt.get("evidence"), dict) else {}
    return _stage(
        state,
        reason_code=last_attempt.get("reason_code") or marker.get("reason_code"),
        message=last_attempt.get("message") or marker.get("message"),
        at=last_attempt.get("checked_at") or marker.get("confirmed_at") or marker.get("last_attempt_at"),
        amount_display=evidence.get("amount_display"),
        currency=evidence.get("currency"),
        task_id=last_attempt.get("task_id"),
    )


def _legacy_payment_summary(extra: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    marker = extra.get("chatgpt_paypal_auto_payment")
    if not isinstance(marker, dict):
        return _stage("not_run", reason_code="no_result"), _stage("not_run", reason_code="no_result")
    status = _text(marker.get("status") or marker.get("state"), 64).lower()
    reason = _text(marker.get("reason_code"), 128)
    message = marker.get("last_error") or marker.get("message")
    marker_at = marker.get("updated_at") or marker.get("completed_at") or marker.get("started_at")
    common = {
        "task_id": marker.get("task_id"),
        "batch_id": marker.get("batch_id"),
        "item_id": marker.get("item_id"),
        "remote_status": marker.get("remote_status"),
    }
    if status == "extract_failed":
        return (
            _stage("failed", reason_code=reason or "payment_link_generation_failed", message=message, at=marker_at, **common),
            _stage("blocked", reason_code="payment_link_not_completed", at=marker_at, **common),
        )
    if status == "pending_auth":
        return (
            _stage("pending_auth", reason_code=reason or "missing_access_token", message=message, at=marker_at, **common),
            _stage("blocked", reason_code="payment_link_not_completed", at=marker_at, **common),
        )
    if status == "running" and reason == "extracting_link":
        return _stage("running", reason_code=reason, message=message, at=marker_at, **common), _stage("blocked", reason_code="payment_link_not_completed", at=marker_at, **common)
    link = _stage("succeeded", reason_code="paypal_url_persisted", at=marker_at, **common)
    if status == "submit_failed":
        payment = _stage("submit_failed", reason_code=reason or "payment_enqueue_failed", message=message, at=marker_at, **common)
    elif status in {"submitted", "payment_pending", "running"}:
        payment = _stage("payment_pending" if status == "payment_pending" else status, reason_code=reason, message=message, at=marker_at, **common)
    elif bool(marker.get("paypal_authorized")) or status in _PAYMENT_SUCCESS_FOLLOWUP_STATES:
        payment = _stage("succeeded", reason_code=marker.get("payment_result_code") or status, message=message, at=marker_at, followup_state=status, **common)
    elif status == "payment_failed":
        payment = _stage("failed", reason_code=marker.get("payment_result_code") or reason or status, message=message, at=marker_at, **common)
    elif status == "payment_unknown":
        payment = _stage("unknown", reason_code=reason or status, message=message, at=marker_at, **common)
    elif status == "link_succeeded":
        payment = _stage("disabled", reason_code="not_requested", at=marker_at)
    else:
        payment = _stage("not_run", reason_code=reason or status or "no_result", message=message, at=marker_at, **common)
    return link, payment


def _prefer_same_task_evidence(
    marker_stage: dict[str, Any],
    evidence_stage: dict[str, Any],
    *,
    marker_task_id: str,
) -> dict[str, Any]:
    evidence_state = _text(evidence_stage.get("state"), 64).lower()
    evidence_task_id = _text(evidence_stage.get("task_id"), 160)
    if evidence_state in {"", "unknown", "not_run"}:
        return marker_stage
    if not marker_stage:
        return evidence_stage
    if not marker_task_id or evidence_task_id != marker_task_id:
        return marker_stage
    marker_state = _text(marker_stage.get("state"), 64).lower()
    marker_at = _timestamp(marker_stage.get("updated_at"))
    evidence_at = _timestamp(evidence_stage.get("updated_at"))
    if evidence_at and marker_at and evidence_at < marker_at:
        return marker_stage
    if marker_state in {
        "queued",
        "running",
        "waiting_zero_amount",
        "blocked",
        "submitting",
        "submitted",
        "payment_pending",
        "pending_auth",
        "not_run",
        "unknown",
    }:
        return evidence_stage
    return evidence_stage if evidence_at >= marker_at > 0 else marker_stage


def registration_pipeline_summary(
    account: Any,
    extra: dict[str, Any],
    *,
    payment_link: dict[str, Any] | None = None,
    payment_link_generated: bool = False,
) -> dict[str, Any]:
    """Build the public pipeline view from new markers and historical evidence."""

    extra = extra if isinstance(extra, dict) else {}
    marker = extra.get(PIPELINE_MARKER_KEY)
    marker = marker if isinstance(marker, dict) else {}
    requested = _requested_flags(marker.get("requested"))
    auth_pending = bool(extra.get("registered_auth_pending")) and not bool(
        _text(extra.get("access_token") or extra.get("accessToken") or getattr(account, "token", ""))
    )
    registration = marker.get("registration") if isinstance(marker.get("registration"), dict) else _stage(
        "pending_auth" if auth_pending else "succeeded",
        reason_code="registered_auth_pending" if auth_pending else "account_saved",
        message="注册成功，Auth 待补抓" if auth_pending else "账号已成功入库",
        at=getattr(account, "created_at", ""),
    )

    registration_state = _text(registration.get("state"), 64).lower()
    if auth_pending and registration_state != "pending_auth":
        registration = _stage(
            "pending_auth",
            reason_code="registered_auth_pending",
            message="注册成功，Auth 待补抓",
            at=registration.get("updated_at"),
        )
    elif not auth_pending and registration_state == "pending_auth":
        registration = _stage(
            "succeeded",
            reason_code="auth_available",
            message="注册成功，Auth 已可用",
            at=getattr(account, "updated_at", ""),
        )

    marker_task_id = _text(marker.get("task_id"), 160)
    historical_zero = _zero_amount_summary(extra)
    zero_amount = (
        _prefer_same_task_evidence(
            marker.get("zero_amount"),
            historical_zero,
            marker_task_id=marker_task_id,
        )
        if isinstance(marker.get("zero_amount"), dict)
        else historical_zero
    )
    legacy_link, legacy_payment = _legacy_payment_summary(extra)
    link = (
        _prefer_same_task_evidence(
            marker.get("payment_link"),
            legacy_link,
            marker_task_id=marker_task_id,
        )
        if isinstance(marker.get("payment_link"), dict)
        else legacy_link
    )
    payment = (
        _prefer_same_task_evidence(
            marker.get("payment"),
            legacy_payment,
            marker_task_id=marker_task_id,
        )
        if isinstance(marker.get("payment"), dict)
        else legacy_payment
    )

    link_state = _text(link.get("state"), 64).lower()
    link_payload = payment_link if isinstance(payment_link, dict) else {}
    if (
        not isinstance(marker.get("payment_link"), dict)
        and payment_link_generated
        and link_state in {"", "unknown", "not_run", "blocked"}
    ):
        link = _stage(
            "succeeded",
            reason_code="durable_payment_link_history",
            at=link_payload.get("generated_at") or link_payload.get("created_at"),
        )
    if auth_pending and _text(link.get("state"), 64).lower() in {"not_run", "blocked", "queued", "waiting_zero_amount"}:
        link = _stage(
            "pending_auth",
            reason_code="registered_auth_pending",
            message="注册成功但 Auth 待补抓，未执行提链",
        )
    if auth_pending and _text(zero_amount.get("state"), 64).lower() in {"not_run", "queued"} and requested.get("zero_amount"):
        zero_amount = _stage(
            "pending_auth",
            reason_code="registered_auth_pending",
            message="注册成功但 Auth 待补抓，0 元检测暂停",
        )

    stages = {
        "registration": _public_stage(registration),
        "zero_amount": _public_stage(zero_amount),
        "payment_link": _public_stage(link),
        "payment": _public_stage(payment),
    }
    return {
        "version": int(marker.get("version") or 1),
        "task_id": _text(marker.get("task_id"), 160),
        "requested": requested,
        **stages,
        # Activity drives account-list polling. Old interrupted jobs can leave
        # legacy `running` markers behind, so state alone must not keep every
        # browser on a permanent four-second refresh loop.
        "active": any(_stage_is_active(stage) for stage in stages.values()),
        "updated_at": _text(marker.get("updated_at"), 64) or _text(getattr(account, "updated_at", ""), 64),
    }
