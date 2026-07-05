"""Safe logging helpers for ChatGPT task logs.

Runtime code may still need raw proxy/API/token/password/OTP values.  This module
only prepares display/log/history copies and must stay dependency-free.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

REDACTION_VERSION = "chatgpt-task-logging-redaction-v1"
REDACTED = "[REDACTED]"
REDACTED_TOKEN = "[REDACTED_TOKEN]"
REDACTED_OTP = "[REDACTED_OTP]"
REDACTED_URL = "[REDACTED_URL]"

_URL_RE = re.compile(r"https?://[^\s'\"<>]+", re.I)
_PROXY_URL_RE = re.compile(r"(?P<scheme>[a-z][a-z0-9+.-]*://)(?P<userinfo>[^\s/@:]+:[^\s/@]+)@(?P<host>[^\s'\"<>]+)", re.I)
_PHONE_RE = re.compile(r"(?<![A-Za-z0-9])\+\d[\d\s().-]{6,}\d")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")

_TOKEN_KEYS = {
    "token",
    "access_token",
    "accesstoken",
    "refresh_token",
    "refreshtoken",
    "id_token",
    "idtoken",
    "session_token",
    "sessiontoken",
    "bearer",
    "authorization",
    "api_key",
    "apikey",
    "x_api_key",
    "xapikey",
    "api_secret",
    "apisecret",
    "secret",
    "auth_token",
    "authtoken",
    "csrf_token",
    "csrftoken",
    "client_secret",
    "clientsecret",
}
_PASSWORD_KEYS = {
    "password",
    "login_password",
    "loginpassword",
    "chatgpt_phone_signup_password",
    "chatgptphonesignuppassword",
}
_COOKIE_KEYS = {
    "cookie",
    "cookies",
    "cookie_header",
    "cookieheader",
    "set-cookie",
    "setcookie",
    "next_auth_session",
    "nextauthsession",
    "oai-client-auth-session",
    "oaiclientauthsession",
    "login_session",
    "loginsession",
}
_PROXY_KEYS = {"proxy", "proxy_url", "proxyurl", "proxy_used", "proxyused", "specified", "proxy_template", "proxytemplate", "dynamic_proxy_template", "dynamicproxytemplate", "runtime_proxy", "runtimeproxy"}
_URL_KEYS = {
    "api_url",
    "apiurl",
    "detail_url",
    "detailurl",
    "continue_url",
    "continueurl",
    "callback_url",
    "callbackurl",
    "url",
    "endpoint",
    "base_url",
    "baseurl",
}
_FULL_URL_KEYS = {
    "approval_url",
    "approvalurl",
    "provider_redirect_url",
    "providerredirecturl",
    "long_url",
    "longurl",
}
_RAW_LINE_KEYS = {"raw_line", "rawline", "phone_signup_raw_line", "phonesignuprawline", "email_api_line", "emailapiline"}
_TEXT_KEYS = {"reason", "message", "raw_error", "rawerror", "last_error", "lasterror"}
_TEXT_LIST_KEYS = {"logs", "errors", "action_logs", "actionlogs"}
_BODY_KEYS = {"raw_message", "rawmessage", "body", "html", "text", "content"}
_OTP_KEYS = {"otp", "verification_code", "verificationcode", "email_otp", "emailotp", "phone_otp", "phoneotp", "authorization_code", "authorizationcode", "auth_code", "authcode"}


def _norm_key(key: Any) -> str:
    return str(key or "").strip().replace("-", "_").lower()


def _compact_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key or "").lower())


def _looks_sensitive_code(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if re.fullmatch(r"\d{4,8}", text):
        return True
    if len(text) >= 12 and re.fullmatch(r"[A-Za-z0-9._~+/=-]+", text):
        return True
    return False


def _truncate(value: str, limit: int = 240) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return f"{text[: max(0, limit - 40)]}...{text[-30:]}"


def redact_proxy_url(value: Any) -> str:
    """Redact proxy credentials while keeping scheme/host/port for diagnostics."""

    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
        if parts.scheme and parts.netloc:
            hostname = parts.hostname or ""
            port = f":{parts.port}" if parts.port else ""
            if parts.username or parts.password:
                netloc = f"***:***@{hostname}{port}"
            else:
                netloc = f"{hostname}{port}"
            return _truncate(urlunsplit((parts.scheme, netloc, parts.path or "", "", "")), 160)
    except Exception:
        pass
    if "@" in text and ":" in text.split("@", 1)[0]:
        head, tail = text.rsplit("@", 1)
        if "://" in head:
            scheme = head.split("://", 1)[0]
            return _truncate(f"{scheme}://***:***@{tail}", 160)
        return _truncate(f"***:***@{tail}", 160)
    return _truncate(text, 160)


def redact_url(value: Any, *, keep_host: bool = True) -> str:
    """Remove query/fragment and userinfo from URLs.

    For non-URL values this still runs free-text redaction so callers can use it
    as a safe display helper.
    """

    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
        if parts.scheme and parts.netloc:
            if not keep_host:
                return REDACTED_URL
            hostname = parts.hostname or ""
            port = f":{parts.port}" if parts.port else ""
            netloc = f"{hostname}{port}"
            return _truncate(urlunsplit((parts.scheme, netloc, parts.path or "", "", "")), 240)
    except Exception:
        pass
    return _truncate(_redact_text_patterns(text), 240)


def mask_phone_for_log(value: Any) -> str:
    """Mask a phone number for logs while preserving enough prefix/suffix context."""

    text = str(value or "").strip()
    digits = re.sub(r"\D", "", text)
    if not digits:
        return _redact_text_patterns(text)
    prefix = "+" if text.startswith("+") or len(digits) >= 7 else ""
    if len(digits) <= 4:
        return f"{prefix}***"
    if len(digits) <= 6:
        return f"{prefix}{digits[:1]}***{digits[-2:]}"
    if len(digits) <= 10:
        return f"{prefix}{digits[:3]}***{digits[-3:]}"
    return f"{prefix}{digits[:4]}***{digits[-4:]}"


def redact_raw_phone_line(value: Any) -> str:
    """Redact a pasted ``phone----api_url`` line for task display/history."""

    text = str(value or "").strip()
    if not text:
        return ""
    if "----" in text:
        phone, api_url = text.split("----", 1)
        return f"{mask_phone_for_log(phone)}----{redact_url(api_url)}"
    return _redact_text_patterns(text)


def redact_raw_email_api_line(value: Any) -> str:
    """Redact a pasted ``email----api_url`` line while keeping the email visible."""

    text = str(value or "").strip()
    if not text:
        return ""
    parts = re.split(r"-{4,}", text, maxsplit=1)
    if len(parts) == 2:
        email, api_url = parts
        return f"{str(email or '').strip()}----{redact_url(api_url)}"
    return _redact_text_patterns(text)


def _redact_urls_in_text(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        raw = match.group(0)
        trailing = ""
        while raw and raw[-1] in ").,;，。；、]}":
            trailing = raw[-1] + trailing
            raw = raw[:-1]
        try:
            parts = urlsplit(raw)
            if parts.username or parts.password or "***:***@" in raw:
                return redact_proxy_url(raw) + trailing
        except Exception:
            pass
        return redact_url(raw) + trailing

    return _URL_RE.sub(repl, text)


def _redact_otp_context(text: str) -> str:
    contexts = (
        r"验证码|短信码|邮箱码|动态码|一次性代码|授权码|验证\s*OTP\s*码|OTP\s*码|otp\s*码|收码 API 响应不是 JSON|收码 API 返回失败|接码 API|短信 API|SMS API|sms api|OTP|otp|one-time code|verification code|authorization code|auth code"
    )

    def repl(match: re.Match[str]) -> str:
        return f"{match.group(1)}{match.group(2)}{REDACTED_OTP}"

    # Numeric OTP / verification codes.
    text = re.sub(
        rf"({contexts})([^\n\r0-9A-Za-z\u4e00-\u9fff+*]{{0,24}})(\d{{4,8}})(?![-:/]\d)\b",
        repl,
        text,
        flags=re.I,
    )
    # Longer authorization-code-like values.
    text = re.sub(
        rf"({contexts})(\s*[:：=]\s*)([A-Za-z0-9._~+/=-]{{8,}})(?:\.\.\.)?",
        repl,
        text,
        flags=re.I,
    )
    return text


def _redact_key_value_text(text: str, *, expose_otp: bool = False) -> str:
    token_keys = (
        r"access[_-]?token|refresh[_-]?token|id[_-]?token|session[_-]?token|accessToken|sessionToken|csrf[_-]?token|csrf\s+token|token|api[_-]?key|apikey|x[_-]?api[_-]?key|api[_-]?secret|client[_-]?secret|clientSecret|secret"
    )
    password_keys = r"password|login_password|chatgpt_phone_signup_password"
    cookie_keys = r"cookie|cookies|cookie[_-]?header|set-cookie|oai-client-auth-session|login_session|next[_-]?auth[_-]?session|__Secure-next-auth\\.session-token|authjs\\.session-token|oai-did|cf_clearance"
    otp_keys = r"otp|code|verification[_-]?code|phone[_-]?otp|email[_-]?otp|auth[_-]?code|authorization[_-]?code"

    text = re.sub(
        r"(?i)(Authorization\s*[:=]\s*Bearer\s+)[^\s,;}]+",
        rf"\1{REDACTED_TOKEN}",
        text,
    )
    text = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", f"Bearer {REDACTED_TOKEN}", text)
    text = re.sub(
        r"(?i)\b(Authorization|authorization)(\s*[:=]\s*\"?)(?!Bearer\b)[^\"',;}\]\s]+",
        rf"\1\2{REDACTED_TOKEN}",
        text,
    )
    text = re.sub(
        rf"(?i)\b({token_keys})\b(\"?\s*[:=]\s*\"?)[^\"',;}}\]\s]+",
        rf"\1\2{REDACTED_TOKEN}",
        text,
    )
    text = re.sub(
        rf"(?i)([\"']\s*(?:{token_keys})\s*[\"']\s*:\s*[\"'])[^\"']+([\"'])",
        rf"\1{REDACTED_TOKEN}\2",
        text,
    )
    if not expose_otp:
        text = re.sub(
            rf"(?i)([\"']?\b(?:{otp_keys})\b[\"']?\s*[:=]\s*[\"']?)(\d{{4,8}})([\"']?)",
            rf"\1{REDACTED_OTP}\3",
            text,
        )
        text = re.sub(
            rf"(?i)([\"']?\b(?:auth[_-]?code|authorization[_-]?code)\b[\"']?\s*[:=]\s*[\"']?)([A-Za-z0-9._~+/=-]{{8,}})([\"']?)",
            rf"\1{REDACTED_OTP}\3",
            text,
        )
    text = re.sub(
        rf"(?i)\b({password_keys})\b(\"?\s*[:=]\s*\"?)[^\"',;}}\]\s]+",
        rf"\1\2{REDACTED}",
        text,
    )
    text = re.sub(
        rf"(?i)([\"']\s*(?:{password_keys})\s*[\"']\s*:\s*[\"'])[^\"']+([\"'])",
        rf"\1{REDACTED}\2",
        text,
    )
    text = re.sub(
        rf"(?i)\b({cookie_keys})\b(\"?\s*[:=]\s*\"?).*?(?=$|\n|\r)",
        rf"\1\2{REDACTED}",
        text,
    )
    text = re.sub(
        rf"(?i)([\"']\s*(?:{cookie_keys})\s*[\"']\s*:\s*[\"'])[^\"']+([\"'])",
        rf"\1{REDACTED}\2",
        text,
    )
    text = _JWT_RE.sub(REDACTED_TOKEN, text)
    text = re.sub(r"\bac_[A-Za-z0-9._-]{8,}\b", "ac_" + REDACTED_TOKEN, text)
    return text


def _redact_text_patterns(value: Any, *, expose_phone: bool = False, expose_otp: bool = False) -> str:
    text = str(value or "")
    if not text:
        return ""
    text = _PROXY_URL_RE.sub(lambda m: f"{m.group('scheme')}***:***@{m.group('host')}", text)
    text = re.sub(
        r"(?<!\S)(\+?\d[\d().\s-]{6,}\d)----(https?://[^\s'\"<>]+)",
        lambda m: redact_raw_phone_line(m.group(0)),
        text,
    )
    text = _redact_urls_in_text(text)
    text = _redact_key_value_text(text, expose_otp=expose_otp)
    # 先处理 E.164 手机号，再做验证码上下文匹配。否则
    # “发送验证码: +13434832954” 会被长授权码规则误判成 OTP。
    if not expose_phone:
        text = _PHONE_RE.sub(lambda m: mask_phone_for_log(m.group(0)), text)
    if not expose_otp:
        text = _redact_otp_context(text)
    return text


def redact_log_text(value: Any, *, expose_phone: bool = False, expose_otp: bool = False) -> str:
    """Free-text log redaction.  Safe to call at every task-log boundary."""

    return _redact_text_patterns(value, expose_phone=expose_phone, expose_otp=expose_otp)


def sanitize_error_message(value: Any) -> str:
    return redact_log_text(value)


def _sanitize_mapping_item(key: Any, value: Any) -> Any:
    nk = _norm_key(key)
    ck = _compact_key(key)

    if ck in _TOKEN_KEYS:
        return REDACTED_TOKEN if str(value or "") else ""
    if ck in _PASSWORD_KEYS:
        return REDACTED if str(value or "") else ""
    if nk in _COOKIE_KEYS or ck in _COOKIE_KEYS:
        return REDACTED if value not in (None, "") else ""
    if nk in _PROXY_KEYS or ck in _PROXY_KEYS:
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, (Mapping, list, tuple)):
            return sanitize_task_detail(value)
        return redact_proxy_url(value)
    if nk in _FULL_URL_KEYS or ck in _FULL_URL_KEYS:
        return str(value or "")
    if nk in _URL_KEYS or ck in _URL_KEYS:
        return redact_url(value)
    if nk in {"email_api_lines", "emailapi_lines", "email_api_accounts", "emailapiaccounts"} or ck in {"emailapilines", "emailapiaccounts"}:
        if isinstance(value, (list, tuple)):
            return [redact_raw_email_api_line(item) for item in value]
        return "\n".join(redact_raw_email_api_line(line) for line in str(value or "").splitlines())
    if nk in _RAW_LINE_KEYS or ck in _RAW_LINE_KEYS:
        if "email" in nk or "email" in ck:
            return redact_raw_email_api_line(value)
        return redact_raw_phone_line(value)
    if nk == "bound_phone_lines" or ck == "boundphonelines":
        if isinstance(value, (list, tuple)):
            return [redact_raw_phone_line(item) for item in value]
        return redact_raw_phone_line(value)
    if nk == "mailbox_state" or ck == "mailboxstate":
        return summarize_mailbox_state(value)
    if nk in _BODY_KEYS or ck in _BODY_KEYS:
        return REDACTED if value not in (None, "") else ""
    if nk in _TEXT_KEYS or ck in _TEXT_KEYS:
        if isinstance(value, str):
            return redact_log_text(value)
        return sanitize_task_detail(value)
    if nk in _TEXT_LIST_KEYS or ck in _TEXT_LIST_KEYS:
        return sanitize_task_detail(value)
    if nk in _OTP_KEYS or ck in _OTP_KEYS or ck == "code":
        if _looks_sensitive_code(value):
            return REDACTED_OTP
        return sanitize_task_detail(value)
    if nk in {"phone", "phone_number", "phonenumber"}:
        return str(value or "")
    return sanitize_task_detail(value)


def sanitize_task_detail(value: Any) -> Any:
    """Recursively sanitize task detail without changing dict/list shape."""

    if isinstance(value, Mapping):
        return {str(k): _sanitize_mapping_item(k, v) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize_task_detail(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_task_detail(item) for item in value)
    if isinstance(value, str):
        return redact_log_text(value)
    return value


def sanitize_phone_item(item: Mapping[str, Any]) -> dict[str, Any]:
    """Return a display-safe copy of a phone input item."""

    safe = sanitize_task_detail(dict(item or {}))
    phone = str((item or {}).get("phone") or "")
    if phone:
        safe.setdefault("phone", phone)
        safe["phone_masked"] = mask_phone_for_log(phone)
    if "api_url" in item:
        safe["api_url"] = redact_url((item or {}).get("api_url"))
    if "detail_url" in item:
        safe["detail_url"] = redact_url((item or {}).get("detail_url"))
    if "raw_line" in item:
        safe["raw_line"] = redact_raw_phone_line((item or {}).get("raw_line"))
    if "proxy" in item:
        safe["proxy"] = redact_proxy_url((item or {}).get("proxy"))
    return safe


def sanitize_phone_result(item: Mapping[str, Any]) -> dict[str, Any]:
    """Return a display-safe copy of a phone result item."""

    return sanitize_phone_item(item)


def compact_error(
    message: Any,
    *,
    code: str = "",
    phase: str = "",
    retryable: bool | None = None,
    recoverable: bool | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "message": sanitize_error_message(message),
    }
    if code:
        result["code"] = str(code)
    if phase:
        result["phase"] = str(phase)
    if retryable is not None:
        result["retryable"] = bool(retryable)
    if recoverable is not None:
        result["recoverable"] = bool(recoverable)
    return result


def stage_event(flow: str, stage: str, message: str = "", **fields: Any) -> str:
    chunks = [f"[{redact_log_text(flow)}]", f"[{redact_log_text(stage)}]"]
    msg = redact_log_text(message)
    if msg:
        chunks.append(msg)
    for key, value in fields.items():
        safe_value = sanitize_task_detail(value)
        if isinstance(safe_value, (dict, list, tuple)):
            safe_text = redact_log_text(str(safe_value))
        else:
            safe_text = redact_log_text(safe_value)
        chunks.append(f"{key}={safe_text}")
    return " ".join(chunks)


_PHONE_BINDING_MODULE_PREFIX_RE = re.compile(
    r"^\s*\[(?:验证码|手机号验证|号码测试|代理|邮箱|登录|阶段|主链路|注册|结果|TempMailLocal|DEBUG)\]\s*",
    re.I,
)
_PHONE_BINDING_STATUS_FIELD_RE = re.compile(r"(?:^|[｜|，,；;]\s*)状态[:：]\s*([^｜|，,；;]+)")
_PHONE_BINDING_DETAIL_KEYS = (
    "邮箱",
    "账号ID",
    "手机号",
    "手机号序号",
    "来源",
    "尝试",
    "类型",
    "超时",
    "验证码",
    "耗时",
    "原因",
    "处理",
    "结果",
    "字段",
    "手机号状态",
    "号段信号",
    "代理",
    "出口",
    "评分",
    "延迟",
    "渠道",
    "短信时间",
    "恢复时间",
    "HTTP",
    "等待",
    "长度",
    "timeout",
    "source",
    "proxy",
)


def _display_width(value: Any) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in {"F", "W"} else 1 for ch in str(value or ""))


def _pad_display(value: Any, width: int) -> str:
    text = str(value or "")
    return text + (" " * max(int(width or 0) - _display_width(text), 0))


def _clean_phone_binding_body(value: Any) -> str:
    text = str(value or "").strip()
    while True:
        cleaned = _PHONE_BINDING_MODULE_PREFIX_RE.sub("", text)
        if cleaned == text:
            return text
        text = cleaned.strip()


def _normalize_phone_binding_detail(value: Any) -> str:
    text = _clean_phone_binding_body(value)
    if not text:
        return ""
    text = re.sub(r"^使用账号[:：]\s*([^，,｜|]+)", r"邮箱：\1", text)
    text = re.sub(r"([A-Za-z0-9_\u4e00-\u9fa5]+)\s*=\s*", r"\1：", text)
    for raw_key, label in {
        "source": "来源",
        "proxy": "代理",
        "timeout": "超时",
    }.items():
        text = re.sub(rf"\b{raw_key}：", f"{label}：", text, flags=re.I)
    key_pattern = "|".join(re.escape(key) for key in _PHONE_BINDING_DETAIL_KEYS)
    text = re.sub(rf"\s*[，,]\s*(?=(?:{key_pattern})[:：])", "｜", text)
    text = re.sub(rf"\s+(?=(?:{key_pattern})[:：])", "｜", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" ｜|，,；;")


def _remove_phone_binding_status_field(value: str) -> str:
    text = str(value or "")
    match = _PHONE_BINDING_STATUS_FIELD_RE.search(text)
    if not match:
        return text
    detail = f"{text[:match.start()]}{text[match.end():]}"
    return detail.strip(" ｜|，,；;")


def _infer_phone_binding_status(value: Any) -> str:
    text = _clean_phone_binding_body(value)
    lowered = text.lower()
    explicit = _PHONE_BINDING_STATUS_FIELD_RE.search(text)
    if explicit and explicit.group(1):
        return explicit.group(1).strip() or "信息"
    if "phone_already_used" in lowered or "phone number already in use" in lowered or "手机号已被使用" in text:
        return "手机号已用"
    if "rate_limited" in lowered or "限流" in text or "冷却" in text:
        return "限流"
    if "openai 拒绝" in lowered or "拒绝号码" in text or "suspicious" in lowered:
        return "OpenAI拒绝"
    if "api 异常" in lowered or "接口异常" in text or "异常" in text:
        return "异常"
    if "失败" in text or "错误" in text or "未通过" in text or "error" in lowered:
        return "失败"
    if "未收到" in text or "无码" in text or "暂无验证码" in text:
        return "未收到"
    if "等待" in text:
        return "等待"
    if "验证码已获取" in text or "已获取" in text:
        return "已获取"
    if "收到验证码" in text or "已收到" in text:
        return "已收到"
    if "已发码" in text or "已接受手机号" in text or "已接受并发送验证码" in text or "已请求绑定手机号短信验证码" in text:
        return "已发码"
    if "准备提交" in text or "已提交" in text:
        return "已提交"
    if "已选择" in text or "使用代理" in text:
        return "已选择"
    if "已回写" in text or "已写入" in text or "验证通过" in text or "绑定完成" in text or "获取成功" in text or "成功" in text:
        return "成功"
    if "跳过" in text or "无需" in text:
        return "跳过"
    if "开始" in text or "账号id" in lowered or "使用账号" in text or "邮箱" in text:
        return "开始"
    return "信息"


def _tidy_phone_binding_detail_for_status(detail: str, status: str) -> str:
    text = str(detail or "").strip()
    if not text:
        return ""
    if status == "已获取":
        text = re.sub(r"^验证码已获取[:：]?\s*", "", text)
    elif status == "已收到":
        text = re.sub(r"^(?:已收到|收到验证码)[:：]?\s*", "验证码：", text)
    elif status == "已提交":
        text = re.sub(r"^准备提交绑定手机号验证码[:：]?\s*", "验证码：", text)
    return text.strip(" ｜|，,；;")


def _format_phone_binding_timeline_body(message: str, *, stage_tag: str) -> str:
    body = _clean_phone_binding_body(message)
    status = _infer_phone_binding_status(body)
    detail = _tidy_phone_binding_detail_for_status(
        _normalize_phone_binding_detail(_remove_phone_binding_status_field(body)),
        status,
    )
    step_column = _pad_display(stage_tag, 24)
    status_column = _pad_display(status, 10)
    if detail:
        return f"{step_column}  {status_column}  {detail}"
    return f"{step_column}  {status}".rstrip()


def format_task_timeline_log(
    task: str,
    message: str = "",
    *,
    item_index: int | None = None,
    item_total: int | None = None,
    email: str = "",
    account_id: int | str | None = None,
    phone: str = "",
    stage_index: int | None = None,
    stage_total: int | None = None,
    phase_label: str = "",
) -> str:
    """Build a stable human-facing task timeline log line.

    The output intentionally stays plain text so the current frontend keeps
    working, while the wording becomes much more predictable:

    ``[邮箱测活][5/74][a@example.com] 阶段 1/2：登录测活并抓取 AccessToken``
    """

    phone_binding_task = str(task or "").strip() in {"手机绑定", "手机号绑定"}
    expose_phone = phone_binding_task
    expose_otp = phone_binding_task

    def _pad(value: int, total_value: int) -> str:
        width = max(2, len(str(max(int(total_value or 0), 0))))
        return f"{int(value or 0):0{width}d}"

    tags: list[str] = []
    task_label = redact_log_text(task, expose_phone=expose_phone, expose_otp=expose_otp).strip()
    if task_label and not phone_binding_task:
        tags.append(task_label)

    normalized_index = int(item_index or 0)
    normalized_total = int(item_total or 0)
    if phone_binding_task:
        normalized_stage_index = int(stage_index or 0)
        normalized_stage_total = int(stage_total or 0)
        normalized_phase_label = redact_log_text(
            phase_label,
            expose_phone=expose_phone,
            expose_otp=expose_otp,
        ).strip()
        if normalized_stage_index > 0 and normalized_stage_total > 0:
            stage_text = f"步骤{_pad(normalized_stage_index, normalized_stage_total)}/{normalized_stage_total}"
            if normalized_phase_label:
                stage_text = f"{stage_text} {normalized_phase_label}"
            tags.append(stage_text)

        body = redact_log_text(
            message,
            expose_phone=expose_phone,
            expose_otp=expose_otp,
        ).strip()
        stage_tag = f"[{tags[-1]}]" if tags else ""
        if stage_tag:
            return _format_phone_binding_timeline_body(body, stage_tag=stage_tag)
        return body

    if normalized_index > 0 and normalized_total > 0:
        tags.append(f"{normalized_index}/{normalized_total}")

    subject = str(email or "").strip()
    if not subject:
        if phone:
            subject = mask_phone_for_log(phone)
        else:
            account_value = str(account_id or "").strip()
            if account_value:
                subject = f"账号 {account_value}"
    if subject:
        tags.append(redact_log_text(subject))

    body = redact_log_text(message).strip()
    normalized_stage_index = int(stage_index or 0)
    normalized_stage_total = int(stage_total or 0)
    normalized_phase_label = redact_log_text(phase_label).strip()
    if normalized_phase_label:
        if normalized_stage_index > 0 and normalized_stage_total > 0:
            stage_prefix = f"阶段 {normalized_stage_index}/{normalized_stage_total}"
            body = f"{stage_prefix}：{normalized_phase_label}" if not body else f"{stage_prefix}：{normalized_phase_label}；{body}"
        elif not body:
            body = normalized_phase_label
        else:
            body = f"{normalized_phase_label}；{body}"

    prefix = "".join(f"[{tag}]" for tag in tags if tag)
    if prefix and body:
        return f"{prefix} {body}"
    return prefix or body


def build_task_current_state(
    *,
    task: str,
    task_label: str = "",
    item_index: int | None = None,
    item_total: int | None = None,
    email: str = "",
    account_id: int | str | None = None,
    phone: str = "",
    phase: str = "",
    phase_label: str = "",
    stage_index: int | None = None,
    stage_total: int | None = None,
    started_at: str = "",
    last_message: str = "",
    next_step: str = "",
    resource_touched: bool | None = None,
) -> dict[str, Any]:
    """Build a display-safe ``meta.current`` payload for active tasks."""

    payload: dict[str, Any] = {
        "task": str(task or "").strip(),
        "task_label": str(task_label or task or "").strip(),
        "email": str(email or "").strip(),
        "account_id": int(account_id or 0) if str(account_id or "").strip() else 0,
        "phone": mask_phone_for_log(phone) if str(phone or "").strip() else "",
        "phase": str(phase or "").strip(),
        "phase_label": redact_log_text(phase_label).strip(),
        "last_message": redact_log_text(last_message).strip(),
        "next_step": redact_log_text(next_step).strip(),
        "started_at": str(started_at or "").strip(),
    }
    if int(item_index or 0) > 0:
        payload["item_index"] = int(item_index or 0)
    if int(item_total or 0) > 0:
        payload["item_total"] = int(item_total or 0)
    if int(stage_index or 0) > 0:
        payload["stage_index"] = int(stage_index or 0)
    if int(stage_total or 0) > 0:
        payload["stage_total"] = int(stage_total or 0)
    if resource_touched is not None:
        payload["resource_touched"] = bool(resource_touched)
    return payload


def summarize_mailbox_state(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {"has_state": bool(value)}
    account = value.get("account") if isinstance(value.get("account"), Mapping) else {}
    before_ids = value.get("before_ids") or value.get("before_message_ids") or []
    try:
        before_count = len(before_ids)  # type: ignore[arg-type]
    except Exception:
        before_count = 0
    email = str(value.get("email") or account.get("email") or "")
    provider = str(value.get("provider") or value.get("mail_provider") or account.get("provider") or "")
    return {
        "has_state": True,
        "provider": provider,
        "email": email,
        "before_count": before_count,
    }
