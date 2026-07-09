"""ChatGPT / Codex CLI 平台插件"""

import json
import random
import string

from core.base_mailbox import BaseMailbox
from core.base_platform import Account, BasePlatform, RegisterConfig
from core.proxy_utils import normalize_proxy_url, resolve_default_chatgpt_proxy
from services.chatgpt_core.chatgpt_registration_mode_adapter import (
    ChatGPTRegistrationContext,
    build_chatgpt_registration_mode_adapter,
)


class ChatGPTPlatform(BasePlatform):
    name = "chatgpt"
    display_name = "ChatGPT"
    version = "1.0.0"

    def __init__(self, config: RegisterConfig = None, mailbox: BaseMailbox = None):
        super().__init__(config)
        self.mailbox = mailbox

    def check_valid(self, account: Account) -> bool:
        try:
            from services.chatgpt_core.payment import check_subscription_status

            class _A:
                pass

            a = _A()
            extra = account.extra or {}
            a.access_token = extra.get("access_token") or account.token
            a.cookies = extra.get("cookies", "")
            proxy = resolve_default_chatgpt_proxy(self.config.proxy if self.config else None)
            status = check_subscription_status(a, proxy=proxy)
            return status not in ("expired", "invalid", "banned", None)
        except Exception:
            return False

    def register(self, email: str = None, password: str = None) -> Account:
        if not password:
            password = "".join(random.choices(string.ascii_letters + string.digits + "!@#$", k=16))

        browser_mode = (self.config.executor_type if self.config else None) or "protocol"
        extra_config = dict((self.config.extra or {}) if self.config and getattr(self.config, "extra", None) else {})
        task_control = getattr(self, "_task_control", None)
        if task_control is not None:
            extra_config.setdefault("_task_control", task_control)
        task_attempt_id = getattr(self, "_task_attempt_token", None)
        if task_attempt_id is not None:
            extra_config.setdefault("_task_attempt_id", task_attempt_id)
        explicit_proxy = normalize_proxy_url(self.config.proxy if self.config else None)
        proxy_mode_hint = str(
            extra_config.get("__register_proxy_mode")
            or extra_config.get("proxy_mode")
            or ""
        ).strip().lower()
        log_fn = getattr(self, "_log_fn", print)
        if proxy_mode_hint in {"none", "no_proxy", "direct", "直连"}:
            proxy = ""
            if explicit_proxy:
                log_fn("[代理] 已选择直连模式，忽略显式代理配置")
        elif explicit_proxy:
            proxy = explicit_proxy
        else:
            proxy = resolve_default_chatgpt_proxy(self.config.proxy if self.config else None)
        if proxy:
            proxy_label = proxy
            if "://" in proxy_label:
                scheme, rest = proxy_label.split("://", 1)
                if "@" in rest:
                    rest = rest.rsplit("@", 1)[1]
                    proxy_label = f"{scheme}://***:***@{rest}"
            log_fn(f"[代理] ChatGPT 注册核心链路 proxy={proxy_label}")
        else:
            log_fn("[代理] ChatGPT 注册核心链路 proxy=direct")
        max_retries = 3
        try:
            max_retries = int(extra_config.get("register_max_retries", 3) or 3)
        except Exception:
            max_retries = 3

        registration_entry = str(
            extra_config.get("chatgpt_registration_entry")
            or extra_config.get("registration_entry")
            or ""
        ).strip().lower().replace("-", "_")
        if registration_entry in {"phone", "phone_signup", "sms", "sms_signup"}:
            from services.chatgpt_core.phone_registration_engine import PhoneRegistrationEngine

            extra_config["chatgpt_registration_entry"] = "phone_signup"
            extra_config["chatgpt_registration_mode"] = "access_token_only"
            extra_config["chatgpt_has_refresh_token_solution"] = False
            if password:
                extra_config["password"] = password
            engine = PhoneRegistrationEngine(
                extra_config=extra_config,
                proxy_url=proxy,
                browser_mode=browser_mode,
                callback_logger=log_fn,
                stop_checker=(
                    (lambda: task_control.checkpoint(attempt_id=task_attempt_id))
                    if task_control is not None
                    else None
                ),
            )
            result = engine.run()
            if not result or not result.success:
                raise RuntimeError(result.error_message if result else "手机号注册失败")
            return engine.build_account(result)

        def _resolve_mailbox_timeout(requested_timeout: int) -> int:
            # email_api 的 API 本身只是控制面轮询，真正的等待窗口应服从
            # ChatGPT 状态机传入的 register/oauth OTP timeout。否则旧的全局
            # email_otp_timeout_seconds=20 会把 600s 注册等待压成 20s。
            if _mail_provider in {"email_api", "api_email", "email_otp_api", "mail_api_otp"}:
                try:
                    requested_seconds = int(requested_timeout)
                except (TypeError, ValueError):
                    requested_seconds = 0
                if requested_seconds > 0:
                    return requested_seconds
            candidates = (
                extra_config.get("mailbox_otp_timeout_seconds"),
                extra_config.get("email_otp_timeout_seconds"),
                extra_config.get("otp_timeout"),
                requested_timeout,
            )
            for value in candidates:
                if value in (None, ""):
                    continue
                try:
                    seconds = int(value)
                except (TypeError, ValueError):
                    continue
                if seconds > 0:
                    return seconds
            return requested_timeout

        if self.mailbox:
            _mailbox = self.mailbox
            _fixed_email = email
            _mail_provider = (
                str(extra_config.get("mail_provider") or "custom_provider").strip()
                or "custom_provider"
            )
            _task_id = str(extra_config.get("_current_task_id") or "").strip()

            def _json_loads(value, default):
                if isinstance(value, str) and value.strip():
                    try:
                        parsed = json.loads(value)
                    except Exception:
                        return default
                    return parsed if parsed is not None else default
                return value if value is not None else default

            def _export_mailbox_state_payload(acct, before_ids):
                account_extra = dict(getattr(acct, "extra", None) or {}) if acct is not None else {}
                state_config_exporter = getattr(_mailbox, "export_state_config", None)
                if callable(state_config_exporter):
                    try:
                        export_config = state_config_exporter(acct, extra_config)
                    except TypeError:
                        export_config = state_config_exporter()
                else:
                    export_config = {
                        key: value
                        for key, value in dict(extra_config or {}).items()
                        if key not in {"_task_control"} and not callable(value)
                    }
                return {
                    "provider": _mail_provider,
                    "email": str(_fixed_email or getattr(acct, "email", "") or "").strip(),
                    "account": {
                        "email": str(getattr(acct, "email", "") or "").strip(),
                        "account_id": str(getattr(acct, "account_id", "") or "").strip(),
                        "extra": account_extra,
                    },
                    "before_ids": sorted(before_ids or set()),
                    "config": export_config,
                    "proxy": proxy,
                }

            def _sync_hme_rerun_result(acct, *, success: bool, error_message: str = "", task_id: str = ""):
                if _mail_provider != "icloud_hme" or acct is None:
                    return
                account_extra = dict(getattr(acct, "extra", None) or {})
                anonymous_id = str(account_extra.get("anonymous_id") or getattr(acct, "account_id", "") or "").strip()
                hme = str(account_extra.get("hme") or getattr(acct, "email", "") or _fixed_email or "").strip()
                if not anonymous_id and not hme:
                    return
                try:
                    from services.chatgpt_account_state import is_account_deactivated_message

                    from core.db import sync_icloud_hme_rerun_result
                    sync_icloud_hme_rerun_result(
                        anonymous_id=anonymous_id,
                        hme=hme,
                        task_id=str(task_id or _task_id or "").strip(),
                        success=bool(success),
                        error_message=str(error_message or ""),
                        mailbox_state=_export_mailbox_state_payload(acct, getattr(email_service, "_before_ids", set()) if "email_service" in locals() else set()),
                        delete_candidate=bool(not success and is_account_deactivated_message("", str(error_message or ""))),
                    )
                except Exception as exc:
                    log_fn(f"[iCloudHME] 重跑进度写回失败: {exc}")

            def _resolve_email(candidate_email: str = "") -> str:
                resolved_email = str(_fixed_email or candidate_email or "").strip()
                if not resolved_email:
                    raise RuntimeError("custom_provider 返回空邮箱地址")
                return resolved_email

            class GenericEmailService:
                service_type = type("ST", (), {"value": _mail_provider})()

                def __init__(self):
                    self._acct = None
                    self._email = _fixed_email
                    self._before_ids = set()
                    self._mailbox = _mailbox
                    self._last_verification_result = {}

                def _can_reuse_current_account(self) -> bool:
                    acct = self._acct
                    if not acct:
                        return False
                    account_email = str(getattr(acct, "email", "") or "").strip()
                    if not account_email:
                        return False
                    get_current_ids = getattr(_mailbox, "get_current_ids", None)
                    if callable(get_current_ids):
                        try:
                            self._before_ids = set(get_current_ids(acct) or [])
                        except Exception:
                            return False
                    return True

                def _reuse_existing_email_payload(self):
                    if not self._can_reuse_current_account():
                        return None
                    generated_email = str(getattr(self._acct, "email", "") or "").strip()
                    if not self._email or not _fixed_email:
                        self._email = _resolve_email(generated_email)
                    return {
                        "email": self._email,
                        "service_id": str(getattr(self._acct, "account_id", "") or ""),
                        "token": "",
                        "mailbox_action": "reused_existing",
                    }

                def create_email(self, config=None):
                    reused = self._reuse_existing_email_payload()
                    if reused:
                        return reused

                    try:
                        self._acct = _mailbox.get_email()
                    except Exception:
                        reused_after_error = self._reuse_existing_email_payload()
                        if reused_after_error:
                            reused_after_error["mailbox_action"] = "recovered_after_create_error"
                            return reused_after_error
                        raise

                    get_current_ids = getattr(_mailbox, "get_current_ids", None)
                    if callable(get_current_ids):
                        self._before_ids = set(get_current_ids(self._acct) or [])
                    else:
                        self._before_ids = set()
                    generated_email = getattr(self._acct, "email", "")
                    if not self._email:
                        self._email = _resolve_email(generated_email)
                    elif not _fixed_email:
                        self._email = _resolve_email(generated_email)
                    mailbox_action = str((getattr(self._acct, "extra", None) or {}).get("mailbox_action") or "created")
                    return {
                        "email": self._email,
                        "service_id": self._acct.account_id,
                        "token": "",
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
                    if not self._acct:
                        raise RuntimeError("邮箱账户尚未创建，无法获取验证码")
                    code = _mailbox.wait_for_code(
                        self._acct,
                        keyword="",
                        timeout=_resolve_mailbox_timeout(timeout),
                        before_ids=self._before_ids,
                        otp_sent_at=otp_sent_at,
                        exclude_codes=exclude_codes,
                        phase=phase,
                        phase_label=phase_label,
                    )
                    self._last_verification_result = dict(getattr(_mailbox, "_last_verification_result", None) or {})
                    return code

                def export_state(self):
                    return _export_mailbox_state_payload(self._acct, self._before_ids)

                def finalize_success(self, account_email: str = "", task_id: str = ""):
                    if not self._acct:
                        return
                    finalize = getattr(self._mailbox, "finalize_success", None)
                    resolved_task_id = str(task_id or _task_id or "").strip()
                    if callable(finalize):
                        finalize(
                            self._acct,
                            registered_email=str(account_email or self._email or "").strip(),
                            task_id=resolved_task_id,
                        )
                    _sync_hme_rerun_result(self._acct, success=True, task_id=resolved_task_id)

                def finalize_failure(self, error_message: str = "", task_id: str = ""):
                    if not self._acct:
                        return
                    finalize = getattr(self._mailbox, "finalize_failure", None)
                    resolved_task_id = str(task_id or _task_id or "").strip()
                    normalized_error = str(error_message or "").strip()
                    if callable(finalize):
                        finalize(
                            self._acct,
                            error_message=normalized_error,
                            task_id=resolved_task_id,
                        )
                    _sync_hme_rerun_result(self._acct, success=False, error_message=normalized_error, task_id=resolved_task_id)

                def update_status(self, success, error=None):
                    pass

                @property
                def status(self):
                    return None

            email_service = GenericEmailService()
        else:
            from core.base_mailbox import TempMailLolMailbox

            _tmail = TempMailLolMailbox(proxy=proxy)
            _tmail._task_control = getattr(self, "_task_control", None)

            class TempMailEmailService:
                service_type = type("ST", (), {"value": "tempmail_lol"})()

                def __init__(self):
                    self._acct = None
                    self._before_ids = set()
                    self._mailbox = _tmail
                    self._last_verification_result = {}

                def _can_reuse_current_account(self) -> bool:
                    acct = self._acct
                    if not acct:
                        return False
                    account_email = str(getattr(acct, "email", "") or "").strip()
                    if not account_email:
                        return False
                    try:
                        self._before_ids = set(_tmail.get_current_ids(acct) or [])
                    except Exception:
                        return False
                    return True

                def create_email(self, config=None):
                    if self._can_reuse_current_account():
                        return {
                            "email": str(getattr(self._acct, "email", "") or "").strip(),
                            "service_id": str(getattr(self._acct, "account_id", "") or ""),
                            "token": str(getattr(self._acct, "account_id", "") or ""),
                            "mailbox_action": "reused_existing",
                        }

                    try:
                        acct = _tmail.get_email()
                    except Exception:
                        if self._can_reuse_current_account():
                            return {
                                "email": str(getattr(self._acct, "email", "") or "").strip(),
                                "service_id": str(getattr(self._acct, "account_id", "") or ""),
                                "token": str(getattr(self._acct, "account_id", "") or ""),
                                "mailbox_action": "recovered_after_create_error",
                            }
                        raise

                    self._acct = acct
                    self._before_ids = set(_tmail.get_current_ids(acct) or [])
                    resolved_email = str(getattr(acct, "email", "") or "").strip()
                    if not resolved_email:
                        raise RuntimeError("tempmail_lol 返回空邮箱地址")
                    return {
                        "email": resolved_email,
                        "service_id": acct.account_id,
                        "token": acct.account_id,
                        "mailbox_action": "created",
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
                    code = _tmail.wait_for_code(
                        self._acct,
                        keyword="",
                        timeout=_resolve_mailbox_timeout(timeout),
                        before_ids=self._before_ids,
                        otp_sent_at=otp_sent_at,
                        exclude_codes=exclude_codes,
                        phase=phase,
                        phase_label=phase_label,
                    )
                    self._last_verification_result = dict(getattr(_tmail, "_last_verification_result", None) or {})
                    return code

                def export_state(self):
                    return {
                        "provider": "tempmail_lol",
                        "email": str(getattr(self._acct, "email", "") or "").strip(),
                        "account": {
                            "email": str(getattr(self._acct, "email", "") or "").strip(),
                            "account_id": str(getattr(self._acct, "account_id", "") or "").strip(),
                            "extra": getattr(self._acct, "extra", None) or {},
                        },
                        "before_ids": sorted(self._before_ids),
                        "config": dict(extra_config or {}),
                        "proxy": proxy,
                    }

                def update_status(self, success, error=None):
                    pass

                @property
                def status(self):
                    return None

            email_service = TempMailEmailService()

        adapter = build_chatgpt_registration_mode_adapter(extra_config)
        context = ChatGPTRegistrationContext(
            email_service=email_service,
            proxy_url=proxy,
            callback_logger=log_fn,
            email=email,
            password=password,
            browser_mode=browser_mode,
            max_retries=max_retries,
            extra_config=extra_config,
        )
        result = adapter.run(context)
        metadata = (getattr(result, "metadata", None) or {}) if result else {}
        is_deferred_pending = bool(
            result
            and isinstance(metadata, dict)
            and metadata.get("deferred_activation")
            and str(metadata.get("deferred_activation_status") or "") == "invite_sent_pending_activation"
            and metadata.get("registration_stage_complete")
        )
        if not result or (not result.success and not is_deferred_pending):
            raise RuntimeError(result.error_message if result else "注册失败")

        return adapter.build_account(result, password)

    def get_platform_actions(self) -> list:
        return [
            {"id": "probe_local_status", "label": "探测本地状态", "params": []},
            {
                "id": "k12_workspace_recapture",
                "label": "重新进入/导出 K12",
                "params": [
                    {
                        "key": "workspace_ids",
                        "label": "目标 K12 workspace_id",
                        "type": "textarea",
                        "default": "",
                    }
                ],
            },
            {"id": "sync_cliproxyapi_status", "label": "同步 CLIProxyAPI 状态", "params": []},
            {"id": "sync_sub2api_status", "label": "同步 Sub2API 状态", "params": []},
            {"id": "sync_oaipay_status", "label": "同步 OAIPay 状态", "params": []},
            {"id": "refresh_token", "label": "刷新 Token", "params": []},
            {
                "id": "payment_link",
                "label": "生成订阅链接",
                "params": [
                    {"key": "plan", "label": "套餐", "type": "select", "options": ["plus", "team"]},
                    {"key": "country", "label": "地区", "type": "checkout_country", "default": "ID"},
                    {"key": "currency", "label": "货币", "type": "text", "default": "IDR"},
                    {
                        "key": "payment_link_format",
                        "label": "生成路径",
                        "type": "select",
                        "options": ["long_hosted", "short_chatgpt"],
                        "default": "long_hosted",
                    },
                ],
            },
            {
                "id": "resume_subscription_auth",
                "label": "补抓Auth",
                "params": [
                    {
                        "key": "allow_phone_verification",
                        "label": "允许手机号验证",
                        "type": "boolean",
                    }
                ],
            },
            {"id": "invalid_recheck", "label": "失效测活", "params": []},
            {
                "id": "upload_cpa",
                "label": "上传 CPA",
                "params": [
                    {"key": "api_url", "label": "CPA API URL", "type": "text"},
                    {"key": "api_key", "label": "CPA API Key", "type": "text"},
                ],
            },
            {
                "id": "upload_sub2api",
                "label": "上传 Sub2API",
                "params": [
                    {"key": "api_url", "label": "Sub2API API URL", "type": "text"},
                    {"key": "api_key", "label": "Sub2API API Key", "type": "text"},
                ],
            },
            {
                "id": "upload_tm",
                "label": "上传 Team Manager",
                "params": [
                    {"key": "api_url", "label": "TM API URL", "type": "text"},
                    {"key": "api_key", "label": "TM API Key", "type": "text"},
                ],
            },
            {
                "id": "upload_codex_proxy",
                "label": "上传 CodexProxy",
                "params": [
                    {"key": "api_url", "label": "API URL", "type": "text"},
                    {"key": "api_key", "label": "Admin Key", "type": "text"},
                ],
            },
            {
                "id": "upload_oaipay",
                "label": "上传 OAIPay",
                "params": [
                    {"key": "category_id", "label": "OAIPay 分类ID", "type": "text"},
                ],
            },
        ]

    def execute_action(self, action_id: str, account: Account, params: dict) -> dict:
        extra = account.extra or {}

        class _A:
            pass

        a = _A()
        a.email = account.email
        a.access_token = extra.get("access_token") or account.token
        a.refresh_token = extra.get("refresh_token", "")
        a.id_token = extra.get("id_token", "")
        a.session_token = extra.get("session_token", "")
        a.client_id = extra.get("client_id", "app_EMoamEEZ73f0CkXaXp7hrann")
        a.cookies = extra.get("cookies", "")
        a.user_id = account.user_id
        a.workspace_id = extra.get("workspace_id", "")
        a.extra = extra

        if action_id == "probe_local_status":
            from services.chatgpt_core.status_probe import probe_local_chatgpt_status
            from core.proxy_utils import resolve_probe_candidate_proxies, is_proxy_error_text

            candidates = resolve_probe_candidate_proxies(
                params,
                fallback_proxy=None,
                default_mode="global",
            )
            last_error = None
            for i, (proxy_url, proxy_pool, source) in enumerate(candidates):
                try:
                    probe_result = probe_local_chatgpt_status(
                        a,
                        proxy=proxy_url,
                        use_default_proxy=False,
                    )
                    auth_state = str(probe_result.get("auth", {}).get("state") or "").strip()
                    if auth_state == "probe_failed" and i < len(candidates) - 1:
                        if proxy_pool is not None and proxy_url:
                            msg = str(probe_result.get("auth", {}).get("message") or "")
                            if is_proxy_error_text(msg):
                                try:
                                    proxy_pool.report_fail(proxy_url)
                                except Exception:
                                    pass
                        continue
                    if proxy_pool is not None and proxy_url:
                        try:
                            proxy_pool.report_success(proxy_url)
                        except Exception:
                            pass
                    summary = (
                        f"认证={probe_result.get('auth', {}).get('state', 'unknown')}, "
                        f"订阅={probe_result.get('subscription', {}).get('plan', 'unknown')}, "
                        f"Codex={probe_result.get('codex', {}).get('state', 'unknown')}"
                    )
                    return {
                        "ok": True,
                        "data": {
                            "message": f"本地状态探测完成：{summary}",
                            "probe": probe_result,
                        },
                        "account_extra_patch": {
                            "chatgpt_local": probe_result,
                        },
                    }
                except Exception as exc:
                    last_error = exc
                    if proxy_pool is not None and proxy_url and is_proxy_error_text(str(exc)):
                        try:
                            proxy_pool.report_fail(proxy_url)
                        except Exception:
                            pass
                    if i == len(candidates) - 1:
                        raise
            if last_error:
                raise last_error

        if action_id == "sync_cliproxyapi_status":
            from services.cliproxyapi_sync import sync_chatgpt_cliproxyapi_status

            sync_result = sync_chatgpt_cliproxyapi_status(a)
            ok = bool(sync_result.get("uploaded")) and sync_result.get("remote_state") not in {"unreachable", "not_found"}
            summary = (
                f"远端状态={sync_result.get('status') or 'not_found'}, "
                f"探测={sync_result.get('remote_state') or 'not_checked'}"
            )
            return {
                "ok": ok,
                "data": {
                    "message": f"CLIProxyAPI 状态同步完成：{summary}",
                    "sync": sync_result,
                },
                "error": sync_result.get("message") if not ok else "",
                "account_extra_patch": {
                    "sync_statuses": {
                        "cliproxyapi": sync_result,
                    },
                },
            }

        if action_id == "sync_sub2api_status":
            from services.sub2api_sync import probe_chatgpt_sub2api_status

            sync_result = probe_chatgpt_sub2api_status(a)
            remote_state = str(sync_result.get("remote_state") or "").strip().lower()
            ok = remote_state in {"exists", "not_found", "cross_workspace_only"}
            summary = (
                f"远端状态={sync_result.get('status') or remote_state or 'unknown'}, "
                f"探测={remote_state or 'not_checked'}"
            )
            return {
                "ok": ok,
                "data": {
                    "message": f"Sub2API 状态同步完成：{summary}",
                    "sync": sync_result,
                },
                "error": sync_result.get("message") if not ok else "",
                "account_extra_patch": {
                    "sync_statuses": {
                        "sub2api": sync_result,
                    },
                },
            }

        if action_id == "sync_oaipay_status":
            from services.oaipay_sync import probe_chatgpt_oaipay_status

            sync_result = probe_chatgpt_oaipay_status(a)
            remote_state = str(sync_result.get("remote_state") or "").strip().lower()
            ok = remote_state in {"exists", "not_found", "cross_workspace_only"}
            summary = (
                f"远端状态={sync_result.get('status') or remote_state or 'unknown'}, "
                f"探测={remote_state or 'not_checked'}"
            )
            return {
                "ok": ok,
                "data": {
                    "message": f"OAIPay 状态同步完成：{summary}",
                    "sync": sync_result,
                },
                "error": sync_result.get("message") if not ok else "",
                "account_extra_patch": {
                    "sync_statuses": {
                        "oaipay": sync_result,
                    },
                },
            }

        if action_id == "refresh_token":
            from services.chatgpt_core.token_refresh import TokenRefreshManager

            proxy = resolve_default_chatgpt_proxy(self.config.proxy if self.config else None)
            manager = TokenRefreshManager(proxy_url=proxy)
            result = manager.refresh_account(a)
            if result.success:
                return {
                    "ok": True,
                    "data": {
                        "access_token": result.access_token,
                        "refresh_token": result.refresh_token,
                    },
                }
            return {"ok": False, "error": result.error_message}

        if action_id == "payment_link":
            from services.chatgpt_core.payment import generate_plus_link, generate_team_link
            from services.chatgpt_core.payment_link_cache import (
                normalize_payment_link_output_format,
                normalize_payment_link_url,
                payment_link_url_requires_regeneration,
            )

            plan = str(params.get("plan") or "plus").strip().lower()
            if plan not in {"plus", "team"}:
                plan = "plus"
            country = str(params.get("country") or "ID").strip().upper() or "ID"
            currency = str(params.get("currency") or "IDR").strip().upper() or "IDR"
            payment_proxy = resolve_default_chatgpt_proxy(normalize_proxy_url(params.get("proxy")) or None)
            payment_link_format = normalize_payment_link_output_format(params.get("payment_link_format"))
            promo_code = str(params.get("promo_code") or "").strip()
            save_defaults = params.get("save_defaults") is not False
            reuse_cached_link = params.get("reuse_cached_link") is not False
            cached_link = extra.get("chatgpt_last_payment_link") if isinstance(extra.get("chatgpt_last_payment_link"), dict) else {}
            cached_format = normalize_payment_link_output_format(cached_link.get("payment_link_format") or "long_hosted")
            cached_url = normalize_payment_link_url(cached_link.get("url"), cached_format)
            cached_url_needs_regeneration = payment_link_url_requires_regeneration(cached_link.get("url"), cached_format)
            cached_plan = str(cached_link.get("plan") or "").strip().lower()
            cached_country = str(cached_link.get("country") or "").strip().upper()
            cached_currency = str(cached_link.get("currency") or "").strip().upper()
            cached_proxy = normalize_proxy_url(cached_link.get("proxy")) or ""
            billing = {
                "name": str(params.get("billing_name") or "").strip(),
                "email": str(params.get("billing_email") or getattr(a, "email", "") or "").strip(),
                "country": str(params.get("billing_country") or country).strip().upper() or country,
                "line1": str(params.get("billing_line1") or "").strip(),
                "city": str(params.get("billing_city") or "").strip(),
                "state": str(params.get("billing_state") or "").strip(),
                "postal_code": str(params.get("billing_postal_code") or "").strip(),
            }
            cached_billing = cached_link.get("billing") if isinstance(cached_link.get("billing"), dict) else {}
            defaults_patch = (
                {
                    "chatgpt_payment_link_defaults": {
                        "plan": plan,
                        "country": country,
                        "currency": currency,
                        "proxy": payment_proxy,
                        "payment_link_format": payment_link_format,
                        "promo_code": promo_code,
                        "workspace_name": str(params.get("workspace_name") or "MyTeam").strip() or "MyTeam",
                        "seat_quantity": max(2, int(params.get("seat_quantity", 5) or 5)),
                        "price_interval": str(params.get("price_interval") or "month").strip().lower() or "month",
                    },
                }
                if save_defaults
                else {}
            )
            should_reuse_cached_link = (
                reuse_cached_link
                and bool(cached_url)
                and not cached_url_needs_regeneration
                and cached_plan == plan
                and cached_country == country
                and cached_currency == currency
                and cached_proxy == payment_proxy
                and cached_format == payment_link_format
            )
            if should_reuse_cached_link:
                return {
                    "ok": True,
                    "data": {
                        "url": cached_url,
                        "plan": plan,
                        "country": country,
                        "currency": currency,
                        "proxy": payment_proxy,
                        "payment_link_format": payment_link_format,
                        "promo_code": str(cached_link.get("promo_code") or promo_code).strip(),
                        "billing": cached_billing or billing,
                        "cache_reused": True,
                        "cache_source": str(cached_link.get("source") or "cached_payment_link"),
                        "message": "已复用缓存订阅链接",
                    },
                    "account_extra_patch": defaults_patch,
                }
            if plan == "plus":
                url = generate_plus_link(
                    a,
                    proxy=payment_proxy,
                    country=country,
                    currency=currency,
                    billing=billing,
                    link_format=payment_link_format,
                )
            else:
                url = generate_team_link(
                    a,
                    workspace_name=params.get("workspace_name", "MyTeam"),
                    price_interval=params.get("price_interval", "month"),
                    seat_quantity=int(params.get("seat_quantity", 5) or 5),
                    promo_code=promo_code,
                    proxy=payment_proxy,
                    country=country,
                    currency=currency,
                    billing=billing,
                    link_format=payment_link_format,
                )
            return {
                "ok": bool(url),
                "data": {
                    "url": url,
                    "plan": plan,
                    "country": country,
                    "currency": currency,
                    "proxy": payment_proxy,
                    "payment_link_format": payment_link_format,
                    "promo_code": promo_code,
                    "billing": billing,
                    "cache_reused": False,
                    "cache_source": "payment_link_action",
                },
                "account_extra_patch": defaults_patch,
            }

        if action_id == "resume_subscription_auth":
            return {
                "ok": True,
                "data": {
                    "message": "已提交补抓 Auth 请求",
                    "activation_kind": "subscription_auth",
                },
            }

        if action_id == "invalid_recheck":
            return {
                "ok": True,
                "data": {
                    "message": "已提交失效测活请求",
                    "activation_kind": "invalid_recheck",
                },
            }

        if action_id == "upload_cpa":
            from services.chatgpt_core.cpa_upload import generate_token_json, upload_to_cpa
            from services.chatgpt_account_state import is_chatgpt_upload_ready

            ready, gate_msg, _capabilities = is_chatgpt_upload_ready(a)
            if not ready:
                return {"ok": False, "data": gate_msg, "error": gate_msg}

            token_data = generate_token_json(a)
            ok, msg = upload_to_cpa(
                token_data,
                api_url=params.get("api_url"),
                api_key=params.get("api_key"),
            )
            return {"ok": ok, "data": msg}

        if action_id == "upload_sub2api":
            from services.chatgpt_core.sub2api_upload import upload_to_sub2api
            from services.chatgpt_account_state import is_chatgpt_upload_ready

            ready, gate_msg, _capabilities = is_chatgpt_upload_ready(a)
            if not ready:
                return {"ok": False, "data": gate_msg, "error": gate_msg}

            ok, msg = upload_to_sub2api(
                a,
                api_url=params.get("api_url"),
                api_key=params.get("api_key"),
            )
            return {"ok": ok, "data": msg}

        if action_id == "upload_tm":
            from services.chatgpt_core.cpa_upload import upload_to_team_manager
            from services.chatgpt_account_state import is_chatgpt_upload_ready

            ready, gate_msg, _capabilities = is_chatgpt_upload_ready(a)
            if not ready:
                return {"ok": False, "data": gate_msg, "error": gate_msg}

            ok, msg = upload_to_team_manager(
                a,
                api_url=params.get("api_url"),
                api_key=params.get("api_key"),
            )
            return {"ok": ok, "data": msg}

        if action_id == "upload_codex_proxy":
            from services.chatgpt_account_state import is_chatgpt_upload_ready

            ready, gate_msg, _capabilities = is_chatgpt_upload_ready(a)
            if not ready:
                return {"ok": False, "data": gate_msg, "error": gate_msg}

            upload_type = str(
                params.get("upload_type")
                or (self.config.extra or {}).get("codex_proxy_upload_type")
                or "at"
            ).strip().lower()

            if upload_type == "rt":
                from services.chatgpt_core.cpa_upload import upload_to_codex_proxy

                ok, msg = upload_to_codex_proxy(
                    a,
                    api_url=params.get("api_url"),
                    api_key=params.get("api_key"),
                )
            else:
                from services.chatgpt_core.cpa_upload import upload_at_to_codex_proxy

                ok, msg = upload_at_to_codex_proxy(
                    a,
                    api_url=params.get("api_url"),
                    api_key=params.get("api_key"),
                )
            return {"ok": ok, "data": msg}

        raise NotImplementedError(f"未知操作: {action_id}")
