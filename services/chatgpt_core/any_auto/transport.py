"""any-auto registration transport adapters for auto-gpt.

Three executors map 1:1 to any-auto:
- protocol  -> RegistrationEngine (curl_cffi, same-session create + NextAuth AT)
- headless  -> ChatGPTBrowserRegister(headless=True)
- headed    -> ChatGPTBrowserRegister(headless=False)

auto-gpt owns mailbox OTP / inventory save; this module only runs the transport.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from core.task_runtime import SkipCurrentAttemptRequested, TaskInterruption


@dataclass
class AnyAutoRegistrationResult:
    success: bool
    email: str = ""
    password: str = ""
    account_id: str = ""
    workspace_id: str = ""
    access_token: str = ""
    refresh_token: str = ""
    id_token: str = ""
    session_token: str = ""
    cookies: str = ""
    cookie_header: str = ""
    error_message: str = ""
    source: str = "any_auto"
    transport: str = ""
    executor: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        committed_pending = bool(
            self.metadata.get("registration_signup_committed")
            and self.metadata.get("registered_auth_pending")
            and self.metadata.get("session_capture_pending")
        )
        return bool(
            self.success
            and (
                committed_pending
                or (
                    str(self.access_token or "").strip()
                    and str(self.session_token or "").strip()
                    and str(self.cookie_header or self.cookies or "").strip()
                )
            )
        )


class _AnyAutoEmailService:
    """Adapt auto-gpt EmailServiceAdapter / mailbox object to any-auto interface."""

    def __init__(
        self,
        *,
        email: str,
        wait_code: Callable[..., str],
        provider: str = "auto_gpt_mailbox",
        create_email_fn: Optional[Callable[[], dict]] = None,
    ):
        self.service_type = type("ST", (), {"value": provider})()
        self._email = str(email or "").strip()
        self._wait_code = wait_code
        self._create_email_fn = create_email_fn
        self._used_codes: set[str] = set()
        self._phase = 0

    def create_email(self, config=None):
        if callable(self._create_email_fn):
            info = self._create_email_fn() or {}
            email = str(info.get("email") or self._email).strip()
            if email:
                self._email = email
            return {
                "email": self._email,
                "service_id": str(info.get("service_id") or info.get("token") or ""),
                "token": str(info.get("token") or info.get("service_id") or ""),
            }
        if not self._email:
            raise RuntimeError("any-auto protocol registration requires a pre-leased email")
        return {"email": self._email, "service_id": "", "token": ""}

    def get_verification_code(
        self,
        email=None,
        email_id=None,
        timeout=120,
        pattern=None,
        otp_sent_at=None,
        **kwargs,
    ):
        self._phase += 1
        code = self._wait_code(
            email=email or self._email,
            timeout=int(timeout or 120),
            pattern=pattern,
            otp_sent_at=otp_sent_at,
            exclude_codes=set(self._used_codes),
            phase=f"any_auto_otp_{self._phase}",
        )
        code = str(code or "").strip()
        if code:
            self._used_codes.add(code)
        return code

    def update_status(self, success, error=None):
        return None

    @property
    def status(self):
        return None


def _cookies_to_header(cookies: Any) -> str:
    if isinstance(cookies, str):
        return cookies.strip()
    if isinstance(cookies, dict):
        parts = []
        for k, v in cookies.items():
            name = str(k or "").strip()
            if not name:
                continue
            parts.append(f"{name}={v}")
        return "; ".join(parts)
    if isinstance(cookies, list):
        parts = []
        for item in cookies:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            parts.append(f"{name}={item.get('value') or ''}")
        return "; ".join(parts)
    return ""


def _normalize_result(
    *,
    email: str,
    password: str,
    payload: Any,
    executor: str,
    transport: str,
    error: str = "",
) -> AnyAutoRegistrationResult:
    data: dict[str, Any]
    if payload is None:
        data = {}
    elif hasattr(payload, "to_dict") and callable(payload.to_dict):
        # RegistrationResult dataclass from any-auto register.py
        data = {
            "email": getattr(payload, "email", "") or email,
            "password": getattr(payload, "password", "") or password,
            "account_id": getattr(payload, "account_id", "") or "",
            "workspace_id": getattr(payload, "workspace_id", "") or "",
            "access_token": getattr(payload, "access_token", "") or "",
            "refresh_token": getattr(payload, "refresh_token", "") or "",
            "id_token": getattr(payload, "id_token", "") or "",
            "session_token": getattr(payload, "session_token", "") or "",
            "error_message": getattr(payload, "error_message", "") or error,
            "success": bool(getattr(payload, "success", False)),
            "source": getattr(payload, "source", "") or "any_auto",
            "metadata": dict(getattr(payload, "metadata", None) or {}),
            "cookies": "",
        }
    elif isinstance(payload, dict):
        data = dict(payload)
    else:
        data = {}

    metadata = dict(data.get("metadata") or {})
    access_token = str(data.get("access_token") or metadata.get("access_token") or "").strip()
    session_token = str(
        data.get("session_token")
        or data.get("sessionToken")
        or metadata.get("session_token")
        or metadata.get("sessionToken")
        or ""
    ).strip()
    cookies_raw = (
        data.get("cookies")
        or data.get("cookie_header")
        or metadata.get("cookies")
        or metadata.get("cookie_header")
        or ""
    )
    cookie_header = _cookies_to_header(cookies_raw)
    if session_token and "session-token" not in cookie_header:
        # ensure session_token is representable even when cookie map missing
        if cookie_header:
            cookie_header = f"{cookie_header}; __Secure-next-auth.session-token={session_token}"
        else:
            cookie_header = f"__Secure-next-auth.session-token={session_token}"

    committed_pending = bool(
        metadata.get("registration_signup_committed")
        and metadata.get("registered_auth_pending")
        and metadata.get("session_capture_pending")
    )
    success_flag = bool(
        data.get("success", True if access_token or committed_pending else False)
    )
    if (
        not access_token or not session_token or not cookie_header
    ) and not committed_pending:
        success_flag = False

    err = str(
        error
        or data.get("error_message")
        or data.get("error")
        or (
            ""
            if success_flag
            else "any-auto registration failed: incomplete ChatGPT Web Session material"
        )
    ).strip()

    return AnyAutoRegistrationResult(
        success=success_flag,
        email=str(data.get("email") or email or "").strip(),
        password=str(data.get("password") or password or "").strip(),
        account_id=str(data.get("account_id") or data.get("user_id") or "").strip(),
        workspace_id=str(data.get("workspace_id") or "").strip(),
        access_token=access_token,
        refresh_token=str(data.get("refresh_token") or "").strip(),
        id_token=str(data.get("id_token") or "").strip(),
        session_token=session_token,
        cookies=cookie_header,
        cookie_header=cookie_header,
        error_message=err,
        source=str(data.get("source") or "any_auto"),
        transport=transport,
        executor=executor,
        metadata=metadata,
        raw=data if isinstance(data, dict) else {},
    )


def run_any_auto_protocol_registration(
    *,
    email: str,
    password: str,
    proxy_url: Optional[str],
    wait_code: Callable[..., str],
    log_fn: Callable[[str], None] = print,
    provider: str = "auto_gpt_mailbox",
    create_email_fn: Optional[Callable[[], dict]] = None,
    prefer_password: bool = True,
) -> AnyAutoRegistrationResult:
    """protocol executor: any-auto RegistrationEngine end-to-end."""
    from .register import RegistrationEngine

    email_service = _AnyAutoEmailService(
        email=email,
        wait_code=wait_code,
        provider=provider,
        create_email_fn=create_email_fn,
    )
    engine = RegistrationEngine(
        email_service=email_service,
        proxy_url=proxy_url,
        callback_logger=log_fn,
        # RT capture is owned by the mode adapter's second stage. The shared
        # transport stops after GPT signup + ChatGPT Web Session capture.
        capture_codex_oauth=False,
    )
    if prefer_password and password:
        # Prefer the task-assigned password instead of regenerating 3 candidates.
        engine.password = password
        engine._preferred_password = password  # type: ignore[attr-defined]
    engine.email = email
    try:
        raw = engine.run()
    except (TaskInterruption, SkipCurrentAttemptRequested):
        raise
    except Exception as exc:
        return _normalize_result(
            email=email,
            password=password,
            payload=None,
            executor="protocol",
            transport="any_auto_protocol",
            error=f"any_auto_protocol_exception: {exc}",
        )
    return _normalize_result(
        email=email,
        password=password or getattr(raw, "password", "") or "",
        payload=raw,
        executor="protocol",
        transport="any_auto_protocol",
    )


def run_any_auto_browser_registration(
    *,
    email: str,
    password: str,
    proxy_url: Optional[str],
    headless: bool,
    otp_callback: Callable[[], str],
    log_fn: Callable[[str], None] = print,
    phone_callback: Optional[Callable[[], str]] = None,
    profile_name: str = "",
    profile_birthdate: str = "",
    stop_check: Optional[Callable[[], None]] = None,
    login_only: bool = False,
    session_lease: Any = None,
    session_ready_callback: Optional[
        Callable[[AnyAutoRegistrationResult, str], Any]
    ] = None,
) -> AnyAutoRegistrationResult:
    """headless/headed executor: any-auto ChatGPTBrowserRegister."""
    from .browser_register import ChatGPTBrowserRegister

    executor = "headless" if headless else "headed"

    def _publish_session_material(payload: dict[str, Any], reason: str) -> Any:
        normalized = _normalize_result(
            email=email,
            password=password,
            payload=payload,
            executor=executor,
            transport="any_auto_browser",
        )
        if not normalized.ok:
            raise RuntimeError(
                normalized.error_message
                or "登录完成但 ChatGPT Web Session 材料不完整"
            )
        if callable(session_ready_callback):
            return session_ready_callback(normalized, reason)
        return {}

    worker = ChatGPTBrowserRegister(
        headless=headless,
        proxy=proxy_url,
        otp_callback=otp_callback,
        phone_callback=phone_callback,
        profile_name=profile_name,
        profile_birthdate=profile_birthdate,
        stop_check=stop_check,
        login_only=login_only,
        log_fn=log_fn,
        session_lease=session_lease,
        session_ready_callback=(
            _publish_session_material if session_lease is not None else None
        ),
    )
    try:
        raw = worker.run(email=email, password=password)
    except (TaskInterruption, SkipCurrentAttemptRequested):
        raise
    except Exception as exc:
        return _normalize_result(
            email=email,
            password=password,
            payload=None,
            executor=executor,
            transport="any_auto_browser",
            error=f"any_auto_browser_exception: {exc}",
        )
    return _normalize_result(
        email=email,
        password=password,
        payload=raw,
        executor=executor,
        transport="any_auto_browser",
    )
