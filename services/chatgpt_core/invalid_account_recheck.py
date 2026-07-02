from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

from sqlmodel import Session

from core import db as core_db
from core.db import AccountModel
from core.config_store import config_store
from core.task_runtime import TaskInterruption
from services.chatgpt_core.task_logging import redact_log_text, sanitize_error_message
from services.chatgpt_account_state import (
    apply_auth_capture_status,
    classify_chatgpt_capabilities,
    has_payment_pending_marker,
    is_account_deactivated_message,
)
from .local_status_refresh import schedule_chatgpt_local_status_refresh_for_account_id
from .pending_business_invites import RestoredEmailService, _mailbox_state_from_account
from .refresh_token_registration_engine import EmailServiceAdapter, RefreshTokenRegistrationEngine
from .utils import decode_jwt_payload, generate_random_birthday, generate_random_name


DEFAULT_INVALID_RECHECK_RETRY_DELAYS_SECONDS = (5, 10)
TEMPORARY_LOGIN_ERROR_MARKERS = (
    "429",
    "rate limit",
    "rate_limited",
    "timeout",
    "timed out",
    "connection reset",
    "temporarily",
    "temporary",
    "navigation timeout",
    "bootstrap 失败",
    "tls",
    "ssl",
    "curl: (35)",
)
LOGIN_BLOCKED_MARKERS = (
    "add_phone",
    "add-phone",
    "phone verification",
    "手机号",
    "workspace/select",
    "no_valid_organizations",
    "no valid organizations",
)
PASSWORD_INVALID_MARKERS = (
    "invalid credentials",
    "login failed",
    "incorrect password",
    "wrong password",
    "密码验证失败",
    "密码不正确",
    "密码错误",
)
AT_ONLY_CLEAR_EXTRA_KEYS = (
    "refresh_token",
    "id_token",
    "session_token",
    "workspace_id",
    "organization_id",
    "chatgpt_workspace_variants",
    "chatgpt_workspace_scope",
    "chatgpt_workspace_label",
    "chatgpt_workspace_display_name",
    "chatgpt_workspace_variant_key",
    "chatgpt_has_refresh_token_solution",
    "partial_auth",
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_retry_delays(value: Any) -> list[int]:
    if isinstance(value, (list, tuple)):
        candidates = value
    elif value not in (None, ""):
        candidates = re.split(r"[,\s]+", str(value))
    else:
        candidates = DEFAULT_INVALID_RECHECK_RETRY_DELAYS_SECONDS

    delays: list[int] = []
    for item in candidates:
        try:
            seconds = int(float(str(item).strip()))
        except Exception:
            continue
        if seconds < 0:
            continue
        delays.append(min(seconds, 120))
    return delays or list(DEFAULT_INVALID_RECHECK_RETRY_DELAYS_SECONDS)


def _retry_delays_from_config(config: dict[str, Any], explicit: Sequence[int] | None = None) -> list[int]:
    if explicit is not None:
        return _normalize_retry_delays(list(explicit))
    return _normalize_retry_delays(
        config.get("chatgpt_invalid_recheck_retry_delays_seconds")
        or config.get("chatgpt_resume_auth_retry_delays_seconds")
    )




def _to_bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "y", "是", "开启", "允许", "启用"}:
        return True
    if text in {"0", "false", "no", "off", "n", "否", "关闭", "禁止", "禁用"}:
        return False
    return default


def _config_bool(config: dict[str, Any], key: str, *, default: bool = False) -> bool:
    if key not in config or config.get(key) in (None, ""):
        return default
    return _to_bool(config.get(key), default=default)


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    lowered = str(text or "").lower()
    return any(marker.lower() in lowered for marker in markers)


def _classify_recheck_error(error_text: str) -> tuple[str, bool, bool | None]:
    text = str(error_text or "").strip()
    if not text:
        return "unknown_error", False, None
    if is_account_deactivated_message("", text):
        return "account_deactivated", False, False
    if _contains_any(text, ("otp_rate_limited", "too many tries", "too many attempts", "please wait a few minutes")):
        return "otp_rate_limited", True, True
    if _contains_any(text, LOGIN_BLOCKED_MARKERS):
        return "login_blocked", True, None
    if _contains_any(text, PASSWORD_INVALID_MARKERS):
        return "password_invalid", False, False
    if _contains_any(text, TEMPORARY_LOGIN_ERROR_MARKERS):
        return "network_failed", True, None
    return "unknown_error", False, None


def _message_for_status(status: str, raw_error: str = "") -> str:
    if status == "recovered_access_token":
        return "失效测活成功，已重新保存 access_token"
    if status == "account_deactivated":
        return "账号已被删除或停用，保持失效状态"
    if status == "password_invalid":
        return "登录失败，密码可能不正确"
    if status == "login_blocked":
        return "登录被额外验证或工作空间选择阻断，保持原状态"
    if status == "network_failed":
        return "网络或限流导致登录失败，保持原状态"
    if status == "otp_rate_limited":
        return "OTP 校验次数过多，当前邮箱已进入冷却，稍后重试"
    return str(raw_error or "失效测活失败，保持原状态").strip()


def _timeline_log(
    log_fn: Callable[[str], None] | None,
    message: str,
    *,
    level: str = "info",
) -> None:
    if not callable(log_fn):
        return
    try:
        log_fn(message)
    except TypeError:
        try:
            log_fn(message, level)
        except TypeError:
            log_fn(message)


def _account_id_from_access_token(access_token: str) -> str:
    payload = decode_jwt_payload(access_token)
    auth_claims = payload.get("https://api.openai.com/auth") or {}
    return str(auth_claims.get("chatgpt_account_id") or payload.get("sub") or "").strip()


def _build_recheck_payload(
    *,
    status: str,
    email: str,
    raw_error: str = "",
    attempts: int = 1,
    task_id: str = "",
    recoverable: bool | None = None,
    account_id: str = "",
    has_access_token: bool = False,
    exported_mailbox_state: dict[str, Any] | None = None,
    allow_add_phone_verification: bool | None = None,
    allow_existing_phone_verification: bool | None = None,
) -> dict[str, Any]:
    payload = {
        "status": status,
        "recoverable": recoverable,
        "email": str(email or ""),
        "message": _message_for_status(status, raw_error),
        "raw_error": str(raw_error or ""),
        "source": "invalid_account_recheck",
        "task_id": str(task_id or ""),
        "attempts": int(attempts or 0),
        "checked_at": _utcnow().isoformat(),
        "account_id": str(account_id or ""),
        "has_access_token": bool(has_access_token),
    }
    if allow_add_phone_verification is not None:
        payload["allow_add_phone_verification"] = bool(allow_add_phone_verification)
    if allow_existing_phone_verification is not None:
        payload["allow_existing_phone_verification"] = bool(allow_existing_phone_verification)
    if exported_mailbox_state:
        payload["mailbox_state"] = dict(exported_mailbox_state)
    return payload


def _build_revival_marker(
    *,
    source: str,
    mode: str,
    email: str,
    task_id: str = "",
    account_row_id: int = 0,
    has_access_token: bool = False,
    has_refresh_token: bool = False,
    auth_level: str = "",
) -> dict[str, Any]:
    return {
        "source": str(source or "").strip(),
        "mode": str(mode or "").strip(),
        "email": str(email or "").strip(),
        "task_id": str(task_id or "").strip(),
        "account_row_id": int(account_row_id or 0),
        "has_access_token": bool(has_access_token),
        "has_refresh_token": bool(has_refresh_token),
        "auth_level": str(auth_level or "").strip(),
        "revived_at": _utcnow().isoformat(),
    }


def _append_revival_marker(extra: dict[str, Any], marker: dict[str, Any]) -> dict[str, Any]:
    payload = dict(marker or {})
    if not payload:
        return extra
    history = extra.get("chatgpt_revival_history") if isinstance(extra.get("chatgpt_revival_history"), list) else []
    history = [dict(item) for item in history if isinstance(item, dict)][-4:]
    history.append(payload)
    extra["chatgpt_last_revival"] = payload
    extra["chatgpt_revival_history"] = history
    return extra


def _persist_recheck_success(
    account_id: int,
    *,
    email: str,
    tokens: dict[str, Any],
    oauth_client: Any,
    engine_instance: Any,
    attempts: int,
    task_id: str = "",
    exported_mailbox_state: dict[str, Any] | None = None,
    allow_add_phone_verification: bool | None = None,
    allow_existing_phone_verification: bool | None = None,
) -> dict[str, Any]:
    access_token = str(tokens.get("access_token") or "").strip()
    refresh_token = str(tokens.get("refresh_token") or "").strip()
    if not access_token:
        raise ValueError("OAuth 登录成功但未获取 access_token")
    token_account_id = _account_id_from_access_token(access_token)
    if not token_account_id:
        extract_account_info = getattr(engine_instance, "_extract_account_info", None)
        if callable(extract_account_info):
            try:
                token_account_id = str((extract_account_info(tokens) or {}).get("account_id") or "").strip()
            except Exception:
                token_account_id = ""

    with Session(core_db.engine) as session:
        account = session.get(AccountModel, int(account_id or 0))
        if account is None or account.platform != "chatgpt":
            raise ValueError("ChatGPT 账号不存在")

        extra = account.get_extra()
        for key in AT_ONLY_CLEAR_EXTRA_KEYS:
            extra.pop(key, None)
        extra.pop("chatgpt_local", None)
        extra["access_token"] = access_token
        extra["auth_level"] = "access_token_only"
        extra["chatgpt_registration_mode"] = "access_token_only"
        extra["chatgpt_token_source"] = "invalid_account_recheck"
        if exported_mailbox_state:
            extra["chatgpt_mailbox_state"] = dict(exported_mailbox_state)

        revival_marker = _build_revival_marker(
            source="invalid_account_recheck",
            mode="revive_existing",
            email=email,
            task_id=task_id,
            account_row_id=int(account.id or 0),
            has_access_token=True,
            has_refresh_token=bool(refresh_token),
            auth_level="access_token_only",
        )
        recheck_payload = _build_recheck_payload(
            status="recovered_access_token",
            email=email,
            attempts=attempts,
            task_id=task_id,
            recoverable=True,
            account_id=token_account_id or str(account.user_id or ""),
            has_access_token=True,
            exported_mailbox_state=exported_mailbox_state,
            allow_add_phone_verification=allow_add_phone_verification,
            allow_existing_phone_verification=allow_existing_phone_verification,
        )
        recheck_payload["revival_marker"] = dict(revival_marker)
        extra["chatgpt_invalid_recheck"] = recheck_payload
        _append_revival_marker(extra, revival_marker)

        account.token = access_token
        account.set_extra(extra)
        apply_auth_capture_status(
            account,
            "pending_payment" if has_payment_pending_marker(account) else "registered",
        )
        extra = account.get_extra()
        extra["chatgpt_capabilities"] = classify_chatgpt_capabilities(account, local_probe={})
        account.set_extra(extra)
        account.updated_at = _utcnow()
        session.add(account)
        session.commit()
        session.refresh(account)
        schedule_chatgpt_local_status_refresh_for_account_id(account.id, reason="invalid_account_recheck:recovered", delay_seconds=2.0)
        return {
            "status": str(account.status or ""),
            "user_id": str(account.user_id or ""),
            "token_saved": bool(account.token),
            "recheck": recheck_payload,
        }


def _persist_recheck_failure(
    account_id: int,
    *,
    email: str,
    status: str,
    raw_error: str,
    attempts: int,
    task_id: str = "",
    recoverable: bool | None = None,
    exported_mailbox_state: dict[str, Any] | None = None,
    allow_add_phone_verification: bool | None = None,
    allow_existing_phone_verification: bool | None = None,
) -> dict[str, Any]:
    with Session(core_db.engine) as session:
        account = session.get(AccountModel, int(account_id or 0))
        if account is None or account.platform != "chatgpt":
            raise ValueError("ChatGPT 账号不存在")
        payload = _build_recheck_payload(
            status=status,
            email=email,
            raw_error=raw_error,
            attempts=attempts,
            task_id=task_id,
            recoverable=recoverable,
            exported_mailbox_state=exported_mailbox_state,
            allow_add_phone_verification=allow_add_phone_verification,
            allow_existing_phone_verification=allow_existing_phone_verification,
        )
        extra = account.get_extra()
        extra["chatgpt_invalid_recheck"] = payload
        if exported_mailbox_state:
            extra["chatgpt_mailbox_state"] = dict(exported_mailbox_state)
        account.status = "invalid"
        extra["chatgpt_capabilities"] = classify_chatgpt_capabilities(account)
        extra["chatgpt_capabilities"]["auth_level"] = "invalid"
        extra["chatgpt_capabilities"]["upload_gate"] = "blocked_auth_invalid"
        account.set_extra(extra)
        account.updated_at = _utcnow()
        session.add(account)
        session.commit()
        session.refresh(account)
        return {
            "status": str(account.status or ""),
            "token_saved": bool(account.token),
            "recheck": payload,
        }


def _capture_access_token_without_refresh_token(
    *,
    email: str,
    password: str,
    exported_mailbox_state: dict[str, Any],
    merged_config: dict[str, Any],
    browser_mode: str,
    log_fn: Callable[[str], None],
    proxy_url: str | None = None,
    login_source: str = "access_token_probe",
    task_control=None,
    attempt_id: int | None = None,
    allow_add_phone_verification: bool = False,
    allow_existing_phone_verification: bool = False,
) -> tuple[dict[str, Any], dict[str, Any], Any]:
    try:
        email_service = RestoredEmailService(
            state=exported_mailbox_state,
            log_fn=log_fn,
            task_control=task_control,
            attempt_id=attempt_id,
        )
    except TypeError as exc:
        # Older tests/fakes and some local adapters may not expose the newer
        # task-control kwargs. Keep the runtime enhancement optional rather than
        # breaking the recheck flow before OAuth starts.
        if "task_control" not in str(exc) and "attempt_id" not in str(exc):
            raise
        email_service = RestoredEmailService(
            state=exported_mailbox_state,
            log_fn=log_fn,
        )
    email_service.create_email()
    email_adapter = EmailServiceAdapter(email_service, email, log_fn)
    engine_instance = RefreshTokenRegistrationEngine(
        email_service=email_service,
        proxy_url=proxy_url or None,
        callback_logger=lambda msg, level="info", *_: log_fn(str(msg)),
        browser_mode=browser_mode,
        extra_config=merged_config,
    )
    engine_instance.email = email
    engine_instance.password = password
    register_client = engine_instance._build_chatgpt_client()
    oauth_client = engine_instance._build_oauth_client()
    first_name, last_name = generate_random_name()
    birthdate = generate_random_birthday()

    tokens = oauth_client.login_and_get_tokens(
        email,
        password,
        device_id=getattr(register_client, "device_id", "") or "",
        user_agent=getattr(register_client, "ua", None),
        sec_ch_ua=getattr(register_client, "sec_ch_ua", None),
        impersonate=getattr(register_client, "impersonate", None),
        browser_fingerprint=getattr(register_client, "fingerprint", None),
        skymail_client=email_adapter,
        prefer_passwordless_login=True,
        allow_phone_verification=bool(
            allow_add_phone_verification or allow_existing_phone_verification
        ),
        allow_add_phone_verification=bool(allow_add_phone_verification),
        allow_existing_phone_verification=bool(allow_existing_phone_verification),
        force_new_browser=True,
        force_chatgpt_entry=False,
        screen_hint="login",
        force_password_login=bool(password),
        complete_about_you_if_needed=True,
        first_name=first_name,
        last_name=last_name,
        birthdate=birthdate,
        login_source=str(login_source or "access_token_probe"),
        stop_after_login=True,
        workspace_scope_preference="free",
        allow_add_phone_session_recovery=False,
    )
    exported_state = email_service.export_state()
    if tokens and str((tokens or {}).get("access_token") or "").strip():
        return dict(tokens or {}), exported_state, engine_instance

    stop_reason = str(getattr(oauth_client, "last_error", "") or "").strip()
    if stop_reason and "登录链路已完成" not in stop_reason:
        raise RuntimeError(stop_reason)

    oauth_session = getattr(oauth_client, "session", None)
    if oauth_session is not None:
        register_client.session = oauth_session
    register_client.last_registration_state = getattr(oauth_client, "last_state", None) or getattr(
        register_client,
        "last_registration_state",
        None,
    )
    session_ok, session_result = register_client.reuse_session_and_get_tokens()
    if not session_ok:
        raise RuntimeError(
            str(session_result or oauth_client.last_error or "无 RT 登录成功，但读取 ChatGPT Session 失败")
        )
    return dict(session_result or {}), exported_state, engine_instance


def _persist_recheck_followup_result(
    account_id: int,
    *,
    followup_ok: bool,
    followup_saved_account_id: int = 0,
    followup_payload: dict[str, Any] | None = None,
    followup_error: str = "",
) -> dict[str, Any]:
    with Session(core_db.engine) as session:
        account = session.get(AccountModel, int(account_id or 0))
        if account is None or account.platform != "chatgpt":
            raise ValueError("ChatGPT 账号不存在")
        extra = account.get_extra()
        recheck_payload = (
            dict(extra.get("chatgpt_invalid_recheck"))
            if isinstance(extra.get("chatgpt_invalid_recheck"), dict)
            else {}
        )
        payload = dict(followup_payload or {})
        capabilities = classify_chatgpt_capabilities(account, local_probe={})
        recheck_payload["followup_auth_source"] = "custom_email_recheck"
        recheck_payload["followup_auth_ok"] = bool(followup_ok)
        recheck_payload["followup_saved_account_id"] = int(followup_saved_account_id or 0)
        recheck_payload["followup_status"] = str(payload.get("status") or "").strip()
        recheck_payload["followup_has_refresh_token"] = bool(payload.get("has_refresh_token"))
        recheck_payload["followup_has_access_token"] = bool(payload.get("has_access_token"))
        recheck_payload["final_auth_level"] = str(capabilities.get("auth_level") or "")
        recheck_payload["final_has_refresh_token"] = bool(capabilities.get("has_refresh_token"))
        if payload:
            recheck_payload["followup_result"] = {
                "status": str(payload.get("status") or "").strip(),
                "message": str(payload.get("message") or "").strip(),
                "saved_account_id": int(payload.get("saved_account_id") or 0),
                "saved": bool(payload.get("saved")),
                "has_access_token": bool(payload.get("has_access_token")),
                "has_refresh_token": bool(payload.get("has_refresh_token")),
            }
        if followup_error:
            recheck_payload["followup_error"] = str(followup_error or "").strip()
        else:
            recheck_payload.pop("followup_error", None)
        extra["chatgpt_invalid_recheck"] = recheck_payload
        account.set_extra(extra)
        account.updated_at = _utcnow()
        session.add(account)
        session.commit()
        session.refresh(account)
        if followup_ok:
            schedule_chatgpt_local_status_refresh_for_account_id(account.id, reason="invalid_account_recheck:followup", delay_seconds=2.0)
        return {
            "status": str(account.status or ""),
            "token_saved": bool(account.token),
            "recheck": recheck_payload,
        }


def recheck_invalid_chatgpt_account(
    account_id: int,
    *,
    retry_delays_seconds: Sequence[int] | None = None,
    log_fn: Callable[[str], None] | None = None,
    stop_checker: Callable[[], None] | None = None,
    task_id: str = "",
    task_control: Any = None,
    attempt_id: int | None = None,
) -> dict[str, Any]:
    action_logs: list[str] = []

    def _check_stop() -> None:
        if callable(stop_checker):
            stop_checker()

    def _log(message: str, level: str = "info") -> None:
        _check_stop()
        text = redact_log_text(str(message or "").strip())
        if not text:
            return
        action_logs.append(text)
        if callable(log_fn):
            try:
                log_fn(text)
            except TypeError:
                log_fn(text, level)

    account_id = int(account_id or 0)
    if account_id <= 0:
        return {
            "ok": False,
            "error": "account_id 无效",
            "data": {
                "message": "account_id 无效",
                "error_code": "invalid_account_id",
                "retryable": False,
                "logs": list(action_logs),
            },
        }

    with Session(core_db.engine) as session:
        account = session.get(AccountModel, account_id)
        if account is None or account.platform != "chatgpt":
            return {
                "ok": False,
                "error": "ChatGPT 账号不存在",
                "data": {
                    "message": "ChatGPT 账号不存在",
                    "error_code": "account_not_found",
                    "retryable": False,
                    "logs": list(action_logs),
                },
            }
        status = str(account.status or "").strip().lower()
        email = str(account.email or "").strip()
        password = str(account.password or "")
        extra = account.get_extra()
        mailbox_state = _mailbox_state_from_account(account, extra=extra)

    if status != "invalid":
        return {
            "ok": False,
            "error": "仅支持 status=invalid 的账号执行失效测活",
            "data": {
                "message": "仅支持 status=invalid 的账号执行失效测活",
                "error_code": "not_invalid_status",
                "retryable": False,
                "status": status,
                "logs": list(action_logs),
            },
        }
    if not email:
        return {
            "ok": False,
            "error": "账号邮箱为空，无法失效测活",
            "data": {
                "message": "账号邮箱为空，无法失效测活",
                "error_code": "missing_email",
                "retryable": False,
                "logs": list(action_logs),
            },
        }
    if not mailbox_state:
        return {
            "ok": False,
            "error": "mailbox_state 缺失，无法自动获取邮箱验证码",
            "data": {
                "message": "mailbox_state 缺失，无法自动获取邮箱验证码",
                "error_code": "missing_mailbox_state",
                "retryable": False,
                "logs": list(action_logs),
            },
        }

    merged_config = config_store.get_all().copy()
    merged_config.update({key: value for key, value in extra.items() if value not in (None, "")})
    merged_config["_current_account_id"] = account_id
    merged_config["_current_account_email"] = email
    merged_config["_current_task_id"] = task_id
    if task_control is not None:
        merged_config["_task_control"] = task_control
        merged_config["_task_attempt_id"] = attempt_id
        merged_config["_manual_phone_otp_enabled"] = True
        merged_config["_manual_phone_otp_timeout_seconds"] = 60
    allow_add_phone_verification = False
    allow_existing_phone_verification = _config_bool(
        merged_config,
        "chatgpt_recheck_allow_existing_phone_verification",
        default=True,
    )
    allow_phone_verification = bool(allow_add_phone_verification or allow_existing_phone_verification)
    browser_mode = str(
        extra.get("browser_mode")
        or merged_config.get("browser_mode")
        or merged_config.get("default_executor")
        or "protocol"
    ).strip().lower() or "protocol"
    retry_delays = _retry_delays_from_config(merged_config, retry_delays_seconds)
    max_attempts = 1 + len(retry_delays)
    exported_mailbox_state = dict(mailbox_state)

    _timeline_log(log_fn, f"[失效测活][{email}] 开始：仅处理 status=invalid")
    _log(
        "[失效测活] 手机验证策略："
        f"add_phone新绑={'允许' if allow_add_phone_verification else '不允许'}，"
        f"已绑手机号二次验证={'允许' if allow_existing_phone_verification else '不允许'}"
    )
    last_error = ""
    last_error_code = "unknown_error"
    retryable = False
    recoverable: bool | None = None
    attempts_executed = 0
    stage1_result: dict[str, Any] | None = None

    for attempt in range(1, max_attempts + 1):
        attempts_executed = attempt
        _check_stop()
        if attempt > 1:
            _log(f"[失效测活] 开始第 {attempt}/{max_attempts} 次无 RT 登录测活")
        try:
            _timeline_log(log_fn, "[失效测活] 阶段 1/2：无 RT 登录测活并抓取 AccessToken")
            session_tokens, exported_mailbox_state, engine_instance = _capture_access_token_without_refresh_token(
                email=email,
                password=password,
                exported_mailbox_state=exported_mailbox_state,
                merged_config=merged_config,
                browser_mode=browser_mode,
                log_fn=_log,
                task_control=task_control,
                attempt_id=attempt_id,
            )
            persist_result = _persist_recheck_success(
                account_id,
                email=email,
                tokens=session_tokens,
                oauth_client=None,
                engine_instance=engine_instance,
                attempts=attempt,
                task_id=task_id,
                exported_mailbox_state=exported_mailbox_state,
                allow_add_phone_verification=allow_add_phone_verification,
                allow_existing_phone_verification=allow_existing_phone_verification,
            )
            _timeline_log(log_fn, "[失效测活] 阶段 1/2 成功：AccessToken 已保存，状态已复活")
            stage1_result = persist_result
            break
        except TaskInterruption:
            raise
        except Exception as exc:
            last_error = sanitize_error_message(exc or "失效测活失败")
            last_error_code, retryable, recoverable = _classify_recheck_error(last_error)
            if attempt >= max_attempts or not retryable:
                break
            delay_seconds = int(retry_delays[attempt - 1])
            if delay_seconds > 0:
                _log(f"[失效测活] {last_error_code}，{delay_seconds}s 后重试")
                time.sleep(delay_seconds)

    if stage1_result is not None:
        from services.chatgpt_core.custom_email_recheck import recheck_custom_chatgpt_email

        followup_payload: dict[str, Any] = {}
        followup_error = ""
        followup_saved_account_id = int(account_id or 0)
        followup_full_auth_ok = False

        _timeline_log(log_fn, "[失效测活] 阶段 2/2：补抓完整 Auth/RT")
        try:
            followup_result = recheck_custom_chatgpt_email(
                email=email,
                password=password,
                save_on_success=True,
                task_id=task_id,
                log_fn=_log,
                stop_checker=stop_checker,
                task_control=task_control,
                attempt_id=attempt_id,
                preferred_account_id=account_id,
                skip_access_token_probe=True,
            )
            followup_data = followup_result.get("data") if isinstance(followup_result.get("data"), dict) else {}
            followup_payload = (
                dict(followup_data.get("custom_email_recheck"))
                if isinstance(followup_data.get("custom_email_recheck"), dict)
                else {}
            )
            followup_saved_account_id = int(
                followup_data.get("saved_account_id")
                or followup_payload.get("saved_account_id")
                or account_id
                or 0
            )
            followup_error = sanitize_error_message(
                followup_result.get("error")
                or followup_data.get("message")
                or followup_payload.get("message")
                or ""
            ).strip()
            followup_full_auth_ok = bool(followup_result.get("ok")) and bool(followup_payload.get("has_refresh_token"))
            if followup_full_auth_ok:
                _timeline_log(log_fn, "[失效测活] 结果：复活成功，已补全完整 Auth/RT")
            else:
                if bool(followup_result.get("ok")):
                    followup_error = followup_error or "第二阶段完成登录探测，但未获取 refresh_token"
                _timeline_log(log_fn, f"[失效测活] 阶段 2/2 失败：{followup_error or '未获取 refresh_token'}；保留第一阶段复活结果")
        except TaskInterruption:
            raise
        except Exception as exc:
            followup_error = sanitize_error_message(exc or "第二阶段补抓 Auth 异常")
            _timeline_log(log_fn, f"[失效测活] 阶段 2/2 异常：{followup_error}；保留第一阶段已保存的 access_token")

        final_persist = _persist_recheck_followup_result(
            account_id,
            followup_ok=followup_full_auth_ok,
            followup_saved_account_id=followup_saved_account_id,
            followup_payload=followup_payload,
            followup_error=followup_error,
        )
        if followup_full_auth_ok:
            message = "失效测活成功，已复活原账号并补全完整 Auth"
        else:
            message = "失效测活成功，已保存 access_token；完整 Auth 未补全"
            _timeline_log(log_fn, "[失效测活] 结果：复活成功，auth=access_token_only，待补抓 Auth")
        return {
            "ok": True,
            "data": {
                "message": message,
                "status": final_persist.get("status"),
                "token_saved": bool(final_persist.get("token_saved")),
                "followup_auth_ok": bool(followup_full_auth_ok),
                "invalid_recheck": final_persist.get("recheck") or {},
                "logs": list(action_logs),
            },
            "error": "",
        }

    persist_result = _persist_recheck_failure(
        account_id,
        email=email,
        status=last_error_code,
        raw_error=sanitize_error_message(last_error),
        attempts=max(1, attempts_executed),
        task_id=task_id,
        recoverable=recoverable,
        exported_mailbox_state=exported_mailbox_state,
        allow_add_phone_verification=allow_add_phone_verification,
        allow_existing_phone_verification=allow_existing_phone_verification,
    )
    message = sanitize_error_message(_message_for_status(last_error_code, last_error))
    return {
        "ok": False,
        "error": message,
        "data": {
            "message": message,
            "error_code": last_error_code,
            "retryable": retryable,
            "status": persist_result.get("status"),
            "invalid_recheck": persist_result.get("recheck") or {},
            "logs": list(action_logs),
        },
    }
