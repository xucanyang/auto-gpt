"""ChatGPT 注册阶段遇到已有账号时的登录路由策略。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from core.task_runtime import SkipCurrentAttemptRequested


LOGIN_ROUTE_ENABLED_KEY = "chatgpt_existing_account_login_route_enabled"
LOGIN_ROUTE_EVENT_KEY = "chatgpt_existing_account_login_route"
LOGIN_ROUTE_TASK_META_KEY = "existing_account_login_routes"
LOGIN_ROUTE_BLOCKED_CODE = "existing_account_login_route_blocked"
LOGIN_ROUTE_ROUTED_CODE = "existing_account_login_routed"
EXISTING_ACCOUNT_DETECTED_CODE = "existing_account_detected"

_TRUTHY = {"1", "true", "yes", "on"}
_FALSEY = {"0", "false", "no", "off"}


def parse_bool(value: Any, *, default: bool = False) -> bool:
    if value is None or value == "":
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value or "").strip().lower()
    if text in _TRUTHY:
        return True
    if text in _FALSEY:
        return False
    return bool(default)


def existing_account_login_route_enabled(extra_config: dict | None) -> bool:
    """默认保持旧行为：遇到已注册邮箱时允许路由到登录恢复。"""

    extra = extra_config or {}
    return parse_bool(extra.get(LOGIN_ROUTE_ENABLED_KEY), default=True)


def is_existing_account_login_route_message(message: Any) -> bool:
    """Whether a registration/OAuth error means OpenAI treated the email as already registered.

    Used by registration engines to choose login recovery vs skip.  Keep this slightly
    broader than :func:`is_existing_account_detected_message` so protocol recovery paths
    that surface add-phone after an existing-account fork still classify correctly.
    """

    text = str(message or "").lower()
    markers = (
        "user_already_exists",
        "account already exists",
        "please login instead",
        "existing_account_login_route",
        "login_route",
        "邮箱已存在",
        "已注册邮箱",
        "login_password",
        "add_phone",
        "add-phone",
    )
    return any(marker in text for marker in markers)


def is_existing_account_detected_message(message: Any) -> bool:
    """Narrow detector for scheduling: dirty email / existing account, not uncertain signup.

    Task control uses this to avoid consuming browser identity slots and to skip
    proxy-identity-fork framing when the failure is a deterministic existing account.
    """

    text = str(message or "").lower()
    markers = (
        "user_already_exists",
        "account already exists",
        "please login instead",
        "existing_account_login_route",
        "邮箱已存在",
        "已注册邮箱",
        "login_password",
        "explicit_existing_account_capture",
        "已按配置跳过且不保存",
        "已按配置禁止路由到登录",
    )
    return any(marker in text for marker in markers)


def _short_text(value: Any, limit: int = 500) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 1)] + "…"


def build_existing_account_login_route_event(
    *,
    email: str = "",
    reason: Any = "",
    stage: str = "register_complete_flow",
    enabled: bool | None = True,
    routed: bool = False,
    blocked: bool = False,
    action: str = "",
    source: str = "registration",
    signal: str = "",
    page_type: str = "",
    deterministic: bool | None = None,
    code: str = "",
    base_event: dict | None = None,
) -> dict[str, Any]:
    base = dict(base_event or {}) if isinstance(base_event, dict) else {}
    payload = {
        **base,
        "email": str(email or base.get("email") or "").strip(),
        "stage": str(stage or base.get("stage") or "register_complete_flow").strip(),
        "source": str(source or base.get("source") or "registration").strip(),
        "reason": _short_text(reason or base.get("reason") or ""),
        "enabled": None if enabled is None else bool(enabled),
        "routed": bool(routed),
        "blocked": bool(blocked),
        "action": str(action or base.get("action") or ("skip_save" if blocked else "login_recovery" if routed else "")).strip(),
        "signal": str(signal or base.get("signal") or "").strip(),
        "page_type": str(page_type or base.get("page_type") or "").strip(),
        "deterministic": (
            bool(deterministic)
            if deterministic is not None
            else bool(base.get("deterministic"))
            if "deterministic" in base
            else None
        ),
        "code": (
            LOGIN_ROUTE_BLOCKED_CODE
            if blocked
            else LOGIN_ROUTE_ROUTED_CODE
            if routed
            else str(code or base.get("code") or "")
        ),
        "detected_at": str(base.get("detected_at") or datetime.now(timezone.utc).isoformat()),
    }
    return {key: value for key, value in payload.items() if value not in (None, "")}


class ExistingAccountDetected(RuntimeError):
    """Structured deterministic signal emitted by a registration state machine.

    Browser registration runs in an isolated subprocess, so callers serialize
    ``route_event`` and make the login-recovery/skip policy decision in the
    parent engine.  The state machine must never click back to signup after this
    signal has been observed.
    """

    code = EXISTING_ACCOUNT_DETECTED_CODE

    def __init__(
        self,
        email: str = "",
        reason: Any = "",
        *,
        stage: str,
        signal: str,
        page_type: str = "",
        source: str = "registration",
        event: dict | None = None,
    ):
        self.email = str(email or "").strip()
        self.reason = _short_text(reason)
        self.route_event = build_existing_account_login_route_event(
            email=self.email,
            reason=self.reason,
            stage=stage,
            enabled=None,
            routed=False,
            blocked=False,
            action="detect",
            source=source,
            signal=signal,
            page_type=page_type,
            deterministic=True,
            code=self.code,
            base_event=event,
        )
        label = self.email or "-"
        super().__init__(
            "user_already_exists: "
            f"stage={stage or '-'} signal={signal or '-'} "
            f"page={page_type or '-'} email={label} reason={self.reason or '-'}"
        )


class ExistingAccountLoginRouteBlocked(SkipCurrentAttemptRequested):
    """注册检测到已有账号，但当前任务禁止路由到登录恢复。"""

    code = LOGIN_ROUTE_BLOCKED_CODE

    def __init__(self, email: str = "", reason: Any = "", event: dict | None = None):
        self.email = str(email or "").strip()
        self.reason = _short_text(reason)
        base = dict(event or {}) if isinstance(event, dict) else {}
        self.route_event = build_existing_account_login_route_event(
            email=self.email or str(base.get("email") or ""),
            reason=self.reason or base.get("reason") or "",
            stage=str(base.get("stage") or "register_complete_flow"),
            enabled=bool(base.get("enabled")) if "enabled" in base else False,
            routed=False,
            blocked=True,
            action="skip_save",
            source=str(base.get("source") or "registration"),
            base_event=base,
        )
        label = self.email or str(self.route_event.get("email") or "") or "-"
        message = f"注册阶段检测到已注册邮箱，已按配置跳过且不保存账号: {label}"
        if self.reason:
            message = f"{message} reason={self.reason}"
        super().__init__(message)
