from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import time
from typing import Any, Callable, Iterable, Optional
from urllib.parse import urlsplit

from core.base_platform import Account, AccountStatus
from core.task_runtime import TaskInterruption
from services.chatgpt_core.phone_service import (
    UploadedPhoneEntry,
    UploadedPhoneService,
    create_phone_service,
    parse_uploaded_phone_lines,
)
from services.chatgpt_core.task_logging import mask_phone_for_log, redact_log_text, redact_proxy_url, redact_raw_phone_line, sanitize_phone_result
from services.chatgpt_core.phone_signup_client import (
    AUTH_BASE,
    PhoneRegistrationRouteError,
    PhoneSignupClient,
    mask_phone,
    normalize_phone,
    short_url,
)
from services.chatgpt_core.utils import generate_random_birthday, generate_random_name, generate_random_password


@dataclass
class PhoneSignupResult:
    success: bool
    phone: str = ""
    password: str = ""
    flow: str = "phone_signup"
    account_id: str = ""
    user_id: str = ""
    access_token: str = ""
    session_token: str = ""
    cookies: str = ""
    error_message: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


def _truthy(value: Any, default: bool = False) -> bool:
    if value in (None, ""):
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "y", "是", "启用", "开启"}:
        return True
    if text in {"0", "false", "no", "off", "n", "否", "禁用", "关闭"}:
        return False
    return default


def _positive_int(value: Any, default: int, minimum: int = 1, maximum: int = 3600) -> int:
    try:
        parsed = int(float(str(value or "").strip()))
    except Exception:
        parsed = default
    return max(minimum, min(maximum, parsed))


def _utcnow_text() -> str:
    return datetime.now(timezone.utc).isoformat()


def _phone_prefix4(phone: str) -> str:
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    return digits[:4] if len(digits) >= 4 else ""


def _coerce_prefix_list(value: Any) -> list[str]:
    raw_items: list[Any]
    if isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        text = str(value or "").strip()
        if not text:
            raw_items = []
        else:
            try:
                parsed = json.loads(text)
            except Exception:
                parsed = None
            if isinstance(parsed, list):
                raw_items = parsed
            else:
                raw_items = text.replace("\n", ",").replace(" ", ",").split(",")

    prefixes: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        prefix = "".join(ch for ch in str(item or "") if ch.isdigit())[:4]
        if len(prefix) != 4 or prefix in seen:
            continue
        seen.add(prefix)
        prefixes.append(prefix)
    return prefixes


def _prefix_sample_filter(value: Any) -> str:
    text = str(value or "all").strip().lower()
    if text in {"available", "available_only", "healthy", "healthy_only"}:
        return "available"
    if text in {"rejected", "rejected_only", "unavailable", "unavailable_only"}:
        return "rejected"
    return "all"


def _extract_continue_url(data: dict[str, Any] | None) -> str:
    if not isinstance(data, dict):
        return ""
    return str(
        data.get("continue_url")
        or ((data.get("page") or {}).get("payload") or {}).get("url")
        or ""
    ).strip()


def _status_label(status: str) -> str:
    return {
        "registered_phone_signup": "手机号注册成功",
        "openai_rejected": "OpenAI 拒绝号码",
        "already_registered": "号码已注册",
        "api_no_code": "OpenAI 已发码但 API 未收到",
        "api_error": "收码 API 异常",
        "invalid_otp": "短信验证码错误",
        "browser_error": "浏览器/网络异常",
        "session_failed": "注册完成但 Session 获取失败",
        "unknown": "未知",
    }.get(str(status or ""), str(status or "unknown"))


def _error_status(error_text: str) -> str:
    lowered = str(error_text or "").strip().lower()
    if not lowered:
        return "unknown"
    if any(
        marker in lowered
        for marker in (
            "落到登录密码页",
            "已注册手机号",
            "already registered",
            "already in use",
            "phone_number_in_use",
            "phone number already in use",
            "log-in/password",
        )
    ):
        return "already_registered"
    if any(
        marker in lowered
        for marker in (
            "phone number is invalid",
            "invalid phone number",
            "invalid phone",
            "unable to send sms",
            "not a valid mobile number",
            "unsupported phone number",
            "phone number not supported",
            "carrier not supported",
            "detected suspicious behavior from phone numbers",
            "suspicious behavior from phone numbers",
            "电话号码无效",
            "手机号无效",
            "发送短信验证失败",
            "号码无效",
            "号码不支持",
            "手机号不支持",
        )
    ):
        return "openai_rejected"
    if any(marker in lowered for marker in ("invalid otp", "验证码错误", "otp 验证失败", "phone-otp/validate 失败")):
        return "invalid_otp"
    if any(marker in lowered for marker in ("未收到短信验证码", "no verification code", "达到同号重发上限")):
        return "api_no_code"
    if any(marker in lowered for marker in ("收码 api", "api 请求失败", "api 响应不是 json", "api 返回失败")):
        return "api_error"
    if any(marker in lowered for marker in ("/api/auth/session", "未返回 accesstoken", "session")):
        return "session_failed"
    if any(marker in lowered for marker in ("timeout", "timed out", "network", "connection", "proxy", "ssl", "tls", "csrf", "authorize")):
        return "browser_error"
    return "unknown"


class PhoneRegistrationEngine:
    """ChatGPT phone-number signup integrated with this project's phone services."""

    def __init__(
        self,
        *,
        extra_config: Optional[dict[str, Any]] = None,
        proxy_url: str | None = None,
        browser_mode: str = "protocol",
        callback_logger: Optional[Callable[..., None]] = None,
        stop_checker: Optional[Callable[[], None]] = None,
        phone_service: Any = None,
        client_factory: Optional[Callable[..., PhoneSignupClient]] = None,
    ) -> None:
        self.extra_config = dict(extra_config or {})
        self.proxy_url = str(proxy_url or "").strip()
        self.browser_mode = str(browser_mode or "protocol").strip() or "protocol"
        self.callback_logger = callback_logger or (lambda msg, *_args: None)
        self.stop_checker = stop_checker or self.extra_config.get("_task_stop_checker")
        self.phone_service = phone_service
        self.client_factory = client_factory or PhoneSignupClient
        self.logs: list[str] = []
        self.email: str = ""
        self.password: str = ""
        self._uploaded_entries: list[UploadedPhoneEntry] = []
        self._uploaded_cursor = 0
        self._pool_managed_by_phone: dict[str, dict[str, Any]] = {}
        self.panel_results: list[dict[str, Any]] = []

    def _log(self, message: str, level: str = "info") -> None:
        text = str(message or "")
        normalized_level = str(level or "info").strip().lower() or "info"
        if normalized_level == "debug" and not text.lstrip().upper().startswith("[DEBUG]"):
            text = f"[DEBUG] {text}"
        text = redact_log_text(text)
        self.logs.append(text)
        try:
            self.callback_logger(text, normalized_level)
        except TypeError:
            self.callback_logger(text)

    def _check_stop(self) -> None:
        if callable(self.stop_checker):
            self.stop_checker()

    def _service_config(self) -> dict[str, Any]:
        cfg = dict(self.extra_config or {})
        cfg.setdefault("_task_stop_checker", self.stop_checker)
        timeout = self.extra_config.get("chatgpt_phone_signup_timeout_seconds") or self.extra_config.get("uploaded_phone_timeout_seconds") or 180
        poll_interval = self.extra_config.get("chatgpt_phone_signup_poll_interval_seconds") or self.extra_config.get("uploaded_phone_poll_interval_seconds") or 5
        max_resends = self.extra_config.get("chatgpt_phone_signup_max_resend_attempts")
        if max_resends in (None, ""):
            max_resends = self.extra_config.get("uploaded_phone_max_resend_attempts") or 1
        max_resends = _positive_int(max_resends, 1, minimum=1, maximum=5)
        resend_interval = self.extra_config.get("chatgpt_phone_signup_resend_interval_seconds") or self.extra_config.get("uploaded_phone_resend_interval_seconds") or 60
        resend_interval = _positive_int(resend_interval, 60, minimum=10, maximum=600)
        cfg["uploaded_phone_timeout_seconds"] = timeout
        cfg["uploaded_phone_poll_interval_seconds"] = poll_interval
        cfg["uploaded_phone_max_resend_attempts"] = max_resends
        cfg["uploaded_phone_resend_interval_seconds"] = resend_interval
        return cfg

    def _wait_for_phone_code(self, service: Any, entry: Any, *, timeout: int | None = None) -> str | None:
        if timeout is None:
            return service.wait_for_code(entry)
        try:
            return service.wait_for_code(entry, timeout=timeout)
        except TypeError:
            return service.wait_for_code(entry)

    def _timeline(self, message: str, level: str = "info") -> None:
        self._log(message, level)

    def _load_uploaded_entries_from_pool(self, limit: int = 0) -> list[UploadedPhoneEntry]:
        from services.chatgpt_core.phone_pool_repository import PhonePoolRepository

        repo = PhonePoolRepository()
        prefix_bind_enabled = _truthy(
            self.extra_config.get("chatgpt_phone_signup_prefix_bind_enabled")
            if self.extra_config.get("chatgpt_phone_signup_prefix_bind_enabled") not in (None, "")
            else self.extra_config.get("prefix_bind_enabled"),
            default=False,
        )
        prefix_sample_enabled = _truthy(
            self.extra_config.get("chatgpt_phone_signup_prefix_sample_enabled")
            if self.extra_config.get("chatgpt_phone_signup_prefix_sample_enabled") not in (None, "")
            else self.extra_config.get("prefix_sample_enabled"),
            default=False,
        ) and not prefix_bind_enabled
        selected_prefixes = _coerce_prefix_list(
            self.extra_config.get("chatgpt_phone_signup_selected_prefixes")
            or self.extra_config.get("selected_prefixes")
        )
        prefix_sample_size = 2 if _positive_int(
            self.extra_config.get("chatgpt_phone_signup_prefix_sample_size")
            or self.extra_config.get("prefix_sample_size"),
            1,
            minimum=1,
            maximum=2,
        ) == 2 else 1
        prefix_filter = _prefix_sample_filter(
            self.extra_config.get("chatgpt_phone_signup_prefix_sample_filter")
            or self.extra_config.get("prefix_sample_filter")
        )

        if prefix_bind_enabled:
            if not selected_prefixes:
                raise RuntimeError("限定号段注册需要至少选择一个号段")
            records = repo.list_available_by_prefixes(selected_prefixes)
            self._log(f"[手机号注册] 使用限定号段手机号池: {','.join(selected_prefixes)}，候选 {len(records)} 个")
        elif prefix_sample_enabled:
            if selected_prefixes:
                records = repo.sample_selected_prefixes(selected_prefixes, prefix_sample_size)
                mode_text = f"指定号段 {','.join(selected_prefixes)}"
            elif prefix_filter == "rejected":
                records = repo.sample_rejected_by_prefix(prefix_sample_size)
                mode_text = "仅 OpenAI 拒绝号段"
            elif prefix_filter == "available":
                records = repo.sample_available_by_prefix(prefix_sample_size)
                mode_text = "仅可用号段"
            else:
                records = repo.sample_testable_by_prefix(prefix_sample_size)
                mode_text = "全部号段"
            if records:
                records = repo.restore_prefix_sample_records(
                    [int(getattr(record, "id", 0) or 0) for record in records]
                )
            self._log(f"[手机号注册] 使用号段抽样手机号池: {mode_text}，每段 {prefix_sample_size} 个，候选 {len(records)} 个")
        else:
            records = repo.list_available()
        item_limit = 0 if prefix_sample_enabled else (limit or 0)
        items = repo.to_phone_items(records, limit_accounts=item_limit)
        entries: list[UploadedPhoneEntry] = []
        self._pool_managed_by_phone = {}
        for item in items:
            phone = str(item.get("phone") or "").strip()
            api_url = str(item.get("api_url") or "").strip()
            if not phone or not api_url:
                continue
            entry = UploadedPhoneEntry(
                country_slug="phone_pool",
                phone=phone,
                detail_url=api_url,
                api_url=api_url,
                raw_line=str(item.get("raw_line") or f"{phone}----{api_url}"),
                line_no=int(item.get("line_no") or len(entries) + 1),
            )
            entries.append(entry)
            self._pool_managed_by_phone[phone] = dict(item)
        return entries

    def _build_phone_service(self):
        if self.phone_service is not None:
            return self.phone_service

        cfg = self._service_config()
        phone_lines = str(
            self.extra_config.get("chatgpt_phone_signup_phone_lines")
            or self.extra_config.get("phone_signup_phone_lines")
            or ""
        ).strip()
        use_pool = _truthy(
            self.extra_config.get("chatgpt_phone_signup_use_pool")
            or self.extra_config.get("phone_signup_use_pool"),
            default=False,
        )
        if phone_lines:
            entries, errors = parse_uploaded_phone_lines(phone_lines)
            if errors:
                self._log(f"[手机号注册] 收码行解析跳过 {len(errors)} 条无效记录")
            if not entries:
                raise RuntimeError("手机号注册需要至少一条有效的 手机号----收码API")
            self._uploaded_entries = entries
            service = UploadedPhoneService(entries, cfg, log_fn=self._log)
            return service
        if use_pool:
            target_count = _positive_int(self.extra_config.get("_target_success_count") or self.extra_config.get("count"), 1, minimum=1, maximum=1000)
            entries = self._load_uploaded_entries_from_pool(limit=target_count)
            if not entries:
                raise RuntimeError("手机号池没有可用号码，请导入/启用可用手机号，或粘贴 手机号----收码API")
            self._uploaded_entries = entries
            return UploadedPhoneService(entries, cfg, log_fn=self._log)
        return create_phone_service(cfg, log_fn=self._log)

    def _next_uploaded_entry(self, service) -> UploadedPhoneEntry | None:
        if not isinstance(service, UploadedPhoneService):
            return None
        if self._uploaded_cursor >= len(self._uploaded_entries):
            return None
        entry = self._uploaded_entries[self._uploaded_cursor]
        self._uploaded_cursor += 1
        service.bind_entry(entry)
        return entry

    def _acquire_entry(self, service, excluded_prefixes: Iterable[str]):
        self._check_stop()
        bound_entry = self._next_uploaded_entry(service)
        if bound_entry is not None:
            return service.acquire_phone(exclude_prefixes=excluded_prefixes)
        return service.acquire_phone(
            exclude_prefixes=excluded_prefixes,
            email="",
            account_id=0,
            task_id=str(self.extra_config.get("_current_task_id") or self.extra_config.get("task_id") or ""),
            purpose="chatgpt_phone_signup",
        )

    def _record_pool_status(self, phone: str, status: str, reason: str = "", email: str = "") -> None:
        if not phone:
            return
        try:
            from services.chatgpt_core.phone_pool_repository import PhonePoolRepository

            prefix_state = PhonePoolRepository().record_phone_signup_prefix_status(
                phone,
                status,
                reason=reason,
                email=email,
            )
            if prefix_state:
                label = "可注册" if str(prefix_state.get("status") or "") == "available" else "不可注册"
                self._log(
                    f"[手机号注册] 号段状态更新: {prefix_state.get('prefix') or '-'} -> {label}；仅更新注册号段状态，不改手机号自身状态"
                )
        except Exception as exc:
            self._log(f"[手机号注册] 号段状态回写失败: {phone} - {exc}", "debug")

    def _panel_result(
        self,
        entry,
        status: str,
        *,
        email: str = "",
        reason: str = "",
        code_received: bool = False,
        service: Any = None,
    ) -> dict[str, Any]:
        phone = str(getattr(entry, "phone", "") or "").strip()
        api_url = str(getattr(entry, "api_url", "") or getattr(entry, "detail_url", "") or "").strip()
        return {
            "line_no": int(getattr(entry, "line_no", 0) or 0),
            "phone": phone,
            "prefix4": _phone_prefix4(phone),
            "api_url": api_url,
            "raw_line": str(getattr(entry, "raw_line", "") or (f"{phone}----{api_url}" if api_url else phone)).strip(),
            "status": status,
            "status_label": _status_label(status),
            "account_id": 0,
            "email": email,
            "reason": str(reason or ""),
            "code_received": bool(code_received),
            "api_expired_date": str(getattr(service, "last_expired_date", "") or ""),
            "code_time": str(getattr(service, "last_code_time", "") or ""),
            "code_extracted": bool(getattr(service, "last_code_was_extracted", False)),
            "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    def run(self) -> PhoneSignupResult:
        result = PhoneSignupResult(success=False)
        service = self._build_phone_service()
        if not getattr(service, "enabled", False):
            result.error_message = "未配置可用的接码服务或手机号注册号码"
            return result

        max_attempts = int(getattr(service, "max_attempts", 1) or 1)
        if isinstance(service, UploadedPhoneService):
            max_attempts = len(self._uploaded_entries)
        configured_attempts = self.extra_config.get("chatgpt_phone_signup_max_attempts") or self.extra_config.get("register_max_attempts")
        if configured_attempts not in (None, ""):
            max_attempts = min(max_attempts if max_attempts > 0 else 1, _positive_int(configured_attempts, max_attempts or 1, minimum=1, maximum=1000))
        max_attempts = max(max_attempts, 1)

        excluded_prefixes: set[str] = set()
        last_error = ""
        configured_password = str(
            self.password
            or self.extra_config.get("password")
            or self.extra_config.get("chatgpt_phone_signup_password")
            or self.extra_config.get("login_password")
            or ""
        ).strip()
        password = configured_password or generate_random_password(16)
        self.password = password
        first, last = generate_random_name()
        full_name = f"{first} {last}"
        birthdate = generate_random_birthday()

        for attempt in range(max_attempts):
            self._check_stop()
            entry = None
            phone = ""
            try:
                entry = self._acquire_entry(service, excluded_prefixes)
                if not entry:
                    last_error = "接码服务中无可用手机号"
                    break
                phone = normalize_phone(str(getattr(entry, "phone", "") or ""))
                self._timeline(f"[手机号注册][尝试 {attempt + 1}/{max_attempts}][{mask_phone_for_log(phone)}] 开始：取号成功，准备进入 ChatGPT")
                client = self.client_factory(
                    proxy=self.proxy_url,
                    browser_mode=self.browser_mode,
                    log_fn=lambda msg, level="debug", *_: self._log(f"[手机号注册链路] {msg}", level),
                    stop_checker=self.stop_checker,
                    fingerprint=(
                        self.extra_config.get("chatgpt_browser_fingerprint")
                        or self.extra_config.get("browser_fingerprint")
                    ),
                )
                auth_route = client.warm_chatgpt_and_signin(phone)
                route_path = urlsplit(str(getattr(auth_route, "final_url", "") or "")).path
                flow = "phone_signup"

                if "/log-in/password" in route_path:
                    flow = "phone_existing_login"
                    if not configured_password:
                        raise RuntimeError("已注册手机号登录需要密码；请填写手机号注册/登录密码")
                    self._timeline(f"[手机号注册] 阶段 1/4：检测到已注册手机号，切换到手机号登录续跑")
                    verify_data = client.verify_login_password_for_existing_phone(password)
                    page_type = ((verify_data.get("page") or {}).get("type") or "").strip()
                    continue_url = _extract_continue_url(verify_data)
                    client.open_contact_verification_page(continue_url, referer=f"{AUTH_BASE}/log-in/password")
                    sms_sent = page_type == "contact_verification" or "/contact-verification" in urlsplit(continue_url).path
                    if not sms_sent:
                        raise RuntimeError(f"已注册手机号登录没有进入短信验证页: page={page_type or '-'} continue={short_url(continue_url)}")
                    service.mark_sms_sent(entry)
                    self._timeline(f"[手机号注册] 阶段 2/4：OpenAI 已发送短信验证码")
                else:
                    client.ensure_registration_route(auth_route, phone=phone)
                    register_data = client.register_phone_password(phone, password)
                    page_type = ((register_data.get("page") or {}).get("type") or "").strip()
                    continue_url = str(register_data.get("continue_url") or "")
                    client.maybe_send_phone_otp(continue_url, explicit_send=True)
                    sms_sent = page_type == "phone_otp_send" or bool(continue_url)
                    if sms_sent:
                        service.mark_sms_sent(entry)
                        self._timeline(f"[手机号注册] 阶段 2/4：OpenAI 已接受手机号并发码")
                    else:
                        self._log(f"[手机号注册] user/register 未明确返回发码状态，仍进入收码等待: page={page_type or '-'}")
                        service.mark_sms_sent(entry)

                self._timeline("[手机号注册] 阶段 3/4：等待短信验证码")
                code = self._wait_for_phone_code(service, entry)
                max_resends = int(getattr(service, "max_resend_attempts", 0) or 0)
                resend_wait_seconds = max(int(getattr(service, "resend_interval_seconds", 0) or 0), 0)
                for resend_attempt in range(1, max_resends + 1):
                    if code:
                        break
                    self._log(
                        f"[手机号注册] 长时间未收到短信，重发验证码 {resend_attempt}/{max_resends}: "
                        f"{mask_phone_for_log(phone)}；重发后等待 {resend_wait_seconds or '-'}s"
                    )
                    if not client.resend_phone_otp():
                        break
                    try:
                        service.request_next_code(entry)
                    except Exception as exc:
                        self._log(f"[手机号注册] 接码服务请求下一条短信失败，将继续等待当前通道: {redact_log_text(exc)}")
                    service.mark_sms_sent(entry)
                    code = self._wait_for_phone_code(service, entry, timeout=resend_wait_seconds or None)

                if not code:
                    raise RuntimeError(f"手机号 {phone} 未收到短信验证码")

                self._timeline("[手机号注册] 阶段 3/4：验证码已收到并提交")
                validate_data = client.validate_phone_otp(code)
                validate_page_type = ((validate_data.get("page") or {}).get("type") or "").strip()
                callback_url = _extract_continue_url(validate_data)
                if flow == "phone_existing_login" and (validate_page_type == "about_you" or "/about-you" in urlsplit(callback_url).path):
                    self._log("[手机号注册] 已注册手机号处于待完善资料状态，继续提交姓名生日")
                    create_data = client.create_account(full_name=full_name, birthdate=birthdate)
                    callback_url = _extract_continue_url(create_data)
                elif flow == "phone_signup":
                    create_data = client.create_account(full_name=full_name, birthdate=birthdate)
                    callback_url = _extract_continue_url(create_data)
                if not callback_url or "chatgpt.com" not in urlsplit(callback_url).netloc:
                    raise RuntimeError(f"短信验证后没有返回 ChatGPT callback: page={validate_page_type or '-'} continue={short_url(callback_url)}")
                session_info = client.follow_chatgpt_callback_and_capture(callback_url, phone)
                access_token = str(session_info.get("access_token") or "").strip()
                if not access_token:
                    raise RuntimeError("手机号注册完成但未获取 accessToken")
                service.complete(entry)
                account_email = f"phone:{phone}"
                panel_result = self._panel_result(
                    entry,
                    "registered_phone_signup",
                    email=account_email,
                    reason=("已注册手机号登录完成，已保存 AccessToken" if flow == "phone_existing_login" else "手机号注册完成，已保存注册阶段 AccessToken"),
                    code_received=True,
                    service=service,
                )
                panel_result["flow"] = flow
                self.panel_results.append(dict(panel_result))
                self._record_pool_status(phone, "registered_phone_signup", reason=panel_result["reason"], email=account_email)
                result.success = True
                result.phone = phone
                result.password = password
                result.flow = flow
                result.account_id = str(session_info.get("account_id") or session_info.get("user_id") or "").strip()
                result.user_id = str(session_info.get("user_id") or result.account_id or "").strip()
                result.access_token = access_token
                result.session_token = str(session_info.get("session_token") or "").strip()
                result.cookies = str(session_info.get("cookies") or "").strip()
                result.metadata = {
                    "phone_signup": {
                        "flow": flow,
                        "phone": phone,
                        "masked_phone": mask_phone(phone),
                        "name": full_name,
                        "birthdate": birthdate,
                        "registered_at": _utcnow_text(),
                        "account_id": result.account_id,
                        "user_id": result.user_id,
                        "session_phone_number": str(session_info.get("phone_number") or ""),
                        "me_phone_number_missing": bool(session_info.get("me_phone_number_missing")),
                        "session_email": session_info.get("email"),
                        "country": str(session_info.get("country") or ""),
                    },
                    "phone_signup_result": panel_result,
                    "phone_signup_results": list(self.panel_results),
                    "phone_signup_raw_line": str(panel_result.get("raw_line") or ""),
                    "phone_signup_raw_line_redacted": redact_raw_phone_line(panel_result.get("raw_line") or ""),
                    "phone_signup_source": str(getattr(entry, "country_slug", "") or ""),
                    "browser_mode": self.browser_mode,
                    "proxy_used": self.proxy_url,
                    "proxy_used_redacted": redact_proxy_url(self.proxy_url),
                }
                self._timeline(
                    f"[手机号注册] 结果：成功，phone:{mask_phone_for_log(phone)}，auth=access_token_only"
                )
                return result
            except TaskInterruption:
                raise
            except Exception as exc:
                last_error = str(exc or "手机号注册失败")
                status = _error_status(last_error)
                self._timeline(f"[手机号注册] 结果：失败，{redact_log_text(last_error)}")
                if entry is not None:
                    if status in {"openai_rejected", "already_registered"}:
                        try:
                            service.mark_blacklisted(str(getattr(entry, "phone", "") or phone), reason=last_error)
                        except Exception:
                            pass
                    else:
                        try:
                            service.cancel(entry, reason=last_error)
                        except Exception:
                            pass
                    if phone:
                        failure_result = self._panel_result(
                            entry,
                            status,
                            email="",
                            reason=last_error,
                            code_received=bool(getattr(service, "last_code_was_extracted", False)),
                            service=service,
                        )
                        self.panel_results.append(failure_result)
                        self._record_pool_status(phone, status, reason=last_error)
                        if status == "browser_error" and not bool(getattr(service, "last_sms_sent", False)):
                            self._log(
                                "[手机号注册] 发码前浏览器/代理失败，停止消耗当前手机号池，交给外层切换代理重试",
                                "debug",
                            )
                            raise
                        excluded_prefixes.add(_phone_prefix4(phone))
                continue

        result.error_message = last_error or "手机号注册失败"
        result.metadata = {"phone_signup_results": list(self.panel_results)}
        return result

    def build_account(self, result: PhoneSignupResult) -> Account:
        if not result or not result.success:
            raise RuntimeError(result.error_message if result else "手机号注册失败")
        phone = normalize_phone(result.phone)
        account_email = f"phone:{phone}"
        phone_signup = dict((result.metadata or {}).get("phone_signup") or {})
        panel_result = dict((result.metadata or {}).get("phone_signup_result") or {})
        panel_results = [item for item in (result.metadata or {}).get("phone_signup_results") or [] if isinstance(item, dict)]
        if panel_result and not panel_results:
            panel_results = [panel_result]
        flow = str(result.flow or phone_signup.get("flow") or panel_result.get("flow") or "phone_signup").strip() or "phone_signup"
        now = _utcnow_text()
        phone_binding = {
            "phone": phone,
            "api_url": str(panel_result.get("api_url") or ""),
            "raw_line": str(panel_result.get("raw_line") or ""),
            "account_id": 0,
            "email": account_email,
            "task_id": str(self.extra_config.get("_current_task_id") or self.extra_config.get("task_id") or ""),
            "source": "phone_signup",
            "flow": flow,
            "status": "bound",
            "status_label": "已注册手机号登录成功" if flow == "phone_existing_login" else "手机号注册成功",
            "api_expired_date": str(panel_result.get("api_expired_date") or ""),
            "code_time": str(panel_result.get("code_time") or ""),
            "code_extracted": bool(panel_result.get("code_extracted")),
            "bound_at": str(panel_result.get("finished_at") or now),
        }
        extra = {
            "access_token": result.access_token,
            "refresh_token": "",
            "session_token": result.session_token,
            "cookies": result.cookies,
            "workspace_id": result.account_id,
            "auth_level": "access_token_only",
            "partial_auth": True,
            "chatgpt_registration_entry": "phone_signup",
            "chatgpt_identifier_type": "phone",
            "chatgpt_registration_mode": "access_token_only",
            "chatgpt_has_refresh_token_solution": False,
            "chatgpt_phone_auth_flow": flow,
            "chatgpt_token_source": "phone_existing_login" if flow == "phone_existing_login" else "phone_signup_registration",
            "chatgpt_workspace_scope": "free",
            "chatgpt_workspace_label": flow,
            "chatgpt_workspace_display_name": f"{account_email} [{flow}]",
            "chatgpt_workspace_variant_key": f"{flow}:{result.account_id or phone}",
            "chatgpt_phone_number": phone,
            "chatgpt_bound_phone_number": phone,
            "chatgpt_bound_phone": {
                "phone": phone,
                "phone_number": phone,
                "masked": "",
                "masked_phone": "",
                "source": "phone_signup",
                "detected_at": now,
                "updated_at": now,
                "last_seen_reason": "phone_signup_registration",
                "verification_status": "verified",
                "display": phone,
                "is_masked": False,
            },
            "chatgpt_phone_binding": phone_binding,
            "chatgpt_phone_binding_history": [phone_binding],
            "chatgpt_phone_signup": phone_signup,
            "chatgpt_phone_signup_result": panel_result,
            "chatgpt_phone_signup_results": panel_results,
            "phone_signup": phone_signup,
        }
        return Account(
            platform="chatgpt",
            email=account_email,
            password=result.password,
            user_id=result.account_id or result.user_id or "",
            token=result.access_token,
            status=AccountStatus.PENDING_PAYMENT,
            extra=extra,
        )
