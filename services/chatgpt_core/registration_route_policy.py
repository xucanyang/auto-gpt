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
    text = str(message or "").lower()
    markers = (
        "user_already_exists",
        "account already exists",
        "please login instead",
        "existing_account_login_route",
        "login_route",
        "add_phone",
        "add-phone",
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
    enabled: bool = True,
    routed: bool = False,
    blocked: bool = False,
    action: str = "",
    source: str = "registration",
    base_event: dict | None = None,
) -> dict[str, Any]:
    base = dict(base_event or {}) if isinstance(base_event, dict) else {}
    payload = {
        **base,
        "email": str(email or base.get("email") or "").strip(),
        "stage": str(stage or base.get("stage") or "register_complete_flow").strip(),
        "source": str(source or base.get("source") or "registration").strip(),
        "reason": _short_text(reason or base.get("reason") or ""),
        "enabled": bool(enabled),
        "routed": bool(routed),
        "blocked": bool(blocked),
        "action": str(action or base.get("action") or ("skip_save" if blocked else "login_recovery" if routed else "")).strip(),
        "code": LOGIN_ROUTE_BLOCKED_CODE if blocked else LOGIN_ROUTE_ROUTED_CODE if routed else str(base.get("code") or ""),
        "detected_at": str(base.get("detected_at") or datetime.now(timezone.utc).isoformat()),
    }
    return {key: value for key, value in payload.items() if value not in (None, "")}


class ExistingAccountLoginRouteBlocked(SkipCurrentAttemptRequested):
    """注册检测到已有账号，但当前任务禁止路由到登录恢复。"""

    code = LOGIN_ROUTE_BLOCKED_CODE

    def __init__(self, email: str = "", reason: Any = "", event: dict | None = None):
        self.email = str(email or "").strip()
        self.reason = _short_text(reason)
        self.route_event = build_existing_account_login_route_event(
            email=self.email,
            reason=self.reason,
            enabled=False,
            routed=False,
            blocked=True,
            action="skip_save",
            base_event=event,
        )
        label = self.email or "-"
        message = f"注册阶段检测到已注册邮箱，已按配置跳过且不保存账号: {label}"
        if self.reason:
            message = f"{message} reason={self.reason}"
        super().__init__(message)
