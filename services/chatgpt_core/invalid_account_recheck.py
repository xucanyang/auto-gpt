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
from services.chatgpt_account_state import (
    apply_auth_capture_status,
    classify_chatgpt_capabilities,
    has_payment_pending_marker,
    is_account_deactivated_message,
)
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


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    lowered = str(text or "").lower()
    return any(marker.lower() in lowered for marker in markers)


def _classify_recheck_error(error_text: str) -> tuple[str, bool, bool | None]:
    text = str(error_text or "").strip()
    if not text:
        return "unknown_error", False, None
    if is_account_deactivated_message("", text):
        return "account_deactivated", False, False
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
    return str(raw_error or "失效测活失败，保持原状态").strip()


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
    if exported_mailbox_state:
        payload["mailbox_state"] = dict(exported_mailbox_state)
    return payload


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
) -> dict[str, Any]:
    access_token = str(tokens.get("access_token") or "").strip()
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

        recheck_payload = _build_recheck_payload(
            status="recovered_access_token",
            email=email,
            attempts=attempts,
            task_id=task_id,
            recoverable=True,
            account_id=token_account_id or str(account.user_id or ""),
            has_access_token=True,
            exported_mailbox_state=exported_mailbox_state,
        )
        extra["chatgpt_invalid_recheck"] = recheck_payload

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


def recheck_invalid_chatgpt_account(
    account_id: int,
    *,
    retry_delays_seconds: Sequence[int] | None = None,
    log_fn: Callable[[str], None] | None = None,
    stop_checker: Callable[[], None] | None = None,
    task_id: str = "",
) -> dict[str, Any]:
    action_logs: list[str] = []

    def _check_stop() -> None:
        if callable(stop_checker):
            stop_checker()

    def _log(message: str, level: str = "info") -> None:
        _check_stop()
        text = str(message or "").strip()
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
                "logs": action_logs,
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
                    "logs": action_logs,
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
                "logs": action_logs,
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
                "logs": action_logs,
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
                "logs": action_logs,
            },
        }

    merged_config = config_store.get_all().copy()
    merged_config.update({key: value for key, value in extra.items() if value not in (None, "")})
    merged_config["_current_account_id"] = account_id
    merged_config["_current_account_email"] = email
    browser_mode = str(
        extra.get("browser_mode")
        or merged_config.get("browser_mode")
        or merged_config.get("default_executor")
        or "protocol"
    ).strip().lower() or "protocol"
    retry_delays = _retry_delays_from_config(merged_config, retry_delays_seconds)
    max_attempts = 1 + len(retry_delays)
    exported_mailbox_state = dict(mailbox_state)

    _log(f"[失效测活] 开始处理账号：{email}")
    last_error = ""
    last_error_code = "unknown_error"
    retryable = False
    recoverable: bool | None = None
    attempts_executed = 0

    for attempt in range(1, max_attempts + 1):
        attempts_executed = attempt
        _check_stop()
        if attempt > 1:
            _log(f"[失效测活] 开始第 {attempt}/{max_attempts} 次登录测活")
        email_service = None
        try:
            email_service = RestoredEmailService(state=exported_mailbox_state, log_fn=_log)
            email_service.create_email()
            email_adapter = EmailServiceAdapter(email_service, email, _log)
            engine_instance = RefreshTokenRegistrationEngine(
                email_service=email_service,
                proxy_url=None,
                callback_logger=lambda msg, level="info", *_: _log(str(msg), str(level or "info")),
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
                allow_phone_verification=False,
                force_new_browser=True,
                force_chatgpt_entry=False,
                screen_hint="login",
                force_password_login=bool(password),
                complete_about_you_if_needed=True,
                first_name=first_name,
                last_name=last_name,
                birthdate=birthdate,
                login_source="workspace_capture_free:invalid_account_recheck",
                stop_after_login=False,
                workspace_scope_preference="free",
                allow_add_phone_session_recovery=False,
            )
            exported_mailbox_state = email_service.export_state()
            if not tokens or not str((tokens or {}).get("access_token") or "").strip():
                raise RuntimeError(oauth_client.last_error or "OAuth 登录成功但未获取 access_token")

            persist_result = _persist_recheck_success(
                account_id,
                email=email,
                tokens=tokens,
                oauth_client=oauth_client,
                engine_instance=engine_instance,
                attempts=attempt,
                task_id=task_id,
                exported_mailbox_state=exported_mailbox_state,
            )
            _log(f"[失效测活] 恢复成功，已保存 access_token：{email}")
            data = {
                "message": "失效测活成功，已重新保存 access_token",
                "status": persist_result.get("status"),
                "token_saved": bool(persist_result.get("token_saved")),
                "invalid_recheck": persist_result.get("recheck") or {},
                "logs": action_logs,
            }
            return {"ok": True, "data": data, "error": ""}
        except TaskInterruption:
            raise
        except Exception as exc:
            last_error = str(exc or "失效测活失败")
            last_error_code, retryable, recoverable = _classify_recheck_error(last_error)
            if email_service is not None:
                exporter = getattr(email_service, "export_state", None)
                if callable(exporter):
                    try:
                        exported_mailbox_state = exporter() or exported_mailbox_state
                    except Exception:
                        pass
            if attempt >= max_attempts or not retryable:
                break
            delay_seconds = int(retry_delays[attempt - 1])
            if delay_seconds > 0:
                _log(f"[失效测活] {last_error_code}，{delay_seconds}s 后重试")
                time.sleep(delay_seconds)

    persist_result = _persist_recheck_failure(
        account_id,
        email=email,
        status=last_error_code,
        raw_error=last_error,
        attempts=max(1, attempts_executed),
        task_id=task_id,
        recoverable=recoverable,
        exported_mailbox_state=exported_mailbox_state,
    )
    message = _message_for_status(last_error_code, last_error)
    return {
        "ok": False,
        "error": message,
        "data": {
            "message": message,
            "error_code": last_error_code,
            "retryable": retryable,
            "status": persist_result.get("status"),
            "invalid_recheck": persist_result.get("recheck") or {},
            "logs": action_logs,
        },
    }
