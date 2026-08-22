"""ChatGPT primary-email change state machine.

The OpenAI email-change endpoints are browser-session operations.  This module
owns the durable business boundary around them: a prepared target mailbox is
frozen before the task starts, the two OTP phases are isolated, and the local
account row is updated only after the target session proves the original
ChatGPT identity.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Callable, Iterator

from sqlmodel import Session, select

from core.base_mailbox import MailboxAccount, create_mailbox
from core.config_store import config_store
from core.db import (
    AccountModel,
    ChatGPTEmailChangeModel,
    engine,
)
from core.task_runtime import TaskInterruption

from .account_fingerprint import build_browser_fingerprint_payload
from .auth_lifecycle import apply_material_capture
from .local_status_refresh import schedule_chatgpt_local_status_refresh_for_account_id
from .mailbox_state import mailbox_state_summary, normalize_mailbox_provider, sanitize_mailbox_state
from .restored_email_service import RestoredEmailService, mailbox_state_from_account
from .task_logging import sanitize_error_message
from .utils import decode_jwt_payload


CHATGPT_EMAIL_CHANGE_SOURCE = "chatgpt_email_change"

PHASE_CREATED = "created"
PHASE_SOURCE_REAUTH_REQUIRED = "source_reauth_required"
PHASE_ELIGIBILITY_CHECKED = "eligibility_checked"
PHASE_BEGIN_SENT = "begin_sent"
PHASE_WAITING_CHANGE_OTP = "waiting_change_otp"
PHASE_REMOTE_EMAIL_CHANGED = "remote_email_changed"
PHASE_WAITING_TARGET_LOGIN_OTP = "waiting_target_login_otp"
PHASE_SESSION_CAPTURED = "session_captured"
PHASE_IDENTITY_VERIFIED = "identity_verified"
PHASE_COMMITTED = "committed"
PHASE_RECOVERY_REQUIRED = "recovery_required"
PHASE_RATE_LIMITED = "rate_limited"
PHASE_RELEASED = "released"

STATUS_CREATED = "created"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_PARTIAL = "partial"
STATUS_RELEASING = "releasing"
STATUS_RELEASED = "released"

TARGET_PROVIDER_HME = "hme_ready_api"
TARGET_PROVIDER_TEMPMAIL = "tempmail_local"
TARGET_PROVIDER_MANUAL = "manual_email_otp"
TARGET_PROVIDER_VALUES = {
    TARGET_PROVIDER_HME,
    TARGET_PROVIDER_TEMPMAIL,
    TARGET_PROVIDER_MANUAL,
}

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class EmailChangeError(RuntimeError):
    """A classified, safe-to-display email-change failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        phase: str = "",
        retryable: bool = False,
        remote_changed: bool = False,
    ) -> None:
        self.code = str(code or "email_change_failed").strip().lower()
        self.phase = str(phase or "").strip()
        self.retryable = bool(retryable)
        self.remote_changed = bool(remote_changed)
        self.message = sanitize_error_message(str(message or self.code))[:1000]
        super().__init__(self.message)


class EmailChangeIdentityMismatch(EmailChangeError):
    def __init__(
        self,
        message: str,
        *,
        phase: str = PHASE_IDENTITY_VERIFIED,
        remote_changed: bool = True,
    ) -> None:
        super().__init__(
            "identity_mismatch",
            message,
            phase=phase,
            remote_changed=remote_changed,
        )


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_email(value: Any) -> str:
    return str(value or "").strip().lower()


def validate_email(value: Any) -> str:
    normalized = normalize_email(value)
    if not normalized or not EMAIL_RE.match(normalized) or len(normalized) > 320:
        raise ValueError("目标邮箱格式不合法")
    return normalized


def _safe_mailbox_state(state: dict[str, Any] | None, *, email: str = "") -> dict[str, Any]:
    cleaned = sanitize_mailbox_state(dict(state or {}), account_email=email)
    if not isinstance(cleaned, dict):
        return {}
    # ``before_ids`` is a polling cursor, never an unbounded message archive.
    before_ids = cleaned.get("before_ids")
    if isinstance(before_ids, (list, tuple, set)):
        cleaned["before_ids"] = [str(item)[:256] for item in list(before_ids)[-128:] if str(item or "").strip()]
    return cleaned


def _config_snapshot(provider: str, *, target_domain: str = "") -> dict[str, Any]:
    raw = config_store.get_all() or {}
    normalized = normalize_mailbox_provider(provider)
    if normalized == TARGET_PROVIDER_HME:
        keys = {
            "icloud_hme_mode",
            "icloud_forward_to",
            "icloud_hme_helper_api_url",
            "icloud_hme_helper_internal_key",
            "icloud_hme_helper_api_key",
            "icloud_hme_helper_api_key_header",
            "icloud_hme_helper_header",
            "icloud_hme_helper_consumer",
            "icloud_hme_helper_checkout_ttl_seconds",
            "icloud_hme_helper_wait_timeout_seconds",
            "icloud_hme_helper_max_cache_age_seconds",
            "tempmail_api_url",
            "tempmail_api_key",
            "tempmail_api_key_header",
            "tempmail_wait_timeout_seconds",
            "tempmail_proxy",
            "tempmail_api_proxy",
            "tempmail_use_task_proxy",
        }
        result = {key: raw.get(key) for key in keys if raw.get(key) not in (None, "")}
        result["icloud_hme_mode"] = "helper_ready_api"
        return result

    if normalized == TARGET_PROVIDER_TEMPMAIL:
        keys = {
            "tempmail_api_url",
            "tempmail_api_key",
            "tempmail_api_key_header",
            "tempmail_primary_domain",
            "tempmail_fixed_domains",
            "tempmail_mode",
            "tempmail_wait_timeout_seconds",
            "tempmail_ttl_minutes",
            "tempmail_reuse_window_minutes",
            "tempmail_permanent",
            "tempmail_platform",
            "tempmail_proxy",
            "tempmail_api_proxy",
            "tempmail_use_task_proxy",
        }
        result = {key: raw.get(key) for key in keys if raw.get(key) not in (None, "")}
        result["tempmail_mode"] = "fixed_domain"
        # A domain selected by the operator becomes a frozen creation
        # constraint.  ``tempmail_preferred_domains`` is intentionally ignored.
        if target_domain:
            result["tempmail_fixed_domains"] = target_domain
        return result

    return {"manual_email_address": "", "mail_provider": TARGET_PROVIDER_MANUAL}


def _mailbox_state_from_account(
    provider: str,
    account: MailboxAccount,
    config: dict[str, Any],
    *,
    before_ids: set[str] | None = None,
) -> dict[str, Any]:
    email = normalize_email(getattr(account, "email", ""))
    state = {
        "provider": normalize_mailbox_provider(provider),
        "email": email,
        "account": {
            "email": email,
            "account_id": str(getattr(account, "account_id", "") or ""),
            "extra": dict(getattr(account, "extra", None) or {}),
        },
        "before_ids": sorted(str(item) for item in (before_ids or set()) if str(item or "").strip()),
        "config": dict(config or {}),
        "proxy": "",
        "email_change_target": True,
    }
    return _safe_mailbox_state(state, email=email)


def _extract_nested_email(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("email", "email_address", "emailAddress", "username", "address"):
            value = normalize_email(payload.get(key))
            if value and "@" in value:
                return value
        for value in payload.values():
            found = _extract_nested_email(value)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _extract_nested_email(value)
            if found:
                return found
    return ""


def extract_identity(payload: Any) -> dict[str, str]:
    payload = payload if isinstance(payload, dict) else {}
    auth = payload.get("https://api.openai.com/auth")
    auth = auth if isinstance(auth, dict) else {}
    user_id = str(payload.get("user_id") or payload.get("userId") or "").strip()
    if not user_id and str(payload.get("object") or "").strip().lower() == "user":
        user_id = str(payload.get("id") or "").strip()

    organization_id = str(
        payload.get("organization_id")
        or payload.get("organizationId")
        or payload.get("org_id")
        or payload.get("orgId")
        or ""
    ).strip()
    orgs = payload.get("orgs")
    org_items = orgs.get("data") if isinstance(orgs, dict) else []
    if not organization_id and isinstance(org_items, list):
        selected = next(
            (
                item
                for item in org_items
                if isinstance(item, dict) and item.get("is_default") is True
            ),
            None,
        )
        if selected is None:
            selected = next((item for item in org_items if isinstance(item, dict)), None)
        if isinstance(selected, dict):
            organization_id = str(selected.get("id") or "").strip()

    return {
        "email": normalize_email(payload.get("email") or _extract_nested_email(payload)),
        "user_id": user_id,
        "account_id": str(
            auth.get("chatgpt_account_id")
            or payload.get("chatgpt_account_id")
            or payload.get("chatgptAccountId")
            or payload.get("workspace_id")
            or payload.get("workspaceId")
            or ""
        ).strip(),
        "organization_id": organization_id,
    }


def build_begin_payload(target_email: str, *, remove_social_subs: bool = False) -> dict[str, Any]:
    """Build the exact upstream body without inventing optional behavior."""

    payload: dict[str, Any] = {"email": validate_email(target_email)}
    if remove_social_subs is True:
        payload["remove_social_subs"] = True
    return payload


def _cookie_header_from_extra(extra: dict[str, Any]) -> str:
    header = str(extra.get("cookies") or extra.get("cookie_header") or "").strip()
    if header:
        return header
    token = str(extra.get("session_token") or extra.get("sessionToken") or "").strip()
    if not token:
        return ""
    return (
        f"__Secure-next-auth.session-token={token}; "
        f"__Secure-authjs.session-token={token}"
    )


def _playwright_cookies(header: str) -> list[dict[str, Any]]:
    cookies: list[dict[str, Any]] = []
    for raw in str(header or "").split(";"):
        name, separator, value = raw.strip().partition("=")
        if not separator or not name.strip():
            continue
        cookies.append(
            {
                "name": name.strip(),
                "value": value.strip(),
                "domain": ".chatgpt.com",
                "path": "/",
                "secure": True,
            }
        )
    return cookies


def _browser_identity(page) -> tuple[str, str]:
    try:
        user_agent = str(page.evaluate("() => navigator.userAgent") or "").strip()
    except Exception:
        user_agent = ""
    device_id = ""
    try:
        for item in list(page.context.cookies() or []):
            if str(item.get("name") or "").strip() == "oai-did":
                device_id = str(item.get("value") or "").strip()
                break
    except Exception:
        pass
    return user_agent, device_id


def _classify_http_error(response: dict[str, Any], *, phase: str, remote_changed: bool = False) -> EmailChangeError:
    status = int(response.get("status") or 0)
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    text = str(response.get("text") or "")[:500]
    code = str(
        data.get("error_code")
        or data.get("code")
        or data.get("error")
        or data.get("type")
        or ""
    ).strip().lower().replace("-", "_")
    lowered = f"{code} {text}".lower()
    if "reauth_required" in lowered or "reauth" in lowered:
        return EmailChangeError("reauth_required", "OpenAI 要求源账号重新认证", phase=PHASE_SOURCE_REAUTH_REQUIRED, remote_changed=remote_changed)
    if "email_change_rate_limited" in lowered or status == 429:
        return EmailChangeError("email_change_rate_limited", "邮箱换绑请求被限流，请稍后重试", phase=PHASE_RATE_LIMITED, retryable=True, remote_changed=remote_changed)
    if "email_change_limit_reached" in lowered:
        return EmailChangeError("email_change_limit_reached", "该账号已达到邮箱换绑次数上限", phase=phase, remote_changed=remote_changed)
    if status in {401, 403}:
        return EmailChangeError("source_session_unauthorized", "源账号网页会话已失效或无权执行邮箱换绑", phase=phase, remote_changed=remote_changed)
    if status == 422:
        return EmailChangeError("invalid_request", "OpenAI 拒绝了邮箱换绑请求", phase=phase, remote_changed=remote_changed)
    return EmailChangeError(
        code or "openai_request_failed",
        f"OpenAI 邮箱换绑请求失败（HTTP {status or 'network'}）",
        phase=phase,
        retryable=status in {0, 408, 425, 500, 502, 503, 504},
        remote_changed=remote_changed,
    )


class ChatGPTEmailChangeService:
    """Execute one frozen target mailbox against one original account row."""

    def __init__(
        self,
        *,
        task_id: str,
        row_id: int,
        control: Any = None,
        attempt_id: int | None = None,
        log_fn: Callable[[str], None] | None = None,
        phase_fn: Callable[[str, str, dict[str, Any] | None], None] | None = None,
        proxy_url: str | None = None,
    ) -> None:
        self.task_id = str(task_id or "").strip()
        self.row_id = int(row_id or 0)
        self.control = control
        self.attempt_id = attempt_id
        self.log_fn = log_fn
        self.phase_fn = phase_fn
        self.proxy_url = str(proxy_url or "").strip() or None
        self.remote_changed = False

    def _log(self, message: str) -> None:
        if callable(self.log_fn):
            self.log_fn(str(message or ""))

    def _checkpoint(self) -> None:
        if self.control is not None:
            self.control.checkpoint(attempt_id=self.attempt_id)

    def _load_row(self) -> ChatGPTEmailChangeModel:
        with Session(engine) as session:
            row = session.get(ChatGPTEmailChangeModel, self.row_id)
            if row is None:
                raise EmailChangeError("task_record_missing", "邮箱换绑持久化记录不存在")
            session.expunge(row)
            return row

    def _update(
        self,
        *,
        phase: str | None = None,
        status: str | None = None,
        patch: dict[str, Any] | None = None,
        mailbox_state: dict[str, Any] | None = None,
        error: EmailChangeError | None = None,
        resumable: bool | None = None,
    ) -> ChatGPTEmailChangeModel:
        updates = dict(patch or {})
        if phase is not None:
            updates["phase"] = phase
        if status is not None:
            updates["status"] = status
        if error is not None:
            updates.update(
                {
                    "error_code": error.code,
                    "sanitized_error": error.message,
                }
            )
        if resumable is not None:
            updates["resumable"] = bool(resumable)
        updates["updated_at"] = datetime.now(timezone.utc)
        with Session(engine) as session:
            row = session.get(ChatGPTEmailChangeModel, self.row_id)
            if row is None:
                raise EmailChangeError("task_record_missing", "邮箱换绑持久化记录不存在")
            for key, value in updates.items():
                if hasattr(row, key) and key not in {"id", "created_at"}:
                    setattr(row, key, value)
            if mailbox_state is not None:
                row.set_mailbox_state(_safe_mailbox_state(mailbox_state, email=row.target_email))
            session.add(row)
            session.commit()
            session.refresh(row)
            session.expunge(row)
        if phase is not None:
            safe = {
                "account_id": int(row.account_id or 0),
                "target_email": row.target_email,
                "phase": row.phase,
                "provider": row.target_mailbox_provider,
                "resumable": bool(row.resumable),
            }
            if error is not None:
                safe["error_code"] = error.code
            if callable(self.phase_fn):
                self.phase_fn(phase, str(error.message if error else ""), safe)
        return row

    @staticmethod
    def _local_identity(account: Any) -> dict[str, str]:
        extra = account.get_extra()
        token_payload = decode_jwt_payload(
            str(extra.get("access_token") or extra.get("accessToken") or account.token or "")
        )
        auth = token_payload.get("https://api.openai.com/auth") if isinstance(token_payload, dict) else {}
        auth = auth if isinstance(auth, dict) else {}
        token_user_id = str(token_payload.get("sub") or "").strip()
        stored_user_id = str(account.user_id or "").strip()
        if stored_user_id and not stored_user_id.lower().startswith(("user-", "user_")):
            stored_user_id = ""
        return {
            "email": normalize_email(account.email),
            "user_id": token_user_id or stored_user_id,
            "account_id": str(
                auth.get("chatgpt_account_id")
                or extra.get("account_id")
                or ""
            ).strip(),
            "organization_id": str(
                extra.get("organization_id")
                or extra.get("default_organization_id")
                or ""
            ).strip(),
        }

    @classmethod
    def _source_identity(cls, account: Any, me_payload: dict[str, Any]) -> dict[str, str]:
        local = cls._local_identity(account)
        remote = extract_identity(me_payload)
        return {
            "email": normalize_email(remote.get("email") or local.get("email")),
            "user_id": str(remote.get("user_id") or local.get("user_id") or "").strip(),
            "account_id": str(local.get("account_id") or remote.get("account_id") or "").strip(),
            "organization_id": str(
                remote.get("organization_id") or local.get("organization_id") or ""
            ).strip(),
        }

    @classmethod
    def _source_identity_from_row(
        cls,
        row: ChatGPTEmailChangeModel,
        account: Any,
    ) -> dict[str, str]:
        local = cls._local_identity(account)
        return {
            "email": normalize_email(row.source_email or local.get("email")),
            "user_id": str(row.source_chatgpt_user_id or local.get("user_id") or "").strip(),
            "account_id": str(
                row.source_chatgpt_account_id or local.get("account_id") or ""
            ).strip(),
            "organization_id": str(
                row.source_organization_id or local.get("organization_id") or ""
            ).strip(),
        }

    @staticmethod
    def _assert_source_identity(
        actual: dict[str, str],
        expected: dict[str, str],
        *,
        source_email: str,
    ) -> None:
        actual_email = normalize_email(actual.get("email"))
        actual_user_id = str(actual.get("user_id") or "").strip()
        actual_account_id = str(actual.get("account_id") or "").strip()
        if not actual_email or actual_email != normalize_email(source_email):
            raise EmailChangeIdentityMismatch(
                "源网页会话邮箱与本地账号不一致",
                phase=PHASE_ELIGIBILITY_CHECKED,
                remote_changed=False,
            )
        if not actual_user_id:
            raise EmailChangeError(
                "source_user_id_missing",
                "源网页会话未返回用户 ID",
                phase=PHASE_SOURCE_REAUTH_REQUIRED,
                retryable=True,
            )
        expected_user_id = str(expected.get("user_id") or "").strip()
        if expected_user_id and actual_user_id != expected_user_id:
            raise EmailChangeIdentityMismatch(
                "源网页会话用户 ID 与原账号不一致",
                phase=PHASE_ELIGIBILITY_CHECKED,
                remote_changed=False,
            )
        if not actual_account_id:
            raise EmailChangeError(
                "source_account_id_missing",
                "源账号缺少可校验的 ChatGPT 账号 ID，需要重新认证",
                phase=PHASE_SOURCE_REAUTH_REQUIRED,
                retryable=True,
            )
        expected_account_id = str(expected.get("account_id") or "").strip()
        if expected_account_id and actual_account_id != expected_account_id:
            raise EmailChangeIdentityMismatch(
                "源网页会话 ChatGPT 账号 ID 与原账号不一致",
                phase=PHASE_ELIGIBILITY_CHECKED,
                remote_changed=False,
            )
        expected_organization_id = str(expected.get("organization_id") or "").strip()
        actual_organization_id = str(actual.get("organization_id") or "").strip()
        if (
            expected_organization_id
            and actual_organization_id
            and actual_organization_id != expected_organization_id
        ):
            raise EmailChangeIdentityMismatch(
                "源网页会话默认组织与原账号不一致",
                phase=PHASE_ELIGIBILITY_CHECKED,
                remote_changed=False,
            )

    @contextmanager
    def _source_browser(self, account: AccountModel) -> Iterator[Any]:
        from .any_auto.browser_register import _browser_fetch, _build_browser_headers, _build_browser_sentinel_token
        from .shared_camoufox import shared_camoufox_registration_session

        extra = account.get_extra()
        cookie_header = _cookie_header_from_extra(extra)
        if not cookie_header:
            raise EmailChangeError("source_session_missing", "源账号缺少网页 Cookie 会话")
        with shared_camoufox_registration_session(
            headless=True,
            proxy=self.proxy_url,
            browser_fingerprint=extra.get("chatgpt_browser_fingerprint"),
            logger=self._log,
        ) as browser_session:
            context = browser_session.context
            page = browser_session.page
            cookies = _playwright_cookies(cookie_header)
            if cookies:
                context.add_cookies(cookies)
            try:
                page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=30000)
            except Exception as exc:
                self._log(f"[邮箱换绑] ChatGPT 页面预热失败，继续调用 API: {type(exc).__name__}")

            def request(method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
                self._checkpoint()
                ua, device_id = _browser_identity(page)
                headers = _build_browser_headers(
                    user_agent=ua,
                    accept="application/json",
                    referer="https://chatgpt.com/",
                    origin="https://chatgpt.com",
                    content_type="application/json" if method.upper() != "GET" else "",
                    extra_headers={"oai-device-id": device_id} if device_id else {},
                )
                access_token = str(extra.get("access_token") or extra.get("accessToken") or account.token or "").strip()
                if access_token:
                    headers["authorization"] = f"Bearer {access_token}"
                if method.upper() != "GET":
                    try:
                        sentinel = _build_browser_sentinel_token(page, device_id, "email_change", ua)
                    except Exception:
                        sentinel = ""
                    if sentinel:
                        headers["openai-sentinel-token"] = sentinel
                response = _browser_fetch(
                    page,
                    f"https://chatgpt.com{path}",
                    method=method,
                    headers=headers,
                    body=json.dumps(payload, ensure_ascii=False) if payload is not None else None,
                    redirect="follow",
                )
                return response

            yield page, request

    def _wait_for_otp(
        self,
        state: dict[str, Any],
        *,
        email: str,
        phase: str,
        label: str,
        sent_at: float,
    ) -> tuple[str, str, dict[str, Any]]:
        service = RestoredEmailService(
            state=state,
            log_fn=self._log,
            task_control=self.control,
            attempt_id=self.attempt_id,
        )
        self._checkpoint()
        code = service.get_verification_code(
            email=email,
            timeout=300,
            otp_sent_at=sent_at,
            phase=phase,
            phase_label=label,
        )
        code = str(code or "").strip()
        if not code:
            raise EmailChangeError("otp_timeout", f"{label}未获取到验证码", phase=phase, remote_changed=self.remote_changed)
        meta = dict(getattr(service, "_last_verification_result", None) or {})
        message_id = str(meta.get("message_id") or "").strip()
        if message_id:
            service.mark_verification_message_processed(message_id)
        return code, message_id, service.export_state()

    @staticmethod
    def _detached_account(account: AccountModel) -> Any:
        extra = dict(account.get_extra())
        return SimpleNamespace(
            id=int(account.id or 0),
            platform=str(account.platform or ""),
            email=normalize_email(account.email),
            password=str(account.password or ""),
            user_id=str(account.user_id or ""),
            token=str(account.token or extra.get("access_token") or ""),
            get_extra=lambda captured=extra: dict(captured),
        )

    def _persist_source_session(
        self,
        *,
        account_id: int,
        source_email: str,
        identity: dict[str, str],
        tokens: dict[str, Any],
        mailbox_state: dict[str, Any],
    ) -> Any:
        access_token = str(tokens.get("access_token") or "").strip()
        session_token = str(tokens.get("session_token") or "").strip()
        cookie_header = str(tokens.get("cookie_header") or tokens.get("cookies") or "").strip()
        captured_account_id = str(identity.get("account_id") or "").strip()
        if not access_token or not session_token or not cookie_header or not captured_account_id:
            raise EmailChangeError(
                "source_reauth_incomplete",
                "源账号重新认证成功但网页会话材料不完整",
                phase=PHASE_SOURCE_REAUTH_REQUIRED,
                retryable=True,
            )
        with Session(engine) as session:
            row = session.get(AccountModel, int(account_id or 0))
            if row is None or row.platform != "chatgpt":
                raise EmailChangeError("account_missing", "原 ChatGPT 账号不存在")
            if normalize_email(row.email) != normalize_email(source_email):
                raise EmailChangeError("source_row_changed", "源账号记录在重新认证期间已变化")
            extra = row.get_extra()
            existing_account_id = str(extra.get("account_id") or "").strip()
            if existing_account_id and existing_account_id != captured_account_id:
                raise EmailChangeIdentityMismatch(
                    "源账号重新认证捕获了另一个 ChatGPT 账号",
                    phase=PHASE_SOURCE_REAUTH_REQUIRED,
                    remote_changed=False,
                )
            row.token = access_token
            row.user_id = str(identity.get("user_id") or row.user_id or "").strip()
            extra["access_token"] = access_token
            extra["session_token"] = session_token
            extra["cookies"] = cookie_header
            extra["cookie_header"] = cookie_header
            extra["account_id"] = captured_account_id
            if identity.get("organization_id"):
                extra["organization_id"] = str(identity["organization_id"])
            if tokens.get("workspace_id"):
                extra["workspace_id"] = str(tokens["workspace_id"])
            extra["chatgpt_mailbox_state"] = _safe_mailbox_state(
                mailbox_state,
                email=source_email,
            )
            extra["chatgpt_token_source"] = f"{CHATGPT_EMAIL_CHANGE_SOURCE}_source_reauth"
            fingerprint = build_browser_fingerprint_payload(tokens.get("browser_fingerprint"))
            if fingerprint:
                extra["chatgpt_browser_fingerprint"] = fingerprint
            row.set_extra(extra)
            row.updated_at = datetime.now(timezone.utc)
            apply_material_capture(
                session,
                row,
                extra=extra,
                web_session_expires_at=tokens.get("web_session_expires_at"),
                operation=f"{CHATGPT_EMAIL_CHANGE_SOURCE}_source_reauth",
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return self._detached_account(row)

    def _reauth_source(
        self,
        account: Any,
        source_mailbox_state: dict[str, Any],
        expected_identity: dict[str, str],
    ) -> tuple[Any, dict[str, Any], dict[str, str]]:
        from .web_session_login import capture_web_session_without_refresh_token

        source_email = normalize_email(account.email)
        state = _safe_mailbox_state(source_mailbox_state, email=source_email)
        if not state or normalize_email(state.get("email")) != source_email:
            raise EmailChangeError(
                "source_mailbox_missing",
                "源账号缺少可恢复的邮箱收码通道，无法重新认证",
                phase=PHASE_SOURCE_REAUTH_REQUIRED,
            )
        self._update(
            phase=PHASE_SOURCE_REAUTH_REQUIRED,
            status=STATUS_RUNNING,
            resumable=True,
        )
        self._log(f"[邮箱换绑] 源网页会话需要重新认证：{source_email}")
        try:
            tokens, exported_state = capture_web_session_without_refresh_token(
                email=source_email,
                password=str(account.password or ""),
                exported_mailbox_state=state,
                browser_mode="headless",
                log_fn=self._log,
                proxy_url=self.proxy_url,
                task_control=self.control,
                attempt_id=self.attempt_id,
                stop_checker=self._checkpoint,
                otp_phase="chatgpt_email_change_source_reauth_otp",
                otp_phase_label="源账号重新认证验证码",
                browser_fingerprint=account.get_extra().get("chatgpt_browser_fingerprint"),
            )
            verified = self._verify_remote_identity(
                tokens,
                expected_email=source_email,
                expected_user_id=str(expected_identity.get("user_id") or ""),
                expected_account_id=str(expected_identity.get("account_id") or ""),
                expected_organization_id=str(expected_identity.get("organization_id") or ""),
                context_label="源账号重新认证",
                remote_changed=False,
            )
        except TaskInterruption:
            raise
        except EmailChangeError:
            raise
        except Exception as exc:
            raise EmailChangeError(
                "source_reauth_failed",
                f"源账号重新认证失败：{sanitize_error_message(str(exc))}",
                phase=PHASE_SOURCE_REAUTH_REQUIRED,
                retryable=True,
            ) from exc
        exported_state = _safe_mailbox_state(exported_state, email=source_email)
        refreshed = self._persist_source_session(
            account_id=int(account.id or 0),
            source_email=source_email,
            identity=verified,
            tokens=tokens,
            mailbox_state=exported_state,
        )
        self._update(
            phase=PHASE_SOURCE_REAUTH_REQUIRED,
            status=STATUS_RUNNING,
            patch={
                "source_reauth_at": utc_iso(),
                "source_chatgpt_user_id": verified.get("user_id") or "",
                "source_chatgpt_account_id": verified.get("account_id") or "",
                "source_organization_id": verified.get("organization_id") or "",
            },
            resumable=False,
        )
        return refreshed, exported_state, verified

    def _prepare_target_login(
        self,
        account: AccountModel,
        target_state: dict[str, Any],
        source_identity: dict[str, str],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        from .web_session_login import capture_web_session_without_refresh_token

        target_email = normalize_email(target_state.get("email"))
        if not target_email:
            raise EmailChangeError("target_mailbox_missing", "目标邮箱状态缺少地址", remote_changed=True)
        self._update(phase=PHASE_WAITING_TARGET_LOGIN_OTP, status=STATUS_RUNNING)
        self._log(f"[邮箱换绑] 远端邮箱已变更，开始登录目标邮箱：{target_email}")
        extra = account.get_extra()
        password = str(account.password or "")
        previous_ids = {
            str(item)
            for item in (target_state.get("before_ids") or [])
            if str(item or "").strip()
        }
        self._update(
            phase=PHASE_WAITING_TARGET_LOGIN_OTP,
            status=STATUS_RUNNING,
            patch={"target_login_otp_sent_at": utc_iso()},
            mailbox_state=target_state,
        )
        try:
            tokens, exported_state = capture_web_session_without_refresh_token(
                email=target_email,
                password=password,
                exported_mailbox_state=target_state,
                browser_mode="headless",
                log_fn=self._log,
                proxy_url=self.proxy_url,
                task_control=self.control,
                attempt_id=self.attempt_id,
                stop_checker=self._checkpoint,
                otp_phase="chatgpt_email_change_target_login_otp",
                otp_phase_label="新邮箱登录验证码",
                browser_fingerprint=extra.get("chatgpt_browser_fingerprint"),
            )
        except TaskInterruption:
            raise
        except EmailChangeError:
            raise
        except Exception as exc:
            raise EmailChangeError(
                "target_login_failed",
                f"目标邮箱登录失败：{sanitize_error_message(str(exc))}",
                phase=PHASE_WAITING_TARGET_LOGIN_OTP,
                retryable=True,
                remote_changed=True,
            ) from exc
        if not tokens.get("access_token") or not tokens.get("session_token") or not tokens.get("cookie_header"):
            raise EmailChangeError("target_login_incomplete", "目标邮箱登录成功但网页会话材料不完整", remote_changed=True)
        exported_state = _safe_mailbox_state(exported_state, email=target_email)
        consumed_ids = [
            str(item)
            for item in (exported_state.get("before_ids") or [])
            if str(item or "").strip() and str(item) not in previous_ids
        ]
        self._update(
            phase=PHASE_SESSION_CAPTURED,
            status=STATUS_RUNNING,
            patch={
                "session_captured_at": utc_iso(),
                "target_login_otp_message_id": consumed_ids[-1] if consumed_ids else "",
            },
            mailbox_state=exported_state,
        )
        return tokens, exported_state

    @staticmethod
    def _target_identity(tokens: dict[str, Any], target_email: str) -> dict[str, str]:
        access_token = str(tokens.get("access_token") or "").strip()
        payload = decode_jwt_payload(access_token)
        auth = payload.get("https://api.openai.com/auth") if isinstance(payload, dict) else {}
        auth = auth if isinstance(auth, dict) else {}
        return {
            "email": normalize_email(tokens.get("email") or target_email),
            "user_id": str(payload.get("sub") or "").strip() if isinstance(payload, dict) else "",
            "account_id": str(
                auth.get("chatgpt_account_id")
                or tokens.get("account_id")
                or ""
            ).strip(),
            "organization_id": str(tokens.get("workspace_id") or "").strip(),
        }

    def _verify_remote_identity(
        self,
        tokens: dict[str, Any],
        *,
        expected_email: str,
        expected_user_id: str,
        expected_account_id: str,
        expected_organization_id: str,
        context_label: str,
        remote_changed: bool,
    ) -> dict[str, str]:
        """Verify the captured session against ChatGPT's authoritative ``/me``."""

        import requests

        access_token = str(tokens.get("access_token") or "").strip()
        cookie_header = str(tokens.get("cookie_header") or tokens.get("cookies") or "").strip()
        headers = {
            "accept": "application/json",
            "authorization": f"Bearer {access_token}",
            "user-agent": str(
                (tokens.get("browser_fingerprint") or {}).get("user_agent")
                if isinstance(tokens.get("browser_fingerprint"), dict)
                else ""
            )
            or "Mozilla/5.0",
        }
        if cookie_header:
            headers["cookie"] = cookie_header
        try:
            from core.proxy_utils import build_requests_proxy_config

            proxies = build_requests_proxy_config(self.proxy_url)
        except Exception:
            proxies = None
        try:
            response = requests.get(
                "https://chatgpt.com/backend-api/me",
                headers=headers,
                proxies=proxies,
                timeout=25,
            )
        except Exception as exc:
            raise EmailChangeError(
                "identity_probe_failed",
                f"{context_label}后的账号身份校验请求失败",
                phase=PHASE_SESSION_CAPTURED,
                remote_changed=remote_changed,
                retryable=True,
            ) from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise EmailChangeError(
                "identity_probe_failed",
                f"{context_label}后的账号身份校验未通过",
                phase=PHASE_SESSION_CAPTURED,
                remote_changed=remote_changed,
                retryable=response.status_code in {408, 429, 500, 502, 503, 504},
            )
        try:
            payload = response.json()
        except Exception as exc:
            raise EmailChangeError(
                "identity_probe_failed",
                f"{context_label}账号身份校验返回格式异常",
                phase=PHASE_SESSION_CAPTURED,
                remote_changed=remote_changed,
            ) from exc
        remote = extract_identity(payload)
        token_identity = self._target_identity(tokens, expected_email)
        actual_email = normalize_email(remote.get("email"))
        actual_user_id = str(remote.get("user_id") or "").strip()
        token_user_id = str(token_identity.get("user_id") or "").strip()
        actual_account_id = str(token_identity.get("account_id") or "").strip()
        captured_account_id = str(tokens.get("account_id") or "").strip()
        actual_organization_id = str(remote.get("organization_id") or "").strip()
        mismatch_kwargs = {
            "phase": PHASE_IDENTITY_VERIFIED if remote_changed else PHASE_SOURCE_REAUTH_REQUIRED,
            "remote_changed": remote_changed,
        }
        if not actual_email or actual_email != normalize_email(expected_email):
            raise EmailChangeIdentityMismatch(
                f"{context_label} /me 邮箱与预期邮箱不一致",
                **mismatch_kwargs,
            )
        if not actual_user_id:
            raise EmailChangeIdentityMismatch(
                f"{context_label} /me 未返回用户 ID",
                **mismatch_kwargs,
            )
        if expected_user_id and actual_user_id != expected_user_id:
            raise EmailChangeIdentityMismatch(
                f"{context_label} /me 用户 ID 与原账号不一致",
                **mismatch_kwargs,
            )
        if token_user_id and token_user_id != actual_user_id:
            raise EmailChangeIdentityMismatch(
                f"{context_label} JWT 用户 ID 与 /me 不一致",
                **mismatch_kwargs,
            )
        if not actual_account_id:
            raise EmailChangeIdentityMismatch(
                f"{context_label} JWT 未返回 ChatGPT 账号 ID",
                **mismatch_kwargs,
            )
        if expected_account_id and actual_account_id != expected_account_id:
            raise EmailChangeIdentityMismatch(
                f"{context_label} JWT ChatGPT 账号 ID 与原账号不一致",
                **mismatch_kwargs,
            )
        if captured_account_id and captured_account_id != actual_account_id:
            raise EmailChangeIdentityMismatch(
                f"{context_label}捕获账号 ID 与 JWT 不一致",
                **mismatch_kwargs,
            )
        if (
            expected_organization_id
            and actual_organization_id
            and actual_organization_id != expected_organization_id
        ):
            raise EmailChangeIdentityMismatch(
                f"{context_label}默认组织与原账号不一致",
                **mismatch_kwargs,
            )
        return {
            "email": actual_email,
            "user_id": actual_user_id,
            "account_id": actual_account_id,
            "organization_id": actual_organization_id,
        }

    def _commit_account(
        self,
        *,
        account_id: int,
        source_identity: dict[str, str],
        target_identity: dict[str, str],
        tokens: dict[str, Any],
        mailbox_state: dict[str, Any],
    ) -> None:
        target_email = normalize_email(mailbox_state.get("email"))
        access_token = str(tokens.get("access_token") or "").strip()
        session_token = str(tokens.get("session_token") or "").strip()
        cookie_header = str(tokens.get("cookie_header") or tokens.get("cookies") or "").strip()
        captured_account_id = str(target_identity.get("account_id") or "").strip()
        if not target_email or not access_token or not session_token or not cookie_header:
            raise EmailChangeError("target_session_incomplete", "目标登录材料不完整", remote_changed=True)
        if source_identity.get("account_id") and captured_account_id and source_identity["account_id"] != captured_account_id:
            raise EmailChangeIdentityMismatch("目标登录属于另一个 ChatGPT 账号")
        if (
            source_identity.get("user_id")
            and target_identity.get("user_id")
            and source_identity["user_id"] != target_identity["user_id"]
        ):
            raise EmailChangeIdentityMismatch("目标登录用户 ID 与原账号不一致")
        if (
            source_identity.get("organization_id")
            and target_identity.get("organization_id")
            and source_identity["organization_id"] != target_identity["organization_id"]
        ):
            raise EmailChangeIdentityMismatch("目标登录默认组织与原账号不一致")
        if target_identity.get("email") and target_identity["email"] != target_email:
            raise EmailChangeIdentityMismatch("目标登录邮箱与冻结目标不一致")

        with Session(engine) as session:
            row = session.get(AccountModel, int(account_id or 0))
            if row is None or row.platform != "chatgpt":
                raise EmailChangeError("account_missing", "原 ChatGPT 账号不存在", remote_changed=True)
            if normalize_email(row.email) != normalize_email(source_identity.get("email")):
                raise EmailChangeError("source_row_changed", "原账号记录在换绑期间已被修改", remote_changed=True)
            conflict = session.exec(
                select(AccountModel)
                .where(AccountModel.platform == "chatgpt")
                .where(AccountModel.email.collate("NOCASE") == target_email)
                .where(AccountModel.id != int(account_id))
            ).first()
            if conflict is not None:
                raise EmailChangeError("target_email_conflict", "目标邮箱已被其他 ChatGPT 账号占用", remote_changed=True)

            extra = row.get_extra()
            row.email = target_email
            row.token = access_token
            row.user_id = str(source_identity.get("user_id") or row.user_id or target_identity.get("user_id") or "").strip()
            extra["access_token"] = access_token
            extra["session_token"] = session_token
            extra["cookies"] = cookie_header
            extra["cookie_header"] = cookie_header
            if captured_account_id:
                extra["account_id"] = captured_account_id
            workspace_id = str(tokens.get("workspace_id") or target_identity.get("organization_id") or "").strip()
            if workspace_id:
                extra["workspace_id"] = workspace_id
            if target_identity.get("organization_id"):
                extra["organization_id"] = str(target_identity["organization_id"])
            # A target login only captures a fresh Web Session.  Old refresh
            # material may still contain the pre-change account boundary; clear
            # it instead of advertising it as a valid post-change credential.
            for key in ("refresh_token", "refreshToken", "id_token", "idToken"):
                extra.pop(key, None)
            extra["auth_level"] = "access_token_only"
            extra["chatgpt_token_source"] = CHATGPT_EMAIL_CHANGE_SOURCE
            cleaned_mailbox_state = _safe_mailbox_state(mailbox_state, email=target_email)
            extra["chatgpt_mailbox_state"] = cleaned_mailbox_state
            extra["mail_provider"] = normalize_mailbox_provider(
                cleaned_mailbox_state.get("provider")
            )
            extra["chatgpt_email_change"] = {
                "task_id": self.task_id,
                "source_email": normalize_email(source_identity.get("email")),
                "target_email": target_email,
                "committed_at": utc_iso(),
            }
            extra["chatgpt_email_change_recovery"] = {}
            fingerprint = build_browser_fingerprint_payload(tokens.get("browser_fingerprint"))
            if fingerprint:
                extra["chatgpt_browser_fingerprint"] = fingerprint
            row.set_extra(extra)
            row.updated_at = datetime.now(timezone.utc)
            apply_material_capture(
                session,
                row,
                extra=extra,
                web_session_expires_at=tokens.get("web_session_expires_at"),
                operation=CHATGPT_EMAIL_CHANGE_SOURCE,
            )
            session.add(row)
            try:
                from services.account_filters import upsert_account_list_state_for_account_ids

                upsert_account_list_state_for_account_ids(session, [int(account_id)], commit=False)
            except Exception:
                pass
            session.commit()
        schedule_chatgpt_local_status_refresh_for_account_id(
            int(account_id),
            reason=CHATGPT_EMAIL_CHANGE_SOURCE,
            delay_seconds=2.0,
        )

    def _local_commit_present(
        self,
        account: Any,
        row: ChatGPTEmailChangeModel,
    ) -> bool:
        if normalize_email(account.email) != normalize_email(row.target_email):
            return False
        marker = account.get_extra().get("chatgpt_email_change")
        marker = marker if isinstance(marker, dict) else {}
        return str(marker.get("task_id") or "").strip() == self.task_id

    def _persist_finalized_mailbox_state(
        self,
        *,
        account_id: int,
        target_email: str,
        mailbox_state: dict[str, Any],
    ) -> None:
        cleaned = _safe_mailbox_state(mailbox_state, email=target_email)
        with Session(engine) as session:
            account = session.get(AccountModel, int(account_id or 0))
            if account is None or account.platform != "chatgpt":
                raise EmailChangeError(
                    "account_missing",
                    "本地提交后的 ChatGPT 账号不存在",
                    remote_changed=True,
                )
            if normalize_email(account.email) != normalize_email(target_email):
                raise EmailChangeError(
                    "local_commit_missing",
                    "目标邮箱已远端换绑，但本地账号提交状态不一致",
                    remote_changed=True,
                )
            extra = account.get_extra()
            marker = extra.get("chatgpt_email_change")
            marker = marker if isinstance(marker, dict) else {}
            if str(marker.get("task_id") or "").strip() != self.task_id:
                raise EmailChangeError(
                    "local_commit_marker_mismatch",
                    "目标邮箱对应的本地提交标记不属于当前任务",
                    remote_changed=True,
                )
            extra["chatgpt_mailbox_state"] = cleaned
            extra["mail_provider"] = normalize_mailbox_provider(cleaned.get("provider"))
            marker["mailbox_finalized_at"] = utc_iso()
            extra["chatgpt_email_change"] = marker
            extra["chatgpt_email_change_recovery"] = {}
            account.set_extra(extra)
            account.updated_at = datetime.now(timezone.utc)
            session.add(account)
            session.commit()

    def _finalize_target_mailbox(
        self,
        *,
        account_id: int,
        target_email: str,
        mailbox_state: dict[str, Any],
    ) -> dict[str, Any]:
        self._update(
            phase=PHASE_IDENTITY_VERIFIED,
            status=STATUS_RUNNING,
            patch={"mailbox_finalize_started_at": utc_iso()},
            mailbox_state=mailbox_state,
        )
        try:
            service = RestoredEmailService(
                state=mailbox_state,
                log_fn=self._log,
                task_control=self.control,
                attempt_id=self.attempt_id,
            )
            service.finalize_success(
                account_email=normalize_email(target_email),
                task_id=self.task_id,
            )
            finalized_state = _safe_mailbox_state(
                service.export_state(),
                email=target_email,
            )
        except TaskInterruption:
            raise
        except Exception as exc:
            raise EmailChangeError(
                "mailbox_finalize_failed",
                f"目标邮箱租约确认失败：{sanitize_error_message(str(exc))}",
                phase=PHASE_RECOVERY_REQUIRED,
                retryable=True,
                remote_changed=True,
            ) from exc
        self._persist_finalized_mailbox_state(
            account_id=account_id,
            target_email=target_email,
            mailbox_state=finalized_state,
        )
        self._update(
            phase=PHASE_IDENTITY_VERIFIED,
            status=STATUS_RUNNING,
            patch={"mailbox_finalized_at": utc_iso()},
            mailbox_state=finalized_state,
        )
        return finalized_state

    def _persist_recovery_marker(self, error: EmailChangeError) -> None:
        """Expose a non-secret local warning without changing account state."""

        try:
            row = self._load_row()
            with Session(engine) as session:
                account = session.get(AccountModel, int(row.account_id or 0))
                if account is None or account.platform != "chatgpt":
                    return
                extra = account.get_extra()
                extra["chatgpt_email_change_recovery"] = {
                    "task_id": self.task_id,
                    "source_email": normalize_email(row.source_email),
                    "target_email": normalize_email(row.target_email),
                    "phase": PHASE_RECOVERY_REQUIRED,
                    "error_code": str(error.code or ""),
                    "updated_at": utc_iso(),
                }
                account.set_extra(extra)
                account.updated_at = datetime.now(timezone.utc)
                session.add(account)
                session.commit()
        except Exception:
            # The durable email-change row remains authoritative. A secondary
            # account projection failure must never replace the original error.
            return

    def _execute_remote_change(
        self,
        *,
        row: ChatGPTEmailChangeModel,
        account: Any,
        source_mailbox_state: dict[str, Any],
        target_state: dict[str, Any],
    ) -> tuple[Any, dict[str, str], dict[str, Any]]:
        expected_identity = self._source_identity_from_row(row, account)
        reauth_attempted = False
        reauth_codes = {
            "reauth_required",
            "source_session_unauthorized",
            "source_session_missing",
            "source_account_id_missing",
            "source_user_id_missing",
        }

        while True:
            try:
                self._update(phase=PHASE_ELIGIBILITY_CHECKED, status=STATUS_RUNNING)
                with self._source_browser(account) as (_page, request):
                    me_response = request("GET", "/backend-api/me")
                    if not me_response.get("ok"):
                        raise _classify_http_error(
                            me_response,
                            phase=PHASE_ELIGIBILITY_CHECKED,
                        )
                    me_payload = (
                        me_response.get("data")
                        if isinstance(me_response.get("data"), dict)
                        else {}
                    )
                    source_identity = self._source_identity(account, me_payload)
                    self._assert_source_identity(
                        source_identity,
                        expected_identity,
                        source_email=row.source_email,
                    )
                    self._update(
                        patch={
                            "source_chatgpt_user_id": source_identity.get("user_id") or "",
                            "source_chatgpt_account_id": source_identity.get("account_id") or "",
                            "source_organization_id": source_identity.get("organization_id") or "",
                        }
                    )

                    eligibility = request(
                        "GET",
                        "/backend-api/accounts/change_email/eligibility",
                    )
                    if not eligibility.get("ok"):
                        raise _classify_http_error(
                            eligibility,
                            phase=PHASE_ELIGIBILITY_CHECKED,
                        )
                    eligibility_data = (
                        eligibility.get("data")
                        if isinstance(eligibility.get("data"), dict)
                        else {}
                    )
                    if eligibility_data.get("eligible") is not True:
                        raise EmailChangeError(
                            "not_eligible",
                            "当前账号不满足邮箱换绑条件",
                            phase=PHASE_ELIGIBILITY_CHECKED,
                        )
                    eligibility_type = str(
                        eligibility_data.get("eligibility_type") or ""
                    ).strip()
                    self._update(
                        phase=PHASE_ELIGIBILITY_CHECKED,
                        status=STATUS_RUNNING,
                        patch={"eligibility_type": eligibility_type},
                    )

                    begin = request(
                        "POST",
                        "/backend-api/accounts/change_email/begin",
                        build_begin_payload(
                            row.target_email,
                            remove_social_subs=bool(row.remove_social_subs),
                        ),
                    )
                    if not begin.get("ok"):
                        raise _classify_http_error(begin, phase=PHASE_BEGIN_SENT)
                    begin_data = begin.get("data") if isinstance(begin.get("data"), dict) else {}
                    if begin_data.get("success") is not True:
                        raise EmailChangeError(
                            "begin_response_invalid",
                            "OpenAI 未确认已发送换绑验证码",
                            phase=PHASE_BEGIN_SENT,
                            retryable=True,
                        )
                    self._update(phase=PHASE_BEGIN_SENT, status=STATUS_RUNNING)

                    sent_at = time.time()
                    self._update(
                        phase=PHASE_WAITING_CHANGE_OTP,
                        status=STATUS_RUNNING,
                        patch={"change_otp_sent_at": utc_iso()},
                        mailbox_state=target_state,
                    )
                    change_code, change_message_id, target_state = self._wait_for_otp(
                        target_state,
                        email=row.target_email,
                        phase="chatgpt_email_change_verify_otp",
                        label="换绑确认验证码",
                        sent_at=sent_at,
                    )
                    # Freeze the consumed message cursor before the irreversible
                    # request. A restart must not reuse this message for login.
                    self._update(
                        phase=PHASE_WAITING_CHANGE_OTP,
                        status=STATUS_RUNNING,
                        patch={"change_otp_message_id": change_message_id},
                        mailbox_state=target_state,
                    )
                    self._update(
                        phase=PHASE_WAITING_CHANGE_OTP,
                        status=STATUS_RUNNING,
                        patch={"verify_submitted_at": utc_iso()},
                        mailbox_state=target_state,
                    )
                    try:
                        verify = request(
                            "POST",
                            "/backend-api/accounts/change_email/verify",
                            {
                                "email": normalize_email(row.target_email),
                                "code": change_code,
                            },
                        )
                    except TaskInterruption:
                        self.remote_changed = True
                        raise
                    except Exception as exc:
                        self.remote_changed = True
                        raise EmailChangeError(
                            "verify_outcome_unknown",
                            "换绑确认请求结果未知，已转入只恢复模式",
                            phase=PHASE_RECOVERY_REQUIRED,
                            retryable=True,
                            remote_changed=True,
                        ) from exc
                    if not verify.get("ok"):
                        status_code = int(verify.get("status") or 0)
                        if status_code == 0 or status_code in {408, 409, 425, 500, 502, 503, 504}:
                            self.remote_changed = True
                            raise EmailChangeError(
                                "verify_outcome_unknown",
                                "换绑确认请求结果未知，已转入只恢复模式",
                                phase=PHASE_RECOVERY_REQUIRED,
                                retryable=True,
                                remote_changed=True,
                            )
                        self._update(patch={"verify_submitted_at": ""})
                        raise _classify_http_error(
                            verify,
                            phase=PHASE_WAITING_CHANGE_OTP,
                        )
                    verify_data = (
                        verify.get("data")
                        if isinstance(verify.get("data"), dict)
                        else {}
                    )
                    if verify_data.get("success") is not True:
                        self.remote_changed = True
                        raise EmailChangeError(
                            "verify_outcome_unknown",
                            "换绑确认响应缺少成功凭据，已转入只恢复模式",
                            phase=PHASE_RECOVERY_REQUIRED,
                            retryable=True,
                            remote_changed=True,
                        )
                    self.remote_changed = True
                    self._update(
                        phase=PHASE_REMOTE_EMAIL_CHANGED,
                        status=STATUS_RUNNING,
                        patch={"remote_changed_at": utc_iso()},
                        mailbox_state=target_state,
                    )
                    return account, source_identity, target_state
            except EmailChangeError as error:
                requires_reauth = (
                    error.code in reauth_codes
                    or error.phase == PHASE_SOURCE_REAUTH_REQUIRED
                )
                if not requires_reauth:
                    raise
                if reauth_attempted:
                    raise EmailChangeError(
                        "source_reauth_exhausted",
                        "源账号重新认证后仍无法通过换绑资格校验",
                        phase=PHASE_SOURCE_REAUTH_REQUIRED,
                        retryable=True,
                    ) from error
                account, source_mailbox_state, verified = self._reauth_source(
                    account,
                    source_mailbox_state,
                    expected_identity,
                )
                expected_identity = {
                    **expected_identity,
                    **{key: value for key, value in verified.items() if value},
                }
                reauth_attempted = True

    def run(self) -> dict[str, Any]:
        row = self._load_row()
        target_state = _safe_mailbox_state(
            row.get_mailbox_state(),
            email=row.target_email,
        )
        self.remote_changed = bool(row.remote_changed_at or row.verify_submitted_at)
        try:
            self._checkpoint()
            self._update(
                phase=(PHASE_RECOVERY_REQUIRED if self.remote_changed else PHASE_CREATED),
                status=STATUS_RUNNING,
                patch={"error_code": "", "sanitized_error": ""},
                resumable=False,
            )
            with Session(engine) as session:
                db_account = session.get(AccountModel, int(row.account_id or 0))
                if db_account is None or db_account.platform != "chatgpt":
                    raise EmailChangeError("account_missing", "ChatGPT 账号不存在")
                current_email = normalize_email(db_account.email)
                source_mailbox_state = (
                    mailbox_state_from_account(db_account)
                    if current_email == normalize_email(row.source_email)
                    else {}
                )
                account = self._detached_account(db_account)

            if (
                not target_state
                or normalize_email(target_state.get("email"))
                != normalize_email(row.target_email)
            ):
                raise EmailChangeError(
                    "target_mailbox_missing",
                    "目标邮箱预留状态不存在或地址已变化",
                    remote_changed=self.remote_changed,
                )

            source_email = normalize_email(row.source_email)
            target_email = normalize_email(row.target_email)
            local_commit_present = self._local_commit_present(account, row)
            if current_email == target_email:
                self.remote_changed = True
                if not local_commit_present:
                    raise EmailChangeError(
                        "local_commit_marker_mismatch",
                        "本地账号邮箱已是目标地址，但缺少当前任务的提交标记",
                        remote_changed=True,
                    )
                if not row.committed_at:
                    self._update(patch={"committed_at": utc_iso()})
                if not row.mailbox_finalized_at:
                    target_state = self._finalize_target_mailbox(
                        account_id=int(row.account_id),
                        target_email=target_email,
                        mailbox_state=target_state,
                    )
                self._update(
                    phase=PHASE_COMMITTED,
                    status=STATUS_DONE,
                    patch={
                        "committed_at": row.committed_at or utc_iso(),
                        "mailbox_finalized_at": row.mailbox_finalized_at or utc_iso(),
                    },
                    mailbox_state=target_state,
                    resumable=False,
                )
                return {
                    "ok": True,
                    "phase": PHASE_COMMITTED,
                    "target_email": target_email,
                    "recovered": True,
                }
            if current_email != source_email:
                raise EmailChangeError(
                    "source_row_changed",
                    "原账号邮箱已变化，无法确认当前换绑归属",
                    remote_changed=self.remote_changed,
                )
            if row.committed_at:
                raise EmailChangeError(
                    "local_commit_missing",
                    "持久化记录显示已提交，但原账号邮箱仍未更新",
                    remote_changed=True,
                )

            source_identity = self._source_identity_from_row(row, account)
            if not self.remote_changed:
                account, source_identity, target_state = self._execute_remote_change(
                    row=row,
                    account=account,
                    source_mailbox_state=source_mailbox_state,
                    target_state=target_state,
                )
            else:
                if not source_identity.get("user_id") or not source_identity.get("account_id"):
                    raise EmailChangeError(
                        "source_identity_missing_for_recovery",
                        "远端换绑结果待恢复，但缺少原账号身份快照，禁止自动提交",
                        remote_changed=True,
                    )
                self._log("[邮箱换绑] 已越过远端确认边界，仅执行目标登录、本地提交和租约确认")

            tokens, target_state = self._prepare_target_login(
                account,
                target_state,
                source_identity,
            )
            target_identity = self._verify_remote_identity(
                tokens,
                expected_email=target_email,
                expected_user_id=str(source_identity.get("user_id") or ""),
                expected_account_id=str(source_identity.get("account_id") or ""),
                expected_organization_id=str(source_identity.get("organization_id") or ""),
                context_label="目标邮箱登录",
                remote_changed=True,
            )
            self._update(
                phase=PHASE_IDENTITY_VERIFIED,
                status=STATUS_RUNNING,
                patch={"identity_verified_at": utc_iso()},
                mailbox_state=target_state,
            )
            self._commit_account(
                account_id=int(row.account_id),
                source_identity=source_identity,
                target_identity=target_identity,
                tokens=tokens,
                mailbox_state=target_state,
            )
            committed_at = utc_iso()
            self._update(
                phase=PHASE_IDENTITY_VERIFIED,
                status=STATUS_RUNNING,
                patch={"committed_at": committed_at},
                mailbox_state=target_state,
                resumable=True,
            )
            target_state = self._finalize_target_mailbox(
                account_id=int(row.account_id),
                target_email=target_email,
                mailbox_state=target_state,
            )
            self._update(
                phase=PHASE_COMMITTED,
                status=STATUS_DONE,
                patch={
                    "committed_at": committed_at,
                    "mailbox_finalized_at": utc_iso(),
                },
                mailbox_state=target_state,
                resumable=False,
            )
            return {
                "ok": True,
                "phase": PHASE_COMMITTED,
                "target_email": target_email,
            }
        except TaskInterruption:
            error = EmailChangeError(
                "stopped",
                "任务已停止",
                phase=PHASE_RECOVERY_REQUIRED if self.remote_changed else PHASE_CREATED,
                remote_changed=self.remote_changed,
            )
            self._update(
                phase=PHASE_RECOVERY_REQUIRED if self.remote_changed else PHASE_CREATED,
                status=STATUS_PARTIAL if self.remote_changed else STATUS_FAILED,
                error=error,
                resumable=self.remote_changed,
            )
            if self.remote_changed:
                self._persist_recovery_marker(error)
            raise
        except EmailChangeError as error:
            crossed_remote_boundary = bool(error.remote_changed or self.remote_changed)
            phase = (
                PHASE_RECOVERY_REQUIRED
                if crossed_remote_boundary
                else error.phase or PHASE_CREATED
            )
            status = STATUS_PARTIAL if crossed_remote_boundary else STATUS_FAILED
            resumable = bool(
                crossed_remote_boundary
                or error.retryable
                or error.phase in {PHASE_SOURCE_REAUTH_REQUIRED, PHASE_RATE_LIMITED}
            )
            self._update(
                phase=phase,
                status=status,
                error=error,
                resumable=resumable,
            )
            if crossed_remote_boundary:
                self._persist_recovery_marker(error)
            raise
        except Exception as exc:
            error = EmailChangeError(
                "email_change_exception",
                f"邮箱换绑异常：{type(exc).__name__}",
                phase=PHASE_RECOVERY_REQUIRED if self.remote_changed else PHASE_CREATED,
                remote_changed=self.remote_changed,
            )
            self._update(
                phase=PHASE_RECOVERY_REQUIRED if self.remote_changed else PHASE_CREATED,
                status=STATUS_PARTIAL if self.remote_changed else STATUS_FAILED,
                error=error,
                resumable=self.remote_changed,
            )
            if self.remote_changed:
                self._persist_recovery_marker(error)
            raise error from exc


def prepare_target_mailbox(
    *,
    provider: str,
    target_email: str = "",
    domain: str = "",
    task_id: str = "",
) -> dict[str, Any]:
    """Create/reserve one concrete target mailbox and return safe state."""

    normalized_provider = normalize_mailbox_provider(provider)
    if normalized_provider not in TARGET_PROVIDER_VALUES:
        raise ValueError("目标邮箱提供方仅支持 HME、TempMail 或手动邮箱")
    config = _config_snapshot(normalized_provider, target_domain=str(domain or "").strip().lower().lstrip("@."))
    if normalized_provider == TARGET_PROVIDER_MANUAL:
        email = validate_email(target_email)
        config["manual_email_address"] = email
        account = MailboxAccount(email=email, account_id=email, extra={"provider": TARGET_PROVIDER_MANUAL})
        state = _mailbox_state_from_account(normalized_provider, account, config)
    elif normalized_provider == TARGET_PROVIDER_TEMPMAIL:
        mailbox = create_mailbox(normalized_provider, extra=config)
        if task_id:
            setattr(mailbox, "_task_attempt_token", str(task_id))
        account = mailbox.get_email()
        email = validate_email(account.email)
        try:
            before_ids = set(mailbox.get_current_ids(account) or set())
        except Exception:
            before_ids = set()
        state = _mailbox_state_from_account(normalized_provider, account, config, before_ids=before_ids)
    else:
        mailbox = create_mailbox(normalized_provider, extra=config)
        setattr(mailbox, "_task_attempt_token", str(task_id or uuid.uuid4().hex))
        setattr(mailbox, "_registration_task_id", str(task_id or ""))
        account = mailbox.get_email()
        email = validate_email(account.email)
        try:
            before_ids = set(mailbox.get_current_ids(account) or set())
        except Exception:
            before_ids = set()
        state = _mailbox_state_from_account(normalized_provider, account, config, before_ids=before_ids)

    return {
        "target_email": email,
        "target_mailbox_ref": f"{normalized_provider}:{str(account.account_id or email).strip()}",
        "provider": normalized_provider,
        "mailbox_state": state,
        "mailbox_summary": mailbox_state_summary(state, account_email=email),
        "lease_expires_at": str(
            (getattr(account, "extra", None) or {}).get("lease_expires_at")
            or (getattr(account, "extra", None) or {}).get("expires_at")
            or ""
        ).strip(),
    }


def release_target_mailbox(
    state: dict[str, Any],
    *,
    task_id: str = "",
    reason: str = "",
) -> dict[str, Any]:
    """Release only a target that never crossed the remote verify boundary."""

    cleaned = _safe_mailbox_state(state)
    provider = normalize_mailbox_provider(cleaned.get("provider"))
    if provider not in TARGET_PROVIDER_VALUES:
        return cleaned
    service = RestoredEmailService(state=cleaned, log_fn=None)
    service.finalize_failure(error_message=reason, task_id=task_id)
    return _safe_mailbox_state(service.export_state())


def target_mailbox_options() -> dict[str, Any]:
    """Return selector options without exposing provider credentials."""

    config = config_store.get_all() or {}
    domains: list[str] = []
    fixed = config.get("tempmail_fixed_domains")
    if isinstance(fixed, (list, tuple, set)):
        values = fixed
    else:
        values = re.split(r"[\s,;]+", str(fixed or ""))
    for value in values:
        domain = str(value or "").strip().lower().lstrip("@.")
        if domain and domain not in domains:
            domains.append(domain)
    primary = str(config.get("tempmail_primary_domain") or "").strip().lower().lstrip("@.")
    if primary and primary not in domains:
        domains.insert(0, primary)
    return {
        "providers": [
            {"provider": TARGET_PROVIDER_HME, "label": "HME Ready 自动分配"},
            {"provider": TARGET_PROVIDER_TEMPMAIL, "label": "TempMail 新建并锁定", "domains": domains},
            {"provider": TARGET_PROVIDER_MANUAL, "label": "手动外部邮箱"},
        ],
        "tempmail_domains": domains,
    }


__all__ = [
    "CHATGPT_EMAIL_CHANGE_SOURCE",
    "ChatGPTEmailChangeService",
    "EmailChangeError",
    "PHASE_COMMITTED",
    "PHASE_CREATED",
    "PHASE_RECOVERY_REQUIRED",
    "PHASE_REMOTE_EMAIL_CHANGED",
    "STATUS_DONE",
    "STATUS_FAILED",
    "STATUS_PARTIAL",
    "STATUS_RELEASED",
    "STATUS_RELEASING",
    "build_begin_payload",
    "prepare_target_mailbox",
    "release_target_mailbox",
    "target_mailbox_options",
    "validate_email",
]
