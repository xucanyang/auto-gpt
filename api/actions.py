"""平台操作 API - 通用接口，各平台通过 get_platform_actions/execute_action 实现"""
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlmodel import Session, select
from pydantic import BaseModel, Field
import json
import random
import time
from datetime import datetime
from typing import Any
from core.db import AccountModel, PaymentLinkGenerationModel, get_session
from core.base_platform import RegisterConfig
from core.config_store import config_store
from services.account_filters import (
    AccountFilterRequestMixin,
    AccountFilterScopeChangedError,
    resolve_filtered_accounts,
    upsert_account_list_state_for_account_ids,
)
from services.account_rate_limit_recovery import reconcile_rate_limited_accounts
from services.chatgpt_account_state import apply_chatgpt_status_policy, classify_chatgpt_capabilities, mark_payment_pending
from services.chatgpt_core import ChatGPTPlatform
from services.chatgpt_core.local_status_refresh import (
    build_chatgpt_local_status_probe_account,
    schedule_chatgpt_local_status_refresh_for_account_id,
    sync_chatgpt_account_local_status_by_id,
)
from services.chatgpt_core.invalid_account_recheck import recheck_invalid_chatgpt_account
from services.chatgpt_core.payment_link_cache import (
    PAYMENT_LINK_FORMAT_PAYPAL,
    PAYMENT_LINK_PLAN_TEAM,
    PAYMENT_SOURCE_LONG_LINK_PAYPAL,
    build_payment_link_cache_payload,
    normalize_payment_link_output_format,
    normalize_payment_link_plan,
    normalize_payment_link_source,
    payment_link_cache_for_params,
    payment_link_generation_kind,
    payment_link_variant_key,
    store_payment_link_variant,
)
from services.chatgpt_sync import update_account_model_cliproxy_sync
from services.sub2api_sync import backfill_chatgpt_account_to_sub2api, probe_chatgpt_sub2api_status, update_account_model_sub2api_sync
from services.oaipay_sync import backfill_chatgpt_account_to_oaipay, probe_chatgpt_oaipay_status, update_account_model_oaipay_sync

router = APIRouter(prefix="/actions", tags=["actions"])


def _payment_link_account_created_at_text(value: Any) -> str:
    """Match the timestamp representation persisted in SQLite account rows."""

    if isinstance(value, datetime):
        return value.replace(tzinfo=None).isoformat(sep=" ")
    return str(value or "").strip()

_LOCAL_STATUS_AUTH_ACTION_IDS = {
    "refresh_token",
    "refresh_web_session",
    "resume_subscription_auth",
    "invalid_recheck",
}
_LOCAL_STATUS_AUTH_PATCH_KEYS = {
    "access_token",
    "accessToken",
    "refresh_token",
    "refreshToken",
    "webAccessToken",
}


class ActionRequest(BaseModel):
    params: dict = {}


class BatchActionRequest(AccountFilterRequestMixin):
    account_ids: list[int] = []
    all_filtered: bool = False
    confirmed_total: int | None = Field(default=None, ge=0)
    params: dict = {}


def _task_mode_action(
    instance: ChatGPTPlatform,
    action_id: str,
) -> dict[str, Any] | None:
    normalized = str(action_id or "").strip()
    for action in instance.get_platform_actions():
        if not isinstance(action, dict) or str(action.get("id") or "").strip() != normalized:
            continue
        execution = action.get("execution") if isinstance(action.get("execution"), dict) else {}
        return dict(action) if str(execution.get("mode") or "").strip() == "task" else None
    return None


def _merge_extra_patch(base: dict, patch: dict) -> dict:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge_extra_patch(base[key], value)
        else:
            base[key] = value
    return base


def _to_bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _resolve_resume_auth_allow_phone_verification(value: Any = None) -> bool:
    if value is not None:
        return _to_bool(value, default=False)
    return _to_bool(config_store.get("chatgpt_resume_auth_allow_phone_verification", "false"), default=False)


def _to_platform_account(acc_model: AccountModel):
    from core.base_platform import Account, AccountStatus

    return Account(
        platform=acc_model.platform,
        email=acc_model.email,
        password=acc_model.password,
        user_id=acc_model.user_id,
        token=acc_model.token,
        status=AccountStatus(acc_model.status),
        extra=acc_model.get_extra(),
    )


def _apply_action_result(
    platform: str,
    action_id: str,
    acc_model: AccountModel,
    result: dict[str, Any],
    session: Session,
) -> None:
    remote_sync_key = ""
    if platform == "chatgpt" and action_id in {
        "sync_cliproxyapi_status",
        "sync_sub2api_status",
        "sync_oaipay_status",
    }:
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        sync_result = data.get("sync") if isinstance(data.get("sync"), dict) else {}
        if action_id == "sync_cliproxyapi_status":
            remote_sync_key = "cliproxyapi"
            update_account_model_cliproxy_sync(
                acc_model,
                sync_result,
                session=session,
                commit=False,
            )
        elif action_id == "sync_sub2api_status":
            remote_sync_key = "sub2api"
            update_account_model_sub2api_sync(
                acc_model,
                sync_result,
                session=session,
                commit=False,
            )
        else:
            remote_sync_key = "oaipay"
            update_account_model_oaipay_sync(
                acc_model,
                sync_result,
                session=session,
                commit=False,
            )

    extra_remove = result.get("account_extra_remove")
    if isinstance(extra_remove, list):
        extra = acc_model.get_extra()
        changed = False
        for key in extra_remove:
            normalized_key = str(key or "").strip()
            if normalized_key and normalized_key in extra:
                extra.pop(normalized_key, None)
                changed = True
        if changed:
            acc_model.set_extra(extra)
            from datetime import datetime, timezone

            acc_model.updated_at = datetime.now(timezone.utc)
            session.add(acc_model)
    if result.get("account_token_clear") is True and acc_model.token:
        acc_model.token = ""
        from datetime import datetime, timezone

        acc_model.updated_at = datetime.now(timezone.utc)
        session.add(acc_model)
    if platform == "chatgpt":
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        status_reason = ""
        if action_id == "probe_local_status":
            status_reason = apply_chatgpt_status_policy(acc_model, local_probe=data.get("probe"))
        if status_reason:
            from datetime import datetime, timezone

            acc_model.updated_at = datetime.now(timezone.utc)
            session.add(acc_model)
    account_extra_patch = result.get("account_extra_patch")
    if isinstance(account_extra_patch, dict) and remote_sync_key:
        # Dedicated sync writers normalize timestamps/state and refresh the
        # derived list index. Do not overwrite that state with the raw plugin
        # payload, while still preserving any unrelated patch keys.
        account_extra_patch = dict(account_extra_patch)
        sync_statuses = account_extra_patch.get("sync_statuses")
        if isinstance(sync_statuses, dict):
            remaining_sync_statuses = dict(sync_statuses)
            remaining_sync_statuses.pop(remote_sync_key, None)
            if remaining_sync_statuses:
                account_extra_patch["sync_statuses"] = remaining_sync_statuses
            else:
                account_extra_patch.pop("sync_statuses", None)
    if isinstance(account_extra_patch, dict) and account_extra_patch:
        extra = acc_model.get_extra()
        _merge_extra_patch(extra, account_extra_patch)
        patch = account_extra_patch
        patched_access_token = str(
            patch.get("access_token") or patch.get("accessToken") or ""
        ).strip()
        if patched_access_token:
            acc_model.token = patched_access_token
        if platform == "chatgpt":
            data = result.get("data") if isinstance(result.get("data"), dict) else {}
            probe = data.get("probe") if isinstance(data.get("probe"), dict) else extra.get("chatgpt_local")
            sync = data.get("sync") if isinstance(data.get("sync"), dict) else None
            extra["chatgpt_capabilities"] = classify_chatgpt_capabilities(
                acc_model,
                local_probe=probe if isinstance(probe, dict) else None,
                remote_sync=sync if isinstance(sync, dict) else None,
            )
        acc_model.set_extra(extra)
        from datetime import datetime, timezone
        acc_model.updated_at = datetime.now(timezone.utc)
        session.add(acc_model)
    if platform == "chatgpt" and action_id == "payment_link" and result.get("ok"):
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        checkout_url = str(data.get("url") or data.get("checkout_url") or data.get("cashier_url") or "").strip()
        if checkout_url:
            remote_request_id = str(data.get("remote_request_id") or data.get("request_id") or "").strip()
            account_email = str(acc_model.email or "").strip().lower()
            account_created_at = _payment_link_account_created_at_text(acc_model.created_at)
            existing_history = None
            if remote_request_id:
                existing_history = session.exec(
                    select(PaymentLinkGenerationModel).where(
                        PaymentLinkGenerationModel.request_id == remote_request_id
                    )
                ).first()
                if existing_history is not None:
                    history_email = str(getattr(existing_history, "account_email", "") or "").strip().lower()
                    history_created_at = _payment_link_account_created_at_text(
                        getattr(existing_history, "account_created_at", "")
                    )
                    if (
                        (history_email and history_email != account_email)
                        or (history_created_at and history_created_at != account_created_at)
                    ):
                        raise ValueError("支付链接历史请求身份不匹配")
            extra = acc_model.get_extra()
            existing_cache = payment_link_cache_for_params(extra, data)
            cache_payload = build_payment_link_cache_payload(
                data,
                source=str(data.get("cache_source") or "payment_link_action"),
                fallback=existing_cache,
            )
            if not cache_payload:
                raise ValueError("支付链接结果无法构建安全缓存")
            acc_model.cashier_url = checkout_url
            cache_payload["generation_kind"] = str(
                data.get("generation_kind") or payment_link_generation_kind(cache_payload)
            )
            cache_payload["variant_key"] = str(
                data.get("variant_key") or payment_link_variant_key(cache_payload)
            )
            store_payment_link_variant(extra, cache_payload, make_current=True)
            is_team = normalize_payment_link_plan(cache_payload.get("plan")) == PAYMENT_LINK_PLAN_TEAM
            if (
                not is_team
                and (
                    str(data.get("link_type") or cache_payload.get("link_type") or "").strip().lower() == "paypal"
                    or normalize_payment_link_output_format(cache_payload.get("payment_link_format")) == PAYMENT_LINK_FORMAT_PAYPAL
                    or normalize_payment_link_source(cache_payload.get("payment_source")) == PAYMENT_SOURCE_LONG_LINK_PAYPAL
                )
            ):
                paypal_payload = dict(cache_payload)
                paypal_payload["paypal_url"] = str(paypal_payload.get("paypal_url") or checkout_url).strip()
                extra["chatgpt_paypal_url"] = paypal_payload
            acc_model.set_extra(extra)
            mark_payment_pending(acc_model, reason="payment_link_generated")
            from datetime import datetime, timezone

            acc_model.updated_at = datetime.now(timezone.utc)
            session.add(acc_model)
            if remote_request_id:
                history = existing_history
                if history is None:
                    history = PaymentLinkGenerationModel(
                        account_id=int(acc_model.id or 0),
                        account_email=account_email,
                        account_created_at=account_created_at,
                        request_id=remote_request_id,
                    )
                history.account_id = int(acc_model.id or 0)
                history.account_email = account_email
                history.account_created_at = account_created_at
                history.status = "succeeded"
                history.remote_batch_id = str(data.get("remote_batch_id") or history.remote_batch_id or "")[:128]
                history.remote_job_id = str(data.get("remote_job_id") or history.remote_job_id or "")[:128]
                history.profile_hash = str(data.get("profile_hash") or history.profile_hash or "")[:128]
                history.link_type = str(data.get("link_type") or history.link_type or "")[:64]
                history.generation_kind = str(
                    data.get("generation_kind") or cache_payload.get("generation_kind") or history.generation_kind or "plus_checkout"
                )[:64]
                history.variant_key = str(
                    data.get("variant_key") or cache_payload.get("variant_key") or history.variant_key or ""
                )[:128]
                history.url = checkout_url[:10_000]
                history.generated_at = str(data.get("generated_at") or history.generated_at or datetime.now(timezone.utc).isoformat())[:64]
                history.persisted_at = datetime.now(timezone.utc).isoformat()
                history.sanitized_error = ""
                history.set_result(
                    {
                        key: data[key]
                        for key in (
                            "url",
                            "paypal_url",
                            "plan",
                            "generation_kind",
                            "variant_key",
                            "plan_name",
                            "team_plan_data",
                            "workspace_name",
                            "price_interval",
                            "seat_quantity",
                            "promo_code_digest",
                            "cancel_url",
                            "checkout_proxy_region",
                            "checkout_ui_mode",
                            "payment_link_format",
                            "payment_source",
                            "login_required",
                            "web_session_available",
                            "link_type",
                            "link_expires_at",
                            "link_expiry_source",
                            "gcash_qr_payload",
                            "gcash_qr_expires_at",
                            "profile_hash",
                            "remote_batch_id",
                            "remote_job_id",
                            "remote_request_id",
                            "generated_at",
                            "country",
                            "billing_country",
                            "currency",
                            "provider_redirect_url",
                            "long_url",
                            "cs_id",
                        )
                        if key in data and data[key] is not None and data[key] != ""
                    }
                )
                history.updated_at = datetime.now(timezone.utc)
                session.add(history)
        data["auth_capture_required"] = True
        result["data"] = data

    if platform == "chatgpt" and action_id == "upload_cpa":
        from services.chatgpt_sync import update_account_model_cpa_sync

        sync_msg = result.get("data") or result.get("error") or ""
        update_account_model_cpa_sync(
            acc_model,
            bool(result.get("ok")),
            str(sync_msg),
            session=session,
            commit=False,
        )
    if result.get("ok") and result.get("data", {}) and isinstance(result["data"], dict):
        data = result["data"]
        tracked_keys = {"access_token", "accessToken", "refreshToken", "clientId", "clientSecret", "webAccessToken"}
        if tracked_keys.intersection(data.keys()):
            extra = acc_model.get_extra()
            extra.update(data)
            acc_model.set_extra(extra)
            if data.get("access_token"):
                acc_model.token = data["access_token"]
            elif data.get("accessToken"):
                acc_model.token = data["accessToken"]
            from datetime import datetime, timezone

            acc_model.updated_at = datetime.now(timezone.utc)
            session.add(acc_model)
    if platform == "chatgpt" and result.get("account_auth_material_changed") is True:
        from services.chatgpt_core.auth_lifecycle import apply_material_capture

        extra = acc_model.get_extra()
        apply_material_capture(
            session,
            acc_model,
            extra=extra,
            access_token_expires_at=result.get("account_access_token_expires_at") or "",
            access_token_expiry_source=str(result.get("account_access_token_expiry_source") or ""),
            web_session_expires_at=result.get("account_web_session_expires_at") or "",
            operation=str(result.get("account_auth_material_operation") or action_id or "auth_material_change")[:64],
        )
        acc_model.set_extra(extra)
        session.add(acc_model)
        if int(acc_model.id or 0) > 0:
            upsert_account_list_state_for_account_ids(session, [int(acc_model.id)], commit=False)


def _action_should_auto_refresh_local_status(action_id: str, result: dict[str, Any], acc_model: AccountModel) -> bool:
    if not bool(result.get("ok")):
        return False
    if str(action_id or "") == "probe_local_status":
        # This action already persists through the synchronous by-id refresh.
        return False
    if str(action_id or "") in _LOCAL_STATUS_AUTH_ACTION_IDS:
        return True

    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    if _LOCAL_STATUS_AUTH_PATCH_KEYS.intersection(data.keys()):
        return True

    patch = result.get("account_extra_patch") if isinstance(result.get("account_extra_patch"), dict) else {}
    if _LOCAL_STATUS_AUTH_PATCH_KEYS.intersection(patch.keys()):
        return True

    return False


def _action_local_status_refresh_ids(action_id: str, result: dict[str, Any], acc_model: AccountModel) -> list[int]:
    """Return all ChatGPT account IDs whose local status should be refreshed after an action commit."""
    if not bool(result.get("ok")):
        return []
    ids: list[int] = []
    if _action_should_auto_refresh_local_status(action_id, result, acc_model):
        try:
            value = int(acc_model.id or 0)
        except Exception:
            value = 0
        if value > 0 and value not in ids:
            ids.append(value)
    return ids


def _execute_chatgpt_resume_subscription_auth(
    acc_model: AccountModel,
    *,
    allow_phone_verification: bool | None = None,
    log_fn=None,
    shared_phone_service=None,
    stop_checker=None,
    retry_delays_seconds=None,
    proxy_url: str | None = None,
    phone_sms_probe_only: bool = False,
) -> dict[str, Any]:
    from services.chatgpt_core.subscription_auth_capture import capture_subscription_auth_for_account

    action_logs: list[str] = []
    resolved_allow_phone_verification = _resolve_resume_auth_allow_phone_verification(allow_phone_verification)

    def _collect_log(message: str) -> None:
        if callable(stop_checker):
            stop_checker()
        text = str(message or "").strip()
        if text:
            action_logs.append(text)
            if callable(log_fn):
                log_fn(text)

    _collect_log(f"[补抓] 开始处理账号：{acc_model.email}")
    capture_kwargs = {
        "allow_phone_verification": resolved_allow_phone_verification,
        "log_fn": _collect_log,
        "phone_sms_probe_only": bool(phone_sms_probe_only),
    }
    if proxy_url:
        capture_kwargs["proxy_url"] = str(proxy_url or "")
    if shared_phone_service is not None:
        capture_kwargs["shared_phone_service"] = shared_phone_service
    if callable(stop_checker):
        capture_kwargs["stop_checker"] = stop_checker
    if retry_delays_seconds is not None:
        capture_kwargs["retry_delays_seconds"] = retry_delays_seconds
    result = capture_subscription_auth_for_account(int(acc_model.id or 0), **capture_kwargs)
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    logs = list(data.get("logs") or []) if isinstance(data.get("logs"), list) else []
    if logs:
        # capture_subscription_auth_for_account 已通过 log_fn 实时输出；这里去重后保证返回体也包含完整日志。
        seen = set()
        merged_logs = []
        for line in action_logs + [str(item) for item in logs]:
            if line in seen:
                continue
            seen.add(line)
            merged_logs.append(line)
        data["logs"] = merged_logs
        result["data"] = data
    return result


def _apply_chatgpt_resume_auth_result(acc_model: AccountModel, result: dict[str, Any], session: Session) -> None:
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    auth_capture = data.get("auth_capture") if isinstance(data.get("auth_capture"), dict) else {}
    extra = acc_model.get_extra()
    _merge_extra_patch(
        extra,
        {
            "chatgpt_subscription_auth_result": auth_capture,
            "chatgpt_last_auth_capture": auth_capture,
        },
    )
    extra["chatgpt_capabilities"] = classify_chatgpt_capabilities(acc_model, local_probe=extra.get("chatgpt_local"))
    acc_model.set_extra(extra)
    from datetime import datetime, timezone

    acc_model.updated_at = datetime.now(timezone.utc)
    session.add(acc_model)


def _apply_chatgpt_invalid_recheck_result(acc_model: AccountModel, result: dict[str, Any], session: Session) -> None:
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    recheck = data.get("invalid_recheck") if isinstance(data.get("invalid_recheck"), dict) else {}
    if not recheck:
        return
    extra = acc_model.get_extra()
    extra["chatgpt_invalid_recheck"] = recheck
    local_probe = extra.get("chatgpt_local") if isinstance(extra.get("chatgpt_local"), dict) else None
    if str(recheck.get("status") or "") == "recovered_access_token":
        local_probe = {}
    extra["chatgpt_capabilities"] = classify_chatgpt_capabilities(acc_model, local_probe=local_probe)
    acc_model.set_extra(extra)
    from datetime import datetime, timezone

    acc_model.updated_at = datetime.now(timezone.utc)
    session.add(acc_model)
    from services.account_filters import upsert_account_list_state_for_account_ids

    upsert_account_list_state_for_account_ids(session, [acc_model.id], commit=False)


def _execute_chatgpt_resume_subscription_auth_action(
    acc_model: AccountModel,
    session: Session,
    params: dict | None = None,
) -> dict[str, Any]:
    params = params or {}
    allow_phone_verification = (
        params.get("allow_phone_verification")
        if "allow_phone_verification" in params
        else None
    )
    result = _execute_chatgpt_resume_subscription_auth(
        acc_model,
        allow_phone_verification=allow_phone_verification,
        proxy_url=str(params.get("proxy_url") or params.get("proxy") or "") or None,
    )
    session.refresh(acc_model)
    _apply_chatgpt_resume_auth_result(acc_model, result, session)
    return result


def _execute_chatgpt_invalid_recheck(
    acc_model: AccountModel,
    *,
    log_fn=None,
    stop_checker=None,
    task_id: str = "",
    task_control=None,
    attempt_id: int | None = None,
    proxy_url: str | None = None,
) -> dict[str, Any]:
    return recheck_invalid_chatgpt_account(
        int(acc_model.id or 0),
        log_fn=log_fn,
        stop_checker=stop_checker,
        task_id=task_id,
        task_control=task_control,
        attempt_id=attempt_id,
        proxy_url=proxy_url,
    )


def _execute_chatgpt_invalid_recheck_action(
    acc_model: AccountModel,
    session: Session,
    params: dict | None = None,
) -> dict[str, Any]:
    result = _execute_chatgpt_invalid_recheck(
        acc_model,
        task_id=str((params or {}).get("task_id") or ""),
        proxy_url=str((params or {}).get("proxy_url") or (params or {}).get("proxy") or "") or None,
    )
    session.refresh(acc_model)
    _apply_chatgpt_invalid_recheck_result(acc_model, result, session)
    return result


def _execute_chatgpt_probe_local_status_action(
    instance: ChatGPTPlatform,
    acc_model: AccountModel,
    session: Session,
    params: dict | None = None,
) -> dict[str, Any]:
    try:
        account_id = int(acc_model.id or 0)
    except (TypeError, ValueError):
        account_id = 0
    if account_id <= 0:
        raise ValueError("本地状态刷新账号 ID 无效")

    prepared_account = build_chatgpt_local_status_probe_account(acc_model)
    candidate_state: dict[str, Any] = {}
    # The request/batch Session loaded this ORM row and therefore owns a checked
    # out connection. End that transaction before the by-id helper starts its
    # short-read -> connection-free probe -> short-write lifecycle.
    session.commit()
    sync_result = sync_chatgpt_account_local_status_by_id(
        account_id,
        prepared_account=prepared_account,
        probe_runner=lambda probe_account: instance.probe_local_status_with_candidates(
            probe_account,
            dict(params or {}),
            manage_local_status_slots=False,
            candidate_state=candidate_state,
        ),
    )
    session.refresh(acc_model)
    return instance.build_local_status_probe_action_result(sync_result.get("probe") or {})


def _execute_platform_action(
    instance: Any,
    platform: str,
    acc_model: AccountModel,
    action_id: str,
    params: dict,
    session: Session,
) -> dict[str, Any]:
    if platform == "chatgpt" and action_id == "probe_local_status":
        return _execute_chatgpt_probe_local_status_action(instance, acc_model, session, params)

    if platform == "chatgpt" and action_id == "resume_subscription_auth":
        return _execute_chatgpt_resume_subscription_auth_action(acc_model, session, params)

    if platform == "chatgpt" and action_id == "invalid_recheck":
        return _execute_chatgpt_invalid_recheck_action(acc_model, session, params)

    if platform == "chatgpt" and action_id == "upload_sub2api":
        outcome = backfill_chatgpt_account_to_sub2api(
            acc_model,
            session=session,
            commit=False,
            api_url=str(params.get("api_url") or "").strip() or None,
            api_key=str(params.get("api_key") or "").strip() or None,
            group_ids=params.get("group_ids"),
        )
        result = {
            "ok": bool(outcome.get("ok")),
            "data": {
                "message": str(outcome.get("message") or ""),
                "results": outcome.get("results") or [],
            },
            "error": "" if outcome.get("ok") else str(outcome.get("message") or ""),
        }
        _apply_action_result(platform, action_id, acc_model, result, session)
        return result
    if platform == "chatgpt" and action_id == "upload_oaipay":
        category_id = params.get("category_id")
        outcome = backfill_chatgpt_account_to_oaipay(
            acc_model,
            session=session,
            commit=False,
            category_id=category_id,
            category_mode=str(params.get("category_mode") or "auto"),
            fallback_category_id=params.get("fallback_category_id"),
        )
        result = {
            "ok": bool(outcome.get("ok")),
            "data": {
                "message": str(outcome.get("message") or ""),
                "results": outcome.get("results") or [],
            },
            "error": "" if outcome.get("ok") else str(outcome.get("message") or ""),
        }
        _apply_action_result(platform, action_id, acc_model, result, session)
        return result

    account = _to_platform_account(acc_model)
    result = instance.execute_action(action_id, account, params)
    _apply_action_result(platform, action_id, acc_model, result, session)
    return result


def _resolve_batch_accounts(platform: str, body: BatchActionRequest, session: Session) -> tuple[list[AccountModel], list[int]]:
    if body.account_ids:
        account_ids = []
        seen = set()
        for raw in body.account_ids:
            value = int(raw)
            if value <= 0 or value in seen:
                continue
            seen.add(value)
            account_ids.append(value)

        if not account_ids:
            raise HTTPException(400, "账号 ID 列表不能为空")
        if len(account_ids) > 1000:
            raise HTTPException(400, "单次最多处理 1000 个账号")

        reconcile_rate_limited_accounts(session, platform=platform, account_ids=account_ids)
        rows = session.exec(
            select(AccountModel)
            .where(AccountModel.platform == platform)
            .where(AccountModel.id.in_(account_ids))
        ).all()
        row_map = {row.id: row for row in rows}
        ordered_rows = [row_map[account_id] for account_id in account_ids if account_id in row_map]
        missing_ids = [account_id for account_id in account_ids if account_id not in row_map]
        return ordered_rows, missing_ids

    if not body.all_filtered:
        raise HTTPException(400, "请提供 account_ids，或指定 all_filtered=true")

    reconcile_rate_limited_accounts(session, platform=platform)
    try:
        resolution = resolve_filtered_accounts(
            session,
            platform=platform,
            filter_source=body,
            verify_expected_total=True,
        )
    except AccountFilterScopeChangedError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    rows = list(resolution.rows)
    if len(rows) > 1000:
        raise HTTPException(400, "单次最多处理 1000 个账号")
    return rows, []


def _result_message(result: dict[str, Any]) -> str:
    data = result.get("data")
    if isinstance(data, dict):
        for key in ("message", "detail", "url", "checkout_url", "cashier_url"):
            value = str(data.get(key) or "").strip()
            if value:
                return value
        return json.dumps(data, ensure_ascii=False)
    if str(data or "").strip():
        return str(data)
    return str(result.get("error") or "").strip()


def _execute_batch_cliproxy_sync(accounts: list[AccountModel], session: Session) -> dict[str, Any]:
    from services.cliproxyapi_sync import sync_chatgpt_cliproxyapi_status_batch

    class SyncAccount:
        def __init__(self, model: AccountModel):
            extra = model.get_extra()
            self.id = model.id
            self.email = model.email
            self.user_id = model.user_id
            self.token = model.token
            self.extra = extra
            self.access_token = extra.get("access_token") or model.token
            self.refresh_token = extra.get("refresh_token", "")
            self.id_token = extra.get("id_token", "")
            self.session_token = extra.get("session_token", "")
            self.client_id = extra.get("client_id", "app_EMoamEEZ73f0CkXaXp7hrann")
            self.cookies = extra.get("cookies", "")

    sync_accounts = [SyncAccount(model) for model in accounts]
    sync_results = sync_chatgpt_cliproxyapi_status_batch(sync_accounts)

    items = []
    success_count = 0
    failed_count = 0
    for acc_model in accounts:
        sync_result = sync_results.get(int(acc_model.id or 0), {})
        update_account_model_cliproxy_sync(acc_model, sync_result, session=session, commit=False)
        remote_state = str(sync_result.get("remote_state") or "").strip().lower()
        ok = bool(sync_result.get("uploaded")) and remote_state not in {"unreachable", "not_found"}
        if ok:
            success_count += 1
        else:
            failed_count += 1
        summary = (
            f"远端状态={sync_result.get('status') or 'not_found'}, "
            f"探测={sync_result.get('remote_state') or 'not_checked'}"
        )
        items.append(
            {
                "id": acc_model.id,
                "email": acc_model.email,
                "ok": ok,
                "message": f"CLIProxyAPI 状态同步完成：{summary}",
                "status": acc_model.status,
            }
        )
    return {
        "total": len(items),
        "success": success_count,
        "failed": failed_count,
        "items": items,
    }


def _execute_batch_sub2api_sync(accounts: list[AccountModel], session: Session) -> dict[str, Any]:
    items = []
    success_count = 0
    failed_count = 0

    # Finish every remote request before mutating the shared SQLite session.
    # Otherwise the first account update keeps the single-writer lock while
    # later probes wait on the network, which can block unrelated API calls.
    probed_accounts = [
        (acc_model, probe_chatgpt_sub2api_status(acc_model))
        for acc_model in accounts
    ]

    for acc_model, sync_result in probed_accounts:
        # Another task may update auth/subscription evidence while the remote
        # phase is running. Merge into the latest row instead of overwriting it
        # with the ORM snapshot captured before those network requests.
        session.refresh(acc_model)
        update_account_model_sub2api_sync(acc_model, sync_result, session=session, commit=False)
        remote_state = str(sync_result.get("remote_state") or "").strip().lower()
        ok = remote_state in {"exists", "not_found", "cross_workspace_only"}
        if ok:
            success_count += 1
        else:
            failed_count += 1
        summary = (
            f"远端状态={sync_result.get('status') or remote_state or 'unknown'}, "
            f"探测={remote_state or 'not_checked'}"
        )
        items.append(
            {
                "id": acc_model.id,
                "email": acc_model.email,
                "ok": ok,
                "message": f"Sub2API 状态同步完成：{summary}",
                "status": acc_model.status,
            }
        )

    return {
        "total": len(items),
        "success": success_count,
        "failed": failed_count,
        "items": items,
    }


def _execute_batch_oaipay_sync(accounts: list[AccountModel], session: Session) -> dict[str, Any]:
    items = []
    success_count = 0
    failed_count = 0

    # OAIPay can try several remote routes and each one has a bounded timeout.
    # Keep that entire network phase outside the SQLite write transaction.
    probed_accounts = [
        (acc_model, probe_chatgpt_oaipay_status(acc_model))
        for acc_model in accounts
    ]

    for acc_model, sync_result in probed_accounts:
        session.refresh(acc_model)
        update_account_model_oaipay_sync(acc_model, sync_result, session=session, commit=False)
        remote_state = str(sync_result.get("remote_state") or "").strip().lower()
        ok = remote_state in {"exists", "not_found", "cross_workspace_only"}
        if ok:
            success_count += 1
        else:
            failed_count += 1
        summary = (
            f"远端状态={sync_result.get('status') or remote_state or 'unknown'}, "
            f"探测={remote_state or 'not_checked'}"
        )
        items.append(
            {
                "id": acc_model.id,
                "email": acc_model.email,
                "ok": ok,
                "message": f"OAIPay 状态同步完成：{summary}",
                "status": acc_model.status,
            }
        )

    return {
        "total": len(items),
        "success": success_count,
        "failed": failed_count,
        "items": items,
    }


@router.get("/{platform}")
def list_actions(platform: str):
    """获取平台支持的操作列表"""
    if platform != "chatgpt":
        raise HTTPException(404, "平台不存在")
    instance = ChatGPTPlatform(config=RegisterConfig(extra=config_store.get_all()))
    return {"actions": instance.get_platform_actions()}


@router.post("/{platform}/{action_id}/batch")
def execute_batch_action(
    platform: str,
    action_id: str,
    body: BatchActionRequest,
    session: Session = Depends(get_session),
    background_tasks: BackgroundTasks = None,
):
    if platform != "chatgpt":
        raise HTTPException(404, "平台不存在")
    instance = ChatGPTPlatform(config=RegisterConfig(extra=config_store.get_all()))
    if _task_mode_action(instance, action_id) is not None:
        from api.tasks import BatchAccountActionTaskRequest, enqueue_batch_account_action_task

        task_payload = body.model_dump()
        task_payload["action_id"] = action_id
        return enqueue_batch_account_action_task(
            BatchAccountActionTaskRequest(**task_payload),
            background_tasks=background_tasks,
        )

    accounts, missing_ids = _resolve_batch_accounts(platform, body, session)

    if not accounts and not missing_ids:
        return {"total": 0, "success": 0, "failed": 0, "items": []}

    if platform == "chatgpt" and action_id in {"sync_cliproxyapi_status", "sync_sub2api_status", "sync_oaipay_status"}:
        if action_id == "sync_cliproxyapi_status":
            batch_result = _execute_batch_cliproxy_sync(accounts, session)
        elif action_id == "sync_sub2api_status":
            batch_result = _execute_batch_sub2api_sync(accounts, session)
        else:
            batch_result = _execute_batch_oaipay_sync(accounts, session)
        if missing_ids:
            for missing_id in missing_ids:
                batch_result["failed"] += 1
                batch_result["total"] += 1
                batch_result["items"].append(
                    {
                        "id": missing_id,
                        "email": "",
                        "ok": False,
                        "message": "账号不存在",
                        "status": "",
                    }
                )
        session.commit()
        return batch_result

    items = []
    success_count = 0
    failed_count = 0
    local_status_auto_refresh_ids: list[int] = []

    for missing_id in missing_ids:
        failed_count += 1
        items.append(
            {
                "id": missing_id,
                "email": "",
                "ok": False,
                "message": "账号不存在",
                "status": "",
            }
        )

    delay_min = 0.0
    delay_max = 0.0
    if isinstance(body.params, dict):
        try:
            delay_min = max(0.0, float(body.params.get("delay_seconds") or body.params.get("register_delay_seconds") or body.params.get("probe_delay_seconds") or 0.0))
            delay_max = max(0.0, float(body.params.get("delay_max_seconds") or body.params.get("register_delay_max_seconds") or body.params.get("probe_delay_max_seconds") or 0.0))
        except (ValueError, TypeError):
            pass

    next_start_time = 0.0

    for idx, acc_model in enumerate(accounts):
        if (delay_min > 0 or delay_max > 0) and idx > 0:
            now = time.time()
            wait_seconds = max(0.0, next_start_time - now)
            if wait_seconds > 0:
                time.sleep(wait_seconds)
            chosen_delay = random.uniform(delay_min, delay_max) if delay_max > delay_min else delay_min
            next_start_time = time.time() + chosen_delay
        elif (delay_min > 0 or delay_max > 0) and idx == 0:
            chosen_delay = random.uniform(delay_min, delay_max) if delay_max > delay_min else delay_min
            next_start_time = time.time() + chosen_delay

        account_id_snapshot = acc_model.id
        account_email_snapshot = acc_model.email
        account_status_snapshot = acc_model.status
        try:
            result = _execute_platform_action(instance, platform, acc_model, action_id, body.params, session)
            pending_auto_refresh_ids = (
                _action_local_status_refresh_ids(action_id, result, acc_model)
                if platform == "chatgpt"
                else []
            )
            ok = bool(result.get("ok"))
            item = {
                "id": acc_model.id,
                "email": acc_model.email,
                "ok": ok,
                "message": _result_message(result),
                "status": acc_model.status,
            }
            # Batch actions are explicitly reported per account, so there is no
            # all-or-nothing contract to preserve. Commit each local mutation
            # before the next account can perform network I/O or an intentional
            # inter-account delay. Build the response snapshot first because a
            # commit expires ORM fields and reading them afterwards checks the
            # request connection back out of the pool.
            session.commit()
            local_status_auto_refresh_ids.extend(pending_auto_refresh_ids)
            if ok:
                success_count += 1
            else:
                failed_count += 1
            items.append(item)
        except Exception as exc:
            # A flush/commit error leaves SQLAlchemy's Session unusable until
            # rollback. Use the pre-action response snapshot afterwards so no
            # expired ORM attribute implicitly checks a connection back out.
            session.rollback()
            failed_count += 1
            items.append(
                {
                    "id": account_id_snapshot,
                    "email": account_email_snapshot,
                    "ok": False,
                    "message": str(exc),
                    "status": account_status_snapshot,
                }
            )

    for account_id_value in dict.fromkeys(local_status_auto_refresh_ids):
        schedule_chatgpt_local_status_refresh_for_account_id(account_id_value, reason=f"action:{action_id}")
    return {
        "total": len(items),
        "success": success_count,
        "failed": failed_count,
        "items": items,
    }


@router.post("/{platform}/{account_id}/{action_id}")
def execute_action(
    platform: str,
    account_id: int,
    action_id: str,
    body: ActionRequest,
    session: Session = Depends(get_session),
    background_tasks: BackgroundTasks = None,
):
    """执行平台特定操作"""
    acc_model = session.get(AccountModel, account_id)
    if not acc_model or acc_model.platform != platform:
        raise HTTPException(404, "账号不存在")

    if platform != "chatgpt":
        raise HTTPException(404, "平台不存在")
    instance = ChatGPTPlatform(config=RegisterConfig(extra=config_store.get_all()))
    task_action = _task_mode_action(instance, action_id)
    if task_action is not None:
        from api.tasks import BatchAccountActionTaskRequest, enqueue_batch_account_action_task

        batch_config = task_action.get("batch") if isinstance(task_action.get("batch"), dict) else {}
        danger = str(batch_config.get("danger") or "").strip().lower()
        return enqueue_batch_account_action_task(
            BatchAccountActionTaskRequest(
                action_id=action_id,
                scope="single",
                account_ids=[int(account_id)],
                confirmed_total=1 if danger in {"warning", "danger"} else None,
                params=dict(body.params or {}),
            ),
            background_tasks=background_tasks,
        )

    try:
        result = _execute_platform_action(instance, platform, acc_model, action_id, body.params, session)
        local_status_auto_refresh_ids = _action_local_status_refresh_ids(action_id, result, acc_model)
        session.commit()
        for account_id_value in dict.fromkeys(local_status_auto_refresh_ids):
            schedule_chatgpt_local_status_refresh_for_account_id(account_id_value, reason=f"action:{action_id}")
        return result
    except NotImplementedError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        return {"ok": False, "error": str(e)}
