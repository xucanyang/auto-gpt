"""平台操作 API - 通用接口，各平台通过 get_platform_actions/execute_action 实现"""
from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select
from pydantic import BaseModel
import json
import random
import time
from typing import Any
from core.db import AccountModel, get_session
from core.base_platform import RegisterConfig
from core.config_store import config_store
from services.account_filters import account_base_query, filter_account_rows
from services.account_rate_limit_recovery import reconcile_rate_limited_accounts
from services.chatgpt_account_state import apply_chatgpt_status_policy, classify_chatgpt_capabilities, mark_payment_pending
from services.chatgpt_core import ChatGPTPlatform
from services.chatgpt_core.local_status_refresh import schedule_chatgpt_local_status_refresh_for_account_id
from services.chatgpt_core.invalid_account_recheck import recheck_invalid_chatgpt_account
from services.chatgpt_core.payment_link_cache import build_payment_link_cache_payload
from services.chatgpt_sync import update_account_model_cliproxy_sync
from services.sub2api_sync import backfill_chatgpt_account_to_sub2api, probe_chatgpt_sub2api_status, update_account_model_sub2api_sync
from services.oaipay_sync import backfill_chatgpt_account_to_oaipay, probe_chatgpt_oaipay_status, update_account_model_oaipay_sync

router = APIRouter(prefix="/actions", tags=["actions"])

_LOCAL_STATUS_AUTH_ACTION_IDS = {
    "refresh_token",
    "resume_subscription_auth",
    "invalid_recheck",
    "k12_workspace_recapture",
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


class BatchActionRequest(BaseModel):
    account_ids: list[int] = []
    all_filtered: bool = False
    email: str = ""
    status: str = ""
    manually_used: str | None = None
    auth_type: str = ""
    subscription_type: str = ""
    account_validity: str = ""
    sub2api_state: str = ""
    oaipay_state: str = ""
    idea_submit_state: str = ""
    params: dict = {}


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


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    text = str(value or "").strip().lower()
    if not text:
        return None
    if text in {"1", "true", "yes", "on", "used"}:
        return True
    if text in {"0", "false", "no", "off", "unused"}:
        return False
    return None


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
    if platform == "chatgpt":
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        status_reason = ""
        if action_id == "probe_local_status":
            status_reason = apply_chatgpt_status_policy(acc_model, local_probe=data.get("probe"))
        elif action_id == "sync_cliproxyapi_status":
            status_reason = apply_chatgpt_status_policy(acc_model, remote_sync=data.get("sync"))
        if status_reason:
            from datetime import datetime, timezone

            acc_model.updated_at = datetime.now(timezone.utc)
            session.add(acc_model)
    if isinstance(result.get("account_extra_patch"), dict):
        extra = acc_model.get_extra()
        _merge_extra_patch(extra, result["account_extra_patch"])
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
            extra = acc_model.get_extra()
            acc_model.cashier_url = checkout_url
            existing_cache = extra.get("chatgpt_last_payment_link") if isinstance(extra.get("chatgpt_last_payment_link"), dict) else {}
            extra["chatgpt_last_payment_link"] = build_payment_link_cache_payload(
                data,
                source=str(data.get("cache_source") or "payment_link_action"),
                fallback=existing_cache,
            )
            acc_model.set_extra(extra)
            mark_payment_pending(acc_model, reason="payment_link_generated")
            from datetime import datetime, timezone

            acc_model.updated_at = datetime.now(timezone.utc)
            session.add(acc_model)
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
    if platform == "chatgpt" and action_id == "sync_sub2api_status":
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        sync_result = data.get("sync") if isinstance(data.get("sync"), dict) else {}
        update_account_model_sub2api_sync(
            acc_model,
            sync_result,
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


def _action_should_auto_refresh_local_status(action_id: str, result: dict[str, Any], acc_model: AccountModel) -> bool:
    if not bool(result.get("ok")):
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
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    if str(action_id or "") == "k12_workspace_recapture":
        for raw in data.get("changed_account_ids") or []:
            try:
                value = int(raw)
            except Exception:
                continue
            if value > 0 and value not in ids:
                ids.append(value)
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
) -> dict[str, Any]:
    return recheck_invalid_chatgpt_account(
        int(acc_model.id or 0),
        log_fn=log_fn,
        stop_checker=stop_checker,
        task_id=task_id,
        task_control=task_control,
        attempt_id=attempt_id,
    )


def _execute_chatgpt_invalid_recheck_action(
    acc_model: AccountModel,
    session: Session,
    params: dict | None = None,
) -> dict[str, Any]:
    result = _execute_chatgpt_invalid_recheck(
        acc_model,
        task_id=str((params or {}).get("task_id") or ""),
    )
    session.refresh(acc_model)
    _apply_chatgpt_invalid_recheck_result(acc_model, result, session)
    return result


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(float(str(value or "").strip()))
    except Exception:
        parsed = int(default)
    return max(minimum, min(maximum, parsed))


def _chatgpt_k12_recapture_config(params: dict[str, Any] | None = None) -> dict[str, Any]:
    from services.chatgpt_core.k12_recapture import K12_RECAPTURE_CONFIG_KEYS

    params = params or {}
    config = {key: config_store.get(key, "") for key in K12_RECAPTURE_CONFIG_KEYS}
    if params.get("join_timeout_seconds") not in (None, ""):
        config["chatgpt_k12_join_timeout_seconds"] = _bounded_int(
            params.get("join_timeout_seconds"),
            default=60,
            minimum=5,
            maximum=180,
        )
    if params.get("join_retry_count") not in (None, ""):
        config["chatgpt_k12_join_retry_count"] = _bounded_int(
            params.get("join_retry_count"),
            default=2,
            minimum=0,
            maximum=5,
        )
    if params.get("post_join_poll_seconds") not in (None, ""):
        config["chatgpt_k12_post_join_poll_seconds"] = str(params.get("post_join_poll_seconds") or "")
    return config


def _chatgpt_k12_recapture_message(result: dict[str, Any]) -> str:
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    try:
        saved_spaces = int(summary.get("saved_spaces") or len(result.get("artifacts") or []) or 0)
    except Exception:
        saved_spaces = len(result.get("artifacts") or [])
    saved_accounts = len(result.get("saved_accounts") or [])
    changed_ids = len(result.get("changed_account_ids") or [])
    if bool(result.get("ok")):
        return f"K12 / Workspace 重跑完成：导出 {saved_spaces} 个空间，写入 {saved_accounts} 个账号，变更 {changed_ids} 个账号"
    error_text = str(
        summary.get("accounts_check_error")
        or summary.get("error")
        or result.get("error")
        or "未导出有效 workspace"
    ).strip()
    return f"K12 / Workspace 重跑未完全成功：导出 {saved_spaces} 个空间，写入 {saved_accounts} 个账号；{error_text}"


def _execute_chatgpt_k12_workspace_recapture_action(
    acc_model: AccountModel,
    session: Session,
    params: dict | None = None,
    *,
    log_fn=None,
    stop_checker=None,
) -> dict[str, Any]:
    from core.proxy_utils import is_proxy_error_text, resolve_probe_candidate_proxies
    from services.chatgpt_core.k12_recapture import recapture_saved_account_k12_workspaces
    from services.chatgpt_core.k12_workspace import safe_k12_error

    params = params or {}
    config = _chatgpt_k12_recapture_config(params)
    candidates = resolve_probe_candidate_proxies(
        params,
        fallback_proxy=None,
        default_mode="direct",
    )
    last_error = ""
    last_data: dict[str, Any] = {}

    for idx, (proxy_url, proxy_pool, source) in enumerate(candidates):
        proxy_text = str(proxy_url or "").strip()
        source_text = str(source or ("specified" if proxy_text else "direct")).strip()
        if callable(log_fn):
            try:
                log_fn(
                    f"[K12] 使用代理候选 {idx + 1}/{len(candidates)}：source={source_text} mode={params.get('proxy_mode') or 'direct'}",
                    "info",
                )
            except TypeError:
                log_fn(f"[K12] 使用代理候选 {idx + 1}/{len(candidates)}：source={source_text} mode={params.get('proxy_mode') or 'direct'}")
        try:
            if callable(stop_checker):
                stop_checker()
            result = recapture_saved_account_k12_workspaces(
                session=session,
                account=acc_model,
                config=config,
                workspace_ids=params.get("workspace_ids"),
                save_all_spaces=params.get("save_all_spaces") is not False,
                strict_join=_to_bool(params.get("strict_join"), default=False),
                proxy=proxy_text,
                log_fn=log_fn,
                stop_checker=stop_checker,
            )
            if callable(stop_checker):
                stop_checker()
            data = dict(result)
            data["proxy_source"] = source_text
            data["proxy_used"] = bool(proxy_text)
            data["message"] = _chatgpt_k12_recapture_message(data)
            ok = bool(data.get("ok"))
            error_text = "" if ok else str(
                (data.get("summary") if isinstance(data.get("summary"), dict) else {}).get("accounts_check_error")
                or (data.get("summary") if isinstance(data.get("summary"), dict) else {}).get("error")
                or data.get("message")
                or "K12 / Workspace 重跑未产生有效空间"
            )
            if ok and proxy_pool is not None and proxy_text:
                try:
                    proxy_pool.report_success(proxy_text)
                except Exception:
                    pass
            if not ok and proxy_pool is not None and proxy_text and is_proxy_error_text(error_text):
                try:
                    proxy_pool.report_fail(proxy_text)
                except Exception:
                    pass
            if not ok and idx < len(candidates) - 1 and is_proxy_error_text(error_text):
                last_error = safe_k12_error(error_text, 300)
                last_data = data
                continue
            return {
                "ok": ok,
                "data": data,
                "error": "" if ok else safe_k12_error(error_text, 300),
            }
        except ValueError as exc:
            error_text = safe_k12_error(exc, 300)
            return {
                "ok": False,
                "data": {
                    "message": f"K12 / Workspace 重跑失败：{error_text}",
                    "proxy_source": source_text,
                    "proxy_used": bool(proxy_text),
                },
                "error": error_text,
            }
        except Exception as exc:
            if exc.__class__.__name__ in {"StopTaskRequested", "SkipCurrentAttemptRequested"}:
                raise
            error_text = safe_k12_error(exc, 300)
            if proxy_pool is not None and proxy_text and is_proxy_error_text(error_text):
                try:
                    proxy_pool.report_fail(proxy_text)
                except Exception:
                    pass
            if idx < len(candidates) - 1 and is_proxy_error_text(error_text):
                last_error = error_text
                last_data = {
                    "message": f"K12 / Workspace 重跑失败，已尝试切换代理：{error_text}",
                    "proxy_source": source_text,
                    "proxy_used": bool(proxy_text),
                }
                continue
            return {
                "ok": False,
                "data": {
                    "message": f"K12 / Workspace 重跑失败：{error_text}",
                    "proxy_source": source_text,
                    "proxy_used": bool(proxy_text),
                },
                "error": error_text,
            }

    return {
        "ok": False,
        "data": last_data or {"message": "K12 / Workspace 重跑失败"},
        "error": last_error or "K12 / Workspace 重跑失败",
    }


def _execute_platform_action(
    instance: Any,
    platform: str,
    acc_model: AccountModel,
    action_id: str,
    params: dict,
    session: Session,
) -> dict[str, Any]:
    if platform == "chatgpt" and action_id == "resume_subscription_auth":
        return _execute_chatgpt_resume_subscription_auth_action(acc_model, session, params)

    if platform == "chatgpt" and action_id == "invalid_recheck":
        return _execute_chatgpt_invalid_recheck_action(acc_model, session, params)

    if platform == "chatgpt" and action_id == "k12_workspace_recapture":
        return _execute_chatgpt_k12_workspace_recapture_action(acc_model, session, params)

    if platform == "chatgpt" and action_id == "upload_sub2api":
        outcome = backfill_chatgpt_account_to_sub2api(acc_model, session=session, commit=False)
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
    query = account_base_query(platform=platform, status=body.status, email=body.email)
    rows = filter_account_rows(
        session.exec(query).all(),
        manually_used=_optional_bool(body.manually_used),
        auth_type=body.auth_type,
        subscription_type=body.subscription_type,
        account_validity_filter=body.account_validity,
        sub2api_state=body.sub2api_state,
        oaipay_state=body.oaipay_state,
        idea_submit_state=body.idea_submit_state,
    )
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

    for acc_model in accounts:
        sync_result = probe_chatgpt_sub2api_status(acc_model)
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

    from services.oaipay_sync import probe_chatgpt_oaipay_status, update_account_model_oaipay_sync

    for acc_model in accounts:
        sync_result = probe_chatgpt_oaipay_status(acc_model)
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
):
    if platform != "chatgpt":
        raise HTTPException(404, "平台不存在")
    instance = ChatGPTPlatform(config=RegisterConfig(extra=config_store.get_all()))
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

        try:
            result = _execute_platform_action(instance, platform, acc_model, action_id, body.params, session)
            if platform == "chatgpt":
                local_status_auto_refresh_ids.extend(_action_local_status_refresh_ids(action_id, result, acc_model))
            ok = bool(result.get("ok"))
            if ok:
                success_count += 1
            else:
                failed_count += 1
            items.append(
                {
                    "id": acc_model.id,
                    "email": acc_model.email,
                    "ok": ok,
                    "message": _result_message(result),
                    "status": acc_model.status,
                }
            )
        except Exception as exc:
            failed_count += 1
            items.append(
                {
                    "id": acc_model.id,
                    "email": acc_model.email,
                    "ok": False,
                    "message": str(exc),
                    "status": acc_model.status,
                }
            )

    session.commit()
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
):
    """执行平台特定操作"""
    acc_model = session.get(AccountModel, account_id)
    if not acc_model or acc_model.platform != platform:
        raise HTTPException(404, "账号不存在")

    if platform != "chatgpt":
        raise HTTPException(404, "平台不存在")
    instance = ChatGPTPlatform(config=RegisterConfig(extra=config_store.get_all()))

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
