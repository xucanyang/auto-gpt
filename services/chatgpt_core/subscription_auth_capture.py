from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

from sqlmodel import Session

from core import db as core_db
from core.db import AccountModel
from core.config_store import config_store
from core.proxy_utils import normalize_proxy_url
from core.task_runtime import TaskInterruption
from services.chatgpt_account_state import apply_auth_capture_status, classify_chatgpt_capabilities
from services.chatgpt_core.task_logging import redact_log_text, redact_proxy_url, sanitize_error_message, sanitize_task_detail
from .chatgpt_registration_mode_adapter import RefreshTokenChatGPTRegistrationAdapter
from .account_fingerprint import (
    build_browser_fingerprint_payload,
    fingerprint_signature,
    inject_account_browser_fingerprint,
    persist_account_browser_fingerprint,
)
from .local_status_refresh import schedule_chatgpt_local_status_refresh_for_account_id
from .mailbox_state import sanitize_mailbox_state
from .restored_email_service import RestoredEmailService, mailbox_state_from_account
from .refresh_token_registration_engine import (
    EmailServiceAdapter,
    RefreshTokenRegistrationEngine,
    RegistrationResult,
)
from .utils import generate_random_birthday, generate_random_name


DEFAULT_SUBSCRIPTION_AUTH_CAPTURE_RETRY_DELAYS_SECONDS = (5, 10)
ADD_PHONE_ERROR_MARKERS = (
    "add_phone",
    "add-phone",
    "phone verification",
    "手机号",
)
PHONE_UNAVAILABLE_MARKERS = (
    "未配置可用的 smstome",
    "未找到 smstome",
    "号码池",
    "phone service",
    "smstome",
)
TEMPORARY_AUTH_ERROR_MARKERS = (
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
    "no_valid_organizations",
    "no valid organizations",
    "workspace/select",
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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


def _normalize_retry_delays(value: Any, *, use_default_when_empty: bool = True) -> list[int]:
    if isinstance(value, (list, tuple)):
        candidates = value
    elif value not in (None, ""):
        candidates = re.split(r"[,\s]+", str(value))
    else:
        candidates = DEFAULT_SUBSCRIPTION_AUTH_CAPTURE_RETRY_DELAYS_SECONDS

    delays: list[int] = []
    for item in candidates:
        try:
            seconds = int(float(str(item).strip()))
        except Exception:
            continue
        if seconds < 0:
            continue
        delays.append(min(seconds, 120))
    if delays:
        return delays
    if use_default_when_empty:
        return list(DEFAULT_SUBSCRIPTION_AUTH_CAPTURE_RETRY_DELAYS_SECONDS)
    return []


def _retry_delays_from_config(
    config: dict[str, Any],
    explicit: Sequence[int] | None = None,
) -> list[int]:
    if explicit is not None:
        return _normalize_retry_delays(list(explicit), use_default_when_empty=False)
    return _normalize_retry_delays(
        config.get("chatgpt_subscription_auth_capture_retry_delays_seconds")
        or config.get("chatgpt_resume_auth_retry_delays_seconds")
    )


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    lowered = str(text or "").lower()
    return any(marker.lower() in lowered for marker in markers)


def _classify_capture_error(error_text: str, *, allow_phone_verification: bool) -> tuple[str, bool]:
    text = str(error_text or "").strip()
    if not text:
        return "auth_capture_failed", False
    if allow_phone_verification and _contains_any(text, PHONE_UNAVAILABLE_MARKERS):
        return "phone_verification_unavailable", False
    if _contains_any(text, ADD_PHONE_ERROR_MARKERS):
        return ("phone_verification_failed" if allow_phone_verification else "add_phone_required"), True
    if "no_valid_organizations" in text.lower() or "no valid organizations" in text.lower():
        return "workspace_org_not_ready", True
    if _contains_any(text, TEMPORARY_AUTH_ERROR_MARKERS):
        return "temporary_auth_error", True
    if "未获取 refresh_token" in text or "refresh_token" in text:
        return "missing_refresh_token", True
    return "auth_capture_failed", False


def _build_auth_capture_payload(
    *,
    result: RegistrationResult,
    allow_phone_verification: bool,
    attempts: int,
    allow_add_phone_verification: bool | None = None,
    allow_existing_phone_verification: bool | None = None,
) -> dict[str, Any]:
    return {
        "ok": bool(result.success),
        "email": str(result.email or ""),
        "account_id": str(result.account_id or ""),
        "workspace_id": str(result.workspace_id or ""),
        "source": str(result.source or "subscription_auth_capture"),
        "has_access_token": bool(result.access_token),
        "has_refresh_token": bool(result.refresh_token),
        "allow_phone_verification": bool(allow_phone_verification),
        "allow_add_phone_verification": bool(allow_add_phone_verification) if allow_add_phone_verification is not None else bool(allow_phone_verification),
        "allow_existing_phone_verification": bool(allow_existing_phone_verification) if allow_existing_phone_verification is not None else bool(allow_phone_verification),
        "attempts": int(attempts or 0),
        "captured_at": _utcnow().isoformat(),
    }


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


def _persist_subscription_auth_result(
    account_id: int,
    result: RegistrationResult,
    *,
    auth_capture: dict[str, Any],
    mailbox_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    with Session(core_db.engine) as session:
        account = session.get(AccountModel, int(account_id or 0))
        if account is None or account.platform != "chatgpt":
            raise ValueError("ChatGPT 账号不存在")

        adapter = RefreshTokenChatGPTRegistrationAdapter()
        primary = adapter.build_account(result, account.password)
        account.user_id = primary.user_id or account.user_id
        account.token = primary.token or account.token

        extra = account.get_extra()
        extra.update(primary.extra or {})
        extra = persist_account_browser_fingerprint(extra, source="subscription_auth_capture", overwrite=False)
        if mailbox_state:
            cleaned_mailbox_state = sanitize_mailbox_state(
                mailbox_state,
                account_email=str(getattr(account, "email", "") or ""),
            )
            if cleaned_mailbox_state:
                extra["chatgpt_mailbox_state"] = cleaned_mailbox_state
        extra["chatgpt_last_auth_capture"] = dict(auth_capture)
        extra["chatgpt_subscription_auth_result"] = dict(auth_capture)
        account.set_extra(extra)
        extra["chatgpt_capabilities"] = classify_chatgpt_capabilities(
            account,
            local_probe=extra.get("chatgpt_local") if isinstance(extra.get("chatgpt_local"), dict) else None,
        )
        account.set_extra(extra)
        apply_auth_capture_status(account, getattr(primary.status, "value", primary.status))
        account.updated_at = _utcnow()
        session.add(account)
        session.commit()
        session.refresh(account)
        schedule_chatgpt_local_status_refresh_for_account_id(account.id, reason="subscription_auth_capture", delay_seconds=2.0)
        return {
            "status": str(account.status or ""),
            "user_id": str(account.user_id or ""),
            "token_saved": bool(account.token),
        }


def capture_subscription_auth_for_account(
    account_id: int,
    *,
    allow_phone_verification: bool = False,
    allow_add_phone_verification: bool | None = None,
    allow_existing_phone_verification: bool | None = None,
    retry_delays_seconds: Sequence[int] | None = None,
    log_fn: Callable[[str], None] | None = None,
    shared_phone_service: Any = None,
    stop_checker: Callable[[], None] | None = None,
    proxy_url: str | None = None,
    phone_sms_probe_only: bool = False,
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
                log_fn(text, level)
            except TypeError:
                log_fn(text)

    allow_phone_verification = _to_bool(allow_phone_verification, default=False)
    phone_sms_probe_only = _to_bool(phone_sms_probe_only, default=False)
    proxy_url = normalize_proxy_url(proxy_url) or ""
    account_id = int(account_id or 0)
    _check_stop()
    if account_id <= 0:
        return {
            "ok": False,
            "error": "account_id 无效",
            "data": {
                "message": "account_id 无效",
                "error_code": "invalid_account_id",
                "retryable": False,
                "allow_phone_verification": allow_phone_verification,
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
                    "allow_phone_verification": allow_phone_verification,
                    "logs": list(action_logs),
                },
            }
        email = str(account.email or "").strip()
        password = str(account.password or "")
        extra = account.get_extra()
        mailbox_state = mailbox_state_from_account(account, extra=extra)

    if not email:
        return {
            "ok": False,
            "error": "账号邮箱为空，无法补抓 Auth",
            "data": {
                "message": "账号邮箱为空，无法补抓 Auth",
                "error_code": "missing_email",
                "retryable": False,
                "allow_phone_verification": allow_phone_verification,
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
                "allow_phone_verification": allow_phone_verification,
                "logs": list(action_logs),
            },
        }

    merged_config = config_store.get_all().copy()
    merged_config.update({k: v for k, v in extra.items() if v not in (None, "")})
    merged_config = inject_account_browser_fingerprint(merged_config, extra, overwrite=False)
    merged_config["_current_account_id"] = account_id
    merged_config["_current_account_email"] = email
    merged_config["_current_task_id"] = str(merged_config.get("_current_task_id") or "")
    if allow_add_phone_verification is None:
        allow_add_phone_verification = _config_bool(
            merged_config,
            "chatgpt_resume_auth_allow_add_phone_verification",
            default=allow_phone_verification,
        )
    else:
        allow_add_phone_verification = _to_bool(allow_add_phone_verification, default=False)
    if allow_existing_phone_verification is None:
        allow_existing_phone_verification = _config_bool(
            merged_config,
            "chatgpt_resume_auth_allow_existing_phone_verification",
            default=True,
        )
    else:
        allow_existing_phone_verification = _to_bool(allow_existing_phone_verification, default=True)
    if shared_phone_service is not None:
        merged_config["_shared_phone_service"] = shared_phone_service
    if callable(stop_checker):
        merged_config["_task_stop_checker"] = stop_checker
    if proxy_url:
        merged_config["_runtime_proxy_url"] = proxy_url
    browser_mode = str(
        extra.get("browser_mode")
        or merged_config.get("browser_mode")
        or merged_config.get("default_executor")
        or "protocol"
    ).strip().lower() or "protocol"
    retry_delays = _retry_delays_from_config(merged_config, retry_delays_seconds)
    max_attempts = 1 + len(retry_delays)

    _log(
        f"[补抓] 账号级 Auth 捕获开始：{email}，"
        f"allow_phone_verification={'true' if allow_phone_verification else 'false'}，"
        f"allow_add_phone={'true' if allow_add_phone_verification else 'false'}，"
        f"allow_existing_phone_otp={'true' if allow_existing_phone_verification else 'false'}，"
        f"proxy={redact_proxy_url(proxy_url) or 'direct'}"
    )
    _timeline_log(
        log_fn,
        "[补抓Auth] 开始：恢复邮箱状态并进入 OAuth 登录",
    )

    last_error = ""
    last_error_code = "auth_capture_failed"
    retryable = False
    result: RegistrationResult | None = None
    exported_mailbox_state = dict(mailbox_state)

    for attempt in range(1, max_attempts + 1):
        _check_stop()
        if attempt > 1:
            _log(f"[补抓] 开始第 {attempt}/{max_attempts} 次账号级 Auth 捕获")
        email_service = None
        engine_instance: RefreshTokenRegistrationEngine | None = None
        try:
            _timeline_log(log_fn, f"[补抓Auth] 阶段 2/5：OAuth 登录并补抓完整 Auth/RT（尝试 {attempt}/{max_attempts}）")
            email_service = RestoredEmailService(state=mailbox_state, log_fn=_log)
            engine_instance = RefreshTokenRegistrationEngine(
                email_service=email_service,
                proxy_url=proxy_url or None,
                callback_logger=lambda msg, level="info", *_: _log(str(msg), str(level or "info")),
                browser_mode=browser_mode,
                extra_config=merged_config,
            )
            engine_instance.email = email
            engine_instance.password = password
            if not engine_instance._create_email():
                raise RuntimeError("恢复邮箱失败")

            email_adapter = EmailServiceAdapter(
                email_service,
                email,
                lambda msg, level="info": _log(str(msg), str(level or "info")),
            )
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
                allow_phone_verification=bool(allow_phone_verification or allow_add_phone_verification or allow_existing_phone_verification),
                allow_add_phone_verification=allow_add_phone_verification,
                allow_existing_phone_verification=allow_existing_phone_verification,
                phone_sms_probe_only=phone_sms_probe_only,
                force_new_browser=True,
                force_chatgpt_entry=False,
                screen_hint="login",
                force_password_login=bool(password),
                complete_about_you_if_needed=True,
                first_name=first_name,
                last_name=last_name,
                birthdate=birthdate,
                login_source="subscription_auth_capture",
                stop_after_login=False,
                allow_add_phone_session_recovery=False,
            )
            if not tokens:
                raise RuntimeError(oauth_client.last_error or "OAuth 登录失败")

            result = RegistrationResult(success=False, email=email, password=password, logs=action_logs)
            engine_instance._populate_result_from_tokens(
                result=result,
                tokens=tokens,
                oauth_client=oauth_client,
                registration_message="subscription_auth_capture:ok",
                source="subscription_auth_capture",
                register_client=register_client,
            )
            if not result.success:
                raise RuntimeError(result.error_message or "OAuth 登录成功但未获取 refresh_token")

            exported_mailbox_state = email_service.export_state()
            result.metadata = {
                **dict(result.metadata or {}),
                "token_flow": "oauth_client.login_and_get_tokens",
                "registration_flow": "subscription_auth_capture",
                "mailbox_state": exported_mailbox_state,
                "allow_phone_verification": allow_phone_verification,
                "allow_add_phone_verification": allow_add_phone_verification,
                "allow_existing_phone_verification": allow_existing_phone_verification,
                "proxy": proxy_url or "direct",
                "proxy_redacted": redact_proxy_url(proxy_url) or "direct",
            }
            browser_fingerprint = build_browser_fingerprint_payload(getattr(register_client, "fingerprint", None))
            if browser_fingerprint:
                result.metadata["chatgpt_browser_fingerprint"] = browser_fingerprint
                result.metadata["chatgpt_browser_fingerprint_signature"] = fingerprint_signature(browser_fingerprint)
                result.metadata["chatgpt_browser_fingerprint_source"] = "account"
                result.metadata["chatgpt_browser_fingerprint_isolated"] = True
                result.metadata["registration_context"] = {
                    "device_id": browser_fingerprint.get("device_id") or "",
                    "user_agent": browser_fingerprint.get("user_agent") or "",
                    "sec_ch_ua": browser_fingerprint.get("sec_ch_ua") or "",
                    "impersonate": browser_fingerprint.get("impersonate") or "",
                    "accept_language": browser_fingerprint.get("accept_language") or "",
                    "browser_fingerprint": browser_fingerprint,
                    "first_name": first_name,
                    "last_name": last_name,
                    "birthdate": birthdate,
                }

            auth_capture = _build_auth_capture_payload(
                result=result,
                allow_phone_verification=allow_phone_verification,
                allow_add_phone_verification=allow_add_phone_verification,
                allow_existing_phone_verification=allow_existing_phone_verification,
                attempts=attempt,
            )
            persist_result = _persist_subscription_auth_result(
                account_id,
                result,
                auth_capture=auth_capture,
                mailbox_state=exported_mailbox_state,
            )
            auth_capture.update(persist_result)
            _log(
                f"[补抓] Auth 捕获完成：account_id={result.account_id or '-'} "
                f"workspace_id={result.workspace_id or '-'}"
            )
            _timeline_log(log_fn, "[补抓Auth] 成功：refresh_token 已保存")
            return {
                "ok": True,
                "data": {
                    "message": "补抓 Auth 完成",
                    "auth_capture": auth_capture,
                    "logs": list(action_logs),
                },
                "error": "",
            }
        except TaskInterruption:
            raise
        except Exception as exc:
            last_error = sanitize_error_message(exc or "补抓 Auth 失败")
            last_error_code, retryable = _classify_capture_error(
                last_error,
                allow_phone_verification=allow_phone_verification,
            )
            if attempt >= max_attempts or not retryable:
                break
            delay_seconds = int(retry_delays[attempt - 1])
            _log(
                f"[补抓] 本次失败：{last_error}；等待 {delay_seconds}s 后重试 "
                f"({attempt + 1}/{max_attempts})"
            )
            _timeline_log(log_fn, f"[补抓Auth] 本次失败：{last_error}；等待 {delay_seconds}s 后重试")
            if delay_seconds > 0:
                deadline = time.monotonic() + delay_seconds
                while True:
                    _check_stop()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    time.sleep(min(1, remaining))

    message = sanitize_error_message(last_error or "补抓 Auth 失败")
    _log(f"[补抓] 失败：{message}")
    _timeline_log(log_fn, f"[补抓Auth] 失败：{message}")
    return {
        "ok": False,
        "error": sanitize_error_message(message),
        "data": {
            "message": message,
            "error_code": last_error_code,
            "retryable": bool(retryable),
            "allow_phone_verification": allow_phone_verification,
            "logs": list(action_logs),
            "auth_capture": {
                "ok": False,
                "email": email,
                "source": "subscription_auth_capture",
                "allow_phone_verification": allow_phone_verification,
                "allow_add_phone_verification": allow_add_phone_verification,
                "allow_existing_phone_verification": allow_existing_phone_verification,
                "attempts": max_attempts,
                "error_code": last_error_code,
                "error": sanitize_error_message(message),
            },
        },
    }
