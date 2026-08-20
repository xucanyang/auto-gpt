"""ChatGPT / Codex CLI 平台插件"""

import json
import secrets

from core.base_mailbox import BaseMailbox
from core.base_platform import Account, BasePlatform, RegisterConfig
from core.proxy_utils import normalize_proxy_url, resolve_default_chatgpt_proxy
from services.chatgpt_core.chatgpt_registration_mode_adapter import (
    ChatGPTRegistrationContext,
    build_chatgpt_registration_mode_adapter,
)
from services.chatgpt_core.mailbox_state import (
    build_mailbox_state,
    export_mailbox_state_config,
    normalize_mailbox_provider,
)


def _generate_chatgpt_registration_password(length: int = 16) -> str:
    """Generate a password that always satisfies the OpenAI signup form."""
    minimum_length = 12
    size = max(int(length or minimum_length), minimum_length)
    specials = ",._!@#"
    characters = [
        secrets.choice("abcdefghijklmnopqrstuvwxyz"),
        secrets.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ"),
        secrets.choice("0123456789"),
        secrets.choice(specials),
    ]
    pool = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" + specials
    characters.extend(secrets.choice(pool) for _ in range(size - len(characters)))
    secrets.SystemRandom().shuffle(characters)
    return "".join(characters)


def _execute_chatgpt_short_payment_link(
    account: object,
    params: dict,
    *,
    default_proxy: str | None = None,
) -> dict:
    """Generate a Plus checkout URL that remains bound to ChatGPT Web login."""

    from datetime import datetime, timezone
    import uuid

    from services.chatgpt_core.payment import (
        PAYMENT_LINK_FORMAT_SHORT,
        extract_checkout_session_id,
        generate_plus_short_link,
        has_chatgpt_web_session,
        normalize_checkout_country,
        normalize_checkout_currency,
    )
    from services.chatgpt_core.payment_link_cache import (
        PAYMENT_SOURCE_CHATGPT_HOSTED,
        normalize_payment_link_params,
        normalize_payment_link_url,
        payment_link_cache_for_params,
        payment_link_cache_matches,
        payment_link_variant_key,
    )

    if not has_chatgpt_web_session(account):
        return {
            "ok": False,
            "error": "账号缺少 ChatGPT Web Session Cookie，无法生成登录态短链",
        }

    country = normalize_checkout_country(params.get("country") or "US")
    currency = normalize_checkout_currency(params.get("currency") or "USD", country)
    cache_request = normalize_payment_link_params(
        {
            "plan": "plus",
            "country": country,
            "currency": currency,
            "proxy": "",
            "payment_link_format": PAYMENT_LINK_FORMAT_SHORT,
            "payment_source": PAYMENT_SOURCE_CHATGPT_HOSTED,
        }
    )
    cache_request["variant_key"] = payment_link_variant_key(cache_request)
    extra = getattr(account, "extra", None)
    extra = extra if isinstance(extra, dict) else {}
    cached_link = payment_link_cache_for_params(extra, cache_request)
    cached_url = normalize_payment_link_url(
        cached_link.get("url"),
        cached_link.get("payment_link_format") or PAYMENT_LINK_FORMAT_SHORT,
    )
    if (
        params.get("reuse_cached_link") is not False
        and cached_url
        and payment_link_cache_matches(cached_link, cache_request)
    ):
        cached_data = dict(cached_link)
        cached_data.update(
            {
                "url": cached_url,
                "plan": "plus",
                "country": country,
                "currency": currency,
                "proxy": "",
                "payment_link_format": PAYMENT_LINK_FORMAT_SHORT,
                "payment_source": PAYMENT_SOURCE_CHATGPT_HOSTED,
                "link_type": "chatgpt",
                "checkout_ui_mode": "custom",
                "login_required": True,
                "web_session_available": True,
                "cache_reused": True,
                "cache_source": str(cached_link.get("source") or "cached_payment_link"),
                "message": "已复用缓存登录态短链",
            }
        )
        return {"ok": True, "data": cached_data, "account_extra_patch": {}}

    requested_proxy = normalize_proxy_url(params.get("proxy"))
    if requested_proxy or default_proxy:
        payment_proxy = resolve_default_chatgpt_proxy(requested_proxy or default_proxy)
    else:
        try:
            payment_proxy = resolve_default_chatgpt_proxy(None)
        except RuntimeError as exc:
            # A local short-link request is still valid in direct mode when no
            # dynamic proxy template has been configured. Explicit proxy/config
            # errors remain visible to the operator.
            if "动态代理模式" in str(exc):
                payment_proxy = ""
            else:
                raise
    url = generate_plus_short_link(
        account,
        proxy=payment_proxy,
        country=country,
        currency=currency,
    )
    request_id = str(params.get("request_id") or "").strip() or f"local:{uuid.uuid4().hex}"
    generated_at = datetime.now(timezone.utc).isoformat()
    data = {
        "url": url,
        "plan": "plus",
        "generation_kind": "plus_checkout",
        "variant_key": cache_request["variant_key"],
        "country": country,
        "currency": currency,
        "proxy": "",
        "payment_link_format": PAYMENT_LINK_FORMAT_SHORT,
        "payment_source": PAYMENT_SOURCE_CHATGPT_HOSTED,
        "link_type": "chatgpt",
        "checkout_ui_mode": "custom",
        "login_required": True,
        "web_session_available": True,
        "cs_id": extract_checkout_session_id(url),
        "processor_entity": str(extra.get("chatgpt_checkout_processor_entity") or "openai_llc"),
        "remote_request_id": request_id,
        "generated_at": generated_at,
        "created_at": generated_at,
        "cache_reused": False,
        "cache_source": "chatgpt_short",
        "message": "登录态短链已生成",
    }
    return {"ok": True, "data": data, "account_extra_patch": {}}


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
            password = _generate_chatgpt_registration_password()

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
            # email_api 与 HME Ready 都只是邮箱控制面：实际的 OTP 等待窗口
            # 必须服从 ChatGPT 状态机传入的 register/oauth timeout。否则旧的
            # mailbox_otp_timeout_seconds=20 会把首次/重发的 60/90s 等待静默
            # 压成 20s，日志显示的等待时长也会与实际不一致。
            provider_uses_state_machine_timeout = _mail_provider in {
                "email_api",
                "api_email",
                "email_otp_api",
                "mail_api_otp",
                "hme_ready_api",
                "icloud_hme_ready",
                "icloud_hme_helper_ready",
            } or str(extra_config.get("icloud_hme_mode") or "").strip().lower() == "helper_ready_api"
            if provider_uses_state_machine_timeout:
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
            _mail_provider = normalize_mailbox_provider(
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
                    # Never persist the registration/global config wholesale.
                    # It contains unrelated and potentially unbounded runtime
                    # state (GoPay batches, phone pools, pipelines, filters).
                    export_config = export_mailbox_state_config(_mail_provider, extra_config)
                return build_mailbox_state(
                    provider=_mail_provider,
                    email=str(_fixed_email or getattr(acct, "email", "") or "").strip(),
                    account_email=str(getattr(acct, "email", "") or "").strip(),
                    account_id=str(getattr(acct, "account_id", "") or "").strip(),
                    account_extra=account_extra,
                    before_ids=before_ids,
                    config=export_config,
                    proxy=proxy,
                )

            def _sync_hme_rerun_result(
                acct,
                *,
                success: bool,
                error_message: str = "",
                task_id: str = "",
                result_code: str = "",
                access_token_saved: bool = False,
            ):
                if _mail_provider != "hme_ready_api" or acct is None:
                    return
                account_extra = dict(getattr(acct, "extra", None) or {})
                anonymous_id = str(account_extra.get("anonymous_id") or "").strip()
                lease_id = str(account_extra.get("lease_id") or account_extra.get("checkout_id") or "").strip()
                source = str(account_extra.get("source") or "").strip().lower()
                raw_account_provider = str(account_extra.get("provider") or "").strip().lower()
                legacy_state = source in {"legacy-icloud-hme", "icloud-hme-legacy"} or (
                    raw_account_provider == "icloud_hme" and bool(anonymous_id) and not lease_id
                )
                # The old SQLite alias table has no representation of a Ready
                # lease.  Only historical anonymous aliases may be projected
                # there; never mistake a Helper lease/account_id for one.
                if not legacy_state or not anonymous_id:
                    return
                hme = str(account_extra.get("hme") or getattr(acct, "email", "") or _fixed_email or "").strip()
                if not hme:
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
                        access_token_saved=bool(access_token_saved),
                        result_code=str(result_code or ""),
                        mailbox_state=_export_mailbox_state_payload(acct, getattr(email_service, "_before_ids", set()) if "email_service" in locals() else set()),
                        delete_candidate=bool(not success and is_account_deactivated_message("", str(error_message or ""))),
                    )
                except Exception as exc:
                    log_fn(f"[HME Ready] 重跑进度写回失败: {exc}")

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
                    self._post_finalize_state = None
                    self._registration_failure_outcome = ""

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

                def mark_verification_message_processed(self, message_id):
                    normalized = str(message_id or "").strip()
                    if normalized:
                        self._before_ids.add(normalized)

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
                    # Helper finalize can return authoritative registration /
                    # lease state.  Capture it after the mutation so account
                    # persistence never stores the pre-commit snapshot.
                    self._post_finalize_state = self.export_state()
                    _sync_hme_rerun_result(
                        self._acct,
                        success=True,
                        task_id=resolved_task_id,
                        result_code=str(
                            getattr(self, "_registration_result_code", "") or "login_alive"
                        ),
                        access_token_saved=bool(
                            getattr(self, "_registration_access_token_saved", False)
                        ),
                    )

                def finalize_failure(self, error_message: str = "", task_id: str = ""):
                    if not self._acct:
                        return
                    finalize = getattr(self._mailbox, "finalize_failure", None)
                    resolved_task_id = str(task_id or _task_id or "").strip()
                    normalized_error = str(error_message or "").strip()
                    if callable(finalize):
                        finalize_outcome = finalize(
                            self._acct,
                            error_message=normalized_error,
                            task_id=resolved_task_id,
                        )
                        self._registration_failure_outcome = str(
                            finalize_outcome or ""
                        ).strip().lower()
                    self._post_finalize_state = self.export_state()
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

                def mark_verification_message_processed(self, message_id):
                    normalized = str(message_id or "").strip()
                    if normalized:
                        self._before_ids.add(normalized)

                def export_state(self):
                    return build_mailbox_state(
                        provider="tempmail_lol",
                        email=str(getattr(self._acct, "email", "") or "").strip(),
                        account_email=str(getattr(self._acct, "email", "") or "").strip(),
                        account_id=str(getattr(self._acct, "account_id", "") or "").strip(),
                        account_extra=getattr(self._acct, "extra", None) or {},
                        before_ids=self._before_ids,
                        config={},
                        proxy=proxy,
                    )

                def update_status(self, success, error=None):
                    pass

                @property
                def status(self):
                    return None

            email_service = TempMailEmailService()

        # Signup is intentionally a single-purpose task.  Older saved forms
        # may still contain ``refresh_token``/``has_refresh_token_solution``;
        # preserve those inputs for compatibility but normalize the actual
        # registration execution to AccessToken-only.
        requested_registration_mode = str(
            extra_config.get("chatgpt_registration_mode")
            or ("refresh_token" if extra_config.get("chatgpt_has_refresh_token_solution") else "")
        ).strip()
        if requested_registration_mode and requested_registration_mode != "access_token_only":
            extra_config["chatgpt_registration_requested_mode"] = requested_registration_mode
        extra_config["chatgpt_registration_mode"] = "access_token_only"
        extra_config["chatgpt_has_refresh_token_solution"] = False
        extra_config["chatgpt_access_token_only_checkout_amount_check_enabled"] = False

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
        if not result or not result.success:
            failure = RuntimeError(result.error_message if result else "注册失败")
            result_metadata = getattr(result, "metadata", None) if result else None
            if isinstance(result_metadata, dict):
                failure.registration_metadata = dict(result_metadata)
            raise failure

        return adapter.build_account(result, password)

    def get_platform_actions(self) -> list:
        return [
            {"id": "probe_local_status", "label": "探测本地状态", "params": []},
            {"id": "sync_cliproxyapi_status", "label": "同步 CLIProxyAPI 状态", "params": []},
            {"id": "sync_sub2api_status", "label": "同步 Sub2API 状态", "params": []},
            {"id": "sync_oaipay_status", "label": "同步 OAIPay 状态", "params": []},
            {"id": "refresh_token", "label": "刷新 Token", "params": []},
            {
                "id": "logout_web_session",
                "label": "退出 ChatGPT 网页会话",
                "params": [
                    {
                        "key": "confirm_logout",
                        "label": "我确认只退出当前账号保存的 ChatGPT 网页 Cookie 会话",
                        "type": "boolean",
                        "default": False,
                    }
                ],
            },
            {
                "id": "payment_link",
                "label": "支付链接生成",
                "params": [],
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
            {"id": "web_session_login", "label": "执行登录态", "params": []},
            {"id": "invalid_recheck", "label": "失效测活", "params": []},
            {"id": "zero_amount_eligibility", "label": "检测 0 元试用资格", "params": []},
            {"id": "payment_methods", "label": "检测支付方式", "params": []},
            {"id": "gcash_payment_method", "label": "检测 GCash 支付方式", "params": []},
            {"id": "checkout_link_type", "label": "检测支付链接格式", "params": []},
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

    @staticmethod
    def build_local_status_probe_action_result(probe_result: dict) -> dict:
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

    def probe_local_status_with_candidates(
        self,
        account: object,
        params: dict,
        *,
        manage_local_status_slots: bool = True,
        candidate_state: dict | None = None,
    ) -> dict:
        from services.chatgpt_core.local_status_proxy import run_local_status_probe_with_candidates
        from services.chatgpt_core.status_probe import probe_local_chatgpt_status

        def run_candidates() -> dict:
            return run_local_status_probe_with_candidates(
                account,
                params,
                probe_local_chatgpt_status,
                default_mode="global",
                candidate_state=candidate_state,
                config_values=self.config.extra if isinstance(self.config.extra, dict) else {},
            )

        if not manage_local_status_slots:
            return run_candidates()

        from services.chatgpt_core.local_status_refresh import (
            local_status_capacity_slot,
            local_status_identity_slot,
            refresh_local_status_concurrency_from_store,
        )

        refresh_local_status_concurrency_from_store()
        with local_status_identity_slot(account):
            with local_status_capacity_slot():
                return run_candidates()

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
            probe_result = self.probe_local_status_with_candidates(
                a,
                params,
            )
            return self.build_local_status_probe_action_result(probe_result)

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
                        "expires_at": result.expires_at.isoformat() if result.expires_at else "",
                        "expiry_source": result.expiry_source or "oauth_expires_in",
                    },
                }
            return {"ok": False, "error": result.error_message}

        if action_id == "logout_web_session":
            if params.get("confirm_logout") is not True:
                return {"ok": False, "error": "请确认退出当前账号的 ChatGPT 网页会话"}

            from datetime import datetime, timezone
            from services.chatgpt_core.account_fingerprint import resolve_account_browser_fingerprint
            from services.chatgpt_core.web_logout import logout_chatgpt_web_session

            fingerprint = resolve_account_browser_fingerprint(extra) or {}
            proxy = resolve_default_chatgpt_proxy(self.config.proxy if self.config else None)
            result = logout_chatgpt_web_session(
                cookies=str(extra.get("cookies") or extra.get("cookie_header") or ""),
                session_token=str(extra.get("session_token") or extra.get("sessionToken") or ""),
                proxy_url=proxy,
                user_agent=str(fingerprint.get("user_agent") or ""),
                accept_language=str(fingerprint.get("accept_language") or ""),
            )
            if not result.success:
                return {"ok": False, "error": result.error_message}
            return {
                "ok": True,
                "data": {
                    "message": "ChatGPT 网页会话已退出；本地 cookies/session 已清除，RT 与 AT 未改动",
                    "status_code": result.status_code,
                },
                "account_extra_remove": [
                    "cookies",
                    "cookie_header",
                    "cookie",
                    "cookie_jar",
                    "session_token",
                    "sessionToken",
                    "nextauth_session_token",
                ],
                "account_extra_patch": {
                    "chatgpt_web_logout": {
                        "at": datetime.now(timezone.utc).isoformat(),
                        "status_code": result.status_code,
                    }
                },
            }

        if action_id == "payment_link":
            import os
            import time
            import uuid

            from services.chatgpt_core.long_link_payment_client import (
                LongLinkPaymentClient,
                LongLinkPaymentError,
                payment_link_from_remote_job,
            )
            from services.chatgpt_core.payment_link_cache import (
                PAYMENT_LINK_FORMAT_LONG_LINK,
                PAYMENT_LINK_PLAN_TEAM,
                PAYMENT_SOURCE_LONG_LINK,
                normalize_payment_link_params,
                normalize_payment_link_plan,
                normalize_payment_link_url,
                normalize_team_billing_country,
                normalize_team_checkout_ui_mode,
                is_local_short_payment_link_params,
                payment_link_cache_matches,
                payment_link_cache_for_params,
                payment_link_variant_key,
                validate_payment_link_request_params,
            )

            try:
                validate_payment_link_request_params(params)
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            plan = normalize_payment_link_plan(params.get("plan"))
            is_team = plan == PAYMENT_LINK_PLAN_TEAM
            if is_local_short_payment_link_params(params):
                if is_team:
                    return {"ok": False, "error": "登录态短链仅支持 Plus 支付计划"}
                return _execute_chatgpt_short_payment_link(
                    a,
                    params,
                    default_proxy=self.config.proxy if self.config else None,
                )
            profile_overrides = {**params, "plan": plan}
            if is_team:
                for inherited_key in ("billingCountry", "country", "currency"):
                    profile_overrides.pop(inherited_key, None)
                profile_overrides["billing_country"] = normalize_team_billing_country(params)
                profile_overrides["checkout_ui_mode"] = normalize_team_checkout_ui_mode(params)
            client = LongLinkPaymentClient.from_env()
            payment_profile = (
                client.get_profile(overrides=profile_overrides)
                if is_team
                else client.get_profile()
            )
            payment_profile_hash = str(payment_profile.get("profile_hash") or "").strip()
            expected_profile_hash = str(params.get("payment_profile_hash") or params.get("profile_hash") or "").strip()
            if expected_profile_hash and expected_profile_hash != payment_profile_hash:
                return {"ok": False, "error": "支付链接管理端配置已变化，请重新发起任务"}
            country = str(payment_profile.get("country") or "").strip().upper()
            currency = str(payment_profile.get("currency") or "").strip().upper()
            reuse_cached_link = params.get("reuse_cached_link") is not False
            profile_detail = payment_profile.get("profile") if isinstance(payment_profile.get("profile"), dict) else {}
            profile_regions = payment_profile.get("regions") if isinstance(payment_profile.get("regions"), dict) else profile_detail.get("regions")
            profile_regions = profile_regions if isinstance(profile_regions, dict) else {}
            team_profile = payment_profile.get("team") if isinstance(payment_profile.get("team"), dict) else profile_detail.get("team")
            team_profile = team_profile if isinstance(team_profile, dict) else {}
            cache_request = {
                **params,
                "plan": plan,
                "country": country,
                "currency": currency,
                "payment_link_format": PAYMENT_LINK_FORMAT_LONG_LINK,
                "payment_source": PAYMENT_SOURCE_LONG_LINK,
                "profile_hash": payment_profile_hash,
            }
            if is_team:
                cache_request.update(
                    {
                        "team_plan_data": {
                            "workspace_name": str(team_profile.get("workspace_name") or "").strip(),
                            "price_interval": str(team_profile.get("price_interval") or "month").strip().lower(),
                            "seat_quantity": team_profile.get("seat_quantity") or 2,
                        },
                        "cancel_url": str(team_profile.get("cancel_url") or "").strip(),
                        "promo_code_digest": str(
                            payment_profile.get("promo_code_digest")
                            or profile_detail.get("promo_code_digest")
                            or ""
                        ).strip(),
                        "plan_name": "chatgptteamplan",
                        "billing_country": country,
                        "checkout_proxy_region": str(
                            profile_regions.get("checkout")
                            or params.get("checkout_proxy_region")
                            or ""
                        ).strip().upper(),
                        "checkout_ui_mode": str(
                            payment_profile.get("checkout_ui_mode")
                            or profile_detail.get("checkout_ui_mode")
                            or profile_overrides.get("checkout_ui_mode")
                            or "hosted"
                        ).strip().lower(),
                    }
                )
            normalized_cache_params = normalize_payment_link_params(cache_request)
            normalized_cache_params["variant_key"] = payment_link_variant_key(cache_request)
            cached_link = payment_link_cache_for_params(extra, normalized_cache_params)
            cached_format = str(cached_link.get("payment_link_format") or "long_hosted")
            cached_url = normalize_payment_link_url(cached_link.get("url"), cached_format)
            should_reuse_cached_link = (
                reuse_cached_link
                and bool(cached_url)
                and payment_link_cache_matches(cached_link, normalized_cache_params)
            )
            if should_reuse_cached_link:
                cached_data = dict(cached_link)
                cached_data.update(
                    {
                        "url": cached_url,
                        "plan": plan,
                        "country": country,
                        "currency": currency,
                        "proxy": "",
                        "payment_link_format": PAYMENT_LINK_FORMAT_LONG_LINK,
                        "payment_source": PAYMENT_SOURCE_LONG_LINK,
                        "profile_hash": payment_profile_hash,
                        "cache_reused": True,
                        "cache_source": str(cached_link.get("source") or "cached_payment_link"),
                        "message": "已复用缓存支付链接",
                    }
                )
                return {
                    "ok": True,
                    "data": cached_data,
                    "account_extra_patch": {},
                }
            request_id = str(params.get("request_id") or "").strip() or f"direct:{uuid.uuid4().hex}"
            access_token = str(a.access_token or "").strip()
            if not access_token:
                return {"ok": False, "error": "账号缺少 Access Token"}
            submitted = (
                client.submit_batch(
                    items=[{"access_token": access_token, "request_id": request_id}],
                    expected_profile_hash=payment_profile_hash,
                    profile_overrides=profile_overrides,
                )
                if is_team
                else client.submit_batch(
                    items=[{"access_token": access_token, "request_id": request_id}],
                    expected_profile_hash=payment_profile_hash,
                )
            )
            remote_items = submitted.get("items") if isinstance(submitted.get("items"), list) else []
            remote = next(
                (
                    item
                    for item in remote_items
                    if isinstance(item, dict) and str(item.get("request_id") or "").strip() == request_id
                ),
                {},
            )
            if not remote:
                return {"ok": False, "error": "支付链接生成服务未返回当前账号任务"}
            deadline = time.monotonic() + max(float(os.getenv("OPENAI_PAY_LONG_LINK_JOB_TIMEOUT_SECONDS") or 1800), 5.0)
            while str(remote.get("status") or "").strip().lower() in {"queued", "running"}:
                batch_id = str(remote.get("batch_id") or submitted.get("batch_id") or "").strip()
                if not batch_id:
                    return {"ok": False, "error": "支付链接生成服务未返回远端批次 ID"}
                if time.monotonic() >= deadline:
                    return {"ok": False, "error": "支付链接远端任务轮询超时，结果状态未知"}
                time.sleep(max(float(os.getenv("OPENAI_PAY_LONG_LINK_POLL_INTERVAL_SECONDS") or 2.0), 0.2))
                batch = client.get_batch(batch_id)
                remote = next(
                    (
                        item
                        for item in (batch.get("items") if isinstance(batch.get("items"), list) else [])
                        if isinstance(item, dict) and str(item.get("request_id") or "").strip() == request_id
                    ),
                    {},
                )
                if not remote:
                    return {"ok": False, "error": "支付链接远端批次未返回当前账号任务"}
            remote_status = str(remote.get("status") or "").strip().lower()
            if remote_status != "done":
                detail = str(remote.get("error") or "支付链接生成失败").strip()
                if remote_status == "interrupted":
                    detail = f"远端任务中断: {detail}"
                return {"ok": False, "error": detail}
            try:
                data = payment_link_from_remote_job(remote, profile=payment_profile)
            except LongLinkPaymentError as exc:
                return {"ok": False, "error": str(exc)}
            data.update(
                {
                    "plan": plan,
                    "country": str(data.get("country") or country).strip().upper(),
                    "currency": str(data.get("currency") or currency).strip().upper(),
                    "cache_reused": False,
                    "cache_source": PAYMENT_SOURCE_LONG_LINK,
                }
            )
            return {
                "ok": bool(data.get("url")),
                "data": data,
                "account_extra_patch": {},
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

        if action_id == "web_session_login":
            return {
                "ok": True,
                "data": {
                    "message": "已提交执行登录态请求",
                    "activation_kind": "web_session_login",
                },
            }

        if action_id == "zero_amount_eligibility":
            return {
                "ok": True,
                "data": {
                    "message": "已提交 0 元试用资格检测请求",
                    "activation_kind": "zero_amount_eligibility",
                },
            }

        if action_id == "gcash_payment_method":
            return {
                "ok": True,
                "data": {
                    "message": "已提交 GCash 支付方式检测请求",
                    "activation_kind": "gcash_payment_method",
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
