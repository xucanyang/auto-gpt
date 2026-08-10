"""Capture and persist a fresh ChatGPT Web Session for an existing account."""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

from sqlmodel import Session

from core import db as core_db
from core.config_store import config_store
from core.db import AccountModel
from core.task_runtime import TaskInterruption

from .account_fingerprint import (
    build_browser_fingerprint_payload,
    inject_account_browser_fingerprint,
    persist_account_browser_fingerprint,
    resolve_account_browser_fingerprint,
)
from .local_status_refresh import (
    prepare_chatgpt_account_for_local_status_refresh,
    schedule_chatgpt_local_status_refresh_for_account_id,
)
from .mailbox_state import mailbox_state_summary, sanitize_mailbox_state
from .refresh_token_registration_engine import EmailServiceAdapter
from .restored_email_service import RestoredEmailService, mailbox_state_from_account
from .task_logging import redact_log_text, sanitize_error_message
from .utils import decode_jwt_payload, generate_browser_fingerprint
from .web_session_lease import WebSessionLeaseReleaseRequested


DEFAULT_WEB_SESSION_LOGIN_RETRY_DELAYS_SECONDS = (5, 10)
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


class WebSessionIdentityMismatchError(RuntimeError):
    """The captured Web Session belongs to a different ChatGPT account."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _invoke_log(log_fn: Callable[[str], None] | None, message: str, level: str = "info") -> None:
    if not callable(log_fn):
        return
    try:
        log_fn(message)
    except TypeError:
        log_fn(message, level)


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    lowered = str(text or "").lower()
    return any(marker.lower() in lowered for marker in markers)


def _normalize_retry_delays(value: Any) -> list[int]:
    if isinstance(value, (list, tuple)):
        candidates = value
    elif value not in (None, ""):
        candidates = re.split(r"[,\s]+", str(value))
    else:
        candidates = DEFAULT_WEB_SESSION_LOGIN_RETRY_DELAYS_SECONDS

    delays: list[int] = []
    for item in candidates:
        try:
            seconds = int(float(str(item).strip()))
        except Exception:
            continue
        if seconds >= 0:
            delays.append(min(seconds, 120))
    return delays or list(DEFAULT_WEB_SESSION_LOGIN_RETRY_DELAYS_SECONDS)


def _retry_delays_from_config(config: dict[str, Any], explicit: Sequence[int] | None) -> list[int]:
    if explicit is not None:
        return _normalize_retry_delays(list(explicit))
    return _normalize_retry_delays(
        config.get("chatgpt_web_session_login_retry_delays_seconds")
        or config.get("chatgpt_invalid_recheck_retry_delays_seconds")
        or config.get("chatgpt_resume_auth_retry_delays_seconds")
    )


def _classify_login_error(error_text: str) -> tuple[str, bool]:
    text = str(error_text or "").strip()
    if isinstance(error_text, WebSessionIdentityMismatchError):
        return "account_identity_mismatch", False
    if _contains_any(text, ("account_identity_mismatch", "登录账号身份不一致")):
        return "account_identity_mismatch", False
    if _contains_any(text, ("otp_rate_limited", "too many tries", "too many attempts", "please wait a few minutes")):
        return "otp_rate_limited", True
    if _contains_any(text, LOGIN_BLOCKED_MARKERS):
        return "login_blocked", False
    if _contains_any(text, PASSWORD_INVALID_MARKERS):
        return "password_invalid", False
    if _contains_any(text, TEMPORARY_LOGIN_ERROR_MARKERS):
        return "network_failed", True
    return "login_failed", False


def _account_id_from_access_token(access_token: str) -> str:
    payload = decode_jwt_payload(access_token)
    auth_claims = payload.get("https://api.openai.com/auth") or {}
    return str(auth_claims.get("chatgpt_account_id") or payload.get("sub") or "").strip()


def _masked_identity(value: str) -> str:
    text = str(value or "").strip()
    if len(text) <= 8:
        return text or "-"
    return f"{text[:4]}...{text[-4:]}"


def _interruptible_sleep(seconds: int, stop_checker: Callable[[], None] | None) -> None:
    deadline = time.monotonic() + max(int(seconds or 0), 0)
    while True:
        if callable(stop_checker):
            stop_checker()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 0.5))


def capture_web_session_without_refresh_token(
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
    otp_phase: str = "web_session_login_otp",
    otp_phase_label: str = "执行登录态验证码",
    email_service_cls=None,
    session_lease: Any = None,
    session_ready_callback: Callable[
        [dict[str, Any], dict[str, Any], str], dict[str, Any]
    ] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run the existing-account browser login and return complete session material."""

    service_cls = email_service_cls or RestoredEmailService
    try:
        email_service = service_cls(
            state=exported_mailbox_state,
            log_fn=log_fn,
            task_control=task_control,
            attempt_id=attempt_id,
        )
    except TypeError as exc:
        if "task_control" not in str(exc) and "attempt_id" not in str(exc):
            raise
        email_service = service_cls(state=exported_mailbox_state, log_fn=log_fn)

    email_service.create_email()
    email_adapter = EmailServiceAdapter(email_service, email, log_fn)
    from .any_auto.transport import run_any_auto_browser_registration

    def _tokens_from_result(result: Any) -> dict[str, Any]:
        metadata = dict(getattr(result, "metadata", None) or {})
        return {
            "access_token": str(result.access_token or "").strip(),
            "session_token": str(result.session_token or "").strip(),
            "cookies": str(result.cookies or result.cookie_header or "").strip(),
            "cookie_header": str(result.cookie_header or result.cookies or "").strip(),
            "account_id": str(result.account_id or "").strip(),
            "workspace_id": str(result.workspace_id or result.account_id or "").strip(),
            "refresh_token": "",
            "browser_fingerprint": build_browser_fingerprint_payload(
                metadata.get("web_session_browser_fingerprint")
                or metadata.get("chatgpt_browser_fingerprint")
                or metadata.get("browser_fingerprint")
            ),
        }

    def _publish_ready(result: Any, reason: str) -> dict[str, Any]:
        if not result.ok:
            raise RuntimeError(
                str(result.error_message or "登录完成但 ChatGPT Web Session 材料不完整")
            )
        if not callable(session_ready_callback):
            return {}
        return dict(
            session_ready_callback(
                _tokens_from_result(result),
                email_service.export_state(),
                str(reason or "login"),
            )
            or {}
        )

    result = run_any_auto_browser_registration(
        email=email,
        password=password,
        proxy_url=proxy_url or None,
        headless=str(browser_mode or "").strip().lower() != "headed",
        otp_callback=lambda: email_adapter.wait_for_verification_code(
            email,
            timeout=120,
            phase=otp_phase,
            phase_label=otp_phase_label,
        ),
        phone_callback=None,
        stop_check=stop_checker,
        login_only=True,
        log_fn=log_fn,
        session_lease=session_lease,
        session_ready_callback=(
            _publish_ready if session_lease is not None else None
        ),
    )
    exported_state = email_service.export_state()
    if not result.ok:
        raise RuntimeError(str(result.error_message or "登录完成但 ChatGPT Web Session 材料不完整"))

    return _tokens_from_result(result), exported_state


def _build_login_payload(
    *,
    status: str,
    email: str,
    task_id: str,
    attempts: int,
    account_id: str = "",
    workspace_id: str = "",
    error_code: str = "",
    error: str = "",
    mailbox_state: dict[str, Any] | None = None,
    browser_fingerprint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": str(status or ""),
        "source": "web_session_login",
        "email": str(email or ""),
        "task_id": str(task_id or ""),
        "attempts": int(attempts or 0),
        "checked_at": _utcnow().isoformat(),
        "account_id": str(account_id or ""),
        "workspace_id": str(workspace_id or ""),
        "has_access_token": status == "success",
        "has_session_token": status == "success",
        "has_cookies": status == "success",
        "web_session_complete": status == "success",
    }
    if error_code:
        payload["error_code"] = str(error_code)
    if error:
        payload["error"] = sanitize_error_message(error)
    if mailbox_state:
        payload["mailbox_state"] = mailbox_state_summary(mailbox_state, account_email=email)
    fingerprint = build_browser_fingerprint_payload(browser_fingerprint)
    if fingerprint:
        payload["browser_fingerprint"] = fingerprint
    return payload


def _persist_login_success(
    account_id: int,
    *,
    expected_email: str,
    expected_account_id: str,
    tokens: dict[str, Any],
    exported_mailbox_state: dict[str, Any],
    task_id: str,
    attempts: int,
    proxy_url: str | None = None,
) -> dict[str, Any]:
    access_token = str(tokens.get("access_token") or "").strip()
    session_token = str(tokens.get("session_token") or "").strip()
    cookie_header = str(tokens.get("cookie_header") or tokens.get("cookies") or "").strip()
    captured_account_id = str(tokens.get("account_id") or "").strip() or _account_id_from_access_token(access_token)
    workspace_id = str(tokens.get("workspace_id") or captured_account_id or "").strip()
    if not access_token or not session_token or not cookie_header or not captured_account_id:
        raise ValueError(
            "ChatGPT Web Session 材料不完整: "
            f"AT状态={'存在' if access_token else '缺失'}｜"
            f"Session状态={'存在' if session_token else '缺失'}｜"
            f"Cookie状态={'存在' if cookie_header else '缺失'}｜"
            f"账号ID状态={'存在' if captured_account_id else '缺失'}"
        )
    if expected_account_id and captured_account_id != expected_account_id:
        raise WebSessionIdentityMismatchError(
            "account_identity_mismatch: 登录账号身份不一致，"
            f"原账号={_masked_identity(expected_account_id)}，捕获账号={_masked_identity(captured_account_id)}"
        )

    captured_fingerprint = build_browser_fingerprint_payload(tokens.get("browser_fingerprint"))
    with Session(core_db.engine) as session:
        account = session.get(AccountModel, int(account_id or 0))
        if account is None or account.platform != "chatgpt":
            raise ValueError("ChatGPT 账号不存在")
        if str(account.email or "").strip().casefold() != str(expected_email or "").strip().casefold():
            raise WebSessionIdentityMismatchError("account_identity_mismatch: 登录期间原账号记录已被替换")

        extra = account.get_extra()
        current_expected_account_id = str(extra.get("account_id") or account.user_id or "").strip()
        if current_expected_account_id and current_expected_account_id != captured_account_id:
            raise WebSessionIdentityMismatchError(
                "account_identity_mismatch: 登录账号身份不一致，"
                f"原账号={_masked_identity(current_expected_account_id)}，捕获账号={_masked_identity(captured_account_id)}"
            )

        extra["access_token"] = access_token
        extra["session_token"] = session_token
        extra["cookies"] = cookie_header
        extra["cookie_header"] = cookie_header
        extra["account_id"] = captured_account_id
        if workspace_id:
            extra["workspace_id"] = workspace_id
        extra["chatgpt_token_source"] = "web_session_login"
        extra.setdefault("auth_level", "access_token_only")

        cleaned_mailbox_state = sanitize_mailbox_state(
            exported_mailbox_state,
            account_email=expected_email,
        )
        if cleaned_mailbox_state:
            extra["chatgpt_mailbox_state"] = cleaned_mailbox_state

        if captured_fingerprint:
            extra["chatgpt_web_session_browser_fingerprint"] = captured_fingerprint
            existing_fingerprint = resolve_account_browser_fingerprint(extra)
            if existing_fingerprint:
                canonical_fingerprint = dict(existing_fingerprint)
            else:
                canonical_fingerprint = build_browser_fingerprint_payload(
                    generate_browser_fingerprint(
                        device_id=captured_fingerprint.get("device_id") or None,
                        accept_language=captured_fingerprint.get("accept_language") or None,
                    )
                )
            if captured_fingerprint.get("device_id") and canonical_fingerprint:
                canonical_fingerprint["device_id"] = captured_fingerprint["device_id"]
            extra = persist_account_browser_fingerprint(
                extra,
                canonical_fingerprint,
                source="web_session_login",
                overwrite=True,
            )
        else:
            extra = persist_account_browser_fingerprint(
                extra,
                source="web_session_login",
                overwrite=False,
            )

        login_payload = _build_login_payload(
            status="success",
            email=expected_email,
            task_id=task_id,
            attempts=attempts,
            account_id=captured_account_id,
            workspace_id=workspace_id,
            mailbox_state=cleaned_mailbox_state,
            browser_fingerprint=captured_fingerprint,
        )
        extra["chatgpt_web_session_login"] = login_payload

        account.token = access_token
        account.user_id = captured_account_id
        account.set_extra(extra)
        prepare_chatgpt_account_for_local_status_refresh(
            account,
            reason="web_session_login:success",
        )
        account.updated_at = _utcnow()
        session.add(account)

        from services.account_filters import upsert_account_list_state_for_account_ids

        upsert_account_list_state_for_account_ids(session, [account.id], commit=False)
        session.commit()
        status = str(account.status or "")

    local_status_refresh_scheduled = True
    try:
        schedule_chatgpt_local_status_refresh_for_account_id(
            int(account_id),
            reason="web_session_login:success",
            proxy=proxy_url or None,
            use_default_proxy=False if proxy_url else True,
            delay_seconds=2.0,
        )
    except Exception:
        local_status_refresh_scheduled = False
    return {
        "status": status,
        "token_saved": True,
        "web_session_complete": True,
        "local_status_refresh_scheduled": local_status_refresh_scheduled,
        "web_session_login": login_payload,
    }


def _persist_login_failure(
    account_id: int,
    *,
    expected_email: str,
    exported_mailbox_state: dict[str, Any],
    task_id: str,
    attempts: int,
    error_code: str,
    error: str,
) -> dict[str, Any]:
    with Session(core_db.engine) as session:
        account = session.get(AccountModel, int(account_id or 0))
        if account is None or account.platform != "chatgpt":
            raise ValueError("ChatGPT 账号不存在")
        if str(account.email or "").strip().casefold() != str(expected_email or "").strip().casefold():
            raise WebSessionIdentityMismatchError("account_identity_mismatch: 登录期间原账号记录已被替换")

        extra = account.get_extra()
        cleaned_mailbox_state = sanitize_mailbox_state(
            exported_mailbox_state,
            account_email=expected_email,
        )
        if cleaned_mailbox_state:
            extra["chatgpt_mailbox_state"] = cleaned_mailbox_state
        payload = _build_login_payload(
            status="failed",
            email=expected_email,
            task_id=task_id,
            attempts=attempts,
            error_code=error_code,
            error=error,
            mailbox_state=cleaned_mailbox_state,
        )
        extra["chatgpt_web_session_login"] = payload
        account.set_extra(extra)
        account.updated_at = _utcnow()
        session.add(account)
        session.commit()
        return payload


def execute_chatgpt_web_session_login(
    account_id: int,
    *,
    retry_delays_seconds: Sequence[int] | None = None,
    log_fn: Callable[[str], None] | None = None,
    stop_checker: Callable[[], None] | None = None,
    task_id: str = "",
    task_control: Any = None,
    attempt_id: int | None = None,
    proxy_url: str | None = None,
    hold_browser: bool = False,
    lease_change_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Log in an existing account and atomically replace only Web Session auth material."""

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
        _invoke_log(log_fn, text, level)

    account_id = int(account_id or 0)
    if account_id <= 0:
        return {"ok": False, "error": "account_id 无效", "data": {"error_code": "invalid_account_id", "logs": action_logs}}

    with Session(core_db.engine) as session:
        account = session.get(AccountModel, account_id)
        if account is None or account.platform != "chatgpt":
            return {"ok": False, "error": "ChatGPT 账号不存在", "data": {"error_code": "account_not_found", "logs": action_logs}}
        email = str(account.email or "").strip()
        password = str(account.password or "")
        status = str(account.status or "")
        extra = account.get_extra()
        mailbox_state = mailbox_state_from_account(account, extra=extra)
        expected_account_id = str(extra.get("account_id") or account.user_id or "").strip()

    for value, error_code, message in (
        (email, "missing_email", "账号邮箱为空，无法执行登录态"),
        (password, "missing_password", "账号密码为空，无法执行登录态"),
        (mailbox_state, "missing_mailbox_state", "mailbox_state 缺失，无法自动获取登录验证码"),
    ):
        if not value:
            return {"ok": False, "error": message, "data": {"message": message, "error_code": error_code, "logs": action_logs}}

    merged_config = config_store.get_all().copy()
    merged_config.update({key: value for key, value in extra.items() if value not in (None, "")})
    merged_config = inject_account_browser_fingerprint(merged_config, extra, overwrite=False)
    merged_config["_current_account_id"] = account_id
    merged_config["_current_account_email"] = email
    merged_config["_current_task_id"] = task_id
    if task_control is not None:
        merged_config["_task_control"] = task_control
        merged_config["_task_attempt_id"] = attempt_id

    browser_mode = str(
        extra.get("browser_mode")
        or merged_config.get("browser_mode")
        or merged_config.get("default_executor")
        or "protocol"
    ).strip().lower() or "protocol"
    retry_delays = _retry_delays_from_config(merged_config, retry_delays_seconds)
    max_attempts = 1 + len(retry_delays)
    exported_mailbox_state = dict(mailbox_state)
    attempts_executed = 0
    last_error = ""
    last_error_code = "login_failed"
    retryable = False
    ready_persisted: dict[str, Any] = {}
    session_lease = None

    if hold_browser:
        from .web_session_lease import web_session_lease_manager

        account_fingerprint = resolve_account_browser_fingerprint(extra) or {}
        session_lease = web_session_lease_manager.create(
            task_id=task_id,
            account_id=account_id,
            email=email,
            cookie_header=str(extra.get("cookies") or extra.get("cookie_header") or ""),
            session_token=str(extra.get("session_token") or extra.get("sessionToken") or ""),
            device_id=str(account_fingerprint.get("device_id") or ""),
            on_change=lease_change_callback,
        )

    _invoke_log(log_fn, f"[执行登录态][{email}] 开始｜原状态={status or '-'}")
    _log(
        "[执行登录态] 身份基线："
        f"账号行ID={account_id}｜ChatGPT账号ID={_masked_identity(expected_account_id)}"
    )
    _log("[执行登录态] 目标：捕获 AccessToken、Session Cookie、完整 Cookie 与账号 ID")

    for attempt in range(1, max_attempts + 1):
        attempts_executed = attempt
        _check_stop()
        if attempt > 1:
            _log(f"[执行登录态] 开始第 {attempt}/{max_attempts} 次登录")
        try:
            _log("[执行登录态] 启动已有账号登录浏览器")

            def _persist_ready_session(
                captured_tokens: dict[str, Any],
                captured_mailbox_state: dict[str, Any],
                reason: str,
            ) -> dict[str, Any]:
                captured_account_id = str(
                    captured_tokens.get("account_id") or ""
                ).strip() or _account_id_from_access_token(
                    str(captured_tokens.get("access_token") or "")
                )
                _log(
                    "[执行登录态] Session 捕获完成｜"
                    f"账号ID={_masked_identity(captured_account_id)}"
                )
                _log("[执行登录态] 正在核对账号身份并写回认证材料")
                persisted_result = _persist_login_success(
                    account_id,
                    expected_email=email,
                    expected_account_id=expected_account_id,
                    tokens=captured_tokens,
                    exported_mailbox_state=captured_mailbox_state,
                    task_id=task_id,
                    attempts=attempt,
                    proxy_url=proxy_url,
                )
                ready_persisted.clear()
                ready_persisted.update(persisted_result)
                refresh_scheduled = bool(
                    persisted_result.get("local_status_refresh_scheduled")
                )
                _log(
                    "[执行登录态] "
                    f"{'保持中同步' if reason == 'refresh' else '写回'}完成｜"
                    "AT=已更新｜Session=已更新｜Cookie材料已更新｜"
                    f"本地状态刷新={'已调度' if refresh_scheduled else '调度失败（不影响登录态成功）'}"
                )
                return dict(persisted_result)

            tokens, exported_mailbox_state = capture_web_session_without_refresh_token(
                email=email,
                password=password,
                exported_mailbox_state=exported_mailbox_state,
                browser_mode=browser_mode,
                log_fn=_log,
                proxy_url=proxy_url,
                task_control=task_control,
                attempt_id=attempt_id,
                stop_checker=stop_checker,
                otp_phase="web_session_login_otp",
                otp_phase_label="执行登录态验证码",
                session_lease=session_lease,
                session_ready_callback=(
                    _persist_ready_session if session_lease is not None else None
                ),
            )
            if ready_persisted:
                persisted = dict(ready_persisted)
                _log("[执行登录态] 浏览器已按人工请求保存并释放，网页会话未注销")
            else:
                persisted = _persist_ready_session(
                    tokens,
                    exported_mailbox_state,
                    "login",
                )
            refresh_scheduled = bool(persisted.get("local_status_refresh_scheduled"))
            return {
                "ok": True,
                "error": "",
                "data": {
                    "message": (
                        "浏览器登录态已保存并按人工请求释放，未执行网页注销"
                        if hold_browser
                        else "执行登录态成功，完整 ChatGPT Web Session 已写回原账号"
                    ),
                    "status": persisted.get("status"),
                    "token_saved": True,
                    "web_session_complete": True,
                    "local_status_refresh_scheduled": refresh_scheduled,
                    "web_session_login": persisted.get("web_session_login") or {},
                    "browser_lease": (
                        session_lease.snapshot() if session_lease is not None else {}
                    ),
                    "logs": list(action_logs),
                },
            }
        except WebSessionLeaseReleaseRequested as exc:
            message = sanitize_error_message(exc or "浏览器已按人工请求释放")
            return {
                "ok": False,
                "error": message,
                "data": {
                    "message": message,
                    "error_code": "browser_lease_released",
                    "retryable": False,
                    "web_session_complete": bool(ready_persisted),
                    "credentials_preserved": bool(ready_persisted),
                    "browser_lease": (
                        session_lease.snapshot() if session_lease is not None else {}
                    ),
                    "logs": list(action_logs),
                },
            }
        except TaskInterruption:
            raise
        except Exception as exc:
            last_error = sanitize_error_message(exc or "执行登录态失败")
            last_error_code, retryable = _classify_login_error(exc if isinstance(exc, WebSessionIdentityMismatchError) else last_error)
            _log(f"[执行登录态] 本次失败｜类型={last_error_code}｜原因={last_error}")
            if attempt >= max_attempts or not retryable:
                break
            delay_seconds = int(retry_delays[attempt - 1])
            if delay_seconds > 0:
                _log(f"[执行登录态] {delay_seconds}s 后重试")
                _interruptible_sleep(delay_seconds, stop_checker)

    if ready_persisted:
        message = sanitize_error_message(
            last_error or "浏览器登录态在保持期间异常中断"
        )
        return {
            "ok": False,
            "error": message,
            "data": {
                "message": message,
                "error_code": "browser_lease_interrupted",
                "retryable": False,
                "web_session_complete": True,
                "credentials_preserved": True,
                "web_session_login": ready_persisted.get("web_session_login") or {},
                "browser_lease": (
                    session_lease.snapshot() if session_lease is not None else {}
                ),
                "logs": list(action_logs),
            },
        }

    if session_lease is not None and str(session_lease.status) not in {
        "failed",
        "interrupted",
        "released",
        "stopped",
    }:
        session_lease.transition("failed", error=last_error)

    failure_payload = _persist_login_failure(
        account_id,
        expected_email=email,
        exported_mailbox_state=exported_mailbox_state,
        task_id=task_id,
        attempts=max(1, attempts_executed),
        error_code=last_error_code,
        error=last_error,
    )
    message = sanitize_error_message(last_error or "执行登录态失败")
    return {
        "ok": False,
        "error": message,
        "data": {
            "message": message,
            "error_code": last_error_code,
            "retryable": retryable,
            "web_session_login": failure_payload,
            "logs": list(action_logs),
        },
    }
