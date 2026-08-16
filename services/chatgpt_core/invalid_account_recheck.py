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
from .account_fingerprint import inject_account_browser_fingerprint, persist_account_browser_fingerprint
from .local_status_refresh import (
    prepare_chatgpt_account_for_local_status_refresh,
    schedule_chatgpt_local_status_refresh_for_account_id,
)
from .mailbox_state import mailbox_state_summary, sanitize_mailbox_state
from .restored_email_service import RestoredEmailService, mailbox_state_from_account
from .utils import decode_jwt_payload
from .web_session_login import capture_web_session_without_refresh_token


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
    "incorrect email address or password",
    "incorrect email or password",
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
    "chatgpt_has_refresh_token_solution",
    "partial_auth",
)
INVALID_RECHECK_CLEAR_EXTRA_KEYS = (
    *AT_ONLY_CLEAR_EXTRA_KEYS,
    "cookies",
    "cookie_header",
    "account_id",
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
        return "失效测活成功，已重新保存完整 ChatGPT Web Session"
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
    has_session_token: bool = False,
    has_cookies: bool = False,
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
        "has_session_token": bool(has_session_token),
        "has_cookies": bool(has_cookies),
        "web_session_complete": bool(has_access_token and has_session_token and has_cookies),
    }
    if allow_add_phone_verification is not None:
        payload["allow_add_phone_verification"] = bool(allow_add_phone_verification)
    if allow_existing_phone_verification is not None:
        payload["allow_existing_phone_verification"] = bool(allow_existing_phone_verification)
    if exported_mailbox_state:
        payload["mailbox_state"] = mailbox_state_summary(
            exported_mailbox_state,
            account_email=email,
        )
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
    has_session_token: bool = False,
    has_cookies: bool = False,
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
        "has_session_token": bool(has_session_token),
        "has_cookies": bool(has_cookies),
        "web_session_complete": bool(has_access_token and has_session_token and has_cookies),
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
    attempts: int,
    task_id: str = "",
    exported_mailbox_state: dict[str, Any] | None = None,
    allow_add_phone_verification: bool | None = None,
    allow_existing_phone_verification: bool | None = None,
    proxy_url: str | None = None,
) -> dict[str, Any]:
    access_token = str(tokens.get("access_token") or "").strip()
    session_token = str(tokens.get("session_token") or "").strip()
    cookie_header = str(tokens.get("cookie_header") or tokens.get("cookies") or "").strip()
    if not access_token or not session_token or not cookie_header:
        raise ValueError(
            "ChatGPT Web Session 材料不完整: "
            f"AT状态={'存在' if access_token else '缺失'}｜"
            f"Session状态={'存在' if session_token else '缺失'}｜"
            f"Cookie状态={'存在' if cookie_header else '缺失'}"
        )
    token_account_id = str(tokens.get("account_id") or "").strip() or _account_id_from_access_token(access_token)
    workspace_id = str(tokens.get("workspace_id") or token_account_id or "").strip()

    with Session(core_db.engine) as session:
        account = session.get(AccountModel, int(account_id or 0))
        if account is None or account.platform != "chatgpt":
            raise ValueError("ChatGPT 账号不存在")

        extra = account.get_extra()
        for key in INVALID_RECHECK_CLEAR_EXTRA_KEYS:
            extra.pop(key, None)
        extra["access_token"] = access_token
        extra["session_token"] = session_token
        extra["cookies"] = cookie_header
        extra["cookie_header"] = cookie_header
        if workspace_id:
            extra["workspace_id"] = workspace_id
        if token_account_id:
            extra["account_id"] = token_account_id
        extra["auth_level"] = "access_token_only"
        extra["chatgpt_registration_mode"] = "access_token_only"
        extra["chatgpt_token_source"] = "invalid_account_recheck"
        extra["chatgpt_has_refresh_token_solution"] = False
        if exported_mailbox_state:
            cleaned_mailbox_state = sanitize_mailbox_state(
                exported_mailbox_state,
                account_email=email,
            )
            if cleaned_mailbox_state:
                extra["chatgpt_mailbox_state"] = cleaned_mailbox_state

        revival_marker = _build_revival_marker(
            source="invalid_account_recheck",
            mode="revive_existing",
            email=email,
            task_id=task_id,
            account_row_id=int(account.id or 0),
            has_access_token=True,
            has_refresh_token=False,
            has_session_token=True,
            has_cookies=True,
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
            has_session_token=True,
            has_cookies=True,
            exported_mailbox_state=exported_mailbox_state,
            allow_add_phone_verification=allow_add_phone_verification,
            allow_existing_phone_verification=allow_existing_phone_verification,
        )
        recheck_payload["revival_marker"] = dict(revival_marker)
        extra["chatgpt_invalid_recheck"] = recheck_payload
        _append_revival_marker(extra, revival_marker)
        extra = persist_account_browser_fingerprint(extra, source="invalid_account_recheck", overwrite=False)

        account.token = access_token
        if token_account_id:
            account.user_id = token_account_id
        account.set_extra(extra)
        apply_auth_capture_status(
            account,
            "pending_payment" if has_payment_pending_marker(account) else "registered",
        )
        prepare_chatgpt_account_for_local_status_refresh(
            account,
            reason="invalid_account_recheck:recovered",
        )
        account.updated_at = _utcnow()
        session.add(account)
        from services.account_filters import upsert_account_list_state_for_account_ids

        upsert_account_list_state_for_account_ids(session, [account.id], commit=False)
        session.commit()
        session.refresh(account)
        schedule_chatgpt_local_status_refresh_for_account_id(
            account.id,
            proxy=proxy_url or None,
            use_default_proxy=False if proxy_url else True,
            reason="invalid_account_recheck:recovered",
            delay_seconds=2.0,
        )
        return {
            "status": str(account.status or ""),
            "user_id": str(account.user_id or ""),
            "token_saved": bool(account.token),
            "web_session_complete": True,
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
            cleaned_mailbox_state = sanitize_mailbox_state(
                exported_mailbox_state,
                account_email=email,
            )
            if cleaned_mailbox_state:
                extra["chatgpt_mailbox_state"] = cleaned_mailbox_state
        extra = persist_account_browser_fingerprint(extra, source="invalid_account_recheck", overwrite=False)
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


def _capture_web_session_without_refresh_token(
    *,
    email: str,
    password: str,
    exported_mailbox_state: dict[str, Any],
    browser_mode: str,
    log_fn: Callable[[str], None],
    proxy_url: str | None = None,
    task_control=None,
    attempt_id: int | None = None,
    stop_checker: Callable[[], None] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    return capture_web_session_without_refresh_token(
        email=email,
        password=password,
        exported_mailbox_state=exported_mailbox_state,
        browser_mode=browser_mode,
        log_fn=log_fn,
        proxy_url=proxy_url,
        task_control=task_control,
        attempt_id=attempt_id,
        stop_checker=stop_checker,
        otp_phase="invalid_recheck_login_otp",
        otp_phase_label="失效测活登录验证码",
        email_service_cls=RestoredEmailService,
    )


def recheck_invalid_chatgpt_account(
    account_id: int,
    *,
    retry_delays_seconds: Sequence[int] | None = None,
    log_fn: Callable[[str], None] | None = None,
    stop_checker: Callable[[], None] | None = None,
    task_id: str = "",
    task_control: Any = None,
    attempt_id: int | None = None,
    proxy_url: str | None = None,
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
        mailbox_state = mailbox_state_from_account(account, extra=extra)

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
    merged_config = inject_account_browser_fingerprint(merged_config, extra, overwrite=False)
    merged_config["_current_account_id"] = account_id
    merged_config["_current_account_email"] = email
    merged_config["_current_task_id"] = task_id
    if task_control is not None:
        merged_config["_task_control"] = task_control
        merged_config["_task_attempt_id"] = attempt_id
        merged_config["_manual_phone_otp_enabled"] = True
        merged_config["_manual_phone_otp_timeout_seconds"] = 60
    allow_add_phone_verification = False
    allow_existing_phone_verification = False
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
        "不执行 add_phone 新绑，不进入手机号补抓流程"
    )
    last_error = ""
    last_error_code = "unknown_error"
    retryable = False
    recoverable: bool | None = None
    attempts_executed = 0
    success_result: dict[str, Any] | None = None

    for attempt in range(1, max_attempts + 1):
        attempts_executed = attempt
        _check_stop()
        if attempt > 1:
            _log(f"[失效测活] 开始第 {attempt}/{max_attempts} 次 Web Session 登录测活")
        try:
            _timeline_log(log_fn, "[失效测活] 登录已有账号并抓取完整 ChatGPT Web Session")
            session_tokens, exported_mailbox_state = _capture_web_session_without_refresh_token(
                email=email,
                password=password,
                exported_mailbox_state=exported_mailbox_state,
                browser_mode=browser_mode,
                log_fn=_log,
                proxy_url=proxy_url,
                task_control=task_control,
                attempt_id=attempt_id,
                stop_checker=stop_checker,
            )
            persist_result = _persist_recheck_success(
                account_id,
                email=email,
                tokens=session_tokens,
                attempts=attempt,
                task_id=task_id,
                exported_mailbox_state=exported_mailbox_state,
                allow_add_phone_verification=allow_add_phone_verification,
                allow_existing_phone_verification=allow_existing_phone_verification,
                proxy_url=proxy_url,
            )
            _timeline_log(log_fn, "[失效测活] 结果：Web Session 已写回原账号，状态已复活并调度本地刷新")
            success_result = persist_result
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

    if success_result is not None:
        return {
            "ok": True,
            "data": {
                "message": "失效测活成功，已刷新原账号 Web Session 与本地状态",
                "status": success_result.get("status"),
                "token_saved": bool(success_result.get("token_saved")),
                "web_session_complete": bool(success_result.get("web_session_complete")),
                "invalid_recheck": success_result.get("recheck") or {},
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
