from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Callable, Optional

from sqlmodel import Session, select

from core.base_mailbox import MailboxAccount, create_mailbox
from core.config_store import config_store
from core.db import AccountModel, PendingBusinessInviteModel, engine
from services.chatgpt_account_state import apply_auth_capture_status
from .business_workspace_recovery import BusinessWorkspaceRecovery
from .refresh_token_registration_engine import (
    EmailServiceAdapter,
    RefreshTokenRegistrationEngine,
    RegistrationResult,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _loads(raw: str | None, default: Any):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def _dumps(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False)


ACTIVATION_PROGRESS_STATUSES = {
    "activation_fetching_invite_mail",
    "activation_auth_login",
    "activation_consuming_invite",
    "activation_capturing_workspace",
    "subscription_pending_auth",
}
NON_ACTIVATABLE_PENDING_STATUSES = {"completed", "abandoned", "failed_terminal"}
CHECKPOINT_LABELS = {
    "invite_sent_pending_activation": "待激活",
    "subscription_pending_auth": "订阅待补抓",
    "activation_fetching_invite_mail": "拉取邀请邮件",
    "activation_auth_login": "登录并消费邀请",
    "activation_consuming_invite": "消费邀请链接",
    "activation_capturing_workspace": "抓取工作空间",
    "completed": "已完成",
}
TERMINAL_ERROR_CODES = {"missing_mailbox_state", "account_not_found", "invite_not_found", "abandoned"}
DEFAULT_SUBSCRIPTION_AUTH_RETRY_DELAYS_SECONDS = (30, 60, 120)
SUBSCRIPTION_AUTH_RETRYABLE_ERROR_MARKERS = (
    "workspace/select",
    "workspace/org",
    "page=consent",
    "codex/consent",
    "consent",
    "未获取到 workspace",
    "未获取所选工作空间",
    "未能获取所选工作空间",
    "workspace / callback",
    "workspace/callback",
    "workspace_capture_failed",
    "add_phone",
    "add-phone",
    "phone verification",
    "手机号",
    "429",
    "rate limit",
    "rate_limited",
)
TEMPMAIL_STATE_CONFIG_KEYS = (
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
)
ICLOUD_HME_STATE_CONFIG_KEYS = (
    "icloud_hme_mode",
    "icloud_cookie",
    "icloud_domain_base",
    "icloud_forward_to",
    "icloud_forward_mailbox_id",
    "icloud_hme_helper_api_url",
    "icloud_hme_helper_internal_key",
    "icloud_hme_helper_api_key_header",
    "icloud_hme_helper_consumer",
    "icloud_hme_helper_checkout_ttl_seconds",
    "icloud_hme_helper_wait_timeout_seconds",
    "icloud_hme_helper_max_cache_age_seconds",
    "tempmail_api_url",
    "tempmail_api_key",
    "tempmail_api_key_header",
    "tempmail_wait_timeout_seconds",
)
ICLOUD_HME_PROVIDER_VALUES = {"icloud_hme", "hme_ready_api", "icloud_hme_ready", "icloud_hme_helper_ready"}


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _current_mailbox_config(keys: tuple[str, ...]) -> dict[str, Any]:
    try:
        global_config = config_store.get_all()
    except Exception:
        global_config = {}
    return {
        key: value
        for key in keys
        if _has_value(value := (global_config or {}).get(key))
    }


def _with_current_tempmail_config(mailbox_state: dict[str, Any]) -> dict[str, Any]:
    state = dict(mailbox_state or {})
    provider = str(state.get("provider") or "").strip()
    if provider not in {"tempmail_local", "tempmail_api", *ICLOUD_HME_PROVIDER_VALUES}:
        return state

    keys = ICLOUD_HME_STATE_CONFIG_KEYS if provider in ICLOUD_HME_PROVIDER_VALUES else TEMPMAIL_STATE_CONFIG_KEYS
    current_config = _current_mailbox_config(keys)
    if not current_config:
        return state

    config = dict(state.get("config") or {})
    config.update(current_config)
    if provider in {"hme_ready_api", "icloud_hme_ready", "icloud_hme_helper_ready"}:
        config["icloud_hme_mode"] = "helper_ready_api"
    current_forward_mailbox_id = current_config.get("icloud_forward_mailbox_id")
    if provider in ICLOUD_HME_PROVIDER_VALUES and not _has_value(current_forward_mailbox_id):
        # The persisted config value is not a durable service endpoint.  If the
        # current global config has no explicit forward mailbox id, remove any
        # historical id from the constructor config so the mailbox layer can
        # resolve/rebind by forward_to instead of blindly trusting stale state.
        config.pop("icloud_forward_mailbox_id", None)
    state["config"] = config

    account = dict(state.get("account") or {})
    account_extra = dict(account.get("extra") or {})
    if provider in ICLOUD_HME_PROVIDER_VALUES:
        if _has_value(config.get("icloud_forward_to")):
            account_extra["forward_to"] = str(config.get("icloud_forward_to") or "").strip()
        # Global icloud_forward_mailbox_id is optional on this host.  If it is
        # empty, keep the saved account-extra id only as a cache; IcloudHmeMailbox
        # verifies it against the current TempMail service and rebinds by
        # forward_to when it is stale.
        if _has_value(current_forward_mailbox_id):
            account_extra["forward_mailbox_id"] = str(current_forward_mailbox_id or "").strip()
    if account_extra:
        account["extra"] = account_extra
    if not str(account.get("email") or "").strip() and str(state.get("email") or "").strip():
        account["email"] = str(state.get("email") or "").strip()
    if account:
        state["account"] = account

    state["config_refreshed_from_current"] = True
    return state


def _mailbox_state_from_account(account: AccountModel, *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    account_extra = dict(extra or account.get_extra() or {})
    mailbox_state = dict(account_extra.get("chatgpt_mailbox_state") or {})
    if mailbox_state:
        return _with_current_tempmail_config(mailbox_state)

    legacy_state = dict(account_extra.get("mailbox_state") or {})
    if legacy_state:
        return _with_current_tempmail_config(legacy_state)

    provider = str(
        account_extra.get("mail_provider")
        or account_extra.get("email_service")
        or account_extra.get("mailbox_provider")
        or ""
    ).strip()
    if provider in ICLOUD_HME_PROVIDER_VALUES:
        email = str(getattr(account, "email", "") or account_extra.get("email") or "").strip()
        if not email:
            return {}
        try:
            global_config = config_store.get_all()
        except Exception:
            global_config = {}

        anonymous_id = str(
            account_extra.get("anonymous_id")
            or account_extra.get("mailbox_id")
            or account_extra.get("service_id")
            or ""
        ).strip()
        if not anonymous_id:
            try:
                from core.db import IcloudHmeAliasModel

                with Session(engine) as session:
                    alias_row = session.exec(
                        select(IcloudHmeAliasModel).where(IcloudHmeAliasModel.hme == email)
                    ).first()
                    if alias_row is not None:
                        anonymous_id = str(getattr(alias_row, "anonymous_id", "") or "").strip()
            except Exception:
                anonymous_id = ""

        mailbox_config: dict[str, Any] = {}
        for key in ICLOUD_HME_STATE_CONFIG_KEYS:
            value = account_extra.get(key)
            if not _has_value(value):
                value = (global_config or {}).get(key)
            if _has_value(value):
                mailbox_config[key] = value

        if not _has_value(mailbox_config.get("icloud_forward_to")):
            mailbox_config["icloud_forward_to"] = "b@cccy.me"
        if not _has_value(mailbox_config.get("icloud_domain_base")):
            mailbox_config["icloud_domain_base"] = "icloud.com"
        if not _has_value(mailbox_config.get("icloud_hme_mode")):
            mailbox_config["icloud_hme_mode"] = "import_pool"

        required_keys = (
            "icloud_hme_mode",
            "icloud_forward_to",
            "tempmail_api_url",
            "tempmail_api_key",
        )
        if any(not _has_value(mailbox_config.get(key)) for key in required_keys):
            return {}

        return {
            "provider": "icloud_hme",
            "email": email,
            "account": {
                "email": email,
                "account_id": anonymous_id,
                "extra": {
                    "provider": "icloud_hme",
                    "forward_to": str(mailbox_config.get("icloud_forward_to") or "").strip(),
                    "forward_mailbox_id": str(mailbox_config.get("icloud_forward_mailbox_id") or account_extra.get("forward_mailbox_id") or "").strip(),
                },
            },
            "before_ids": [],
            "config": mailbox_config,
            "proxy": str(account_extra.get("proxy") or account_extra.get("proxy_url") or "").strip(),
            "recovered_from_account_config": True,
        }
    if provider not in {"tempmail_local", "tempmail_api"}:
        return {}

    email = str(getattr(account, "email", "") or account_extra.get("email") or "").strip()
    if not email:
        return {}

    try:
        global_config = config_store.get_all()
    except Exception:
        global_config = {}

    mailbox_config: dict[str, Any] = {}
    for key in TEMPMAIL_STATE_CONFIG_KEYS:
        value = account_extra.get(key)
        if not _has_value(value):
            value = (global_config or {}).get(key)
        if _has_value(value):
            mailbox_config[key] = value

    if not _has_value(mailbox_config.get("tempmail_api_url")) or not _has_value(mailbox_config.get("tempmail_api_key")):
        return {}

    return {
        "provider": provider,
        "email": email,
        "account": {
            "email": email,
            "account_id": str(account_extra.get("mailbox_id") or account_extra.get("service_id") or "").strip(),
            "extra": {},
        },
        "before_ids": [],
        "config": mailbox_config,
        "proxy": str(account_extra.get("proxy") or account_extra.get("proxy_url") or "").strip(),
        "recovered_from_account_config": True,
    }


def _persist_account_mailbox_state_if_missing(
    session: Session,
    account_id: int,
    mailbox_state: dict[str, Any],
) -> None:
    if not account_id or not mailbox_state:
        return
    account_db = session.get(AccountModel, int(account_id))
    if account_db is None:
        return
    extra = account_db.get_extra()
    if dict(extra.get("chatgpt_mailbox_state") or {}):
        return
    extra["chatgpt_mailbox_state"] = mailbox_state
    account_db.set_extra(extra)
    account_db.updated_at = _utcnow()
    session.add(account_db)


def _make_activation_run_id() -> str:
    return _utcnow().strftime("activation-%Y%m%d%H%M%S%f")


def _checkpoint_label(checkpoint: str) -> str:
    normalized = str(checkpoint or "").strip()
    return CHECKPOINT_LABELS.get(normalized, normalized or "-")


def _derive_activation_error_code(text: str) -> str:
    raw = str(text or "").strip().lower()
    if not raw:
        return "unknown"
    if "mailbox_state" in raw:
        return "missing_mailbox_state"
    if "关联账号不存在" in raw:
        return "account_not_found"
    if "pending invite 不存在" in raw:
        return "invite_not_found"
    if "未读取到邀请邮件" in raw or "未等到邀请邮件链接" in raw:
        return "invite_mail_missing"
    if "auth 登录后未拿到可继续的 business 恢复结果" in raw:
        return "auth_login_failed"
    if "rate limit exceeded" in raw or "429" in raw:
        return "rate_limited"
    if "工作空间失败" in raw or "workspace" in raw:
        return "workspace_capture_failed"
    if "abandoned" in raw or "已放弃" in raw:
        return "abandoned"
    return "activation_failed"


def _is_activation_error_retryable(error_code: str) -> bool:
    return str(error_code or "") not in TERMINAL_ERROR_CODES


def _status_can_activate(status: str) -> bool:
    return str(status or "") not in NON_ACTIVATABLE_PENDING_STATUSES


def _mark_pending_invite_state(
    invite_id: int,
    *,
    status: str | None = None,
    checkpoint: str | None = None,
    error: str | None = None,
    error_code: str | None = None,
    run_id: str | None = None,
    increment_attempt: bool = False,
    clear_error: bool = False,
    abandon: bool = False,
) -> PendingBusinessInviteModel | None:
    now = _utcnow()
    with Session(engine) as session:
        pending = session.get(PendingBusinessInviteModel, int(invite_id))
        if pending is None:
            return None
        if status is not None:
            pending.status = str(status or "")
        if checkpoint is not None:
            pending.last_checkpoint = str(checkpoint or "")
        if clear_error:
            pending.last_error = ""
            pending.last_error_code = ""
        if error is not None:
            pending.last_error = str(error or "")
        if error_code is not None:
            pending.last_error_code = str(error_code or "")
        if run_id is not None:
            pending.activation_run_id = str(run_id or "")
        if increment_attempt:
            pending.activation_attempt_count = int(pending.activation_attempt_count or 0) + 1
            pending.last_attempt_at = now.isoformat()
        if abandon:
            pending.abandoned_at = now.isoformat()
        pending.updated_at = now
        session.add(pending)
        session.commit()
        session.refresh(pending)
        return pending


def _build_joined_activation_result(
    *,
    team_id: int,
    invite_url: str,
    workspace_id: str,
) -> dict[str, Any]:
    return {
        "team_id": int(team_id or 0),
        "workspace_id": str(workspace_id or ""),
        "invite_url": str(invite_url or ""),
        "joined": True,
        "source": "activation_resume_joined",
    }


def _simplify_activation_error(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return "未知错误"
    if "Rate limit exceeded" in raw or "HTTP 429" in raw:
        return "请求过快，被上游限流（429）"
    if "add_phone" in raw:
        return "命中手机号验证页，未拿到工作空间回调"
    if "auth 登录后未拿到可继续的 business 恢复结果" in raw:
        return "auth 登录后未拿到可继续的 business 恢复结果"
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw[:220]


def _normalize_activation_log_message(message: str, level: str = "info") -> str:
    raw_text = str(message or "").strip()
    if not raw_text:
        return ""

    normalized_level = str(level or "info").strip().lower() or "info"
    is_debug = normalized_level == "debug" or raw_text.startswith("[DEBUG] ")
    text = raw_text[len("[DEBUG] "):].strip() if raw_text.startswith("[DEBUG] ") else raw_text
    if not text:
        return ""

    if is_debug:
        return f"[DEBUG] {text}"

    if text.startswith("[订阅] OpenAI 空间/权限可能仍在同步"):
        return text

    if text.startswith("正在等待邮箱 ") or text.startswith("成功获取验证码（"):
        return ""
    if "Sentinel Browser" in text or "email_otp_validate:" in text:
        return ""
    if any(marker in text for marker in (
        "/oauth/authorize ->",
        "/authorize/continue ->",
        "/passwordless/send-otp ->",
        "/email-otp/validate ->",
        "authorize_continue:",
        "状态步进[",
        "follow[",
        "OAuth 指纹:",
        "OAuth 策略:",
        "workspace 候选:",
        "workspace 解析入口:",
        "选择 workspace:",
        "选择 organization:",
        "workspace/select ->",
        "organization/select ->",
        "获取到 authorization code:",
        "login_session: 已获取",
        "passwordless OTP 已触发",
        "OAuth OTP 等待窗口:",
        "使用 wait_for_verification_code 进行阻塞式获取新验证码",
        "authorize_continue 分支判定:",
        "workspace state ->",
        "follow state ->",
        "page=",
        "method=GET next=",
        "method=POST next=",
    )):
        return ""

    if text.startswith("business recovery: 命中邀请邮件"):
        return ""
    if text.startswith("business recovery: 直接访问邀请链接 ->"):
        return ""
    if text.startswith("business recovery: 浏览器打开邀请链接"):
        return ""
    if text.startswith("business recovery: 浏览器页面存在 Log in 按钮"):
        return ""
    if text.startswith("business recovery: 浏览器 accept 完成 ->"):
        return ""
    if text.startswith("business recovery: 浏览器页面提示:"):
        return ""
    if text.startswith("business recovery: joined="):
        return ""

    if text.startswith("business recovery: 邀请登录重试"):
        return text.replace("business recovery:", "[邀请]", 1)
    if text.startswith("business recovery: 没有可用 team"):
        return "[邀请] 当前没有可用 team"
    if text.startswith("business recovery: 所有 team 尝试完毕仍未发出邀请"):
        return "[邀请] 所有 team 尝试完毕，仍未成功发出邀请"
    if text.startswith("business recovery: 账号暂未 joined team="):
        return text.replace("business recovery:", "[邀请]", 1)
    if text.startswith("business recovery: 所有 team 尝试完毕仍未 joined"):
        return "[邀请] 所有 team 尝试完毕，仍未 joined"
    if text.startswith("business recovery: 浏览器 accept 失败:"):
        return f"[邀请] 浏览器消费邀请链接失败：{_simplify_activation_error(text.split(':', 2)[-1])}"

    if text.startswith("[business-recovery]"):
        return ""

    if text.startswith("[business] auth 登录失败:"):
        detail = text.split(":", 1)[-1].strip()
        return f"[business] auth 登录失败：{_simplify_activation_error(detail)}"
    if text.startswith("[邀请] auth 登录状态消费邀请链接失败"):
        return "[邀请] 使用 auth 登录状态消费邀请链接失败"
    if text.startswith("[邀请] auth 登录状态已打开邀请链接，但 joined 未同步"):
        return "[邀请] 已打开邀请链接，但 joined 尚未同步"
    if text.startswith("抓取 free 工作空间失败，但不会回滚已保存的 business 信息"):
        return "[free] 抓取失败，但不会回滚已保存的 business 信息"
    if text.startswith("抓取 ") and " 工作空间失败" in text:
        return f"[结果] {_simplify_activation_error(text)}"

    if level in {"warning", "error"} and "失败" in text and not text.startswith("["):
        return f"[结果] {_simplify_activation_error(text)}"
    return text


class RestoredEmailService:
    def __init__(
        self,
        *,
        state: dict[str, Any],
        proxy: str | None = None,
        log_fn: Callable[[str, str], None] | None = None,
        task_control: Any | None = None,
        attempt_id: int | None = None,
    ):
        self._state = _with_current_tempmail_config(dict(state or {}))
        self._provider = str(self._state.get("provider") or "").strip()
        if not self._provider:
            raise ValueError("mailbox_state.provider 缺失")
        self._config = dict(self._state.get("config") or {})
        self._proxy = proxy if proxy is not None else self._state.get("proxy")
        self._log_fn = log_fn
        self._mailbox = create_mailbox(self._provider, extra=self._config, proxy=self._proxy)
        setattr(self._mailbox, "_log_fn", lambda message: self._log(str(message)))
        setattr(self._mailbox, "_task_control", task_control)
        setattr(self._mailbox, "_task_attempt_token", attempt_id)
        account_payload = dict(self._state.get("account") or {})
        self._acct = MailboxAccount(
            email=str(account_payload.get("email") or self._state.get("email") or "").strip(),
            account_id=str(account_payload.get("account_id") or "").strip(),
            extra=dict(account_payload.get("extra") or {}),
        )
        self._email = self._acct.email
        self._before_ids = set(self._state.get("before_ids") or [])
        self._last_verification_result = {}
        self.service_type = type("ST", (), {"value": self._provider})()

    def _log(self, message: str, level: str = "info") -> None:
        if callable(self._log_fn):
            try:
                self._log_fn(message, level)
            except TypeError:
                self._log_fn(message)

    def _ensure_restored_tempmail_account(self) -> None:
        if self._provider not in {"tempmail_local", "tempmail_api"}:
            return
        email = str(getattr(self._acct, "email", "") or self._email or self._state.get("email") or "").strip()
        if not email:
            return
        ensure_by_email = getattr(self._mailbox, "ensure_mailbox_by_email", None)
        if not callable(ensure_by_email):
            return

        previous_id = str(getattr(self._acct, "account_id", "") or "").strip()
        account = ensure_by_email(email)
        if not account or not str(getattr(account, "account_id", "") or "").strip():
            raise RuntimeError(f"TempMail 远端邮箱不可用，无法绑定自动收码: {email}")

        self._acct = account
        self._email = str(getattr(account, "email", "") or email).strip()
        action = str((getattr(account, "extra", None) or {}).get("mailbox_action") or "")
        current_id = str(getattr(account, "account_id", "") or "").strip()
        if action == "created_exact_address":
            self._log(f"[邮箱] 远端 TempMail 已过期/不存在，已按原地址新建: {self._email}")
        elif previous_id and current_id and previous_id != current_id:
            self._log(f"[邮箱] 远端 TempMail mailbox_id 已更新: {self._email}")
        else:
            self._log(f"[邮箱] 复用远端 TempMail 邮箱: {self._email}")

    def create_email(self, config=None) -> dict[str, str]:
        if not self._acct or not str(getattr(self._acct, "email", "") or "").strip():
            raise RuntimeError("恢复邮箱状态缺少 email，无法复用")
        self._ensure_restored_tempmail_account()
        get_current_ids = getattr(self._mailbox, "get_current_ids", None)
        if callable(get_current_ids):
            try:
                self._before_ids = set(get_current_ids(self._acct) or self._before_ids or [])
            except Exception:
                # 某些 provider 不支持旧账号列表探测；保留导出时的 before_ids 继续等新验证码。
                self._before_ids = set(self._before_ids or [])
        self._email = str(getattr(self._acct, "email", "") or "").strip()
        mailbox_action = str((getattr(self._acct, "extra", None) or {}).get("mailbox_action") or "restored_existing")
        return {
            "email": self._email,
            "service_id": str(getattr(self._acct, "account_id", "") or ""),
            "token": str(getattr(self._acct, "account_id", "") or ""),
            "mailbox_action": mailbox_action,
        }

    def get_verification_code(
        self,
        email=None,
        email_id=None,
        timeout=120,
        pattern=None,
        otp_sent_at=None,
        exclude_codes=None,
        phase=None,
        phase_label=None,
    ):
        code = self._mailbox.wait_for_code(
            self._acct,
            keyword="",
            timeout=int(timeout or 120),
            before_ids=self._before_ids,
            otp_sent_at=otp_sent_at,
            exclude_codes=exclude_codes,
            phase=phase,
            phase_label=phase_label,
        )
        self._last_verification_result = dict(
            getattr(self._mailbox, "_last_verification_result", None) or {}
        )
        return code

    def mark_verification_message_processed(self, message_id: str | None) -> None:
        normalized = str(message_id or "").strip()
        if normalized:
            self._before_ids.add(normalized)

    def export_state(self) -> dict[str, Any]:
        return {
            **dict(self._state),
            "provider": self._provider,
            "email": str(self._email or getattr(self._acct, "email", "") or "").strip(),
            "account": {
                "email": str(getattr(self._acct, "email", "") or self._email or "").strip(),
                "account_id": str(getattr(self._acct, "account_id", "") or "").strip(),
                "extra": getattr(self._acct, "extra", None) or {},
            },
            "before_ids": sorted(self._before_ids),
            "config": dict(self._config or {}),
            "proxy": self._proxy,
        }


def upsert_pending_invite_from_account(account: AccountModel) -> PendingBusinessInviteModel | None:
    extra = account.get_extra()
    pending = dict(extra.get("chatgpt_pending_business_invite") or {})
    if not pending:
        return None

    mailbox_state = _mailbox_state_from_account(account, extra=extra)
    registration_context = dict(extra.get("chatgpt_registration_context") or {})

    with Session(engine) as session:
        existing = session.exec(
            select(PendingBusinessInviteModel)
            .where(PendingBusinessInviteModel.account_id == int(account.id or 0))
        ).first()
        if existing is None:
            existing = PendingBusinessInviteModel(
                account_id=int(account.id or 0),
                email=account.email,
                status=str(pending.get("status") or "invite_sent_pending_activation"),
            )

        existing.email = account.email
        existing.status = str(pending.get("status") or existing.status or "invite_sent_pending_activation")
        existing.team_id = int(pending.get("team_id") or 0)
        existing.team_name = str(pending.get("team_name") or "")
        existing.invite_url = str(pending.get("invite_url") or existing.invite_url or "")
        existing.invite_workspace_id = str(pending.get("workspace_id") or existing.invite_workspace_id or "")
        existing.invite_message_id = str(pending.get("message_id") or existing.invite_message_id or "")
        existing.mail_provider = str(mailbox_state.get("provider") or extra.get("mail_provider") or "")
        existing.mailbox_state_json = _dumps(mailbox_state)
        existing.registration_context_json = _dumps(registration_context)
        existing.invited_at = str(pending.get("invite_sent_at") or pending.get("invited_at") or existing.invited_at or "")
        existing.last_error = ""
        existing.last_error_code = ""
        existing.last_checkpoint = str(
            pending.get("last_checkpoint") or existing.last_checkpoint or existing.status or "invite_sent_pending_activation"
        )
        existing.updated_at = _utcnow()
        _persist_account_mailbox_state_if_missing(session, int(account.id or 0), mailbox_state)
        session.add(existing)
        session.commit()
        session.refresh(existing)
        return SimpleNamespace(
            id=int(existing.id or 0),
            account_id=int(existing.account_id or 0),
            email=str(existing.email or ""),
            team_id=int(existing.team_id or 0),
            team_name=str(existing.team_name or ""),
            status=str(existing.status or ""),
        )


def upsert_pending_subscription_auth_from_account(
    account: AccountModel,
    *,
    checkout_url: str = "",
    plan: str = "",
    country: str = "",
    currency: str = "",
) -> SimpleNamespace:
    extra = account.get_extra()
    mailbox_state = _mailbox_state_from_account(account, extra=extra)
    registration_context = dict(extra.get("chatgpt_registration_context") or {})
    activation_context = {
        **registration_context,
        "activation_kind": "subscription_auth",
        "checkout_url": str(checkout_url or ""),
        "plan": str(plan or ""),
        "country": str(country or ""),
        "currency": str(currency or ""),
        "created_from_action": "payment_link",
    }
    now_iso = _utcnow().isoformat()

    with Session(engine) as session:
        existing = session.exec(
            select(PendingBusinessInviteModel)
            .where(PendingBusinessInviteModel.account_id == int(account.id or 0))
            .where(PendingBusinessInviteModel.status.notin_(tuple(NON_ACTIVATABLE_PENDING_STATUSES)))
        ).first()
        if existing is None:
            existing = PendingBusinessInviteModel(
                account_id=int(account.id or 0),
                email=account.email,
                status="subscription_pending_auth",
            )

        existing.email = account.email
        existing.status = "subscription_pending_auth"
        existing.team_id = 0
        existing.team_name = str(plan or "subscription")
        existing.invite_url = str(checkout_url or existing.invite_url or "")
        existing.mail_provider = str(mailbox_state.get("provider") or extra.get("mail_provider") or "")
        existing.mailbox_state_json = _dumps(mailbox_state)
        existing.registration_context_json = _dumps(activation_context)
        existing.invited_at = now_iso
        existing.last_error = ""
        existing.last_error_code = ""
        existing.last_checkpoint = "subscription_pending_auth"
        existing.updated_at = _utcnow()
        _persist_account_mailbox_state_if_missing(session, int(account.id or 0), mailbox_state)
        session.add(existing)
        session.commit()
        session.refresh(existing)
        return SimpleNamespace(
            id=int(existing.id or 0),
            account_id=int(existing.account_id or 0),
            email=str(existing.email or ""),
            team_id=0,
            team_name=str(existing.team_name or ""),
            status=str(existing.status or ""),
            activation_kind="subscription_auth",
        )


def list_pending_invites(*, status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 200), 1000))
    with Session(engine) as session:
        stmt = select(PendingBusinessInviteModel).order_by(
            PendingBusinessInviteModel.updated_at.desc(),
            PendingBusinessInviteModel.invited_at.desc(),
            PendingBusinessInviteModel.id.desc(),
        )
        if status:
            stmt = stmt.where(PendingBusinessInviteModel.status == status)
        rows = session.exec(stmt).all()
        items: list[dict[str, Any]] = []
        for row in rows[:limit]:
            account = session.get(AccountModel, row.account_id) if row.account_id else None
            registration_context = _loads(row.registration_context_json, {})
            activation_kind = str(registration_context.get("activation_kind") or "business_invite")
            items.append(
                {
                    "id": int(row.id or 0),
                    "account_id": int(row.account_id or 0),
                    "email": row.email,
                    "activation_kind": activation_kind,
                    "status": row.status,
                    "team_id": row.team_id,
                    "team_name": row.team_name,
                    "invite_workspace_id": row.invite_workspace_id,
                    "invited_at": row.invited_at,
                    "join_consumed_at": row.join_consumed_at,
                    "joined_at": row.joined_at,
                    "last_error": row.last_error,
                    "last_error_code": getattr(row, "last_error_code", "") or "",
                    "last_checkpoint": getattr(row, "last_checkpoint", "") or row.status or "",
                    "last_checkpoint_label": _checkpoint_label(getattr(row, "last_checkpoint", "") or row.status or ""),
                    "activation_attempt_count": int(getattr(row, "activation_attempt_count", 0) or 0),
                    "last_attempt_at": getattr(row, "last_attempt_at", "") or "",
                    "activation_run_id": getattr(row, "activation_run_id", "") or "",
                    "abandoned_at": getattr(row, "abandoned_at", "") or "",
                    "created_at": row.created_at.isoformat() if row.created_at else "",
                    "updated_at": row.updated_at.isoformat() if row.updated_at else "",
                    "has_invite_url": bool(row.invite_url),
                    "can_activate": _status_can_activate(str(row.status or "")),
                    "account_status": getattr(account, "status", "") if account else "",
                    "workspace_scope": (
                        (account.get_extra() or {}).get("chatgpt_workspace_scope") if account else ""
                    ) or "",
                }
            )
        return items


def list_pending_invite_ids_for_activation(*, invite_ids: Optional[list[int]] = None, limit: int = 200) -> list[int]:
    if invite_ids:
        seen: set[int] = set()
        ordered: list[int] = []
        for raw in invite_ids:
            try:
                invite_id = int(raw)
            except Exception:
                continue
            if invite_id <= 0 or invite_id in seen:
                continue
            seen.add(invite_id)
            ordered.append(invite_id)
        return ordered

    limit = max(1, min(int(limit or 200), 1000))
    with Session(engine) as session:
        rows = session.exec(
            select(PendingBusinessInviteModel)
            .where(PendingBusinessInviteModel.status.notin_(tuple(NON_ACTIVATABLE_PENDING_STATUSES)))
            .order_by(PendingBusinessInviteModel.id.asc())
        ).all()
        return [int(row.id or 0) for row in rows[:limit] if int(row.id or 0) > 0]


def abandon_pending_invite(invite_id: int) -> dict[str, Any]:
    updated = _mark_pending_invite_state(
        int(invite_id),
        status="abandoned",
        checkpoint="abandoned",
        error="已手动标记放弃，不再自动激活",
        error_code="abandoned",
        abandon=True,
    )
    if updated is None:
        raise ValueError("pending invite 不存在")
    return {
        "ok": True,
        "invite_id": int(updated.id or 0),
        "status": str(updated.status or "abandoned"),
    }


BUSINESS_SUBSCRIPTION_PLANS = {"team", "business", "enterprise"}


def _normalize_subscription_plan(value: Any) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    if normalized in {"chatgptteamplan", "team_plan", "teams"}:
        return "team"
    if normalized in {"business_plan", "workspace", "workspaces"}:
        return "business"
    if normalized in {"enterprise_plan"}:
        return "enterprise"
    if normalized in {"chatgptplusplan", "plus_plan"}:
        return "plus"
    if normalized in {"free_plan", "none"}:
        return "free"
    return normalized


def _infer_subscription_auth_capture_scope(
    *,
    registration_context: dict[str, Any],
    account_extra: dict[str, Any],
) -> tuple[bool, bool, str]:
    context = registration_context or {}
    extra = account_extra or {}
    capabilities = extra.get("chatgpt_capabilities") if isinstance(extra.get("chatgpt_capabilities"), dict) else {}
    local_probe = extra.get("chatgpt_local") if isinstance(extra.get("chatgpt_local"), dict) else {}
    local_subscription = local_probe.get("subscription") if isinstance(local_probe.get("subscription"), dict) else {}

    candidates = [
        context.get("plan"),
        context.get("checkout_plan"),
        context.get("subscription_plan"),
        extra.get("chatgpt_plan_type"),
        extra.get("plan_type"),
        capabilities.get("subscription_plan"),
        local_subscription.get("plan"),
        local_subscription.get("workspace_plan_type"),
    ]
    plan = ""
    for item in candidates:
        plan = _normalize_subscription_plan(item)
        if plan and plan != "unknown":
            break
    if plan in BUSINESS_SUBSCRIPTION_PLANS:
        return False, True, plan
    return True, False, plan or "free"


def _is_subscription_auth_retryable_error(error_text: str) -> bool:
    text = str(error_text or "").strip().lower()
    if not text:
        return False
    return any(marker.lower() in text for marker in SUBSCRIPTION_AUTH_RETRYABLE_ERROR_MARKERS)


def _subscription_auth_retry_delays_seconds(config: dict[str, Any] | None = None) -> list[int]:
    raw = (config or {}).get("chatgpt_subscription_auth_retry_delays_seconds")
    values: list[int] = []
    if isinstance(raw, (list, tuple)):
        candidates = raw
    elif raw not in (None, ""):
        candidates = re.split(r"[,\s]+", str(raw))
    else:
        candidates = DEFAULT_SUBSCRIPTION_AUTH_RETRY_DELAYS_SECONDS
    for item in candidates:
        try:
            seconds = int(float(str(item).strip()))
        except Exception:
            continue
        if seconds < 0:
            continue
        values.append(min(seconds, 600))
    return values or list(DEFAULT_SUBSCRIPTION_AUTH_RETRY_DELAYS_SECONDS)


def _run_subscription_auth_engine_once(
    *,
    mailbox_state: dict[str, Any],
    merged_config: dict[str, Any],
    browser_mode: str,
    account_email: str,
    account_password: str,
    log_fn: Callable[[str, str], None],
    proxy_url: str = "",
) -> RegistrationResult:
    email_service = RestoredEmailService(state=mailbox_state, log_fn=log_fn)
    engine_instance = RefreshTokenRegistrationEngine(
        email_service=email_service,
        proxy_url=proxy_url or None,
        callback_logger=lambda msg, level="info", *_: log_fn(msg, level),
        browser_mode=browser_mode,
        extra_config=merged_config,
    )
    engine_instance.email = account_email
    engine_instance.password = account_password
    return engine_instance.run()


def _account_identity_for_dedupe(account: Any) -> tuple[str, ...]:
    extra = dict(getattr(account, "extra", None) or {})
    return (
        str(getattr(account, "platform", "") or "").strip(),
        str(getattr(account, "email", "") or "").strip().lower(),
        str(extra.get("chatgpt_workspace_variant_key") or "").strip(),
        str(extra.get("workspace_id") or "").strip(),
        str(getattr(account, "user_id", "") or "").strip(),
        str(extra.get("refresh_token") or "").strip(),
    )


def _dedupe_linked_accounts(primary: Any, linked_accounts: list[Any]) -> list[Any]:
    seen = {_account_identity_for_dedupe(primary)}
    deduped: list[Any] = []
    for linked in linked_accounts or []:
        key = _account_identity_for_dedupe(linked)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(linked)
    return deduped


def _update_account_from_activation_result(account: AccountModel, result: RegistrationResult) -> None:
    from .chatgpt_registration_mode_adapter import RefreshTokenChatGPTRegistrationAdapter

    adapter = RefreshTokenChatGPTRegistrationAdapter()
    accounts = adapter._build_workspace_accounts(result, account.password)
    if not accounts:
        return

    primary = accounts[0]
    primary.extra = dict(primary.extra or {})
    primary.extra.pop("_linked_accounts_to_save", None)
    account.user_id = primary.user_id or account.user_id
    account.token = primary.token or account.token

    merged_extra = account.get_extra()
    merged_extra.update(primary.extra or {})
    merged_extra["chatgpt_pending_business_invite"] = {
        **dict(merged_extra.get("chatgpt_pending_business_invite") or {}),
        "status": "completed",
        "joined_at": _utcnow().isoformat(),
    }
    account.set_extra(merged_extra)
    apply_auth_capture_status(account, primary.status.value)
    account.updated_at = _utcnow()

    linked_accounts = _dedupe_linked_accounts(primary, accounts[1:])
    return primary, linked_accounts


def _activate_subscription_auth_pending(
    *,
    invite_id: int,
    pending_account_id: int,
    account_email: str,
    account_password: str,
    mailbox_state: dict[str, Any],
    registration_context: dict[str, Any],
    activation_run_id: str,
    log_fn: Callable[[str, str], None],
) -> dict[str, Any]:
    current_checkpoint = "activation_auth_login"
    _mark_pending_invite_state(
        int(invite_id),
        status=current_checkpoint,
        checkpoint=current_checkpoint,
        run_id=activation_run_id,
        increment_attempt=True,
        clear_error=True,
    )
    log_fn("[订阅] 开始重新登录并补抓 auth", "info")

    merged_config = config_store.get_all().copy()
    extra: dict[str, Any] = {}
    with Session(engine) as session:
        account = session.get(AccountModel, pending_account_id)
        extra = account.get_extra() if account else {}
        merged_config.update({k: v for k, v in extra.items() if v not in (None, "")})

    merged_config["chatgpt_existing_account_capture"] = True
    capture_free, capture_business, inferred_plan = _infer_subscription_auth_capture_scope(
        registration_context=registration_context or {},
        account_extra=extra or {},
    )
    merged_config["chatgpt_capture_free_workspace"] = capture_free
    merged_config["chatgpt_capture_business_workspace"] = capture_business
    log_fn(
        f"[订阅] 补抓范围：plan={inferred_plan}，free={'on' if capture_free else 'off'}，business={'on' if capture_business else 'off'}",
        "info",
    )
    browser_mode = str(
        (registration_context or {}).get("browser_mode")
        or merged_config.get("browser_mode")
        or merged_config.get("default_executor")
        or "protocol"
    )

    retry_delays = _subscription_auth_retry_delays_seconds(merged_config)
    max_attempts = 1 + len(retry_delays)
    result: RegistrationResult | None = None
    last_error = ""
    emitted_result_log_count = 0
    attempt = 0
    while attempt < max_attempts:
        attempt += 1
        if attempt > 1:
            log_fn(f"[订阅] 开始第 {attempt}/{max_attempts} 次补抓 auth", "info")
        result = _run_subscription_auth_engine_once(
            mailbox_state=mailbox_state,
            merged_config=merged_config,
            browser_mode=browser_mode,
            account_email=account_email,
            account_password=account_password,
            log_fn=log_fn,
            proxy_url="",
        )
        current_logs = list(getattr(result, "logs", None) or []) if result else []
        if current_logs and len(current_logs) > emitted_result_log_count:
            for line in current_logs[emitted_result_log_count:]:
                log_fn(str(line), "info")
            emitted_result_log_count = len(current_logs)
        if result and result.success:
            if attempt > 1:
                log_fn(f"[订阅] 第 {attempt} 次补抓成功", "info")
            break

        last_error = str(getattr(result, "error_message", "") or "订阅后 auth 补抓失败")
        if attempt >= max_attempts or not _is_subscription_auth_retryable_error(last_error):
            raise ValueError(last_error)

        delay_seconds = int(retry_delays[attempt - 1])
        compact_error = _simplify_activation_error(last_error)
        log_fn(
            f"[订阅] OpenAI 空间/权限可能仍在同步，本次失败：{compact_error}；等待 {delay_seconds}s 后自动重试 ({attempt + 1}/{max_attempts})",
            "warning",
        )
        _mark_pending_invite_state(
            int(invite_id),
            status="subscription_pending_auth",
            checkpoint="activation_auth_login",
            error=last_error,
            error_code="workspace_sync_retry_wait",
            run_id=activation_run_id,
        )
        if delay_seconds > 0:
            time.sleep(delay_seconds)

    if not result or not result.success:
        raise ValueError(last_error or "订阅后 auth 补抓失败")

    current_checkpoint = "activation_capturing_workspace"
    _mark_pending_invite_state(
        int(invite_id),
        status=current_checkpoint,
        checkpoint=current_checkpoint,
        run_id=activation_run_id,
        clear_error=True,
    )

    local_account_id = pending_account_id
    linked_account_ids: list[int] = []
    now_iso = _utcnow().isoformat()
    with Session(engine) as session:
        pending_db = session.get(PendingBusinessInviteModel, int(invite_id))
        account_db = session.get(AccountModel, pending_account_id)
        update_result = _update_account_from_activation_result(account_db, result)
        linked_accounts = update_result[1] if isinstance(update_result, tuple) and len(update_result) == 2 else []
        local_account_id = int(account_db.id or 0) if account_db is not None else pending_account_id
        session.add(account_db)
        for linked in linked_accounts or []:
            from core.db import save_account

            saved_linked = save_account(linked)
            saved_linked_id = int(getattr(saved_linked, "id", 0) or 0)
            if saved_linked_id > 0:
                linked_account_ids.append(saved_linked_id)

        if pending_db is not None:
            pending_db.status = "completed"
            pending_db.join_consumed_at = pending_db.join_consumed_at or now_iso
            pending_db.joined_at = pending_db.joined_at or now_iso
            pending_db.last_error = ""
            pending_db.last_error_code = ""
            pending_db.last_checkpoint = "completed"
            pending_db.activation_run_id = activation_run_id
            pending_db.updated_at = _utcnow()
            session.add(pending_db)
        session.commit()

    log_fn("[订阅] auth 补抓完成", "info")
    return {
        "ok": True,
        "invite_id": int(invite_id),
        "activation_kind": "subscription_auth",
        "email": account_email,
        "local_account_id": local_account_id,
        "linked_account_ids": linked_account_ids,
        "account_id": result.account_id,
        "workspace_id": result.workspace_id,
        "workspace_artifacts": result.workspace_artifacts or [],
        "activation_run_id": activation_run_id,
    }


def activate_pending_invite(
    invite_id: int,
    *,
    log_fn: Callable[[str], None] | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    def _log(message: str, level: str = "info") -> None:
        normalized = _normalize_activation_log_message(message, level)
        if normalized and callable(log_fn):
            log_fn(normalized)

    activation_run_id = str(run_id or _make_activation_run_id())
    current_checkpoint = "invite_sent_pending_activation"
    pending_account_id = 0
    account_email = ""

    try:
        with Session(engine) as session:
            pending = session.get(PendingBusinessInviteModel, int(invite_id))
            if pending is None:
                raise ValueError("pending invite 不存在")
            if str(pending.status or "") == "completed":
                raise ValueError("该 pending invite 已完成，无需重复激活")
            if str(pending.status or "") == "abandoned":
                raise ValueError("该 pending invite 已标记放弃")
            if str(pending.status or "") == "failed_terminal":
                raise ValueError("该 pending invite 已标记为终止失败，请先检查基础数据")

            pending_account_id = int(pending.account_id or 0)
            account = session.get(AccountModel, pending_account_id) if pending_account_id else None
            if account is None or account.platform != "chatgpt":
                raise ValueError("关联账号不存在")

            account_email = str(account.email or "").strip()
            account_password = str(account.password or "")
            pending_team_id = int(pending.team_id or 0)
            pending_invite_url = str(pending.invite_url or "").strip()
            pending_invite_workspace_id = str(pending.invite_workspace_id or "").strip()
            mailbox_state = _loads(pending.mailbox_state_json, {})
            registration_context = _loads(pending.registration_context_json, {})
            activation_kind = str(registration_context.get("activation_kind") or "business_invite")
            current_checkpoint = str(
                pending.last_checkpoint
                or ("activation_auth_login" if pending_invite_url else "invite_sent_pending_activation")
            )
            if not mailbox_state:
                raise ValueError("mailbox_state 缺失，无法继续激活")

        if activation_kind == "subscription_auth":
            return _activate_subscription_auth_pending(
                invite_id=int(invite_id),
                pending_account_id=pending_account_id,
                account_email=account_email,
                account_password=account_password,
                mailbox_state=mailbox_state,
                registration_context=registration_context,
                activation_run_id=activation_run_id,
                log_fn=_log,
            )

        _mark_pending_invite_state(
            int(invite_id),
            status="activation_auth_login" if pending_invite_url else "activation_fetching_invite_mail",
            checkpoint=current_checkpoint,
            run_id=activation_run_id,
            increment_attempt=True,
            clear_error=True,
        )
        _log(f"[激活] 从检查点继续：{_checkpoint_label(current_checkpoint)}")

        merged_config = config_store.get_all().copy()
        with Session(engine) as session:
            account = session.get(AccountModel, pending_account_id)
            extra = account.get_extra() if account else {}
            merged_config.update({k: v for k, v in extra.items() if v not in (None, "")})
            browser_mode = str((extra or {}).get("browser_mode") or merged_config.get("default_executor") or "protocol")

        email_service = RestoredEmailService(
            state=mailbox_state,
            log_fn=lambda msg, level="info": _log(msg, level),
        )
        email_adapter = EmailServiceAdapter(email_service, account_email, lambda msg, *_: _log(msg))
        recovery = BusinessWorkspaceRecovery(
            merged_config,
            proxy=None,
            browser_mode=browser_mode,
            log_fn=lambda msg, level="info": _log(msg, level),
        )

        register_client = SimpleNamespace(
            device_id=str(registration_context.get("device_id") or ""),
            ua=registration_context.get("user_agent"),
            sec_ch_ua=registration_context.get("sec_ch_ua"),
            impersonate=registration_context.get("impersonate"),
            fingerprint=registration_context.get("browser_fingerprint"),
            accept_language=registration_context.get("accept_language"),
        )

        activation_result: dict[str, Any] | None = None
        invite_url = pending_invite_url
        invite_workspace_id = pending_invite_workspace_id

        if pending_team_id > 0 and recovery.check_joined(pending_team_id, account_email, force=True):
            _log("[激活] 检测到账号已在 team 内，直接补抓工作空间")
            current_checkpoint = "activation_capturing_workspace"
            _mark_pending_invite_state(
                int(invite_id),
                status=current_checkpoint,
                checkpoint=current_checkpoint,
                run_id=activation_run_id,
                clear_error=True,
            )
            activation_result = _build_joined_activation_result(
                team_id=pending_team_id,
                invite_url=invite_url,
                workspace_id=invite_workspace_id,
            )
        else:
            if not invite_url:
                current_checkpoint = "activation_fetching_invite_mail"
                _mark_pending_invite_state(
                    int(invite_id),
                    status=current_checkpoint,
                    checkpoint=current_checkpoint,
                    run_id=activation_run_id,
                    clear_error=True,
                )
                invite_payload = recovery.fetch_invite_link_for_activation(
                    email=account_email,
                    email_adapter=email_adapter,
                )
                if not invite_payload:
                    raise ValueError("激活失败：未读取到邀请邮件或邀请链接")
                invite_url = str(invite_payload.get("invite_url") or "").strip()
                invite_workspace_id = str(
                    invite_payload.get("workspace_id") or pending_invite_workspace_id or ""
                ).strip()
                with Session(engine) as session:
                    pending_db = session.get(PendingBusinessInviteModel, int(invite_id))
                    if pending_db is not None:
                        pending_db.invite_url = invite_url
                        pending_db.invite_workspace_id = invite_workspace_id
                        pending_db.updated_at = _utcnow()
                        session.add(pending_db)
                        session.commit()
            else:
                _log("[激活] 复用已保存邀请链接")

            current_checkpoint = "activation_consuming_invite"
            _mark_pending_invite_state(
                int(invite_id),
                status=current_checkpoint,
                checkpoint=current_checkpoint,
                run_id=activation_run_id,
                clear_error=True,
            )
            activation_result = recovery.activate_pending_invite_for_account(
                email=account_email,
                password=account_password,
                invite_url=invite_url,
                team_id=pending_team_id,
                workspace_id=invite_workspace_id,
                device_id=register_client.device_id,
                email_adapter=email_adapter,
                user_agent=register_client.ua,
                accept_language=register_client.accept_language,
                sec_ch_ua=register_client.sec_ch_ua,
                impersonate=register_client.impersonate,
                browser_fingerprint=register_client.fingerprint,
                first_name=str(registration_context.get("first_name") or "John"),
                last_name=str(registration_context.get("last_name") or "Doe"),
                birthdate=str(registration_context.get("birthdate") or "1995-01-01"),
            )
            if not activation_result:
                raise ValueError("激活失败：auth 登录后未拿到可继续的 business 恢复结果")

        current_checkpoint = "activation_capturing_workspace"
        _mark_pending_invite_state(
            int(invite_id),
            status=current_checkpoint,
            checkpoint=current_checkpoint,
            run_id=activation_run_id,
            clear_error=True,
        )

        engine_instance = RefreshTokenRegistrationEngine(
            email_service=email_service,
            proxy_url=None,
            callback_logger=lambda msg, level="info", *_: _log(msg, level),
            browser_mode=browser_mode,
            extra_config=merged_config,
        )
        engine_instance.email = account_email
        engine_instance.password = account_password
        result = RegistrationResult(success=False, email=account_email, password=account_password, logs=[])
        ok = engine_instance._capture_workspace_artifacts_after_business_join(
            result=result,
            register_client=register_client,
            email_adapter=email_adapter,
            first_name=str(registration_context.get("first_name") or "John"),
            last_name=str(registration_context.get("last_name") or "Doe"),
            birthdate=str(registration_context.get("birthdate") or "1995-01-01"),
            business_join_result=activation_result,
        )
        if not ok:
            raise ValueError(result.error_message or "激活后抓取工作空间失败")

        local_account_id = pending_account_id
        linked_accounts = []
        linked_account_ids: list[int] = []
        now_iso = _utcnow().isoformat()
        with Session(engine) as session:
            pending_db = session.get(PendingBusinessInviteModel, int(invite_id))
            account_db = session.get(AccountModel, pending_account_id)
            update_result = _update_account_from_activation_result(account_db, result)
            if isinstance(update_result, tuple) and len(update_result) == 2:
                _, linked_accounts = update_result
            else:
                linked_accounts = update_result or []
            local_account_id = int(account_db.id or 0) if account_db is not None else pending_account_id
            session.add(account_db)
            for linked in linked_accounts:
                from core.db import save_account
                saved_linked = save_account(linked)
                saved_linked_id = int(getattr(saved_linked, "id", 0) or 0)
                if saved_linked_id > 0:
                    linked_account_ids.append(saved_linked_id)

            if pending_db is not None:
                pending_db.status = "completed"
                pending_db.join_consumed_at = pending_db.join_consumed_at or now_iso
                pending_db.joined_at = pending_db.joined_at or now_iso
                pending_db.last_error = ""
                pending_db.last_error_code = ""
                pending_db.last_checkpoint = "completed"
                pending_db.activation_run_id = activation_run_id
                pending_db.updated_at = _utcnow()
                session.add(pending_db)
            session.commit()

        return {
            "ok": True,
            "invite_id": int(invite_id),
            "email": account_email,
            "local_account_id": local_account_id,
            "linked_account_ids": linked_account_ids,
            "account_id": result.account_id,
            "workspace_id": result.workspace_id,
            "workspace_artifacts": result.workspace_artifacts or [],
            "activation_run_id": activation_run_id,
        }
    except Exception as exc:
        simplified_error = _simplify_activation_error(str(exc))
        error_code = _derive_activation_error_code(str(exc))
        failed_status = "failed_retryable" if _is_activation_error_retryable(error_code) else "failed_terminal"
        if int(invite_id or 0) > 0:
            _mark_pending_invite_state(
                int(invite_id),
                status=failed_status,
                checkpoint=current_checkpoint,
                error=simplified_error,
                error_code=error_code,
                run_id=activation_run_id,
            )
        raise ValueError(simplified_error) from exc


def activate_pending_invites(
    invite_ids: Optional[list[int]] = None,
    *,
    limit: int = 200,
    log_fn: Callable[[str], None] | None = None,
    on_success: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    def _log(message: str) -> None:
        if callable(log_fn):
            log_fn(message)

    resolved_ids = list_pending_invite_ids_for_activation(invite_ids=invite_ids, limit=limit)
    results: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    activation_run_id = _make_activation_run_id()

    for index, invite_id in enumerate(resolved_ids, start=1):
        email = ""
        with Session(engine) as session:
            row = session.get(PendingBusinessInviteModel, int(invite_id))
            email = str(getattr(row, "email", "") or "")
        account_label = email or f"invite_id={invite_id}"
        _log(f"[账号] -------- 激活 {index}/{len(resolved_ids)} | {account_label} --------")
        _log(f"[激活] 批量阶段 {index}/{len(resolved_ids)} invite_id={invite_id}")
        try:
            item = activate_pending_invite(invite_id, log_fn=log_fn, run_id=activation_run_id)
            if callable(on_success):
                on_success(item)
            results.append(item)
        except Exception as exc:
            simplified_error = _simplify_activation_error(str(exc))
            failed.append({"invite_id": int(invite_id), "email": email, "error": simplified_error})
            _log(f"[激活] invite_id={invite_id} 失败：{simplified_error}")

    return {
        "ok": len(failed) == 0,
        "total": len(resolved_ids),
        "success": len(results),
        "failed": len(failed),
        "results": results,
        "errors": failed,
        "activation_run_id": activation_run_id,
    }
